"""Tenant MCP catalog router tests (``routers/org_mcp.py``, 2026-10-01 design).

The tenant admin's ONE MCP surface: platform-shared ('*') + own-tenant
services are listed; roles are configured here (service-level default +
tool-level subset); 接入连接器 mints the tenant-shared connector with
catalog-owned roles. Same style as test_org_admin — router functions called
directly against a migrated SQLite pool.
"""

from __future__ import annotations

import pytest

from octop.api.routers import org_admin, org_mcp
from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.audit import AuditRepo
from octop.infra.db.repos.connectors import ConnectorRepo
from octop.infra.db.repos.orgs import OrgRepo
from octop.infra.db.repos.secrets import SecretRepo
from octop.infra.db.repos.settings import SettingsRepo
from octop.infra.db.repos.users import UserRepo
from octop.mcp_catalog.repo import CatalogRepo

ORG = "org-a"
OTHER_ORG = "org-b"
T_PREFIX = "t_org-a_"


class _Repos:
    def __init__(self, pool) -> None:
        self.db = pool
        self.user_repo = UserRepo(pool)
        self.org_repo = OrgRepo(pool)
        self.audit_repo = AuditRepo(pool)
        self.connector_repo = ConnectorRepo(pool)
        self.secret_repo = SecretRepo(pool)
        self.settings_repo = SettingsRepo(pool)
        self.config = OctopConfig()

        class _ReposInner:
            connector_repo = self.connector_repo

        self.repos = _ReposInner()


class _User:
    def __init__(self, uid: int, username: str, *, is_admin: bool = False) -> None:
        self.id = uid
        self.username = username
        self.is_admin = is_admin


@pytest.fixture
def env(tmp_path):
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    repos = _Repos(pool)
    repos.org_repo.create_org(org_id=ORG, name="Org A")
    repos.org_repo.create_org(org_id=OTHER_ORG, name="Org B")
    boss = repos.user_repo.create(username="boss", password_hash="h", role="user")
    repos.org_repo.upsert_membership(
        boss, ORG, role_keys=["org_admin"], status="active", source="manual"
    )
    server = type("S", (), {"services": repos})()
    cat = CatalogRepo(pool)
    return repos, server, _User(boss, "boss"), cat


def _svc(cat: CatalogRepo, service_id: str, org_id: str | None, **over) -> object:
    fields = {
        "service_id": service_id,
        "display_name": service_id,
        "nacos_service_name": service_id,
        "backend_port": 80,
        "org_id": org_id,
        "allowed_roles": ["member"],
    }
    fields.update(over)
    return cat.create_service(fields)


def _tool(cat: CatalogRepo, service_id: str, tool_name: str, roles=None):
    return cat.upsert_tool(
        {
            "service_id": service_id,
            "tool_name": tool_name,
            "description": "d",
            "http_method": "POST",
            "http_path": "/rpc-api/x",
            "allowed_roles": list(roles or []),
        }
    )


# ----- listing -----


async def test_list_shows_own_and_shared_rows_only(env) -> None:
    repos, server, boss, cat = env
    _svc(cat, "svc-own", ORG)
    _svc(cat, "svc-shared", "*")
    _svc(cat, "svc-priv", None)
    _svc(cat, "svc-other", OTHER_ORG)
    _tool(cat, "svc-own", "t1")

    rows = await org_mcp.list_tenant_mcp_services(ORG, user=boss, server=server)
    by_id = {r["service_id"]: r for r in rows}
    assert set(by_id) == {"svc-own", "svc-shared"}
    assert by_id["svc-own"]["writable"] is True
    assert by_id["svc-shared"]["writable"] is False
    assert by_id["svc-own"]["attached"] is False
    # tools carry both raw and effective (subset-applied) roles
    tool = by_id["svc-own"]["tools"][0]
    assert tool["effective_roles"] == ["member"]
    assert tool["inherited"] is True


# ----- service-level roles -----


