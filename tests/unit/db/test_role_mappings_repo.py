"""Role-mapping repo + JIT auto-provisioning tests.

Covers the 角色映射 semantics:

* one role code owns a *set* of ability packages, replaced atomically;
* renaming the role code never leaves it granting nothing, and never unions
  onto the code it is renamed into;
* a role code the platform has never seen is auto-created on login against the
  default-minimal ability package (``roles.is_default``), and only when it has
  no row at all;
* auto-creation is scoped to **the tenant whose login revealed the code**, so
  one tenant's first sign-in never sets policy for tenants nobody reviewed;
* an admin-authored ``'*'`` row is **unioned** with tenant rows rather than
  overridden by them, so it deliberately reaches every tenant — which is why
  every row carries provenance (``origin`` + the triggering tenant in
  ``source_org``) instead of looking like an unattributed global default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.orgs import OrgRepo
from octop.infra.users.org_resolution import (
    ANY_ORG,
    expand_role_keys,
    provision_missing_mappings,
    resolve_org_and_roles,
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
                "VALUES (?, ?, 'tenant', 'active', 0, 0)",
                (org_id, name),
            )
        # Level-① claim binding, so ``org_ref=<ORG>`` resolves without a prior
        # membership — the state a genuinely first-time user is in.
        conn.execute(
            "INSERT INTO idp_org_bindings(idp_alias, match_type, match_value, org_id, "
            "created_at, updated_at) VALUES (?, 'claim', ?, ?, 0, 0)",
            (ALIAS, ORG, ORG),
        )
    return pool


@pytest.fixture
def repo(db: SqlitePool) -> OrgRepo:
    r = OrgRepo(db)
    r.create_role(role_key="intake_submitter", name_zh="进件提交")
    r.create_role(role_key="finance_operator", name_zh="财务处理")
    r.create_role(role_key="report_viewer", name_zh="报表查看")
    r.create_role(role_key="expert_user", name_zh="专家使用者", is_default=True)
    return r


def _rows(repo: OrgRepo, org_id: str | None = None) -> set[tuple[str, str, str]]:
    return {(m.org_id, m.job_code, m.role_key) for m in repo.list_role_mappings(org_id=org_id)}


# --------------------------------------------------------------- replace


def test_set_role_mappings_replaces_the_whole_set(repo: OrgRepo) -> None:
    repo.set_role_mappings(ANY_ORG, "Business Accounting", ["finance_operator", "report_viewer"])
    assert _rows(repo) == {
        (ANY_ORG, "Business Accounting", "finance_operator"),
        (ANY_ORG, "Business Accounting", "report_viewer"),
    }

    # Editing down to a single package must drop the one left out.
    repo.set_role_mappings(ANY_ORG, "Business Accounting", ["report_viewer"])
    assert _rows(repo) == {(ANY_ORG, "Business Accounting", "report_viewer")}


def test_set_role_mappings_empty_list_deletes_the_mapping(repo: OrgRepo) -> None:
    repo.set_role_mappings(ANY_ORG, "Business Accounting", ["finance_operator"])
    repo.set_role_mappings(ANY_ORG, "Business Accounting", [])
    assert _rows(repo) == set()


def test_renaming_keeps_no_orphan_rows(repo: OrgRepo) -> None:
    repo.set_role_mappings(ANY_ORG, "finance_manager", ["finance_operator"])

    repo.set_role_mappings(
        ANY_ORG,
        "Finance Manager",
        ["finance_operator", "report_viewer"],
        previous_org_id=ANY_ORG,
        previous_job_code="finance_manager",
    )

    assert _rows(repo) == {
        (ANY_ORG, "Finance Manager", "finance_operator"),
        (ANY_ORG, "Finance Manager", "report_viewer"),
    }


def test_renaming_onto_an_existing_code_replaces_instead_of_unioning(repo: OrgRepo) -> None:
    repo.set_role_mappings(ANY_ORG, "old_code", ["intake_submitter"])
    repo.set_role_mappings(ANY_ORG, "new_code", ["report_viewer"])

    repo.set_role_mappings(
        ANY_ORG,
        "new_code",
        ["finance_operator"],
        previous_org_id=ANY_ORG,
        previous_job_code="old_code",
    )

    # ``new_code`` must hold exactly what the admin chose: neither its previous
    # ``report_viewer`` nor the old code's ``intake_submitter``.
    assert _rows(repo) == {(ANY_ORG, "new_code", "finance_operator")}


def test_moving_a_role_code_to_another_scope_leaves_the_old_scope_empty(repo: OrgRepo) -> None:
    repo.set_role_mappings(ANY_ORG, "regional_manager", ["intake_submitter"])

    repo.set_role_mappings(
        OTHER_ORG,
        "regional_manager",
        ["finance_operator"],
        previous_org_id=ANY_ORG,
        previous_job_code="regional_manager",
    )

    assert _rows(repo) == {(OTHER_ORG, "regional_manager", "finance_operator")}


# ------------------------------------------------------------ is_default


def test_update_role_can_flag_the_default_minimal_package(repo: OrgRepo) -> None:
    # The migrations seed the platform ``member`` package as the guaranteed
    # JIT fallback, so ``expert_user`` joins it rather than being the only one.
    assert [r.role_key for r in repo.list_roles() if r.is_default] == [
        "expert_user",
        "member",
    ]

    updated = repo.update_role("intake_submitter", is_default=True)
    assert updated is not None and updated.is_default is True

    flagged = {r.role_key for r in repo.list_roles() if r.is_default}
    assert flagged == {"expert_user", "intake_submitter", "member"}

    # And it can be cleared again — ``None`` means "leave alone", not "false".
    assert repo.update_role("intake_submitter", is_default=False).is_default is False
    assert repo.update_role("intake_submitter", name_zh="进件提交").is_default is False


# ------------------------------------------------------- auto-provision


def test_provision_creates_missing_codes_with_the_default_package(repo: OrgRepo) -> None:
    created = provision_missing_mappings(repo, ORG, ["new_upstream_role"])

    assert created == ["new_upstream_role"]
    # Scoped to the tenant whose login revealed the code — never to ``'*'``.
    # Every is_default package the tenant can see is granted, so the seeded
    # platform ``member`` fallback rides along with ``expert_user``.
    assert _rows(repo) == {
        (ORG, "new_upstream_role", "expert_user"),
        (ORG, "new_upstream_role", "member"),
    }


def test_provision_is_idempotent_and_reports_nothing_the_second_time(repo: OrgRepo) -> None:
    provision_missing_mappings(repo, ORG, ["new_upstream_role"])
    assert provision_missing_mappings(repo, ORG, ["new_upstream_role"]) == []


def test_provision_leaves_a_trimmed_code_alone(repo: OrgRepo) -> None:
    """An admin who trims a code down must not be overridden on next login."""
    repo.set_role_mappings(ANY_ORG, "Finance Manager", ["report_viewer"])

    assert provision_missing_mappings(repo, ORG, ["Finance Manager"]) == []
    assert _rows(repo) == {(ANY_ORG, "Finance Manager", "report_viewer")}


def test_provision_treats_an_org_specific_row_as_covering_the_code(repo: OrgRepo) -> None:
    repo.set_role_mappings(OTHER_ORG, "regional_manager", ["report_viewer"])

    # No ``'*'`` row exists, but the code is mapped *for that tenant*, so
    # nothing is created for it...
    assert provision_missing_mappings(repo, OTHER_ORG, ["regional_manager"]) == []
    # ...while the other tenant is not covered by that row and gets its own.
    assert provision_missing_mappings(repo, ORG, ["regional_manager"]) == ["regional_manager"]
    assert _rows(repo) == {
        (OTHER_ORG, "regional_manager", "report_viewer"),
        (ORG, "regional_manager", "expert_user"),
        (ORG, "regional_manager", "member"),
    }


def test_provision_creates_nothing_without_a_default_package(db: SqlitePool) -> None:
    bare = OrgRepo(db)
    # The migrations seed a platform fallback package on purpose; drop every
    # is_default row to restore the "no minimal package exists" premise.
    with db.transaction() as conn:
        conn.execute("DELETE FROM roles WHERE is_default = 1")
    bare.create_role(role_key="only_package", name_zh="唯一能力包")

    assert provision_missing_mappings(bare, ORG, ["unmapped_role"]) == []
    assert _rows(bare) == set()


def test_provision_ignores_empty_input(repo: OrgRepo) -> None:
    assert provision_missing_mappings(repo, ORG, []) == []
    assert _rows(repo) == set()


# -------------------------------------------------- end-to-end resolution


def test_new_role_code_gets_its_minimal_package_on_the_very_first_login(
    repo: OrgRepo,
) -> None:
    result = resolve_org_and_roles(
        repo,
        idp_alias=ALIAS,
        claims={"org_ref": ORG, "job_code": "brand_new_role"},
        user_id=1,
    )

    assert result.status == "active"
    assert result.matched_level == "claim"
    assert result.created_mappings == ["brand_new_role"]
    # The point of provisioning before expanding: usable on login #1.
    assert result.role_keys == ["expert_user", "member"]
    assert _rows(repo) == {
        (ORG, "brand_new_role", "expert_user"),
        (ORG, "brand_new_role", "member"),
    }


def test_fully_unmapped_login_still_yields_nothing_when_no_default_exists(
    db: SqlitePool,
) -> None:
    """Without a flagged package the old fail-closed behaviour is preserved."""
    bare = OrgRepo(db)
    # Drop the seeded fallback so the premise actually holds.
    with db.transaction() as conn:
        conn.execute("DELETE FROM roles WHERE is_default = 1")
    bare.create_role(role_key="only_package", name_zh="唯一能力包")

    result = resolve_org_and_roles(
        bare,
        idp_alias=ALIAS,
        claims={"org_ref": ORG, "job_code": "brand_new_role"},
        user_id=1,
    )

    assert result.role_keys == []
    assert result.created_mappings == []


def test_mixed_mapped_and_unmapped_codes_union_correctly(repo: OrgRepo) -> None:
    repo.set_role_mappings(ANY_ORG, "Business Accounting", ["finance_operator"])
    repo.set_role_mappings(ANY_ORG, "Finance Manager", ["report_viewer"])

    result = resolve_org_and_roles(
        repo,
        idp_alias=ALIAS,
        claims={"org_ref": ORG, "job_code": "Business Accounting,Finance Manager,newcomer"},
        user_id=1,
    )

    assert result.created_mappings == ["newcomer"]
    assert set(result.role_keys) == {
        "finance_operator",
        "report_viewer",
        "expert_user",
        "member",
    }
    # Only the unknown code was provisioned, and for this tenant only.
    assert (ORG, "newcomer", "expert_user") in _rows(repo)
    assert (ORG, "newcomer", "member") in _rows(repo)
    assert (ANY_ORG, "newcomer", "expert_user") not in _rows(repo)
    assert len(_rows(repo)) == 4


def test_pending_user_never_seeds_mappings(repo: OrgRepo) -> None:
    """No tenant matched ⇒ nothing is written, not even the default row."""
    result = resolve_org_and_roles(
        repo,
        idp_alias=ALIAS,
        claims={"org_ref": "no-such-org", "job_code": "brand_new_role"},
        user_id=1,
    )

    assert result.is_pending
    assert result.created_mappings == []
    assert _rows(repo) == set()


def test_expand_only_returns_mapped_packages(repo: OrgRepo) -> None:
    repo.set_role_mappings(ANY_ORG, "khown", ["finance_operator"])
    assert expand_role_keys(repo, ORG, ["khown", "unknown"]) == ["finance_operator"]
    assert expand_role_keys(repo, ORG, []) == []


def test_provision_rejects_junk_role_codes(repo: OrgRepo) -> None:
    """A misconfigured IdP must not be able to grow the table without bound."""
    created = provision_missing_mappings(repo, ORG, ["good_code", "has space", "x" * 65, "emoji🎉"])

    assert created == ["good_code"]
    assert _rows(repo) == {
        (ORG, "good_code", "expert_user"),
        (ORG, "good_code", "member"),
    }


# ------------------------------------------------- scope: union, not override


def test_a_tenant_row_is_unioned_with_the_global_row_not_overriding_it(repo: OrgRepo) -> None:
    """``'*'`` is a platform-wide grant, not a default that a tenant row replaces.

    This is the single most misread rule in the 角色映射 page: the UI labels the
    scope 「全部租户（默认）」, which reads like "fallback — a tenant row takes
    over". It does not. ``list_role_mappings`` filters ``org_id IN ('*', ?)``
    and ``expand_role_keys`` unions the hits, so adding a tenant-specific row
    *adds* to what the global row already grants.

    Consequence worth knowing before trimming access: you cannot reduce a role
    code for one tenant by adding a narrower tenant row — you must edit or
    delete the ``'*'`` row, which changes **every** tenant at once.
    """
    repo.set_role_mappings(ANY_ORG, "super_admin", ["expert_user"])
    assert expand_role_keys(repo, ORG, ["super_admin"]) == ["expert_user"]

    repo.set_role_mappings(ORG, "super_admin", ["finance_operator"])

    assert expand_role_keys(repo, ORG, ["super_admin"]) == ["expert_user", "finance_operator"]
    # The other tenant is untouched — only the tenant-scoped row is filtered out.
    assert expand_role_keys(repo, OTHER_ORG, ["super_admin"]) == ["expert_user"]


def test_a_jit_row_is_scoped_to_the_tenant_whose_login_created_it(repo: OrgRepo) -> None:
    """One tenant's first login must never set policy for the others.

    Auto-provisioning used to write ``('*', code, default)``, so cheryfs's very
    first login handed ``expert_user`` to every dealer on the platform —
    including tenants no admin had ever opened. The row is written against the
    triggering tenant now, so the dealers keep getting nothing until one of
    *their* users signs in and provisions their own row.
    """
    resolve_org_and_roles(
        repo,
        idp_alias=ALIAS,
        claims={"org_ref": ORG, "job_code": "brand_new_role"},
        user_id=1,
    )

    assert expand_role_keys(repo, ORG, ["brand_new_role"]) == [
        "expert_user",
        "member",
    ]
    # The dealer is not covered by cheryfs's row, and is not given one either.
    assert expand_role_keys(repo, OTHER_ORG, ["brand_new_role"]) == []


def test_each_tenant_provisions_its_own_row_on_its_own_first_login(repo: OrgRepo) -> None:
    """Same code, different tenants ⇒ one row each, not one shared ``'*'`` row."""
    resolve_org_and_roles(
        repo,
        idp_alias=ALIAS,
        claims={"org_ref": ORG, "job_code": "brand_new_role"},
        user_id=1,
    )
    assert _rows(repo) == {
        (ORG, "brand_new_role", "expert_user"),
        (ORG, "brand_new_role", "member"),
    }

    created = provision_missing_mappings(repo, OTHER_ORG, ["brand_new_role"])

    assert created == ["brand_new_role"]
    assert _rows(repo) == {
        (ORG, "brand_new_role", "expert_user"),
        (ORG, "brand_new_role", "member"),
        (OTHER_ORG, "brand_new_role", "expert_user"),
        (OTHER_ORG, "brand_new_role", "member"),
    }
    assert expand_role_keys(repo, OTHER_ORG, ["brand_new_role"]) == [
        "expert_user",
        "member",
    ]


def test_an_admin_global_row_covers_every_tenant_and_suppresses_provisioning(
    repo: OrgRepo,
) -> None:
    """``'*'`` is still the deliberate platform-wide grant — admins only.

    A tenant whose code is covered by an explicit ``'*'`` row gets that grant,
    and JIT has nothing left to fill in: the row is visible to the tenant, so
    the code counts as covered.
    """
    repo.set_role_mappings(ANY_ORG, "brand_new_role", ["report_viewer"])

    assert expand_role_keys(repo, OTHER_ORG, ["brand_new_role"]) == ["report_viewer"]
    assert provision_missing_mappings(repo, OTHER_ORG, ["brand_new_role"]) == []


# ------------------------------------------------------------ provenance


def test_jit_rows_record_origin_and_the_tenant_that_triggered_them(repo: OrgRepo) -> None:
    created = provision_missing_mappings(repo, ORG, ["brand_new_role"])

    assert created == ["brand_new_role"]
    rows = repo.list_role_mappings(org_id=None, job_codes=["brand_new_role"])
    assert {r.role_key for r in rows} == {"expert_user", "member"}
    for row in rows:
        assert row.origin == "jit"
        assert row.is_jit is True
        # Scope and provenance agree: the row applies where it was triggered.
        assert row.org_id == ORG
        assert row.source_org == ORG
        assert row.created_at > 0


def test_admin_written_rows_are_marked_manual_with_no_source_tenant(repo: OrgRepo) -> None:
    repo.set_role_mappings(ANY_ORG, "super_admin", ["expert_user"])

    (row,) = repo.list_role_mappings(org_id=None, job_codes=["super_admin"])
    assert row.origin == "manual"
    assert row.is_jit is False
    assert row.source_org is None
    assert row.created_at > 0


def test_editing_a_jit_row_clears_its_auto_provisioned_provenance(repo: OrgRepo) -> None:
    """An admin review must not keep looking like an unreviewed default.

    ``set_role_mappings`` deletes and re-inserts the group, so the replacement
    rows are stamped ``manual``. Carrying the old ``source_org`` over would make
    a deliberately narrowed policy indistinguishable from what JIT guessed.
    """
    provision_missing_mappings(repo, ORG, ["brand_new_role"])
    assert repo.list_role_mappings(org_id=None, job_codes=["brand_new_role"])[0].is_jit

    repo.set_role_mappings(ORG, "brand_new_role", ["finance_operator"])

    (row,) = repo.list_role_mappings(org_id=None, job_codes=["brand_new_role"])
    assert row.origin == "manual"
    assert row.source_org is None
    assert row.role_key == "finance_operator"


def test_add_role_mapping_defaults_to_manual(repo: OrgRepo) -> None:
    """The admin POST path calls this without provenance arguments."""
    repo.add_role_mapping(ANY_ORG, "super_admin", "expert_user")

    (row,) = repo.list_role_mappings(org_id=None, job_codes=["super_admin"])
    assert (row.origin, row.source_org) == ("manual", None)


def test_provenance_survives_the_duplicate_no_op(repo: OrgRepo) -> None:
    """``ON CONFLICT DO NOTHING`` must not rewrite an existing row's origin."""
    provision_missing_mappings(repo, ORG, ["brand_new_role"])
    repo.add_role_mapping(ORG, "brand_new_role", "expert_user")

    rows = {
        r.role_key: r
        for r in repo.list_role_mappings(org_id=None, job_codes=["brand_new_role"])
    }
    for role_key in ("expert_user", "member"):
        row = rows[role_key]
        assert (row.origin, row.source_org) == ("jit", ORG)


