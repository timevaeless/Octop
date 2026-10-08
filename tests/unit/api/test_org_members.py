"""Org-member management face tests (T4-2).

Exercises the router functions directly against a migrated SQLite pool
(same style as test_pending_users): the Depends-injected parameters are
passed by hand. The gate matrix is the point — org_admin scoping must be
proven, not assumed.
"""

from __future__ import annotations

import pytest

from octop.api.routers import org_members
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.audit import AuditRepo
from octop.infra.db.repos.orgs import OrgRepo
from octop.infra.db.repos.users import UserRepo


class _Repos:
    def __init__(self, pool) -> None:
        self.user_repo = UserRepo(pool)
        self.org_repo = OrgRepo(pool)
        self.audit_repo = AuditRepo(pool)


class _StubManager:
    """Mimics UserManager.disable/enable for router-level tests."""

    def __init__(self, repos: _Repos) -> None:
        self._repos = repos

    async def disable(self, username: str) -> None:
        row = self._repos.user_repo.get_by_username(username)
        assert row is not None
        self._repos.user_repo.set_disabled(row.id, True)

    async def enable(self, username: str) -> None:
        row = self._repos.user_repo.get_by_username(username)
        assert row is not None
        self._repos.user_repo.set_disabled(row.id, False)


@pytest.fixture
def db(tmp_path):
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


class _User:
    """Just the fields the router reads off the token-resolved caller."""

    def __init__(self, uid: int, username: str, *, is_admin: bool = False) -> None:
        self.id = uid
        self.username = username
        self.is_admin = is_admin


def _seed(db):
    repos = _Repos(db)
    org_repo = repos.org_repo
    org_repo.create_org(org_id="org-a", name="Org A")
    # ``org_admin`` is seeded by the migrations with is_system=1; the
    # ON-CONFLICT-DO-NOTHING insert below leaves that row untouched.
    org_repo.create_role(role_key="org_admin", name_zh="租户管理员")
    org_repo.create_role(role_key="expert_user", name_zh="专家使用者", is_default=True)
    org_repo.create_role(role_key="knowledge_editor", name_zh="知识库维护")
    # A regular platform package: visible to, and grantable by, the tenant.
    org_repo.create_role(role_key="platform_admin", name_zh="平台管理员")

    admin = repos.user_repo.create(username="plat-admin", password_hash="h", role="admin")
    boss = repos.user_repo.create(username="boss", password_hash="h", role="user")
    peer = repos.user_repo.create(username="peer", password_hash="h", role="user")
    plain = repos.user_repo.create(username="plain", password_hash="h", role="user")

    org_repo.upsert_membership(boss, "org-a", role_keys=["org_admin"], status="active", source="manual")
    org_repo.upsert_membership(peer, "org-a", role_keys=["expert_user"], status="active", source="jit")
    org_repo.upsert_membership(plain, "org-a", role_keys=[], status="active", source="jit")

    repos.user_repo.set_default_org_id(boss, "org-a")
    server = type(
        "S", (), {"services": repos, "user_manager": _StubManager(repos)}
    )()
    return repos, server, {
        "admin": admin,
        "boss": boss,
        "peer": peer,
        "plain": plain,
    }


async def test_gate_matrix(db) -> None:
    repos, server, ids = _seed(db)

    # org_admin sees the roster of their own org.
    roster = await org_members.list_org_members(
        "org-a", user=_User(ids["boss"], "boss"), server=server
    )
    assert {m["username"] for m in roster} == {"boss", "peer", "plain"}

    # A plain member does not; a stranger (no membership) does not either.
    with pytest.raises(org_members.HTTPException) as ei:
        await org_members.list_org_members("org-a", user=_User(ids["peer"], "peer"), server=server)
    assert ei.value.status_code == 403
    with pytest.raises(org_members.HTTPException) as ei:
        await org_members.list_org_members("org-a", user=_User(99999, "ghost"), server=server)
    assert ei.value.status_code == 403

    # Platform admin passes the gate without a membership.
    roster = await org_members.list_org_members(
        "org-a", user=_User(ids["admin"], "plat-admin", is_admin=True), server=server
    )
    assert len(roster) == 3

    # Missing org fails closed with 404 (even for the platform admin).
    with pytest.raises(org_members.HTTPException) as ei:
        await org_members.list_org_members(
            "org-x", user=_User(ids["admin"], "plat-admin", is_admin=True), server=server
        )
    assert ei.value.status_code == 404


