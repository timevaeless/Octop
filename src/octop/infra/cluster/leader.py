"""Leader election for background singletons (stage 3).

Multi-replica api nodes must run the cron scheduler, IM channel pollers,
proactive-care loops and knowledge-recovery exactly once cluster-wide.
Mechanism (per ``docs/production-ha.md``): a **session-level advisory lock**
held on one dedicated PG connection.

Why session-level instead of TTL leases:

- Crash-safe by construction — PG releases the lock the moment the session
  dies; no TTL tuning, no clock-skew window, no split brain.
- Failover latency = reconnect delay of the standby (seconds).

``LeaderRunner`` attaches *service handles* (start/stop coroutines). It
acquires the lock, starts every handle, and watches the session; on loss
(PG restart, network cut) it stops the services and re-contends. Follows
keep everything constructed — only the background loops are gated.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import psycopg

logger = logging.getLogger(__name__)

#: Distinct from the queue-schema lock (918273645) and worker warmup (918273646).
LEADER_LOCK_KEY = 918273647

#: TCP keepalives on the lock-holding session: a silently dropped connection
#: (NAT expiry, firewall) is detected in ~1 minute instead of hanging until
#: the OS-level TCP timeout (which can be many minutes and delays failover).
_KEEPALIVE_KWARGS: dict[str, Any] = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}

__all__ = ["LEADER_LOCK_KEY", "BackgroundGate", "LeaderHandle", "LeaderRunner"]


@dataclass(frozen=True)
class LeaderHandle:
    """One background singleton: started on leadership, stopped on loss."""

    name: str
    start: Callable[[], Awaitable[None]]
    stop: Callable[[], Awaitable[None]]


class LeaderRunner:
    """Session-advisory-lock leader with attached service handles."""

    def __init__(
        self,
        dsn: str,
        *,
        lock_key: int = LEADER_LOCK_KEY,
        poll_interval: float = 5.0,
        retry_delay: float = 1.0,
        heartbeat_timeout: float | None = None,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self._dsn = dsn
        self._lock_key = lock_key
        self._poll_interval = poll_interval
        self._retry_delay = retry_delay
        # Bounds how long a hung (half-open) heartbeat can delay failover.
        # Default: a few poll cycles; keepalives usually trip first.
        self._heartbeat_timeout = heartbeat_timeout or max(poll_interval * 3.0, 5.0)
        self._connect = connect or psycopg.AsyncConnection.connect
        self._handles: list[LeaderHandle] = []
        self._leader = False
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        # Serializes handle start/stop transitions: leadership can be lost
        # while a start is still in flight (PG restart mid-boot); without
        # the lock stop would run concurrently with a half-finished start.
        self._transition_lock = asyncio.Lock()
        self._transitions: set[asyncio.Task[None]] = set()
        self._handles_running = False

    # ----- registration -----

    def attach(self, handle: LeaderHandle) -> None:
        self._handles.append(handle)

    @property
    def is_leader(self) -> bool:
        return self._leader

    def im_active(self) -> bool:
        """Predicate for :class:`~octop.infra.gateway.gateway.Gateway`."""
        return self._leader

    # ----- lifecycle -----

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="octop-leader-runner")

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - shutdown path
                logger.debug("leader runner stopped", exc_info=True)
        # Wait out any in-flight transition (it may be mid-stop already),
        # then stop the handles exactly once.
        if self._transitions:
            await asyncio.gather(*list(self._transitions), return_exceptions=True)
        async with self._transition_lock:
            if self._handles_running:
                self._handles_running = False
                await self._stop_handles("runner stopped")

    # ----- internals -----

    async def _loop(self) -> None:
        conn: psycopg.AsyncConnection | None = None
        try:
            while not self._stopping:
                try:
                    conn = await self._connect(self._dsn, autocommit=True, **self._connect_kwargs())
                    cur = await conn.execute("SELECT pg_try_advisory_lock(%s)", (self._lock_key,))
                    row = await cur.fetchone()
                    if row is None or not row[0]:
                        with contextlib.suppress(Exception):
                            await conn.close()
                        conn = None
                        self._set_leader(False)
                        await asyncio.sleep(self._poll_interval)
                        continue
                    logger.info("leadership acquired (advisory lock %d)", self._lock_key)
                    self._set_leader(True)
                    await self._watch_session(conn)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if not self._stopping:
                        logger.exception("leader loop error; retrying")
                finally:
                    if conn is not None:
                        with contextlib.suppress(Exception):
                            await conn.close()
                        conn = None
                # Losing the session releases the lock server-side; services
                # must stop before re-contending.
                self._set_leader(False)
                if not self._stopping:
                    await asyncio.sleep(self._retry_delay)
        finally:
            self._set_leader(False)

    async def _watch_session(self, conn: psycopg.AsyncConnection) -> None:
        """Hold the session until it breaks (lock release ⇒ leadership loss)."""
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                cur = await asyncio.wait_for(
                    conn.execute("SELECT 1"), timeout=self._heartbeat_timeout
                )
            except TimeoutError:
                # A hung half-open connection must not delay failover for
                # minutes; treat the timeout as a session break.
                raise RuntimeError(
                    f"leader heartbeat timed out after {self._heartbeat_timeout:.0f}s"
                ) from None
            await cur.fetchone()

    def _set_leader(self, value: bool) -> None:
        if self._leader == value:
            return
        self._leader = value
        if value:
            logger.info(
                "node became leader; starting %d background singleton(s)", len(self._handles)
            )
            self._spawn_transition(True)
        else:
            logger.info("leadership lost; stopping background singletons")
            self._spawn_transition(False)

    def _spawn_transition(self, value: bool) -> None:
        task = asyncio.create_task(self._transition(value), name="octop-leader-transition")
        # Keep a strong reference: bare fire-and-forget tasks can be
        # garbage-collected mid-execution (asyncio only holds weak refs).
        self._transitions.add(task)
        task.add_done_callback(self._transitions.discard)

    async def _transition(self, value: bool) -> None:
        async with self._transition_lock:
            if value:
                if self._handles_running:
                    return
                self._handles_running = True
                await self._start_handles()
            else:
                if not self._handles_running:
                    return
                self._handles_running = False
                await self._stop_handles("leadership lost")

    async def _start_handles(self) -> None:
        for handle in self._handles:
            try:
                await handle.start()
                logger.info("leader service started: %s", handle.name)
            except Exception:
                logger.exception("leader service failed to start: %s", handle.name)

    async def _stop_handles(self, reason: str) -> None:
        for handle in reversed(self._handles):
            try:
                await handle.stop()
                logger.info("leader service stopped: %s (%s)", handle.name, reason)
            except Exception:
                logger.exception("leader service failed to stop: %s", handle.name)

    def _connect_kwargs(self) -> dict[str, Any]:
        """libpq keepalives only apply to the real psycopg connector; test
        fakes and alternative connectors get a plain keyword set."""
        # NOTE: bound-classmethod equality, not `is` — each access to
        # ``AsyncConnection.connect`` yields a new bound method object.
        if self._connect == psycopg.AsyncConnection.connect:
            return dict(_KEEPALIVE_KWARGS)
        return {}


def _as_async(fn: Callable[..., Any]) -> Callable[[], Awaitable[None]]:
    """Adapt a sync or async zero-arg callable to an async callable."""

    async def _wrapped() -> None:
        out = fn()
        if inspect.isawaitable(out):
            await out

    return _wrapped


class BackgroundGate:
    """Routes background singletons: direct on a single node, leader-gated in a cluster.

    Kept in the isolated ``octop.infra.cluster`` package so the wiring in
    ``server.py`` stays a handful of ``gate.route(...)`` calls — minimising
    merge surface against upstream changes to server bootstrapping.

    Three node roles:
    - standalone (``runner=None, enabled=True``): singletons start immediately,
      matching the pre-cluster behaviour;
    - cluster member competing for leadership (``runner`` set): singletons are
      attached as :class:`LeaderHandle` and only run on the cluster leader;
    - cluster member that never runs background tasks (``enabled=False``):
      agent-worker role. ``runner=None`` alone cannot express this — without
      the explicit flag a worker was misread as "standalone" and booted every
      singleton (review P1-2: N workers re-running cron / IM / proactive).
    """

    def __init__(self, runner: LeaderRunner | None, *, enabled: bool | None = None) -> None:
        self._runner = runner
        # Default keeps the historical standalone interpretation: run inline.
        # Disabled mode is opt-in via server wiring (worker role).
        self._enabled = True if enabled is None else enabled

    async def route(
        self,
        name: str,
        start: Callable[..., Any],
        stop: Callable[..., Any],
    ) -> None:
        """Start the singleton now (standalone) or attach it to the leader."""
        if not self._enabled:
            logger.debug("background singleton %r skipped: node runs no background tasks", name)
            return
        if self._runner is None:
            await _as_async(start)()
            return
        self._runner.attach(LeaderHandle(name, _as_async(start), _as_async(stop)))

    def start(self) -> None:
        """Begin leadership contention (no-op standalone / disabled)."""
        if self._runner is not None:
            self._runner.start()
