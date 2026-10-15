"""Tests for the mcp-hub ACL pull channel (方案A, 2026-10-15).

* ``acl_channel``  — snapshot 缓存（TTL/失效/版本指纹）与 global_acl 同源性；
* ``internal_acl`` — Bearer 委派密钥鉴权（缺失/错误 401、常量时间比较路径），
  version 与 full 端点形状。

不拉起 FastAPI app：路由处理函数直调（``get_server`` 依赖不在被测面内）。
"""

from __future__ import annotations

import types

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.errors import OctopError
from octop.mcp_catalog import acl_channel
from octop.mcp_catalog.publisher import global_acl
from octop.mcp_catalog.repo import CatalogRepo


@pytest.fixture()
def db(tmp_path):
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


@pytest.fixture()
def repo(db: SqlitePool) -> CatalogRepo:
    return CatalogRepo(db)


@pytest.fixture(autouse=True)
def _delegation_secret(monkeypatch: pytest.MonkeyPatch):
    """端点鉴权用 env 密钥（部署形态：与 hub MCP_DELEGATION_SECRET 同值）。"""
    monkeypatch.setenv("OCTOP_MCP_DELEGATION_SECRET", "unit-test-secret")
    acl_channel.invalidate()  # 隔离用例间的模块级缓存


def _svc_fields(service_id="summer-cmc", **over):
    fields = {
        "service_id": service_id,
        "display_name": "渠管中心",
        "nacos_namespace": "local",
        "nacos_group": "DEFAULT_GROUP",
        "nacos_service_name": "summer-cmc",
        "backend_port": 48088,
    }
    fields.update(over)
    return fields


def _tool_fields(service_id="summer-cmc", **over):
    fields = {
        "service_id": service_id,
        "tool_name": "list_cmc_banks",
        "description": "查询银行列表",
        "http_method": "POST",
        "http_path": "/rpc-api/cmc/bank-info/banks",
        "request_body": "{}",
        "allowed_roles": ["cheryfs"],
    }
    fields.update(over)
    return fields


def _server(db) -> types.SimpleNamespace:
    """最小 server 桩：env 密钥路径不触 secrets 表，但属性访问面要齐。"""
    return types.SimpleNamespace(
        services=types.SimpleNamespace(
            db=db,
            secret_repo=types.SimpleNamespace(
                get_or_create=lambda key, factory: b"db-generated-secret"
            ),
        )
    )


# ---------------------------------------------------------------- acl_channel

def test_snapshot_version_stable_within_cache(repo: CatalogRepo):
    repo.create_service(_svc_fields(allowed_roles=["cheryfs"]))
    repo.update_service("summer-cmc", {"status": "published"})
    repo.upsert_tool(_tool_fields())

    v1, acl1 = acl_channel.snapshot(repo)
    v2, acl2 = acl_channel.snapshot(repo)
    assert v1 == v2
    assert acl1 == acl2 == {"cheryfs": ["list_cmc_banks"]}


def test_snapshot_reflects_data_after_invalidate(repo: CatalogRepo):
    repo.create_service(_svc_fields(allowed_roles=["cheryfs"]))
    repo.update_service("summer-cmc", {"status": "published"})
    repo.upsert_tool(_tool_fields())
    v1, _ = acl_channel.snapshot(repo)

    repo.upsert_tool(_tool_fields(tool_name="submit_draft"))
    acl_channel.invalidate()  # 发布路径的失效钩子

    v2, acl = acl_channel.snapshot(repo)
    assert v1 != v2
    assert acl == {"cheryfs": ["list_cmc_banks", "submit_draft"]}


def test_snapshot_empty_catalog_is_empty_object(repo: CatalogRepo):
    """空库 = 空对象：hub 语义「已配置、全拒绝」（与 nacos dataId 通道一致）。"""
    version, acl = acl_channel.snapshot(repo)
    assert acl == {}
    assert version == acl_channel.version_of({})


def test_version_of_order_insensitive():
    """版本指纹基于 canonical JSON：键序/空白差异不影响指纹。"""
    assert acl_channel.version_of({"a": ["t1", "t2"], "b": []}) == acl_channel.version_of(
        {"b": [], "a": ["t1", "t2"]}
    )


def test_global_acl_matches_router_projection(repo: CatalogRepo):
    """双通道同源断言：端点的 global_acl 与发布路径 router._global_acl 恒等。"""
    from octop.mcp_catalog import router

    repo.create_service(_svc_fields(service_id="svc-a", allowed_roles=["expert_user"]))
    repo.update_service("svc-a", {"status": "published"})
    repo.upsert_tool(_tool_fields(service_id="svc-a", tool_name="t_inherit", allowed_roles=[]))
    assert global_acl(repo) == router._global_acl(repo)


# ---------------------------------------------------------------- internal_acl

def _auth(value: str) -> dict[str, str]:
    return {"authorization": value}


def test_acl_version_endpoint_shape(repo: CatalogRepo):
    from octop.api.routers import internal_acl

    repo.create_service(_svc_fields(allowed_roles=["cheryfs"]))
    repo.update_service("summer-cmc", {"status": "published"})
    repo.upsert_tool(_tool_fields())
    server = _server(repo._db)

    out = internal_acl.acl_version(request=None, server=server, authorization="Bearer unit-test-secret")
    assert out["version"] == acl_channel.version_of({"cheryfs": ["list_cmc_banks"]})

    full = internal_acl.acl_full(request=None, server=server, authorization="Bearer unit-test-secret")
    assert full == {"version": out["version"], "acl": {"cheryfs": ["list_cmc_banks"]}}


def test_acl_endpoint_rejects_missing_and_wrong_token(repo: CatalogRepo):
    from octop.api.routers import internal_acl

    server = _server(repo._db)
    with pytest.raises(OctopError):
        internal_acl.acl_version(request=None, server=server, authorization=None)
    with pytest.raises(OctopError):
        internal_acl.acl_version(request=None, server=server, authorization="Bearer wrong-secret")
    with pytest.raises(OctopError):
        # 非 Bearer 形态同样拒绝
        internal_acl.acl_full(request=None, server=server, authorization="unit-test-secret")


def test_acl_endpoint_env_secret_wins_over_db(repo: CatalogRepo, monkeypatch: pytest.MonkeyPatch):
    """env 密钥是部署同步机制：env 存在时不读 secrets 表（delegation 语义一致）。"""
    from octop.api.routers import internal_acl

    class _Boom:
        def get_or_create(self, *args, **kwargs):  # pragma: no cover — 触发即失败
            raise AssertionError("secrets table must not be touched when env secret set")

    server = types.SimpleNamespace(
        services=types.SimpleNamespace(db=repo._db, secret_repo=_Boom())
    )
    out = internal_acl.acl_version(request=None, server=server, authorization="Bearer unit-test-secret")
    assert set(out) == {"version"}
