"""Unit tests for custom MCP validation and assembly."""

from __future__ import annotations

from pathlib import Path

import pytest

from octop.config import OctopConfig
from octop.infra.connectors.builder import build_mcp_server_configs_for_user, mcp_server_name
from octop.infra.connectors.custom_mcp import (
    CUSTOM_MCP_KIND,
    enabled_harness_configs,
    extract_servers,
    harness_spec_for_server,
    normalize_server_spec,
    validate_servers_map,
)
from octop.infra.connectors.service import ConnectorService
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.connectors import ConnectorRepo
from octop.infra.db.repos.secrets import SecretRepo
from octop.infra.db.repos.settings import SettingsRepo
from octop.infra.utils.ulid import new_ulid

ORG = "cheryfs"


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@pytest.fixture
def svc(db: SqlitePool) -> ConnectorService:
    return ConnectorService(
        repo=ConnectorRepo(db),
        secret_repo=SecretRepo(db),
        settings_repo=SettingsRepo(db),
        config=OctopConfig(),
    )


def _ensure_user(db: SqlitePool) -> int:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) "
            "VALUES ('u1', 'x', 'user', 1)"
        )
        row = conn.execute("SELECT id FROM users WHERE username = 'u1'").fetchone()
    assert row is not None
    return int(row["id"])


def _ensure_second_user(db: SqlitePool) -> int:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) "
            "VALUES ('u2', 'x', 'user', 1)"
        )
        row = conn.execute("SELECT id FROM users WHERE username = 'u2'").fetchone()
    assert row is not None
    return int(row["id"])


def test_normalize_streamable_http_and_stdio():
    http = normalize_server_spec(
        "deepwiki",
        {
            "transport": "streamable_http",
            "url": "https://mcp.deepwiki.com/mcp",
            "headers": {"Authorization": "Bearer t"},
        },
    )
    assert http["transport"] == "streamable_http"
    assert http["url"] == "https://mcp.deepwiki.com/mcp"
    assert http["headers"]["Authorization"] == "Bearer t"
    assert http["enabled"] is True

    stdio = normalize_server_spec(
        "local",
        {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "pkg"],
            "enabled": False,
        },
    )
    assert stdio["transport"] == "stdio"
    assert stdio["command"] == "npx"
    assert stdio["args"] == ["-y", "pkg"]
    assert stdio["enabled"] is False


def _svc(svc: ConnectorService, db: SqlitePool, uid: int, name: str, servers: dict, org_id=None):
    """Create one multi-doc connector and return its instance_id."""
    return svc.create_custom_connector(uid, name, servers, org_id=org_id)["instance_id"]


def test_shared_custom_server_uses_collision_safe_name_for_viewer(
    svc: ConnectorService,
    db: SqlitePool,
) -> None:
    owner_id = _ensure_user(db)
    viewer_id = _ensure_second_user(db)
    _svc(
        svc,
        db,
        owner_id,
        "Conns",
        {
            "linear": {
                "transport": "streamable_http",
                "url": "https://mcp.linear.app/mcp",
                "shared": True,
            },
            "private": {
                "transport": "streamable_http",
                "url": "https://private.example.com/mcp",
            },
        },
    )

    visible = svc.list_instances_for_api(viewer_id)
    assert [row["display_name"] for row in visible] == ["linear"]
    shared_name = str(visible[0]["mcp_server_name"])
    assert shared_name.startswith("custom__")
    assert shared_name.endswith("__linear")
    assert svc.list_active_mcp_server_names(viewer_id) == [shared_name]
    assert list(svc.custom_harness_configs(viewer_id)) == [shared_name]
    assert svc.list_default_open_mcp_server_names(viewer_id) == []


