"""Thin async client for the WhatsApp Cloud API (Meta Graph API).

All functions take an ``aiohttp.ClientSession`` owned by the channel and a
long-lived system-user access token. Errors are raised as
:class:`WhatsAppApiError` with the HTTP status attached so callers can map
probe failures to stable diagnostic codes.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v21.0"
DEFAULT_GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

#: Hosts serving business media downloads; requests to them need the Bearer token.
MEDIA_DOWNLOAD_HOSTS = frozenset(
    {"lookaside.fbsbx.com", "lookaside.intl.fbsbx.com", "graph.facebook.com"}
)

# WhatsApp Cloud API per-media upload ceilings (bytes).
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_VIDEO_BYTES = 16 * 1024 * 1024
MAX_AUDIO_BYTES = 16 * 1024 * 1024
MAX_DOCUMENT_BYTES = 100 * 1024 * 1024


class WhatsAppApiError(RuntimeError):
    """Graph API call failure with the HTTP status attached."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"whatsapp graph api {status}: {detail}")


async def graph_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    token: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Perform one Graph API call and return the decoded JSON body."""
    headers = {"Authorization": f"Bearer {token}"}
    async with session.request(
        method, url, headers=headers, json=json_body, params=params
    ) as resp:
        raw = await resp.text()
        if resp.status >= 400:
            raise WhatsAppApiError(resp.status, raw[:500])
        try:
            data: dict[str, Any] = await resp.json(content_type=None)
        except Exception as exc:  # pylint: disable=broad-except
            raise WhatsAppApiError(resp.status, f"non-json response: {raw[:200]}") from exc
        return data


async def verify_credentials(
    session: aiohttp.ClientSession,
    graph_url: str,
    phone_number_id: str,
    token: str,
) -> None:
    """Validate ``phone_number_id`` + token; raise on any failure.

    Used by ``WhatsAppChannel.start()`` which doubles as the credential probe.
    """
    url = f"{graph_url.rstrip('/')}/{phone_number_id}"
    await graph_request(session, "GET", url, token, params={"fields": "verified_name,quality_rating"})


async def send_message(
    session: aiohttp.ClientSession,
    graph_url: str,
    phone_number_id: str,
    token: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """POST one outbound message to ``/{phone_number_id}/messages``."""
    url = f"{graph_url.rstrip('/')}/{phone_number_id}/messages"
    return await graph_request(session, "POST", url, token, json_body=payload)


async def upload_media(
    session: aiohttp.ClientSession,
    graph_url: str,
    phone_number_id: str,
    token: str,
    data: bytes,
    mime_type: str,
    filename: str,
) -> str:
    """Upload bytes to the media API and return the server-assigned media id."""
    url = f"{graph_url.rstrip('/')}/{phone_number_id}/media"
    form = aiohttp.FormData()
    form.add_field("messaging_product", "whatsapp")
    form.add_field(
        "file",
        data,
        filename=filename or "file",
        content_type=mime_type or "application/octet-stream",
    )
    headers = {"Authorization": f"Bearer {token}"}
    async with session.post(url, headers=headers, data=form) as resp:
        raw = await resp.text()
        if resp.status >= 400:
            raise WhatsAppApiError(resp.status, raw[:500])
        body: dict[str, Any] = await resp.json(content_type=None)
    media_id = str(body.get("id") or "")
    if not media_id:
        raise WhatsAppApiError(resp.status, f"media upload returned no id: {raw[:200]}")
    return media_id


async def resolve_media_url(
    session: aiohttp.ClientSession,
    graph_url: str,
    media_id: str,
    token: str,
) -> tuple[str, str]:
    """Resolve a media id to ``(download_url, mime_type)``."""
    url = f"{graph_url.rstrip('/')}/{media_id}"
    data = await graph_request(session, "GET", url, token)
    download_url = str(data.get("url") or "")
    if not download_url:
        raise WhatsAppApiError(404, f"media {media_id} has no downloadable url")
    return download_url, str(data.get("mime_type") or "")
