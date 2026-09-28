"""Unit tests for the remote turn-execution module (stage 2).

Covers the pieces that do not need a live PostgreSQL:
- chunk JSON encoding (mirrors the SSE fallback chain),
- RemoteExecutor deferral contract (payload shape, per-thread lock),
- stream/call/resume chunk semantics via injected fakes,
- the worker-side ``_execute_turn`` task body (publisher sentinel protocol),
- worker app task registration.

The real queue + NOTIFY round trip is exercised by
``scripts/stage2/run_stage2.py`` (two processes, embedded PostgreSQL).
"""

from __future__ import annotations

import asyncio
import types
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import BaseModel

from octop.infra.agents.executor import AgentExecutor
from octop.infra.cluster.remote import (
    CHUNK_KIND_END,
    CHUNK_KIND_ERROR,
    CHUNK_KIND_RESULT,
    QUEUE_NAME,
    TASK_NAME,
    RemoteExecutor,
    RemoteTurnError,
    _execute_turn,
    build_worker_app,
    encode_chunk,
)

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeQueue:
    """Records deferrals; quacks like TurnQueue."""

    def __init__(self) -> None:
        self.deferrals: list[tuple[dict[str, Any], str]] = []
        self.closed = False

    async def defer(self, payload: dict[str, Any], *, lock: str) -> None:
        self.deferrals.append((payload, lock))

    async def aclose(self) -> None:
        self.closed = True


class FakeReader:
    """Async-iterates a canned chunk list; quacks like TurnStreamReader."""

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    async def chunks(self) -> AsyncIterator[dict[str, Any]]:
        for chunk in self._chunks:
            yield chunk


class RecordingPublisher:
    def __init__(
        self,
        sink: list[RecordingPublisher],
        job_id: str,
        *,
        open_error: Exception | None = None,
    ) -> None:
        self._sink = sink
        self._job_id = job_id
        self.published: list[Any] = []
        self.failures: list[str] = []
        self.opened = False
        self.closed = False
        self.open_error = open_error

    async def open(self) -> None:
        if self.open_error is not None:
            raise self.open_error
        self.opened = True
        self._sink.append(self)

    async def publish(self, chunk: Any) -> None:
        self.published.append(chunk)

    async def finish(self) -> None:
        self.published.append({"type": CHUNK_KIND_END})

    async def fail(self, message: str) -> None:
        self.failures.append(message)
        self.published.append({"type": CHUNK_KIND_ERROR, "error": message})

    async def aclose(self) -> None:
        self.closed = True


