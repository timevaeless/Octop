"""WhatsApp extension switch and lifecycle.

The whole WhatsApp support lives inside the octop project as a runtime
injection into ``octop_gateway.channels.BUILTIN_CHANNELS`` (upstream has no
plugin API). This module owns the kill switch so the extension can be turned
off — now or once upstream ships native WhatsApp support — without touching
code:

- Settings key ``channels.whatsapp_extension`` (``"1"``/``"0"``, default on).
- ``install_extension()`` / ``uninstall_extension()`` are idempotent and
  mutate only the in-process registry; DB channel rows are preserved so
  re-enabling restores the previous configuration.
- Webhook and config endpoints check the flag per request, so toggling takes
  effect immediately (no restart).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

SETTING_KEY = "channels.whatsapp_extension"
WHATSAPP_KIND = "whatsapp"
DEFAULT_ENABLED = True

_injected = False


def _read_setting(settings_repo: Any) -> str | None:
    try:
        raw = settings_repo.get(SETTING_KEY)
    except Exception:  # pylint: disable=broad-except
        # Fail open: the default state is enabled, and infra hiccups in the
        # settings table must not silently kill live channels.
        logger.exception("Failed to read whatsapp extension setting; defaulting to enabled")
        return None
    return None if raw is None else str(raw)


def parse_enabled(raw: str | None) -> bool:
    if raw is None or not str(raw).strip():
        return DEFAULT_ENABLED
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def is_enabled(gateway: Any) -> bool:
    """Read the kill switch via the gateway's settings repo."""
    return parse_enabled(_read_setting(gateway._repos.settings_repo))  # noqa: SLF001


def assert_enabled(gateway: Any) -> None:
    """Raise ``ValueError`` when the extension is disabled (API gating)."""
    if not is_enabled(gateway):
        raise ValueError("whatsapp extension is disabled")


def install_extension() -> None:
    """Inject :class:`WhatsAppChannel` into the upstream builtin registry."""
    global _injected  # pylint: disable=global-statement
    if _injected:
        return
    from octop_gateway.channels import BUILTIN_CHANNELS  # noqa: PLC0415

    from octop.infra.gateway.whatsapp.channel import WhatsAppChannel  # noqa: PLC0415

    BUILTIN_CHANNELS[WHATSAPP_KIND] = WhatsAppChannel
    _injected = True
    logger.info("WhatsApp extension installed into BUILTIN_CHANNELS")


def uninstall_extension() -> None:
    """Remove the injection. In-process only; DB rows are kept."""
    global _injected  # pylint: disable=global-statement
    if not _injected:
        return
    from octop_gateway.channels import BUILTIN_CHANNELS  # noqa: PLC0415

    BUILTIN_CHANNELS.pop(WHATSAPP_KIND, None)
    _injected = False
    logger.info("WhatsApp extension removed from BUILTIN_CHANNELS")


def _whatsapp_rows(gateway: Any) -> list[Any]:
    try:
        rows = gateway._repos.channel_repo.list_all(include_disabled=True)  # noqa: SLF001
    except Exception:  # pylint: disable=broad-except
        logger.exception("Failed to list channel rows for whatsapp extension")
        return []
    return [row for row in rows if row.kind == WHATSAPP_KIND]


def filter_boot_rows(gateway: Any, rows: list[Any]) -> list[Any]:
    """Boot hook: drop whatsapp rows (marked ``disabled``) when switched off."""
    if is_enabled(gateway):
        return rows
    keep: list[Any] = []
    for row in rows:
        if row.kind == WHATSAPP_KIND:
            gateway._set_runtime_status(  # noqa: SLF001
                row.channel_id, connected=False, reason="disabled"
            )
            continue
        keep.append(row)
    return keep


async def apply_extension_state(gateway: Any, enabled: bool) -> None:
    """Hot-apply the switch: install/uninstall the registry entry and the
    live channel instances accordingly."""
    if enabled:
        install_extension()
        for row in _whatsapp_rows(gateway):
            if not row.enabled:
                continue
            try:
                await gateway._safe_register_channel(row)  # noqa: SLF001
            except Exception:  # pylint: disable=broad-except
                logger.exception("Failed to re-register whatsapp channel %s", row.channel_id)
        return

    uninstall_extension()
    manager = gateway._channel_manager  # noqa: SLF001
    for row in _whatsapp_rows(gateway):
        if manager is not None:
            try:
                if manager.get_channel(row.channel_id) is not None:
                    await manager.remove_channel(row.channel_id)
            except Exception:  # pylint: disable=broad-except
                logger.exception("Failed to remove whatsapp channel %s", row.channel_id)
        gateway._set_runtime_status(  # noqa: SLF001
            row.channel_id, connected=False, reason="disabled"
        )


def set_enabled(gateway: Any, enabled: bool) -> None:
    """Persist the switch value. Callers invoke :func:`apply_extension_state`."""
    gateway._repos.settings_repo.set(  # noqa: SLF001
        SETTING_KEY, "1" if enabled else "0"
    )
