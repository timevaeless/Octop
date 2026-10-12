"""Expert MCP mount-key health checks (T4 pluggable module, 2026-10-12).

Incident that motivated this (EQFXCA, 2026-10-12): a catalog re-import
rebuilt the connector instance under a new ULID and every expert whose
stored ``mcp_servers`` key referenced the old ULID silently lost its
tools. Dashboard masked it (the composer sends explicit picks each turn)
and IM masked it twice: the turn merge filters unavailable names BEFORE
``prepare_chat_mcp`` can report them, and ``raise_on_failure=False``
swallows the rest — the expert just ran toolless.

Two probes; both fail-open (they observe, never block the host flow —
authorization itself stays in the merge/validate paths):

- :func:`audit_agent_mounts` — hook at ``AgentRegistry._reload_agent``:
  re-validate the expert's stored keys against the owner's currently
  active connectors and persist ``mount_warnings`` into ``config_json``
  (surfaced to clients via ``GET /api/agents/{id}`` ``config``). A stale
  warning is cleared once the keys recover.
- :func:`note_dropped_turn_picks` — hook at
  ``ConnectorService.merge_turn_mcp_servers``: names an expert's composer
  picks lost during the merge because they are no longer available to the
  user. Rate-limited (one warning per key per TTL) because merge runs on
  every turn.

Upstream footprint (user T4 convention — extract, don't weave): two
lazy two-line hooks; deleting them and this module restores upstream
behaviour verbatim.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Rate-limit for the per-turn merge hook: warn at most once per key per TTL.
_DROP_WARN_TTL_SECONDS = 6 * 3600
_drop_warn_at: dict[tuple[int, str], float] = {}


def dead_mount_keys(connector_svc: Any, user_id: int | None, keys: list[str]) -> list[str]:
    """Keys in *keys* that are no longer active servers for *user_id*.

    Fail-open: on transient listing errors this returns ``[]`` — it is a
    health probe, never an authorization decision.
    """
    clean = sorted({str(k).strip() for k in (keys or []) if str(k).strip()})
    if not clean or user_id is None:
        return []
    try:
        allowed = set(connector_svc.list_active_mcp_server_names(user_id))
    except Exception:  # noqa: BLE001 — the probe must never break its host flow
        logger.debug("mount_health: active-server listing failed", exc_info=True)
        return []
    return sorted(k for k in clean if k not in allowed)


def note_dropped_turn_picks(user_id: int, extra_defaults: list[str] | None, allowed: Any) -> None:
    """Warn when expert composer picks were silently dropped by the turn merge.

    ``merge_turn_mcp_servers`` filters expert picks against *allowed* before
    tool loading, so dead keys never reach ``prepare_chat_mcp``'s failure
    report — this restores observability at the exact drop point.
    Rate-limited per ``(user_id, key)``.
    """
    allowed_set = set(allowed or [])
    dropped = sorted(
        {str(k).strip() for k in (extra_defaults or []) if str(k).strip()} - allowed_set
    )
    if not dropped:
        return
    now = time.monotonic()
    fresh = [
        k
        for k in dropped
        if now - _drop_warn_at.get((user_id, k), float("-inf")) > _DROP_WARN_TTL_SECONDS
    ]
    if not fresh:
        return
    for key in fresh:
        _drop_warn_at[(user_id, key)] = now
    logger.warning(
        "expert default MCP keys unavailable user=%s dead=%s (connector instance rebuilt? "
        "re-pick the mounts on the affected experts or migrate their mcp_servers)",
        user_id,
        fresh,
    )


def audit_agent_mounts(
    agent_id: str, row: Any, *, connector_svc: Any, agent_repo: Any
) -> list[str]:
    """Reload-time mount health check; persists ``mount_warnings`` into config_json.

    Never raises and never fails the reload. Returns the dead-key list
    (empty = healthy). Writes only when the warning set changed, and clears
    a previously persisted warning once the keys recover.
    """
    try:
        from octop.infra.agents.settings.profile import id_list_from_row  # noqa: PLC0415

        if row is None or row.user_id is None:
            return []
        if (getattr(row, "kind", None) or "expert") == "team":
            return []
        keys = id_list_from_row(row, "mcp_servers")
        if not keys:
            return []
        dead = dead_mount_keys(connector_svc, row.user_id, keys)
        try:
            cfg = json.loads(row.config_json or "{}")
            if not isinstance(cfg, dict):
                cfg = {}
        except (TypeError, ValueError):
            cfg = {}
        if dead == list(cfg.get("mount_warnings") or []):
            return dead  # unchanged — skip the pointless write on every reload
        if dead:
            cfg["mount_warnings"] = dead
            logger.warning(
                "agent %s mounts dead keys %s (owner user=%s); persisted to config.mount_warnings",
                agent_id,
                dead,
                row.user_id,
            )
        else:
            cfg.pop("mount_warnings", None)  # recovered — clear the stale flag
        agent_repo.update_config(agent_id, config_json=json.dumps(cfg, ensure_ascii=False))
        return dead
    except Exception:  # noqa: BLE001 — the probe must never break reload
        logger.debug("mount_health: audit failed for agent %s", agent_id, exc_info=True)
        return []
