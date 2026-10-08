"""Tenant-admin router tests (``routers/org_admin.py``, 2026-09-30 split).

Exercises the router functions directly against a migrated SQLite pool (same
style as test_org_members): the Depends-injected parameters are passed by
hand. The point of the file is the **isolation matrix** — cross-tenant
reads/writes must 403/404 without leaking existence, ``is_system`` rows must
be untouchable, and no endpoint accepts an ``org_id`` from the client.
"""

from __future__ import annotations

import pytest

from octop.api.routers import org_admin
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.audit import AuditRepo
from octop.infra.db.repos.connectors import ConnectorRepo
from octop.infra.db.repos.knowledge import KnowledgeRepo
from octop.infra.db.repos.orgs import OrgRepo
from octop.infra.db.repos.published_experts import PublishedExpertRepo
from octop.infra.db.repos.users import UserRepo

ORG = "org-a"
OTHER_ORG = "org-b"
T_PREFIX = "t_org-a_"


class _Repos:
    def __init__(self, pool) -> None:
        self.db = pool
        self.user_repo = UserRepo(pool)
        self.org_repo = OrgRepo(pool)
        self.audit_repo = AuditRepo(pool)
        self.agent_repo = AgentRepo(pool)
        self.connector_repo = ConnectorRepo(pool)
        self.knowledge_repo = KnowledgeRepo(pool)
        self.published_expert_repo = PublishedExpertRepo(pool)


class _User:
    def __init__(self, uid: int, username: str, *, is_admin: bool = False) -> None:
        self.id = uid
        self.username = username
        self.is_admin = is_admin


@pytest.fixture
def db(tmp_path):
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@pytest.fixture
def env(db):
    repos = _Repos(db)
    repos.org_repo.create_org(org_id=ORG, name="Org A")
    repos.org_repo.create_org(org_id=OTHER_ORG, name="Org B")
    boss = repos.user_repo.create(username="boss", password_hash="h", role="user")
    repos.org_repo.upsert_membership(
        boss, ORG, role_keys=["org_admin"], status="active", source="manual"
    )
    repos.org_repo.upsert_membership(
        boss, OTHER_ORG, role_keys=[], status="active", source="jit"
    )
    server = type("S", (), {"services": repos})()
    return repos, server, {"boss": boss}


def _tenant_role(storage_key: str, name: str, org_id: str = ORG, **kw):
    from octop.infra.db.repos.orgs import OrgRepo

    return OrgRepo(_DB).create_role(role_key=storage_key, name_zh=name, org_id=org_id, **kw)


_DB = None


@pytest.fixture(autouse=True)
def _capture_db(db):
    global _DB
    _DB = db


# ----- packages -----


async def test_package_crud_is_scoped_to_the_path_org(env) -> None:
    repos, server, ids = env
    boss = _User(ids["boss"], "boss")

    created = await org_admin.create_tenant_role(
        ORG,
        org_admin.TenantRoleBody(bare_key="editor", name_zh="编辑"),
        user=boss,
        server=server,
    )
    assert created["role_key"] == T_PREFIX + "editor"
    assert created["org_id"] == ORG
    assert created["is_system"] is False

    listed = await org_admin.list_tenant_roles(ORG, user=boss, server=server)
    keys = {r["role_key"] for r in listed}
    assert keys == {"org_admin", "member", T_PREFIX + "editor"}

    updated = await org_admin.update_tenant_role(
        ORG,
        T_PREFIX + "editor",
        org_admin.TenantRoleUpdateBody(name_zh="编辑者"),
        user=boss,
        server=server,
    )
    assert updated["name_zh"] == "编辑者"

    await org_admin.delete_tenant_role(
        ORG, T_PREFIX + "editor", user=boss, server=server
    )
    assert repos.org_repo.get_role(T_PREFIX + "editor") is None
    events = repos.audit_repo.query(action="role.create", org_id=ORG, limit=5)
    assert len(events) == 1