def test_rejects_public_http_and_allows_lan():
    from octop.infra.errors import ErrorCode, OctopError

    with pytest.raises(OctopError) as http_exc:
        normalize_server_spec(
            "bad",
            {"transport": "streamable_http", "url": "http://example.com/mcp"},
        )
    assert http_exc.value.code is ErrorCode.CONNECTOR_MCP_HTTPS_REQUIRED

    for url in (
        "http://10.0.0.1/mcp",
        "https://192.168.1.10:8443/mcp",
        "http://host.docker.internal:3000/mcp",
        "http://nas.local/mcp",
    ):
        spec = normalize_server_spec(
            "lan",
            {"transport": "streamable_http", "url": url},
        )
        assert spec["url"] == url


def test_non_rfc1918_intranet_http_via_private_cidrs_env(
    monkeypatch: pytest.MonkeyPatch,
):
    """172.11.x is public per RFC1918; OCTOP_PRIVATE_CIDRS opts it in as LAN."""
    from octop.infra.errors import ErrorCode, OctopError

    monkeypatch.delenv("OCTOP_PRIVATE_CIDRS", raising=False)
    with pytest.raises(OctopError) as http_exc:
        normalize_server_spec(
            "intranet",
            {"transport": "streamable_http", "url": "http://172.11.2.243/api/v1"},
        )
    assert http_exc.value.code is ErrorCode.CONNECTOR_MCP_HTTPS_REQUIRED

    monkeypatch.setenv("OCTOP_PRIVATE_CIDRS", "172.11.0.0/16")
    spec = normalize_server_spec(
        "intranet",
        {"transport": "streamable_http", "url": "http://172.11.2.243/api/v1"},
    )
    assert spec["url"] == "http://172.11.2.243/api/v1"


def test_allows_loopback_http_and_https():
    for url in (
        "http://localhost:8080/mcp",
        "https://127.0.0.1/mcp",
        "http://[::1]/mcp",
    ):
        spec = normalize_server_spec(
            "local",
            {"transport": "streamable_http", "url": url},
        )
        assert spec["url"] == url


def test_mcp_url_invalid_error_code():
    from octop.infra.connectors.custom_mcp import validate_mcp_http_url
    from octop.infra.errors import ErrorCode, OctopError

    with pytest.raises(OctopError) as exc:
        validate_mcp_http_url("ftp://example.com/mcp")
    assert exc.value.code is ErrorCode.CONNECTOR_MCP_URL_INVALID
    assert exc.value.details.get("reason") == "url must be http or https"


def test_validate_servers_map_reserved_and_duplicate():
    with pytest.raises(ValueError, match="conflicts"):
        validate_servers_map(
            {"taken": {"transport": "stdio", "command": "x"}},
            reserved_names={"taken"},
        )
    with pytest.raises(ValueError, match="invalid server name"):
        validate_servers_map({"bad name": {"transport": "stdio", "command": "x"}})


def test_enabled_harness_configs_filters_disabled():
    servers = validate_servers_map(
        {
            "on": {
                "transport": "streamable_http",
                "url": "https://mcp.example.com/mcp",
                "display_name": "示例搜索",
            },
            "off": {
                "transport": "stdio",
                "command": "npx",
                "enabled": False,
            },
        }
    )
    configs = enabled_harness_configs(servers)
    assert set(configs) == {"on"}
    assert "enabled" not in configs["on"]
    assert "display_name" not in configs["on"]
    assert configs["on"]["transport"] == "streamable_http"


def test_display_name_normalized_and_used_in_list_api(svc: ConnectorService, db: SqlitePool):
    uid = _ensure_user(db)
    _svc(
        svc,
        db,
        uid,
        "Docs",
        {
            "http-server-1": {
                "transport": "streamable_http",
                "url": "https://mcp.example.com/mcp",
                "display_name": "  我的知识库  ",
            },
        },
    )
    assert svc.get_custom_servers(uid)["http-server-1"]["display_name"] == "我的知识库"
    listed = svc.list_instances_for_api(uid)
    assert len(listed) == 1
    assert listed[0]["mcp_server_name"] == "http-server-1"
    assert listed[0]["display_name"] == "我的知识库"


