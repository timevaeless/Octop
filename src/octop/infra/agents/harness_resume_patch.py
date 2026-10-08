"""Runtime patch for octop-harness ``HarnessAgent.resume_hitl``.

Upstream (PyPI octop-harness 1.0.x) rebuilds only
``{"configurable": {"thread_id": ...}}`` when resuming a paused HITL
interrupt, dropping every request-scoped configurable field.
``MCPToolMiddleware`` then resolves zero active MCP servers and strips
all MCP tools from the resumed turn (observed in logs as ``mcp=7->0``
right after a resume request with ``agent=-`` / ``mcp_servers=None``).

This module is a pluggable octop-side fix: it replays the small upstream
method body but merges an ``extra_configurable`` mapping supplied by the
caller (octop injects the agent's default MCP servers). Removing the
hook in ``AgentManager`` restores stock upstream behaviour. Revisit or
delete once the upstream package preserves request configurable fields
on resume.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)

_PATCH_FLAG = "_octop_resume_configurable_patched"


def resume_mcp_configurable(default_servers: list[str] | None) -> dict[str, Any] | None:
    """Configurable extras so a resumed turn keeps its MCP tools mounted.

    Returns ``None`` when the agent has no default servers, so the patched
    resume stays byte-for-byte equivalent to upstream for MCP-less agents.

    Note: the paused turn's per-request composer picks are not persisted,
    so resume restores the agent-level default servers. Agents whose
    connectors are attached on the expert (the common case) are fully
    restored; turn-only picks degrade to the default set.
    """
    servers = [s for s in (default_servers or []) if isinstance(s, str) and s.strip()]
    if not servers:
        return None
    return {"mcp_servers": list(servers)}


def apply_harness_resume_patch() -> bool:
    """Idempotently patch ``HarnessAgent.resume_hitl``.

    Returns ``True`` when the patch was applied now, ``False`` when it was
    already in place. The patched method accepts one additional keyword
    argument, ``extra_configurable``, merged into the resume config's
    ``configurable``; every other upstream behaviour is preserved.
    """
    from octop_harness.agent import HarnessAgent  # noqa: PLC0415

    if getattr(HarnessAgent, _PATCH_FLAG, False):
        return False

    async def resume_hitl(
        self: Any,
        thread_id: str,
        decisions: list[dict[str, Any]],
        *,
        protocol: str | None = None,
        extra_configurable: dict[str, Any] | None = None,
    ) -> AsyncIterator[Any]:
        """Upstream resume_hitl + extra_configurable merge into configurable."""
        from octop_harness.llm.session_header import session_header_scope  # noqa: PLC0415

        proto = self._get_protocol(protocol)
        resume_stream = getattr(proto, "resume_stream", None)
        if resume_stream is None:
            msg = f"Protocol {proto.name!r} does not support HITL resume"
            raise ValueError(msg)
        config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
        if extra_configurable:
            configurable = dict(config["configurable"])
            configurable.update(extra_configurable)
            config["configurable"] = configurable
        async with self._invocation():
            with session_header_scope(thread_id):
                async for chunk in resume_stream(thread_id, decisions, config):
                    yield chunk

    setattr(HarnessAgent, _PATCH_FLAG, True)
    HarnessAgent.resume_hitl = resume_hitl  # type: ignore[method-assign]
    logger.info("patched octop_harness HarnessAgent.resume_hitl: extra_configurable support")
    return True


__all__ = ["apply_harness_resume_patch", "resume_mcp_configurable"]
