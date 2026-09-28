"""Unit tests for the stage-3 leader runner (background singleton election).

Covers, against fake PG sessions:
- exactly one contender acquires the session advisory lock,
- handles start on gain and stop on loss,
- session break (PG restart / network cut) hands leadership to the standby,
- stop() is safe and stops attached services.

The real advisory-lock round trip is exercised by the stage-3 integration
script (embedded PostgreSQL).
"""

from __future__ import annotations

import asyncio
from typing import Any

from octop.infra.cluster.leader import BackgroundGate, LeaderHandle, LeaderRunner

# ---------------------------------------------------------------------------
# fakes: a shared advisory-lock state and fake PG sessions
# ---------------------------------------------------------------------------


class _FakeCur:
    def __init__(self, row: tuple[Any, ...]) -> None:
        self._row = row

    async def fetchone(self) -> tuple[Any, ...]:
        return self._row


class LockState:
    """Mimics PG session-level advisory lock semantics."""

    def __init__(self) -> None:
        self.holder: FakeLeaderConn | None = None

    def acquire(self, conn: FakeLeaderConn) -> bool:
        if self.holder is None:
            self.holder = conn
            return True
        return False

    def release(self, conn: FakeLeaderConn) -> None:
        if self.holder is conn:
            self.holder = None


class FakeLeaderConn:
    def __init__(self, state: LockState) -> None:
        self._state = state
        self.closed = False
        self.broken = False

    async def execute(self, sql: str, params: Any = None) -> _FakeCur:
        if "pg_try_advisory_lock" in sql:
            return _FakeCur((self._state.acquire(self),))
        if sql == "SELECT 1":
            if self.broken:
                raise RuntimeError("session broken")
            return _FakeCur((1,))
        raise AssertionError(f"unexpected sql: {sql}")

    async def close(self) -> None:
        self._state.release(self)
        self.closed = True


def _make_connect(state: LockState, created: list[FakeLeaderConn]) -> Any:
    async def connect(_dsn: str, **_kwargs: Any) -> FakeLeaderConn:
        conn = FakeLeaderConn(state)
        created.append(conn)
        return conn

    return connect


class Recorder:
    def __init__(self, name: str) -> None:
        self.name = name
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