def test_list_instances_echoes_org_and_allowed_roles(svc: ConnectorService, db: SqlitePool):
    """Regression: the admin resource-ACL tab reads tenancy/ACL off the listing.

    Expanded custom-MCP children inherit the parent row's values (the ACL lives
    on the parent); a stored ACL must survive a refresh instead of rendering
    as "unknown" on the dashboard.
    """
    from octop.infra.connectors.custom_mcp import CUSTOM_MCP_KIND as KIND

    uid = _ensure_user(db)
    iid = _svc(
        svc,
        db,
        uid,
        "Linear",
        {
            "linear": {
                "transport": "streamable_http",
                "url": "https://mcp.linear.app/mcp",
            },
        },
        org_id=ORG,
    )
    svc._repo.update_metadata(iid, allowed_roles=["finance_operator"])

    listed = svc.list_instances_for_api(uid)
    assert len(listed) == 1
    row = listed[0]
    assert row["org_id"] == ORG
    assert row["allowed_roles"] == ["finance_operator"]


def test_resolve_acl_parent_maps_synthetic_ids(svc: ConnectorService, db: SqlitePool):
    """``custom:PARENT:NAME`` → shared parent row; ``custom:NAME`` → owning doc."""
    from octop.infra.connectors.custom_mcp import CUSTOM_MCP_KIND as KIND
    from octop.infra.connectors.custom_multi import resolve_acl_parent

    uid = _ensure_user(db)
    parent_iid = svc._repo.create(
        instance_id=new_ulid(),
        user_id=uid,
        kind=KIND,
        display_name="shared-src",
        mcp_server_name="custom-mcp",
        shared=True,
        org_id=ORG,
    )
    own_iid = _svc(
        svc,
        db,
        uid,
        "Own",
        {"linear": {"transport": "streamable_http", "url": "https://mcp.linear.app/mcp"}},
    )

    # Shared child: parent id embedded in the synthetic id.
    shared_child = f"custom:{parent_iid}:linear"
    assert resolve_acl_parent(svc, shared_child, uid).instance_id == parent_iid
    # Own child: resolves to the document actually holding the server.
    assert resolve_acl_parent(svc, "custom:linear", uid).instance_id == own_iid
    # Own child naming a server no document holds → None.
    assert resolve_acl_parent(svc, "custom:missing", uid) is None
    # Not a synthetic id → None (caller falls back to the plain lookup).
    assert resolve_acl_parent(svc, parent_iid, uid) is None
    # Shared child pointing at a non-existent parent → None.
    assert resolve_acl_parent(svc, "custom:nope:linear", uid) is None


def test_harness_spec_stdio_default_args():
    spec = harness_spec_for_server({"transport": "stdio", "command": "uvx", "enabled": True})
    assert spec["args"] == []
    assert "enabled" not in spec


def test_harness_spec_streamable_http_adds_accept():
    spec = harness_spec_for_server(
        {
            "transport": "streamable_http",
            "url": "https://mcp.example.com/mcp",
            "headers": {"Authorization": "Bearer x"},
            "enabled": True,
        }
    )
    assert spec["headers"]["Authorization"] == "Bearer x"
    assert spec["headers"]["Accept"] == "application/json, text/event-stream"
    assert "enabled" not in spec


def test_create_and_expand_custom_mcp(svc: ConnectorService, db: SqlitePool):
    uid = _ensure_user(db)
    iid = _svc(
        svc,
        db,
        uid,
        "Conns",
        {
            "deepwiki": {
                "transport": "streamable_http",
                "url": "https://mcp.deepwiki.com/mcp",
            },
            "local": {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "x"],
                "enabled": False,
            },
        },
    )
    assert set(svc.get_custom_servers(uid)) == {"deepwiki", "local"}
    assert extract_servers(svc.decrypt(iid))  # noqa: SLF001

    listed = svc.list_instances_for_api(uid)
    assert len(listed) == 2
    by_name = {row["mcp_server_name"]: row for row in listed}
    assert by_name["deepwiki"]["status"] == "active"
    assert by_name["local"]["status"] == "disabled"
    assert by_name["deepwiki"]["instance_id"] == "custom:deepwiki"

    active = svc.list_active_mcp_server_names(uid)
    assert active == ["deepwiki"]

    svc.patch_custom_server_enabled(uid, "local", enabled=True)
    assert set(svc.list_active_mcp_server_names(uid)) == {"deepwiki", "local"}


