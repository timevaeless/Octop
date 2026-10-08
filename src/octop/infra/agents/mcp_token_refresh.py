"""Delegated-token refresh logic for mounted MCP tools (T3, pluggable
module).

User convention 2026-09-26 (extract, don't weave). The manager keeps the
expiry *state* (``_mcp_token_expiry``, same class of attribute as the
community ``_mcp_tool_cache``); this module owns the *decisions*:

* :func:`delegation_expiring` — whether a mounted server's delegated token
  entered the refresh margin (per-agent by construction, review
  2026-09-26 P1-5: the authoritative source is the spec mounted on *this*
  agent, so agent A's renewal cannot mark agent B's stale connection as
  fresh; the store remains as fallback for mounts predating the per-agent
  config stamp);
* :func:`replace_server_tools` — swap one server's mounted tools on a live
  agent (``append_mcp_tools`` dedupes by name, so a refresh must go through
  a full-list replace).

Upstream touch points (``infra/agents/manager.py``): the two lazy delegate
methods ``_mcp_delegation_expiring`` / ``_replace_server_mcp_tools``.
Deleting this module together with those delegates restores community
behaviour verbatim: long-running chats lose the self-heal path and would
fail once mcp-hub rejects the expired delegated token.
"""

from __future__ import annotations

import time
from typing import Any

__all__ = ["delegation_expiring", "replace_server_tools"]


def delegation_expiring(
    expiry_store: dict[tuple[int, str], float | None],
    user_id: int | None,
    server_name: str,
    agent: Any = None,
) -> bool:
    """True when the mounted delegated token enters the refresh margin.

    Only delegated-identity connections carry an expiry: static-auth or
    unauthenticated servers record ``None`` and are never refreshed.
    """
    if user_id is None:
        return False
    from octop.infra.connectors.delegation import refresh_margin_seconds, spec_token_exp

    exp: float | None = None
    configs = getattr(getattr(agent, "config", None), "mcp_server_configs", None)
    spec = configs.get(server_name) if isinstance(configs, dict) else None
    if isinstance(spec, dict):
        exp = spec_token_exp(spec)
    if exp is None:
        exp = (expiry_store or {}).get((user_id, server_name))
    if exp is None:
        return False
    return bool(exp - time.time() < refresh_margin_seconds())


def replace_server_tools(agent: Any, server_name: str, tools: list[Any]) -> None:
    """Swap one MCP server's mounted tools on a live agent.

    ``append_mcp_tools`` dedupes by name, so refreshing a connection whose
    tools are already mounted must go through a full-list replace (drop the
    server's old tools, insert the freshly loaded ones).
    """
    old = list(getattr(agent, "_mcp_tools", None) or [])
    prefix = f"{server_name}_"
    kept = [t for t in old if not str(getattr(t, "name", "") or "").startswith(prefix)]
    agent.replace_mcp_tools(kept + list(tools))