async def test_set_service_roles_writes_and_audits(env) -> None:
    repos, server, boss, cat = env
    repos.org_repo.create_role(role_key=T_PREFIX + "editor", name_zh="编辑", org_id=ORG)
    _svc(cat, "svc-own", ORG)

    got = await org_mcp.set_tenant_mcp_service_roles(
        ORG,
        "svc-own",
        org_mcp.RolesBody(allowed_roles=["member", T_PREFIX + "editor"]),
        user=boss,
        server=server,
    )
    assert got["allowed_roles"] == ["member", T_PREFIX + "editor"]
    assert cat.get_service("svc-own").allowed_roles == ["member", T_PREFIX + "editor"]
    events = repos.audit_repo.query(action="resource.acl.set", org_id=ORG, limit=5)
    assert [e.target for e in events] == ["mcp-services/svc-own"]


async def test_set_service_roles_gates(env) -> None:
    repos, server, boss, cat = env
    _svc(cat, "svc-shared", "*")
    _svc(cat, "svc-own", ORG)

    # shared row: configured by the platform → 403
    with pytest.raises(org_mcp.HTTPException) as ei:
        await org_mcp.set_tenant_mcp_service_roles(
            ORG, "svc-shared", org_mcp.RolesBody(allowed_roles=[]),
            user=boss, server=server,
        )
    assert ei.value.status_code == 403

    # out-of-tenant key: 400
    repos.org_repo.create_role(role_key="t_org-b_x", name_zh="别家的", org_id=OTHER_ORG)
    with pytest.raises(org_mcp.HTTPException) as ei:
        await org_mcp.set_tenant_mcp_service_roles(
            ORG, "svc-own", org_mcp.RolesBody(allowed_roles=["t_org-b_x"]),
            user=boss, server=server,
        )
    assert ei.value.status_code == 400

    # unknown service: 404 (no existence leak for other-tenant rows)
    with pytest.raises(org_mcp.HTTPException) as ei:
        await org_mcp.set_tenant_mcp_service_roles(
            ORG, "svc-other", org_mcp.RolesBody(allowed_roles=[]),
            user=boss, server=server,
        )
    assert ei.value.status_code == 404


async def test_published_service_role_edit_republishes_hub_acl(env, monkeypatch) -> None:
    """Review P1-4 successor: an ACL edit of a PUBLISHED service must push the
    hub ACL projection; a draft edit must not touch nacos at all."""
    repos, server, boss, cat = env
    _svc(cat, "svc-pub", ORG)
    cat.update_service("svc-pub", {"status": "published"})
    _svc(cat, "svc-draft", ORG)

    pushed: list[dict] = []

    class _FakePublisher:
        def push_acl(self, acl_map, *, fence=None):  # noqa: ANN001
            fence()
            pushed.append(acl_map)

    monkeypatch.setattr("octop.mcp_catalog.router._publisher", lambda: _FakePublisher())

    await org_mcp.set_tenant_mcp_service_roles(
        ORG, "svc-pub", org_mcp.RolesBody(allowed_roles=["member"]),
        user=boss, server=server,
    )
    assert len(pushed) == 1
    await org_mcp.set_tenant_mcp_service_roles(
        ORG, "svc-draft", org_mcp.RolesBody(allowed_roles=[]),
        user=boss, server=server,
    )
    assert len(pushed) == 1


# ----- tool-level roles (subset rule) -----


async def test_tool_roles_must_stay_within_service_default(env) -> None:
    repos, server, boss, cat = env
    repos.org_repo.create_role(role_key=T_PREFIX + "editor", name_zh="编辑", org_id=ORG)
    _svc(cat, "svc-own", ORG, allowed_roles=["member"])
    _tool(cat, "svc-own", "t1")

    # inside the default: ok
    got = await org_mcp.set_tenant_mcp_tool_roles(
        ORG, "svc-own", "t1", org_mcp.RolesBody(allowed_roles=["member"]),
        user=boss, server=server,
    )
    assert got["allowed_roles"] == ["member"]

    # outside the default: 400 — a tool never widens the service gate
    with pytest.raises(org_mcp.HTTPException) as ei:
        await org_mcp.set_tenant_mcp_tool_roles(
            ORG, "svc-own", "t1", org_mcp.RolesBody(allowed_roles=[T_PREFIX + "editor"]),
            user=boss, server=server,
        )
    assert ei.value.status_code == 400

    # narrowing back to empty = inherit the default again
    got = await org_mcp.set_tenant_mcp_tool_roles(
        ORG, "svc-own", "t1", org_mcp.RolesBody(allowed_roles=[]),
        user=boss, server=server,
    )
    assert got["allowed_roles"] == []


