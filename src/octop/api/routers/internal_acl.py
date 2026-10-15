"""Internal ACL distribution endpoints for mcp-hub (方案A: hub pulls from octop).

mcp-hub 工具 ACL 分发主通道（2026-10-15 拍板 A+version，nacos dataId 降级为
legacy 兼容通道）：

* ``GET /api/internal/acl/version`` — 几十字节哈希，hub 每 15s 轮询；
* ``GET /api/internal/acl`` — 全量 ``{"version": ..., "acl": {role: [tools]}}``，
  hub 仅在哈希变化时拉取并 ``ToolAcl.replace``。

鉴权：``Authorization: Bearer <委派密钥>``——与 mcp-hub 的
``MCP_DELEGATION_SECRET`` 同值（两进程本就共享该密钥做委派 JWT 验签，不引入
新凭证）。常量时间比较；缺失/不匹配一律 401。

数据：``acl_channel.snapshot``（30s TTL 缓存 + 发布路径主动失效），内容与
legacy nacos dataId 同源（``publisher.global_acl``），两通道永不分叉。
语义对齐 nacos 通道：端点 404（旧版 octop 无此路由）= catalog 未接管，hub 侧
回落 env 兜底；空对象 = 已配置、全拒绝。
"""

from __future__ import annotations

import secrets as _secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, Request

from octop.api.deps import get_server
from octop.infra.connectors.delegation import get_or_create_delegation_secret
from octop.infra.errors import ErrorCode, OctopError
from octop.mcp_catalog import acl_channel
from octop.mcp_catalog.repo import CatalogRepo

router = APIRouter()


def _repo(server: Any) -> CatalogRepo:
    return CatalogRepo(server.services.db)


def _authorize(server: Any, authorization: str | None) -> None:
    """Bearer 委派密钥校验（常量时间比较；与 hub 共享同值，无新凭证）。"""
    raw = authorization or ""
    token = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
    if not token:
        raise OctopError(ErrorCode.AUTH_FAILED, "missing internal token")
    expected = get_or_create_delegation_secret(server.services.secret_repo)
    if not _secrets.compare_digest(token.encode("utf-8"), expected):
        raise OctopError(ErrorCode.AUTH_FAILED, "invalid internal token")


@router.get("/internal/acl/version")
def acl_version(
    request: Request,  # noqa: ARG001 — FastAPI 依赖注入载体
    server: Any = Depends(get_server),
    authorization: str | None = Header(None),
) -> dict[str, str]:
    """轻量版本端点：hub 稳态轮询只打这里（体积与 ACL 规模无关）。"""
    _authorize(server, authorization)
    version, _acl = acl_channel.snapshot(_repo(server))
    return {"version": version}


@router.get("/internal/acl")
def acl_full(
    request: Request,  # noqa: ARG001 — FastAPI 依赖注入载体
    server: Any = Depends(get_server),
    authorization: str | None = Header(None),
) -> dict[str, Any]:
    """全量 ACL 快照：hub 仅在 version 变化后拉取。"""
    _authorize(server, authorization)
    version, acl = acl_channel.snapshot(_repo(server))
    return {"version": version, "acl": acl}