async def test_set_roles_grants_and_revokes_with_epoch_bump(db) -> None:
    repos, server, ids = _seed(db)
    epoch_before = repos.user_repo.get(ids["peer"]).token_epoch

    got = await org_members.set_member_roles(
        "org-a",
        ids["peer"],
        org_members.RolesBody(role_keys=["expert_user", "knowledge_editor"]),
        user=_User(ids["boss"], "boss"),
        server=server,
    )
    assert got["role_keys"] == ["expert_user", "knowledge_editor"]
    membership = repos.org_repo.get_membership(ids["peer"], "org-a")
    assert membership.role_keys == ["expert_user", "knowledge_editor"]
    assert membership.source == "jit"  # preserved on update
    assert repos.user_repo.get(ids["peer"]).token_epoch == epoch_before + 1
    events = repos.audit_repo.query(action="org.member_roles_updated", org_id="org-a", limit=5)
    assert len(events) == 1


async def test_set_roles_rejects_system_and_unknown_keys(db) -> None:
    repos, server, ids = _seed(db)

    # The system ``org_admin`` package is not grantable: designating tenant
    # admins is the platform admin's job, and the is_system row is the marker.
    with pytest.raises(org_members.HTTPException) as ei:
        await org_members.set_member_roles(
            "org-a",
            ids["peer"],
            org_members.RolesBody(role_keys=["org_admin"]),
            user=_User(ids["boss"], "boss"),
            server=server,
        )
    assert ei.value.status_code == 400
    with pytest.raises(org_members.HTTPException) as ei:
        await org_members.set_member_roles(
            "org-a",
            ids["peer"],
            org_members.RolesBody(role_keys=["nope"]),
            user=_User(ids["boss"], "boss"),
            server=server,
        )
    assert ei.value.status_code == 400
    assert repos.org_repo.get_membership(ids["peer"], "org-a").role_keys == ["expert_user"]
    # …while a regular platform package is grantable by the tenant admin.
    got = await org_members.set_member_roles(
        "org-a",
        ids["peer"],
        org_members.RolesBody(role_keys=["platform_admin"]),
        user=_User(ids["boss"], "boss"),
        server=server,
    )
    assert got["role_keys"] == ["platform_admin"]


async def test_sole_org_admin_cannot_be_demoted_or_suspended(db) -> None:
    repos, server, ids = _seed(db)

    # A tenant admin cannot strip system packages at all: the PUT passes the
    # held ``org_admin`` through untouched, so the sole-org-admin guard never
    # even fires on the tenant-admin path.
    got = await org_members.set_member_roles(
        "org-a",
        ids["boss"],
        org_members.RolesBody(role_keys=["expert_user"]),
        user=_User(ids["boss"], "boss"),
        server=server,
    )
    assert got["role_keys"] == ["expert_user", "org_admin"]
    assert repos.org_repo.get_membership(ids["boss"], "org-a").role_keys == [
        "expert_user",
        "org_admin",
    ]
    with pytest.raises(org_members.HTTPException) as ei:
        await org_members.suspend_member(
            "org-a", ids["boss"], user=_User(ids["boss"], "boss"), server=server
        )
    assert ei.value.status_code == 409

    # The platform admin CAN strip the designation — and the sole-org-admin
    # guard fires when that would leave the tenant with none.
    with pytest.raises(org_members.HTTPException) as ei:
        await org_members.set_member_roles(
            "org-a",
            ids["boss"],
            org_members.RolesBody(role_keys=["expert_user"]),
            user=_User(ids["admin"], "plat-admin", is_admin=True),
            server=server,
        )
    assert ei.value.status_code == 409
    # …but a second org_admin makes the demotion legal.
    repos.org_repo.upsert_membership(
        ids["plain"], "org-a", role_keys=["org_admin"], status="active", source="manual"
    )
    await org_members.set_member_roles(
        "org-a",
        ids["boss"],
        org_members.RolesBody(role_keys=["expert_user"]),
        user=_User(ids["admin"], "plat-admin", is_admin=True),
        server=server,
    )
    assert repos.org_repo.get_membership(ids["boss"], "org-a").role_keys == ["expert_user"]