def test_save_empty_servers_deletes_that_connector_only(
    svc: ConnectorService, db: SqlitePool
):
    uid = _ensure_user(db)
    iid_a = _svc(svc, db, uid, "A", {"a": {"transport": "stdio", "command": "npx"}})
    iid_b = _svc(svc, db, uid, "B", {"b": {"transport": "stdio", "command": "npx"}})
    assert svc.save_custom_connector(uid, iid_a, {}) == {}
    assert svc._repo.get(iid_a) is None  # noqa: SLF001
    assert set(svc.get_custom_servers(uid)) == {"b"}
    assert svc._repo.get(iid_b) is not None  # noqa: SLF001
    assert svc.list_instances_for_api(uid) != []


def test_build_mcp_configs_merges_enabled_custom(svc: ConnectorService, db: SqlitePool):
    uid = _ensure_user(db)
    # Built-in-style row that catalog would skip if unknown — use real catalog kind optional.
    # Only custom servers here.
    _svc(
        svc,
        db,
        uid,
        "Conns",
        {
            "a": {
                "transport": "streamable_http",
                "url": "https://mcp.example.com/a",
            },
            "b": {
                "transport": "stdio",
                "command": "npx",
                "enabled": False,
            },
        },
    )
    configs = build_mcp_server_configs_for_user(
        svc=svc,
        connector_repo=svc._repo,  # noqa: SLF001
        user_id=uid,
        agent_id="agent-1",
        agent_user_id=uid,
        config=OctopConfig(),
        log=False,
    )
    assert set(configs) == {"a"}
    # Custom MCP is deferred (placeholder) until prepare_chat_mcp.
    assert configs["a"] == {}
    assert "transport" not in configs["a"]


def test_custom_mcp_name_conflicts_with_builtin(svc: ConnectorService, db: SqlitePool):
    uid = _ensure_user(db)
    iid = new_ulid()
    name = mcp_server_name("tencent-docs", iid)
    svc._repo.create(  # noqa: SLF001
        instance_id=iid,
        user_id=uid,
        kind="tencent-docs",
        display_name="Docs",
        mcp_server_name=name,
    )
    with pytest.raises(ValueError, match="conflicts"):
        svc.create_custom_connector(
            uid,
            "Clash",
            {
                name: {
                    "transport": "stdio",
                    "command": "npx",
                }
            },
        )


def test_normalize_preserves_default_open():
    spec = normalize_server_spec(
        "deepwiki",
        {
            "transport": "streamable_http",
            "url": "https://mcp.deepwiki.com/mcp",
            "default_open": True,
        },
    )
    assert spec["default_open"] is True
    harness = harness_spec_for_server(spec)
    assert "default_open" not in harness


def test_default_open_false_by_default_and_listed(svc: ConnectorService, db: SqlitePool):
    uid = _ensure_user(db)
    _svc(
        svc,
        db,
        uid,
        "Conns",
        {
            "always": {
                "transport": "streamable_http",
                "url": "https://mcp.example.com/mcp",
                "default_open": True,
            },
            "opt": {
                "transport": "stdio",
                "command": "npx",
            },
        },
    )
    listed = {row["mcp_server_name"]: row for row in svc.list_instances_for_api(uid)}
    assert listed["always"]["default_open"] is True
    assert listed["opt"]["default_open"] is False


