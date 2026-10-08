"""Octop-owned behaviour patches declared against upstream callables.

Every entry is registered in :mod:`octop.ext.patches`' registry and installed
once by ``octop.api.app`` at startup. Targets are dotted paths into possibly
upstream code; a target that upstream renamed or moved fails LOUDLY at boot
(:class:`octop.ext.patches.PatchError`) instead of silently skipping — fix or
drop the entry then.

Removing an entry together with its implementation module restores the
upstream behaviour verbatim: no upstream file carries a matching hook.
"""

from __future__ import annotations

from typing import Any

from octop.ext.patches import before
from octop.infra.errors import OctopError


@before("octop.infra.agents.manager:AgentManager.delete_thread_checkpoint")
async def _warm_checkpointer_before_delete(
    self: Any, agent_id: str, thread_id: str
) -> None:
    """Checkpointer warm-up before thread deletion.

    Was woven into ``AgentManager.delete_thread_checkpoint`` (2026-09-28):
    after a restart the default ``AsyncSqliteSaver`` connection is still
    lazily unopened, and langgraph's ``adelete_thread`` — unlike every other
    saver op — does not ensure the connection first, so deleting a thread
    before the agent's first turn raised ``ValueError("no active
    connection")``. See :mod:`octop.infra.agents.checkpointer_warmup`.

    Mirrors the in-method guards: only warm when the agent is running and its
    harness actually exposes ``adelete_thread`` — otherwise the original
    method returns ``False`` without touching any connection.
    """
    from octop.infra.agents.checkpointer_warmup import warm_up_checkpointer

    try:
        harness = self.get_agent(agent_id)
    except OctopError:
        # Original method logs and returns False in this state; no connection
        # will be touched, so there is nothing to warm.
        return
    if getattr(harness, "adelete_thread", None) is None:
        return
    await warm_up_checkpointer(harness)
