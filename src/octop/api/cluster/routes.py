"""Cluster-only chat routes: attach to an in-flight remote turn (SSE, D3).

Mounted conditionally in ``api/app.py`` only when ``execution_mode ==
"remote"``; the community edition never imports this module (pluggability
contract §8).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from octop.api.deps import current_user, get_server
from octop.api.routers.chat.history import _require_thread
from octop.api.routers.chat.sse import format_sse
from octop.i18n.domains.stream import format_stream_error
from octop.infra.cluster.remote import RemoteTurnError
from octop.infra.utils.locale import resolve_request_locale

router = APIRouter()
logger = logging.getLogger(__name__)

_TURN_ATTACH_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@router.get(
    "/agents/{agent_id}/threads/{thread_id}/turn/stream",
    summary="Attach to an in-flight remote turn (SSE)",
    response_model=None,
)
async def attach_turn_stream(
    agent_id: str,
    thread_id: str,
    request: Request,
    as_user: int | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> StreamingResponse:
    """Re-stream the thread's in-flight remote turn (cluster plan D3).

    After an api crash or a rolling upgrade, a reconnecting client (or the
    new node itself) attaches here: the worker-persisted thread → job mapping
    locates the turn, the turn-events table replays it from seq 0, and the
    shared NotifyHub wakes the reader cross-node for the live tail.
    """
    _require_thread(server, agent_id, thread_id, user, as_user)
    runtime = server.app_runtime
    executor = getattr(runtime, "remote_executor", None) if runtime is not None else None
    if executor is None:
        raise HTTPException(status_code=404, detail="remote execution not enabled")

    async def gen() -> AsyncIterator[str]:
        job_id = await executor.active_job_id(thread_id)
        if job_id is None:
            yield format_sse("chunk", {"type": "error", "message": "no active turn for thread"})
            return
        try:
            async for chunk in executor.attach(job_id):
                yield format_sse("chunk", chunk)
            yield format_sse("chunk", {"type": "done"})
        except RemoteTurnError as exc:
            yield format_sse("chunk", {"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - SSE boundary
            logger.exception("turn attach stream failed thread=%s", thread_id)
            yield format_sse(
                "chunk",
                {
                    "type": "error",
                    "message": format_stream_error(exc, resolve_request_locale(request)),
                },
            )

    return StreamingResponse(
        gen(), media_type="text/event-stream", headers=_TURN_ATTACH_SSE_HEADERS
    )