async def test_set_roles_passes_through_held_system_packages(db) -> None:
    """A wholesale PUT keeping held system packages neither 400s nor strips them.

    The roster edit modal pre-fills ``member``/``org_admin`` for designated
    users; saving that form is a plain grant/revoke of the *grantable* keys.
    The designation is platform property: it must survive the write on the
    membership AND stay out of per-user grants (a grant residue would
    resurrect it on JIT re-expansion after a platform-side revocation).
    """
    repos, server, ids = _seed(db)
    repos.org_repo.upsert_membership(
        ids["peer"],
        "org-a",
        role_keys=["org_admin", "expert_user"],
        status="active",
        source="manual",
    )

    got = await org_members.set_member_roles(
        "org-a",
        ids["peer"],
        org_members.RolesBody(role_keys=["knowledge_editor"]),
        user=_User(ids["boss"], "boss"),
        server=server,
    )
    assert got["role_keys"] == ["knowledge_editor", "org_admin"]
    membership = repos.org_repo.get_membership(ids["peer"], "org-a")
    assert membership.role_keys == ["knowledge_editor", "org_admin"]
    # The designation MUST ride in per-user grants: JIT re-expansion unions
    # mappings ∪ grants, so a designation in neither would be wiped on the
    # member's next SSO login (an org_admin editing their own packages would
    # lose it). Wholesale replace keeps a platform-side revocation
    # authoritative — the residue gets overwritten, not resurrected.
    assert repos.org_repo.list_member_grants(ids["peer"], "org-a") == [
        "knowledge_editor",
        "org_admin",
    ]


async def test_refs_returns_visible_pool_with_system_flag(db) -> None:
    """refs ships the FULL visible pool (system rows flagged) for labels.

    The roster must render existing memberships like ``member``/``org_admin``
    with their Chinese names, so refs cannot drop them (grantable-only did).
    Granting stays restricted: the client picker filters ``is_system`` and the
    write path re-validates against the grantable set server-side.
    """
    repos, server, ids = _seed(db)

    got = await org_members.org_refs(
        "org-a", user=_User(ids["boss"], "boss"), server=server
    )
    assert got["org"] == {"org_id": "org-a", "name": "Org A", "status": "active"}
    by_key = {r["role_key"]: r for r in got["roles"]}
    # System platform packages ride along, flagged, with display names.
    assert by_key["org_admin"]["is_system"] is True
    assert by_key["org_admin"]["name_zh"] == "租户管理员"
    assert by_key["member"]["is_system"] is True
    # Regular platform packages: visible and grantable.
    assert by_key["expert_user"]["is_system"] is False
    assert by_key["expert_user"]["is_default"] is True
    # The picker set (client-side !is_system) is exactly the grantable pool.
    picker_keys = {k for k, r in by_key.items() if not r["is_system"]}
    assert picker_keys == {"expert_user", "knowledge_editor", "platform_admin"}


