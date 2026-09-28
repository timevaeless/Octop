"""Unit tests for cluster plan D2: cross-node channel-change propagation.

Covers, against fake PG connections:
- ``ChannelChangeNotifier`` publishes ``pg_notify`` on the dedicated channel
  and never raises (a failed notify must not fail the channel write);
- ``ChannelChangeWatcher`` invokes the reload callback per notify, survives
  reload errors, and reconnects after a session break;
- Gateway channel writes fire the notify hook (the regression the cluster
  plan calls ``test_channel_change_propagates_to_leader``).

The real NOTIFY/LISTEN round trip across two nodes is exercised by the
stage-3 integration scripts (embedded PostgreSQL), same split as the
leader-runner tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from octop.config import OctopConfig
from octop.infra.agents.manager import AgentManager
from octop.infra.cluster.channel_bus import (
    CHANNEL_CHANGE_CHANNEL,
    ChannelChangeNotifier,
    ChannelChangeWatcher,
)
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.gateway.gateway import ChannelCreateSpec, Gateway
from octop.infra.utils.paths import PathLayout
from octop.infra.utils.ulid import new_ulid

# ---------------------------------------------------------------------------
# fakes: publisher connections and LISTEN connections
# ---------------------------------------------------------------------------


class FakePubConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.closed = False

    async def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    async def close(self) -> None:
        self.closed = True


class FakeWatchConn:
    """LISTEN-side fake: ``notifies()`` drains an inbox fed by the test."""

    def __init__(self) -> None:
        self.closed = False
        self.listened = False
        self.inbox: list[tuple[str, int, str]] = []

    async def execute(self, sql: str, params: Any = None) -> None:
        if sql.startswith("LISTEN"):
            self.listened = True

    async def notifies(self):  # type: ignore[no-untyped-def]
        while True:
            if self.closed:
                raise ConnectionError("session closed")
            if self.inbox:
                yield self.inbox.pop(0)
                continue
            await asyncio.sleep(0.01)

    async def close(self) -> None:
        self.closed = True


def _make_connect(conns: list[Any]) -> Any:
    async def connect(_dsn: str, **_kwargs: Any) -> Any:
        assert conns, "no more fake connections prepared"
        return conns.pop(0)

    return connect


# ---------------------------------------------------------------------------
# ChannelChangeNotifier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notifier_publishes_pg_notify_on_dedicated_channel() -> None:
    conn = FakePubConn()
    notifier = ChannelChangeNotifier("postgresql://dsn", connect=_make_connect([conn]))

    await notifier.publish()

    assert conn.executed == [("SELECT pg_notify(%s, '')", (CHANNEL_CHANGE_CHANNEL,))]


@pytest.mark.asyncio
async def test_notifier_publish_never_raises_and_reconnects() -> None:
    conn1 = FakePubConn()

    async def failing_execute(sql: str, params: Any = None) -> None:
        raise RuntimeError("pg went away mid-notify")

    conn1.execute = failing_execute  # type: ignore[method-assign]
    conn2 = FakePubConn()
    notifier = ChannelChangeNotifier("postgresql://dsn", connect=_make_connect([conn1, conn2]))

    await notifier.publish()  # must not raise
    assert conn1.closed  # broken connection dropped

    await notifier.publish()  # next publish reconnects transparently
    assert conn2.executed == [("SELECT pg_notify(%s, '')", (CHANNEL_CHANGE_CHANNEL,))]


@pytest.mark.asyncio
async def test_notifier_connect_failure_does_not_raise() -> None:
    attempts: list[int] = []

    async def failing_connect(_dsn: str, **_kwargs: Any) -> FakePubConn:
        attempts.append(1)
        raise RuntimeError("pg down")

    notifier = ChannelChangeNotifier("postgresql://dsn", connect=failing_connect)
    await notifier.publish()  # must not raise
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_notifier_aclose_is_idempotent() -> None:
    conn = FakePubConn()
    notifier = ChannelChangeNotifier("postgresql://dsn", connect=_make_connect([conn]))
    await notifier.publish()
    await notifier.aclose()
    assert conn.closed
    await notifier.aclose()  # second close is a no-op


# ---------------------------------------------------------------------------
# ChannelChangeWatcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watcher_reloads_on_notify() -> None:
    conn = FakeWatchConn()
    calls: list[int] = []
    called = asyncio.Event()

    async def on_change() -> None:
        calls.append(1)
        called.set()

    watcher = ChannelChangeWatcher(
        "postgresql://dsn", on_change=on_change, connect=_make_connect([conn])
    )
    await watcher.open()
    assert conn.listened

    conn.inbox.append((CHANNEL_CHANGE_CHANNEL, 1, ""))
    await asyncio.wait_for(called.wait(), timeout=2.0)
    assert calls == [1]

    await watcher.aclose()
    assert conn.closed


@pytest.mark.asyncio
async def test_watcher_survives_reload_error() -> None:
    conn = FakeWatchConn()
    calls: list[int] = []
    called = asyncio.Event()

    async def on_change() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("reload boom")
        called.set()

    watcher = ChannelChangeWatcher(
        "postgresql://dsn", on_change=on_change, connect=_make_connect([conn])
    )
    await watcher.open()

    conn.inbox.append((CHANNEL_CHANGE_CHANNEL, 1, ""))
    conn.inbox.append((CHANNEL_CHANGE_CHANNEL, 1, ""))
    await asyncio.wait_for(called.wait(), timeout=2.0)
    assert len(calls) == 2  # first failure did not kill the watcher

    await watcher.aclose()


@pytest.mark.asyncio
async def test_watcher_reconnects_after_session_break() -> None:
    conn1 = FakeWatchConn()
    conn2 = FakeWatchConn()
    calls: list[int] = []
    called = asyncio.Event()

    async def on_change() -> None:
        calls.append(1)
        called.set()

    watcher = ChannelChangeWatcher(
        "postgresql://dsn",
        on_change=on_change,
        reconnect_delay=0.01,
        connect=_make_connect([conn1, conn2]),
    )
    await watcher.open()

    conn1.inbox.append((CHANNEL_CHANGE_CHANNEL, 1, ""))
    await asyncio.wait_for(called.wait(), timeout=2.0)

    # Simulate a dropped session: the watcher must reconnect and keep working.
    conn1.closed = True
    for _ in range(500):
        if conn2.listened:
            break
        await asyncio.sleep(0.01)
    assert conn2.listened, "watcher did not reconnect"

    called.clear()
    conn2.inbox.append((CHANNEL_CHANGE_CHANNEL, 1, ""))
    await asyncio.wait_for(called.wait(), timeout=2.0)
    assert len(calls) == 2

    await watcher.aclose()


@pytest.mark.asyncio
async def test_watcher_open_is_idempotent() -> None:
    conn = FakeWatchConn()
    watcher = ChannelChangeWatcher(
        "postgresql://dsn", on_change=AsyncMock(), connect=_make_connect([conn])
    )
    await watcher.open()
    await watcher.open()  # second open must not create a second loop/connection
    assert watcher.connection_count == 1
    await watcher.aclose()


# ---------------------------------------------------------------------------
# Gateway wiring: channel writes fire the notify hook
# ---------------------------------------------------------------------------


def _make_services(tmp_path: Path):
    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    return build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())


def _seed_agent(services: Any) -> tuple[str, int]:
    uid = UserRepo(services.db).create(username="alice", password_hash="h", role="admin")
    aid = new_ulid()
    AgentRepo(services.db).create(agent_id=aid, user_id=uid, name="bot")
    return aid, uid


@pytest.mark.asyncio
async def test_channel_change_propagates_to_leader(tmp_path: Path) -> None:
    """Regression for cluster plan D2: every channel write must notify the
    node running the IM pollers (the leader) so it reloads from the DB."""
    services = _make_services(tmp_path)
    agent_id, user_id = _seed_agent(services)
    registry = AgentManager(
        repos=services.repos,
        paths=services.paths,
        config=services.config,
    )
    notified: list[str] = []

    async def on_notify() -> None:
        notified.append("change")

    gw = Gateway(agent_manager=registry, repos=services.repos, channel_notify=on_notify)
    gw._channel_manager = MagicMock()
    gw._channel_manager.add_channel = AsyncMock()
    gw._channel_manager.remove_channel = AsyncMock()
    gw._channel_manager.get_channel = MagicMock(return_value=MagicMock())
    gw._processor = MagicMock()

    channel_id = new_ulid()
    await gw.create_channel(
        ChannelCreateSpec(
            channel_id=channel_id,
            agent_id=agent_id,
            user_id=user_id,
            kind="weixin",
            name="weixin",
            config={"token": "t"},
        )
    )
    assert notified == ["change"]

    await gw.update_channel(channel_id, config_json='{"token": "t2"}', enabled=1)
    assert notified == ["change", "change"]

    await gw.delete_channel(channel_id)
    assert notified == ["change", "change", "change"]

    # A gateway without a notify hook (local mode) keeps working untouched.
    local_gw = Gateway(agent_manager=registry, repos=services.repos)
    local_gw._channel_manager = gw._channel_manager
    local_gw._processor = MagicMock()
    await local_gw.create_channel(
        ChannelCreateSpec(
            channel_id=new_ulid(),
            agent_id=agent_id,
            user_id=user_id,
            kind="weixin",
            name="weixin-local",
            config={"token": "t"},
        )
    )
    assert notified == ["change", "change", "change"]

    await registry.shutdown()
