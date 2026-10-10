"""OCR tool plugin — thin registration shell.

All implementation lives in the octop-side bridge module
(``octop.infra.agents.plugins.ocr_bridge``) so the plugin can be unplugged
without trace (T4): delete this directory, the bridge module and its
server.py hook lines, and the community tree is restored.
"""

from __future__ import annotations

import logging

from octop_harness.plugins import PluginContext

logger = logging.getLogger(__name__)


def setup(ctx: PluginContext) -> None:
    try:
        from octop.infra.agents.plugins.ocr_bridge import register_ocr_tool
    except Exception:  # pragma: no cover - embedded contexts without octop runtime
        logger.warning("ocr plugin: octop bridge unavailable, tool not registered")
        return
    register_ocr_tool(ctx)


__all__ = ["setup"]
