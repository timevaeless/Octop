"""Naming queries against registry backends (v2 P3.1).

The service form's AutoComplete data source: list service names registered
in one mcpbridge registry entry, and resolve one service's instance (ip/port)
for auto-fill.

通道选型（2026-10-15 拍板）：nacos 型来源统一走 **Higress Console 聚合视图**
（``GET /v1/services``，mcpbridge 的权威投影）——不再直连 nacos。原因：
① nacos 直查硬编码 v3 admin API，MSE nacos2 服务端没有该端点（与 ACL 通道
退役同源）；② v3 退役形态下 octop 本就不再配 nacos 地址。Console 视图带
endpoints（健康实例），单服务详情端点不存在（实测 404），列表即权威。

Console 服务名形态（实测 2.2.4）：nacos 型 = ``{service}.{group}.{namespace}.nacos``，
dns/static 型 = ``{service}.{type}``。过滤 = 类型后缀 + group/namespace 中缀
匹配。⚠️ mcpbridge 会对 group 段做 ``-``/``_`` 规范化（实测来源记录
``DEFAULT_GROUP`` → 全名段 ``DEFAULT-GROUP``），故中缀比较走 :func:`_norm_seg`
归一化。多 nacos 来源归一后仍同名时无法区分——UX 预填可接受，注释明示。

legacy nacos 直查（v3 admin API）与 eureka 直查保留在下方——前者服务于
仍配了 nacos 地址的存量环境，后者 eureka 来源不经 Console 聚合。

Failures raise ``NamingError`` → the endpoint 502s and the frontend falls
back to manual input. NEVER return fabricated options.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

_TIMEOUT_SECONDS = 10


class NamingError(RuntimeError):
    pass


# ---- higress console（mcpbridge 聚合视图，nacos 型来源的主通道）----


def _console_service_rows(client: Any) -> list[dict[str, Any]]:
    """Console 聚合服务列表 → dict 行列表；连接失败译为 NamingError。"""
    from octop.mcp_catalog.gateway_client import GatewayClientError

    try:
        rows = client.list_services()
    except GatewayClientError as e:
        raise NamingError(f"console service list failed: {e}") from e
    return [r for r in rows if isinstance(r, dict) and r.get("name")]


def _split_nacos_fullname(fullname: str) -> tuple[str, str, str] | None:
    """``{service}.{group}.{namespace}.nacos`` → (service, group, namespace)。

    service 名本身含点时 rsplit 仍取对尾两段（group/namespace 是单段）；
    非三段结构（如裸 ``x.nacos``）返回 None —— 交由前缀回退兜底。
    """
    if not fullname.endswith(".nacos"):
        return None
    parts = fullname[: -len(".nacos")].rsplit(".", 2)
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def _norm_seg(seg: str) -> str:
    """mcpbridge 全名段与来源记录的 group/namespace 归一化比较键。

    实测（本地靶场 2.2.4，2026-10-15）：来源记录 ``DEFAULT_GROUP`` 在服务
    全名里表现为 ``DEFAULT-GROUP`` —— mcpbridge 对段内 ``_``/``-`` 做了
    规范化，严格相等会静默漏配。
    """
    return seg.replace("-", "_").strip().lower()


def list_console_services(
    client: Any, *, namespace: str, group: str
) -> list[str]:
    """某 nacos 来源（group/namespace）下的裸服务名列表（表单预填用）。"""
    want_ns, want_g = _norm_seg(namespace), _norm_seg(group)
    names: set[str] = set()
    for row in _console_service_rows(client):
        parsed = _split_nacos_fullname(str(row["name"]))
        if parsed is None:
            continue
        svc, g, ns = parsed
        if _norm_seg(g) == want_g and _norm_seg(ns) == want_ns:
            names.add(svc)
    return sorted(names)


def console_instance(
    client: Any, *, namespace: str, group: str, service_name: str
) -> dict[str, Any]:
    """某服务的实例（ip/port）——Console 视图的 endpoints 即健康实例。

    匹配序：先精确匹配（服务段原样相等 + group/namespace 归一化相等）；
    未命中回退「服务段相等 + ``.nacos`` 尾缀」的第一条（容忍来源记录里
    group/namespace 与注册实例漂移）。都没有 → NamingError。
    """
    rows = _console_service_rows(client)
    want_g, want_ns = _norm_seg(group), _norm_seg(namespace)

    def _matches(row_name: str) -> tuple[str, str, str] | None:
        parsed = _split_nacos_fullname(row_name)
        if parsed is None or parsed[0] != service_name:
            return None
        svc, g, ns = parsed
        return parsed if (_norm_seg(g) == want_g and _norm_seg(ns) == want_ns) else None

    row = next(
        (r for r in rows if _matches(str(r["name"])) is not None), None
    )
    if row is None:
        row = next(
            (
                r
                for r in rows
                if str(r["name"]).startswith(f"{service_name}.")
                and str(r["name"]).endswith(".nacos")
            ),
            None,
        )
    if row is None:
        raise NamingError(
            f"no instance registered for '{service_name}' in {namespace}/{group}"
        )
    endpoints = [e for e in (row.get("endpoints") or []) if e]
    if not endpoints:
        raise NamingError(
            f"service '{service_name}' has no live endpoint in {namespace}/{group}"
        )
    return {
        "ip": str(endpoints[0]),
        "port": int(row["port"]) if row.get("port") is not None else None,
        "namespace": namespace,
        "group": group,
    }


# ---- legacy nacos 直查（v3 admin API；仅存量配置了 nacos 地址的环境）----


def list_nacos_services(
    client: Any, *, namespace: str, group: str, page_size: int = 200
) -> list[str]:
    """Registered service names under one nacos namespace/group."""
    payload = client.admin_get(
        "/nacos/v3/admin/ns/service/list",
        {"namespaceId": namespace, "groupName": group, "pageNo": 1, "pageSize": page_size},
    )
    data = payload.get("data")
    items = data.get("pageItems") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise NamingError(f"nacos service list returned unexpected shape: {payload!r:.200}")
    names: set[str] = set()
    for it in items:
        if isinstance(it, dict) and it.get("name"):
            names.add(str(it["name"]))
        elif isinstance(it, str) and it:
            names.add(it)  # tolerate a bare-name list shape
    return sorted(names)


def nacos_instance(
    client: Any, *, namespace: str, group: str, service_name: str
) -> dict[str, Any]:
    """First healthy instance (else first instance) of one nacos service."""
    payload = client.admin_get(
        "/nacos/v3/admin/ns/instance/list",
        {"namespaceId": namespace, "groupName": group, "serviceName": service_name},
    )
    data = payload.get("data")
    hosts = data if isinstance(data, list) else (data or {}).get("pageItems")
    if not isinstance(hosts, list) or not hosts:
        raise NamingError(f"no instance registered for '{service_name}' in {namespace}/{group}")
    hosts = [h for h in hosts if isinstance(h, dict)]
    healthy = [h for h in hosts if h.get("healthy")]
    pick = (healthy or hosts)[0]
    return {
        "ip": str(pick.get("ip") or ""),
        "port": int(pick["port"]) if pick.get("port") is not None else None,
        "namespace": namespace,
        "group": group,
    }


# ---- eureka ----


def _eureka_get(domain: str, path: str) -> dict[str, Any]:
    url = f"http://{domain.rstrip('/')}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            return json.load(resp)  # type: ignore[no-any-return]
    except (OSError, ValueError) as e:
        raise NamingError(f"eureka query failed: {e}") from e


def list_eureka_services(domain: str) -> list[str]:
    """Application names from ``/eureka/apps`` (eureka stores them UPPERCASED)."""
    doc = _eureka_get(domain, "/eureka/apps")
    apps = (doc.get("applications") or {}).get("application")
    if apps is None:
        apps = (doc.get("applications") or {}).get("application")  # keep shape tolerance
    if isinstance(apps, dict):  # single-app registry quirk
        apps = [apps]
    if not isinstance(apps, list):
        raise NamingError(f"eureka /apps returned unexpected shape")
    names = {str(a["name"]) for a in apps if isinstance(a, dict) and a.get("name")}
    return sorted(names)


def eureka_instance(domain: str, service_name: str) -> dict[str, Any]:
    """First UP instance of one eureka application.

    Eureka matches paths case-sensitively on the uppercased name; try the
    name as given first, then uppercased (the form may carry either).
    """
    doc: dict[str, Any] | None = None
    last_error: Exception | None = None
    for candidate in dict.fromkeys([service_name, service_name.upper()]):
        try:
            doc = _eureka_get(domain, f"/eureka/apps/{candidate}")
            break
        except NamingError as e:
            last_error = e
    app = ((doc or {}).get("application") or {})
    instances = app.get("instance")
    if isinstance(instances, dict):
        instances = [instances]
    if not isinstance(instances, list) or not instances:
        raise NamingError(
            f"no instance registered for '{service_name}' in eureka"
        ) from last_error
    up = [i for i in instances if isinstance(i, dict) and str(i.get("status")) == "UP"]
    pick = (up or instances)[0]
    port = pick.get("port")
    port_value = port.get("$") if isinstance(port, dict) else port
    return {
        "ip": str(pick.get("hostName") or pick.get("ipAddr") or ""),
        "port": int(port_value) if port_value is not None else None,
        "namespace": None,
        "group": None,
    }
