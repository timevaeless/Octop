"""WhatsApp webhook callback and extension switch endpoints.

``webhook_router`` is JWT-exempt (machine-to-machine from Meta); every event
is authenticated by the ``X-Hub-Signature-256`` HMAC of the channel's
``app_secret``. ``admin_router`` toggles the extension switch and stays
behind the normal JWT + permission gate.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

from octop.api.deps import get_server, require_permission
from octop.infra.gateway.whatsapp.extension import (
    WHATSAPP_KIND,
    apply_extension_state,
    is_enabled,
    set_enabled,
)

logger = logging.getLogger(__name__)

webhook_router = APIRouter()
admin_router = APIRouter()

#: Reject oversized webhook bodies before parsing.
_MAX_WEBHOOK_BODY_BYTES = 1024 * 1024


def _gateway_for(server: Any) -> Any:
    gateway = getattr(getattr(server, "app_runtime", None), "gateway", None)
    if gateway is None:
        raise HTTPException(status_code=503, detail="gateway not booted")
    return gateway


def _whatsapp_row(gateway: Any, channel_id: str) -> Any:
    row = gateway.get_channel(channel_id)
    if row is None or row.kind != WHATSAPP_KIND:
        # Same 404 for unknown ids and wrong kinds: do not leak existence.
        raise HTTPException(status_code=404, detail="channel not found")
    return row


def _config_of(row: Any) -> dict[str, Any]:
    try:
        config = json.loads(row.config_json or "{}")
    except json.JSONDecodeError:
        config = {}
    return config if isinstance(config, dict) else {}


def verify_signature(raw: bytes, app_secret: str, header: str | None) -> bool:
    """Constant-time ``X-Hub-Signature-256`` check (``sha256=<hex>``)."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", header)


def split_webhook_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Split one Meta webhook body into per-message enqueue payloads."""
    events: list[dict[str, Any]] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue
            contacts = value.get("contacts") or []
            contact_map = {
                c.get("wa_id"): c for c in contacts if isinstance(c, dict)
            }
            for message in value.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                events.append(
                    {
                        "value": {"metadata": value.get("metadata") or {}, "contacts": contacts},
                        "message": message,
                        "contact": contact_map.get(message.get("from")),
                    }
                )
    return events


@webhook_router.get("/channels/whatsapp/webhook/{channel_id}")
async def whatsapp_webhook_verify(
    channel_id: str,
    request: Request,
    server: Any = Depends(get_server),
) -> PlainTextResponse:
    """Meta subscription handshake: echo ``hub.challenge`` on token match."""
    gateway = _gateway_for(server)
    if not is_enabled(gateway):
        raise HTTPException(status_code=404, detail="channel not found")
    row = _whatsapp_row(gateway, channel_id)
    config = _config_of(row)
    params = request.query_params
    if (
        params.get("hub.mode") != "subscribe"
        or not config.get("verify_token")
        or params.get("hub.verify_token") != config.get("verify_token")
    ):
        raise HTTPException(status_code=403, detail="verification failed")
    challenge = params.get("hub.challenge") or ""
    return PlainTextResponse(challenge)


@webhook_router.post("/channels/whatsapp/webhook/{channel_id}")
async def whatsapp_webhook_receive(
    channel_id: str,
    request: Request,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Receive Meta events: verify HMAC, split, enqueue onto the channel."""
    gateway = _gateway_for(server)
    raw = await request.body()
    if len(raw) > _MAX_WEBHOOK_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")

    if not is_enabled(gateway):
        raise HTTPException(status_code=404, detail="channel not found")
    row = _whatsapp_row(gateway, channel_id)
    config = _config_of(row)
    if not verify_signature(raw, str(config.get("app_secret") or ""), request.headers.get("x-hub-signature-256")):
        raise HTTPException(status_code=403, detail="invalid signature")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid json")

    manager = gateway._channel_manager  # noqa: SLF001
    if manager is None:
        # 503 so Meta retries after the gateway finished booting.
        raise HTTPException(status_code=503, detail="gateway not ready")

    events = split_webhook_events(payload)
    for event in events:
        manager.enqueue(channel_id, event)
    return {"received": True, "count": len(events)}


@admin_router.get("/channels/whatsapp/extension")
async def get_whatsapp_extension(
    server: Any = Depends(get_server),
    user: Any = Depends(require_permission("channels")),
) -> dict[str, Any]:
    """Report whether the WhatsApp extension is enabled."""
    return {"enabled": is_enabled(_gateway_for(server))}


@admin_router.post("/channels/whatsapp/extension")
async def set_whatsapp_extension(
    server: Any = Depends(get_server),
    user: Any = Depends(require_permission("channels")),
    body: dict[str, bool] = Body(default=None),
) -> dict[str, Any]:
    """Toggle the extension: persist the flag and hot-apply runtime state."""
    enabled = bool((body or {}).get("enabled", True))
    gateway = _gateway_for(server)
    set_enabled(gateway, enabled)
    await apply_extension_state(gateway, enabled)
    logger.info("WhatsApp extension toggled: enabled=%s", enabled)
    return {"enabled": is_enabled(gateway)}
