"""Unit tests for cluster plan D3: in-flight turn continuity (attach).

Story covered, against fakes (real two-process round trip lives in the
stage-2/3 integration scripts):

- the worker claims the thread → job mapping when a turn starts and clears
  it in ``finally`` (success, failure, and abort paths alike);
- a fresh api node can discover the active job for a thread and re-attach
  to the same job stream after the original node died
  (``test_api_crash_continues_turn`` — the plan's named regression);
- the registry SQL keeps its guards (UPSERT semantics, job-scoped delete).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from octop.infra.cluster.remote import (
    ACTIVE_JOBS_TABLE,
    CHUNK_KIND_END,
    CHUNK_KIND_ERROR,
    ORPHAN_JOB_STATUSES,
    ActiveJobRegistry,
    RemoteExecutor,
    RemoteTurnError,
    _execute_turn,
    turn_job_status,
)

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeRegistry:
    """Dict-backed registry recording claim/clear order."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, str]] = {}
        self.events: list[tuple[str, str]] = []

    async def claim(self, *, agent_id: str, thread_id: str, job_id: str, kind: str) -> None:
        self.rows[thread_id] = {
            "agent_id": agent_id,
            "job_id": job_id,
            "kind": kind,
        }
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
    """Records published chunks; quacks like TurnEventPublisher."""

    def __init__(self, sink: list[list[Any]], job_id: str) -> None:
        self._sink = sink
        self._job_id = job_id
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