class LocalStubExecutor:
    """Local executor stub for the worker-side task body tests."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.cancelled: list[tuple[str, str]] = []

    async def stream(self, agent_id: str, request: dict[str, Any]) -> AsyncIterator[Any]:
        if self.error is not None:
            raise self.error
        for i in range(3):
            yield {"type": "token", "content": f"c{i}"}

    async def call(self, agent_id: str, request: dict[str, Any]) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        return {"reply": "ok"}

    async def resume_hitl(
        self, agent_id: str, thread_id: str, decisions: list[dict[str, Any]]
    ) -> AsyncIterator[Any]:
        yield {"type": "resumed", "thread_id": thread_id}

    def cancel_stream(self, agent_id: str, thread_id: str) -> None:
        self.cancelled.append((agent_id, thread_id))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _remote_executor(
    chunks: list[Any],
) -> tuple[RemoteExecutor, FakeQueue]:
    queue = FakeQueue()
    executor = RemoteExecutor(
        "postgresql://fake",
        queue=queue,
        reader_factory=lambda job_id: FakeReader(chunks),
    )
    return executor, queue


# ---------------------------------------------------------------------------
# encode_chunk
# ---------------------------------------------------------------------------


def test_encode_chunk_round_trips_plain_dict() -> None:
    chunk = {"type": "token", "content": "你好"}
    assert encode_chunk(chunk) == '{"type": "token", "content": "你好"}'


def test_encode_chunk_handles_pydantic_models() -> None:
    class Model(BaseModel):
        kind: str

    encoded = encode_chunk({"type": "wrap", "body": Model(kind="x")})
    assert '"kind": "x"' in encoded


def test_encode_chunk_falls_back_to_repr() -> None:
    class Opaque:
        def __repr__(self) -> str:
            return "<opaque-42>"

    encoded = encode_chunk({"type": "weird", "obj": Opaque()})
    assert "<opaque-42>" in encoded


# ---------------------------------------------------------------------------
# RemoteExecutor deferral contract
# ---------------------------------------------------------------------------


def test_remote_executor_satisfies_protocol() -> None:
    executor, _ = _remote_executor([])
    assert isinstance(executor, AgentExecutor)


def test_stream_defers_job_with_thread_lock_and_yields_chunks() -> None:
    async def run() -> tuple[list[Any], list[tuple[dict[str, Any], str]]]:
        executor, queue = _remote_executor([{"type": "token", "content": "hi"}])
        chunks = [
            chunk
            async for chunk in executor.stream("agent-1", {"thread_id": "t-9", "messages": "hello"})
        ]
        return chunks, queue.deferrals

    chunks, deferrals = asyncio.run(run())
    assert chunks == [{"type": "token", "content": "hi"}]
    assert len(deferrals) == 1
    payload, lock = deferrals[0]
    assert payload["kind"] == "stream"
    assert payload["agent_id"] == "agent-1"
    assert payload["thread_id"] == "t-9"
    assert payload["request"] == {"thread_id": "t-9", "messages": "hello"}
    assert payload["job_id"]
    assert lock == "octop-turn:agent-1:t-9"


def test_call_returns_result_sentinel_payload() -> None:
    async def run() -> dict[str, Any]:
        executor, _ = _remote_executor([{"type": CHUNK_KIND_RESULT, "result": {"reply": "pong"}}])
        return await executor.call("agent-2", {"thread_id": "t1"})

    assert asyncio.run(run()) == {"reply": "pong"}


def test_call_raises_when_turn_ends_without_result() -> None:
    async def run() -> None:
        executor, _ = _remote_executor([{"type": "token", "content": "orphan"}])
        await executor.call("agent-2", {"thread_id": "t1"})

    with pytest.raises(RemoteTurnError):
        asyncio.run(run())


def test_resume_hitl_defers_with_decisions() -> None:
    async def run() -> list[tuple[dict[str, Any], str]]:
        decisions = [{"tool": "shell", "decision": "approve"}]
        executor, queue = _remote_executor([{"type": "token", "content": "resumed"}])
        _ = [chunk async for chunk in executor.resume_hitl("agent-3", "thread-5", decisions)]
        return queue.deferrals

    deferrals = asyncio.run(run())
    payload, lock = deferrals[0]
    assert payload["kind"] == "resume"
    assert payload["decisions"] == [{"tool": "shell", "decision": "approve"}]
    assert lock == "octop-turn:agent-3:thread-5"


def test_stream_defers_preserve_job_identity() -> None:
    """Two deferrals get distinct job ids (events are keyed per job)."""

    async def run() -> list[str]:
        queue = FakeQueue()
        executor = RemoteExecutor(
            "postgresql://fake",
            queue=queue,
            reader_factory=lambda job_id: FakeReader([]),
        )
        _ = [chunk async for chunk in executor.stream("a", {"thread_id": "t"})]
        _ = [chunk async for chunk in executor.stream("a", {"thread_id": "t"})]
        return [payload["job_id"] for payload, _lock in queue.deferrals]

    ids = asyncio.run(run())
    assert len(ids) == 2
    assert ids[0] != ids[1]


# ---------------------------------------------------------------------------
# worker-side task body
# ---------------------------------------------------------------------------


def test_execute_turn_stream_publishes_chunks_then_end() -> None:
    async def run() -> list[Any]:
        sinks: list[list[Any]] = []
        publishers: list[RecordingPublisher] = []

        def factory(job_id: str) -> RecordingPublisher:
            pub = RecordingPublisher(sinks, job_id)
            publishers.append(pub)
            return pub

        executor = LocalStubExecutor()
        await _execute_turn(
            {"kind": "stream", "job_id": "j1", "agent_id": "a", "request": {}},
            local_executor=executor,  # type: ignore[arg-type]
            publisher_factory=factory,
            channel="ch",
        )
        assert publishers[0].opened and publishers[0].closed
        return publishers[0].published

    published = asyncio.run(run())
    assert [c["content"] for c in published[:3]] == ["c0", "c1", "c2"]
    assert published[-1] == {"type": CHUNK_KIND_END}


def test_execute_turn_call_publishes_result() -> None:
    async def run() -> list[Any]:
        sinks: list[RecordingPublisher] = []

        def factory(job_id: str) -> RecordingPublisher:
            return RecordingPublisher(sinks, job_id)  # type: ignore[arg-type]

        await _execute_turn(
            {"kind": "call", "job_id": "j2", "agent_id": "a", "request": {}},
            local_executor=LocalStubExecutor(),  # type: ignore[arg-type]
            publisher_factory=factory,
            channel="ch",
        )
        return sinks[0].published

    published = asyncio.run(run())
    assert published[0] == {"type": CHUNK_KIND_RESULT, "result": {"reply": "ok"}}
    assert published[-1] == {"type": CHUNK_KIND_END}


def test_execute_turn_error_publishes_sentinel_and_reraises() -> None:
    async def run() -> tuple[list[str], list[Any]]:
        sinks: list[RecordingPublisher] = []

        def factory(job_id: str) -> RecordingPublisher:
            return RecordingPublisher(sinks, job_id)

        try:
            await _execute_turn(
                {"kind": "stream", "job_id": "j3", "agent_id": "a", "request": {}},
                local_executor=LocalStubExecutor(error=ValueError("boom")),  # type: ignore[arg-type]
                publisher_factory=factory,
                channel="ch",
            )
        except ValueError:
            pass
        else:
            pytest.fail("expected the original error to re-raise")
        publisher = sinks[0]
        return publisher.failures, publisher.published

    failures, published = asyncio.run(run())
    assert failures == ["ValueError: boom"]
    assert published[-1] == {"type": CHUNK_KIND_ERROR, "error": "ValueError: boom"}


def test_execute_turn_resume_reaches_local_executor() -> None:
    async def run() -> list[Any]:
        sinks: list[RecordingPublisher] = []

        def factory(job_id: str) -> RecordingPublisher:
            return RecordingPublisher(sinks, job_id)  # type: ignore[arg-type]

        await _execute_turn(
            {
                "kind": "resume",
                "job_id": "j4",
                "agent_id": "a",
                "thread_id": "t-7",
                "decisions": [{"decision": "approve"}],
            },
            local_executor=LocalStubExecutor(),  # type: ignore[arg-type]
            publisher_factory=factory,
            channel="ch",
        )
        return sinks[0].published

    published = asyncio.run(run())
    assert published[0] == {"type": "resumed", "thread_id": "t-7"}
    assert published[-1] == {"type": CHUNK_KIND_END}


# ---------------------------------------------------------------------------
# worker app registration
# ---------------------------------------------------------------------------


def test_build_worker_app_registers_turn_task() -> None:
    app = build_worker_app(
        "postgresql://fake",
        LocalStubExecutor(),  # type: ignore[arg-type]
        queue_name=QUEUE_NAME,
    )
    assert TASK_NAME in app.tasks
    task = app.tasks[TASK_NAME]
    assert task.queue == QUEUE_NAME


# ---------------------------------------------------------------------------
# stage 3: NotifyHub / TurnFetchPool / shared-mode reader
# ---------------------------------------------------------------------------


class FakeListenConn:
    """Quacks like a psycopg connection in LISTEN mode."""

    def __init__(self) -> None:
        self.listened: list[str] = []
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False

    async def execute(self, sql: str, *args: Any) -> None:
        self.listened.append(sql)

    async def notifies(self) -> AsyncIterator[Any]:
        while True:
            yield await self.inbox.get()

    async def close(self) -> None:
        self.closed = True


def test_notify_hub_dispatches_notifications_by_job_id() -> None:
    from types import SimpleNamespace

    from octop.infra.cluster.remote import NotifyHub

    async def run() -> None:
        conn = FakeListenConn()

        async def connect(_dsn: str, **_kw: Any) -> FakeListenConn:
            return conn

        hub = NotifyHub("postgresql://fake", connect=connect)
        await hub.open()
        assert conn.listened and conn.listened[0].startswith("LISTEN")

        event = hub.subscribe("job-1")
        assert not event.is_set()
        await conn.inbox.put(SimpleNamespace(payload="job-1"))
        await asyncio.wait_for(event.wait(), timeout=1.0)
        assert hub.connection_count == 1

        hub.unsubscribe("job-1", event)
        await hub.aclose()
        assert conn.closed

    asyncio.run(run())


def test_notify_hub_close_without_open_is_safe() -> None:
    from octop.infra.cluster.remote import NotifyHub

    async def run() -> None:
        hub = NotifyHub("postgresql://fake")
        await hub.aclose()

    asyncio.run(run())


class FakePoolConn:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def cursor(self) -> _FakePoolCursor:
        return _FakePoolCursor(self._rows)

    async def close(self) -> None:
        return None


class _FakePoolCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    async def __aenter__(self) -> _FakePoolCursor:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        _ = sql, params

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


def test_turn_fetch_pool_reuses_connections_up_to_size() -> None:
    from octop.infra.cluster.remote import TurnFetchPool

    async def run() -> tuple[list[FakePoolConn], list[list[tuple[Any, ...]]]]:
        created: list[FakePoolConn] = []

        async def connect(_dsn: str, **_kw: Any) -> FakePoolConn:
            conn = FakePoolConn([(1, "a")])
            created.append(conn)
            return conn

        pool = TurnFetchPool("postgresql://fake", size=2, connect=connect)
        results = [await pool.fetch("SELECT 1", ()) for _ in range(5)]
        await pool.aclose()
        return created, results

    created, results = asyncio.run(run())
    assert len(created) == 1  # sequential fetches reuse the single connection
    assert all(rows == [(1, "a")] for rows in results)


class FakeHub:
    def __init__(self) -> None:
        self._events: dict[str, list[asyncio.Event]] = {}
        self.unsubscribes = 0

    def subscribe(self, job_id: str) -> asyncio.Event:
        event = asyncio.Event()
        self._events.setdefault(job_id, []).append(event)
        return event

    def unsubscribe(self, job_id: str, event: asyncio.Event) -> None:
        self.unsubscribes += 1


class FakeFetch:
    def __init__(self, batches: list[list[tuple[int, str]]]) -> None:
        self._batches = batches
        self.calls = 0

    async def fetch(self, sql: str, params: tuple[Any, ...]) -> list[tuple[int, str]]:
        _ = sql, params
        rows = self._batches[min(self.calls, len(self._batches) - 1)]
        self.calls += 1
        return rows


def test_turn_stream_reader_shared_mode_yields_and_unsubscribes() -> None:
    import json

    from octop.infra.cluster.remote import TurnStreamReader

    async def run() -> list[Any]:
        hub = FakeHub()
        batch1 = [
            (1, json.dumps({"c": encode_chunk({"type": "token", "content": "hi"}), "ts": 0.0}))
        ]
        batch2 = [(2, json.dumps({"c": encode_chunk({"type": CHUNK_KIND_END}), "ts": 0.0}))]
        fetch = FakeFetch([batch1, batch2])
        reader = TurnStreamReader(
            "postgresql://fake",
            "job-9",
            poll_interval=0.01,
            hub=hub,  # type: ignore[arg-type]
            fetch=fetch,  # type: ignore[arg-type]
        )
        return [chunk async for chunk in reader.chunks()]

    chunks = asyncio.run(run())
    assert chunks == [{"type": "token", "content": "hi"}]


def test_turn_stream_reader_shared_mode_raises_on_error_sentinel() -> None:
    import json

    from octop.infra.cluster.remote import TurnStreamReader

    async def run() -> None:
        hub = FakeHub()
        batch = [
            (
                1,
                json.dumps(
                    {"c": encode_chunk({"type": CHUNK_KIND_ERROR, "error": "boom"}), "ts": 0.0}
                ),
            )
        ]
        reader = TurnStreamReader(
            "postgresql://fake",
            "job-9",
            poll_interval=0.01,
            hub=hub,  # type: ignore[arg-type]
            fetch=FakeFetch([batch]),  # type: ignore[arg-type]
        )
        _ = [chunk async for chunk in reader.chunks()]

    with pytest.raises(RemoteTurnError, match="boom"):
        asyncio.run(run())


# ---------------------------------------------------------------------------
# RemoteExecutor cold-start concurrency (stage-3 regression)
# ---------------------------------------------------------------------------


class _SlowFakeQueue:
    """Queue stand-in whose open() yields, exposing racy lazy init."""

    instances: list[_SlowFakeQueue] = []

    def __init__(self, dsn: str) -> None:
        type(self).instances.append(self)

    async def open(self) -> None:
        await asyncio.sleep(0.01)

    async def defer(self, payload: dict[str, Any], *, lock: str) -> None:
        return None

    async def aclose(self) -> None:
        return None


def test_concurrent_cold_start_creates_single_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """A burst of streams on a cold node must open ONE queue, not one per caller.

    Regression: the unlocked _ensure_open race created a procrastinate app
    (and its PG connection pool) per concurrent stream, exhausting PG
    max_connections under the stage-3 load test.
    """
    import octop.infra.cluster.remote as remote_mod

    _SlowFakeQueue.instances = []
    monkeypatch.setattr(remote_mod, "ProcrastinateTurnQueue", _SlowFakeQueue)
    executor = RemoteExecutor("postgresql://fake", reader_factory=lambda job_id: FakeReader([]))

    async def run() -> None:
        async def one(i: int) -> None:
            _ = [c async for c in executor.stream("a", {"thread_id": f"t{i}"})]

        await asyncio.gather(*(one(i) for i in range(20)))

    asyncio.run(run())
    assert len(_SlowFakeQueue.instances) == 1


def test_concurrent_cold_start_creates_single_hub_and_fetch_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hub + fetch pool must also be created exactly once on concurrent cold start."""
    import json

    import octop.infra.cluster.remote as remote_mod

    counts = {"hub": 0, "fetch": 0}

    class SlowQueue:
        def __init__(self, dsn: str) -> None:
            pass

        async def open(self) -> None:
            await asyncio.sleep(0.01)

        async def defer(self, payload: dict[str, Any], *, lock: str) -> None:
            return None

        async def aclose(self) -> None:
            return None

    class SlowHub:
        def __init__(self, dsn: str, *, channel: str = "x") -> None:
            counts["hub"] += 1

        async def open(self) -> None:
            await asyncio.sleep(0.01)

        def subscribe(self, job_id: str) -> asyncio.Event:
            return asyncio.Event()

        def unsubscribe(self, job_id: str, event: asyncio.Event) -> None:
            return None

        async def aclose(self) -> None:
            return None

    class SlowFetch:
        def __init__(self, dsn: str, *, size: int = 8) -> None:
            counts["fetch"] += 1

        async def fetch(self, sql: str, params: tuple[Any, ...]) -> list[Any]:
            envelope = json.dumps({"c": encode_chunk({"type": CHUNK_KIND_END}), "ts": 0.0})
            return [(1, envelope)]

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(remote_mod, "ProcrastinateTurnQueue", SlowQueue)
    monkeypatch.setattr(remote_mod, "NotifyHub", SlowHub)
    monkeypatch.setattr(remote_mod, "TurnFetchPool", SlowFetch)
    executor = RemoteExecutor("postgresql://fake")

    async def run() -> None:
        async def one(i: int) -> None:
            _ = [c async for c in executor.stream("a", {"thread_id": f"t{i}"})]

        await asyncio.gather(*(one(i) for i in range(20)))

    asyncio.run(run())
    assert counts == {"hub": 1, "fetch": 1}


