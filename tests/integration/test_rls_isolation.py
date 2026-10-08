"""Gated PostgreSQL RLS isolation tests (migration 025).

Review 2026-09-26 task E: the RLS layer shipped with zero PG-level tests —
the Python side was only exercised against fakes, and SQLite is inert. This
file proves the load-bearing behaviors against a live database:

1. fail-closed: an unbound scope sees only explicit platform shares
   (``org_id = '*'``); unattributed drafts (``org_id IS NULL``) are
   bypass-only (2026-09-27 attribution redesign, migration 029);
2. two tenants are mutually invisible through the app pool;
3. WITH CHECK rejects cross-tenant INSERTs;
4. ``set_config(..., is_local=true)`` dies with the checkout — a pooled
   connection never carries a stale tenant;
5. FORCE makes even the (non-superuser) table owner subject to the policy;
6. (documented PG semantics) a superuser bypasses FORCE entirely.

Setup (DEDICATED database — the test drops schema public)::

    docker exec octop-postgres psql -U octop_admin -c 'CREATE DATABASE octop_rls_test;'
    export OCTOP_TEST_DATABASE_URL='postgresql://octop_admin:octop_admin@127.0.0.1:15433/octop_rls_test'

The URL user must be able to CREATE ROLE (superuser/CREATEROLE): the test
provisions a dedicated non-superuser schema owner and a probe role, because
every interesting RLS property is invisible from a superuser connection.
"""

from __future__ import annotations

import os
import re
import uuid
from urllib.parse import urlparse

import pytest

from tests.support.postgresql import requires_postgresql


def _conninfo() -> str:
    return os.environ["OCTOP_TEST_DATABASE_URL"]


def _conninfo_as(url: str, user: str, password: str) -> str:
    return re.sub(r"//[^@/]+@", f"//{user}:{password}@", url)


def _dbname(url: str) -> str:
    return (urlparse(url).path or "/octop").lstrip("/") or "octop"


