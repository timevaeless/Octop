"""Tests for the pluggable checkpointer warm-up hook (thread-deletion fix).

Root cause regression: after a process restart the default AsyncSqliteSaver
connection is lazily unopened, and langgraph's ``adelete_thread`` — unlike
every other saver op — never ensures the connection, so deleting a thread
before the agent's first turn raised ``ValueError("no active connection")``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from octop.infra.agents.checkpointer_warmup import warm_up_checkpointer


class _SqliteLikeSaver:
    """AsyncSqliteSaver shape: async ``setup`` + a ``conn`` attribute."""

    def __init__(self) -> None:
        self.conn: Any = object()  # lazily-connected aiosqlite stand-in
        self.setup_calls = 0

    async def setup(self) -> None:
        self.setup_calls += 1


class _MemoryLikeSaver:
    """octop_memory.Memory shape: self-managed connection, no setup/conn."""

    def __init__(self) -> None:
        self.adelete_thread_calls = 0

    async def adelete_thread(self, thread_id: str) -> None:
        self.adelete_thread_calls += 1


class _SyncSetupSaver:
    """Non-coroutine ``setup`` (unknown saver / test double) — must be skipped."""

    def __init__(self) -> None:
        self.conn: Any = object()
        self.setup_calls = 0

    def setup(self) -> None:  # sync — not the AsyncSqliteSaver shape
        self.setup_calls += 1


class _ExplodingSaver:
    def __init__(self) -> None:
        self.conn: Any = object()

    async def setup(self) -> None:
        raise ValueError("no active connection")


@pytest.mark.asyncio
async def test_warms_up_sqlite_saver() -> None:
    saver = _SqliteLikeSaver()
    harness = MagicMock()
    harness.checkpointer = saver

    await warm_up_checkpointer(harness)

    assert saver.setup_calls == 1


@pytest.mark.asyncio
async def test_skips_memory_backed_saver() -> None:
    saver = _MemoryLikeSaver()
    harness = MagicMock()
    harness.checkpointer = saver

    await warm_up_checkpointer(harness)

    assert saver.adelete_thread_calls == 0  # untouched — its own path


@pytest.mark.asyncio
async def test_skips_when_no_conn_attribute() -> None:
    saver = _SyncSetupSaver.__new__(_SyncSetupSaver)  # shape without conn
    harness = MagicMock()
    harness.checkpointer = saver

    await warm_up_checkpointer(harness)  # must not raise / not call setup


@pytest.mark.asyncio
async def test_skips_sync_setup_test_double() -> None:
    saver = _SyncSetupSaver()
    harness = MagicMock()
    harness.checkpointer = saver

    await warm_up_checkpointer(harness)

    assert saver.setup_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpointer", [False, None])
async def test_skips_when_checkpointer_disabled(checkpointer: Any) -> None:
    harness = MagicMock()
    harness.checkpointer = checkpointer

    await warm_up_checkpointer(harness)  # no-op, must not raise


@pytest.mark.asyncio
async def test_skips_when_harness_has_no_checkpointer_attr() -> None:
    harness = MagicMock(spec=[])  # no attributes at all

    await warm_up_checkpointer(harness)  # must not raise


@pytest.mark.asyncio
async def test_setup_failure_propagates() -> None:
    harness = MagicMock()
    harness.checkpointer = _ExplodingSaver()

    with pytest.raises(ValueError, match="no active connection"):
        await warm_up_checkpointer(harness)


@pytest.mark.asyncio
async def test_magicmock_harness_is_skipped() -> None:
    """Bare MagicMock doubles (existing manager-test fixtures) must be inert."""
    harness = MagicMock()  # checkpointer/setup/conn are auto-MagicMock attrs

    await warm_up_checkpointer(harness)  # must not raise
