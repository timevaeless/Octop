"""Remote turn execution over a PostgreSQL task queue (cluster module).

Everything in this file is cluster-only: the community single-node edition
(SQLite + ``InProcessExecutor``) never imports it. The only core wiring
points are two lazy, guarded imports (``infra/server.py`` executor assembly
and the ``octop agent-worker`` CLI) plus the ``octop.api.cluster`` routes —
see the pluggability contract in
``docs/cluster-implementation-plan-2026-09-25.md`` §8.

Implements the queue-backed side of ``docs/microservice-scaling-1000.md`` §4:
:class:`RemoteExecutor` (api side) defers turn jobs to a Procrastinate
(PostgreSQL) queue instead of running them in-process; an :class:`AgentWorker`
(agent side) consumes the jobs with a local :class:`AgentExecutor` (typically
:class:`~octop.infra.agents.executor.InProcessExecutor` over ``AgentManager``)
and streams every chunk back through a dedicated events table + PostgreSQL
``NOTIFY`` (channel A of PoC 2 — measured p95 ≤ 51 ms at 50 ms flush).

Wire format
------------
- Job payload: ``{"kind": "stream|resume|call", "job_id", "agent_id",
  "thread_id", "request"/"decisions"}`` — JSON-serializable by construction.
- Event row (``octop_turn_events``): ``{"c": <chunk JSON text>, "ts": <epoch>}``
  with ``ts`` used by integration tests for latency measurement.
- Sentinels: ``__turn_end__`` terminates the reader; ``__turn_error__`` raises
  :class:`RemoteTurnError` on the api side (mirrors a local executor raising —
  callers such as ``GlobalProcessor`` already convert exceptions into user
  facing error frames).
- Per-thread serialization: jobs defer with procrastinate ``lock`` =
  ``octop-turn:{agent_id}:{thread_id}`` — procrastinate guarantees jobs sharing
  a lock run one at a time in creation order, replacing the in-process
  ``asyncio.Lock`` (PoC 1 phase 3 requirement).
- Cancel: api side publishes ``pg_notify('octop_turn_cancel', {agent_id,
  thread_id})``; the worker listens and forwards to its local executor, whose
  harness stream ends, which closes the event stream with an end sentinel.
  Cancel only affects the *active* stream — matching local semantics. Queued
  (not yet started) jobs of the same thread are not aborted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

import psycopg

from octop.infra.utils.json_serialize import json_chunk_default
from octop.infra.utils.ulid import new_ulid

if TYPE_CHECKING:
    import procrastinate

    from octop.infra.agents.executor import AgentExecutor

logger = logging.getLogger(__name__)

__all__ = [
    "AgentWorker",
    "NotifyHub",
    "ORPHAN_JOB_STATUSES",
    "ProcrastinateTurnQueue",
    "RemoteExecutor",
    "RemoteTurnError",
    "TurnEventPublisher",
    "TurnFetchPool",
    "TurnStreamReader",
    "TurnQueue",
    "build_worker_app",
    "encode_chunk",
    "ensure_queue_schema",
    "ensure_turn_schema",
    "prune_turn_events",
    "turn_job_status",
]

QUEUE_NAME = "octop_agent_turns"
TASK_NAME = "octop_agent.execute_turn"
EVENTS_TABLE = "octop_turn_events"
EVENTS_CHANNEL = "octop_turn_events"
CANCEL_CHANNEL = "octop_turn_cancel"

CHUNK_KIND_RESULT = "__turn_result__"
CHUNK_KIND_END = "__turn_end__"
CHUNK_KIND_ERROR = "__turn_error__"

#: Design default validated by PoC 2 (p95 <= 51 ms end to end at 100 tok/s).
FLUSH_SECONDS = 0.05
#: Fallback poll for consumers in case a NOTIFY is lost (correctness never
#: depends on it; a lost notify costs at most one poll interval of latency).
POLL_INTERVAL = 1.0

#: How often each worker scans for stalled turn jobs (review P0-1 self-heal).
STALLED_RETRY_INTERVAL_SECONDS = 60.0
#: Worker-heartbeat gap after which a ``doing`` job's worker counts as dead.
STALLED_HEARTBEAT_SECONDS = 60.0
#: A hung turn (worker alive, heartbeat fresh, run stuck — LLM upstream stall,
#: tool that never returns) cannot be re-dispatched by the stalled-retry loop
#: without risking a double-run. Instead ``_execute_turn`` aborts the run at
#: this bound (cluster review D15): the job fails, the error sentinel reaches
#: the reader, and the per-thread queue ``lock`` is released.
TURN_JOB_MAX_RUNTIME_SECONDS = 2100.0


class RemoteTurnError(RuntimeError):
    """Raised on the api side when the worker reported a turn failure."""


def encode_chunk(chunk: Any) -> str:
    """JSON-encode one harness chunk (same fallback as the api SSE writer)."""
    return json.dumps(chunk, ensure_ascii=False, default=json_chunk_default)


async def ensure_turn_schema(dsn: str) -> None:
    """Create the turn-events table if absent (idempotent, both roles)."""
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {EVENTS_TABLE} (
                seq BIGSERIAL PRIMARY KEY,
                job_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        # Deployments that created the table before stage 3 lack the column.
        await conn.execute(
            f"ALTER TABLE {EVENTS_TABLE} "
            f"ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()"
        )
        await conn.execute(
            f"CREATE INDEX IF NOT EXISTS {EVENTS_TABLE}_job_seq_idx ON {EVENTS_TABLE} (job_id, seq)"
        )
        await conn.execute(
            f"CREATE INDEX IF NOT EXISTS {EVENTS_TABLE}_created_idx ON {EVENTS_TABLE} (created_at)"
        )
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {ACTIVE_JOBS_TABLE} (
                thread_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'stream',
                claimed_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )


#: thread → in-flight job mapping (cluster plan D3). The worker UPSERTs on
#: claim and deletes on completion, so any api node can discover which job is
#: streaming a thread's turn and re-attach after the original node died
#: (rolling-upgrade / crash continuity). Per-thread queue serialisation
#: guarantees at most one claimed job per thread.
ACTIVE_JOBS_TABLE = "octop_turn_active_jobs"


class ActiveJobRegistry:
    """Persisted thread → job_id map for in-flight remote turns (plan D3).

    Best-effort by design: a failed claim/clear never breaks the turn itself
    (the DB row is only the discovery aid; the events table stays the source
    of truth). Every call uses a short dedicated connection — lookups are
    rare (attach time) and claims happen once per turn.
    """

    def __init__(self, dsn: str, *, connect: Callable[..., Any] | None = None) -> None:
        self._dsn = dsn
        self._connect = connect or psycopg.AsyncConnection.connect

    async def claim(self, *, agent_id: str, thread_id: str, job_id: str, kind: str) -> None:
        """Mark ``job_id`` as the active turn for ``thread_id`` (UPSERT)."""
        conn = await self._connect(self._dsn, autocommit=True)
        try:
            await conn.execute(
                f"INSERT INTO {ACTIVE_JOBS_TABLE} (thread_id, agent_id, job_id, kind) "
                f"VALUES (%s, %s, %s, %s) "
                f"ON CONFLICT (thread_id) DO UPDATE SET agent_id = EXCLUDED.agent_id, "
                f"job_id = EXCLUDED.job_id, kind = EXCLUDED.kind, claimed_at = now()",
                (thread_id, agent_id, job_id, kind),
            )
        finally:
            await conn.close()

    async def clear(self, *, thread_id: str, job_id: str) -> None:
        """Remove the mapping unless a newer job already claimed the row."""
        conn = await self._connect(self._dsn, autocommit=True)
        try:
            await conn.execute(
                f"DELETE FROM {ACTIVE_JOBS_TABLE} WHERE thread_id = %s AND job_id = %s",
                (thread_id, job_id),
            )
        finally:
            await conn.close()

    async def active_job_id(self, thread_id: str) -> str | None:
        """Return the job currently executing for ``thread_id`` (None if idle)."""
        conn = await self._connect(self._dsn, autocommit=True)
        try:
            cur = await conn.execute(
                f"SELECT job_id FROM {ACTIVE_JOBS_TABLE} WHERE thread_id = %s",
                (thread_id,),
            )
            row = await cur.fetchone()
            return str(row[0]) if row is not None else None
        finally:
            await conn.close()


#: procrastinate job statuses after which a mapped job can never produce new
#: turn events again (cluster plan D3 orphan handling: a stale mapping row —
#: worker SIGKILLed mid-turn, retries exhausted, job cancelled — must not
#: leave ``attach`` waiting the full reader watchdog). ``aborting`` is a
#: transient on the way to ``aborted``. ``todo`` / ``doing`` / ``succeeded``
#: stay attachable: ``todo`` jobs were re-queued by the stalled-retry loop
#: and will run; ``succeeded`` jobs replay to their end sentinel and
#: terminate immediately.
ORPHAN_JOB_STATUSES = frozenset({"failed", "cancelled", "aborting", "aborted"})


async def turn_job_status(
    dsn: str,
    job_id: str,
    *,
    connect: Callable[..., Any] | None = None,
) -> str | None:
    """Look up a turn job's procrastinate status by its payload ``job_id``.

    The D3 mapping stores our ULID ``job_id`` inside the job payload, not
    procrastinate's own row id, so the lookup goes through the JSONB args.
    Returns ``None`` when no row matches (housekeeping pruned it) — for
    attach purposes that outcome is terminal too.
    """
    conn = await (connect or psycopg.AsyncConnection.connect)(dsn, autocommit=True)
    try:
        cur = await conn.execute(
            "SELECT status::text FROM procrastinate_jobs "
            "WHERE task_name = %s AND args->'payload'->>'job_id' = %s "
            "ORDER BY id DESC LIMIT 1",
            (TASK_NAME, job_id),
        )
        row = await cur.fetchone()
        return str(row[0]) if row is not None else None
    finally:
        await conn.close()


#: Events older than this are pruned periodically (bounds table growth while
#: keeping a reconnect/replay window for slow SSE clients).
EVENT_RETENTION_SECONDS = 1800.0
#: How often each worker prunes expired turn events.
PRUNE_INTERVAL_SECONDS = 600.0
#: Max rows deleted per prune statement (bounds transaction size / lock time).
PRUNE_BATCH_SIZE = 5000


async def prune_turn_events(
    dsn: str,
    *,
    retention_seconds: float = EVENT_RETENTION_SECONDS,
    batch_size: int = PRUNE_BATCH_SIZE,
) -> int:
    """Delete turn events older than the retention window. Returns rowcount.

    Deletes in bounded batches so a first run against a large backlog
    cannot hold a huge transaction / row locks for a long time.
    """
    total = 0
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        while True:
            cur = await conn.execute(
                f"DELETE FROM {EVENTS_TABLE} WHERE seq IN ("
                f"SELECT seq FROM {EVENTS_TABLE} "
                f"WHERE created_at < now() - make_interval(secs => %s) "
                f"LIMIT %s)",
                (retention_seconds, batch_size),
            )
            deleted = max(cur.rowcount or 0, 0)
            total += deleted
            if deleted < batch_size:
                return total


_QUEUE_SCHEMA_LOCK_KEY = 918273645


async def ensure_queue_schema(dsn: str) -> None:
    """Install the procrastinate schema exactly once, race-free.

    procrastinate's ``schema.sql`` is NOT idempotent (plain ``CREATE TYPE``),
    so several nodes booting concurrently would collide. Serialize via a
    fixed PG advisory lock and skip when ``procrastinate_jobs`` exists.
    """
    from importlib import resources

    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        await conn.execute("SELECT pg_advisory_lock(%s)", (_QUEUE_SCHEMA_LOCK_KEY,))
        cur = await conn.execute("SELECT to_regclass('public.procrastinate_jobs')")
        row = await cur.fetchone()
        if row is None or row[0] is None:
            sql = (resources.files("procrastinate.sql") / "schema.sql").read_text(encoding="utf-8")
            await conn.execute(sql)
        else:
            logger.debug("procrastinate schema already installed; skipping apply")
    finally:
        with contextlib.suppress(Exception):
            await conn.execute("SELECT pg_advisory_unlock(%s)", (_QUEUE_SCHEMA_LOCK_KEY,))
        await conn.close()


# ---------------------------------------------------------------------------
# api side: queue + reader + executor
# ---------------------------------------------------------------------------


class NotifyHubLike(Protocol):
    """Per-process notification fan-out (real impl: :class:`NotifyHub`)."""

    def subscribe(self, job_id: str) -> asyncio.Event: ...

    def unsubscribe(self, job_id: str, event: asyncio.Event) -> None: ...


class FetchPoolLike(Protocol):
    """Borrowed-connection event fetching (real impl: :class:`TurnFetchPool`)."""

    async def fetch(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]: ...


class NotifyHub:
    """One LISTEN connection per process, dispatching by job payload.

    Stage 3 convergence: previously every active stream held a dedicated
    LISTEN connection (O(streams) connections). The hub holds exactly one
    per api process and wakes per-turn waiters through ``asyncio.Event``.
    Readers still poll as a fallback, so a lost hub only costs latency,
    never correctness.
    """

    def __init__(
        self,
        dsn: str,
        *,
        channel: str = EVENTS_CHANNEL,
        reconnect_delay: float = 1.0,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self._dsn = dsn
        self._channel = channel
        self._reconnect_delay = reconnect_delay
        self._connect = connect or psycopg.AsyncConnection.connect
        self._subs: dict[str, set[asyncio.Event]] = {}
        self._conn: psycopg.AsyncConnection | None = None
        self._task: asyncio.Task[None] | None = None

    async def open(self) -> None:
        self._conn = await self._connect(self._dsn, autocommit=True)
        await self._conn.execute(f"LISTEN {self._channel}")
        self._task = asyncio.create_task(self._dispatch_loop(), name="octop-turn-notify-hub")

    async def _dispatch_loop(self) -> None:
        while True:
            conn = self._conn
            if conn is None:
                return
            try:
                async for notify in conn.notifies():
                    for event in tuple(self._subs.get(notify.payload, ())):
                        event.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("notify hub dispatch failed; reconnecting")
                with contextlib.suppress(Exception):
                    await conn.close()
                await asyncio.sleep(self._reconnect_delay)
                try:
                    self._conn = await self._connect(self._dsn, autocommit=True)
                    await self._conn.execute(f"LISTEN {self._channel}")
                except Exception:
                    logger.exception("notify hub reconnect failed; retrying")

    def subscribe(self, job_id: str) -> asyncio.Event:
        event = asyncio.Event()
        self._subs.setdefault(job_id, set()).add(event)
        return event

    def unsubscribe(self, job_id: str, event: asyncio.Event) -> None:
        events = self._subs.get(job_id)
        if events is not None:
            events.discard(event)
            if not events:
                self._subs.pop(job_id, None)

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
        self._subs.clear()


class TurnFetchPool:
    """Small fixed pool of connections for incremental event SELECTs.

    Replaces the per-reader dedicated fetch connection (the second half of
    the O(streams) connection cost). Exhaustion blocks callers, which is the
    desired backpressure under load.
    """

    def __init__(
        self,
        dsn: str,
        *,
        size: int = 8,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self._dsn = dsn
        self._size = size
        self._connect = connect or psycopg.AsyncConnection.connect
        # ``None`` entries are wake sentinels: capacity released while a
        # waiter may be blocked on an empty queue.
        self._idle: asyncio.Queue[psycopg.AsyncConnection | None] = asyncio.Queue()
        self._open_count = 0

    async def fetch(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        conn = await self._acquire()
        try:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        except BaseException:
            # Broken connection OR cancellation (CancelledError is a
            # BaseException): drop the connection, free the reserved slot and
            # wake any waiter blocked on the idle queue. Slot must be released
            # and the sentinel queued *before* awaiting close() — a second
            # cancellation during close() must not skip the wakeup.
            self._open_count -= 1
            self._idle.put_nowait(None)
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await conn.close()
            raise
        self._idle.put_nowait(conn)
        return rows

    async def _acquire(self) -> psycopg.AsyncConnection:
        while True:
            if self._idle.empty() and self._open_count < self._size:
                # Reserve the slot before awaiting so concurrent acquires cannot
                # oversubscribe; roll the reservation back if the connect fails
                # or is cancelled (BaseException, not just Exception) or the
                # pool capacity would permanently shrink.
                self._open_count += 1
                try:
                    return await self._connect(self._dsn, autocommit=True)
                except BaseException:
                    self._open_count -= 1
                    raise
            item = await self._idle.get()
            if item is None:
                # Wake sentinel: a slot was released by fetch()'s error path.
                continue
            return item

    async def aclose(self) -> None:
        while not self._idle.empty():
            item = self._idle.get_nowait()
            if item is None:  # wake sentinel
                continue
            self._open_count -= 1
            with contextlib.suppress(Exception):
                await item.close()
        self._open_count = 0


class TurnQueue(Protocol):
    """Turn-job deferral seam (procrastinate in production, fakes in tests)."""

    async def defer(self, payload: dict[str, Any], *, lock: str) -> None: ...

    async def aclose(self) -> None: ...


class ProcrastinateTurnQueue:
    """Defers turn jobs through a Procrastinate app (PostgreSQL queue)."""

    def __init__(self, dsn: str, *, queue_name: str = QUEUE_NAME) -> None:
        self._dsn = dsn
        self._queue_name = queue_name
        self._app: procrastinate.App | None = None

    async def open(self) -> None:
        import procrastinate  # noqa: PLC0415

        # The turn-events table must exist before the first job runs.
        await ensure_turn_schema(self._dsn)
        await ensure_queue_schema(self._dsn)
        app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=self._dsn))
        await app.open_async()
        self._app = app

    async def defer(self, payload: dict[str, Any], *, lock: str) -> None:
        if self._app is None:
            raise RemoteTurnError("queue not opened")
        await self._app.configure_task(
            name=TASK_NAME, queue=self._queue_name, lock=lock
        ).defer_async(payload=payload)

    async def aclose(self) -> None:
        if self._app is not None:
            await self._app.close_async()
            self._app = None


class TurnStreamReader:
    """Consumes one turn's chunks: LISTEN wakeup + incremental SELECT.

    ``turn_timeout`` is a watchdog on *progress* (time since the last event
    row): if a worker dies without publishing an end/error sentinel, the
    reader raises instead of waiting forever. ``None`` disables it.
    """

    def __init__(
        self,
        dsn: str,
        job_id: str,
        *,
        channel: str = EVENTS_CHANNEL,
        poll_interval: float = POLL_INTERVAL,
        turn_timeout: float | None = 1800.0,
        hub: NotifyHubLike | None = None,
        fetch: FetchPoolLike | None = None,
    ) -> None:
        self._dsn = dsn
        self._job_id = job_id
        self._channel = channel
        self._poll_interval = poll_interval
        self._turn_timeout = turn_timeout
        # Shared per-process hub/pool (stage 3): no dedicated connections.
        # When either is missing the reader falls back to its own two
        # connections (standalone usage, tests).
        self._hub = hub
        self._fetch = fetch

    async def chunks(self) -> AsyncIterator[dict[str, Any]]:
        if self._hub is not None and self._fetch is not None:
            async for chunk in self._chunks_shared():
                yield chunk
        else:
            async for chunk in self._chunks_dedicated():
                yield chunk

    async def _chunks_shared(self) -> AsyncIterator[dict[str, Any]]:
        assert self._hub is not None and self._fetch is not None
        wakeup = self._hub.subscribe(self._job_id)
        try:
            last_seq = 0
            last_progress = asyncio.get_running_loop().time()
            while True:
                end = False
                rows = await self._fetch.fetch(
                    f"SELECT seq, payload FROM {EVENTS_TABLE} "
                    f"WHERE job_id = %s AND seq > %s ORDER BY seq",
                    (self._job_id, last_seq),
                )
                if rows:
                    last_progress = asyncio.get_running_loop().time()
                for seq, payload in rows:
                    last_seq = seq
                    envelope = json.loads(payload)
                    chunk = json.loads(envelope["c"])
                    ctype = chunk.get("type") if isinstance(chunk, dict) else None
                    if ctype == CHUNK_KIND_ERROR:
                        raise RemoteTurnError(str(chunk.get("error") or "turn failed"))
                    if ctype == CHUNK_KIND_END:
                        end = True
                        break
                    yield chunk
                if end:
                    return
                if (
                    self._turn_timeout is not None
                    and asyncio.get_running_loop().time() - last_progress > self._turn_timeout
                ):
                    raise RemoteTurnError(
                        f"no worker events for {self._turn_timeout:.0f}s "
                        f"(job {self._job_id} likely died or was never picked up)"
                    )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wakeup.wait(), timeout=self._poll_interval)
                wakeup.clear()
        finally:
            self._hub.unsubscribe(self._job_id, wakeup)

    async def _chunks_dedicated(self) -> AsyncIterator[dict[str, Any]]:
        listener = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
        fetcher = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
        try:
            await listener.execute(f"LISTEN {self._channel}")
            last_seq = 0
            last_progress = asyncio.get_running_loop().time()
            while True:
                end = False
                async with fetcher.cursor() as cur:
                    await cur.execute(
                        f"SELECT seq, payload FROM {EVENTS_TABLE} "
                        f"WHERE job_id = %s AND seq > %s ORDER BY seq",
                        (self._job_id, last_seq),
                    )
                    rows = await cur.fetchall()
                if rows:
                    last_progress = asyncio.get_running_loop().time()
                for seq, payload in rows:
                    last_seq = seq
                    envelope = json.loads(payload)
                    chunk = json.loads(envelope["c"])
                    ctype = chunk.get("type") if isinstance(chunk, dict) else None
                    if ctype == CHUNK_KIND_ERROR:
                        raise RemoteTurnError(str(chunk.get("error") or "turn failed"))
                    if ctype == CHUNK_KIND_END:
                        end = True
                        break
                    yield chunk
                if end:
                    return
                if (
                    self._turn_timeout is not None
                    and asyncio.get_running_loop().time() - last_progress > self._turn_timeout
                ):
                    raise RemoteTurnError(
                        f"no worker events for {self._turn_timeout:.0f}s "
                        f"(job {self._job_id} likely died or was never picked up)"
                    )
                await self._wait_notify(listener)
        finally:
            await listener.close()
            await fetcher.close()

    async def _wait_notify(self, listener: psycopg.AsyncConnection) -> None:
        """Block until a NOTIFY arrives or the fallback poll interval elapses."""

        async def _first() -> None:
            async for _notify in listener.notifies():
                return

        with contextlib.suppress(TimeoutError, StopAsyncIteration):
            await asyncio.wait_for(_first(), timeout=self._poll_interval)


def _turn_job_status_lookup(dsn: str) -> Callable[[str], Awaitable[str | None]]:
    """Bind :func:`turn_job_status` to a DSN (attach-time orphan guard)."""

    async def lookup(job_id: str) -> str | None:
        return await turn_job_status(dsn, job_id)

    return lookup


class _CancelNotifier:
    """Async, non-blocking cancel-notify sender (review P2-6).

    ``cancel_stream`` must stay a *sync* method (the ``AgentExecutor``
    protocol is sync), but a synchronous ``psycopg.connect`` inside the
    event loop stalls the whole api node for up to ``connect_timeout`` when
    PG is slow. Instead the sync method schedules the notify onto the
    running loop through one persistent, lazily-opened connection with
    reconnect. Outside a loop it degrades to the old synchronous path.
    """

    def __init__(
        self,
        dsn: str,
        *,
        connect: Callable[..., Any] | None = None,
        connect_timeout: int = 3,
    ) -> None:
        self._dsn = dsn
        self._connect = connect or psycopg.AsyncConnection.connect
        self._connect_timeout = connect_timeout
        self._conn: psycopg.AsyncConnection | None = None

    def submit(self, payload: str) -> None:
        """Schedule a cancel notify; fire-and-forget from a sync context."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (truly sync caller): blocking is acceptable here.
            self._submit_sync(payload)
            return
        asyncio.create_task(self._notify(payload), name="octop-turn-cancel-notify")

    def _submit_sync(self, payload: str) -> None:
        try:
            with psycopg.connect(
                self._dsn, autocommit=True, connect_timeout=self._connect_timeout
            ) as conn:
                conn.execute("SELECT pg_notify(%s, %s)", (CANCEL_CHANNEL, payload))
        except Exception:
            logger.exception("remote cancel notify failed")

    async def _ensure_conn(self) -> psycopg.AsyncConnection:
        if self._conn is not None and not self._conn.closed:
            return self._conn
        conn = await self._connect(self._dsn, autocommit=True)
        self._conn = conn
        return conn

    async def _notify(self, payload: str) -> None:
        try:
            conn = await self._ensure_conn()
            await conn.execute("SELECT pg_notify(%s, %s)", (CANCEL_CHANNEL, payload))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Best effort by design: drop the connection so the next submit
            # reconnects; cancel falls back to preemption/watchdog semantics.
            logger.exception("remote cancel notify failed")
            stale = self._conn
            self._conn = None
            if stale is not None:
                with contextlib.suppress(Exception):
                    await stale.close()

    async def aclose(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.close()


class RemoteExecutor:
    """Queue-backed ``AgentExecutor`` — runs turns on agent worker nodes.

    Drop-in replacement for :class:`~octop.infra.agents.executor.InProcessExecutor`
    at the composition root when ``execution_mode == "remote"``. The five
    consumer call sites (gateway processor / stream projection / cron delivery /
    teams) are unchanged since stage 1 constructor injection.
    """

    def __init__(
        self,
        dsn: str,
        *,
        queue: TurnQueue | None = None,
        channel: str = EVENTS_CHANNEL,
        poll_interval: float = POLL_INTERVAL,
        turn_timeout: float | None = 1800.0,
        reader_factory: Callable[[str], Any] | None = None,
        hub: NotifyHubLike | None = None,
        fetch_pool: FetchPoolLike | None = None,
        pool_size: int = 8,
        registry: ActiveJobRegistry | None = None,
        job_status: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None:
        self._dsn = dsn
        self._channel = channel
        self._poll_interval = poll_interval
        self._turn_timeout = turn_timeout
        self._queue: TurnQueue | None = queue
        self._queue_open = queue is not None
        self._open_lock = asyncio.Lock()
        self._reader_factory = reader_factory
        # Shared per-process notification fan-out + fetch pool (stage 3).
        # Owns them when not injected; readers fall back to dedicated
        # connections only when both are absent (tests).
        self._hub: NotifyHubLike | None = hub
        self._fetch: FetchPoolLike | None = fetch_pool
        self._pool_size = pool_size
        # Non-blocking cancel notify (review P2-6); tests may inject a fake.
        self._cancel_notifier = _CancelNotifier(dsn)
        # thread → active job mapping (plan D3); tests may inject a fake.
        self._registry = registry
        # D3 orphan guard: job-status lookup used by ``attach``. None means
        # "not built yet" — built lazily for the production default path;
        # readers injected via ``reader_factory`` (tests, standalone usage)
        # run without PG, so the guard stays off for them unless injected.
        self._job_status = job_status

    def _make_reader(self, job_id: str) -> Any:
        if self._reader_factory is not None:
            return self._reader_factory(job_id)
        return TurnStreamReader(
            self._dsn,
            job_id,
            channel=self._channel,
            poll_interval=self._poll_interval,
            turn_timeout=self._turn_timeout,
            hub=self._hub,
            fetch=self._fetch,
        )

    async def _ensure_open(self) -> TurnQueue:
        # Serialized: concurrent first-use (burst of streams on a cold node)
        # must create exactly one queue/hub/fetch-pool, not one per caller.
        async with self._open_lock:
            if not self._queue_open:
                queue = ProcrastinateTurnQueue(self._dsn)
                await queue.open()
                self._queue = queue
                self._queue_open = True
            # Shared hub/pool only for the production default reader path; tests
            # injecting a reader_factory run standalone without PG.
            if self._reader_factory is None:
                if self._hub is None:
                    hub = NotifyHub(self._dsn, channel=self._channel)
                    await hub.open()
                    self._hub = hub
                if self._fetch is None:
                    self._fetch = TurnFetchPool(self._dsn, size=self._pool_size)
            assert self._queue is not None
            return self._queue

    @staticmethod
    def _thread_lock(agent_id: str, thread_id: str) -> str:
        return f"octop-turn:{agent_id}:{thread_id}"

    async def _defer_turn(
        self,
        *,
        kind: str,
        agent_id: str,
        thread_id: str,
        extra: dict[str, Any],
    ) -> str:
        queue = await self._ensure_open()
        job_id = new_ulid()
        payload: dict[str, Any] = {
            "kind": kind,
            "job_id": job_id,
            "agent_id": agent_id,
            "thread_id": thread_id,
            **extra,
        }
        await queue.defer(payload, lock=self._thread_lock(agent_id, thread_id))
        return job_id

    async def stream(self, agent_id: str, request: dict[str, Any]) -> AsyncIterator[Any]:
        """Stream harness chunks for one remote turn."""
        thread_id = str(request.get("thread_id") or "")
        job_id = await self._defer_turn(
            kind="stream", agent_id=agent_id, thread_id=thread_id, extra={"request": request}
        )
        reader = self._make_reader(job_id)
        async for chunk in reader.chunks():
            yield chunk

    async def call(self, agent_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Non-streaming one-shot remote invocation."""
        thread_id = str(request.get("thread_id") or "")
        job_id = await self._defer_turn(
            kind="call", agent_id=agent_id, thread_id=thread_id, extra={"request": request}
        )
        reader = self._make_reader(job_id)
        async for chunk in reader.chunks():
            if chunk.get("type") == CHUNK_KIND_RESULT:
                result = chunk.get("result")
                return result if isinstance(result, dict) else {"result": result}
        raise RemoteTurnError("turn ended without a result")

    async def resume_hitl(
        self,
        agent_id: str,
        thread_id: str,
        decisions: list[dict[str, Any]],
    ) -> AsyncIterator[Any]:
        """Resume a paused HITL interrupt on a worker node."""
        job_id = await self._defer_turn(
            kind="resume",
            agent_id=agent_id,
            thread_id=thread_id,
            extra={"decisions": decisions},
        )
        reader = self._make_reader(job_id)
        async for chunk in reader.chunks():
            yield chunk

    async def active_job_id(self, thread_id: str) -> str | None:
        """Discover the job currently streaming ``thread_id``'s turn (D3).

        None means the thread has no in-flight remote turn (or this node's
        registry is unavailable — continuity degrades, nothing breaks).
        """
        if self._registry is None:
            self._registry = ActiveJobRegistry(self._dsn)
        try:
            return await self._registry.active_job_id(thread_id)
        except Exception:
            logger.exception("active-job lookup failed thread=%s", thread_id)
            return None

    async def attach(self, job_id: str) -> AsyncIterator[Any]:
        """Re-stream an in-flight turn from seq 0 (crash / rolling-upgrade).

        A fresh :class:`TurnStreamReader` replays the job's persisted events
        from the beginning and then follows live through the shared
        ``NotifyHub`` — which listens cross-node — so any api node can take
        over a stream whose original reader died with its process.

        D3 orphan guard: a mapping row can outlive its job (worker SIGKILLed
        before the mapping was cleared, retries exhausted, job cancelled).
        Before attaching, the job's procrastinate status is consulted and
        terminal jobs raise :class:`RemoteTurnError` immediately, so callers
        report "turn is no longer running" instead of waiting the reader
        watchdog (1800 s by default). A lookup failure degrades to the
        watchdog path rather than blocking attach entirely.
        """
        lookup = self._job_status
        if lookup is None and self._reader_factory is None:
            dsn = self._dsn
            lookup = self._job_status = _turn_job_status_lookup(dsn)
        if lookup is not None:
            try:
                status = await lookup(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("turn job status lookup failed job=%s", job_id)
            else:
                if status is None or status in ORPHAN_JOB_STATUSES:
                    raise RemoteTurnError(
                        f"turn job {job_id} is no longer running "
                        f"(status={status or 'gone'}); not attaching"
                    )
        reader = self._make_reader(job_id)
        async for chunk in reader.chunks():
            yield chunk

    def cancel_stream(self, agent_id: str, thread_id: str) -> None:
        """Notify worker nodes to stop the active stream (best effort).

        Non-blocking: schedules an async pg_notify through the shared
        notifier connection instead of a synchronous connect (which would
        stall the api event loop when PG is slow).
        """
        payload = json.dumps({"agent_id": agent_id, "thread_id": thread_id})
        self._cancel_notifier.submit(payload)

    async def aclose(self) -> None:
        if self._queue is not None:
            await self._queue.aclose()
            self._queue_open = False
        if self._hub is not None:
            aclose = getattr(self._hub, "aclose", None)
            if aclose is not None:
                await aclose()
            self._hub = None
        if self._fetch is not None:
            aclose = getattr(self._fetch, "aclose", None)
            if aclose is not None:
                await aclose()
            self._fetch = None
        await self._cancel_notifier.aclose()


# ---------------------------------------------------------------------------
# worker side: publisher + task + runner
# ---------------------------------------------------------------------------


class TurnEventPublisher:
    """Worker-side chunk emitter: batched INSERT + pg_notify (PoC 2 pattern)."""

    def __init__(
        self,
        dsn: str,
        job_id: str,
        *,
        channel: str = EVENTS_CHANNEL,
        flush_seconds: float = FLUSH_SECONDS,
    ) -> None:
        self._dsn = dsn
        self._job_id = job_id
        self._channel = channel
        self._flush_seconds = flush_seconds
        self._conn: psycopg.AsyncConnection | None = None
        self._buffer: list[str] = []
        self._last_flush = 0.0

    async def open(self) -> None:
        self._conn = await psycopg.AsyncConnection.connect(self._dsn)
        self._last_flush = asyncio.get_running_loop().time()

    async def publish(self, chunk: Any) -> None:
        assert self._conn is not None, "publisher not opened"
        self._buffer.append(encode_chunk(chunk))
        now = asyncio.get_running_loop().time()
        if now - self._last_flush >= self._flush_seconds:
            await self._flush()

    async def _flush(self) -> None:
        assert self._conn is not None
        if not self._buffer:
            return
        rows = [(self._job_id, json.dumps({"c": text, "ts": time.time()})) for text in self._buffer]
        async with self._conn.transaction():
            async with self._conn.cursor() as cur:
                await cur.executemany(
                    f"INSERT INTO {EVENTS_TABLE} (job_id, payload) VALUES (%s, %s)", rows
                )
            await self._conn.execute("SELECT pg_notify(%s, %s)", (self._channel, self._job_id))
        self._buffer = []
        self._last_flush = asyncio.get_running_loop().time()

    async def finish(self) -> None:
        """Emit the end sentinel so api-side readers terminate cleanly."""
        self._buffer.append(encode_chunk({"type": CHUNK_KIND_END}))
        self._last_flush = 0.0
        await self._flush()

    async def fail(self, message: str) -> None:
        """Emit an error sentinel; api-side readers raise RemoteTurnError.

        When ``open()`` never succeeded (PG unreachable at turn start, D13),
        make a best-effort one-shot connection so the api reader still
        receives the sentinel instead of waiting the full 1800s watchdog.
        If that connection also fails the reader falls back to its watchdog
        — nothing more can be done when PG is truly unreachable.
        """
        chunk = encode_chunk({"type": CHUNK_KIND_ERROR, "error": message})
        if self._conn is not None:
            self._buffer.append(chunk)
            self._last_flush = 0.0
            await self._flush()
            return
        try:
            conn = await psycopg.AsyncConnection.connect(self._dsn)
            try:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            f"INSERT INTO {EVENTS_TABLE} (job_id, payload) VALUES (%s, %s)",
                            (self._job_id, json.dumps({"c": chunk, "ts": time.time()})),
                        )
                    await conn.execute("SELECT pg_notify(%s, %s)", (self._channel, self._job_id))
            finally:
                await conn.close()
        except Exception:
            logger.exception(
                "best-effort error sentinel failed job=%s (reader will wait watchdog)",
                self._job_id,
            )

    async def aclose(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


PublisherFactory = Callable[[str], Any]


def _default_publisher_factory(dsn: str, channel: str, flush_seconds: float) -> PublisherFactory:
    def factory(job_id: str) -> TurnEventPublisher:
        return TurnEventPublisher(dsn, job_id, channel=channel, flush_seconds=flush_seconds)

    return factory


async def _execute_turn(
    payload: dict[str, Any],
    *,
    local_executor: AgentExecutor,
    publisher_factory: PublisherFactory,
    channel: str,
    registry: ActiveJobRegistry | None = None,
    job_runtime_seconds: float | None = TURN_JOB_MAX_RUNTIME_SECONDS,
) -> None:
    """Run one queued turn on this node, streaming chunks back."""
    kind = str(payload.get("kind") or "stream")
    agent_id = str(payload.get("agent_id") or "")
    thread_id = str(payload.get("thread_id") or "")
    job_id = str(payload.get("job_id") or "")
    publisher = publisher_factory(job_id)
    logger.debug("remote turn start kind=%s agent=%s job=%s", kind, agent_id, job_id)
    # D3: publish the thread → job mapping ASAP so api nodes can discover and
    # re-attach to this turn even if this worker (or the submitting api node)
    # dies mid-run. Best-effort: discovery loss only costs continuity.
    if registry is not None and thread_id:
        try:
            await registry.claim(agent_id=agent_id, thread_id=thread_id, job_id=job_id, kind=kind)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("active-job claim failed job=%s thread=%s", job_id, thread_id)

    async def _run_turn() -> None:
        await publisher.open()
        if kind == "call":
            result = await local_executor.call(agent_id, dict(payload.get("request") or {}))
            await publisher.publish({"type": CHUNK_KIND_RESULT, "result": result})
        else:
            if kind == "resume":
                stream = local_executor.resume_hitl(
                    agent_id,
                    str(payload.get("thread_id") or ""),
                    list(payload.get("decisions") or []),
                )
            else:
                stream = local_executor.stream(agent_id, dict(payload.get("request") or {}))
            async for chunk in stream:
                await publisher.publish(chunk)
        await publisher.finish()
        logger.debug("remote turn done kind=%s agent=%s job=%s", kind, agent_id, job_id)

    try:
        # D15 (cluster review S3): a hung turn (LLM upstream stall, tool that
        # never returns, stuck DB call) holds the per-thread queue lock
        # forever — the stalled-retry loop deliberately does not re-dispatch
        # jobs whose worker is alive, and the api-side watchdog cannot free
        # the lock. Abort the run at the runtime bound instead: the timeout
        # converts to a RemoteTurnError below, the failure path publishes the
        # error sentinel, and the job fails — releasing the thread lock.
        # No double-run: the job is failed, not retried.
        if job_runtime_seconds is None:
            await _run_turn()
        else:
            try:
                await asyncio.wait_for(_run_turn(), timeout=job_runtime_seconds)
            except TimeoutError:
                raise RemoteTurnError(
                    f"turn exceeded {job_runtime_seconds:.0f}s runtime; worker aborted it"
                ) from None
    except BaseException as exc:
        # CancelledError (worker shutdown/abort) and failures both must
        # terminate the api-side reader — never leave it waiting silently.
        # ``open()`` runs inside this try (D13): an open failure still routes
        # through ``publisher.fail`` (which best-effort opens a fresh
        # connection) instead of leaving the reader to the 1800s watchdog.
        if not isinstance(exc, asyncio.CancelledError):
            logger.exception("remote turn failed kind=%s agent=%s job=%s", kind, agent_id, job_id)
        try:
            if isinstance(exc, asyncio.CancelledError):
                await publisher.fail("turn aborted (worker stopped)")
            else:
                await publisher.fail(f"{type(exc).__name__}: {exc}")
        except Exception:
            logger.exception("failed to publish turn error sentinel job=%s", job_id)
        raise
    finally:
        if registry is not None and thread_id:
            try:
                await registry.clear(thread_id=thread_id, job_id=job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("active-job clear failed job=%s thread=%s", job_id, thread_id)
        await publisher.aclose()


def build_worker_app(
    dsn: str,
    local_executor: AgentExecutor,
    *,
    queue_name: str = QUEUE_NAME,
    channel: str = EVENTS_CHANNEL,
    flush_seconds: float = FLUSH_SECONDS,
    publisher_factory: PublisherFactory | None = None,
    connector: Any | None = None,
    registry: ActiveJobRegistry | None = None,
    job_runtime_seconds: float | None = TURN_JOB_MAX_RUNTIME_SECONDS,
) -> Any:
    """Create a Procrastinate app whose single task executes remote turns."""
    import procrastinate  # noqa: PLC0415

    factory = publisher_factory or _default_publisher_factory(dsn, channel, flush_seconds)
    connector_kwargs: dict[str, Any] = (
        {"connector": connector}
        if connector is not None
        else {"connector": procrastinate.PsycopgConnector(conninfo=dsn)}
    )
    app = procrastinate.App(**connector_kwargs)
    job_registry = registry or ActiveJobRegistry(dsn)

    @app.task(queue=queue_name, name=TASK_NAME, pass_context=True)
    async def execute_turn(context: Any, *, payload: dict[str, Any]) -> None:
        await _execute_turn(
            payload,
            local_executor=local_executor,
            publisher_factory=factory,
            channel=channel,
            registry=job_registry,
            job_runtime_seconds=job_runtime_seconds,
        )

    return app


class AgentWorker:
    """Agent-role process: consumes turn jobs and executes them locally."""

    def __init__(
        self,
        dsn: str,
        local_executor: AgentExecutor,
        *,
        queue_name: str = QUEUE_NAME,
        channel: str = EVENTS_CHANNEL,
        concurrency: int = 4,
        worker_name: str | None = None,
        publisher_factory: PublisherFactory | None = None,
        app: Any | None = None,
        warmup: Callable[[], Awaitable[None]] | None = None,
        event_retention_seconds: float = EVENT_RETENTION_SECONDS,
        prune_interval_seconds: float = PRUNE_INTERVAL_SECONDS,
        stalled_retry_interval: float = STALLED_RETRY_INTERVAL_SECONDS,
        cancel_reconnect_delay: float = 1.0,
        job_runtime_seconds: float | None = TURN_JOB_MAX_RUNTIME_SECONDS,
    ) -> None:
        self._dsn = dsn
        self._local_executor = local_executor
        self._queue_name = queue_name
        self._channel = channel
        self._concurrency = concurrency
        self._worker_name = worker_name
        self._publisher_factory = publisher_factory
        self._app = app
        self._warmup = warmup
        self._event_retention = event_retention_seconds
        self._prune_interval = prune_interval_seconds
        self._stalled_interval = stalled_retry_interval
        self._cancel_reconnect_delay = cancel_reconnect_delay
        # D15: runtime bound for a single turn (frees the thread lock on hang).
        self._job_runtime_seconds = job_runtime_seconds
        self._cancel_task: asyncio.Task[None] | None = None
        self._prune_task: asyncio.Task[None] | None = None
        self._stalled_task: asyncio.Task[None] | None = None

    async def run_forever(self) -> None:
        """Run until SIGTERM/SIGINT (procrastinate installs default handlers)."""
        if self._warmup is not None:
            # Must complete BEFORE any job is claimed: lazy per-turn
            # initialisation that runs DDL (e.g. langgraph's
            # CREATE INDEX CONCURRENTLY) deadlocks against the
            # "idle in transaction" fetch connection procrastinate keeps
            # open while a job runs.
            await self._warmup()
        await ensure_turn_schema(self._dsn)
        await ensure_queue_schema(self._dsn)
        app = self._app or build_worker_app(
            self._dsn,
            self._local_executor,
            queue_name=self._queue_name,
            channel=self._channel,
            publisher_factory=self._publisher_factory,
            job_runtime_seconds=self._job_runtime_seconds,
        )
        self._app = app
        self._cancel_task = asyncio.create_task(
            self._cancel_loop(), name="octop-turn-cancel-listener"
        )
        self._prune_task = asyncio.create_task(self._prune_loop(), name="octop-turn-events-pruner")
        self._stalled_task = asyncio.create_task(
            self._stalled_loop(), name="octop-turn-stalled-retry"
        )
        try:
            async with app.open_async():
                worker_kwargs: dict[str, Any] = {
                    "queues": [self._queue_name],
                    "concurrency": self._concurrency,
                    "wait": True,
                }
                if self._worker_name:
                    worker_kwargs["name"] = self._worker_name
                await app.run_worker_async(**worker_kwargs)
        finally:
            await self._stop_cancel_loop()
            await self._stop_prune_loop()
            await self._stop_stalled_loop()

    async def _stop_prune_loop(self) -> None:
        task = self._prune_task
        self._prune_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - shutdown path
                logger.debug("events pruner stopped", exc_info=True)

    async def _prune_loop(self) -> None:
        """Bound octop_turn_events growth (stage 3 convergence)."""
        while True:
            await asyncio.sleep(self._prune_interval)
            try:
                deleted = await prune_turn_events(
                    self._dsn, retention_seconds=self._event_retention
                )
                if deleted:
                    logger.info("pruned %d expired turn events", deleted)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("turn events pruning failed")

    async def _stop_cancel_loop(self) -> None:
        task = self._cancel_task
        self._cancel_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - shutdown path
                logger.debug("cancel listener stopped", exc_info=True)

    async def _cancel_loop(self) -> None:
        """LISTEN for cancel notifications; reconnects after connection loss.

        Review P2-5: a single-shot listener died silently on any PG hiccup,
        permanently disabling cancel on that worker. Mirrors the reconnect
        pattern of :meth:`NotifyHub._dispatch_loop`.
        """
        while True:
            try:
                listener = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("cancel listener connect failed; retrying")
                await asyncio.sleep(self._cancel_reconnect_delay)
                continue
            try:
                await listener.execute(f"LISTEN {CANCEL_CHANNEL}")
                async for notify in listener.notifies():
                    try:
                        data = json.loads(notify.payload or "{}")
                        self._local_executor.cancel_stream(
                            str(data.get("agent_id") or ""), str(data.get("thread_id") or "")
                        )
                    except Exception:
                        logger.exception("cancel notify handling failed")
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await listener.close()
                raise
            except Exception:
                logger.exception("cancel listener lost connection; reconnecting")
            finally:
                with contextlib.suppress(Exception):
                    await listener.close()
            await asyncio.sleep(self._cancel_reconnect_delay)

    async def _stop_stalled_loop(self) -> None:
        task = self._stalled_task
        self._stalled_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - shutdown path
                logger.debug("stalled-job retry loop stopped", exc_info=True)

    async def _stalled_loop(self) -> None:
        """Self-heal zombie ``doing`` jobs from *dead* workers.

        Review P0-1: procrastinate only frees a job's ``lock`` when the job
        leaves ``doing``. A worker that dies (OOM / SIGKILL / node loss)
        would keep the per-thread lock forever — every later message of that
        thread queues but never runs. This loop retries stalled jobs whose
        worker is genuinely gone (heartbeat stale / row pruned) back to
        ``todo``, freeing the lock.

        Deliberately NOT covered here: a worker that is *alive* with a fresh
        heartbeat but whose run hangs (S3). Re-dispatching those would double-
        run the turn; they are instead aborted by the per-turn runtime bound
        (``TURN_JOB_MAX_RUNTIME_SECONDS`` in ``_execute_turn``, review D15),
        which fails the job and releases the lock without a retry.
        """
        while True:
            await asyncio.sleep(self._stalled_interval)
            try:
                app = self._app
                job_manager = getattr(app, "job_manager", None)
                if job_manager is None:
                    continue
                retried = await self.retry_stalled_jobs_once(job_manager)
                if retried:
                    logger.warning(
                        "retried %d stalled turn job(s); locks released for re-execution",
                        retried,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("stalled turn job retry scan failed")

    async def retry_stalled_jobs_once(self, job_manager: Any) -> int:
        """Retry stalled turn jobs once. Returns the number retried.

        Only the heartbeat detector triggers retry: a job whose worker
        stopped heartbeating (crashed / OOM / SIGKILL / node loss / event
        loop frozen) can never finish, so its per-thread ``lock`` must be
        freed for re-execution.

        The runtime detector (``nb_seconds``) is queried for monitoring only
        and **does not retry**. A ``doing`` job older than
        ``TURN_JOB_MAX_RUNTIME_SECONDS`` whose worker is still heartbeating
        is a legitimately long turn — retrying it would start a second
        worker on the same thread; ``procrastinate`` would then ``finish``
        the job once (releasing the ``lock`` early) and the two runs would
        race on the same LangGraph checkpoint, **breaking the same-thread
        serialization guarantee** (cluster-assessment §3.A). Workers that
        are actually dead/frozen already fail the heartbeat check above;
        ``worker_id IS NULL`` orphans (worker row pruned) are covered by
        the heartbeat query too, so the runtime path adds no valid retry.
        """
        retried = 0
        seen: set[int] = set()
        heartbeat_stalled = await job_manager.get_stalled_jobs(
            queue=self._queue_name,
            task_name=TASK_NAME,
            seconds_since_heartbeat=STALLED_HEARTBEAT_SECONDS,
        )
        for job in heartbeat_stalled:
            jid = int(job.id)
            seen.add(jid)
            # D14: per-job tolerance — a concurrent scan or a job that was
            # finished between scan and retry raises inside retry_job; one
            # failure must not abort the rest of this scan's retries.
            try:
                await job_manager.retry_job(job)
            except Exception:
                logger.exception("retry_job failed for stalled job=%s", jid)
                continue
            retried += 1
        # Runtime over-long detector: monitor only, never retry. A live worker
        # running a long turn must not be double-dispatched (D1).
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            long_running = await job_manager.get_stalled_jobs(
                nb_seconds=int(TURN_JOB_MAX_RUNTIME_SECONDS),
                queue=self._queue_name,
                task_name=TASK_NAME,
            )
        for job in long_running:
            jid = int(job.id)
            if jid in seen:
                continue
            logger.debug(
                "long turn job=%s running >%ss with a live worker; not retrying "
                "(retry would double-run and break same-thread serialization)",
                jid,
                int(TURN_JOB_MAX_RUNTIME_SECONDS),
            )
        return retried