# ---------------------------------------------------------------------------
# stage-3 review fixes: pool slot reservation, batched prune
# ---------------------------------------------------------------------------


def test_fetch_pool_connect_failure_does_not_leak_capacity() -> None:
    """A failed pool connect must roll back the reserved slot, or the pool
    permanently shrinks under cold-start connection storms."""
    import octop.infra.cluster.remote as remote_mod

    calls = {"n": 0}

    async def flaky_connect(_dsn: str, **_kw: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("pg unavailable")

    pool = remote_mod.TurnFetchPool("postgresql://fake", size=2, connect=flaky_connect)
    with pytest.raises(RuntimeError):
        asyncio.run(pool._acquire())
    assert pool._open_count == 0

    async def ok_connect(_dsn: str, **_kw: Any) -> object:
        return object()

    pool2 = remote_mod.TurnFetchPool("postgresql://fake", size=2, connect=ok_connect)
    asyncio.run(pool2._acquire())
    assert pool2._open_count == 1


def test_prune_turn_events_deletes_in_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prune loops in bounded DELETE batches until a short batch returns."""
    import octop.infra.cluster.remote as remote_mod

    executed: list[tuple[str, tuple[Any, ...]]] = []

    class FakeCur:
        def __init__(self, deleted: int) -> None:
            self.rowcount = deleted

    class FakeConn:
        def __init__(self, plan: list[int]) -> None:
            self._plan = plan

        async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> FakeCur:
            executed.append((sql, params))
            return FakeCur(self._plan.pop(0) if self._plan else 0)

        async def __aenter__(self) -> FakeConn:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    plan = [remote_mod.PRUNE_BATCH_SIZE, remote_mod.PRUNE_BATCH_SIZE, 123]
    conns = [FakeConn(plan)]

    async def fake_connect(_dsn: str, **_kw: Any) -> FakeConn:
        return conns[0]

    fake_psycopg = types.SimpleNamespace(
        AsyncConnection=types.SimpleNamespace(connect=fake_connect)
    )
    monkeypatch.setattr(remote_mod, "psycopg", fake_psycopg)

    total = asyncio.run(remote_mod.prune_turn_events("postgresql://fake"))
    assert total == 2 * remote_mod.PRUNE_BATCH_SIZE + 123
    assert len(executed) == 3
    sql, params = executed[0]
    assert "LIMIT %s" in sql and "seq IN" in sql
    assert params == (remote_mod.EVENT_RETENTION_SECONDS, remote_mod.PRUNE_BATCH_SIZE)


# ---------------------------------------------------------------------------
# review 2026-09-25 fixes: pool cancel-safety, cancel reconnect, stalled retry
# ---------------------------------------------------------------------------


class _HangConn:
    """Fake PG connection whose cursor.execute hangs until released."""

    def __init__(self) -> None:
        self.closed = False
        self.release = asyncio.Event()

    async def close(self) -> None:
        self.closed = True

    def cursor(self) -> Any:
        return self

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        await self.release.wait()

    async def fetchall(self) -> list[Any]:
        return []


def test_fetch_pool_returns_slot_on_cancel() -> None:
    """Cancelling an in-flight fetch must free the slot AND wake waiters."""
    import octop.infra.cluster.remote as remote_mod

    conn = _HangConn()

    async def fake_connect(_dsn: str, **_kw: Any) -> _HangConn:
        return conn

    pool = remote_mod.TurnFetchPool("postgresql://fake", size=1, connect=fake_connect)

    async def run() -> None:
        task = asyncio.create_task(pool.fetch("SELECT 1", ()))
        await asyncio.sleep(0.02)  # let it acquire and hang in execute
        assert pool._open_count == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pool._open_count == 0
        assert conn.closed
        # A waiter blocked before the cancel must now get a fresh connection
        # (sentinel wakeup) instead of hanging forever.
        waiter = asyncio.create_task(pool._acquire())
        got = await asyncio.wait_for(waiter, timeout=1.0)
        assert got is not None

    asyncio.run(run())


def test_fetch_pool_acquire_cancel_rolls_back_slot() -> None:
    """Cancelling during connect must not permanently shrink the pool."""
    import octop.infra.cluster.remote as remote_mod

    started = asyncio.Event()

    async def hang_connect(_dsn: str, **_kw: Any) -> Any:
        started.set()
        await asyncio.sleep(10)

    pool = remote_mod.TurnFetchPool("postgresql://fake", size=2, connect=hang_connect)

    async def run() -> None:
        task = asyncio.create_task(pool._acquire())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pool._open_count == 0

    asyncio.run(run())


def test_cancel_loop_reconnects_after_connection_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a PG hiccup the cancel listener must reconnect, not die."""
    import json as _json

    import octop.infra.cluster.remote as remote_mod

    cancels: list[tuple[str, str]] = []

    class LocalExec:
        def cancel_stream(self, agent_id: str, thread_id: str) -> None:
            cancels.append((agent_id, thread_id))

    class FakeNotifies:
        def __init__(self, broken: bool, payload: str | None = None) -> None:
            self._broken = broken
            self._payload = payload

        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> Any:
            if self._broken:
                raise ConnectionError("pg gone")
            if self._payload is None:
                raise StopAsyncIteration
            notify = types.SimpleNamespace(payload=self._payload)
            self._payload = None
            return notify

    class FakeListenerConn:
        def __init__(self, broken: bool, payload: str | None = None) -> None:
            self.closed = False
            self._broken = broken
            self._payload = payload

        async def execute(self, sql: str, params: Any = None) -> None:
            assert "LISTEN" in sql

        def notifies(self) -> FakeNotifies:
            return FakeNotifies(self._broken, self._payload)

        async def close(self) -> None:
            self.closed = True

    conns = [
        FakeListenerConn(broken=True),
        FakeListenerConn(broken=False, payload=_json.dumps({"agent_id": "a", "thread_id": "t"})),
    ]

    async def fake_connect(_dsn: str, **_kw: Any) -> FakeListenerConn:
        return conns.pop(0)

    fake_psycopg = types.SimpleNamespace(
        AsyncConnection=types.SimpleNamespace(connect=fake_connect)
    )
    monkeypatch.setattr(remote_mod, "psycopg", fake_psycopg)

    worker = remote_mod.AgentWorker(
        "postgresql://fake",
        LocalExec(),  # type: ignore[arg-type]
        cancel_reconnect_delay=0.01,
    )

    async def run() -> None:
        task = asyncio.create_task(worker._cancel_loop())
        for _ in range(200):
            await asyncio.sleep(0.01)
            if cancels:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancels == [("a", "t")]

    asyncio.run(run())


def test_retry_stalled_retries_dead_workers_not_live_long_turns() -> None:
    """Heartbeat-dead workers retry; a live-worker long turn is not retried (D1)."""

    class FakeJob:
        def __init__(self, job_id: int) -> None:
            self.id = job_id

    class FakeJobManager:
        def __init__(self) -> None:
            self.retried: list[int] = []

        async def get_stalled_jobs(self, **kw: Any) -> list[FakeJob]:
            if "nb_seconds" in kw:
                # job 2: live worker, long turn — must NOT be retried;
                # job 1: also long-running but already retried via heartbeat.
                return [FakeJob(2), FakeJob(1)]
            # job 1: worker stopped heartbeating — retry.
            return [FakeJob(1)]

        async def retry_job(self, job: FakeJob) -> None:
            self.retried.append(int(job.id))

    worker = remote_worker_stub()
    jm = FakeJobManager()
    # Only the heartbeat-dead job 1 is retried; the live-worker long turn
    # (job 2) is monitored but not retried — retry would double-run it.
    assert asyncio.run(worker.retry_stalled_jobs_once(jm)) == 1
    assert jm.retried == [1]


def test_runtime_retry_skips_live_worker_long_turn() -> None:
    """A long turn whose worker is still heartbeating is never retried (D1)."""

    class FakeJob:
        def __init__(self, job_id: int) -> None:
            self.id = job_id

    class FakeJobManager:
        def __init__(self) -> None:
            self.retried: list[int] = []

        async def get_stalled_jobs(self, **kw: Any) -> list[FakeJob]:
            if "nb_seconds" in kw:
                # Long-running job, but worker heartbeat is healthy.
                return [FakeJob(7)]
            # No heartbeat-stalled jobs — worker is alive.
            return []

        async def retry_job(self, job: FakeJob) -> None:
            self.retried.append(int(job.id))

    worker = remote_worker_stub()
    jm = FakeJobManager()
    assert asyncio.run(worker.retry_stalled_jobs_once(jm)) == 0
    assert jm.retried == []


def test_retry_continues_after_one_job_raises() -> None:
    """A failing retry_job must not abort the rest of the scan (D14)."""

    class FakeJob:
        def __init__(self, job_id: int) -> None:
            self.id = job_id

    class FakeJobManager:
        def __init__(self) -> None:
            self.retried: list[int] = []

        async def get_stalled_jobs(self, **kw: Any) -> list[FakeJob]:
            if "nb_seconds" in kw:
                return []
            # Two dead workers; retrying the first raises (concurrent finish).
            return [FakeJob(1), FakeJob(2)]

        async def retry_job(self, job: FakeJob) -> None:
            if int(job.id) == 1:
                raise RuntimeError("Job was not found or has an invalid status to retry")
            self.retried.append(int(job.id))

    worker = remote_worker_stub()
    jm = FakeJobManager()
    # job 1 raises (tolerated); job 2 still gets retried.
    assert asyncio.run(worker.retry_stalled_jobs_once(jm)) == 1
    assert jm.retried == [2]


def test_execute_turn_open_failure_emits_sentinel() -> None:
    """publisher.open() failure still routes through publisher.fail (D13)."""

    async def run() -> list[str]:
        publishers: list[RecordingPublisher] = []
        sink: list[RecordingPublisher] = []

        def factory(job_id: str) -> RecordingPublisher:
            pub = RecordingPublisher(sink, job_id, open_error=RuntimeError("pg unreachable"))
            publishers.append(pub)
            return pub

        try:
            await _execute_turn(
                {"kind": "stream", "job_id": "j5", "agent_id": "a", "request": {}},
                local_executor=LocalStubExecutor(),  # type: ignore[arg-type]
                publisher_factory=factory,
                channel="ch",
            )
        except RuntimeError:
            pass
        else:
            pytest.fail("expected open error to re-raise")
        assert publishers, "factory was never called"
        return publishers[0].failures

    failures = asyncio.run(run())
    assert failures == ["RuntimeError: pg unreachable"]


def remote_worker_stub() -> Any:
    from octop.infra.cluster.remote import AgentWorker

    class _Exec:
        def cancel_stream(self, agent_id: str, thread_id: str) -> None:
            return None

    return AgentWorker("postgresql://fake", _Exec())  # type: ignore[arg-type]


def test_remote_executor_cancel_stream_is_nonblocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cancel_stream must schedule an async notify, never sync-connect."""
    import octop.infra.cluster.remote as remote_mod

    submitted: list[str] = []

    class FakeNotifier:
        def submit(self, payload: str) -> None:
            submitted.append(payload)

        async def aclose(self) -> None:
            return None

    executor = RemoteExecutor("postgresql://fake", reader_factory=lambda job_id: FakeReader([]))
    executor._cancel_notifier = FakeNotifier()  # type: ignore[assignment]

    async def run() -> None:
        # Must not touch the real (sync) psycopg at all.
        monkeypatch.delattr(remote_mod.psycopg, "connect", raising=False)
        executor.cancel_stream("agent-1", "thread-1")
        await asyncio.sleep(0)

    asyncio.run(run())
    assert len(submitted) == 1
    assert '"agent-1"' in submitted[0]


def test_cancel_turn_routes_to_remote_executor_first() -> None:
    """The cancel helper must prefer the remote executor (P1-3)."""
    from octop.infra.agents.executor import cancel_turn

    calls: list[str] = []

    class Remote:
        def cancel_stream(self, agent_id: str, thread_id: str) -> None:
            calls.append("remote")

    class Registry:
        def cancel_stream(self, agent_id: str, thread_id: str) -> None:
            calls.append("local")

    rt = types.SimpleNamespace(remote_executor=Remote(), agent_registry=Registry())
    cancel_turn(rt, "a", "t")
    assert calls == ["remote"]

    rt_local = types.SimpleNamespace(remote_executor=None, agent_registry=Registry())
    cancel_turn(rt_local, "a", "t")
    assert calls == ["remote", "local"]