def test_list_default_open_mcp_server_names(svc: ConnectorService, db: SqlitePool):
    uid = _ensure_user(db)
    iid = new_ulid()
    svc._repo.create(
        instance_id=iid,
        user_id=uid,
        kind="tencent-docs",
        display_name="docs",
        mcp_server_name=mcp_server_name("tencent-docs", iid),
        config_json='{"default_open": true}',
    )
    from octop.infra.connectors.crypto import encrypt_credentials

    blob = encrypt_credentials(svc._secret_repo, {"token": "t", "instance_id": iid})
    svc._repo.upsert_credentials(instance_id=iid, blob=blob)

    _svc(
        svc,
        db,
        uid,
        "Conns",
        {
            "always": {
                "transport": "streamable_http",
                "url": "https://mcp.example.com/mcp",
                "default_open": True,
            },
            "opt": {
                "transport": "stdio",
                "command": "npx",
            },
        },
    )
    names = svc.list_default_open_mcp_server_names(uid)
    assert mcp_server_name("tencent-docs", iid) in names
    assert "always" in names
    assert "opt" not in names


@pytest.mark.asyncio
async def test_ensure_fresh_custom_servers_marks_reauth_when_refresh_fails_expired(
    svc: ConnectorService,
    db: SqlitePool,
) -> None:
    import time
    from unittest.mock import AsyncMock, patch

    uid = _ensure_user(db)
    expired_at = int(time.time()) - 60
    _svc(svc, db, uid, "Conns", {"srv": {"transport": "streamable_http", "url": "https://example.com/mcp"}})
    svc.apply_custom_server_oauth(
        uid,
        "srv",
        {
            "access_token": "old",
            "refresh_token": "refresh-me",
            "expires_at": expired_at,
            "oauth_client_id": "cid",
        },
        issuer="https://example.com",
        resource="https://example.com/mcp",
    )
    with patch(
        "octop.infra.connectors.oauth.registry.refresh_custom_mcp_oauth",
        new_callable=AsyncMock,
        side_effect=ValueError("refresh failed"),
    ):
        await svc.ensure_fresh_custom_servers(uid)

    preview = svc.get_custom_servers_for_api(uid)
    assert preview["srv"]["oauth"] == {"configured": False, "required": True}
    stored = svc.get_custom_servers(uid)["srv"]["oauth"]
    assert stored == {"required": True}


@pytest.mark.asyncio
async def test_ensure_fresh_custom_servers_keeps_valid_token_when_refresh_fails(
    svc: ConnectorService,
    db: SqlitePool,
) -> None:
    import time
    from unittest.mock import AsyncMock, patch

    uid = _ensure_user(db)
    expires_at = int(time.time()) + 60
    _svc(svc, db, uid, "Conns", {"srv": {"transport": "streamable_http", "url": "https://example.com/mcp"}})
    svc.apply_custom_server_oauth(
        uid,
        "srv",
        {
            "access_token": "still-valid",
            "refresh_token": "refresh-me",
            "expires_at": expires_at,
            "oauth_client_id": "cid",
        },
        issuer="https://example.com",
        resource="https://example.com/mcp",
    )
    with patch(
        "octop.infra.connectors.oauth.registry.refresh_custom_mcp_oauth",
        new_callable=AsyncMock,
        side_effect=ValueError("refresh failed"),
    ):
        await svc.ensure_fresh_custom_servers(uid)

    preview = svc.get_custom_servers_for_api(uid)
    assert preview["srv"]["oauth"]["configured"] is True
    assert svc.get_custom_servers(uid)["srv"]["oauth"]["access_token"] == "still-valid"


def test_save_adopts_org_for_orgless_connector(
    svc: ConnectorService, db: SqlitePool
) -> None:
    """A platform (orgless) connector adopts the tenant on its owner's save.

    Mirrors the single-document adoption rule: the row must not stay orgless
    forever, or its shared servers stay visible platform-wide.
    """
    uid = _ensure_user(db)
    servers = {
        "linear": {
            "transport": "streamable_http",
            "url": "https://mcp.linear.app/mcp",
        }
    }
    iid = svc.create_custom_connector(uid, "Linear", servers, org_id=None)["instance_id"]
    assert svc._repo.get(iid).org_id is None  # noqa: SLF001

    svc.save_custom_connector(uid, iid, servers, org_id=ORG)
    assert svc._repo.get(iid).org_id == ORG  # noqa: SLF001


