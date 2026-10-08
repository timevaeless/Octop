"""Tenant + resolution-binding repo tests (``orgs``, ``idp_org_bindings``).

Covers the admin-plane writes behind the 租户 tab: creating a tenant, patching
it (including disabling), deleting it behind a dependents guard, and managing
the upstream→tenant resolution keys.

Two constraints do real work here:

* **Foreign keys.** ``SqlitePool.__init__`` issues ``PRAGMA foreign_keys = ON``,
  so the ``ON DELETE CASCADE`` from ``orgs`` *is* observable — unlike a default
  ``sqlite3`` connection, which would silently accept the orphan delete. Both
  the guard the API relies on and the cascade it guards against are asserted.
* **CHECK.** ``idp_org_bindings`` constrains ``match_type`` and pairs it with
  ``match_value``, so a bad combination is rejected at the repo boundary.

The lesson the last two tests encode: a tenant with no resolution key is
invisible to the chain, so creating the tenant is only half the job.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.orgs import OrgRepo
from octop.infra.users.org_resolution import resolve_org_and_roles

ORG = "cheryfs"
NEW_ORG = "chery-dealer-009"
ALIAS = "chery"


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    with pool.transaction() as conn:
        conn.execute(
            "INSERT INTO orgs(org_id, name, kind, status, created_at, updated_at) "
            "VALUES (?, ?, 'internal', 'active', 0, 0)",
            (ORG, "奇瑞金融（总部）"),
        )
    return pool


@pytest.fixture
def repo(db: SqlitePool) -> OrgRepo:
    return OrgRepo(db)


def make_user(db: SqlitePool, username: str) -> int:
    """Insert a real user row — memberships carry an enforced FK to it."""
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO users(username, role, created_at) VALUES (?, 'user', 0)",
            (username,),
        )
        return int(cur.lastrowid)


# ----- create -----


def test_create_org_stores_every_field(repo: OrgRepo) -> None:
    row = repo.create_org(org_id=NEW_ORG, name="合作商 009", kind="partner")
    assert row is not None
    assert (row.org_id, row.name, row.kind, row.status) == (
        NEW_ORG,
        "合作商 009",
        "partner",
        "active",
    )


def test_create_org_defaults_to_internal_and_active(repo: OrgRepo) -> None:
    row = repo.create_org(org_id=NEW_ORG, name="合作商 009")
    assert row is not None
    assert (row.kind, row.status) == ("internal", "active")


def test_create_org_reports_a_duplicate_instead_of_raising(repo: OrgRepo) -> None:
    assert repo.create_org(org_id=ORG, name="重复的名字") is None
    # The 409 path must not clobber the existing tenant on its way out.
    existing = repo.get_org_any(ORG)
    assert existing is not None
    assert existing.name == "奇瑞金融（总部）"


def test_create_org_can_start_disabled(repo: OrgRepo) -> None:
    row = repo.create_org(org_id=NEW_ORG, name="未启用", status="disabled")
    assert row is not None
    assert row.status == "disabled"
    assert repo.get_org(NEW_ORG) is None


# ----- update (tri-state patch) -----


def test_update_org_patches_only_the_named_fields(repo: OrgRepo) -> None:
    repo.create_org(org_id=NEW_ORG, name="旧名", kind="partner")
    row = repo.update_org(NEW_ORG, name="新名")
    assert row is not None
    assert row.name == "新名"
    # ``None`` means "leave alone" — kind must survive a name-only patch.
    assert row.kind == "partner"


def test_update_org_leaves_updated_at_alone_when_nothing_is_patched(
    repo: OrgRepo,
) -> None:
    before = repo.get_org_any(ORG)
    after = repo.update_org(ORG)
    assert before is not None and after is not None
    assert after.updated_at == before.updated_at


def test_update_org_on_a_missing_tenant_returns_none(repo: OrgRepo) -> None:
    assert repo.update_org("no-such-org", name="x") is None


def test_disabled_org_is_hidden_from_get_org_but_visible_to_the_admin(
    repo: OrgRepo,
) -> None:
    repo.update_org(ORG, status="disabled")
    # get_org is the resolution-chain predicate: a disabled tenant must not
    # resolve. get_org_any is the admin view, which must still see it so the
    # page can re-enable it.
    assert repo.get_org(ORG) is None
    disabled = repo.get_org_any(ORG)
    assert disabled is not None
    assert disabled.status == "disabled"


def test_re_enabling_a_tenant_makes_it_resolvable_again(repo: OrgRepo) -> None:
    repo.update_org(ORG, status="disabled")
    assert repo.get_org(ORG) is None
    repo.update_org(ORG, status="active")
    assert repo.get_org(ORG) is not None


def test_disabling_a_tenant_fails_the_resolution_chain_closed(repo: OrgRepo) -> None:
    repo.create_binding(idp_alias=ALIAS, match_type="claim", match_value=ORG, org_id=ORG)
    repo.update_org(ORG, status="disabled")
    result = resolve_org_and_roles(repo, idp_alias=ALIAS, claims={"org_ref": ORG}, user_id=1)
    assert result.status == "pending"
    assert result.org_id is None
    assert result.role_keys == []


# ----- delete + dependents guard -----


def test_delete_org_reports_whether_it_existed(repo: OrgRepo) -> None:
    assert repo.delete_org(NEW_ORG) is False
    repo.create_org(org_id=NEW_ORG, name="待删")
    assert repo.delete_org(NEW_ORG) is True
    assert repo.get_org_any(NEW_ORG) is None


def test_deleting_an_org_cascades_its_keys_and_memberships(repo: OrgRepo, db: SqlitePool) -> None:
    """The blast radius the API's 409 guard exists to stop.

    ``idp_org_bindings`` and ``org_memberships`` both cascade, so the delete
    itself succeeds — which is exactly why it must be gated upstream.
    """
    repo.create_org(org_id=NEW_ORG, name="合作商 009")
    repo.create_binding(idp_alias=ALIAS, match_type="claim", match_value="9", org_id=NEW_ORG)
    repo.upsert_membership(make_user(db, "u1"), NEW_ORG, role_keys=["expert_user"])
    assert repo.count_org_dependents(NEW_ORG) == (1, 1)

    assert repo.delete_org(NEW_ORG) is True

    assert repo.count_org_dependents(NEW_ORG) == (0, 0)
    assert repo.list_all_bindings() == []
    assert repo.count_memberships_by_org() == {}


def test_count_org_dependents_starts_at_zero(repo: OrgRepo) -> None:
    assert repo.count_org_dependents(ORG) == (0, 0)


def test_count_org_dependents_counts_bindings_and_memberships(
    repo: OrgRepo, db: SqlitePool
) -> None:
    repo.create_binding(idp_alias=ALIAS, match_type="claim", match_value="1", org_id=ORG)
    repo.create_binding(idp_alias=ALIAS, match_type="domain", match_value="qq.com", org_id=ORG)
    repo.upsert_membership(make_user(db, "u1"), ORG, role_keys=[])
    repo.upsert_membership(make_user(db, "u2"), ORG, role_keys=[])
    # These two numbers are what the API turns into the 409 body.
    assert repo.count_org_dependents(ORG) == (2, 2)


def test_count_org_dependents_ignores_other_tenants(repo: OrgRepo, db: SqlitePool) -> None:
    repo.create_org(org_id=NEW_ORG, name="合作商 009")
    repo.create_binding(idp_alias=ALIAS, match_type="claim", match_value="9", org_id=NEW_ORG)
    repo.upsert_membership(make_user(db, "u1"), NEW_ORG, role_keys=[])
    assert repo.count_org_dependents(ORG) == (0, 0)
    assert repo.count_org_dependents(NEW_ORG) == (1, 1)


# ----- per-tenant aggregates for the listing -----


def test_counts_by_org_group_correctly(repo: OrgRepo, db: SqlitePool) -> None:
    repo.create_org(org_id=NEW_ORG, name="合作商 009")
    repo.create_binding(idp_alias=ALIAS, match_type="claim", match_value="1", org_id=ORG)
    repo.create_binding(idp_alias=ALIAS, match_type="claim", match_value="9", org_id=NEW_ORG)
    repo.create_binding(idp_alias=ALIAS, match_type="domain", match_value="a.com", org_id=NEW_ORG)
    repo.upsert_membership(make_user(db, "u1"), ORG, role_keys=[])
    repo.upsert_membership(make_user(db, "u2"), NEW_ORG, role_keys=[])
    repo.upsert_membership(make_user(db, "u3"), NEW_ORG, role_keys=[])
    assert repo.count_bindings_by_org() == {ORG: 1, NEW_ORG: 2}
    assert repo.count_memberships_by_org() == {ORG: 1, NEW_ORG: 2}


def test_counts_by_org_omit_tenants_with_no_rows(repo: OrgRepo) -> None:
    # The API fills absent keys with 0, so the repo omits them.
    assert repo.count_bindings_by_org() == {}
    assert repo.count_memberships_by_org() == {}


# ----- bindings -----


def test_create_binding_rejects_a_duplicate_triple(repo: OrgRepo) -> None:
    assert (
        repo.create_binding(idp_alias=ALIAS, match_type="claim", match_value="1", org_id=ORG)
        is True
    )
    assert (
        repo.create_binding(idp_alias=ALIAS, match_type="claim", match_value="1", org_id=ORG)
        is False
    )
    assert len(repo.list_all_bindings()) == 1


def test_the_same_value_under_another_provider_is_a_different_binding(
    repo: OrgRepo,
) -> None:
    assert repo.create_binding(
        idp_alias="summer-cloud", match_type="claim", match_value="1", org_id=ORG
    )
    assert repo.create_binding(
        idp_alias="keycloak", match_type="claim", match_value="1", org_id=ORG
    )
    assert len(repo.list_all_bindings()) == 2


def test_static_binding_must_carry_an_empty_value(repo: OrgRepo) -> None:
    # The DB CHECK owns this pairing. The repo raises rather than silently
    # normalising, because only the caller knows which of the two the admin
    # actually meant.
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_binding(idp_alias=ALIAS, match_type="static", match_value="cheryfs", org_id=ORG)


def test_static_binding_is_accepted_with_an_empty_value(repo: OrgRepo) -> None:
    assert repo.create_binding(idp_alias=ALIAS, match_type="static", match_value="", org_id=ORG)


def test_claim_binding_must_carry_a_value(repo: OrgRepo) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_binding(idp_alias=ALIAS, match_type="claim", match_value="", org_id=ORG)


def test_domain_binding_is_accepted_with_a_value(repo: OrgRepo) -> None:
    assert repo.create_binding(
        idp_alias=ALIAS, match_type="domain", match_value="qq.com", org_id=ORG
    )


def test_domain_binding_must_carry_a_value(repo: OrgRepo) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_binding(idp_alias=ALIAS, match_type="domain", match_value="", org_id=ORG)


def test_an_unknown_match_type_is_rejected(repo: OrgRepo) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_binding(idp_alias=ALIAS, match_type="magic", match_value="x", org_id=ORG)


def test_a_binding_pointing_at_a_missing_tenant_is_rejected(repo: OrgRepo) -> None:
    # The FK is enforced on SQLite too, so the API's 404 on an unknown org is a
    # friendly wrapper over a constraint that would otherwise surface as a 500.
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_binding(idp_alias=ALIAS, match_type="claim", match_value="9", org_id=NEW_ORG)


def test_delete_binding_reports_whether_it_existed(repo: OrgRepo) -> None:
    repo.create_binding(idp_alias=ALIAS, match_type="claim", match_value="1", org_id=ORG)
    assert repo.delete_binding(ALIAS, "claim", "1") is True
    assert repo.delete_binding(ALIAS, "claim", "1") is False


def test_list_all_bindings_spans_providers_and_sorts_deterministically(
    repo: OrgRepo,
) -> None:
    repo.create_binding(idp_alias="summer-cloud", match_type="claim", match_value="1", org_id=ORG)
    repo.create_binding(idp_alias="keycloak", match_type="static", match_value="", org_id=ORG)
    assert [b.idp_alias for b in repo.list_all_bindings()] == [
        "keycloak",
        "summer-cloud",
    ]
    # list_bindings stays provider-scoped — that is the shape resolution uses,
    # and widening it would leak another IdP's keys into the chain.
    assert [b.idp_alias for b in repo.list_bindings("keycloak")] == ["keycloak"]


def test_binding_carries_its_note_and_approval_flag(repo: OrgRepo) -> None:
    repo.create_binding(
        idp_alias=ALIAS,
        match_type="claim",
        match_value="1",
        org_id=ORG,
        requires_approval=True,
        note="门户 summer 库 system_tenant.id = 1",
    )
    (binding,) = repo.list_all_bindings()
    assert binding.requires_approval is True
    assert binding.note == "门户 summer 库 system_tenant.id = 1"


# ----- end to end: what the tab is for -----


def test_new_tenant_plus_binding_makes_a_first_login_land_there(
    repo: OrgRepo,
) -> None:
    """Create tenant → add key → a brand-new user resolves into it.

    This is the whole point of the tab: without either half the chain fails
    closed, so both writes are exercised together.
    """
    repo.create_role(role_key="expert_user", name_zh="专家使用者", is_default=True)
    repo.create_org(org_id=NEW_ORG, name="合作商 009", kind="partner")
    assert repo.create_binding(idp_alias=ALIAS, match_type="claim", match_value="9", org_id=NEW_ORG)

    result = resolve_org_and_roles(
        repo,
        idp_alias=ALIAS,
        claims={"org_ref": "9", "job_code": "sales_manager"},
        user_id=1,
    )

    assert result.status == "active"
    assert result.org_id == NEW_ORG
    assert result.matched_level == "claim"
    # The unknown role code is auto-provisioned and usable on this very login
    # (plus the seeded platform ``member`` fallback that ships with it).
    assert result.created_mappings == ["sales_manager"]
    assert set(result.role_keys) == {"expert_user", "member"}


def test_a_tenant_without_a_binding_never_matches(repo: OrgRepo) -> None:
    """The other half of the same lesson — creating the tenant is not enough."""
    repo.create_role(role_key="expert_user", name_zh="专家使用者", is_default=True)
    repo.create_org(org_id=NEW_ORG, name="合作商 009", kind="partner")

    result = resolve_org_and_roles(
        repo,
        idp_alias=ALIAS,
        claims={"org_ref": "9", "job_code": "sales_manager"},
        user_id=1,
    )

    assert result.status == "pending"
    assert result.org_id is None
    # Fail-closed also means nothing was seeded on the user's behalf.
    assert repo.list_role_mappings(org_id=None) == []
