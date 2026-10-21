"""WhatsApp Cloud API channel extension.

Runtime-injected into ``octop_gateway.channels.BUILTIN_CHANNELS`` so WhatsApp
support lives entirely inside the octop project without patching the
upstream ``octop-gateway`` package. See :mod:`extension` for the kill switch
(``channels.whatsapp_extension`` settings key).
"""

from __future__ import annotations

from octop.infra.gateway.whatsapp.channel import (
    WHATSAPP_CHANNEL_TYPE,
    WhatsAppChannel,
    WhatsAppConfig,
)
from octop.infra.gateway.whatsapp.extension import (
    SETTING_KEY,
    apply_extension_state,
    assert_enabled,
    filter_boot_rows,
    install_extension,
    is_enabled,
    parse_enabled,
    set_enabled,
    uninstall_extension,
)

__all__ = [
    "SETTING_KEY",
    "WHATSAPP_CHANNEL_TYPE",
    "WhatsAppChannel",
    "WhatsAppConfig",
    "apply_extension_state",
    "assert_enabled",
    "filter_boot_rows",
    "install_extension",
    "is_enabled",
    "parse_enabled",
    "set_enabled",
    "uninstall_extension",
]
