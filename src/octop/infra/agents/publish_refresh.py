"""Post-publish refresh of mounted catalog MCP tools (T4 pluggable module).

Problem: catalog-driven connector tools are cached per
``(user_id, server_name, fingerprint)`` in ``AgentManager._mcp_tool_cache``
and the fingerprint covers ONLY the connection spec (transport/url/headers/
command/args/env) — never the tool list or schema. A schema-changing
re-import + re-publish therefore leaves every mounted agent serving the OLD
tools until the delegated token cycles (TTL 30 min, refresh margin 5 min)
or octop restarts.

Decisions owned here (same convention as ``mcp_token_refresh``: the manager
keeps the state, this module owns the decisions):

* which cache entries to drop — every server_name matching the harness-key
  suffix ``__mcp-{service_id}`` (mount keys are
  ``custom__{instance_id}__mcp-{service_id}``, so the suffix is
  collision-proof and works for unpublish too, where the attachment rows
  are already deleted by the time the refresh runs);
* which agents to rebuild — only agents whose default ``mcp_servers``
  mount one of the dropped keys. Per-turn composer picks self-heal through
  the emptied cache at the next ``prepare_chat_mcp`` (cache miss → fresh
  ``aload_mcp_tools`` from the gateway).

Kill switch: env ``OCTOP_MCP_PUBLISH_REFRESH`` in {0,false,off,no} disables
scheduling entirely.

In-flight chats are safe: ``arebuild_agent`` swaps the registry entry and
harness defers resource release while an invocation is in flight — the
running turn finishes on the old tool set, the next turn mounts the fresh
one. A short propagation delay before rebuilding gives nacos →
higress-controller time to apply the new tool schema, so the eager HTTP MCP
load during rebuild reads the NEW schema.

Deleting this module together with the three router hooks
(publish / unpublish / reconcile in ``mcp_catalog/router.py``) restores
upstream behaviour verbatim.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_KILL_SWITCH_ENV = "OCTOP_MCP_PUBLISH_REFRESH"
_OFF_VALUES = frozenset({"0", "false", "off", "no"})

# nacos config write returns before higress-controller applies it; rebuilding
# agents too early would eagerly load the OLD tool schema back into the (now
# empty) cache under the unchanged fingerprint.
_GATEWAY_PROPAGATION_DELAY_SECONDS = 3.0

__all__ = [
    "match_server_name",
    "refresh_after_publish",
    "refresh_enabled",
    "schedule_refresh",
]


def refresh_enabled() -> bool:
    """Env kill switch: default on; {0,false,off,no} turns it off."""
    raw = os.environ.get(_KILL_SWITCH_ENV, "").strip().lower()
    return raw not in _OFF_VALUES


def match_server_name(server_name: str, service_id: str) -> bool:
    """Whether a harness mount key belongs to ``service_id``.

    Mount keys are ``custom__{instance_id}__mcp-{service_id}``
    (:func:`octop.mcp_catalog.tenant_share.harness_key_for_row`); the
    ``__mcp-`` prefix in the suffix guards against ULID-ish id collisions.
    """
    return str(server_name).endswith(f"__mcp-{service_id}")


def _drop_cache_for_service(registry: Any, service_id: str) -> list[str]:
    """Drop cached tool wrappers (and token-expiry records) for one service.

    Returns the dropped server names. Best-effort by construction: the
    attributes exist on the real registry; fakes/partial doubles skip.
    """
    dropped: set[str] = set()
    cache = getattr(registry, "_mcp_tool_cache", None)
    if isinstance(cache, dict):
        stale = [k for k in cache if len(k) == 3 and match_server_name(k[1], service_id)]
        for key in stale:
            dropped.add(str(key[1]))
            del cache[key]
    expiry = getattr(registry, "_mcp_token_expiry", None)
    if isinstance(expiry, dict):
        stale_exp = [k for k in expiry if len(k) == 2 and match_server_name(k[1], service_id)]
        for key in stale_exp:
            del expiry[key]
    return sorted(dropped)


def _default_mount_agent_ids(registry: Any, targets: set[str]) -> list[str]:
    """Agent ids whose default ``mcp_servers`` mount one of ``targets``."""
    if not targets:
        return []
    from octop.infra.agents.settings.profile import id_list_from_row  # noqa: PLC0415

    ids: list[str] = []
    rows = registry._repos.agent_repo.list_all(include_disabled=False)
    for row in rows:
        if _is_team_row(row):
            continue
        if set(id_list_from_row(row, "mcp_servers")) & targets:
            ids.append(row.agent_id)
    return ids


def _is_team_row(row: Any) -> bool:
    try:
        from octop.infra.agents.teams import is_team_agent  # noqa: PLC0415

        return bool(is_team_agent(row))
    except Exception:  # noqa: BLE001 — partial test doubles must not break refresh
        return False


async def refresh_after_publish(server: Any, service_id: str) -> None:
    """Invalidate cached tools and rebuild default-mounting agents (async).

    Runs on the event loop (via BackgroundTasks after the response, or a
    created task). Failures are logged, never raised — a refresh miss must
    not fail an already-successful publish.
    """
    runtime = getattr(server, "app_runtime", None)
    registry = getattr(runtime, "agent_registry", None)
    if registry is None:
        logger.info("publish refresh %s: no agent registry; skipping", service_id)
        return
    await asyncio.sleep(_GATEWAY_PROPAGATION_DELAY_SECONDS)
    try:
        dropped = _drop_cache_for_service(registry, service_id)
        agent_ids = _default_mount_agent_ids(registry, set(dropped))
        if not dropped and not agent_ids:
            logger.info("publish refresh %s: no mounted tools to refresh", service_id)
            return
        logger.info(
            "publish refresh %s: dropped cache servers=%s rebuilding agents=%s",
            service_id,
            dropped,
            agent_ids,
        )
        reload = getattr(registry, "_reload_agents", None)
        if callable(reload) and agent_ids:
            await reload(agent_ids)
    except Exception:  # noqa: BLE001 — refresh is best-effort by design
        logger.exception("publish refresh failed for service %s", service_id)


def schedule_refresh(server: Any, service_id: str, background_tasks: Any = None) -> None:
    """Fire-and-forget hook entry, safe from sync endpoints.

    Preferred: pass the endpoint's Starlette ``BackgroundTasks`` — an async
    task callable is awaited on the event loop after the response, so the
    sync-endpoint threadpool (no running loop) is not a problem.
    Fallback: schedule via ``create_task`` when a loop is already running.
    """
    if not refresh_enabled():
        logger.info(
            "publish refresh disabled via %s; skipping %s", _KILL_SWITCH_ENV, service_id
        )
        return
    if background_tasks is not None:
        background_tasks.add_task(refresh_after_publish, server, service_id)
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "publish refresh %s: no BackgroundTasks and no running loop; skipping",
            service_id,
        )
        return
    loop.create_task(refresh_after_publish(server, service_id))
