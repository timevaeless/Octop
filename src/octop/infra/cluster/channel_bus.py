"""Cross-node channel-change propagation (cluster plan D2, batch 2).

IM channel create/update/delete may land on any api node behind the LB,
but only the leader registers channel pollers. Previously the write was
persisted while the leader's runtime stayed stale — the channel never went
live (or a deleted one kept polling) until a restart.

Mechanism (plan option A): after every channel write the node fires one PG
``NOTIFY`` on a dedicated channel; the node running the IM pollers holds a
``LISTEN`` connection and re-runs ``reload_channels_from_db`` per notify.

Design notes:

- The DB row is the source of truth and the reload is idempotent, so a
  dropped notify only costs latency until the next change — publishing is
  therefore best-effort and must never fail the write itself.
- ``ChannelChangeNotifier`` mirrors ``_CancelNotifier`` (persistent,
  lazily-opened connection with reconnect); ``ChannelChangeWatcher`` mirrors
  ``NotifyHub`` (one LISTEN connection per node, ``conn.notifies()`` loop).
  Both stay in the isolated ``octop.infra.cluster`` package.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg

logger = logging.getLogger(__name__)

#: NOTIFY channel carrying IM channel configuration changes.
CHANNEL_CHANGE_CHANNEL = "octop_channel_changes"

__all__ = ["CHANNEL_CHANGE_CHANNEL", "ChannelChangeNotifier", "ChannelChangeWatcher"]


class ChannelChangeNotifier:
    """Best-effort publisher over one persistent, lazily-opened connection."""

    def __init__(
        self,
        dsn: str,
        *,
        channel: str = CHANNEL_CHANGE_CHANNEL,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self._dsn = dsn
        self._channel = channel
        self._connect = connect or psycopg.AsyncConnection.connect
        self._conn: psycopg.AsyncConnection | None = None

    async def publish(self) -> None:
        """Fire one change notify; failures are logged, never raised."""
        try:
            conn = await self._ensure_conn()
            await conn.execute("SELECT pg_notify(%s, '')", (self._channel,))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("channel-change notify failed")
            stale = self._conn
            self._conn = None
            if stale is not None:
                with contextlib.suppress(Exception):
                    await stale.close()

    async def _ensure_conn(self) -> psycopg.AsyncConnection:
        if self._conn is not None and not self._conn.closed:
            return self._conn
        self._conn = await self._connect(self._dsn, autocommit=True)
        return self._conn

    async def aclose(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close()


class ChannelChangeWatcher:
    """LISTEN connection that reloads IM channels on every change notify.

    One dedicated connection per node (O(nodes), same as :class:`NotifyHub`).
    The reload callback runs once per notify; ``reload_channels_from_db`` is
    idempotent, so a burst of changes converges on the full DB state.
    """

    def __init__(
        self,
        dsn: str,
        *,
        on_change: Callable[[], Awaitable[None]],
        channel: str = CHANNEL_CHANGE_CHANNEL,
        reconnect_delay: float = 1.0,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self._dsn = dsn
        self._on_change = on_change
        self._channel = channel
        self._reconnect_delay = reconnect_delay
        self._connect = connect or psycopg.AsyncConnection.connect
        self._conn: psycopg.AsyncConnection | None = None
        self._task: asyncio.Task[None] | None = None

    async def open(self) -> None:
        """Connect, LISTEN, and start the dispatch loop (idempotent)."""
        if self._task is not None:
            return
        self._conn = await self._connect(self._dsn, autocommit=True)
        await self._conn.execute(f"LISTEN {self._channel}")
        self._task = asyncio.create_task(self._loop(), name="octop-channel-change-watcher")

    async def _loop(self) -> None:
        while True:
            conn = self._conn
            if conn is None:
                return
            try:
                async for _notify in conn.notifies():
                    try:
                        await self._on_change()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # One failed reload must not kill the watcher; the
                        # next notify (or any later change) re-converges.
                        logger.exception("channel reload after notify failed")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("channel-change watcher lost connection; reconnecting")
                with contextlib.suppress(Exception):
                    await conn.close()
                await asyncio.sleep(self._reconnect_delay)
                try:
                    self._conn = await self._connect(self._dsn, autocommit=True)
                    await self._conn.execute(f"LISTEN {self._channel}")
                except Exception:
                    logger.exception("channel-change watcher reconnect failed; retrying")

    @property
    def connection_count(self) -> int:
        """Number of PG connections held (1 while healthy, 0 while reconnecting)."""
        return 1 if self._conn is not None and not self._conn.closed else 0

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None
