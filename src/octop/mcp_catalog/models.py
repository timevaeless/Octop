"""Row dataclasses for the MCP catalog tables (mirrors migration 021)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from octop.infra.db.repos._base import DbRow


def _load_json_list(raw: object) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "[]")
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


@dataclass(frozen=True)
class McpServiceRow:
    service_id: str
    display_name: str
    description: str
    nacos_namespace: str
    nacos_group: str
    nacos_service_name: str
    backend_port: int
    openapi_url: str
    mcp_path: str | None
    status: str  # draft | published | disabled
    created_at: int
    updated_at: int
    # Which mcpbridges.default registry entry backs this service (v2 P2).
    # 'nacos' = legacy path: nacos2 destination without a bridge lookup,
    # byte-identical to pre-P2 publishes. Anything else is resolved against
    # the live bridge and gated on VERIFIED_REGISTRY_TYPES at publish time.
    registry_name: str = "nacos"
    # 033: service-level default roles — the fallback for tools whose OWN
    # allowed_roles is empty. A tool with an explicit configuration (including
    # ["*"]) overrides this; both empty stays fail-closed (no ACL entry).
    allowed_roles: list[str] = field(default_factory=list)
    # 034: three-state attribution — NULL = platform-private (unattributed),
    # '*' = platform share, a tenant id = hard boundary. Decides WHO edits the
    # ACL (platform catalog vs /api/org/{org}/resource-acl/mcp-services), not
    # what passes — that stays with allowed_roles.
    org_id: str | None = None

    @classmethod
    def from_row(cls, r: DbRow) -> McpServiceRow:
        path = r["mcp_path"]
        registry = r["registry_name"] if "registry_name" in r.keys() else "nacos"
        svc_roles = r["allowed_roles"] if "allowed_roles" in r.keys() else []
        org = r["org_id"] if "org_id" in r.keys() else None
        return cls(
            service_id=str(r["service_id"]),
            display_name=str(r["display_name"]),
            description=str(r["description"] or ""),
            nacos_namespace=str(r["nacos_namespace"]),
            nacos_group=str(r["nacos_group"]),
            nacos_service_name=str(r["nacos_service_name"]),
            backend_port=int(r["backend_port"]),
            openapi_url=str(r["openapi_url"] or ""),
            mcp_path=str(path) if path is not None else None,
            status=str(r["status"]),
            created_at=int(r["created_at"]),
            updated_at=int(r["updated_at"]),
            registry_name=str(registry or "nacos"),
            allowed_roles=[str(x) for x in _load_json_list(svc_roles)],
            org_id=str(org) if org is not None else None,
        )

    @property
    def resolved_mcp_path(self) -> str:
        return self.mcp_path or f"/api/internal/mcp/{self.service_id}"

    @property
    def ingress_name(self) -> str:
        return f"{self.service_id}-mcp"

    def nacos_destination(self) -> str:
        """Legacy nacos2 destination — kept byte-identical for the default
        ``registry_name='nacos'`` path (pre-P2 format, live-verified)."""
        return self.gateway_destination(registry_type="nacos2")

    def gateway_destination(
        self, *, registry_type: str = "nacos2", registry_name: str | None = None
    ) -> str:
        """Higress destination, dispatched by mcpbridge registry type (v2 P2).

        Formats (live-calibrated 2026-09-29 against nacos3 ingresses):
        - nacos2/nacos3: ``name.GROUP'.namespace.{registry}:{port}``
          (group underscores → hyphens, case preserved);
        - static: ``{registry}.static:{port}`` — the octop-pilot live ingress
          proves the port is the SERVICE row's backend_port, NOT the registry
          entry's port (design table corrected by live evidence);
        - dns: ``{registry}.dns:{port}`` (旁证: ext-auth ``mcp-hub.dns``);
        - anything else (eureka/zk/consul): ``{service}.{registry}:{port}`` —
          unverified shape, publish-gated until probe validation.
        """
        rname = registry_name or self.registry_name
        if registry_type in ("nacos2", "nacos3"):
            group = self.nacos_group.replace("_", "-")
            return (
                f"{self.nacos_service_name}.{group}.{self.nacos_namespace}.{rname}"
                f":{self.backend_port}"
            )
        if registry_type == "static":
            return f"{rname}.static:{self.backend_port}"
        if registry_type == "dns":
            return f"{rname}.dns:{self.backend_port}"
        return f"{self.nacos_service_name}.{rname}:{self.backend_port}"


@dataclass(frozen=True)
class McpToolRow:
    id: int
    service_id: str
    tool_name: str
    description: str
    http_method: str
    http_path: str
    parameters: list[dict[str, Any]]  # parsed from parameters_json
    request_body: str | None
    response_body: str | None
    allowed_roles: list[str]  # parsed from allowed_roles; ["*"] = all roles
    enabled: bool
    created_at: int
    updated_at: int
    source: str = (
        "manual"  # 'openapi' = import-managed (pruned on re-import); 'manual' = never auto-deleted
    )

    @classmethod
    def from_row(cls, r: DbRow) -> McpToolRow:
        return cls(
            id=int(r["id"]),
            service_id=str(r["service_id"]),
            tool_name=str(r["tool_name"]),
            description=str(r["description"] or ""),
            http_method=str(r["http_method"]).upper(),
            http_path=str(r["http_path"]),
            parameters=_load_json_list(r["parameters_json"]),
            request_body=str(r["request_body"]) if r["request_body"] is not None else None,
            response_body=str(r["response_body"]) if r["response_body"] is not None else None,
            allowed_roles=[str(x) for x in _load_json_list(r["allowed_roles"])],
            enabled=bool(int(r["enabled"])),
            source=str(r["source"] or "manual"),
            created_at=int(r["created_at"]),
            updated_at=int(r["updated_at"]),
        )


@dataclass(frozen=True)
class PublishLogRow:
    id: int
    service_id: str
    action: str  # publish | unpublish | dry_run
    result: str  # ok | error
    detail: str  # JSON payload
    actor: str
    created_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> PublishLogRow:
        return cls(
            id=int(r["id"]),
            service_id=str(r["service_id"]),
            action=str(r["action"]),
            result=str(r["result"]),
            detail=str(r["detail"] or ""),
            actor=str(r["actor"] or ""),
            created_at=int(r["created_at"]),
        )
