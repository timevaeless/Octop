# tests/unit/db/test_agent_is_shared.py
from __future__ import annotations

from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.users import UserRepo


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


def test_is_shared_default_zero_and_list_shared(db):
    run_migrations(db)  # or use fixture that already migrated
    UserRepo(db).create(username="u1", password_hash="h", role="user")
    UserRepo(db).create(username="u2", password_hash="h", role="user")
    repo = AgentRepo(db)
    repo.create(agent_id="a1", user_id=1, name="mine")
    repo.create(agent_id="a2", user_id=2, name="theirs")
    repo.set_shared("a2", True)
    rows = repo.list_shared(exclude_user_id=1)
    assert [r.agent_id for r in rows] == ["a2"]
    assert rows[0].is_shared == 1


# ----------------------------- shared-agent tenancy (2026-09-27 attribution)


def test_create_stamps_org_and_share_scopes_to_it(db):
    UserRepo(db).create(username="o", password_hash="h", role="user")
    UserRepo(db).create(username="p", password_hash="h", role="user")
    repo = AgentRepo(db)
    repo.create(agent_id="a1", user_id=1, name="mine", org_id="cheryfs")
    assert repo.get("a1").org_id == "cheryfs"

    repo.set_shared("a1", True)
    # Viewer = the same-tenant peer (user 2), not the owner.
    same_tenant = repo.list_shared(exclude_user_id=2, org_id="cheryfs", role_keys=[])
    other_tenant = repo.list_shared(exclude_user_id=2, org_id="chery-dealer-001", role_keys=[])
    assert [r.agent_id for r in same_tenant] == ["a1"]
    assert other_tenant == []


def test_share_no_longer_stamps_a_legacy_orgless_agent(db):
    """2026-09-27: sharing is attribution-neutral.

    An org-less (unattributed) row keeps ``org_id=NULL`` when shared — under
    the three-state semantics that makes it admin-reachable only. Re-stamping
    from the sharer's tenant would silently assign an admin draft into a
    tenant behind the admin's back. Attribution changes go through
    ``set_org_id`` on the resource-acl face.
    """
    UserRepo(db).create(username="o", password_hash="h", role="user")
    UserRepo(db).create(username="v", password_hash="h", role="user")
    repo = AgentRepo(db)
    repo.create(agent_id="a1", user_id=1, name="legacy")  # org_id NULL
    repo.set_shared("a1", True)

    assert repo.get("a1").org_id is None
    # Tenants never see it (fail-closed)…
    assert repo.list_shared(exclude_user_id=2, org_id="cheryfs", role_keys=[]) == []
    # …and the platform-admin listing path (role_keys=None) reaches it.
    assert repo.list_shared(exclude_user_id=2, org_id=None, role_keys=None) != []


def test_set_org_id_assigns_attribution(db):
    """The resource-acl face: assign / re-assign / unassign (None) / platform."""
    UserRepo(db).create(username="o", password_hash="h", role="user")
    repo = AgentRepo(db)
    repo.create(agent_id="a1", user_id=1, name="draft")  # admin draft: org NULL
    repo.set_shared("a1", True)
    assert repo.list_shared(exclude_user_id=2, org_id="cheryfs", role_keys=[]) == []

    repo.set_org_id("a1", "cheryfs")
    assert [r.agent_id for r in repo.list_shared(exclude_user_id=2, org_id="cheryfs", role_keys=[])] == ["a1"]
    assert repo.list_shared(exclude_user_id=2, org_id="chery-dealer-001", role_keys=[]) == []

    repo.set_org_id("a1", "*")
    assert [r.agent_id for r in repo.list_shared(exclude_user_id=2, org_id="chery-dealer-001", role_keys=[])] == ["a1"]

    repo.set_org_id("a1", None)
    assert repo.list_shared(exclude_user_id=2, org_id="cheryfs", role_keys=[]) == []


def test_unshare_keeps_the_org_stamp_as_provenance(db):
    UserRepo(db).create(username="o", password_hash="h", role="user")
    repo = AgentRepo(db)
    repo.create(agent_id="a1", user_id=1, name="mine", org_id="cheryfs")
    repo.set_shared("a1", True)
    repo.set_shared("a1", False)
    assert repo.get("a1").org_id == "cheryfs"
    assert repo.get("a1").is_shared == 0
