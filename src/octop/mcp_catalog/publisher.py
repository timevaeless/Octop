"""Publish pipeline: catalog rows → nacos configs (ingress / plugins).

Encodes the three hard-won gateway rules from the summer-cmc pilot:

1. **Ingress per backend service** — the mcp-server plugin forwards by route
   destination, *not* by tool URL host, so every service needs its own ingress
   (longer prefix wins) and tool URLs stay relative.
2. **mcp-server plugin**: ``tools`` is read from the *top level* of a rule's
   config (a nested ``server.tools`` makes the whole listener fail with
   "type not registered"); each service gets its own matchRule.
3. **ext-auth**: identical configs across matchRules get collapsed by the
   controller and the later rule is *dropped*, so additional ingresses are
   appended to the single existing rule's ``ingress`` array instead.

``build_*`` functions are pure (unit-testable); ``CatalogPublisher`` performs
get-merge-publish against nacos, with dry-run support.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

from octop.mcp_catalog.models import McpServiceRow, McpToolRow
from octop.mcp_catalog.sources import parse_bridge_registries

# v2 P2.3 gate (design §3.2): only registry types whose destination format is
# live-verified may be published. 'nacos2' verified (echo-demo/summer-cmc live
# ingresses); 'static' verified (octop-pilot live ingress: {name}.static:{port}
# with the SERVICE row's port). dns/nacos3/eureka/zk/consul stay experimental —
# the publisher refuses them until a probe validates the format.
VERIFIED_REGISTRY_TYPES = frozenset({"nacos2", "static"})

_LEGACY_REGISTRY_NAME = "nacos"


def resolve_registry(client: Any, registry_name: str) -> dict[str, Any]:
    """Resolve one registry entry by name from the LIVE mcpbridges.default.

    Fail-closed: a missing bridge or an unknown registry name raises — the
    caller must never guess a destination for a registry it cannot see.
    """
    raw = client.get_config("mcpbridges.default")
    if raw is None:
        raise ValueError(
            f"mcpbridges.default not found — cannot resolve registry '{registry_name}'"
        )
    for entry in parse_bridge_registries(raw):
        if entry["name"] == registry_name:
            return entry
    raise ValueError(f"registry '{registry_name}' not found in mcpbridges.default")


def registry_is_verified(registry_type: str) -> bool:
    return registry_type in VERIFIED_REGISTRY_TYPES

_INGRESS_TEMPLATE = """apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  annotations:
    higress.io/destination: {destination}
  labels:
    higress.io/domain_higress-default-domain: "true"
    higress.io/resource-definer: higress
  name: {ingress_name}
  namespace: higress-system
spec:
  ingressClassName: higress
  rules:
  - http:
      paths:
      - backend:
          resource:
            apiGroup: networking.higress.io
            kind: McpBridge
            name: default
        path: {mcp_path}
        pathType: Prefix
"""


def build_ingress_yaml(service: McpServiceRow, *, destination: str | None = None) -> str:
    return _INGRESS_TEMPLATE.format(
        destination=destination or service.gateway_destination(),
        ingress_name=service.ingress_name,
        mcp_path=service.resolved_mcp_path,
    )


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


def merge_plugin_rule(
    plugin_config: dict[str, Any], service: McpServiceRow, tools: list[McpToolRow]
) -> dict[str, Any]:
    """Merge this service's tools into the wasmplugins.mcp-server config (mutates)."""
    rules = plugin_config.setdefault("spec", {}).setdefault("matchRules", [])
    ingress_name = service.ingress_name
    tool_entries = [build_plugin_tool(t) for t in tools if t.enabled]

    rule = next((r for r in rules if ingress_name in (r.get("ingress") or [])), None)
    if rule is None:
        rule = {"ingress": [ingress_name]}
        rules.append(rule)
    if not tool_entries:
        rules.remove(rule)  # nothing to expose → remove the stale rule entirely
        return plugin_config
    rule["config"] = {
        "server": {"name": service.service_id, "type": "rest"},
        "tools": tool_entries,
    }
    return plugin_config


def merge_ext_auth_rule(ext_auth_config: dict[str, Any], service: McpServiceRow) -> dict[str, Any]:
    """Add the service's ingress to the single ext-auth matchRule (mutates).

    The rule's config is the authn/authz policy shared by every MCP ingress;
    per the collapse bug we never create a second identical rule.

    Review round-2 A: with more than one rule the collapse bug (higress
    controller drops later same-config matchRules) means we cannot know which
    rule is authoritative — appending to the wrong one would publish the
    service WITHOUT authn/authz. Fail loudly instead: the operator must
    collapse the config by hand.

    Review round-4 §3: an EMPTY rule list used to raise too, which deadlocked
    every subsequent publish — unpublish drops the rule once its last ingress
    is removed (``ingress: []`` semantics on higress are unverified), so after
    removing the last service no publish could ever succeed again. Rebuild
    ONE rule from the frozen live template instead (fail-closed by
    construction: failure_mode_allow=false) and extend it with this service.
    """
    rules = ext_auth_config.setdefault("spec", {}).setdefault("matchRules", [])
    if len(rules) > 1:
        raise ValueError(
            "ext-auth config has multiple matchRules — cannot tell which one "
            "is authoritative (higress collapse bug); refusing to guess"
        )
    if not rules:
        rules.append({"ingress": [], "config": copy.deepcopy(_EXT_AUTH_DEFAULT_RULE_CONFIG)})
    ingress_name = service.ingress_name
    first = rules[0]
    ingress_list = first.setdefault("ingress", [])
    if isinstance(ingress_list, str):
        ingress_list = [ingress_list]
        first["ingress"] = ingress_list
    if ingress_name not in ingress_list:
        ingress_list.append(ingress_name)
    return ext_auth_config


