"""Tenant-scoped ability-package repo tests (``repos/tenant_roles.py``).

Covers the 2026-09-30 split at the storage layer:

* visibility — a tenant sees platform packages (``org_id IS NULL``) plus its
  own rows, never another tenant's;
* the reserved ``t_{org}_`` prefix — display keys are namespaced per tenant
  (two tenants may pick the same name), malformed keys are refused, and the
  platform namespace is protected at the DB level by a CHECK;
* ``is_system`` rows cannot be edited, deleted, or granted by a tenant;
* deleting a package cascades its mapping/grant rows (FK), while stale keys
  inside membership JSON are the hydration pass's problem, not the repo's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.orgs import OrgRepo
from octop.infra.db.repos.tenant_roles import (
    TENANT_ROLE_PREFIX,
    TenantRoleRepo,
    is_tenant_role_key,
    tenant_role_key,
)

ORG = "cheryfs"
OTHER_ORG = "chery-dealer-001"
ALIAS = "chery"


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    with pool.transaction() as conn:
        for org_id, name in ((ORG, "总部"), (OTHER_ORG, "合作商 001")):
            conn.execute(
                "INSERT INTO orgs(org_id, name, kind, status, created_at, updated_at) "
                "VALUES (?, ?, 'internal', 'active', 0, 0)",
                (org_id, name),
            )
    return pool


@pytest.fixture
def repo(db: SqlitePool) -> TenantRoleRepo:
    return TenantRoleRepo(db)


def _keys(rows) -> set[str]:
    return {r.role_key for r in rows}


# ----- visibility -----


def test_visibility_is_platform_plus_own(repo: TenantRoleRepo) -> None:
    # The migrations seed two platform packages; nothing is tenant-owned yet.
    assert _keys(repo.list_visible_roles(ORG)) == {"org_admin", "member"}

    repo.create_tenant_role(ORG, bare_key="editor", name_zh="编辑")
    assert _keys(repo.list_visible_roles(ORG)) == {
        "org_admin",
        "member",
        f"{TENANT_ROLE_PREFIX}{ORG}_editor",
    }
    # The other tenant sees the platform rows but not cheryfs's package.
    assert _keys(repo.list_visible_roles(OTHER_ORG)) == {"org_admin", "member"}


def test_grantable_roles_exclude_system_rows(repo: TenantRoleRepo) -> None:
    repo.create_tenant_role(ORG, bare_key="editor", name_zh="编辑")
    assert _keys(repo.list_grantable_roles(ORG)) == {
        f"{TENANT_ROLE_PREFIX}{ORG}_editor"
    }


def test_a_foreign_tenant_package_is_invisible_here(repo: TenantRoleRepo) -> None:
    mine = repo.create_tenant_role(ORG, bare_key="editor", name_zh="编辑")
    theirs = f"{TENANT_ROLE_PREFIX}{OTHER_ORG}_editor"
    from octop.infra.db.repos.orgs import OrgRepo

    OrgRepo(repo._orgs._db).create_role(
        role_key=theirs, name_zh="别家的", org_id=OTHER_ORG
    )
    assert repo.get_visible_role(ORG, mine.role_key) is not None
    assert repo.get_visible_role(ORG, theirs) is None


# ----- the prefix convention -----


def test_tenant_role_key_namespaces_the_display_key() -> None:
    assert tenant_role_key(ORG, "editor") == f"t_{ORG}_editor"


def test_tenant_role_key_rejects_malformed_display_keys() -> None:
    for bad in ("Editor", "has space", "emoji🎉", "", "x" * 65):
        with pytest.raises(ValueError):
            tenant_role_key(ORG, bad)


def test_two_tenants_may_pick_the_same_display_key(repo: TenantRoleRepo) -> None:
    a = repo.create_tenant_role(ORG, bare_key="editor", name_zh="编辑")
    b = repo.create_tenant_role(OTHER_ORG, bare_key="editor", name_zh="编辑")
    assert a.role_key != b.role_key
    assert a.bare_key == b.bare_key == "editor"
    assert is_tenant_role_key(a.role_key, ORG)
    assert not is_tenant_role_key(a.role_key, OTHER_ORG)


def test_a_duplicate_tenant_package_surfaces_as_keyerror(repo: TenantRoleRepo) -> None:
    """``INSERT … ON CONFLICT DO NOTHING`` must not silently swallow a clash."""
    repo.create_tenant_role(ORG, bare_key="taken", name_zh="第一个")
    with pytest.raises(KeyError):
        repo.create_tenant_role(ORG, bare_key="taken", name_zh="第二个")


def test_the_platform_namespace_is_protected_at_the_db_level(db: SqlitePool) -> None:
    """A platform package must never squat the ``t_`` prefix (018 CHECK)."""
    from octop.infra.db.repos.orgs import OrgRepo
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        OrgRepo(db).create_role(role_key="t_fake", name_zh="伪装", org_id=None)


# ----- CRUD guards -----


def test_update_and_delete_reject_system_and_foreign_rows(repo: TenantRoleRepo) -> None:
    storage_key = f"{TENANT_ROLE_PREFIX}{ORG}_editor"
    repo.create_tenant_role(ORG, bare_key="editor", name_zh="编辑")

    assert repo.update_tenant_role(ORG, "org_admin", name_zh="改名") is None
    assert repo.update_tenant_role(ORG, "member", description="x") is None
    assert repo.update_tenant_role(OTHER_ORG, storage_key, name_zh="改名") is None
    assert repo.delete_tenant_role(ORG, "org_admin") is False
    assert repo.delete_tenant_role(ORG, "member") is False
    assert repo.delete_tenant_role(OTHER_ORG, storage_key) is False
    # The owner still can.
    assert repo.delete_tenant_role(ORG, storage_key) is True


def test_deleting_a_package_cascades_its_mappings_and_grants(
    repo: TenantRoleRepo, db: SqlitePool
) -> None:
    org_repo = repo._orgs
    with db.transaction() as conn:
        conn.execute("INSERT INTO users(username, role, created_at) VALUES ('u1', 'user', 0)")
    view = repo.create_tenant_role(ORG, bare_key="editor", name_zh="编辑")
    org_repo.set_role_mappings(ORG, "code_a", [view.role_key])
    org_repo.replace_member_grants(1, ORG, [view.role_key], granted_by="boss")

    assert repo.delete_tenant_role(ORG, view.role_key) is True

    assert org_repo.list_role_mappings(org_id=ORG, job_codes=["code_a"]) == []
    assert org_repo.list_member_grants(1, ORG) == []


# ----- validation helper -----


def test_validate_keys_visible_flags_everything_out_of_scope(
    repo: TenantRoleRepo,
) -> None:
    mine = repo.create_tenant_role(ORG, bare_key="editor", name_zh="编辑")
    assert repo.validate_keys_visible(ORG, ["member", mine.role_key]) == []
    assert repo.validate_keys_visible(
        ORG, ["member", "nope", f"t_{OTHER_ORG}_x"]
    ) == ["nope", f"t_{OTHER_ORG}_x"]


# ----- tenant mapping view -----


def test_tenant_mappings_exclude_the_global_rows(repo: TenantRoleRepo) -> None:
    org_repo = repo._orgs
    view = repo.create_tenant_role(ORG, bare_key="editor", name_zh="编辑")
    org_repo.set_role_mappings(ORG, "code_a", [view.role_key])
    org_repo.set_role_mappings("*", "code_b", ["member"])

    rows = repo.list_tenant_mappings(ORG)
    assert {m.job_code for m in rows} == {"code_a"}
    assert rows[0].role_key == view.role_key


# ----- boot-time seed repair (review 2026-09-30 P1-1) -----


def test_boot_seed_reasserts_system_invariants(db: SqlitePool) -> None:
    """A pre-is_system upgrade ALTERs the roles table and leaves the seeded
    ``org_admin``/``member`` rows with ``is_system = 0`` — grantable by the
    tenant admin (privilege-escalation channel). The boot seed must re-assert
    the invariants (``DO UPDATE``, not ``DO NOTHING``) on every start.
    """
    from octop.infra.db.tenancy_schema import ensure_multi_tenant_schema

    with db.transaction() as conn:
        conn.execute(
            "UPDATE roles SET is_system = 0, is_default = 1, org_id = ? "
            "WHERE role_key = 'org_admin'",
            (ORG,),
        )
        conn.execute(
            "UPDATE roles SET is_system = 0, is_default = 0 WHERE role_key = 'member'"
        )

    ensure_multi_tenant_schema(db)

    by_key = {r.role_key: r for r in OrgRepo(db).list_roles()}
    assert by_key["org_admin"].is_system is True
    assert by_key["org_admin"].is_default is False
    assert by_key["org_admin"].org_id is None
    assert by_key["member"].is_system is True
    assert by_key["member"].is_default is True