@requires_postgresql
@pytest.mark.postgresql
def test_rls_isolation_scopes_and_force() -> None:
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import PostgresPool
    from octop.infra.users.tenancy_context import (
        bind_tenant_scope,
        reset_tenant_scope,
    )

    suffix = uuid.uuid4().hex[:8]
    owner_name, probe_name = f"rls_owner_{suffix}", f"rls_probe_{suffix}"
    owner_url = _conninfo_as(_conninfo(), owner_name, "pw-owner")
    probe_url = _conninfo_as(_conninfo(), probe_name, "pw-probe")

    admin = PostgresPool(_conninfo())
    try:
        # fresh schema + capability probe on the admin role
        with admin.connect() as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
            conn.execute("COMMIT")
            who = conn.execute(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            ).fetchone()
        assert who is not None
        admin_is_super = bool(who["rolsuper"] or who["rolbypassrls"])
        if not admin_is_super:
            pytest.skip(
                "RLS test provisions roles; point OCTOP_TEST_DATABASE_URL at a "
                "superuser/CREATEROLE connection"
            )

        # dedicated non-superuser schema owner + non-owner probe role: every
        # interesting RLS property is invisible from a superuser connection.
        # Identifiers are f-stringed but safe: both names are fixed prefixes
        # + a uuid4 hex slice generated above (no external input).
        with admin.connect() as conn:
            conn.execute(
                f"CREATE ROLE {owner_name} LOGIN PASSWORD 'pw-owner' NOSUPERUSER"
            )
            conn.execute(
                f"CREATE ROLE {probe_name} LOGIN PASSWORD 'pw-probe' NOSUPERUSER"
            )
            conn.execute(f"ALTER SCHEMA public OWNER TO {owner_name}")
            dbname = _dbname(_conninfo())
            conn.execute(f"GRANT CONNECT ON DATABASE {dbname} TO {owner_name}, {probe_name}")
            conn.execute("COMMIT")

        owner = PostgresPool(owner_url)
        try:
            run_migrations(owner)  # tables owned by the non-superuser role

            with owner.connect() as conn:
                conn.execute(f"GRANT USAGE ON SCHEMA public TO {probe_name}")
                conn.execute(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE "
                    f"ON ALL TABLES IN SCHEMA public TO {probe_name}"
                )
                conn.execute("COMMIT")

            # seed through the app repos under an explicit bypass window —
            # the owner itself is FORCE-subject to the policies now
            from octop.infra.db.repos.knowledge import KnowledgeRepo
            from octop.infra.db.repos.users import UserRepo

            seed_token = bind_tenant_scope(None, bypass=True)
            try:
                users = UserRepo(owner)
                owner_id = users.create(
                    username=f"rls_seed_{suffix}", password_hash="x", role="user"
                )
                krepo = KnowledgeRepo(owner)
                # 2026-09-27: '*' = explicit platform share; NULL = unattributed
                # draft (bypass-only); the two named tenants round out the matrix.
                krepo.create_base(owner_user_id=owner_id, name="kb-shared", org_id="*")
                krepo.create_base(owner_user_id=owner_id, name="kb-draft", org_id=None)
                krepo.create_base(owner_user_id=owner_id, name="kb-a", org_id="org-a")
                krepo.create_base(owner_user_id=owner_id, name="kb-b", org_id="org-b")
            finally:
                reset_tenant_scope(seed_token)

            probe = PostgresPool(probe_url)
            try:

                def kb_names() -> set[str]:
                    with probe.connect() as conn:
                        rows = conn.execute("SELECT name FROM knowledge_bases").fetchall()
                    return {r["name"] for r in rows}

                # 1. fail-closed: no scope -> only explicit platform shares
                # (the unattributed draft stays invisible even here)
                token = bind_tenant_scope(None)
                try:
                    assert kb_names() == {"kb-shared"}
                finally:
                    reset_tenant_scope(token)

                # 1b. the bypass scope is the only non-superuser route to a
                # draft — mirror of the Python request-layer admin flavor
                token = bind_tenant_scope(None, bypass=True)
                try:
                    assert kb_names() == {"kb-shared", "kb-draft", "kb-a", "kb-b"}
                finally:
                    reset_tenant_scope(token)

                # 2. tenant A sees own + shared, never tenant B's rows
                token = bind_tenant_scope("org-a")
                try:
                    assert kb_names() == {"kb-a", "kb-shared"}
                    with probe.connect() as conn:
                        rows = conn.execute(
                            "SELECT name FROM knowledge_bases WHERE name = 'kb-b'"
                        ).fetchall()
                    assert rows == []
                finally:
                    reset_tenant_scope(token)

                # 3. tenant B sees its own rows, not A's (mirror direction)
                token = bind_tenant_scope("org-b")
                try:
                    assert kb_names() == {"kb-b", "kb-shared"}
                finally:
                    reset_tenant_scope(token)

                # 4. WITH CHECK: the org-a scope cannot INSERT an org-b row
                token = bind_tenant_scope("org-a")
                try:
                    with pytest.raises(Exception, match="[Rr]ow-level [Ss]ecurity"):
                        KnowledgeRepo(probe).create_base(
                            owner_user_id=owner_id, name="kb-bad", org_id="org-b"
                        )
                finally:
                    reset_tenant_scope(token)

                # 5. the setting died with the checkout: back to no scope, the
                # same pool must not remember org-a (is_local=true contract)
                token = bind_tenant_scope(None)
                try:
                    assert kb_names() == {"kb-shared"}
                finally:
                    reset_tenant_scope(token)
            finally:
                probe.close()

            # 6. FORCE: the non-superuser owner is subject to its own policies
            token = bind_tenant_scope("org-b")
            try:
                with owner.connect() as conn:
                    rows = conn.execute(
                        "SELECT name FROM knowledge_bases WHERE name = 'kb-a'"
                    ).fetchall()
                assert rows == []
            finally:
                reset_tenant_scope(token)

            # 7. documented PG semantics: a superuser bypasses FORCE entirely
            if admin_is_super:
                with admin.connect() as conn:
                    count = conn.execute(
                        "SELECT count(*) AS n FROM knowledge_bases"
                    ).fetchone()
                assert count is not None
                assert int(count["n"]) == 4
        finally:
            owner.close()
    finally:
        admin.close()
