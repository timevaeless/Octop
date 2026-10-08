"""Bind the RLS tenant scope for websocket connections (T2-4, pluggable).

The HTTP middleware (``jwt_auth``) only wraps ASGI ``http`` scopes — a
``websocket`` scope never passes through it, so WS handlers used to run with
no tenant scope at all. The pool turns "no scope" into a fail-closed empty
``app.org_id`` on every connection checkout, so every RLS-guarded query in a
websocket handler returned zero rows: agents "not found", chat WS dead,
while the same HTTP endpoints (request scope bound by the middleware) kept
working.

This middleware binds the scope for every websocket connection the same way
the HTTP middleware binds it for requests: resolve the user from the token
the WS endpoints accept (``?token=`` / ``?access_token=`` query params) and
bind ``(org_id, bypass=is_admin)``.

Auth stays with the endpoints — the token is resolved best-effort here; a
connection whose token does not resolve simply binds nothing and the
endpoint rejects it exactly as before. Deleting this module together with
its one install line in ``app.py`` restores pre-T2-4 behaviour verbatim.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs

_INSTALL_ATTR = "_octop_ws_tenant_scope_installed"
logger = logging.getLogger(__name__)


def _resolve_ws_user(scope: dict[str, Any]) -> Any:
    """Resolve the connecting user best-effort from the query string."""
    raw_query = scope.get("query_string") or b""
    query = parse_qs(raw_query.decode("latin-1"), keep_blank_values=False)
    raw = None
    for key in ("token", "access_token"):
        values = query.get(key)
        if values and values[0]:
            raw = values[0]
            break
    if not raw:
        return None

    from octop.api.deps import resolve_user_from_token

    server = scope["app"].state.octop_server
    return resolve_user_from_token(server, raw)


class _WsTenantScopeMiddleware:
    """Pure ASGI middleware: pass http through, bind scope for websockets."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "websocket":
            await self.app(scope, receive, send)
            return

        from octop.infra.users.tenancy_context import (
            bind_tenant_scope,
            reset_tenant_scope,
        )

        token = None
        try:
            user = _resolve_ws_user(scope)
        except Exception:
            logger.debug("ws tenant scope: token not resolved", exc_info=True)
            user = None
        if user is not None:
            token = bind_tenant_scope(
                getattr(user, "org_id", None),
                bypass=bool(getattr(user, "is_admin", False)),
            )
        try:
            await self.app(scope, receive, send)
        finally:
            if token is not None:
                reset_tenant_scope(token)


def install(app: Any) -> None:
    """Idempotent install (called once from ``app.build_app``)."""
    if getattr(app, _INSTALL_ATTR, False):
        return
    setattr(app, _INSTALL_ATTR, True)
    app.add_middleware(_WsTenantScopeMiddleware)