async def test_package_gates(env) -> None:
    repos, server, ids = env
    boss = _User(ids["boss"], "boss")
    body = org_admin.TenantRoleBody(bare_key="editor", name_zh="编辑")

    # A member without the org_admin package: 403, existence not disclosed.
    stranger = repos.user_repo.create(username="stranger", password_hash="h", role="user")
    with pytest.raises(org_admin.HTTPException) as ei:
        await org_admin.create_tenant_role(ORG, body, user=_User(stranger, "stranger"), server=server)
    assert ei.value.status_code == 403

    # Unknown org: 404 for the PLATFORM admin; a tenant admin gets the same
    # 403 as any other no-access case — no existence oracle (review
    # 2026-10-02 P1-9; org_admin imports the org_members gate).
    with pytest.raises(org_admin.HTTPException) as ei:
        await org_admin.create_tenant_role("org-x", body, user=boss, server=server)
    assert ei.value.status_code == 403

    # Malformed display key: 400.
    with pytest.raises(org_admin.HTTPException) as ei:
        await org_admin.create_tenant_role(
            ORG, org_admin.TenantRoleBody(bare_key="Bad Key", name_zh="x"), user=boss, server=server
        )
    assert ei.value.status_code == 400

    # Duplicate: 409.
    await org_admin.create_tenant_role(ORG, body, user=boss, server=server)
    with pytest.raises(org_admin.HTTPException) as ei:
        await org_admin.create_tenant_role(ORG, body, user=boss, server=server)
    assert ei.value.status_code == 409

    # System rows: update and delete are both 404 (masked, not 403 — the row
    # is not *this tenant's* to act on).
    for call in (
        lambda: org_admin.update_tenant_role(
            ORG, "org_admin", org_admin.TenantRoleUpdateBody(name_zh="x"), user=boss, server=server
        ),
        lambda: org_admin.delete_tenant_role(ORG, "org_admin", user=boss, server=server),
    ):
        with pytest.raises(org_admin.HTTPException) as ei:
            await call()
        assert ei.value.status_code == 404


async def test_update_of_a_foreign_tenant_package_is_404(env) -> None:
    _tenant_role("t_org-b_edit", "别家的", org_id=OTHER_ORG)
    _, server, ids = env
    with pytest.raises(org_admin.HTTPException) as ei:
        await org_admin.update_tenant_role(
            ORG,
            "t_org-b_edit",
            org_admin.TenantRoleUpdateBody(name_zh="越权"),
            user=_User(ids["boss"], "boss"),
            server=server,
        )
    assert ei.value.status_code == 404


# ----- mappings -----


async def test_mapping_writes_are_scoped_and_validated(env) -> None:
    repos, server, ids = env
    boss = _User(ids["boss"], "boss")
    _tenant_role(T_PREFIX + "editor", "编辑")
    _tenant_role("t_org-b_edit", "别家的", org_id=OTHER_ORG)

    got = await org_admin.set_tenant_mapping(
        ORG,
        org_admin.TenantMappingBody(job_code="finance_manager", role_keys=[T_PREFIX + "editor"]),
        user=boss,
        server=server,
    )
    assert got == {"org_id": ORG, "job_code": "finance_manager", "role_keys": [T_PREFIX + "editor"]}

    rows = await org_admin.list_tenant_mappings(ORG, user=boss, server=server)
    assert [(m["job_code"], m["role_key"]) for m in rows] == [
        ("finance_manager", T_PREFIX + "editor")
    ]

    # A foreign tenant's package is rejected (400), not silently accepted.
    with pytest.raises(org_admin.HTTPException) as ei:
        await org_admin.set_tenant_mapping(
            ORG,
            org_admin.TenantMappingBody(job_code="x", role_keys=["t_org-b_edit"]),
            user=boss,
            server=server,
        )
    assert ei.value.status_code == 400

    # Deleting by empty replacement.
    await org_admin.set_tenant_mapping(
        ORG,
        org_admin.TenantMappingBody(
            job_code="finance_manager",
            role_keys=[],
            previous_job_code="finance_manager",
        ),
        user=boss,
        server=server,
    )
    assert await org_admin.list_tenant_mappings(ORG, user=boss, server=server) == []


# ----- resource ACLs -----


async def _seed_agent(repos, agent_id: str, org_id: str):
    from octop.infra.db.repos.orgs import OrgRepo

    user = repos.user_repo.create(username=f"u-{agent_id}", password_hash="h", role="user")
    if org_id:
        OrgRepo(_DB).update_org(org_id, status="active")
        repos.user_repo.set_default_org_id(user, org_id)
    repos.agent_repo.create(
        user_id=user,
        agent_id=agent_id,
        name=f"agent-{agent_id}",
        system_prompt="",
        org_id=org_id,
    )
    return agent_id


async def test_resource_acl_lists_only_this_tenant(env) -> None:
    repos, server, ids = env
    await _seed_agent(repos, "agent-a", ORG)
    await _seed_agent(repos, "agent-b", OTHER_ORG)

    rows = await org_admin.list_tenant_resource_acl(
        ORG, "agents", user=_User(ids["boss"], "boss"), server=server
    )
    assert [r["id"] for r in rows] == ["agent-a"]