async def _wait_until(cond: Any, *, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for leader condition")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_single_contender_becomes_leader_and_starts_handles() -> None:
    async def run() -> None:
        state = LockState()
        created: list[FakeLeaderConn] = []
        rec = Recorder("cron")
        runner = LeaderRunner(
            "postgresql://fake",
            poll_interval=0.01,
            retry_delay=0.01,
            connect=_make_connect(state, created),
        )
        runner.attach(LeaderHandle("cron", rec.start, rec.stop))
        runner.start()
        await _wait_until(lambda: runner.is_leader)
        await _wait_until(lambda: rec.started == 1)
        await runner.stop()
        assert rec.stopped >= 1

    asyncio.run(run())


def test_standby_never_becomes_leader_while_holder_alive() -> None:
    async def run() -> None:
        state = LockState()
        created: list[FakeLeaderConn] = []
        connect = _make_connect(state, created)
        a = LeaderRunner("d", poll_interval=0.01, retry_delay=0.01, connect=connect)
        b = LeaderRunner("d", poll_interval=0.01, retry_delay=0.01, connect=connect)
        a.start()
        b.start()
        await _wait_until(lambda: a.is_leader)
        await asyncio.sleep(0.05)
        assert not b.is_leader
        await a.stop()
        await b.stop()

    asyncio.run(run())


def test_session_break_fails_over_to_standby() -> None:
    async def run() -> None:
        state = LockState()
        created: list[FakeLeaderConn] = []
        connect = _make_connect(state, created)
        rec_a = Recorder("a-cron")
        rec_b = Recorder("b-cron")
        a = LeaderRunner("d", poll_interval=0.01, retry_delay=0.01, connect=connect)
        b = LeaderRunner("d", poll_interval=0.01, retry_delay=0.01, connect=connect)
        a.attach(LeaderHandle("a-cron", rec_a.start, rec_a.stop))
        b.attach(LeaderHandle("b-cron", rec_b.start, rec_b.stop))
        a.start()
        b.start()
        await _wait_until(lambda: a.is_leader)
        await _wait_until(lambda: rec_a.started == 1)

        # Simulate PG restarting / network cut under the leader's session.
        state.holder.broken = True
        await _wait_until(lambda: not a.is_leader)
        await _wait_until(lambda: rec_a.stopped >= 1)
        await _wait_until(lambda: b.is_leader)
        await _wait_until(lambda: rec_b.started == 1)
        assert state.holder is not None and state.holder in created

        await a.stop()
        await b.stop()

    asyncio.run(run())


def test_stop_without_leadership_is_safe() -> None:
    async def run() -> None:
        state = LockState()
        created: list[FakeLeaderConn] = []
        rec = Recorder("svc")
        runner = LeaderRunner(
            "d", poll_interval=0.05, retry_delay=0.01, connect=_make_connect(state, created)
        )
        runner.attach(LeaderHandle("svc", rec.start, rec.stop))
        # Another (unnamed) holder keeps the lock forever: runner stays follower.
        holder = FakeLeaderConn(state)
        assert state.acquire(holder)
        runner.start()
        await asyncio.sleep(0.03)
        assert not runner.is_leader
        await runner.stop()
        assert rec.started == 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# stage-3 review fixes: exactly-once transitions, serialization, keepalives
# ---------------------------------------------------------------------------


def test_stop_stops_handles_exactly_once() -> None:
    """Graceful shutdown must not run handle.stop() twice (spawned transition
    plus the direct stop path used to race)."""

    async def run() -> None:
        state = LockState()
        created: list[FakeLeaderConn] = []
        rec = Recorder("cron")
        runner = LeaderRunner(
            "postgresql://fake",
            poll_interval=0.01,
            retry_delay=0.01,
            connect=_make_connect(state, created),
        )
        runner.attach(LeaderHandle("cron", rec.start, rec.stop))
        runner.start()
        await _wait_until(lambda: rec.started == 1)
        await runner.stop()
        assert rec.stopped == 1
        assert rec.started == 1

    asyncio.run(run())


def test_stop_serialized_when_session_breaks_mid_start() -> None:
    """Leadership lost while a start is still in flight: stop must wait for
    the start to finish, never interleave with a half-started service."""

    async def run() -> None:
        state = LockState()
        created: list[FakeLeaderConn] = []
        events: list[str] = []

        class Slow:
            async def start(self) -> None:
                events.append("start-begin")
                await asyncio.sleep(0.08)
                events.append("start-end")

            async def stop(self) -> None:
                events.append("stop")

        connect = _make_connect(state, created)
        svc = Slow()
        a = LeaderRunner("d", poll_interval=0.01, retry_delay=0.01, connect=connect)
        b = LeaderRunner("d", poll_interval=0.01, retry_delay=0.01, connect=connect)
        a.attach(LeaderHandle("svc", svc.start, svc.stop))
        a.start()
        b.start()
        await _wait_until(lambda: events[:1] == ["start-begin"])

        # PG dies while the start transition is mid-flight; b takes over so
        # a must not restart the service after its stop transition completes.
        if state.holder is not None:
            state.holder.broken = True
        await _wait_until(lambda: events == ["start-begin", "start-end", "stop"])
        await _wait_until(lambda: b.is_leader)
        await asyncio.sleep(0.05)
        assert events == ["start-begin", "start-end", "stop"]
        await a.stop()
        await b.stop()

    asyncio.run(run())


def test_keepalive_kwargs_only_for_default_psycopg_connect() -> None:
    runner = LeaderRunner("d", connect=_make_connect(LockState(), []))
    assert runner._connect_kwargs() == {}
    default = LeaderRunner("d")
    assert default._connect_kwargs().get("keepalives") == 1


# ---------------------------------------------------------------------------
# review 2026-09-25 fixes: worker role must start no background singletons
# ---------------------------------------------------------------------------


def test_background_gate_disabled_starts_nothing() -> None:
    """enabled=False (worker role) routes nothing and starts no contention."""

    async def run() -> None:
        started: list[str] = []
        attached: list[str] = []

        class FakeRunner:
            def attach(self, handle: Any) -> None:
                attached.append(handle.name)

            def start(self) -> None:
                started.append("start")

        gate = BackgroundGate(None, enabled=False)
        await gate.route("cron-scheduler", lambda: started.append("cron"), lambda: None)
        gate.start()
        assert started == [] and attached == []

        runner = FakeRunner()
        gate_on = BackgroundGate(runner)
        await gate_on.route("proactive-care", lambda: None, lambda: None)
        assert attached == ["proactive-care"]

        # Default (no explicit flag) keeps the standalone inline behaviour.
        inline_started: list[str] = []
        gate_standalone = BackgroundGate(None)
        await gate_standalone.route(
            "im-channels", lambda: inline_started.append("im"), lambda: None
        )
        assert inline_started == ["im"]

    asyncio.run(run())