def test_save_from_redacted_ui_spec_preserves_stored_oauth(
    svc: ConnectorService, db: SqlitePool
) -> None:
    """Saving a redacted (UI-shaped) spec must not wipe stored OAuth tokens.

    Mirrors the old ``put_custom_servers`` semantics: client-supplied oauth
    blobs are dropped, stored tokens are merged back per server name.
    """
    import time

    uid = _ensure_user(db)
    iid = _svc(svc, db, uid, "A", {"srv": {"transport": "streamable_http", "url": "https://a.example/mcp"}})
    svc.apply_custom_server_oauth(
        uid,
        "srv",
        {
            "access_token": "tok-1",
            "refresh_token": "r",
            "expires_at": int(time.time()) + 600,
            "oauth_client_id": "cid",
        },
        issuer="https://a.example",
        resource="https://a.example/mcp",
    )
    stored = svc.get_custom_servers(uid)["srv"]["oauth"]
    assert stored.get("access_token") == "tok-1"

    # The dashboard saves what it received from get_custom_servers_for_api —
    # a redacted spec carrying only the oauth preview.
    redacted = svc.get_custom_servers_for_api(uid)
    svc.save_custom_connector(uid, iid, redacted)

    still = svc.get_custom_servers(uid)["srv"]["oauth"]
    assert still.get("access_token") == "tok-1"


# ----------------------------------------------------- multi-doc isolation


def test_two_connectors_merged_read_and_write_isolation(
    svc: ConnectorService, db: SqlitePool
) -> None:
    """Patching a server in connector A must never rewrite connector B."""
    uid = _ensure_user(db)
    _svc(
        svc,
        db,
        uid,
        "A",
        {
            "alpha": {"transport": "streamable_http", "url": "https://a.example/mcp"},
            "beta": {"transport": "stdio", "command": "npx", "enabled": False},
        },
    )
    iid_b = _svc(
        svc,
        db,
        uid,
        "B",
        {"gamma": {"transport": "streamable_http", "url": "https://b.example/mcp"}},
    )
    before_b = svc.decrypt(iid_b)  # noqa: SLF001

    svc.patch_custom_server(uid, "alpha", enabled=False)

    # Merged read sees both docs; B's document is byte-identical.
    assert set(svc.get_custom_servers(uid)) == {"alpha", "beta", "gamma"}
    assert svc.decrypt(iid_b) == before_b  # noqa: SLF001
    # And the patched server sits in A only.
    from octop.infra.connectors.custom_multi import locate_connector_for_server

    assert locate_connector_for_server(svc, uid, "alpha").display_name == "A"  # noqa: SLF001
    assert locate_connector_for_server(svc, uid, "gamma").display_name == "B"  # noqa: SLF001


def test_cross_document_server_name_collision_rejected(
    svc: ConnectorService, db: SqlitePool
) -> None:
    """Server names are user-level (bare-name mount keys) — no cross-doc dup."""
    uid = _ensure_user(db)
    _svc(svc, db, uid, "A", {"alpha": {"transport": "stdio", "command": "npx"}})
    with pytest.raises(ValueError, match="conflicts"):
        svc.create_custom_connector(
            uid, "B", {"alpha": {"transport": "stdio", "command": "npx"}}
        )
    # Same check on save into an existing second document.
    iid_b = _svc(svc, db, uid, "B", {"beta": {"transport": "stdio", "command": "npx"}})
    with pytest.raises(ValueError, match="conflicts"):
        svc.save_custom_connector(
            uid, iid_b, {"beta": {"transport": "stdio", "command": "npx"}, "alpha": {"transport": "stdio", "command": "npx"}}
        )


