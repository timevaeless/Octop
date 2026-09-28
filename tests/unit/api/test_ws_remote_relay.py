"""Unit tests for cluster plan D3 (WS continuation): cross-node relay.

``_maybe_relay_remote_turn`` bridges the D3 worker-persisted thread→job
mapping into the dashboard WebSocket hub: a reconnecting dashboard on a
node whose local hub is idle must keep rendering an in-flight remote turn
that was started on another api node. Covers relay delivery, terminal
frames, local-mode no-op, lookup failure tolerance, and single-relay
guarding.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from octop.api.routers.chat.ws import _maybe_relay_remote_turn

# Whole module exercises the cluster-only relay (pluggability contract §8):
# skip gracefully when the cluster package is absent from a community build.
pytest.importorskip(
    "octop.api.cluster.relay", reason="cluster module not present (community build)"
)

from octop.api.cluster.relay import _remote_relay_tasks  # noqa: E402


class FakeHub:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self._active: set[str] = set()

    def mark_turn_active(self, thread_id: str) -> None:
        self._active.add(thread_id)

    def mark_turn_idle(self, thread_id: str) -> None:
        self._active.discard(thread_id)

    def is_turn_active(self, thread_id: str) -> bool:
        return thread_id in self._active

    async def push_to_thread(self, thread_id: str, frame: dict[str, Any]) -> None:
        self.frames.append(frame)


class FakeExecutor:
    def __init__(
        self,
        job_by_thread: dict[str, str | None],
        *,
        chunks: list[dict[str, Any]] | None = None,
        fail_after: int | None = None,
        lookup_error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.job_by_thread = job_by_thread
        self.chunks = chunks if chunks is not None else [{"type": "token", "content": "hi"}]
        self.fail_after = fail_after
        self.lookup_error = lookup_error
        self.delay = delay
        self.attach_calls = 0

    async def active_job_id(self, thread_id: str) -> str | None:
        if self.lookup_error is not None:
            raise self.lookup_error
        return self.job_by_thread.get(thread_id)

    async def attach(self, job_id: str) -> Any:
        self.attach_calls += 1
        for i, chunk in enumerate(self.chunks):
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail_after is not None and i == self.fail_after:
                raise RuntimeError("upstream died")
            yield chunk


class FakeRuntime:
    def __init__(self, executor: FakeExecutor | None) -> None:
        self.remote_executor = executor


class FakeServer:
    def __init__(self, executor: FakeExecutor | None) -> None:
        self.app_runtime = FakeRuntime(executor)


THREAD = "t1"
JOB = "job-1"


async def _drain_relay() -> None:
    task = _remote_relay_tasks.get(THREAD)
    if task is not None:
        await task


async def test_relay_streams_remote_job_into_hub() -> None:
    hub = FakeHub()
    executor = FakeExecutor(
        {THREAD: JOB}, chunks=[{"type": "token", "content": "a"}, {"type": "token", "content": "b"}]
    )
    await _maybe_relay_remote_turn(FakeServer(executor), hub, THREAD)
    assert hub.is_turn_active(THREAD)  # status flips before turn_status frame is sent

    await _drain_relay()

    assert hub.frames == [
        {"type": "token", "content": "a"},
        {"type": "token", "content": "b"},
        {"type": "done"},
    ]
    assert not hub.is_turn_active(THREAD)
    assert THREAD not in _remote_relay_tasks


async def test_relay_error_frame_then_done() -> None:
    hub = FakeHub()
    executor = FakeExecutor({THREAD: JOB}, chunks=[{"type": "token", "content": "a"}], fail_after=0)
    await _maybe_relay_remote_turn(FakeServer(executor), hub, THREAD)
    await _drain_relay()

    assert hub.frames == [
        {"type": "error", "message": "stream interrupted: upstream died"},
        {"type": "done"},
    ]
    assert not hub.is_turn_active(THREAD)


async def test_no_relay_when_mapping_empty() -> None:
    hub = FakeHub()
    executor = FakeExecutor({THREAD: None})
    await _maybe_relay_remote_turn(FakeServer(executor), hub, THREAD)
    assert not hub.is_turn_active(THREAD)
    assert THREAD not in _remote_relay_tasks
    assert hub.frames == []


async def test_no_relay_in_local_mode() -> None:
    hub = FakeHub()
    await _maybe_relay_remote_turn(FakeServer(None), hub, THREAD)
    assert not hub.is_turn_active(THREAD)
    assert hub.frames == []


async def test_lookup_failure_is_swallowed() -> None:
    hub = FakeHub()
    executor = FakeExecutor({}, lookup_error=ConnectionError("pg down"))
    await _maybe_relay_remote_turn(FakeServer(executor), hub, THREAD)
    assert not hub.is_turn_active(THREAD)
    assert hub.frames == []


async def test_concurrent_subscribe_does_not_double_relay() -> None:
    hub = FakeHub()
    executor = FakeExecutor({THREAD: JOB}, chunks=[{"type": "token", "content": "a"}], delay=0.01)
    await _maybe_relay_remote_turn(FakeServer(executor), hub, THREAD)
    await _maybe_relay_remote_turn(FakeServer(executor), hub, THREAD)  # second subscribe
    await _drain_relay()

    assert executor.attach_calls == 1
    assert hub.frames == [{"type": "token", "content": "a"}, {"type": "done"}]