async def test_resource_acl_experts_rows_carry_connector_refs(env) -> None:
    """Experts ACL rows include default_mcp_servers — advisory data the
    tenant MCP catalog uses to flag templates whose visible roles exceed
    the referenced service's roles."""
    repos, server, ids = env
    boss = _User(ids["boss"], "boss")
    expert_repo = PublishedExpertRepo(repos.db)
    expert_repo.create(
        id="pe-1",
        slug="pe-1",
        name="小通",
        created_by=str(ids["boss"]),
        org_id=ORG,
        allowed_roles=["member"],
        default_mcp_servers=["custom-mcp:summer-cmc__01M3T3W28TGKD8BJ51JD074GFJ"],
    )

    rows = await org_admin.list_tenant_resource_acl(
        ORG, "experts", user=boss, server=server
    )
    row = next(r for r in rows if r["id"] == "pe-1")
    assert row["default_mcp_servers"] == [
        "custom-mcp:summer-cmc__01M3T3W28TGKD8BJ51JD074GFJ"
    ]
    # Other families keep the lean payload.
    await _seed_agent(repos, "agent-a", ORG)
    agent_rows = await org_admin.list_tenant_resource_acl(
        ORG, "agents", user=boss, server=server
    )
    assert "default_mcp_servers" not in agent_rows[0]


async def test_resource_acl_write_is_allowed_roles_only(env) -> None:
    repos, server, ids = env
    boss = _User(ids["boss"], "boss")
    await _seed_agent(repos, "agent-a", ORG)
    _tenant_role(T_PREFIX + "editor", "编辑")

    got = await org_admin.set_tenant_resource_acl(
        ORG,
        "agents",
        "agent-a",
        org_admin.TenantResourceAclBody(allowed_roles=["member", T_PREFIX + "editor"]),
        user=boss,
        server=server,
    )
    assert got["allowed_roles"] == ["member", T_PREFIX + "editor"]
    assert repos.agent_repo.get("agent-a").org_id == ORG
    assert repos.agent_repo.get("agent-a").allowed_roles == ["member", T_PREFIX + "editor"]

    # The write cannot move attribution — there is no field to express it, and
    # a foreign or unattributed row is a plain 404.
    with pytest.raises(org_admin.HTTPException) as ei:
        await org_admin.set_tenant_resource_acl(
            ORG,
            "agents",
            "agent-of-nowhere",
            org_admin.TenantResourceAclBody(allowed_roles=[]),
            user=boss,
            server=server,
        )
    assert ei.value.status_code == 404

    # Out-of-tenant package in the ACL: 400.
    _tenant_role("t_org-b_edit", "别家的", org_id=OTHER_ORG)
    with pytest.raises(org_admin.HTTPException) as ei:
        await org_admin.set_tenant_resource_acl(
            ORG,
            "agents",
            "agent-a",
            org_admin.TenantResourceAclBody(allowed_roles=["t_org-b_edit"]),
            user=boss,
            server=server,
        )
    assert ei.value.status_code == 400

    events = repos.audit_repo.query(action="resource.acl.set", org_id=ORG, limit=5)
    assert len(events) == 1


async def test_mcp_services_family_is_retired(env) -> None:
    """2026-10-01 收敛：MCP 角色配置唯一入口在租户 MCP 目录（org_mcp），
    资源授权范围的 ``mcp-services`` 族整体退役——list/写一律 400。"""
    repos, server, ids = env
    boss = _User(ids["boss"], "boss")
    for call in (
        lambda: org_admin.list_tenant_resource_acl(ORG, "mcp-services", user=boss, server=server),
        lambda: org_admin.set_tenant_resource_acl(
            ORG,
            "mcp-services",
            "svc-own",
            org_admin.TenantResourceAclBody(allowed_roles=[]),
            user=boss,
            server=server,
        ),
    ):
        with pytest.raises(org_admin.HTTPException) as ei:
            await call()
        assert ei.value.status_code == 400


async def test_connectors_family_hides_catalog_attachments(env) -> None:
    """目录派生连接器（``custom-mcp:{service_id}`` kind）是 MCP 目录的投影，
    不作为独立可配置行出现在 connectors 族；手工 ``custom-mcp`` 照常列出。"""
    repos, server, ids = env
    boss = _User(ids["boss"], "boss")
    repos.connector_repo.create(
        instance_id="cat-attach-1",
        user_id=ids["boss"],
        kind="custom-mcp:svc-1",
        display_name="目录接入 · 某服务",
        mcp_server_name="custom-mcp:svc-1__cat-attach-1",
        shared=True,
        org_id=ORG,
    )
    repos.connector_repo.create(
        instance_id="manual-mcp-1",
        user_id=ids["boss"],
        kind="custom-mcp",
        display_name="手工外接",
        mcp_server_name="custom-mcp__manual-mcp-1",
        shared=True,
        org_id=ORG,
    )

    rows = await org_admin.list_tenant_resource_acl(
        ORG, "connectors", user=boss, server=server
    )
    assert [r["id"] for r in rows] == ["manual-mcp-1"]


# (Review P1-4 coverage — "an ACL edit of a PUBLISHED service re-distributes
# the hub ACL" — moved to test_org_mcp.py together with the retired
# mcp-services family; the org_mcp roles endpoint is its new home.)