# Review round-4 §3: the ext-auth matchRule ``config`` below is the verified
# live policy (nacos higress-system/wasmplugins.ext-auth, snapshot 2026-09-25)
# frozen as a constant: forward_auth to mcp-hub /authz with FAIL_CLOSE and
# status_on_error 403. It is the single source for rebuilding the rule when
# unpublish has emptied the matchRule list (see merge_ext_auth_rule). Do not
# hand-edit without re-verifying against the live config — a drifted template
# would silently change the authn/authz policy of every MCP route.
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
    def __init__(self, client: Any, *, acl_data_id: str = "mcphub.tool-acl.json") -> None:
        self._client = client
        self._acl_data_id = acl_data_id

    @staticmethod
    def _fence(fence: Callable[[], None] | None) -> None:
        """Run the publish-lock fencing check before every nacos write.

        ``None`` keeps the publisher usable without a lock (tests, dry runs).
        The fence raises RuntimeError when the lease was lost mid-publish —
        a second publisher has taken over and continuing here would clobber
        its merged snapshot with our stale one (review 2026-09-26 P1-10).
        """
        if fence is not None:
            fence()

    def publish(
        self,
        service: McpServiceRow,
        tools: list[McpToolRow],
        acl_map: dict[str, list[str]] | None = None,
        *,
        dry_run: bool = False,
        fence: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Compute (and unless ``dry_run``, write) all derived configs.

        ``acl_map`` is the GLOBAL role→tools projection across all published
        services (computed by the caller, which owns the source-of-truth DB).
        It is written as the mcp-hub ACL dataId — a *distribution channel*,
        not storage: hub hot-reloads it with zero restart and zero storage;
        the nacos copy is always regenerable from the catalog DB.
        """
        if acl_map is None:
            # 033: the fallback projection honors the service default roles —
            # the router paths always pass an explicit acl_map; this keeps the
            # pure-function path consistent for direct callers/tests.
            acl_map = collect_acl_map(tools, service.allowed_roles)
        # v2 P2.3: dispatch the destination by the service's registry entry —
        # the default 'nacos' keeps the legacy nacos2 path with NO bridge
        # lookup (byte-compat for every pre-P2 service and all existing
        # tests); any other registry must resolve in the live bridge AND be
        # type-verified, else the publish refuses (fail-closed gate).
        destination: str
        if service.registry_name == _LEGACY_REGISTRY_NAME:
            destination = service.gateway_destination()
        else:
            registry = resolve_registry(self._client, service.registry_name)
            rtype = str(registry.get("type") or "")
            if not registry_is_verified(rtype):
                raise ValueError(
                    f"registry '{service.registry_name}' has type '{rtype}' whose "
                    f"destination format is not live-verified yet "
                    f"(verified: {sorted(VERIFIED_REGISTRY_TYPES)}); refusing to "
                    "publish — run the probe validation first (design §3.2)"
                )
            destination = service.gateway_destination(
                registry_type=rtype, registry_name=service.registry_name
            )
        detail: dict[str, Any] = {
            "ingress": {
                "dataId": f"ingresses.{service.ingress_name}",
                "content": build_ingress_yaml(service, destination=destination),
            },
            "acl": dict(acl_map),
        }

        plugin_raw = self._client.get_config("wasmplugins.mcp-server")
        ext_raw = self._client.get_config("wasmplugins.ext-auth")
        if plugin_raw is None or ext_raw is None:
            raise RuntimeError("wasmplugins.mcp-server / ext-auth not found in nacos")

        plugin_cfg = json.loads(plugin_raw)
        merge_plugin_rule(plugin_cfg, service, tools)
        detail["mcp_server"] = {
            "dataId": "wasmplugins.mcp-server",
            "content": json.dumps(plugin_cfg, ensure_ascii=False, indent=2),
        }

        ext_cfg = json.loads(ext_raw)
        merge_ext_auth_rule(ext_cfg, service)
        detail["ext_auth"] = {
            "dataId": "wasmplugins.ext-auth",
            "content": json.dumps(ext_cfg, ensure_ascii=False, indent=2),
        }

        if not dry_run:
            # order matters — the mirror image of unpublish (round-2 N4):
            # write the COVERAGE first (ext-auth → plugin → hub ACL), the
            # ROUTE last. If a middle step fails, the failure mode is "no
            # route yet, everything else intact" (safe no-op). The previous
            # order (ingress first) failed into "route live, ext-auth rule
            # not yet written" = an unauthenticated proxy to the backend —
            # exactly the state unpublish's ordering exists to avoid.
            self._fence(fence)
            self._client.publish_config("wasmplugins.ext-auth", detail["ext_auth"]["content"])
            self._fence(fence)
            self._client.publish_config("wasmplugins.mcp-server", detail["mcp_server"]["content"])
            self._fence(fence)
            # hub ACL：分发通道而非存储——hub watcher 轮询此 dataId 热更新
            self._fence(fence)
            self._client.publish_config(
                self._acl_data_id, json.dumps(acl_map, ensure_ascii=False, indent=2)
            )
            self._fence(fence)
            self._client.publish_config(
                f"ingresses.{service.ingress_name}",
                detail["ingress"]["content"],
                content_type="yaml",
            )
        detail["written"] = not dry_run
        return detail

    def push_acl(
        self,
        acl_map: dict[str, list[str]],
        *,
        fence: Callable[[], None] | None = None,
    ) -> None:
        """Re-distribute ONLY the hub ACL dataId (no ingress/plugin writes).

        Review 2026-09-30 P1-4: an ACL-only edit (tenant resource-acl on an
        mcp-services row) must reach the gateway — the hub hot-reloads this
        dataId, so re-pushing just the projection converges within one
        watcher interval without a full re-publish. Idempotent nacos write;
        run under the shared publish lock (the projection covers ALL
        services).
        """
        self._fence(fence)
        self._client.publish_config(
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
        """Remove the service's route AND its coverage: drop the plugin rule,
        shrink the ext-auth ingress array, delete the ingress dataId, and
        (via ``remaining_acl``) retract the tools from hub's allow table.

        ``remaining_acl`` is the recomputed global ACL after this service's
        removal (caller-side, from the DB); written so hub drops the removed
        tools from every role's allow list without a restart.
        """
        detail: dict[str, Any] = {}
        ingress_data_id = f"ingresses.{service.ingress_name}"

        plugin_raw = self._client.get_config("wasmplugins.mcp-server")
        ext_raw = self._client.get_config("wasmplugins.ext-auth")
        if plugin_raw is None or ext_raw is None:
            raise RuntimeError("wasmplugins.mcp-server / ext-auth not found in nacos")

        plugin_cfg = json.loads(plugin_raw)
        spec = plugin_cfg.setdefault("spec", {})
        rules = spec.get("matchRules", [])
        spec["matchRules"] = [
            r for r in rules if service.ingress_name not in (r.get("ingress") or [])
        ]
        detail["mcp_server"] = {
            "dataId": "wasmplugins.mcp-server",
            "content": json.dumps(plugin_cfg, ensure_ascii=False, indent=2),
        }

        ext_cfg = json.loads(ext_raw)
        kept_rules: list[dict[str, Any]] = []
        for rule in ext_cfg.get("spec", {}).get("matchRules", []):
            ingresses = rule.get("ingress") or []
            if service.ingress_name in ingresses:
                ingresses = [i for i in ingresses if i != service.ingress_name]
            if isinstance(rule.get("ingress"), list) and not ingresses:
                # drop fully-emptied rules (round-2 §5): ``ingress: []``
                # semantics on higress are unverified — if it means
                # "match everything", an emptied rule would put ext-auth on
                # every route of the gateway. Deleting the rule is the safe
                # interpretation; the next publish recreates it from the
                # frozen template (round-4 §3, see merge_ext_auth_rule —
                # without that rebuild this drop deadlocked all publishes).
                continue
            if isinstance(rule.get("ingress"), list):
                rule["ingress"] = ingresses
            kept_rules.append(rule)
        ext_cfg["spec"]["matchRules"] = kept_rules
        detail["ext_auth"] = {
            "dataId": "wasmplugins.ext-auth",
            "content": json.dumps(ext_cfg, ensure_ascii=False, indent=2),
        }

        if not dry_run:
            # order matters (round-2 N4): remove the ROUTE FIRST — if a later
            # step fails the failure mode is "route gone, everything else
            # intact" (safe no-op state). The previous order (authz strip →
            # route delete) failed into "route live, authz gone" = an
            # unauthenticated proxy to the backend. The intermediate window
            # where the route exists without coverage also disappears.
            self._fence(fence)
            detail["ingress_delete"] = {
                "dataId": ingress_data_id,
                "deleted": self._client.delete_config(ingress_data_id),
            }
            self._fence(fence)
            self._client.publish_config("wasmplugins.mcp-server", detail["mcp_server"]["content"])
            self._fence(fence)
            self._client.publish_config("wasmplugins.ext-auth", detail["ext_auth"]["content"])
            self._fence(fence)
            if remaining_acl is not None:
                self._client.publish_config(
                    self._acl_data_id, json.dumps(remaining_acl, ensure_ascii=False, indent=2)
                )
        detail["written"] = not dry_run
        return detail
