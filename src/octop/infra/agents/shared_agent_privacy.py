"""Shared-agent workspace privacy policy (pluggable module).

Cross-tenant shared agents (``org_id='*'``) run as ONE harness instance whose
workspace — including the ``.octop`` daily session JSONL and the memory store —
is shared by every caller. Two cross-tenant information-flow channels were
confirmed by live probing (verification round 11, 2026-09-28):

* the daily ``.octop/sessions/YYYY-MM-DD.jsonl`` mixes every tenant's events
  (content, thread_id, user id, token usage) and is readable by the model's
  file tools (no ``.octop`` exclusion exists in harness);
* harness memory recall is instance-scoped (``Memory.recall`` has no
  thread/session filter), so atoms extracted from one tenant's conversations
  can be recalled into another tenant's context.

Until per-tenant runtime isolation lands (Phase 2: ``(agent_id, tenant)``
instance keying — see docs/branch-feature-verification-2026-09-27.md), this
module fail-closes both channels by disabling harness memory and the
workspace session log for shared agents only. Tenant-owned agents keep the
upstream behavior untouched.

Pluggability: removing the single hook in
``AgentManager._build_harness_config`` (and this module) restores the
community/original harness behavior exactly.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from octop.infra.users.resource_acl import PLATFORM_SHARED_ORG

logger = logging.getLogger(__name__)


def is_shared_agent_row(row: Any) -> bool:
    """True when *row* is a cross-tenant platform-shared agent."""
    org_id = getattr(row, "org_id", None)
    return isinstance(org_id, str) and org_id == PLATFORM_SHARED_ORG


def apply_shared_agent_privacy(
    config: Any,
    *,
    supported_fields: frozenset[str] | set[str],
) -> Any:
    """Return *config* with workspace memory and session log disabled.

    Only touches fields the installed harness-agent actually supports, so
    older harness versions keep working. With ``memory_enabled=False`` the
    MemoryService is never constructed and with ``session_log_enabled=False``
    the daily JSONL writer is off — harness skips the whole MemoryMiddleware
    (``MemoryRuntime.build_middleware`` returns ``None``).
    """
    updates: dict[str, Any] = {}
    if "memory_enabled" in supported_fields:
        updates["memory_enabled"] = False
    if "session_log_enabled" in supported_fields:
        updates["session_log_enabled"] = False
    if not updates:
        return config
    logger.info(
        "shared-agent privacy: disabling workspace memory/session log (fields: %s)",
        ", ".join(sorted(updates)),
    )
    return dataclasses.replace(config, **updates)


__all__ = ["apply_shared_agent_privacy", "is_shared_agent_row"]
