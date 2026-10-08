"""Lifecycle reactions tests (T4-3): epoch bumps + pending trace.

The module under test is deliberately pluggable (one-line hooks, lazy
imports); these tests exercise the module functions directly against a
migrated SQLite pool, plus the org-level repo companion.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.audit import AuditRepo
from octop.infra.db.repos.orgs import OrgRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.users import lifecycle


@pytest.fixture
def db(tmp_path: Path):
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


class _Services:
    def __init__(self, pool) -> None:
        self.user_repo = UserRepo(pool)
        self.audit_repo = AuditRepo(pool)


def _seed_member(db, *, username: str, org_id: str | None) -> int:
    users = UserRepo(db)
    uid = users.create(username=username, password_hash="h", role="user")
    if org_id is not None:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO org_memberships(user_id, org_id, role_keys, status, is_primary, source, created_at, updated_at)"
                " VALUES (?, ?, '[]', 'active', 1, 'manual', 0, 0)",
                (uid, org_id),
            )
    return uid


def _org(db, org_id: str) -> None:
    OrgRepo(db).create_org(org_id=org_id, name=org_id)


def _sso_provider(db) -> int:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO sso_providers(enabled, display_name, issuer, client_id, created_at, updated_at)"
            " VALUES (1, 'Portal', 'https://portal.example', 'cid', 0, 0)"
        )
        row = conn.execute("SELECT MAX(id) AS id FROM sso_providers").fetchone()
        return int(row["id"] if isinstance(row, dict) else row[0])


def _sso_user(db, users: UserRepo, *, username: str, subject: str, provider_id: int) -> int:
    return users.create(
        username=username,
        password_hash=None,
        role="user",
        sso_provider_id=provider_id,
        sso_subject=subject,
    )


# ----- user-level bump -----


def test_bump_user_epoch_increments_and_audits(db) -> None:
    users = UserRepo(db)
    uid = users.create(username="u1", password_hash="h", role="user")
    assert users.get(uid).token_epoch == 0

    services = _Services(db)
    epoch = lifecycle.bump_user_epoch(services, uid, reason="user.disabled")

    assert epoch == 1
    assert users.get(uid).token_epoch == 1
    events = services.audit_repo.query(action="user.token_epoch_bump", limit=5)
    assert len(events) == 1
    assert "reason=user.disabled" in events[0].payload


# ----- org-level bump -----


def test_bump_org_epochs_hits_only_members(db) -> None:
    _org(db, "org-a")
    _org(db, "org-b")
    users = UserRepo(db)
    member_a = _seed_member(db, username="ma", org_id="org-a")
    member_b = _seed_member(db, username="mb", org_id="org-b")
    outside = users.create(username="outside", password_hash="h", role="user")

    services = _Services(db)
    count = lifecycle.bump_org_epochs(services, "org-a", reason="org.disabled")

    assert count == 1
    assert users.get(member_a).token_epoch == 1
    assert users.get(member_b).token_epoch == 0
    assert users.get(outside).token_epoch == 0
    events = services.audit_repo.query(action="org.token_epoch_bump", org_id="org-a", limit=5)
    assert len(events) == 1
    assert "users=1" in events[0].payload


def test_bump_org_epochs_on_empty_org_is_noop(db) -> None:
    _org(db, "org-empty")
    services = _Services(db)
    assert lifecycle.bump_org_epochs(services, "org-empty", reason="org.disabled") == 0


# ----- pending trace -----


def test_record_pending_resolution_writes_once(db) -> None:
    users = UserRepo(db)
    provider_id = _sso_provider(db)
    uid = _sso_user(db, users, username="pending-1", subject="s1", provider_id=provider_id)
    row = users.get(uid)
    services = _Services(db)

    lifecycle.record_pending_resolution(services, row, idp_alias="portal")
    lifecycle.record_pending_resolution(services, row, idp_alias="portal")
    lifecycle.record_pending_resolution(services, row, idp_alias="portal")

    events = services.audit_repo.query(action=lifecycle.PENDING_AUDIT_ACTION, limit=10)
    assert len(events) == 1
    assert events[0].actor == "pending-1"
    assert events[0].org_id is None
    assert "idp=portal" in events[0].payload


def test_list_pending_sso_users_derivation(db) -> None:
    users = UserRepo(db)
    _org(db, "org-a")
    provider_id = _sso_provider(db)
    pending = _sso_user(db, users, username="sso-pending", subject="s1", provider_id=provider_id)
    landed = _sso_user(db, users, username="sso-landed", subject="s2", provider_id=provider_id)
    users.set_default_org_id(landed, "org-a")
    local = users.create(username="local", password_hash="h", role="user")
    disabled_sso = _sso_user(db, users, username="sso-off", subject="s3", provider_id=provider_id)
    users.set_disabled(disabled_sso, True)

    got = lifecycle.list_pending_sso_users(_Services(db))
    ids = [r.id for r in got]

    assert pending in ids
    assert landed not in ids
    assert local not in ids  # local users never enter the resolution chain
    assert disabled_sso not in ids


# ----- repo companion -----


def test_bump_token_epoch_for_org_returns_member_count(db) -> None:
    _org(db, "org-a")
    users = UserRepo(db)
    _seed_member(db, username="x1", org_id="org-a")
    _seed_member(db, username="x2", org_id="org-a")
    repo = UserRepo(db)
    assert repo.bump_token_epoch_for_org("org-a") == 2
    assert repo.bump_token_epoch_for_org("org-none") == 0


# ----- hook wiring (lazy imports inside the routers must resolve) -----


def test_hooks_are_lazy_importable() -> None:
    from octop.infra.users.lifecycle import (  # noqa: F401
        bump_org_epochs,
        bump_user_epoch,
        record_pending_resolution,
    )
