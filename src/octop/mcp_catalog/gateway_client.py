"""Higress Console API client — Octop 下发网关配置的统一出口.

设计立场（2026-10-15 拍板）：Octop 只依赖 Higress 的 **Console 配置 API**
（模拟人操作页面），不感知、不依赖其配置存储选型 —— nacos / file / k8s
均为 Higress 内部实现细节，与 Octop 无关。

API 形状全部实测于 console 2.2.4（本地 compose 与 ACK all-in-one 同版本
镜像），2026-10-15：

- ``POST /session/login``                      → 201 + ``JSESSIONID`` cookie
- ``GET  /v1/routes/{name}``                   → 200（含乐观锁 ``version``）/ 404
- ``POST /v1/routes``                          → 201（body 见 :class:`RouteSpec`）
- ``PUT  /v1/routes/{name}``                   → 200（**必须带当前 version**）
- ``DELETE /v1/routes/{name}``                 → 204，级联清理路由级插件实例
  （实测：路由删除后版本化 CR 中的对应 matchRule 同步消失，无孤儿）
- ``PUT/GET/DELETE /v1/routes/{r}/plugin-instances/{p}``
  → 200，实例模型：``{pluginName, scope: "ROUTE", target, enabled,
  configurations}``；Console 负责序列化进版本化 WasmPlugin CR
  （如 ``wasmplugins.mcp-server-2.0.1``），并处理同名规则折叠等细节

🔴 Console 2.2.4 的「不存在路由」语义（实测两处，2026-10-15）：

- ``GET /v1/routes/{name}`` 不存在 → **404**
- 实例端点（PUT/GET/DELETE plugin-instances）路由不存在 → **400**
  ``ValidationException: Unknown route: {name}``（不是 404！）

因此 :meth:`get_route_plugin`/:meth:`delete_route_plugin`（以及 unpublish
先删路由再摘实例的写序）必须把 400 "Unknown route" 译为 :class:`GatewayUnknownRoute`
（:class:`GatewayNotFound` 子类）才能表达「目标不存在」——否则服务下线
（路由已删）后的任何实例操作都会伪装成服务端故障抛 502。
- ``GET /v1/service-sources``                  → 200（mcpbridge 服务来源的
  Console 视图；nacos2 型的 namespace/group 在 ``properties`` 里）

路由 destinations 与 Console 服务名形态（实测三型全对齐）::

    nacos2: summer-laac.DEFAULT-GROUP.local.nacos:48084
    static: octop-pilot.static:8088
    dns:    mcp-hub.dns:8090

即 destination 去掉尾部 ``:port`` 就是 ``services[].name``，端口就是
``services[].port``（:func:`split_destination`）。

凭据纪律与 nacos 客户端同款：**fail-closed** —— 缺凭证拒绝一切网关写操作。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx


class GatewayClientError(RuntimeError):
    """Console 调用失败（网络/认证/业务拒绝），message 携带服务端原文."""


class GatewayNotFound(GatewayClientError):
    """目标资源不存在（HTTP 404）——由调用方决定是否容忍."""


class GatewayUnknownRoute(GatewayNotFound):
    """Console 对**不存在路由**上的实例端点返回 400（非 404，实测 2.2.4）.

    语义上等价于 404：调用方按「目标不存在」容忍。识别依据是响应体里的
    ``Unknown route`` 字样（com.alibaba.higress.sdk.exception.ValidationException）。
    """


@dataclass(frozen=True)
class RouteSpec:
    """一条 MCP 路由的最小形状（与 Console Route 模型对齐）."""

    name: str
    path: str  # matchType=PRE 的前缀，如 /api/internal/mcp/summer-laac
    service_name: str  # 如 summer-laac.DEFAULT-GROUP.local.nacos
    service_port: int
    weight: int = 100

    def to_request(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "domains": [],
            "path": {"matchType": "PRE", "matchValue": self.path},
            "services": [
                {"name": self.service_name, "port": self.service_port, "weight": self.weight}
            ],
        }


def split_destination(destination: str) -> tuple[str, int]:
    """``higress.io/destination`` 形态 → (Console 服务名, 端口).

    实测三型统一（见模块 docstring）。端口缺失视为配置错误（fail-closed）。
    """
    name, sep, port = destination.rpartition(":")
    if not sep or not name or not port.isdigit():
        raise GatewayClientError(
            f"destination '{destination}' 不含合法的 ':port' 后缀，无法映射 Console 服务名"
        )
    return name, int(port)


class HigressConsoleClient:
    """Console REST 客户端：会话管理 + 信封解包 + 领域操作."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._logged_in = False
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "HigressConsoleClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ session

    def _login(self) -> None:
        resp = self._http.post(
            "/session/login", json={"username": self._username, "password": self._password}
        )
        if resp.status_code != 201:
            raise GatewayClientError(
                f"console login failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        self._logged_in = True

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        """一次调用 = 认证自愈（401 重登一次）+ 信封解包（fail-closed）."""
        for attempt in (0, 1):
            if not self._logged_in:
                self._login()
            resp = self._http.request(method, path, json=body)
            if resp.status_code == 401 and attempt == 0:
                self._logged_in = False
                continue
            break
        if resp.status_code == 404:
            if allow_404:
                return None
            raise GatewayNotFound(f"console: {method} {path} → 404")
        if resp.status_code >= 400:
            # Console 对不存在路由上的实例端点返回 400 而非 404（见模块
            # docstring）——按「目标不存在」翻译，让调用方的容忍逻辑生效。
            if resp.status_code == 400 and "Unknown route" in resp.text:
                raise GatewayUnknownRoute(
                    f"console: {method} {path} → route not found: {resp.text[:200]}"
                )
            raise GatewayClientError(
                f"console: {method} {path} → HTTP {resp.status_code} {resp.text[:300]}"
            )
        if resp.status_code == 204 or not resp.content:
            return None
        envelope = resp.json()
        if isinstance(envelope, dict) and envelope.get("success") is False:
            raise GatewayClientError(
                f"console: {method} {path} rejected: {envelope.get('message')}"
            )
        return envelope.get("data") if isinstance(envelope, dict) else envelope

    # ------------------------------------------------------------------ routes

    def get_route(self, name: str) -> dict[str, Any] | None:
        return self._request("GET", f"/v1/routes/{name}", allow_404=True)

    def ensure_route(self, spec: RouteSpec) -> str:
        """存在则带乐观锁更新（冲突自动重取一次），不存在则创建。

        返回 ``"created"`` / ``"updated"``。
        """
        existing = self.get_route(spec.name)
        if existing is None:
            self._request("POST", "/v1/routes", body=spec.to_request())
            return "created"
        body = spec.to_request()
        for attempt in (0, 1):
            body["version"] = existing.get("version")
            try:
                self._request("PUT", f"/v1/routes/{spec.name}", body=body)
                return "updated"
            except GatewayClientError:
                # 版本冲突等瞬时拒绝：重读一次 version 再试（仅一次）
                if attempt == 1:
                    raise
                existing = self.get_route(spec.name)
                if existing is None:  # 并发删除：转创建
                    self._request("POST", "/v1/routes", body=spec.to_request())
                    return "created"
        return "updated"  # pragma: no cover — 不可达（循环内必 return 或 raise）

    def delete_route(self, name: str) -> bool:
        """删除路由。Console 会级联清理路由级插件实例（实测）。不存在返回 False."""
        try:
            self._request("DELETE", f"/v1/routes/{name}")
            return True
        except GatewayNotFound:
            return False

    # ------------------------------------------------- route-level plugin instances

    def get_route_plugin(self, route: str, plugin: str) -> dict[str, Any] | None:
        """路由级插件实例的 ``configurations``；未挂载或路由不存在返回 None."""
        try:
            data = self._request(
                "GET", f"/v1/routes/{route}/plugin-instances/{plugin}", allow_404=True
            )
        except GatewayNotFound:
            # 路由不存在（Console 用 400 "Unknown route" 表达，见模块 docstring）
            return None
        if not isinstance(data, dict):
            return None
        if not data.get("enabled"):
            return None
        return data.get("configurations")

    def put_route_plugin(self, route: str, plugin: str, configurations: dict[str, Any]) -> None:
        body = {
            "pluginName": plugin,
            "scope": "ROUTE",
            "target": route,
            "enabled": True,
            "configurations": configurations,
        }
        self._request("PUT", f"/v1/routes/{route}/plugin-instances/{plugin}", body=body)

    def delete_route_plugin(self, route: str, plugin: str) -> bool:
        """摘除路由级插件实例。未挂载或路由不存在返回 False."""
        try:
            self._request("DELETE", f"/v1/routes/{route}/plugin-instances/{plugin}")
            return True
        except GatewayNotFound:
            # 404（未挂载）或 400 "Unknown route"（路由不存在）都不算失败
            return False

    # ------------------------------------------------------------------ sources

    def list_service_sources(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/v1/service-sources")
        if not isinstance(data, list):
            raise GatewayClientError("console: /v1/service-sources 未返回列表")
        return data


_CLIENT_CACHE: dict[tuple[str, str, str], HigressConsoleClient] = {}


def gateway_client_from_env() -> HigressConsoleClient:
    """从环境变量构造，凭据 **fail-closed**（与 nacos 凭证纪律同款）.

    按 (addr, username, password) 缓存单例：httpx.Client 线程安全且自带
    连接池复用，登录态经 401 自愈维护——admin 端点每请求新建客户端会让
    连接池泄漏。env 变更产生新客户端，缓存不串。

    - ``MCP_CATALOG_GATEWAY_ADDR``     Console 基址（如 http://higress-console:8080；
      ACK all-in-one 为 http://higress.default.svc.cluster.local:8001）
    - ``MCP_CATALOG_GATEWAY_USERNAME`` 默认 ``admin``
    - ``MCP_CATALOG_GATEWAY_PASSWORD`` 缺失 → 拒绝一切网关配置操作
    """
    addr = os.environ.get("MCP_CATALOG_GATEWAY_ADDR")
    if not addr:
        raise GatewayClientError("MCP_CATALOG_GATEWAY_ADDR is not set")
    password = os.environ.get("MCP_CATALOG_GATEWAY_PASSWORD")
    if not password:
        raise GatewayClientError(
            "MCP_CATALOG_GATEWAY_PASSWORD is not set; refusing gateway config "
            "operations without credentials (fail-closed)"
        )
    username = os.environ.get("MCP_CATALOG_GATEWAY_USERNAME", "admin")
    key = (addr, username, password)
    client = _CLIENT_CACHE.get(key)
    if client is None:
        client = HigressConsoleClient(addr, username, password)
        _CLIENT_CACHE[key] = client
    return client
