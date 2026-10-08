"""Tenant-scope binding tests (T2-4 RLS Python contract)."""

from __future__ import annotations

from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.orgs import OrgRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.users.context import authz_binding, resolve_authz_context
from octop.infra.users.tenancy_context import (
    TenantScope,
    bind_pooled_connection,
    bind_tenant_scope,
    current_scope,
    reset_tenant_scope,
)


# ----- tenancy_context primitives -----


def test_bind_reset_roundtrip() -> None:
    # The process-wide ContextVar may carry a scope bound by an earlier test
    # (resolve_authz_context binds as a side effect); assert the roundtrip
    # restores whatever was there, not a specific state.
    before = current_scope()
    token = bind_tenant_scope("org-a")
    assert current_scope() == TenantScope(org_id="org-a", bypass=False)
    reset_tenant_scope(token)
    assert current_scope() == before


def test_bind_normalizes_empty_org() -> None:
    token = bind_tenant_scope("")
    assert current_scope() == TenantScope(org_id=None, bypass=False)
    reset_tenant_scope(token)


def test_bind_bypass_flag() -> None:
    token = bind_tenant_scope("org-a", bypass=True)
    assert current_scope() == TenantScope(org_id="org-a", bypass=True)
    reset_tenant_scope(token)


# ----- pool transfer contract -----


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params: object = None) -> None:
        self.calls.append((sql, params))


def test_bind_connection_scope_sets_both_settings_local() -> None:
    conn = _FakeConn()
    token = bind_tenant_scope("org-a")
    try:
        bind_pooled_connection(conn)
    finally:
        reset_tenant_scope(token)
    assert conn.calls == [
        ("select set_config('app.org_id', %s, true)", ("org-a",)),
        ("select set_config('app.bypass_org', %s, true)", ("off",)),
    ]


def test_bind_connection_scope_bypass_on_and_empty_org() -> None:
    conn = _FakeConn()
    token = bind_tenant_scope(None, bypass=True)
    try:
        bind_pooled_connection(conn)
    finally:
        reset_tenant_scope(token)
    assert conn.calls == [
        ("select set_config('app.org_id', %s, true)", ("",)),
        ("select set_config('app.bypass_org', %s, true)", ("on",)),
    ]


# ----- resolve_authz_context binding side effect -----


@pytest.fixture
def db(tmp_path: Path):
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


def _make_admin_and_user(db) -> tuple[int, int]:
    users = UserRepo(db)
    with db.transaction() as conn:
        cur = conn.execute("INSERT INTO users(username, role, created_at) VALUES ('boss','admin',0)")
        admin_id = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO users(username, role, created_at) VALUES ('peon','user',0)"
        )
        user_id = int(cur.lastrowid)
        conn.execute("UPDATE users SET default_org_id = 'org-x' WHERE id = ?", (user_id,))
    orgs = OrgRepo(db)
    orgs.create_org(org_id="org-x", name="org-x")
    orgs.upsert_membership(user_id, "org-x", role_keys=["analyst"])
    return admin_id, user_id


def test_resolve_binds_tenant_scope(db) -> None:
    _, user_id = _make_admin_and_user(db)
    token = bind_tenant_scope(None)  # ensure a clean start
    try:
        authz = resolve_authz_context(UserRepo(db), OrgRepo(db), user_id)
        assert authz.org_id == "org-x"
        assert current_scope() == TenantScope(org_id="org-x", bypass=False)
    finally:
        reset_tenant_scope(token)


def test_resolve_binds_bypass_for_admin(db) -> None:
    admin_id, _ = _make_admin_and_user(db)
    token = bind_tenant_scope(None)
    try:
        authz = resolve_authz_context(UserRepo(db), OrgRepo(db), admin_id)
        assert authz.is_admin is True
        assert current_scope() == TenantScope(org_id=None, bypass=True)
    finally:
        reset_tenant_scope(token)


def test_resolve_unknown_user_binds_empty_scope(db) -> None:
    # Pre-set a tenant scope: the unknown-user resolution must replace it
    # with the empty (fail-closed) one, not inherit it.
    token = bind_tenant_scope("org-x", bypass=True)
    try:
        authz = resolve_authz_context(UserRepo(db), OrgRepo(db), 999999)
        assert authz.org_id is None
        assert current_scope() == TenantScope(org_id=None, bypass=False)
    finally:
        reset_tenant_scope(token)


def test_authz_binding_restores_prior_scope(db) -> None:
    """P1-3: the bounded window must not leave an admin bypass behind."""
    admin_id, user_id = _make_admin_and_user(db)
    prior = bind_tenant_scope("org-previous")
    try:
        # Admin resolution inside the window binds bypass...
        with authz_binding(UserRepo(db), OrgRepo(db), admin_id) as authz:
            assert authz.is_admin is True
            assert current_scope() == TenantScope(org_id=None, bypass=True)
        # ...and the window exit restores the prior scope verbatim.
        assert current_scope() == TenantScope(org_id="org-previous", bypass=False)

        # Same for a non-admin tenant resolution.
        with authz_binding(UserRepo(db), OrgRepo(db), user_id) as authz:
            assert authz.org_id == "org-x"
            assert current_scope() == TenantScope(org_id="org-x", bypass=False)
        assert current_scope() == TenantScope(org_id="org-previous", bypass=False)

        # Unknown users bind the empty (fail-closed) scope inside the window.
        with authz_binding(UserRepo(db), OrgRepo(db), 999999) as authz:
            assert authz.org_id is None
            assert current_scope() == TenantScope(org_id=None, bypass=False)
        assert current_scope() == TenantScope(org_id="org-previous", bypass=False)
    finally:
        reset_tenant_scope(prior)
