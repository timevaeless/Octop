"""mcp-hub ACL 分发通道（方案A，2026-10-15 拍板）：octop 内部端点 + 快照缓存。

通道拓扑（用户拍板 A+version）：hub 每 15s 轮询 octop
``GET /api/internal/acl/version``（几十字节哈希），变了才拉全量
``GET /api/internal/acl`` 并 ToolAcl.replace。取代 nacos dataId 成为主通道
（nacos 写入保留为 legacy 兼容，``MCP_CATALOG_NACOS_ADDR`` 未配置时跳过）。

为什么缓存放这里而不是 API 层：``CatalogPublisher`` 的 publish / push_acl /
unpublish 每次成功外写后调 :func:`invalidate`——发布动作最迟下一个轮询周期
（15s）收敛，不等 30s TTL 过期。API 层（internal_acl 路由）只读快照，
不反向依赖 mcp_catalog 之上的模块。

规模口径：快照 = 全量 role→tools 投影（单 JOIN），500 角色 × 300 工具
≈ MB 级；轮询成本经 version 端点压到恒定几十字节，见 ALIYUN-CHECKLIST §0。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

_TTL_SECONDS = 30.0

_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "version": None, "acl": None}


def _canonical(acl: dict[str, list[str]]) -> str:
    """Canonical JSON（sort_keys + 紧凑分隔符）——版本指纹的唯一序列化。"""
    return json.dumps(acl, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _md5(payload: str) -> str:
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def snapshot(repo: Any) -> tuple[str, dict[str, list[str]]]:
    """当前全局 ACL 快照（version, acl），30s TTL 缓存兜住轮询频率。

    缓存未命中时重算 ``publisher.global_acl``（单 JOIN）；发布路径写成功后
    经 :func:`invalidate` 主动失效，收敛延迟 = hub 轮询间隔（15s），与
    legacy nacos dataId 通道的 watcher 间隔一致。
    """
    now = time.monotonic()
    with _lock:
        if _cache["acl"] is not None and now - _cache["at"] < _TTL_SECONDS:
            return str(_cache["version"]), dict(_cache["acl"])
    # 延迟导入：publisher 顶部引入本模块的 invalidate，模块级反向导入成环
    from octop.mcp_catalog.publisher import global_acl  # noqa: PLC0415

    acl = global_acl(repo)
    version = _md5(_canonical(acl))
    with _lock:
        _cache.update(at=now, version=version, acl=acl)
    return version, acl


def invalidate() -> None:
    """发布/推送/下线成功后调用：下一个请求重算快照（而非等 TTL 过期）。"""
    with _lock:
        _cache.update(at=0.0, version=None, acl=None)


def version_of(acl: dict[str, list[str]]) -> str:
    """任意 ACL map 的版本指纹（测试与校验用，绕过缓存）。"""
    return _md5(_canonical(acl))
