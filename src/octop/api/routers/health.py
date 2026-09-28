"""Liveness / readiness probes (no auth)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from octop.api.deps import get_server
from octop.infra.db import migrate as db_migrate

router = APIRouter()


@router.get("", summary="Health check")
async def health(server: Any = Depends(get_server)) -> dict[str, Any]:
    """Liveness probe: database reachability, user count, and loaded agents. No auth required."""
    bound = server.database_bound
    users = server.user_manager.count() if server.user_manager is not None else 0
    agents = (
        len(server.app_runtime.agent_registry.list_rows()) if server.app_runtime is not None else 0
    )
    return {
        "ok": True,
        "started_at": server._started_at,
        "db": bound,
        "users_loaded": users,
        "agents_running": agents,
    }


@router.get("/ready", summary="Readiness probe")
async def ready(server: Any = Depends(get_server)) -> dict[str, Any]:
    """Readiness probe (cluster plan D9): able to serve traffic.

    Returns 503 unless the database is reachable AND migrated to this
    build's schema version — lets the LB / orchestrator distinguish
    "alive" from "able to serve" (e.g. mid-migration, or a node whose DB
    connection is down).
    """
    if server.services is None:
        raise HTTPException(status_code=503, detail="server not initialized")
    db = server.services.db
    try:
        applied = db_migrate.current_schema_version(db)
        expected = db_migrate.latest_schema_version(db.dialect)
    except Exception:
        raise HTTPException(status_code=503, detail="database unreachable") from None
    if applied < expected:
        raise HTTPException(
            status_code=503,
            detail=f"schema version {applied} older than expected {expected}",
        )
    return {"ok": True, "schema_version": applied, "expected_version": expected}