# ----- attach / detach (接入连接器) -----


async def test_attach_gates(env) -> None:
    repos, server, boss, cat = env
    _svc(cat, "svc-draft", ORG)  # not published

    # unpublished: 409
    repos.settings_repo.set("mcp_catalog_higress_base", "http://higress.local")
    with pytest.raises(org_mcp.HTTPException) as ei:
        await org_mcp.attach_tenant_mcp_service(
            ORG, "svc-draft", user=boss, server=server
        )
    assert ei.value.status_code == 409

    # published but no Higress base configured: 400
    cat.update_service("svc-draft", {"status": "published"})
    repos.settings_repo.set("mcp_catalog_higress_base", "")
    with pytest.raises(org_mcp.HTTPException) as ei:
        await org_mcp.attach_tenant_mcp_service(
            ORG, "svc-draft", user=boss, server=server
        )
    assert ei.value.status_code == 400


async def test_attach_is_idempotent_and_detach_roundtrips(env) -> None:
    repos, server, boss, cat = env
    _svc(cat, "svc-pub", ORG)
    cat.update_service("svc-pub", {"status": "published"})
    repos.settings_repo.set("mcp_catalog_higress_base", "http://higress.local")

    first = await org_mcp.attach_tenant_mcp_service(
        ORG, "svc-pub", user=boss, server=server
    )
    assert first["action"] == "created"
    assert first["allowed_roles"] == ["member"]
    second = await org_mcp.attach_tenant_mcp_service(
        ORG, "svc-pub", user=boss, server=server
    )
    assert second["action"] == "updated"

    rows = repos.connector_repo.list_by_kind("custom-mcp:svc-pub")
    assert len(rows) == 1
    assert rows[0].org_id == ORG and rows[0].shared
    assert rows[0].allowed_roles == ["member"]  # catalog-owned, synced at attach

    # the listing reflects the attachment
    listed = await org_mcp.list_tenant_mcp_services(ORG, user=boss, server=server)
    assert next(r for r in listed if r["service_id"] == "svc-pub")["attached"] is True

    detached = await org_mcp.detach_tenant_mcp_service(
        ORG, "svc-pub", user=boss, server=server
    )
    assert detached["detached"] is True
    with pytest.raises(org_mcp.HTTPException) as ei:
        await org_mcp.detach_tenant_mcp_service(
            ORG, "svc-pub", user=boss, server=server
        )
    assert ei.value.status_code == 404


async def test_resource_acl_connectors_family_shields_catalog_attachments(env) -> None:
    """044 retirement shields: the catalog-derived attachment (kind=
    ``custom-mcp:{sid}``) is invisible in the connectors ACL family (list)
    and a direct PUT is a 400 — its roles are catalog-owned via org_mcp,
    and a write here would bypass the tool-roles ⊆ service-roles invariant.
    Regression: the inline LIKE literal also crashed psycopg on PG (a bare
    ``%`` is a placeholder there) — the pattern must stay a bound param.
    """
    repos, server, boss, cat = env
    _svc(cat, "svc-pub", ORG)
    cat.update_service("svc-pub", {"status": "published"})
    repos.settings_repo.set("mcp_catalog_higress_base", "http://higress.local")
    attached = await org_mcp.attach_tenant_mcp_service(
        ORG, "svc-pub", user=boss, server=server
    )
    instance_id = attached["instance_id"]

    # List: the attachment never appears in the connectors family.
    listed = await org_admin.list_tenant_resource_acl(
        ORG, "connectors", user=boss, server=server
    )
    assert instance_id not in {r["id"] for r in listed}

    # Write: a direct PUT on the attachment id is a 400 with the pointer.
    with pytest.raises(org_admin.HTTPException) as ei:
        await org_admin.set_tenant_resource_acl(
            ORG,
            "connectors",
            instance_id,
            org_admin.TenantResourceAclBody(allowed_roles=["member"]),
            user=boss,
            server=server,
        )
    assert ei.value.status_code == 400
    assert "MCP catalog" in ei.value.detail
