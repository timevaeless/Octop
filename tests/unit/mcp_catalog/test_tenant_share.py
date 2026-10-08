"""Tests for the catalog→connector attachment module (2026-10-01 design).

``tenant_share`` collapses the published-service distribution into ONE
tenant-admin action (接入连接器): one ``custom-mcp:{service_id}`` parent row
per (service, tenant), roles owned by the catalog and synced one-way, viewer
mounting gated by the shared-row rules (tenant boundary + synced roles).
"""

from __future__ import annotations

import pytest

from octop.config import OctopConfig
from octop.infra.connectors.service import ConnectorService
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.connectors import ConnectorRepo
from octop.infra.db.repos.orgs import OrgRepo
from octop.infra.db.repos.secrets import SecretRepo
from octop.infra.db.repos.settings import SettingsRepo
from octop.infra.users.context import AuthzContext
from octop.mcp_catalog.repo import CatalogRepo
from octop.mcp_catalog.tenant_share import (
    CATALOG_KIND_PREFIX,
    attach_service_to_org,
    catalog_kind,
    catalog_parent_row,
    catalog_service_attachments,
    catalog_service_id,
    catalog_shared_harness_configs,
    delete_service_attachments,
    detach_service_from_org,
    sync_service_attachments,
)

ORG = "org-a"
OTHER_ORG = "org-b"


