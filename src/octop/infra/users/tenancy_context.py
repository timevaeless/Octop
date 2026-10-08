"""Tenant scope for PostgreSQL RLS — the single writer of ``app.org_id``.

Migration 025 attaches row-level security policies to the four resource
tables (``published_experts`` / ``agents`` / ``knowledge_bases`` /
``connectors``). The policies read two session settings::

    app.org_id      the tenant the current work belongs to (NULL/'' = none)
    app.bypass_org  'on' → the org boundary is lifted (explicit bypass)

This module owns the Python side of that contract with a ContextVar.
It is written in exactly two kinds of places:

* the auth middleware, once per authenticated request (mirrors the
  request-layer predicate: the org boundary always applies, admins get the
  request-layer cross-tenant flavor);
* long-lived subsystems that resolved an authz context from storage
  (cron delivery, gateway processor, knowledge retrieval, agent manager) —
  they bind the resolved tenant, or bypass in their explicit admin branch.

:mod:`octop.infra.db.pool` transfers the scope onto every connection
checkout with ``set_config(..., is_local=true)``, so the setting dies with
the checkout's transaction — a pooled connection is never left carrying a
stale tenant.

Fail-closed by construction: no scope (background thread, CLI, unknown
user) means the policies see no tenant and no bypass, so only
platform-shared (``org_id IS NULL``) rows are reachable. On SQLite the
scope is inert (no policies); binding it is still correct so behavior is
dialect-independent.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TenantScope:
    """What the RLS policies will see on the next connection checkout."""

    org_id: str | None = None
    bypass: bool = False


_scope: ContextVar[TenantScope | None] = ContextVar("octop_tenant_scope", default=None)


def current_scope() -> TenantScope | None:
    """The scope for the running context, or ``None`` (fail-closed default)."""
    return _scope.get()


def bind_tenant_scope(org_id: str | None, *, bypass: bool = False) -> Token:
    """Bind the scope for the current context; reset with the returned token."""
    return _scope.set(TenantScope(org_id=org_id or None, bypass=bypass))


def reset_tenant_scope(token: Token) -> None:
    _scope.reset(token)


def resolve_and_bind(org_id: str | None, *, bypass: bool = False) -> Token:
    """Alias used by subsystem call sites for grep-ability.

    A reviewer should be able to find every place that lifts the org
    boundary by searching for ``resolve_and_bind(..., bypass=True)``.
    """
    return bind_tenant_scope(org_id, bypass=bypass)


@contextmanager
def scoped_request(org_id: str | None, *, bypass: bool = False) -> Iterator[None]:
    """Bind the scope for the duration of one request dispatch.

    Middleware-shaped wrapper around bind/reset so the upstream hook is a
    single ``with`` around ``call_next`` (the finally-reset is the point:
    the sliding-renew tail must not inherit the request scope).
    """
    token = bind_tenant_scope(org_id, bypass=bypass)
    try:
        yield
    finally:
        reset_tenant_scope(token)


def bind_pooled_connection(conn: Any) -> None:
    """Transfer the current scope onto a Postgres connection (pool checkout).

    ``set_config(..., is_local=true)`` scopes both settings to the checkout's
    transaction: they die when the pool returns the connection, so a pooled
    connection can never carry a stale tenant. Called on every checkout in
    :class:`octop.infra.db.pool.PostgresPool` — a no-op without a scope is
    *not* taken: the empty scope is itself fail-closed (policies see no
    tenant, no bypass). Pluggable hook: the pool keeps only the two-line
    lazy call; deleting this module removes RLS wholesale.
    """
    scope = current_scope()
    org_id = (scope.org_id or "") if scope is not None else ""
    bypass = scope.bypass if scope is not None else False
    conn.execute("select set_config('app.org_id', %s, true)", (org_id,))
    conn.execute(
        "select set_config('app.bypass_org', %s, true)",
        ("on" if bypass else "off",),
    )
