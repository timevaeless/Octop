"""Publish pipeline: catalog rows → Higress gateway objects (Console API).

v3 (2026-10-15 拍板): Octop 下发配置统一走 Higress **Console REST 通道**
（``gateway_client.HigressConsoleClient``，模拟人操作页面）——不感知、不依赖
Higress 的配置存储选型。每个服务 = 一条路由 + 路由级插件实例（实例模型），
不再做跨服务的整份配置读-合并-写：同名规则折叠等 CR 序列化细节由 Console
负责（实测 DELETE 路由会级联清理实例规则，无孤儿）。

编码自 summer-cmc 试点、仍然成立的规则：

1. **每服务一条路由** — mcp-server 插件按路由 destination 转发（长前缀优先），
   工具 URL 保持相对路径；
2. **mcp-server 实例**：``tools`` 挂在路由级实例的 ``configurations`` 顶层
   （嵌套 ``server.tools`` 会让整个 listener 挂掉——该语义随实例模型保持
   不变，``build_plugin_tool`` 输出形状原样沿用）；
3. **ext-auth 实例**：配置取自冻结的线上策略模板
   （:data:`_EXT_AUTH_DEFAULT_RULE_CONFIG`，fail-closed：FAIL_CLOSE + 403）。
   旧数据面下的"同名配置折叠 bug"规避逻辑（单规则追加 ingress）随 dataId
   通道一并退役——Console 的实例模型本身就是按路由管理的。

``build_*`` / role 相关函数是纯函数（unit-testable）；``CatalogPublisher``
编排 Console 调用与 mcp-hub ACL 分发，支持 dry-run。

mcp-hub ACL（``mcphub.tool-acl.json``）是 hub watcher 轮询的**分发通道**，
不是网关配置——仍走 nacos 客户端（``acl_client``），与本通道解耦。
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable
from typing import Any

from octop.mcp_catalog.gateway_client import RouteSpec, split_destination
from octop.mcp_catalog.models import McpServiceRow, McpToolRow
from octop.mcp_catalog.sources import parse_service_sources

logger = logging.getLogger(__name__)

# v2 P2.3 gate (design §3.2): only registry types whose destination format is
# live-verified may be published. 'nacos2' verified (echo-demo/summer-cmc live
# ingresses); 'static' verified (octop-pilot live ingress: {name}.static:{port}
# with the SERVICE row's port). dns/nacos3/eureka/zk/consul stay experimental —
# the publisher refuses them until a probe validates the format.
VERIFIED_REGISTRY_TYPES = frozenset({"nacos2", "static"})

_LEGACY_REGISTRY_NAME = "nacos"


def resolve_registry(client: Any, registry_name: str) -> dict[str, Any]:
    """Resolve one registry entry by name from the LIVE service sources.

    ``client`` is a gateway client exposing ``list_service_sources()`` (the
    Console view of the mcpbridge registries). Fail-closed: an unknown
    registry name raises — the caller must never guess a destination for a
    registry it cannot see.
    """
    for entry in parse_service_sources(client.list_service_sources()):
        if entry["name"] == registry_name:
            return entry
    raise ValueError(f"registry '{registry_name}' not found in live service sources")


def registry_is_verified(registry_type: str) -> bool:
    return registry_type in VERIFIED_REGISTRY_TYPES


def build_plugin_tool(tool: McpToolRow) -> dict[str, Any]:
    """One ``tools[]`` entry for the mcp-server plugin config.

    Arg placement: the plugin's field is ``position`` (query/header/path/body/
    cookie) — the catalog stores the same concept as ``location`` and it is
    translated here (verified live 2026-09-25: the plugin ignores unknown
    keys, and an explicit ``body`` template overrides ``position: body``).
    Body/query templates reference values as ``{{.args.<name>}}``.
    """
    args = [
        {**p, "position": p.pop("location", "query")} if "location" in p else p
        for p in tool.parameters
    ]
    entry: dict[str, Any] = {
        "name": tool.tool_name,
        "description": tool.description,
        "args": args,
    }
    template: dict[str, Any] = {"method": tool.http_method, "url": tool.http_path}
    if tool.request_body is not None:
        template["body"] = tool.request_body
        template["headers"] = [{"key": "Content-Type", "value": "application/json"}]
    if tool.response_body:
        template["responseTemplate"] = {"body": tool.response_body}
    entry["requestTemplate"] = template
    return entry


# Review round-4 §3: the ext-auth configuration below is the verified live
# policy (nacos higress-system/wasmplugins.ext-auth, snapshot 2026-09-25)
# frozen as a constant: forward_auth to mcp-hub /authz with FAIL_CLOSE and
# status_on_error 403. It is applied verbatim as each route's ext-auth plugin
# instance. Do not hand-edit without re-verifying against the live config —
# a drifted template would silently change the authn/authz policy of every
# MCP route.
_EXT_AUTH_DEFAULT_RULE_CONFIG: dict[str, Any] = {
    "http_service": {
        "endpoint_mode": "forward_auth",
        "endpoint": {"service_name": "mcp-hub.dns", "service_port": 8090, "path": "/authz"},
        "request_method": "POST",
        "timeout": 1000,
        "authorization_request": {"allowed_headers": [{"exact": "authorization"}]},
        "authorization_response": {
            "allowed_upstream_headers": [
                {"exact": "x-octop-uid"},
                {"exact": "x-octop-oid"},
                {"exact": "x-octop-sid"},
                # 2026-10-08: identity passthrough for business operators —
                # hub echoes the delegated token's ``ssu`` claim back as this
                # header; downstream /mcp-api filters (e.g. laac) resolve the
                # real operator from it and fail closed without it
                # (1010000175 "MCP 操作人身份缺失"). Must stay in lockstep with
                # the hub's response headers.
                {"exact": "x-octop-ssu"},
                {"exact": "x-envoy-allow-mcp-tools"},
            ]
        },
    },
    "failure_mode_allow": False,
    "status_on_error": 403,
    # 2026-10-15 (laac 401 missing_token 事故): mcp-server 插件把 tools/call
    # 翻译成 REST 后经网关内部回环转发（/mcp-api/...，无 Authorization——身份
    # 已由 x-octop-* 头注入）。这条回环若再被 ext-auth 拦截（典型场景：切通
    # 道后旧的无版本 CR 残留双重实例），hub /authz 会以 missing_token 401 拒绝，
    # 且错误体被插件包装成工具级 "call failed, status: 401"，极难排查。白名单
    # 显式声明：内部 REST 转发不做 ext-auth——入口鉴权与内部转发是两件事。
    # 网关上不存在 /mcp-api 对外路由，豁免不打开未鉴权入口。
    "match_type": "whitelist",
    "match_list": [{"match_rule_path": "/mcp-api/", "match_rule_type": "prefix"}],
}


def effective_tool_roles(
    tool_roles: list[str] | None, svc_roles: list[str] | None
) -> list[str]:
    """The role keys one tool is reachable by, after the 2026-10-01 subset
    rule (user decision): a tool's explicit configuration can only ever be a
    SUBSET of the service-level default — never a widening of it.

    * empty tool config → inherit the service default (033 semantics);
    * ``'*'`` in the service default → wildcard superset, tool config stands;
    * otherwise → intersection (previously an explicit config *overrode* the
      default, which let a tool grant a role the service itself never granted
      — the ambiguity this rule removes). Fail-closed: an empty intersection
      leaves the tool with no roles at all.
    """
    explicit = list(tool_roles or [])
    default = list(svc_roles or [])
    if not explicit:
        return default
    if "*" in default:
        return explicit
    return [r for r in explicit if r in default]


def validate_tool_roles_subset(
    svc_roles: list[str] | None, tool_roles: list[str] | None
) -> None:
    """Reject a tool configuration reaching outside the service default.

    Raises ``ValueError`` naming the offending keys; the API layers translate
    that into 400. ``'*'`` in the service default is the wildcard superset.
    """
    explicit = list(tool_roles or [])
    default = list(svc_roles or [])
    if not explicit or "*" in default:
        return
    outside = [r for r in explicit if r not in default]
    if outside:
        raise ValueError(
            "tool roles must be a subset of the service-level roles "
            f"({', '.join(default) or 'empty'}): {', '.join(outside)}"
        )


def prune_tools_to_service_roles(
    repo: Any, service_id: str, svc_roles: list[str] | None
) -> list[str]:
    """Re-scope every explicit tool config to the (new) service default.

    Called when the service-level roles shrink: the narrowing intent is
    blanket — a tool keeping a revoked role would silently re-widen access.
    Returns the pruned tool names (intersection-empty tools become
    role-less, i.e. unreachable until the service grants again).
    """
    pruned: list[str] = []
    for tool in repo.list_tools(service_id):
        scoped = effective_tool_roles(tool.allowed_roles, svc_roles)
        if tool.allowed_roles and scoped != list(tool.allowed_roles):
            fields = {
                "service_id": service_id,
                "tool_name": tool.tool_name,
                "description": tool.description,
                "http_method": tool.http_method,
                "http_path": tool.http_path,
                "parameters": tool.parameters,
                "request_body": tool.request_body,
                "response_body": tool.response_body,
                "allowed_roles": scoped,
                "enabled": tool.enabled,
                "source": tool.source,
            }
            repo.upsert_tool(fields)
            pruned.append(tool.tool_name)
    return pruned


def collect_acl_map(
    tools: list[McpToolRow], service_default_roles: list[str] | None = None
) -> dict[str, list[str]]:
    """role → [tool_name] projection for hub ToolAcl (["*"] grants everyone).

    2026-10-01 subset rule: a tool with an EMPTY allowed_roles inherits
    ``service_default_roles``; an explicit tool configuration is intersected
    with the default (never widens it). Both empty = no entry for that tool —
    hub's ToolAcl then denies everyone for it (unchanged fail-closed).
    """
    acl: dict[str, list[str]] = {}
    default = list(service_default_roles or [])
    for t in tools:
        if not t.enabled:
            continue
        for role in effective_tool_roles(t.allowed_roles, default):
            acl.setdefault(role, []).append(t.tool_name)
    return dict(sorted(acl.items()))


class CatalogPublisher:
    def __init__(
        self,
        gateway: Any,
        acl_client: Any,
        *,
        acl_data_id: str = "mcphub.tool-acl.json",
    ) -> None:
        self._gateway = gateway  # HigressConsoleClient（路由/插件实例）
        self._acl_client = acl_client  # nacos 客户端（仅 mcp-hub ACL 分发通道）
        self._acl_data_id = acl_data_id

    @staticmethod
    def _fence(fence: Callable[[], None] | None) -> None:
        """Run the publish-lock fencing check before every external write.

        ``None`` keeps the publisher usable without a lock (tests, dry runs).
        The fence raises RuntimeError when the lease was lost mid-publish —
        a second publisher has taken over and continuing here would clobber
        its state (review 2026-09-26 P1-10).
        """
        if fence is not None:
            fence()

    def _destination(self, service: McpServiceRow) -> str:
        """Dispatch the destination by the service's registry entry.

        The default 'nacos' keeps the legacy nacos2 path with NO registry
        lookup; any other registry must resolve in the live service sources
        AND be type-verified, else the publish refuses (fail-closed gate).
        """
        if service.registry_name == _LEGACY_REGISTRY_NAME:
            return service.gateway_destination()
        registry = resolve_registry(self._gateway, service.registry_name)
        rtype = str(registry.get("type") or "")
        if not registry_is_verified(rtype):
            raise ValueError(
                f"registry '{service.registry_name}' has type '{rtype}' whose "
                f"destination format is not live-verified yet "
                f"(verified: {sorted(VERIFIED_REGISTRY_TYPES)}); refusing to "
                "publish — run the probe validation first (design §3.2)"
            )
        return service.gateway_destination(
            registry_type=rtype, registry_name=service.registry_name
        )

    def publish(
        self,
        service: McpServiceRow,
        tools: list[McpToolRow],
        acl_map: dict[str, list[str]] | None = None,
        *,
        dry_run: bool = False,
        fence: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Compute (and unless ``dry_run``, write) all derived gateway objects.

        ``acl_map`` is the GLOBAL role→tools projection across all published
        services (computed by the caller, which owns the source-of-truth DB).
        It is distributed via the mcp-hub ACL dataId — a *distribution
        channel*, not storage: hub hot-reloads it with zero restart.

        Write order (Console constraint, live-verified 2026-10-15): the
        Console REFUSES plugin-instance PUTs targeting a nonexistent route
        (400 "Unknown route"), so the route must exist before the instances
        — the inverse of the old dataId order. The coverage-first invariant
        is preserved by COMPENSATION instead: a failure while attaching
        instances/ACL to a route this call CREATED rolls the route back, so
        the failure terminal state stays "no route, everything else intact"
        — never "route live, authz missing". A failure on an already
        existing (updated) route keeps the old semantics: mixed old/new
        state, repairable by reconcile.
        """
        if acl_map is None:
            # 033: the fallback projection honors the service default roles —
            # the router paths always pass an explicit acl_map; this keeps the
            # pure-function path consistent for direct callers/tests.
            acl_map = collect_acl_map(tools, service.allowed_roles)
        destination = self._destination(service)
        service_name, service_port = split_destination(destination)
        spec = RouteSpec(
            name=service.ingress_name,
            path=service.resolved_mcp_path,
            service_name=service_name,
            service_port=service_port,
        )

        tool_entries = [build_plugin_tool(t) for t in tools if t.enabled]
        mcp_action: str = "set" if tool_entries else "cleared"
        ext_config = copy.deepcopy(_EXT_AUTH_DEFAULT_RULE_CONFIG)

        detail: dict[str, Any] = {
            "route": {
                "name": spec.name,
                "path": spec.path,
                "service": {"name": spec.service_name, "port": spec.service_port},
                "action": None,
            },
            "plugins": {"ext-auth": "set", "mcp-server": mcp_action},
            "acl": dict(acl_map),
        }
        existing = self._gateway.get_route(spec.name)
        route_created = existing is None
        detail["route"]["action"] = "created" if route_created else "updated"

        if not dry_run:
            self._fence(fence)
            self._gateway.ensure_route(spec)
            try:
                self._fence(fence)
                self._gateway.put_route_plugin(spec.name, "ext-auth", ext_config)
                self._fence(fence)
                if tool_entries:
                    self._gateway.put_route_plugin(
                        spec.name,
                        "mcp-server",
                        {
                            "server": {"name": service.service_id, "type": "rest"},
                            "tools": tool_entries,
                        },
                    )
                else:
                    # 无启用工具 → 清掉残留实例（旧数据面下等价于移除该服务的 rule）
                    self._gateway.delete_route_plugin(spec.name, "mcp-server")
                self._fence(fence)
                # hub ACL：分发通道而非存储——hub watcher 轮询此 dataId 热更新
                self._acl_client.publish_config(
                    self._acl_data_id, json.dumps(acl_map, ensure_ascii=False, indent=2)
                )
            except Exception:
                # 补偿（coverage-first 不变量的载体）：刚创建的路由回滚删除，
                # 失败终态回到"无路由"。Console 的 DELETE 级联清理实例规则。
                if route_created:
                    try:
                        self._gateway.delete_route(spec.name)
                    except Exception:  # noqa: BLE001 — 原异常优先，回滚失败仅记录
                        logger.exception(
                            "compensation failed: route %s may leak without coverage",
                            spec.name,
                        )
                raise
        detail["written"] = not dry_run
        return detail

    def push_acl(
        self,
        acl_map: dict[str, list[str]],
        *,
        fence: Callable[[], None] | None = None,
    ) -> None:
        """Re-distribute ONLY the hub ACL dataId (no gateway writes).

        Review 2026-09-30 P1-4: an ACL-only edit (tenant resource-acl on an
        mcp-services row) must reach the hub — the hub hot-reloads this
        dataId, so re-pushing just the projection converges within one
        watcher interval without a full re-publish. Idempotent nacos write;
        run under the shared publish lock (the projection covers ALL
        services).
        """
        self._fence(fence)
        self._acl_client.publish_config(
            self._acl_data_id, json.dumps(acl_map, ensure_ascii=False, indent=2)
        )

    def unpublish(
        self,
        service: McpServiceRow,
        remaining_acl: dict[str, list[str]] | None = None,
        *,
        dry_run: bool = False,
        fence: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Remove the service's route AND its coverage: delete the route
        first (Console cascades the route-scoped plugin instance rules —
        verified live), then explicitly clear both plugin instances
        (tolerating absence), and (via ``remaining_acl``) retract the tools
        from hub's allow table.

        Order (round-2 N4, preserved): the ROUTE goes FIRST — if a later
        step fails the failure mode is "route gone, everything else intact"
        (safe no-op state), never "route live, authz gone".
        """
        detail: dict[str, Any] = {}
        route_name = service.ingress_name
        detail["route_delete"] = {"name": route_name, "deleted": False}
        detail["plugins"] = {"ext-auth": False, "mcp-server": False}

        if not dry_run:
            self._fence(fence)
            detail["route_delete"]["deleted"] = self._gateway.delete_route(route_name)
            self._fence(fence)
            detail["plugins"]["ext-auth"] = self._gateway.delete_route_plugin(
                route_name, "ext-auth"
            )
            self._fence(fence)
            detail["plugins"]["mcp-server"] = self._gateway.delete_route_plugin(
                route_name, "mcp-server"
            )
            self._fence(fence)
            if remaining_acl is not None:
                self._acl_client.publish_config(
                    self._acl_data_id,
                    json.dumps(remaining_acl, ensure_ascii=False, indent=2),
                )
        detail["written"] = not dry_run
        return detail
