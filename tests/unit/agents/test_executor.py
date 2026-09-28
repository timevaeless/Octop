"""Unit tests for the AgentExecutor seam (phase 1, behavior-equivalent).

Evidence for ``docs/microservice-scaling-1000.md`` phase 1: InProcessExecutor
is a pure delegation layer — exact argument pass-through, chunk passthrough,
and protocol conformance. AgentManager itself satisfies the protocol
structurally (same public execution methods).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from octop.infra.agents.executor import AgentExecutor, InProcessExecutor


class RecordingManager:
    """Minimal stub recording calls; quacks like AgentManager's exec surface."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def stream(self, agent_id: str, request: dict[str, Any]) -> AsyncIterator[Any]:
        self.calls.append(("stream", (agent_id, request)))
        for i in range(3):
            yield {"type": "token", "content": f"chunk-{i}", "agent_id": agent_id}

    async def call(self, agent_id: str, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("call", (agent_id, request)))
        return {"result": "ok", "agent_id": agent_id}

    async def resume_hitl(
        self,
        agent_id: str,
        thread_id: str,
        decisions: list[dict[str, Any]],
    ) -> AsyncIterator[Any]:
        self.calls.append(("resume_hitl", (agent_id, thread_id, decisions)))
        yield {"type": "token", "content": "resumed"}

    def cancel_stream(self, agent_id: str, thread_id: str) -> None:
        self.calls.append(("cancel_stream", (agent_id, thread_id)))


def _make() -> tuple[InProcessExecutor, RecordingManager]:
    manager = RecordingManager()
    return InProcessExecutor(manager), manager  # type: ignore[arg-type]


def test_executor_satisfies_protocol() -> None:
    executor, _ = _make()
    assert isinstance(executor, AgentExecutor)


def test_stream_delegates_with_passthrough_chunks() -> None:
    async def run() -> tuple[list[Any], list[tuple[str, tuple[Any, ...]]]]:
        executor, manager = _make()
        chunks = [
            chunk
            async for chunk in executor.stream("agent-1", {"thread_id": "t1", "messages": "hi"})
        ]
        return chunks, manager.calls

    chunks, calls = asyncio.run(run())
    assert [c["content"] for c in chunks] == ["chunk-0", "chunk-1", "chunk-2"]
    assert all(c["agent_id"] == "agent-1" for c in chunks)
    assert calls == [("stream", ("agent-1", {"thread_id": "t1", "messages": "hi"}))]


def test_call_returns_manager_result_unchanged() -> None:
    async def run() -> tuple[dict[str, Any], list[tuple[str, tuple[Any, ...]]]]:
        executor, manager = _make()
        result = await executor.call("agent-2", {"messages": "ping"})
        return result, manager.calls

    result, calls = asyncio.run(run())
    assert result == {"result": "ok", "agent_id": "agent-2"}
    assert calls == [("call", ("agent-2", {"messages": "ping"}))]


def test_resume_hitl_passes_thread_and_decisions() -> None:
    async def run() -> tuple[list[Any], list[tuple[str, tuple[Any, ...]]]]:
        executor, manager = _make()
        decisions = [{"tool": "shell", "decision": "approve"}]
        chunks = [chunk async for chunk in executor.resume_hitl("agent-3", "thread-9", decisions)]
        return chunks, manager.calls

    chunks, calls = asyncio.run(run())
    assert chunks == [{"type": "token", "content": "resumed"}]
    assert calls == [
        ("resume_hitl", ("agent-3", "thread-9", [{"tool": "shell", "decision": "approve"}]))
    ]


def test_cancel_stream_delegates() -> None:
    executor, manager = _make()
    executor.cancel_stream("agent-4", "thread-7")
    assert manager.calls == [("cancel_stream", ("agent-4", "thread-7"))]


@pytest.mark.parametrize(
    "method",
    ["stream", "call", "resume_hitl", "cancel_stream"],
)
def test_agent_manager_structurally_satisfies_protocol(method: str) -> None:
    """The four execution members exist on AgentManager (protocol compatibility)."""
    from octop.infra.agents.manager import AgentManager

    assert callable(getattr(AgentManager, method))
