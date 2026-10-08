"""Unit tests for the websocket tenant-scope middleware (T2-4).

Live finding being locked down: the HTTP middleware never sees ASGI
``websocket`` scopes, so WS handlers ran with no tenant scope — the pool
turned that into a fail-closed empty ``app.org_id`` and every RLS query in
a WS handler returned zero rows (dashboard chat died with 403s while the
same HTTP endpoints kept working).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import jwt
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket

from octop.api.middleware.ws_tenant_scope import install
from octop.infra.users.tenancy_context import current_scope

SECRET = "unit-test-secret"


def _sign(sub: str = "7", ver: int = 0) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": sub,
            "uname": "admin_2",
            "role": "user",
            "ver": ver,
            "iat": now,
            "exp": now + 3600,
        },
        SECRET,
        algorithm="HS256",
    )


def _fake_server() -> SimpleNamespace:
    row = SimpleNamespace(id=7, username="admin_2", disabled=0, token_epoch=0)
    user = SimpleNamespace(
        id=7,
        username="admin_2",
        is_admin=False,
        org_id="cheryfs",
        token_epoch=0,
    )
    services = SimpleNamespace(
        secret_repo=SimpleNamespace(get=lambda key: SECRET),
        user_repo=SimpleNamespace(get=lambda user_id: row),
    )
    return SimpleNamespace(
        services=services,
        user_manager=SimpleNamespace(get_by_id=lambda user_id: user),
    )


def _build_app() -> Starlette:
    async def http_home(request):
        scope = current_scope()
        return JSONResponse({"has_scope": scope is not None})

    async def ws_capture(websocket: WebSocket) -> None:
        scope = current_scope()
        await websocket.accept()
        await websocket.send_json(
            {
                "has_scope": scope is not None,
                "org_id": getattr(scope, "org_id", None),
                "bypass": getattr(scope, "bypass", None),
            }
        )
        await websocket.close()

    app = Starlette(
        routes=[
            Route("/http-home", http_home),
            WebSocketRoute("/ws", ws_capture),
        ]
    )
    app.state.octop_server = _fake_server()
    install(app)
    return app


def _ws_client(app: Starlette) -> TestClient:
    return TestClient(app)


def test_ws_connection_binds_tenant_scope_from_token() -> None:
    app = _build_app()
    token = _sign()
    with _ws_client(app) as client, client.websocket_connect(f"/ws?token={token}") as ws:
        payload = ws.receive_json()
    assert payload["has_scope"] is True
    assert payload["org_id"] == "cheryfs"
    assert payload["bypass"] is False


def test_ws_without_token_binds_nothing() -> None:
    app = _build_app()
    with _ws_client(app) as client, client.websocket_connect("/ws") as ws:
        payload = ws.receive_json()
    assert payload["has_scope"] is False


def test_ws_with_garbage_token_binds_nothing() -> None:
    app = _build_app()
    with _ws_client(app) as client, client.websocket_connect("/ws?token=not-a-jwt") as ws:
        payload = ws.receive_json()
    assert payload["has_scope"] is False


def test_scope_is_reset_after_connection_ends() -> None:
    app = _build_app()
    token = _sign()
    with _ws_client(app) as client, client.websocket_connect(f"/ws?token={token}"):
        pass
    assert current_scope() is None


def test_http_requests_are_untouched() -> None:
    app = _build_app()
    with _ws_client(app) as client:
        resp = client.get("/http-home")
    assert resp.status_code == 200
    assert resp.json() == {"has_scope": False}
