"""Pending-users management face tests (T5-4).

Exercises the router functions directly against a migrated SQLite pool
(same style as test_knowledge_bases): the Depends-injected parameters are
passed by hand.
"""

from __future__ import annotations

import pytest

from octop.api.routers import pending_users
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.audit import AuditRepo
from octop.infra.db.repos.orgs import OrgRepo
from octop.infra.db.repos.sso import SsoRepo
from octop.infra.db.repos.users import UserRepo


@pytest.fixture
def db(tmp_path):
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


class _Repos:
    def __init__(self, pool) -> None:
        self.user_repo = UserRepo(pool)
        self.org_repo = OrgRepo(pool)
        self.sso_repo = SsoRepo(pool)
        self.audit_repo = AuditRepo(pool)


def _org(db, org_id: str, *, active: bool = True) -> None:
    OrgRepo(db).create_org(org_id=org_id, name=org_id)
    if not active:
        OrgRepo(db).update_org(org_id, status="disabled")


def _seed(db):
    repos = _Repos(db)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO sso_providers(enabled, display_name, issuer, client_id, idp_alias, created_at, updated_at)"
            " VALUES (1, 'Portal', 'https://portal.example', 'cid', 'portal', 0, 0)"
        )
    pid = repos.sso_repo.list()[0].id
    pending = repos.user_repo.create(
        username="p-user", password_hash=None, role="user",
        sso_provider_id=pid, sso_subject="s1",
    )
    landed = repos.user_repo.create(
        username="l-user", password_hash=None, role="user",
        sso_provider_id=pid, sso_subject="s2",
    )
    repos.user_repo.set_default_org_id(landed, "org-a")
    local = repos.user_repo.create(username="local", password_hash="h", role="user")
    return repos, pid, pending, landed, local


async def test_list_returns_only_pending_with_alias(db) -> None:
    _org(db, "org-a")
    repos, _, pending, landed, local = _seed(db)
    server = type("S", (), {"services": repos})()

    got = await pending_users.list_pending_users(_=None, server=server)

    assert [r["id"] for r in got] == [pending]
    assert got[0]["username"] == "p-user"
    assert got[0]["idp_alias"] == "portal"


async def test_assign_creates_membership_claims_default_org_and_bumps(db) -> None:
    _org(db, "org-a")
    repos, _, pending_uid, _, _ = _seed(db)
    server = type("S", (), {"services": repos})()

    result = await pending_users.assign_pending_user(
        pending_uid, pending_users.AssignBody(org_id="org-a"), _=None, server=server
    )

    assert result["default_org_claimed"] is True
    membership = repos.org_repo.get_membership(pending_uid, "org-a")
    assert membership is not None
    assert membership.status == "active"
    assert membership.source == "manual"
    assert repos.user_repo.get(pending_uid).default_org_id == "org-a"
    assert repos.user_repo.get(pending_uid).token_epoch == 1
    events = repos.audit_repo.query(action="user.assign_org", org_id="org-a", limit=5)
    assert len(events) == 1
    # Backlog view drops the user once default_org_id is claimed.
    assert await pending_users.list_pending_users(_=None, server=server) == []


async def test_assign_keeps_existing_source_and_is_idempotent(db) -> None:
    _org(db, "org-a")
    repos, pid, pending_uid, _, _ = _seed(db)
    repos.org_repo.upsert_membership(pending_uid, "org-a", role_keys=[], status="pending", source="jit")
    server = type("S", (), {"services": repos})()

    first = await pending_users.assign_pending_user(
        pending_uid, pending_users.AssignBody(org_id="org-a"), _=None, server=server
    )
    second = await pending_users.assign_pending_user(
        pending_uid, pending_users.AssignBody(org_id="org-a", role_keys=[]), _=None, server=server
    )

    membership = repos.org_repo.get_membership(pending_uid, "org-a")
    assert membership.status == "active"
    assert membership.source == "jit"  # existing source preserved (roles.py parity)
    assert first["default_org_claimed"] is True  # claimed on the first assign
    assert second["default_org_claimed"] is False  # idempotent afterwards


async def test_assign_rejects_missing_and_disabled_org(db) -> None:
    _org(db, "org-a")
    _org(db, "org-dead", active=False)
    repos, _, pending_uid, _, _ = _seed(db)
    server = type("S", (), {"services": repos})()

    with pytest.raises(pending_users.HTTPException) as ei:
        await pending_users.assign_pending_user(
            pending_uid, pending_users.AssignBody(org_id="org-x"), _=None, server=server
        )
    assert ei.value.status_code == 404

    # A disabled tenant must fail closed — get_org only sees active rows.
    with pytest.raises(pending_users.HTTPException) as ei:
        await pending_users.assign_pending_user(
            pending_uid, pending_users.AssignBody(org_id="org-dead"), _=None, server=server
        )
    assert ei.value.status_code == 404
    assert repos.org_repo.get_membership(pending_uid, "org-dead") is None


async def test_assign_rejects_unknown_ability_package(db) -> None:
    _org(db, "org-a")
    repos, _, pending_uid, _, _ = _seed(db)
    server = type("S", (), {"services": repos})()

    with pytest.raises(pending_users.HTTPException) as ei:
        await pending_users.assign_pending_user(
            pending_uid,
            pending_users.AssignBody(org_id="org-a", role_keys=["nope"]),
            _=None,
            server=server,
        )
    assert ei.value.status_code == 400
    assert repos.org_repo.get_membership(pending_uid, "org-a") is None