def test_connector_display_name_unique_across_docs(
    svc: ConnectorService, db: SqlitePool
) -> None:
    from octop.infra.connectors.service import ConnectorNameTakenError

    uid = _ensure_user(db)
    _svc(svc, db, uid, "Linear", {"alpha": {"transport": "stdio", "command": "npx"}})
    iid_b = _svc(svc, db, uid, "Other", {"beta": {"transport": "stdio", "command": "npx"}})
    with pytest.raises(ConnectorNameTakenError):
        svc.create_custom_connector(uid, "Linear", {})
    with pytest.raises(ConnectorNameTakenError):
        svc.rename_custom_connector(uid, iid_b, "Linear")
    # Non-custom rows count too.
    iid = new_ulid()
    svc._repo.create(  # noqa: SLF001
        instance_id=iid,
        user_id=uid,
        kind="tencent-docs",
        display_name="Docs",
        mcp_server_name=mcp_server_name("tencent-docs", iid),
    )
    with pytest.raises(ConnectorNameTakenError):
        svc.create_custom_connector(uid, "Docs", {})


def test_delete_connector_removes_only_its_servers(
    svc: ConnectorService, db: SqlitePool
) -> None:
    uid = _ensure_user(db)
    iid_a = _svc(svc, db, uid, "A", {"alpha": {"transport": "stdio", "command": "npx"}})
    _svc(svc, db, uid, "B", {"beta": {"transport": "stdio", "command": "npx"}})

    removed = svc.delete_custom_connector(uid, iid_a)
    assert set(removed) == {"alpha"}
    assert set(svc.get_custom_servers(uid)) == {"beta"}
    assert svc.list_active_mcp_server_names(uid) == ["beta"]


def test_expand_own_lists_all_connectors(svc: ConnectorService, db: SqlitePool) -> None:
    uid = _ensure_user(db)
    _svc(svc, db, uid, "A", {"alpha": {"transport": "stdio", "command": "npx"}})
    _svc(svc, db, uid, "B", {"beta": {"transport": "streamable_http", "url": "https://b.example/mcp"}})
    listed = svc.list_instances_for_api(uid)
    assert {row["mcp_server_name"] for row in listed} == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_oauth_refresh_only_rewrites_owning_document(
    svc: ConnectorService, db: SqlitePool
) -> None:
    """Expired OAuth in A marks A; B's document stays byte-identical."""
    import time
    from unittest.mock import AsyncMock, patch

    uid = _ensure_user(db)
    expired_at = int(time.time()) - 60
    _svc(svc, db, uid, "A", {"stale": {"transport": "streamable_http", "url": "https://a.example/mcp"}})
    iid_b = _svc(svc, db, uid, "B", {"fresh": {"transport": "streamable_http", "url": "https://b.example/mcp"}})
    before_b = svc.decrypt(iid_b)  # noqa: SLF001
    svc.apply_custom_server_oauth(
        uid,
        "stale",
        {
            "access_token": "old",
            "refresh_token": "refresh-me",
            "expires_at": expired_at,
            "oauth_client_id": "cid",
        },
        issuer="https://a.example",
        resource="https://a.example/mcp",
    )
    with patch(
        "octop.infra.connectors.oauth.registry.refresh_custom_mcp_oauth",
        new_callable=AsyncMock,
        side_effect=ValueError("refresh failed"),
    ):
        await svc.ensure_fresh_custom_servers(uid)

    assert svc.get_custom_servers(uid)["stale"]["oauth"] == {"required": True}
    assert svc.decrypt(iid_b) == before_b  # noqa: SLF001


def test_create_rejects_empty_servers(svc: ConnectorService, db: SqlitePool) -> None:
    """Empty create must 400 cleanly — no stub row, no crash."""
    uid = _ensure_user(db)
    with pytest.raises(ValueError, match="at least one server"):
        svc.create_custom_connector(uid, "Empty", {})
    assert svc._repo.list_by_user_kind(uid, CUSTOM_MCP_KIND) == []  # noqa: SLF001