async def test_suspend_reactivate_uses_account_flag(db) -> None:
    repos, server, ids = _seed(db)

    got = await org_members.suspend_member(
        "org-a", ids["peer"], user=_User(ids["boss"], "boss"), server=server
    )
    assert got["disabled"] is True
    assert repos.user_repo.get(ids["peer"]).disabled == 1
    events = repos.audit_repo.query(action="org.member_suspended", org_id="org-a", limit=5)
    assert len(events) == 1

    await org_members.reactivate_member(
        "org-a", ids["peer"], user=_User(ids["boss"], "boss"), server=server
    )
    assert repos.user_repo.get(ids["peer"]).disabled == 0

    # Idempotent repeat is a no-op (no second audit event).
    await org_members.reactivate_member(
        "org-a", ids["peer"], user=_User(ids["boss"], "boss"), server=server
    )
    assert len(repos.audit_repo.query(action="org.member_suspended", org_id="org-a", limit=5)) == 1

    # Platform admins are out of tenant purview.
    with pytest.raises(org_members.HTTPException) as ei:
        await org_members.suspend_member(
            "org-a", ids["admin"], user=_User(ids["boss"], "boss"), server=server
        )
    assert ei.value.status_code == 403


async def test_update_role_cannot_flip_system_is_default(db) -> None:
    """P1 guard: the system packages' is_default is the JIT machinery itself.

    ``member`` must stay the is_default fallback (else first logins get no
    package) and ``org_admin`` must never auto-provision. Display fields
    stay editable; deleting the row was already 403.
    """
    from octop.api.routers import roles as roles_router

    repos, server, ids = _seed(db)

    with pytest.raises(roles_router.HTTPException) as ei:
        await roles_router.update_role(
            "member", roles_router.UpdateRoleBody(is_default=False), None, server
        )
    assert ei.value.status_code == 400
    with pytest.raises(roles_router.HTTPException) as ei:
        await roles_router.update_role(
            "org_admin", roles_router.UpdateRoleBody(is_default=True), None, server
        )
    assert ei.value.status_code == 400
    assert repos.org_repo.get_role("member").is_default is True
    assert repos.org_repo.get_role("org_admin").is_default is False

    # Display fields stay editable, and re-sending the same is_default is fine.
    got = await roles_router.update_role(
        "member",
        roles_router.UpdateRoleBody(name_zh="成员", is_default=True),
        None,
        server,
    )
    assert got["name_zh"] == "成员"
    assert got["is_default"] is True
    assert got["is_system"] is True


async def test_deleted_package_bumps_holders_and_prunes_password_hydration(db) -> None:
    """Review P1-3: a package delete must converge the stale key immediately.

    The membership JSON survives the FK cascade, so (a) holders get an epoch
    bump at delete time (their rls claim dies on the next request) and
    (b) the password-login hydration prunes invisible keys instead of
    re-minting them into a fresh claim (SSO's JIT pass already did).
    """
    from octop.infra.db.repos.tenant_roles import TenantRoleRepo
    from octop.infra.users.tenant_hydration import active_role_keys_for

    repos, server, ids = _seed(db)
    org_repo = repos.org_repo
    trepo = TenantRoleRepo(db)
    view = trepo.create_tenant_role("org-a", bare_key="retail_l1", name_zh="零售审批一级岗")
    org_repo.upsert_membership(
        ids["peer"],
        "org-a",
        role_keys=["expert_user", view.role_key],
        status="active",
        source="jit",
    )
    repos.user_repo.set_default_org_id(ids["peer"], "org-a")
    peer_epoch = repos.user_repo.get(ids["peer"]).token_epoch
    boss_epoch = repos.user_repo.get(ids["boss"]).token_epoch

    assert trepo.delete_tenant_role("org-a", view.role_key) is True
    assert repos.user_repo.bump_token_epoch_for_role_key("org-a", view.role_key) == 1
    assert repos.user_repo.get(ids["peer"]).token_epoch == peer_epoch + 1
    # boss does not hold the key — untouched. Substring keys don't false-hit
    # ("user" inside "expert_user" is not a quoted-token match).
    assert repos.user_repo.get(ids["boss"]).token_epoch == boss_epoch
    assert repos.user_repo.bump_token_epoch_for_role_key("org-a", "user") == 0

    # Password-login hydration: the membership JSON still carries the ghost
    # key, but the fresh claim must not.
    keys = active_role_keys_for(repos, ids["peer"], "org-a")
    assert keys == ["expert_user"]
