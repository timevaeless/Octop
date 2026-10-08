"""028 member grants: admin-granted packages survive the JIT re-expansion.

The regression this pins: before 028, every SSO login re-expanded
``org_memberships.role_keys`` from ``org_role_mappings`` and replaced them
wholesale, so a grant made through the member roster (or the user-membership
face, or pending assign) was silently wiped on the member's next login.

Semantics under test (agreed 2026-09-27):

* effective keys = mapped ∪ granted — grants are additive, never wiped;
* an admin uncheck of a *granted* key revokes it (the grant row goes);
* mapping-derived keys stay mapping-revocable — deleting the mapping row
  drops them from the effective set even though the grant table is empty;
* grants cascade away with their org (FK is observable on SQLite).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.orgs import OrgRepo
from octop.infra.users.tenant_hydration import apply_jit_resolution

ORG = "cheryfs"
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
        conn.execute("INSERT INTO users(username, role, created_at) VALUES ('u1', 'user', 0)")
    return pool


@pytest.fixture
def repo(db: SqlitePool) -> OrgRepo:
    repo = OrgRepo(db)
    repo.create_role(role_key="expert_user", name_zh="专家使用者", is_default=True)
    repo.create_role(role_key="org_admin", name_zh="租户管理员")
    repo.create_binding(idp_alias=ALIAS, match_type="claim", match_value="1", org_id=ORG)
    repo.add_role_mapping(org_id=ORG, job_code="customer_manager", role_key="expert_user")
    return repo


class _FakeUserRepo:
    """Just enough of the real repo for ``apply_jit_resolution``."""

    def __init__(self, db: SqlitePool) -> None:
        self._db = db
        self.bumped: list[int] = []

    def get(self, user_id: int):
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT id, username, default_org_id FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return None
        return SimpleNamespace(id=row["id"], username=row["username"], default_org_id=row["default_org_id"])

    def set_default_org_id(self, user_id: int, org_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE users SET default_org_id = ? WHERE id = ?", (org_id, user_id)
            )

    def bump_token_epoch(self, user_id: int) -> None:
        self.bumped.append(user_id)


class _FakeAuditRepo:
    def __init__(self) -> None:
        self.writes: list[dict] = []

    def write(self, **kwargs: object) -> None:
        self.writes.append(kwargs)


def _services(db: SqlitePool, repo: OrgRepo) -> SimpleNamespace:
    return SimpleNamespace(
        org_repo=repo,
        sso_repo=SimpleNamespace(get_by_id=lambda _pid: SimpleNamespace(idp_alias=ALIAS)),
        user_repo=_FakeUserRepo(db),
        audit_repo=_FakeAuditRepo(),
    )


CLAIMS = {"org_ref": "1", "job_code": "customer_manager"}


def test_grant_survives_the_next_login(db: SqlitePool, repo: OrgRepo) -> None:
    """The regression: member-roster grants must not be wiped by JIT."""
    services = _services(db, repo)
    # First login: mapped keys only, membership created.
    apply_jit_resolution(services, user_id=1, provider_id=1, claims=CLAIMS)
    assert repo.get_membership(1, ORG).role_keys == ["expert_user"]

    # Admin grants the tenant-admin package through the roster face.
    repo.replace_member_grants(1, ORG, ["expert_user", "org_admin"], granted_by="admin")

    # Second login: the grant survives the re-expansion.
    apply_jit_resolution(services, user_id=1, provider_id=1, claims=CLAIMS)
    assert repo.get_membership(1, ORG).role_keys == ["expert_user", "org_admin"]


def test_granted_only_key_is_kept_even_without_any_mapping(
    db: SqlitePool, repo: OrgRepo
) -> None:
    services = _services(db, repo)
    repo.replace_member_grants(1, ORG, ["org_admin"], granted_by="admin")
    apply_jit_resolution(services, user_id=1, provider_id=1, claims=CLAIMS)
    membership = repo.get_membership(1, ORG)
    # Mapped key first, then the grant — union order is stable.
    assert membership.role_keys == ["expert_user", "org_admin"]
    assert membership.status == "active"


def test_unchecking_a_granted_key_revokes_it_on_next_login(
    db: SqlitePool, repo: OrgRepo
) -> None:
    services = _services(db, repo)
    repo.replace_member_grants(1, ORG, ["org_admin"], granted_by="admin")
    apply_jit_resolution(services, user_id=1, provider_id=1, claims=CLAIMS)
    assert "org_admin" in repo.get_membership(1, ORG).role_keys

    # Admin unchecks the granted package → grant row deleted.
    repo.replace_member_grants(1, ORG, [], granted_by="admin")
    apply_jit_resolution(services, user_id=1, provider_id=1, claims=CLAIMS)
    assert repo.get_membership(1, ORG).role_keys == ["expert_user"]


def test_mapped_keys_are_independent_of_grants(db: SqlitePool, repo: OrgRepo) -> None:
    """Mapped keys flow from the mapping table, grants from the grant table.

    Note (pre-existing 018 behaviour, not ours): deleting the whole mapping
    row for a code does not permanently revoke it — the next login of a user
    carrying that code re-provisions it to the ``is_default`` package
    (``provision_missing_mappings``). With the grant set empty the union can
    only ever contain mapped keys, never ``org_admin``.
    """
    services = _services(db, repo)
    repo.replace_member_grants(1, ORG, [], granted_by="admin")
    apply_jit_resolution(services, user_id=1, provider_id=1, claims=CLAIMS)
    assert repo.get_membership(1, ORG).role_keys == ["expert_user"]
    assert "org_admin" not in repo.get_membership(1, ORG).role_keys


def test_replace_member_grants_replaces_wholesale_and_dedupes(repo: OrgRepo) -> None:
    repo.replace_member_grants(1, ORG, ["org_admin"], granted_by="a1")
    repo.replace_member_grants(1, ORG, ["org_admin", "org_admin", "expert_user"], granted_by="a2")
    # Same-transaction rows share one created_at, so ordering falls back to
    # the role key — the set is what matters, not the sequence.
    assert sorted(repo.list_member_grants(1, ORG)) == ["expert_user", "org_admin"]

    repo.replace_member_grants(1, ORG, [], granted_by="a2")
    assert repo.list_member_grants(1, ORG) == []


def test_grants_do_not_leak_across_tenants(db: SqlitePool, repo: OrgRepo) -> None:
    repo.create_org(org_id="other", name="另一家", kind="partner")
    repo.replace_member_grants(1, ORG, ["org_admin"], granted_by="admin")
    assert repo.list_member_grants(1, "other") == []


def test_grants_cascade_with_the_user(db: SqlitePool, repo: OrgRepo) -> None:
    """The FK is observable: deleting the user takes the grants along."""
    repo.replace_member_grants(1, ORG, ["org_admin"], granted_by="admin")
    assert repo.list_member_grants(1, ORG) == ["org_admin"]
    with db.transaction() as conn:
        conn.execute("DELETE FROM users WHERE id = 1")
    assert repo.list_member_grants(1, ORG) == []


def test_login_bumps_epoch_when_grant_changes_the_visible_set(
    db: SqlitePool, repo: OrgRepo
) -> None:
    """Epoch bump still fires when the union changes the effective keys."""
    services = _services(db, repo)
    apply_jit_resolution(services, user_id=1, provider_id=1, claims=CLAIMS)
    services.user_repo.bumped.clear()

    repo.replace_member_grants(1, ORG, ["org_admin"], granted_by="admin")
    apply_jit_resolution(services, user_id=1, provider_id=1, claims=CLAIMS)
    assert services.user_repo.bumped == [1]

    # An unchanged effective set must not bump again.
    services.user_repo.bumped.clear()
    apply_jit_resolution(services, user_id=1, provider_id=1, claims=CLAIMS)
    assert services.user_repo.bumped == []