class StubLocalExecutor:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error

    async def stream(self, agent_id: str, request: dict[str, Any]) -> Any:
        if self.error is not None:
            raise self.error
        for chunk in ({"type": "text", "content": "hello"}, {"type": "text", "content": "!"}):
            yield chunk

    async def call(self, agent_id: str, request: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("not used in this suite")

    async def resume_hitl(self, agent_id: str, thread_id: str, decisions: list[Any]) -> Any:
        raise AssertionError("not used in this suite")

    def cancel_stream(self, agent_id: str, thread_id: str) -> None:  # pragma: no cover
        pass


class ReplayReader:
    """Replays a canned chunk list for a given job (TurnStreamReader fake)."""

    def __init__(self, job_id: str, chunks: list[Any]) -> None:
        self.job_id = job_id
        self._chunks = chunks

    async def chunks(self) -> Any:
        for chunk in self._chunks:
            yield chunk


# ---------------------------------------------------------------------------
# worker-side claim / clear
# ---------------------------------------------------------------------------


def test_worker_claims_and_clears_active_job() -> None:
    async def run() -> FakeRegistry:
        registry = FakeRegistry()
        sink: list[list[Any]] = []
        await _execute_turn(
            {
                "kind": "stream",
                "job_id": "j1",
                "agent_id": "a1",
                "thread_id": "t1",
                "request": {},
            },
            local_executor=StubLocalExecutor(),  # type: ignore[arg-type]
            publisher_factory=lambda job_id: FakePublisher(sink, job_id),
            channel="ch",
            registry=registry,  # type: ignore[arg-type]
        )
        return registry

    registry = asyncio.run(run())
    assert registry.events == [("claim", "j1"), ("clear", "j1")]
    assert registry.rows == {}  # turn finished: mapping gone


def test_worker_clears_mapping_on_failure() -> None:
    async def run() -> FakeRegistry:
        registry = FakeRegistry()
        sink: list[list[Any]] = []
        with pytest.raises(ValueError):
            await _execute_turn(
                {
                    "kind": "stream",
                    "job_id": "j2",
                    "agent_id": "a1",
                    "thread_id": "t1",
                    "request": {},
                },
                local_executor=StubLocalExecutor(error=ValueError("boom")),  # type: ignore[arg-type]
                publisher_factory=lambda job_id: FakePublisher(sink, job_id),
                channel="ch",
                registry=registry,  # type: ignore[arg-type]
            )
        return registry

    registry = asyncio.run(run())
    assert registry.events == [("claim", "j2"), ("clear", "j2")]
    assert registry.rows == {}


def test_worker_claim_is_best_effort() -> None:
    """A failing registry must not break the turn itself."""

    class BrokenRegistry(FakeRegistry):
        async def claim(self, **kwargs: Any) -> None:
            raise RuntimeError("pg down")

        async def clear(self, **kwargs: Any) -> None:
            raise RuntimeError("pg down")

    async def run() -> list[Any]:
        registry = BrokenRegistry()
        sink: list[list[Any]] = []
        await _execute_turn(
            {
                "kind": "stream",
                "job_id": "j3",
                "agent_id": "a1",
                "thread_id": "t1",
                "request": {},
            },
            local_executor=StubLocalExecutor(),  # type: ignore[arg-type]
            publisher_factory=lambda job_id: FakePublisher(sink, job_id),
            channel="ch",
            registry=registry,  # type: ignore[arg-type]
        )
        return sink[0]

    published = asyncio.run(run())
    assert published[-1] == {"type": CHUNK_KIND_END}


# ---------------------------------------------------------------------------
# api-side continuity
# ---------------------------------------------------------------------------


def test_api_crash_continues_turn() -> None:
    """Plan regression: api#1 dies mid-turn; api#2 discovers the job via the
    worker-written mapping and re-attaches to the same job stream."""

    async def run() -> tuple[str | None, list[Any], str]:
        registry = FakeRegistry()

        # -- node #1 (api submits; worker claims and streams) --
        sink: list[list[Any]] = []
        await _execute_turn(
            {
                "kind": "stream",
                "job_id": "job-42",
                "agent_id": "a1",
                "thread_id": "t1",
                "request": {},
            },
            local_executor=StubLocalExecutor(),  # type: ignore[arg-type]
            publisher_factory=lambda job_id: FakePublisher(sink, job_id),
            channel="ch",
            registry=registry,  # type: ignore[arg-type]
        )
        # Simulate the turn still running when api#1 dies: re-claim without
        # clearing (the events sink keeps growing on the worker side).
        await registry.claim(agent_id="a1", thread_id="t1", job_id="job-42", kind="stream")
        worker_stream_so_far = list(sink[0])

        # -- node #2: original api process is gone; a fresh executor on
        # another node looks up the mapping and attaches to the same job --
        captured: dict[str, str] = {}

        def reader_factory(job_id: str) -> ReplayReader:
            captured["job_id"] = job_id
            return ReplayReader(job_id, worker_stream_so_far)

        executor2 = RemoteExecutor(
            "postgresql://dsn",
            reader_factory=reader_factory,
            registry=registry,  # type: ignore[arg-type]
        )
        job_id = await executor2.active_job_id("t1")
        assert job_id == "job-42"
        chunks = [chunk async for chunk in executor2.attach(job_id)]
        return job_id, chunks, captured["job_id"]

    job_id, chunks, attached_job = asyncio.run(run())
    assert job_id == "job-42"
    assert attached_job == "job-42"  # api#2 attached to the SAME job stream
    assert chunks[0] == {"type": "text", "content": "hello"}
    assert chunks[-1] == {"type": CHUNK_KIND_END}


def test_attach_reader_is_created_for_the_existing_job() -> None:
    async def run() -> str:
        seen: list[str] = []

        def reader_factory(job_id: str) -> ReplayReader:
            seen.append(job_id)
            return ReplayReader(job_id, [])

        executor = RemoteExecutor("postgresql://dsn", reader_factory=reader_factory)
        async for _chunk in executor.attach("job-7"):
            pass
        return seen[0]

    assert asyncio.run(run()) == "job-7"


def test_active_job_id_none_when_registry_empty() -> None:
    async def run() -> str | None:
        registry = FakeRegistry()
        executor = RemoteExecutor(
            "postgresql://dsn",
            registry=registry,  # type: ignore[arg-type]
        )
        return await executor.active_job_id("t-idle")

    assert asyncio.run(run()) is None


# ---------------------------------------------------------------------------
# D3 orphan guard: attach refuses jobs that can never stream again
# ---------------------------------------------------------------------------


def test_attach_fails_fast_on_orphaned_job() -> None:
    """A stale mapping row (retries exhausted → ``failed``) must raise at
    once instead of letting the reader wait the full watchdog."""

    async def run() -> str:
        async def job_status(job_id: str) -> str | None:
            return "failed"

        executor = RemoteExecutor(
            "postgresql://dsn",
            reader_factory=lambda job_id: ReplayReader(job_id, []),
            job_status=job_status,
        )
        try:
            async for _chunk in executor.attach("job-dead"):
                pass  # pragma: no cover - must raise before any chunk
        except RemoteTurnError as exc:
            return str(exc)
        raise AssertionError("attach should have raised RemoteTurnError")  # pragma: no cover

    message = asyncio.run(run())
    assert "job-dead" in message
    assert "no longer running" in message


def test_attach_fails_fast_when_job_row_is_gone() -> None:
    async def run() -> str:
        async def job_status(job_id: str) -> str | None:
            return None

        executor = RemoteExecutor(
            "postgresql://dsn",
            reader_factory=lambda job_id: ReplayReader(job_id, []),
            job_status=job_status,
        )
        try:
            async for _chunk in executor.attach("job-pruned"):
                pass  # pragma: no cover
        except RemoteTurnError as exc:
            return str(exc)
        raise AssertionError("attach should have raised RemoteTurnError")  # pragma: no cover

    assert "gone" in asyncio.run(run())


@pytest.mark.parametrize("status", sorted(ORPHAN_JOB_STATUSES - {"failed"}))
def test_attach_fails_fast_on_every_orphan_status(status: str) -> None:
    async def run() -> bool:
        async def job_status(job_id: str) -> str | None:
            return status

        executor = RemoteExecutor(
            "postgresql://dsn",
            reader_factory=lambda job_id: ReplayReader(job_id, []),
            job_status=job_status,
        )
        try:
            async for _chunk in executor.attach("job-x"):
                pass  # pragma: no cover
        except RemoteTurnError:
            return True
        return False  # pragma: no cover

    assert asyncio.run(run()) is True


@pytest.mark.parametrize("status", ["todo", "doing", "succeeded"])
def test_attach_proceeds_for_attachable_statuses(status: str) -> None:
    async def run() -> list[Any]:
        async def job_status(job_id: str) -> str | None:
            return status

        chunks = [{"type": "text", "content": "hi"}, {"type": CHUNK_KIND_END}]
        executor = RemoteExecutor(
            "postgresql://dsn",
            reader_factory=lambda job_id: ReplayReader(job_id, chunks),
            job_status=job_status,
        )
        return [chunk async for chunk in executor.attach("job-live")]

    expected = [{"type": "text", "content": "hi"}, {"type": CHUNK_KIND_END}]
    assert asyncio.run(run()) == expected


def test_attach_proceeds_when_status_lookup_fails() -> None:
    """Best-effort guard: a PG hiccup during the lookup degrades to the
    reader watchdog path, never blocks attach outright."""

    async def run() -> list[Any]:
        async def broken_status(job_id: str) -> str | None:
            raise RuntimeError("pg down")

        chunks = [{"type": CHUNK_KIND_END}]
        executor = RemoteExecutor(
            "postgresql://dsn",
            reader_factory=lambda job_id: ReplayReader(job_id, chunks),
            job_status=broken_status,
        )
        return [chunk async for chunk in executor.attach("job-9")]

    assert asyncio.run(run()) == [{"type": CHUNK_KIND_END}]


class _StatusPgConn:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    async def execute(self, sql: str, params: Any = None) -> Any:
        return self

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._row

    async def close(self) -> None:
        pass


def test_turn_job_status_queries_by_payload_job_id() -> None:
    async def fake_connect(_dsn: str, **_kwargs: Any) -> _StatusPgConn:
        return _StatusPgConn(("doing",))

    async def run() -> str | None:
        # Exercise the real SQL shape through the module-level helper.
        return await turn_job_status("postgresql://dsn", "job-1", connect=fake_connect)

    assert asyncio.run(run()) == "doing"


def test_turn_job_status_none_when_row_missing() -> None:
    async def fake_connect(_dsn: str, **_kwargs: Any) -> _StatusPgConn:
        return _StatusPgConn(None)

    async def run() -> str | None:
        return await turn_job_status("postgresql://dsn", "job-404", connect=fake_connect)

    assert asyncio.run(run()) is None


# ---------------------------------------------------------------------------
# registry SQL guards (fake connection)
# ---------------------------------------------------------------------------


class _FakePgConn:
    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self._log = log

    async def execute(self, sql: str, params: Any = None) -> Any:
        self._log.append((sql, params))
        return None

    async def fetchone(self) -> None:  # pragma: no cover - not reached
        return None

    async def close(self) -> None:
        pass


def _make_connect(log: list[tuple[str, Any]], select_result: str | None = None) -> Any:
    async def connect(_dsn: str, **_kwargs: Any) -> _FakePgConn:
        conn = _FakePgConn(log)
        if select_result is not None:
            conn.fetchone = lambda: asyncio.sleep(0, result=(select_result,))  # type: ignore[method-assign]
        return conn

    return connect


def test_registry_claim_uses_upsert() -> None:
    log: list[tuple[str, Any]] = []
    registry = ActiveJobRegistry("postgresql://dsn", connect=_make_connect(log))
    asyncio.run(registry.claim(agent_id="a", thread_id="t", job_id="j", kind="stream"))
    sql, params = log[0]
    assert ACTIVE_JOBS_TABLE in sql
    assert "ON CONFLICT (thread_id) DO UPDATE" in sql
    assert params == ("t", "a", "j", "stream")


def test_registry_clear_is_job_scoped() -> None:
    log: list[tuple[str, Any]] = []
    registry = ActiveJobRegistry("postgresql://dsn", connect=_make_connect(log))
    asyncio.run(registry.clear(thread_id="t", job_id="j"))
    sql, params = log[0]
    # Guarded delete: a queued newer job's claim must not be wiped.
    assert f"DELETE FROM {ACTIVE_JOBS_TABLE} WHERE thread_id = %s AND job_id = %s" in sql
    assert params == ("t", "j")
