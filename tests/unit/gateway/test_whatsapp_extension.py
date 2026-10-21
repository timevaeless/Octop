"""Unit tests for the in-project WhatsApp channel extension."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from octop.infra.gateway.whatsapp import (
    SETTING_KEY,
    WhatsAppChannel,
    WhatsAppConfig,
    apply_extension_state,
    filter_boot_rows,
    install_extension,
    is_enabled,
    parse_enabled,
    set_enabled,
    uninstall_extension,
)
from octop.infra.gateway.whatsapp.webhook import (
    split_webhook_events,
    verify_signature,
)

# ---------------------------------------------------------------------------
# Registry injection
# ---------------------------------------------------------------------------


def test_install_uninstall_injection() -> None:
    from octop_gateway.channels import BUILTIN_CHANNELS

    try:
        install_extension()
        assert BUILTIN_CHANNELS["whatsapp"] is WhatsAppChannel
        assert "whatsapp" in BUILTIN_CHANNELS
    finally:
        uninstall_extension()
    assert "whatsapp" not in BUILTIN_CHANNELS


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_missing_credentials() -> None:
    cfg = WhatsAppConfig.from_dict(
        {"phone_number_id": "123", "access_token": "tk", "verify_token": "vt", "app_secret": "s"}
    )
    assert cfg.missing_credentials() == []
    empty = WhatsAppConfig.from_dict({})
    assert set(empty.missing_credentials()) == {
        "phone_number_id",
        "access_token",
        "verify_token",
        "app_secret",
    }


# ---------------------------------------------------------------------------
# Webhook signature + payload split
# ---------------------------------------------------------------------------


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature_ok_and_bad() -> None:
    body = b'{"entry":[]}'
    assert verify_signature(body, "secret", _sign("secret", body))
    assert not verify_signature(body, "secret", _sign("wrong", body))
    assert not verify_signature(body, "secret", "sha256=deadbeef")
    assert not verify_signature(body, "", _sign("secret", body))
    assert not verify_signature(body, "secret", None)


def _meta_payload() -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "8613800000000", "phone_number_id": "PN1"},
                            "contacts": [
                                {"profile": {"name": "Alice"}, "wa_id": "8613900000000"}
                            ],
                            "messages": [
                                {"from": "8613900000000", "id": "wamid.1", "timestamp": "1700000000", "type": "text", "text": {"body": "hi"}},
                                {"from": "8613900000000", "id": "wamid.2", "timestamp": "1700000001", "type": "image", "image": {"id": "media1", "mime_type": "image/jpeg"}},
                            ],
                        },
                    }
                ],
            }
        ],
    }


def test_split_webhook_events() -> None:
    events = split_webhook_events(_meta_payload())
    assert len(events) == 2
    first = events[0]
    assert first["message"]["type"] == "text"
    assert first["contact"]["wa_id"] == "8613900000000"
    assert first["value"]["metadata"]["phone_number_id"] == "PN1"
    assert split_webhook_events({"object": "whatsapp_business_account"}) == []


# ---------------------------------------------------------------------------
# parse_inbound
# ---------------------------------------------------------------------------


def _channel() -> WhatsAppChannel:
    cfg = WhatsAppConfig(
        phone_number_id="PN1", access_token="tk", verify_token="vt", app_secret="s"
    )
    async def _proc(msg):  # pragma: no cover - stub
        yield None

    return WhatsAppChannel(_proc, config=cfg, channel_id="ch-wa", tenant_id="agent1")


def test_parse_inbound_route_split_text() -> None:
    ch = _channel()
    msg = {"from": "8613900000000", "id": "wamid.1", "timestamp": "1700000000", "type": "text", "text": {"body": "hi"}}
    raw = {
        "value": {"metadata": {"phone_number_id": "PN1", "display_phone_number": "8613800000000"}},
        "message": msg,
        "contact": {"profile": {"name": "Alice"}, "wa_id": "8613900000000"},
    }
    inbound = ch.parse_inbound(raw)
    assert inbound is not None
    assert inbound.channel_id == "ch-wa"
    assert inbound.tenant_id == "agent1"
    assert inbound.channel_subject is not None
    assert inbound.channel_subject.subject_id == "8613900000000"
    assert inbound.channel_subject.display_name == "Alice"
    assert inbound.metadata["wa_id"] == "8613900000000"
    assert inbound.timestamp == 1700000000.0


def test_parse_inbound_media_uses_marker_url() -> None:
    ch = _channel()
    msg = {"from": "wa1", "id": "m2", "type": "image", "image": {"id": "media1", "mime_type": "image/jpeg"}}
    raw = {"value": {"metadata": {}}, "message": msg}
    inbound = ch.parse_inbound(raw)
    assert inbound is not None
    assert inbound.content[0].url == "wa-media://media1"
    assert inbound.content[0].mime_type == "image/jpeg"


def test_parse_inbound_rejects_garbage() -> None:
    ch = _channel()
    assert ch.parse_inbound({"foo": 1}) is None
    assert ch.parse_inbound("not-a-dict") is None
    assert ch.parse_inbound({"value": {}, "message": {"type": "reaction"}}) is None


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def _gateway_with_setting(tmp_path: Path, value: str | None) -> MagicMock:
    gw = MagicMock()
    repo = MagicMock()
    repo.get.return_value = value
    gw._repos.settings_repo = repo
    return gw


def test_parse_enabled_default_and_values() -> None:
    assert parse_enabled(None) is True
    assert parse_enabled("") is True
    assert parse_enabled("1") is True
    assert parse_enabled("true") is True
    assert parse_enabled("0") is False
    assert parse_enabled("false") is False


def test_is_enabled_reads_settings(tmp_path: Path) -> None:
    gw = _gateway_with_setting(tmp_path, None)
    assert is_enabled(gw) is True
    set_enabled(gw, False)
    write_call = gw._repos.settings_repo.set.call_args
    assert write_call.args == (SETTING_KEY, "0")
    gw._repos.settings_repo.get.return_value = "0"
    assert is_enabled(gw) is False


@pytest.mark.asyncio
async def test_apply_state_uninstall_removes_live_instances(tmp_path: Path) -> None:
    gw = _gateway_with_setting(tmp_path, "1")
    row = MagicMock()
    row.channel_id = "ch-wa"
    row.kind = "whatsapp"
    row.enabled = 1
    gw._repos.channel_repo.list_all.return_value = [row]
    manager = MagicMock()
    manager.get_channel.return_value = object()
    manager.remove_channel = AsyncMock()
    gw._channel_manager = manager
    install_extension()

    await apply_extension_state(gw, False)

    manager.remove_channel.assert_awaited_once_with("ch-wa")
    gw._set_runtime_status.assert_called_once()
    assert gw._set_runtime_status.call_args.kwargs["reason"] == "disabled"


def test_filter_boot_rows_drops_whatsapp_when_disabled(tmp_path: Path) -> None:
    gw = _gateway_with_setting(tmp_path, "0")
    wa_row = MagicMock()
    wa_row.kind = "whatsapp"
    wa_row.channel_id = "ch-wa"
    other_row = MagicMock()
    other_row.kind = "telegram"
    kept = filter_boot_rows(gw, [wa_row, other_row])
    assert [r.kind for r in kept] == ["telegram"]
    assert gw._set_runtime_status.call_args.kwargs["reason"] == "disabled"


def test_filter_boot_rows_keeps_all_when_enabled(tmp_path: Path) -> None:
    gw = _gateway_with_setting(tmp_path, "1")
    rows = [MagicMock(), MagicMock()]
    assert filter_boot_rows(gw, rows) is rows


def test_webhook_payload_roundtrip_matches_parse(tmp_path: Path) -> None:
    """The route-split payload fed to enqueue() must parse back cleanly."""
    ch = _channel()
    for event in split_webhook_events(_meta_payload()):
        inbound = ch.parse_inbound(event)
        assert inbound is not None
        assert inbound.channel_subject is not None
        assert inbound.channel_subject.subject_id == "8613900000000"