@pytest.fixture
def db(tmp_path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@pytest.fixture
def env(db: SqlitePool):
    with db.transaction() as conn:
        for uid in (1, 2):
            conn.execute(
                "INSERT INTO users(id, username, password_hash, role, created_at) "
                f"VALUES ({uid}, 'u{uid}', 'x', 'user', 1)"
            )
    orgs = OrgRepo(db)
    orgs.create_org(org_id=ORG, name="Org A")
    orgs.create_org(org_id=OTHER_ORG, name="Org B")
    conn_svc = ConnectorService(
        repo=ConnectorRepo(db),
        secret_repo=SecretRepo(db),
        settings_repo=SettingsRepo(db),
        config=OctopConfig(),
    )
    return db, orgs, conn_svc, CatalogRepo(db)


def _service(db: SqlitePool, service_id: str = "svc-1", org_id: str = "*") -> object:
    cat = CatalogRepo(db)
    return cat.create_service(
        {
            "service_id": service_id,
            "display_name": "渠管中心",
            "nacos_service_name": service_id,
            "backend_port": 80,
            "org_id": org_id,
            "allowed_roles": ["r1"],
        }
    )


def _attach(env, service_id: str, org_id: str = ORG, owner_id: int = 1):
    _, _, conn_svc, cat = env
    if cat.get_service(service_id) is None:
        _service(env[0], service_id)
    return attach_service_to_org(
        conn_svc,
        conn_svc._repo,
        cat.get_service(service_id),
        org_id=org_id,
        owner_user_id=owner_id,
        higress_base="http://higress.local",
    )


# ----- kind naming -----


def test_catalog_kind_roundtrip():
    assert catalog_kind("svc-1") == "custom-mcp:svc-1"
    assert catalog_service_id("custom-mcp:svc-1") == "svc-1"
    assert catalog_service_id("custom-mcp") is None
    assert catalog_service_id("qcc") is None


# ----- attach / detach (idempotent) -----


def test_attach_is_idempotent_per_tenant(env) -> None:
    first = _attach(env, "svc-1")
    assert first["action"] == "created"
    # roles are stamped on FIRST attach — a fresh attachment without them
    # would mount role-unrestricted (regression guard)
    _, _, conn_svc, _ = env
    row = conn_svc._repo.list_by_kind("custom-mcp:svc-1")[0]
    assert row.allowed_roles == ["r1"]

    second = _attach(env, "svc-1")
    assert second == {"action": "updated", "instance_id": first["instance_id"]}

    rows = conn_svc._repo.list_by_kind("custom-mcp:svc-1")
    assert len(rows) == 1
    assert rows[0].org_id == ORG
    assert rows[0].shared
    # roles are catalog-owned and synced onto the row
    assert rows[0].allowed_roles == ["r1"]
    assert catalog_service_attachments(conn_svc._repo, "svc-1", ORG)
    assert not catalog_service_attachments(conn_svc._repo, "svc-1", OTHER_ORG)


def test_two_tenants_attach_independently(env) -> None:
    a = _attach(env, "svc-1", ORG)
    b = _attach(env, "svc-1", OTHER_ORG, owner_id=2)
    assert a["instance_id"] != b["instance_id"]
    _, _, conn_svc, _ = env
    assert len(conn_svc._repo.list_by_kind("custom-mcp:svc-1")) == 2


def test_attach_and_detach_roundtrip(env) -> None:
    _attach(env, "svc-1")
    _, _, conn_svc, _ = env
    assert detach_service_from_org(conn_svc._repo, "svc-1", ORG)
    assert not catalog_service_attachments(conn_svc._repo, "svc-1", ORG)
    assert not detach_service_from_org(conn_svc._repo, "svc-1", ORG)  # already gone


def test_sync_and_delete_service_attachments(env) -> None:
    _attach(env, "svc-1", ORG)
    _attach(env, "svc-1", OTHER_ORG, owner_id=2)
    _, _, conn_svc, cat = env

    cat.update_service("svc-1", {"allowed_roles": ["r2"], "display_name": "改名"})
    svc = cat.get_service("svc-1")
    assert sync_service_attachments(conn_svc, conn_svc._repo, svc) == 2
    rows = conn_svc._repo.list_by_kind("custom-mcp:svc-1")
    assert {r.org_id: r.allowed_roles for r in rows} == {ORG: ["r2"], OTHER_ORG: ["r2"]}

    assert delete_service_attachments(conn_svc._repo, "svc-1") == 2
    assert conn_svc._repo.list_by_kind("custom-mcp:svc-1") == []


# ----- viewer mounting (catalog_shared_harness_configs) -----


def test_owner_mounts_without_authz(env) -> None:
    _attach(env, "svc-1", ORG, owner_id=1)
    _, _, conn_svc, _ = env
    configs = catalog_shared_harness_configs(conn_svc, 1, authz=None)
    assert len(configs) == 1
    ((name, spec),) = configs.items()
    # The key becomes the LLM tool-name prefix: safe "custom__…" form (no
    # colons, bounded length) — never the colon-bearing row mcp_server_name.
    assert name.startswith("custom__")
    assert spec["url"].endswith("/api/internal/mcp/svc-1")
    # validate's authoritative set and the assignable picker speak the same
    # harness key (the raw row name is stripped by the mount-name hook).
    allowed = conn_svc.list_active_mcp_server_names(1)
    assert allowed == [name]
    assignable = conn_svc.list_assignable_instances_for_api(1)
    assert name in {i["mcp_server_name"] for i in assignable}


def test_other_tenant_viewer_never_mounts(env) -> None:
    _attach(env, "svc-1", ORG, owner_id=1)
    _, _, conn_svc, _ = env
    # foreign viewer with authz: org boundary blocks
    foreign = AuthzContext(user_id=9, org_id=OTHER_ORG, role_keys=("r1",))
    assert catalog_shared_harness_configs(conn_svc, 9, authz=foreign) == {}
    # legacy internal caller (authz=None): fail-closed to own rows only
    assert catalog_shared_harness_configs(conn_svc, 9, authz=None) == {}


def test_same_tenant_viewer_mounts_and_role_gate_applies(env) -> None:
    _attach(env, "svc-1", ORG, owner_id=1)
    _, _, conn_svc, _ = env

    # org_admin: the maximally privileged role inside their tenant
    boss = AuthzContext(user_id=2, org_id=ORG, role_keys=("org_admin",), is_org_admin=True)
    configs = catalog_shared_harness_configs(conn_svc, 2, authz=boss)
    assert len(configs) == 1

    # roleless member: the synced roles gate them out (fail-closed)
    roleless = AuthzContext(user_id=3, org_id=ORG, role_keys=())
    assert catalog_shared_harness_configs(conn_svc, 3, authz=roleless) == {}

    # member holding the granted package: mounts
    member = AuthzContext(user_id=4, org_id=ORG, role_keys=("r1",))
    assert len(catalog_shared_harness_configs(conn_svc, 4, authz=member)) == 1


def test_mount_name_roundtrips_through_validate_and_harness(env) -> None:
    """The name validate issues must be the name chat-time loading resolves.

    Regression for 2026-10-01: validate's authoritative name set exposed the
    connector row's raw ``mcp_server_name`` while the harness spec was keyed
    ``custom__{instance}__mcp-{sid}`` — every agent mount passed validation
    then failed in prepare_chat_mcp with "无法加载 MCP 工具". Both sides must
    speak the harness key.
    """
    _attach(env, "svc-1", ORG, owner_id=1)
    _, _, conn_svc, _ = env
    member = AuthzContext(user_id=4, org_id=ORG, role_keys=("r1",))
    allowed = conn_svc.list_active_mcp_server_names(4, authz=member)
    mounted = [n for n in allowed if "__mcp-svc-1" in n]
    assert mounted, "catalog attachment must be mountable under its harness key"
    # the raw row name must NOT leak into the mountable set
    row = conn_svc._repo.list_by_kind("custom-mcp:svc-1")[0]
    assert row.mcp_server_name not in allowed
    configs = conn_svc.custom_harness_configs(4, authz=member)
    for name in mounted:
        assert name in configs, "mount name must resolve to a harness spec"
        assert configs[name].get("transport") == "streamable_http"
    # the assignable picker offers the same safe name (save-time scope)
    assignable = conn_svc.list_assignable_instances_for_api(4, authz=member)
    picker_names = {i["mcp_server_name"] for i in assignable}
    assert set(mounted) <= picker_names
