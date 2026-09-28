"""Unit tests for cluster plan D5: cross-process migration serialization.

``run_migrations`` has nine call sites (server boot, CLI init/admin/backup,
system-archive restore, DB rebind); with ``docker compose --scale`` several
nodes boot concurrently, so on PostgreSQL the whole body must run under a
session-level advisory lock held on one dedicated pool connection. SQLite
(single-writer WAL) keeps the historical behaviour. The real advisory-lock
round trip across two processes is exercised by the stage-3 integration
scripts (embedded PostgreSQL), same split as the leader-runner tests.

Review round 2 (D5): the lock is taken with ``pg_try_advisory_lock``
polling — waiters log their wait and an over-long wait fails loudly —
instead of a silent blocking ``pg_advisory_lock`` that made booting nodes
look dead.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from octop.infra.db import migrate
from octop.infra.db.migrate import MIGRATION_LOCK_KEY, run_migrations

TRY_LOCK_SQL = "SELECT pg_try_advisory_lock(?)"
UNLOCK_SQL = "SELECT pg_advisory_unlock(?)"


class _FakeCursor:
    def __init__(self, value: Any) -> None:
        self._value = value

    def fetchone(self) -> tuple[Any, ...]:
        return (self._value,)


class _FakeLockConn:
    """Fake pool connection whose try-lock results come from a script.

    ``try_results`` is consumed in order for each try-lock call; the last
    value repeats once the script is exhausted (default: immediate success).
    """

    def __init__(
        self,
        log: list[tuple[str, Any]],
        try_results: list[Any] | None = None,
    ) -> None:
        self._log = log
        self._try_results = list(try_results or [True])

    def execute(self, sql: str, params: Any = None) -> _FakeCursor:
        self._log.append((sql, params))
        if sql == TRY_LOCK_SQL:
            value = self._try_results.pop(0) if len(self._try_results) > 1 else self._try_results[0]
            return _FakeCursor(value)
        return _FakeCursor(None)


class _FakePool:
    def __init__(self, dialect: str, conn: _FakeLockConn) -> None:
        self.dialect = dialect
        self._conn = conn

    @contextmanager
    def connect(self) -> Iterator[_FakeLockConn]:
        yield self._conn


@pytest.fixture()
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(migrate.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


@pytest.fixture()
def body_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake_body(db: Any) -> None:
        calls.append("body")

    monkeypatch.setattr(migrate, "_run_migrations_body", fake_body)
    return calls


def test_postgresql_runs_body_under_advisory_lock(
    monkeypatch: pytest.MonkeyPatch, body_calls: list[str]
) -> None:
    log: list[tuple[str, Any]] = []
    pool = _FakePool("postgresql", _FakeLockConn(log))

    run_migrations(pool)  # type: ignore[arg-type]

    assert body_calls == ["body"]
    # Order: try-lock (acquired first attempt) → body → unlock → conn released.
    assert log[0][0] == TRY_LOCK_SQL
    assert log[0][1] == (MIGRATION_LOCK_KEY,)
    assert log[-1][0] == "__exit__" or log[-1][0] == UNLOCK_SQL
    assert [sql for sql, _ in log if sql in (TRY_LOCK_SQL, UNLOCK_SQL)] == [
        TRY_LOCK_SQL,
        UNLOCK_SQL,
    ]


def test_sqlite_runs_body_without_lock(
    monkeypatch: pytest.MonkeyPatch, body_calls: list[str]
) -> None:
    log: list[tuple[str, Any]] = []
    pool = _FakePool("sqlite", _FakeLockConn(log))

    run_migrations(pool)  # type: ignore[arg-type]

    assert body_calls == ["body"]
    assert log == []  # no advisory lock, no dedicated connection


def test_unlock_runs_when_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[tuple[str, Any]] = []
    pool = _FakePool("postgresql", _FakeLockConn(log))

    def exploding_body(db: Any) -> None:
        log.append(("body",))
        raise RuntimeError("migration failed")

    monkeypatch.setattr(migrate, "_run_migrations_body", exploding_body)

    with pytest.raises(RuntimeError):
        run_migrations(pool)  # type: ignore[arg-type]

    sqls = [
        sql for entry in log if len(entry) == 2 and (sql := entry[0]) in (TRY_LOCK_SQL, UNLOCK_SQL)
    ]
    assert sqls == [TRY_LOCK_SQL, UNLOCK_SQL]  # lock is always released


def test_lock_waits_and_retries_until_acquired(
    monkeypatch: pytest.MonkeyPatch, body_calls: list[str], no_sleep: list[float]
) -> None:
    """Two competing nodes: node B's first polls fail, then it gets in."""
    monkeypatch.setattr(migrate, "_MIGRATION_LOCK_POLL_SECONDS", 0.0)
    log: list[tuple[str, Any]] = []
    pool = _FakePool("postgresql", _FakeLockConn(log, try_results=[False, False, True]))

    run_migrations(pool)  # type: ignore[arg-type]

    try_locks = [sql for sql, _ in log if sql == TRY_LOCK_SQL]
    assert len(try_locks) == 3  # two failed attempts, then success
    assert body_calls == ["body"]  # body only runs once the lock is held
    assert len(no_sleep) == 2  # slept between failed attempts


def test_lock_wait_times_out_loudly(
    monkeypatch: pytest.MonkeyPatch, body_calls: list[str], no_sleep: list[float]
) -> None:
    """A lock that never frees fails the boot with a clear error, and the
    migration body never runs."""
    monkeypatch.setattr(migrate, "_MIGRATION_LOCK_POLL_SECONDS", 0.0)
    monkeypatch.setattr(migrate, "MIGRATION_LOCK_MAX_WAIT_SECONDS", 0.0)
    log: list[tuple[str, Any]] = []
    pool = _FakePool("postgresql", _FakeLockConn(log, try_results=[False]))

    with pytest.raises(TimeoutError, match="migration lock"):
        run_migrations(pool)  # type: ignore[arg-type]

    assert body_calls == []
    assert UNLOCK_SQL not in [sql for sql, _ in log]  # nothing to unlock


def test_lock_key_is_distinct_from_other_cluster_locks() -> None:
    # Guard against accidental reuse of the documented lock-key space.
    # Community builds without the cluster module have no other lock keys.
    pytest.importorskip("octop.infra.cluster.leader")
    from octop.infra.cluster import remote
    from octop.infra.cluster.leader import LEADER_LOCK_KEY

    assert MIGRATION_LOCK_KEY not in {remote._QUEUE_SCHEMA_LOCK_KEY, LEADER_LOCK_KEY}
