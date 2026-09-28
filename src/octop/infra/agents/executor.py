"""Agent execution seam — the single surface for turn execution.

Introduced in phase 1 of the microservice scaling plan
(``docs/microservice-scaling-1000.md``): gateway / cron / team call sites
invoke turns through :class:`AgentExecutor` instead of touching
``AgentManager`` directly. The default executor is
:class:`InProcessExecutor` (behavior-identical delegation, resolved at each
consumer's constructor via ``executor or InProcessExecutor(agent_manager)``);
in phase 2 an API-role deployment injects a queue-backed remote executor at
those construction sites without touching any call site.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from octop.infra.agents.manager import AgentManager

__all__ = ["AgentExecutor", "InProcessExecutor", "cancel_turn"]


@runtime_checkable
class AgentExecutor(Protocol):
    """Turn-execution surface: stream / call / resume HITL / cancel.

    Signatures mirror ``AgentManager``'s public execution methods 1:1 so the
    in-process implementation is a pure delegation (phase 1: zero behavior
    change).
    """

    def stream(self, agent_id: str, request: dict[str, Any]) -> AsyncIterator[Any]:
        """Stream harness chunks for one turn.

        Declared as a plain ``def`` returning ``AsyncIterator``: conforming
        implementations are async-generator functions, whose call type is
        ``(args) -> AsyncIterator`` (not a coroutine).
        """
        ...

    async def call(self, agent_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Non-streaming one-shot invocation."""
        ...

    def resume_hitl(
        self,
        agent_id: str,
        thread_id: str,
        decisions: list[dict[str, Any]],
    ) -> AsyncIterator[Any]:
        """Resume a paused HITL interrupt for *thread_id*."""
        ...

    def cancel_stream(self, agent_id: str, thread_id: str) -> None:
        """Signal the active stream for *(agent_id, thread_id)* to stop."""
        ...


class InProcessExecutor:
    """Runs turns in this process via :class:`AgentManager`.

    Phase 1 default implementation: a pure delegation layer over the
    manager's public execution methods — no locking, retry, or buffering of
    its own. Those concerns stay in ``AgentManager`` (thread execution lock,
    invocation tracking) so behavior is exactly today's.
    """

    def __init__(self, manager: AgentManager) -> None:
        self._manager = manager

    async def stream(self, agent_id: str, request: dict[str, Any]) -> AsyncIterator[Any]:
        async for chunk in self._manager.stream(agent_id, request):
            yield chunk

    async def call(self, agent_id: str, request: dict[str, Any]) -> dict[str, Any]:
        return await self._manager.call(agent_id, request)

    async def resume_hitl(
        self,
        agent_id: str,
        thread_id: str,
        decisions: list[dict[str, Any]],
    ) -> AsyncIterator[Any]:
        async for chunk in self._manager.resume_hitl(agent_id, thread_id, decisions):
            yield chunk

    def cancel_stream(self, agent_id: str, thread_id: str) -> None:
        self._manager.cancel_stream(agent_id, thread_id)


def cancel_turn(app_runtime: Any, agent_id: str, thread_id: str) -> None:
    """Cancel the active turn wherever it runs (review P1-3).

    In remote mode the turn executes on an agent-worker process, so the
    api-side ``agent_registry`` has no stream to cancel — the request must
    route through the :class:`RemoteExecutor` (cross-process ``pg_notify``).
    Local mode keeps the direct registry path. Entry points (WS ``cancel``,
    slash ``/stop``) call this instead of picking an executor themselves.
    """
    remote = getattr(app_runtime, "remote_executor", None)
    if remote is not None:
        remote.cancel_stream(agent_id, thread_id)
        return
    registry = getattr(app_runtime, "agent_registry", None)
    if registry is not None:
        registry.cancel_stream(agent_id, thread_id)
