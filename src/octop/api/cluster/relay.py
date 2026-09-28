"""Cross-node turn relay for WebSocket subscribers (cluster plan D3).

Moved out of ``api/routers/chat/ws.py`` so the community edition carries no
cluster code: ``ws.py`` reaches :func:`maybe_relay_remote_turn` through a
lazy import that only executes when a remote executor is wired (remote mode
implies the cluster module is present).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Cluster plan D3: at most one cross-node relay per thread per process.
_remote_relay_tasks: dict[str, asyncio.Task[None]] = {}


async def maybe_relay_remote_turn(hub: Any, thread_id: str, executor: Any) -> None:
    """Continue an in-flight remote turn cross-node over WebSocket (D3).

    The hub only tracks turns started on this node. When a dashboard
    reconnects after an api crash / rolling upgrade / to another replica
    behind the LB, the local hub reports idle even though the worker is
    still executing. Consult the worker-persisted thread→job mapping and
    relay the job's chunk stream into the hub so subscribers keep
    rendering. No-op when a relay for the thread is already running.
    """
    existing = _remote_relay_tasks.get(thread_id)
    if existing is not None and not existing.done():
        return
    try:
        job_id = await executor.active_job_id(thread_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("active-job lookup failed thread=%s", thread_id)
        return
    if job_id is None:
        return
    hub.mark_turn_active(thread_id)

    async def _relay() -> None:
        try:
            async for chunk in executor.attach(job_id):
                await hub.push_to_thread(thread_id, chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("remote turn relay failed thread=%s job=%s", thread_id, job_id)
            with contextlib.suppress(Exception):
                await hub.push_to_thread(
                    thread_id,
                    {"type": "error", "message": f"stream interrupted: {exc}"},
                )
        finally:
            _remote_relay_tasks.pop(thread_id, None)
            try:
                await hub.push_to_thread(thread_id, {"type": "done"})
            finally:
                hub.mark_turn_idle(thread_id)

    _remote_relay_tasks[thread_id] = asyncio.create_task(_relay())
