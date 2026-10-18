"""Internal MCP endpoint must see connector rows despite the JWT-exempt path.

The route is machine-to-machine: no tenant scope is bound by middleware, and
migration 029 dropped the ``org_id IS NULL`` RLS branch, so an unscoped lookup
fails closed for tenant AND platform rows. Handlers wrap themselves in a
bounded ``scoped_request(None, bypass=True)``; per-instance token checks stay
the only authorization surface.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from octop.infra.connectors.service import ConnectorService
from octop.infra.users.tenancy_context import current_scope, scoped_request
from tests.support.app import octop_client

TOKEN = "local-gateway-token"


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, object]]:
    async with octop_client(tmp_path) as (c, srv):
        yield c, srv


def _seed_instance(srv: object, instance_id: str, *, org_id: str | None) -> None:
    repos = srv.services.repos  # type: ignore[attr-defined]
    owner = repos.user_repo.create(username=f"owner-{instance_id}", role="user")
    if org_id is not None:
        repos.org_repo.create_org(org_id=org_id, name="Chery FS")
    repos.connector_repo.create(
        instance_id=instance_id,
        user_id=owner,
        kind="qcc",
        display_name="Qichacha",
        mcp_server_name=f"qcc-{instance_id}",
        org_id=org_id,
    )
    ConnectorService(
        repo=repos.connector_repo,
        secret_repo=srv.services.secret_repo,  # type: ignore[attr-defined]
        settings_repo=srv.services.settings_repo,  # type: ignore[attr-defined]
        config=srv.services.config,  # type: ignore[attr-defined]
    ).encrypt_and_store(
        instance_id=instance_id,
        payload={"access_token": "upstream", "internal_token": TOKEN},
    )


@pytest.mark.asyncio
async def test_platform_and_tenant_rows_visible_and_token_gated(
    client: tuple[httpx.AsyncClient, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, srv = client
    _seed_instance(srv, "inst-platform", org_id=None)
    _seed_instance(srv, "inst-tenant", org_id="cheryfs")

    calls: list[tuple] = []
    real = scoped_request

    def spy(org_id: str | None, *, bypass: bool = False):
        calls.append((org_id, bypass))
        return real(org_id, bypass=bypass)

    monkeypatch.setattr("octop.api.routers.internal_mcp.scoped_request", spy)

    body = {"jsonrpc": "2.0", "id": 1, "method": "notifications/initialized"}

    # Correct token: both platform-NULL and tenant rows reachable → 202.
    for instance_id in ("inst-platform", "inst-tenant"):
        r = await http.post(
            f"/api/internal/mcp/qcc/{instance_id}",
            params={"token": TOKEN},
            json=body,
        )
        assert r.status_code == 202, r.text

    # Wrong token on an existing row: row WAS found, auth failed → 401.
    r = await http.post(
        "/api/internal/mcp/qcc/inst-platform",
        params={"token": "nope"},
        json=body,
    )
    assert r.status_code == 401

    # Unknown instance: row missing → 404.
    r = await http.post(
        "/api/internal/mcp/qcc/does-not-exist",
        params={"token": TOKEN},
        json=body,
    )
    assert r.status_code == 404

    # Every handler call bound a bounded platform bypass.
    assert calls and all(org is None and bypass for org, bypass in calls)

    # Scope must not leak past the request.
    assert current_scope() is None


@pytest.mark.asyncio
async def test_delete_session_uses_bypass_and_token(
    client: tuple[httpx.AsyncClient, object],
) -> None:
    http, srv = client
    _seed_instance(srv, "inst-del", org_id=None)

    r = await http.delete("/api/internal/mcp/qcc/inst-del", params={"token": TOKEN})
    assert r.status_code == 204

    r = await http.delete("/api/internal/mcp/qcc/inst-del", params={"token": "bad"})
    assert r.status_code == 401

    r = await http.delete("/api/internal/mcp/qcc/ghost", params={"token": TOKEN})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_sse_requires_session_header(
    client: tuple[httpx.AsyncClient, object],
) -> None:
    http, srv = client
    _seed_instance(srv, "inst-get", org_id="cheryfs")

    # No session header → fast 405 JSON (not 404/401: row visible, token OK).
    r = await http.get("/api/internal/mcp/qcc/inst-get", params={"token": TOKEN})
    assert r.status_code == 405

    # With a session header the handler enters the infinite SSE keepalive
    # stream; a client request never "completes". Timing out proves we
    # reached the streaming branch (a 404/401/405 would return immediately).
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            http.get(
                "/api/internal/mcp/qcc/inst-get",
                params={"token": TOKEN},
                headers={"mcp-session-id": "s1"},
            ),
            timeout=3,
        )

    r = await http.get("/api/internal/mcp/qcc/inst-get", params={"token": "bad"})
    assert r.status_code == 401

    r = await http.get("/api/internal/mcp/qcc/ghost", params={"token": TOKEN})
    assert r.status_code == 404