def test_rows_that_predate_provenance_are_marked_legacy(tmp_path: Path) -> None:
    """Upgrading a pre-018 database must not claim those rows were reviewed.

    At migration time the rows include every JIT row created before ``origin``
    existed, and a post-hoc JIT row is indistinguishable from a hand-written
    one. The ALTER hands out ``'manual'``, so the upgrade has to correct it —
    once, on the startup that adds the column.
    """
    pool = SqlitePool(tmp_path / "legacy.db")
    # The pre-018 shape: no provenance, no tenant ownership on roles.
    with pool.transaction() as conn:
        conn.execute(
            "CREATE TABLE roles ("
            "  role_key TEXT PRIMARY KEY, name_zh TEXT NOT NULL,"
            "  permissions TEXT NOT NULL DEFAULT '[]',"
            "  is_default INTEGER NOT NULL DEFAULT 0,"
            "  is_system INTEGER NOT NULL DEFAULT 0,"
            "  description TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(
            "CREATE TABLE org_role_mappings ("
            "  org_id TEXT NOT NULL, job_code TEXT NOT NULL,"
            "  role_key TEXT NOT NULL REFERENCES roles(role_key) ON DELETE CASCADE,"
            "  PRIMARY KEY (org_id, job_code, role_key))"
        )
        conn.execute("INSERT INTO roles(role_key, name_zh) VALUES ('expert_user', '专家使用者')")
        conn.execute(
            "INSERT INTO org_role_mappings(org_id, job_code, role_key) "
            "VALUES ('*', 'super_admin', 'expert_user')"
        )

    run_migrations(pool)

    repo = OrgRepo(pool)
    (legacy,) = repo.list_role_mappings(org_id=None)
    assert legacy.origin == "legacy"
    assert legacy.source_org is None
    # ``created_at`` is stamped too, so the UI never renders 1970.
    assert legacy.created_at > 0

    # Migrations run on every startup: the marking must not repeat, or a row an
    # admin writes tomorrow would be relabelled 'legacy' on the next restart.
    repo.add_role_mapping(ANY_ORG, "finance_manager", "expert_user")
    run_migrations(pool)

    by_code = {row.job_code: row for row in repo.list_role_mappings(org_id=None)}
    assert by_code["super_admin"].origin == "legacy"
    assert by_code["finance_manager"].origin == "manual"
