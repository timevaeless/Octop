"""Unit tests for cluster plan D9: readiness probe.

Covers, against a fake database pool:
- ``/api/health/ready`` returns ok when the DB is reachable and the applied
  schema version matches this build's latest migration;
- 503 semantics: server unbound / database unreachable / schema too old —
  the LB and orchestrators must be able to distinguish "alive" from
  "able to serve";
- the new endpoint inherits the ``/api/health/`` JWT exemption.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import HTTPException

from octop.api.deps import is_jwt_exempt_path
from octop.api.routers.health import ready
from octop.infra.db import migrate as db_migrate


class _Result:
    def __init__(self, version: int) -> None:
        self._version = version

    def fetchone(self) -> dict[str, int]:
        return {"version": self._version}


class _Conn:
    def __init__(self, version: int, error: Exception | None = None) -> None:
        self._version = version
        self._error = error

    def execute(self, sql: str) -> _Result:
        if self._error is not None:
            raise self._error
        return _Result(self._version)

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class _Pool:
    dialect = "postgresql"

    def __init__(self, version: int, error: Exception | None = None) -> None:
        self._version = version
        self._error = error

    def connect(self) -> _Conn:
        return _Conn(self._version, self._error)


class _Services:
    def __init__(self, db: Any) -> None:
        self.db = db


class _Server:
    def __init__(self, db: Any) -> None:
        self.services = _Services(db)


def test_ready_ok_when_schema_current() -> None:
    latest = db_migrate.latest_schema_version("postgresql")
    assert latest > 0
    result = asyncio.run(ready(_Server(_Pool(latest))))
    assert result == {
        "ok": True,
        "schema_version": latest,
        "expected_version": latest,
    }


def test_ready_503_when_database_unreachable() -> None:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ready(_Server(_Pool(0, error=ConnectionError("pg down")))))
    assert exc.value.status_code == 503
    assert "unreachable" in exc.value.detail


def test_ready_503_when_schema_older_than_build() -> None:
    latest = db_migrate.latest_schema_version("postgresql")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ready(_Server(_Pool(latest - 1))))
    assert exc.value.status_code == 503
    assert "older than expected" in exc.value.detail


def test_ready_503_when_server_not_initialized() -> None:
    server = _Server(None)
    server.services = None
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ready(server))
    assert exc.value.status_code == 503


def test_readiness_path_is_jwt_exempt() -> None:
    assert is_jwt_exempt_path("/api/health/ready")
    assert not is_jwt_exempt_path("/api/agents")
