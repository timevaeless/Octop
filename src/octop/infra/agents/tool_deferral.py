"""T4 pluggable progressive tool loading (harness ``ToolSearchMiddleware``).

**Default on**: absent ``config_json["tool_search"]`` enables the feature in
client mode — mounted MCP tools (named ``{server}_{tool}`` after the mount key)
are deferred behind a ``tool_search`` meta-tool, so the model only sees
lightweight name+description references until it searches. The complete tool
set remains registered with the graph, so execution and authorization paths
are untouched. Agents with no mounted MCP server get a ``None`` (zero change).

Opt-outs:
    config_json["tool_search"] = false            -> per-agent off
    config_json["tool_search"] = {"enabled": false} -> per-agent off
    OCTOP_TOOL_SEARCH_DEFAULT=off                 -> global kill switch (env)

Explicit value keeps the original semantics:
    {"mode": "client"|"native", "fallback": "eager"|"error",
     "eager_tools": ["<tool_name>", ...]}

No upstream (octop-harness) code is modified; the middleware is attached
through the existing ``HarnessAgentConfig.middleware`` field.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_VALID_MODES = ("client", "native")
_VALID_FALLBACKS = ("eager", "error")
_OFF_VALUES = {"0", "false", "off", "no"}


def _default_enabled() -> bool:
    """Global kill switch: OCTOP_TOOL_SEARCH_DEFAULT=off restores old eager behavior."""
    return os.getenv("OCTOP_TOOL_SEARCH_DEFAULT", "").strip().lower() not in _OFF_VALUES


@dataclass(frozen=True)
class ToolSearchSettings:
    """Parsed ``config_json["tool_search"]`` section."""

    mode: str = "client"
    fallback: str = "eager"
    eager_tools: frozenset[str] = frozenset()

    @classmethod
    def from_config(cls, raw: Any) -> "ToolSearchSettings | None":
        """Parse the config value; ``None`` means the feature is off."""
        if raw is None or raw is False:
            return None
        if raw is True:
            return cls()
        if isinstance(raw, dict):
            if raw.get("enabled") is False:
                return None
            mode = raw.get("mode", "client")
            if mode not in _VALID_MODES:
                logger.warning("tool_search.mode %r invalid; falling back to client", mode)
                mode = "client"
            fallback = raw.get("fallback", "eager")
            if fallback not in _VALID_FALLBACKS:
                logger.warning("tool_search.fallback %r invalid; falling back to eager", fallback)
                fallback = "eager"
            eager_raw = raw.get("eager_tools")
            eager = (
                frozenset(str(t) for t in eager_raw)
                if isinstance(eager_raw, list)
                else frozenset()
            )
            return cls(mode=mode, fallback=fallback, eager_tools=eager)
        logger.warning("tool_search config %r unsupported; feature stays off", raw)
        return None


class _McpToolNameView:
    """Membership view the harness middleware reads at request time.

    Mounted MCP tools are named ``{server}_{tool}``, so membership is decided
    by mount-key prefix instead of a frozen name list — this keeps dynamically
    appended/replaced MCP tools (lazy load, delegated-token refresh) covered
    without hooking the harness tool mutation methods.

    Always truthy so the middleware constructor keeps the live reference (an
    empty set would be swapped for a frozen empty one and future updates lost).
    """

    def __init__(
        self,
        server_names: Any,
        eager_tools: frozenset[str] = frozenset(),
    ) -> None:
        prefixes = {f"{n}_" for n in server_names if isinstance(n, str) and n}
        self._prefixes: tuple[str, ...] = tuple(sorted(prefixes))
        self._eager = frozenset(eager_tools)

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str) or name in self._eager:
            return False
        return name.startswith(self._prefixes)

    def __bool__(self) -> bool:
        return True


def tool_search_middleware_for(
    cfg: Any,
    mcp_server_names: Any,
) -> Any | None:
    """Build a harness ``ToolSearchMiddleware`` for this agent, or ``None``.

    ``cfg`` is the agent ``config_json`` dict; ``mcp_server_names`` is the
    mounted MCP server config mapping (keys are the mount names). Absent
    config defaults to enabled (client mode); explicit ``false`` or the
    global env kill switch disables it. Returns ``None`` when disabled or
    no MCP server is mounted, so callers can simply skip the append.
    """
    raw = cfg.get("tool_search") if isinstance(cfg, dict) else None
    settings = ToolSearchSettings.from_config(raw)
    if settings is None:
        if raw is not None:
            return None  # explicit opt-out (false / {"enabled": false})
        if not _default_enabled():
            return None  # global kill switch
        settings = ToolSearchSettings()  # default-on: client mode, eager fallback
    servers = [
        n for n in (mcp_server_names or {}) if isinstance(n, str) and n
    ] if hasattr(mcp_server_names, "__iter__") else []
    if not servers:
        return None
    try:
        from octop_harness.middleware.tool_search import ToolSearchMiddleware
    except ImportError:  # pragma: no cover - harness always ships it here
        logger.warning("tool_search enabled but harness middleware unavailable; skipping")
        return None
    view = _McpToolNameView(servers, settings.eager_tools)
    logger.info(
        "tool_search enabled servers=%s mode=%s fallback=%s eager_tools=%d",
        sorted(servers),
        settings.mode,
        settings.fallback,
        len(settings.eager_tools),
    )
    return ToolSearchMiddleware(
        deferred_tools=frozenset(),
        mcp_tool_names=view,  # type: ignore[arg-type]
        defer_mcp_tools=True,
        mode=settings.mode,  # type: ignore[arg-type]
        fallback=settings.fallback,  # type: ignore[arg-type]
    )


__all__ = ["ToolSearchSettings", "tool_search_middleware_for"]
