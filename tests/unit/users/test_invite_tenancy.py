"""T4-1: invite → tenant binding (octop.infra.users.invite_tenancy)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos._base import now_ts
from octop.infra.db.repos.invites import InviteRepo, invite_status_payload
from octop.infra.db.repos.orgs import OrgRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.errors import OctopError
from octop.infra.users.invite_tenancy import validate_invite_org
from octop.infra.users.invites import DEFAULT_EXPIRES_DAYS
from octop.infra.users.password import hash_password

EXPIRES = now_ts() + DEFAULT_EXPIRES_DAYS * 86400


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@dataclass
class _Seed:
    users: UserRepo
    invites: InviteRepo
    orgs: OrgRepo
    admin_id: int

    def make_invite(self, code: str, org_id: str | None = None):
        return self.invites.create(
            code=code,
            created_by=self.admin_id,
            expires_at=EXPIRES,
            note="t4-1",
            org_id=org_id,
        )

    def redeem(self, code: str, username: str):
        return self.invites.redeem_creating_user(
            code=code,
            username=username,
            password_hash=hash_password("NewbiePass12"),
            display_name=None,
            locale="zh",
        )


@pytest.fixture
def seed(db: SqlitePool) -> _Seed:
    users = UserRepo(db)
    admin_id = users.create(
        username="admin",
        password_hash=hash_password("AdminPass12"),
        role="admin",
    )
    return _Seed(users, InviteRepo(db), OrgRepo(db), admin_id)


def test_invite_with_org_lands_membership(seed: _Seed) -> None:
    seed.orgs.create_org(org_id="acme", name="Acme")
    invite = seed.make_invite("InviteOrg01", org_id="acme")
    assert invite_status_payload(invite)["org_id"] == "acme"

    uid, used = seed.redeem("InviteOrg01", "newbie")
    assert used.org_id == "acme"

    membership = seed.orgs.get_membership(uid, "acme")
    assert membership is not None
    assert membership.source == "invite"
    assert membership.status == "active"
    assert membership.role_keys == []

    user = seed.users.get_by_username("newbie")
    assert user is not None
    assert user.default_org_id == "acme"


def test_invite_without_org_is_tenant_less(seed: _Seed) -> None:
    seed.make_invite("PlainInvite")
    uid, used = seed.redeem("PlainInvite", "plain")
    assert used.org_id is None
    assert seed.orgs.get_membership(uid, "acme") is None
    user = seed.users.get_by_username("plain")
    assert user is not None
    assert user.default_org_id is None


def test_redeem_fails_closed_when_org_disabled(seed: _Seed) -> None:
    seed.orgs.create_org(org_id="gone", name="Gone")
    seed.make_invite("StaleInvite", org_id="gone")
    seed.orgs.update_org("gone", status="disabled")

    with pytest.raises(OctopError) as excinfo:
        seed.redeem("StaleInvite", "stranded")
    assert excinfo.value.status == 409

    # Whole redemption rolled back: no user, invite still redeemable.
    assert seed.users.get_by_username("stranded") is None
    still = seed.invites.get_by_code("StaleInvite")
    assert still is not None
    assert still.status() == "pending"


def test_validate_invite_org(seed: _Seed) -> None:
    services = SimpleNamespace(org_repo=seed.orgs)
    seed.orgs.create_org(org_id="acme", name="Acme")
    assert validate_invite_org(services, " acme ") == "acme"
    assert validate_invite_org(services, None) is None
    assert validate_invite_org(services, "") is None

    with pytest.raises(OctopError) as excinfo:
        validate_invite_org(services, "nope")
    assert excinfo.value.status == 404

    seed.orgs.update_org("acme", status="disabled")
    with pytest.raises(OctopError) as excinfo:
        validate_invite_org(services, "acme")
    assert excinfo.value.status == 404
