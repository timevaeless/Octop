"""Unit tests for cluster plan D15: per-turn runtime bound (S3 hang abort).

Review S3: a worker that is alive with a fresh heartbeat but whose turn
hangs (LLM upstream stall, tool that never returns) would hold the
per-thread queue lock forever — the stalled-retry loop deliberately does
not re-dispatch jobs whose worker is alive, and the api-side watchdog only
fails the reader, not the job. The worker-side runtime bound aborts the
run, publishes the error sentinel, and fails the job (releasing the lock).
The real lock-release round trip lives in the stage-3 probe scripts.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from octop.infra.cluster.remote import (
    CHUNK_KIND_END,
    CHUNK_KIND_ERROR,
    TURN_JOB_MAX_RUNTIME_SECONDS,
    RemoteTurnError,
    _execute_turn,
)

# ---------------------------------------------------------------------------
# fakes (local copies: tests/unit/cluster is not a package)
# ---------------------------------------------------------------------------


class FakeRegistry:
    """Dict-backed registry recording claim/clear order."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, str]] = {}
        self.events: list[tuple[str, str]] = []

    async def claim(self, *, agent_id: str, thread_id: str, job_id: str, kind: str) -> None:
        self.rows[thread_id] = {"agent_id": agent_id, "job_id": job_id, "kind": kind}
        self.events.append(("claim", job_id))

    async def clear(self, *, thread_id: str, job_id: str) -> None:
        row = self.rows.get(thread_id)
        if row is not None and row["job_id"] == job_id:
            del self.rows[thread_id]
        self.events.append(("clear", job_id))

    async def active_job_id(self, thread_id: str) -> str | None:
        row = self.rows.get(thread_id)
        return row["job_id"] if row is not None else None


class FakePublisher:
    def __init__(self, sink: list[list[Any]], job_id: str) -> None:
        self._sink = sink
        self.published: list[Any] = []

    async def open(self) -> None:
        self._sink.append(self.published)

    async def publish(self, chunk: Any) -> None:
        self.published.append(chunk)

    async def finish(self) -> None:
        self.published.append({"type": CHUNK_KIND_END})

    async def fail(self, message: str) -> None:
        self.published.append({"type": CHUNK_KIND_ERROR, "error": message})

    async def aclose(self) -> None:
        pass


class HangingLocalExecutor:
    """Simulates a worker-alive turn hang: never yields, never ends."""

    async def stream(self, agent_id: str, request: dict[str, Any]) -> Any:
        await asyncio.Event().wait()  # blocks forever
        yield {}  # pragma: no cover - unreachable

    async def call(self, agent_id: str, request: dict[str, Any]) -> dict[str, Any]:
        await asyncio.Event().wait()  # pragma: no cover - unreachable
        return {}

    async def resume_hitl(self, agent_id: str, thread_id: str, decisions: list[Any]) -> Any:
        raise AssertionError("not used")  # pragma: no cover

    def cancel_stream(self, agent_id: str, thread_id: str) -> None:  # pragma: no cover
        pass


def test_hung_turn_aborts_at_runtime_bound_and_frees_thread() -> None:
    """The plan's S3 fix: a hung run fails the job (lock release) and the
    reader receives an error sentinel instead of waiting the 1800s watchdog."""

    async def run() -> tuple[list[Any], FakeRegistry]:
        registry = FakeRegistry()
        sink: list[list[Any]] = []
        with pytest.raises(RemoteTurnError, match="runtime"):
            await _execute_turn(
                {
                    "kind": "stream",
                    "job_id": "j-hang",
                    "agent_id": "a1",
                    "thread_id": "t1",
                    "request": {},
                },
                local_executor=HangingLocalExecutor(),  # type: ignore[arg-type]
                publisher_factory=lambda job_id: FakePublisher(sink, job_id),
                channel="ch",
                registry=registry,  # type: ignore[arg-type]
                job_runtime_seconds=0.05,
            )
        return sink[0], registry

    published, registry = asyncio.run(run())
    error_chunks = [c for c in published if c.get("type") == CHUNK_KIND_ERROR]
    assert len(error_chunks) == 1
    assert "runtime" in str(error_chunks[0]["error"])
    # The job failed (not retried): the thread mapping is cleared, so later
    # messages of the thread can claim it again.
    assert registry.rows == {}
    assert registry.events == [("claim", "j-hang"), ("clear", "j-hang")]


def test_fast_turn_finishes_normally_under_runtime_bound() -> None:
    async def run() -> list[Any]:
        registry = FakeRegistry()
        sink: list[list[Any]] = []
        await _execute_turn(
            {
                "kind": "stream",
                "job_id": "j-fast",
                "agent_id": "a1",
                "thread_id": "t1",
                "request": {},
            },
            local_executor=_FastExecutor(),  # type: ignore[arg-type]
            publisher_factory=lambda job_id: FakePublisher(sink, job_id),
            channel="ch",
            registry=registry,  # type: ignore[arg-type]
            job_runtime_seconds=5.0,
        )
        return sink[0]

    published = asyncio.run(run())
    assert published[-1] == {"type": CHUNK_KIND_END}


def test_runtime_bound_disabled_with_none() -> None:
    """``None`` opts out (turn runs unbounded) — cancellable from outside."""

    async def run() -> None:
        sink: list[list[Any]] = []
        task = asyncio.ensure_future(
            _execute_turn(
                {
                    "kind": "stream",
                    "job_id": "j-none",
                    "agent_id": "a1",
                    "thread_id": "t1",
                    "request": {},
                },
                local_executor=HangingLocalExecutor(),  # type: ignore[arg-type]
                publisher_factory=lambda job_id: FakePublisher(sink, job_id),
                channel="ch",
                registry=FakeRegistry(),  # type: ignore[arg-type]
                job_runtime_seconds=None,
            )
        )
        await asyncio.sleep(0.05)
        assert not task.done()  # no internal abort happened
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_runtime_default_matches_review_bound() -> None:
    # 2100s > 1800s api watchdog: the worker aborts only after the reader
    # would already have given up — the bound is a lock-release backstop,
    # not the primary latency mechanism.
    assert TURN_JOB_MAX_RUNTIME_SECONDS == 2100.0


class _FastExecutor:
    async def stream(self, agent_id: str, request: dict[str, Any]) -> Any:
        yield {"type": "text", "content": "ok"}

    async def call(self, agent_id: str, request: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("not used")  # pragma: no cover

    async def resume_hitl(self, agent_id: str, thread_id: str, decisions: list[Any]) -> Any:
        raise AssertionError("not used")  # pragma: no cover

    def cancel_stream(self, agent_id: str, thread_id: str) -> None:  # pragma: no cover
        pass
