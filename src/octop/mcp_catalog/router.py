"""Admin API for the MCP catalog — mounted at ``/api/admin/mcp-catalog``.

Platform-admin only (mirrors roles.py reasoning: the catalog decides what the
whole MCP gateway exposes, so tenant-level permission must never reach it).
The repo is constructed lazily from ``server.services.db`` so this module
never appears in ``infra/db/services.py`` (upstream-merge hygiene).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from octop.api.deps import get_server, require_admin
from octop.infra.users.resource_acl import PLATFORM_SHARED_ORG
from octop.mcp_catalog import naming as naming_mod
from octop.mcp_catalog import openapi_import
from octop.mcp_catalog import sources as sources_mod
from octop.mcp_catalog.gateway_client import GatewayClientError, gateway_client_from_env
from octop.mcp_catalog.nacos_client import NacosConfigClient, NacosConfigError
from octop.mcp_catalog.publisher import (
    CatalogPublisher,
    build_plugin_tool,
    effective_tool_roles,
    global_acl,
    prune_tools_to_service_roles,
    registry_is_verified,
    resolve_registry,
    validate_tool_roles_subset,
)
from octop.mcp_catalog.repo import CatalogRepo

router = APIRouter()

logger = logging.getLogger(__name__)

# \Z, not $: Python's $ also matches just before a trailing newline, so
# "abc\n" would pass ^...$ and then break the ingress YAML (review round-6 §3).
_SERVICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,62}[a-z0-9]\Z")
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}\Z")
# Review hardening (2026-09-25): these four fields are formatted into the
# ingress YAML / destination string — a newline or quote would let a caller
# rewrite the route (YAML injection). Nacos names stay alphanumeric+_- ;
# the mcp_path must look like a plain URL path.
_NACOS_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_MCP_PATH_RE = re.compile(r"^/[A-Za-z0-9_\-./]{0,254}\Z")
# 阿里云 nacos 退役形态（2026-10-15 拍板）：MCP_CATALOG_NACOS_ADDR 未配置 →
# 网关配置照常走 Console，mcp-hub ACL 分发改经 octop 内部端点（hub 拉取），
# nacos 写入跳过（_NullAclClient）。缺省从 localhost 改为空——保留该缺省等于
# 强制所有部署配 nacos 凭证（_nacos_creds fail-closed），与退役方向矛盾。
_NACOS_ADDR = os.environ.get("MCP_CATALOG_NACOS_ADDR", "").strip()


class _NullAclClient:
    """ACL 分发通道 no-op 桩：无 nacos 时 hub 走 octop 内部端点拉取，
    dataId 写入静默跳过（通道缺位 ≠ 配置错误，见 internal_acl.py）。"""

    def publish_config(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102
        return None


def _acl_client() -> Any:
    """nacos 在配 → 真客户端（legacy 通道保留）；未配 → no-op 桩。"""
    if not _NACOS_ADDR:
        return _NullAclClient()
    return NacosConfigClient(_NACOS_ADDR, *_nacos_creds())


def _nacos_creds() -> tuple[str, str]:
    """Resolve nacos credentials lazily and FAIL-CLOSED.

    Review 2026-10-02 P1-2: the password used to default to ``"nacos"`` (and
    freeze at import time) — a missing env var then silently authenticated
    with well-known default credentials against whatever nacos the address
    points at. Now: no password → refuse to talk to nacos at all. Read on
    every call (not at import) so processes can pick the env up without a
    reimport.
    """
    password = os.environ.get("MCP_CATALOG_NACOS_PASSWORD")
    if not password:
        raise RuntimeError(
            "MCP_CATALOG_NACOS_PASSWORD is not set; refusing gateway config "
            "operations with default credentials (fail-closed)"
        )
    return os.environ.get("MCP_CATALOG_NACOS_USERNAME", "nacos"), password


def _repo(server: Any) -> CatalogRepo:
    return CatalogRepo(server.services.db)


def _publisher() -> CatalogPublisher:
    """网关配置走 Console 通道；nacos 客户端仅承载 mcp-hub ACL 分发通道
    （MCP_CATALOG_NACOS_ADDR 未配置时为 no-op 桩，hub 走 octop 拉取通道）."""
    return CatalogPublisher(gateway_client_from_env(), _acl_client())


@contextlib.contextmanager
def _publish_lock(repo: CatalogRepo) -> Iterator[Callable[[], None]]:
    """Serialize the nacos read-modify-write cycle with ONE global lock
    (review round-2 N1): the plugin configs and ACL dataId being merged are
    shared by ALL services — a per-service lock would let two different
    services interleave and silently drop each other's rules.

    The lease lives in mcp_locks with a TTL, so a crashed holder cannot block
    publishes forever. A busy lock surfaces as 409 (fail-closed) rather than
    letting two publishers interleave.

    Yields a *fence* callable (review 2026-09-26 P1-10): the publisher runs
    it before every nacos write. It renews the lease when (and only when)
    still ours, and raises RuntimeError once the lease expired and was taken
    over — the TTL(120s) can be shorter than a worst-case publish (6 nacos
    ops × 10s timeout × 1 retry), and without fencing the original holder
    would keep writing over the successor's merged snapshot.
    """
    lock_key = "mcp_publish:gateway"
    try:
        holder = repo.acquire_lock(lock_key)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    def _fence() -> None:
        if not repo.renew_lock(lock_key, holder):
            raise RuntimeError(
                "publish lock lost mid-flight: lease expired and was taken over"
            )

    try:
        yield _fence
    finally:
        repo.release_lock(lock_key, holder)


def _global_acl(
    repo: CatalogRepo, svc: Any | None = None, tools: list[Any] | None = None
) -> dict[str, list[str]]:
    """Role→tools projection across ALL published services' enabled tools.

    ``svc``/``tools`` override the DB view for that one service — publish
    calls this BEFORE flipping status to published, so the override MUST NOT
    gate on ``svc.status == "published"``: the DB query above excludes this
    service, and without the override a FIRST publish would write an empty
    ACL — which hub treats as "configured, deny all" (ToolAcl.replace) —
    leaving the freshly published service 403 for everyone until a second
    publish (review round-5 §2). unpublish passes ``tools=[]`` (no-op loop);
    reconcile passes the real tools of an already-published service (override
    equals the DB view, harmless).
    The catalog DB is the source of truth; the distribution channel (nacos
    dataId legacy / octop internal endpoint) is what mcp-hub hot-reloads.

    The no-override base is ``publisher.global_acl`` (shared with the
    internal ACL endpoint so both channels can never diverge).
    """
    acl: dict[str, set[str]] = {
        role: set(tset)
        for role, tset in global_acl(
            repo, exclude_service=svc.service_id if svc is not None else None
        ).items()
    }
    if svc is not None and tools is not None:
        svc_default = list(svc.allowed_roles or [])
        for tool in tools:
            if not tool.enabled:
                continue
            for role in effective_tool_roles(tool.allowed_roles, svc_default):
                acl.setdefault(role, set()).add(tool.tool_name)
    return {role: sorted(tset) for role, tset in sorted(acl.items())}


class ServiceBody(BaseModel):
    service_id: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1)
    description: str = ""
    nacos_namespace: str = "local"
    nacos_group: str = "DEFAULT_GROUP"
    nacos_service_name: str = Field(min_length=1)
    backend_port: int = Field(gt=0, le=65535)
    openapi_url: str = ""
    mcp_path: str | None = None
    registry_name: str = "nacos"  # v2 P2: mcpbridge registry entry name
    # 033: service-level default roles — inherited by tools left empty
    allowed_roles: list[str] = Field(default_factory=list)
    # 034: three-state attribution. Default '*' (platform share): the platform
    # admin's honest state of knowledge at creation time — they cannot know
    # future callers, and tenant role keys (``t_{org}_``) do not exist yet, so
    # the ACL decision is delegated to the tenant plane. ``None`` =
    # platform-private (admin-only tools such as ops utilities).
    org_id: str | None = "*"

    @field_validator("description", "openapi_url", mode="before")
    @classmethod
    def _null_to_empty(cls, v: Any) -> Any:
        # The refactored form (v2 P1) sends explicit null for cleared optional
        # text fields; pydantic only applies the "" default on ABSENCE, so an
        # explicit null must be coerced here (DB columns are NOT NULL DEFAULT '').
        return "" if v is None else v

    @field_validator("allowed_roles", mode="before")
    @classmethod
    def _null_to_empty_roles(cls, v: Any) -> Any:
        # Same null-coercion as above, for the roles list (list column).
        return [] if v is None else v


class ServicePatchBody(BaseModel):
    display_name: str | None = None
    description: str | None = None
    nacos_namespace: str | None = None
    nacos_group: str | None = None
    nacos_service_name: str | None = None
    backend_port: int | None = Field(default=None, gt=0, le=65535)
    openapi_url: str | None = None
    mcp_path: str | None = None
    status: str | None = None
    registry_name: str | None = None
    # 033: list (not optional-null) — PATCH sends [] to clear the defaults;
    # absent field means "no change" (exclude_none drops None)
    allowed_roles: list[str] | None = None
    # 034: attribution re-assignment. Handled via model_fields_set (not
    # exclude_none) so an explicit ``null`` (platform-private) is expressible.
    org_id: str | None = None


class ToolBody(BaseModel):
    tool_name: str = Field(min_length=2, max_length=64)
    description: str = ""
    http_method: str = "GET"
    http_path: str = Field(min_length=1)
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    request_body: str | None = None
    response_body: str | None = None
    allowed_roles: list[str] = Field(default_factory=list)
    enabled: bool = True


class ToolsBatchDeleteBody(BaseModel):
    tool_names: list[str] = Field(min_length=1)


class PublishBody(BaseModel):
    dry_run: bool = False


class ImportOpenApiBody(BaseModel):
    openapi_url: str | None = None  # default: the service's openapi_url
    # v2 P4.1: paste/upload channel — a raw OpenAPI JSON document, so import
    # works without the backend being able to reach the spec URL itself.
    openapi_body: str | None = None
    # v2 P4.2: preview — compute imported/overwritten/deleted WITHOUT writing.
    dry_run: bool = False


def _service_or_404(repo: CatalogRepo, service_id: str) -> Any:
    svc = repo.get_service(service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail=f"unknown service: {service_id}")
    return svc


def _validate_service_org(server: Any, org_id: str | None) -> str | None:
    """034: validate a service attribution value (same semantics as the
    resource-acl writes in roles.py): ``None`` (platform-private) and ``'*'``
    (platform share) are literals; anything else must name an existing tenant
    — active or not (``get_org_any``), so an admin may pre-assign to a paused
    tenant."""
    if org_id is None or org_id == "*":
        return org_id
    if server.services.org_repo.get_org_any(org_id) is None:
        raise HTTPException(status_code=400, detail=f"unknown org: {org_id!r}")
    return org_id


def _validate_gateway_fields(fields: dict[str, Any]) -> None:
    """Reject values that could break out of the ingress YAML template.

    Applied on create AND patch — the gateway fields are the injection
    surface (review P0-3): they are formatted into the ingress YAML and the
    nacos destination string.
    """
    checks = (
        ("nacos_namespace", _NACOS_NAME_RE, "nacos_namespace"),
        ("nacos_group", _NACOS_NAME_RE, "nacos_group"),
        ("nacos_service_name", _NACOS_NAME_RE, "nacos_service_name"),
        ("mcp_path", _MCP_PATH_RE, "mcp_path must start with '/' and use plain path characters"),
        # v2 P2: registry_name lands in the destination string — same
        # injection surface, same gate
        ("registry_name", _NACOS_NAME_RE, "registry_name"),
    )
    for key, pattern, msg in checks:
        value = fields.get(key)
        if value is not None and not pattern.match(str(value)):
            raise HTTPException(status_code=400, detail=f"invalid {msg}")


def _validate_openapi_url(url: str) -> None:
    """SSRF gate (review P0-4): urllib would happily fetch ``file://`` —
    restrict to http(s) with a non-empty host.

    Round-2 §5 noted private-network URLs still pass. That is deliberate:
    importing from *internal* services (e.g. ``summer-cmc:/v3/api-docs``) is
    this endpoint's entire purpose, and the endpoint is platform-admin only.
    Documented in HANDOFF instead of blocking.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="openapi_url must be an http(s) URL")


_HTTP_PATH_RE = re.compile(r"^/[A-Za-z0-9_\-./{}]*\Z")
_HTTP_PATH_TEMPLATE_RE = re.compile(r"\{\{\.[A-Za-z0-9_]+\}\}")


def _validate_http_path(path: str) -> None:
    """A malformed tool path (review round-2 §5) poisons the WHOLE
    wasmplugins.mcp-server config — one bad Go template breaks every tool.
    Keep it to plain path characters plus balanced ``{{.name}}`` templates.
    """
    if not _HTTP_PATH_RE.match(path):
        raise HTTPException(
            status_code=400,
            detail="http_path must start with '/' and use plain path characters",
        )
    if "{" in _HTTP_PATH_TEMPLATE_RE.sub("", path) or "}" in _HTTP_PATH_TEMPLATE_RE.sub("", path):
        raise HTTPException(
            status_code=400,
            detail="http_path templates must be balanced {{.name}} placeholders",
        )


_PARAM_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}\Z")
# query keys are appended to the URL query string verbatim by the plugin —
# they never enter a Go template, so ``page-size`` / ``filter.name`` /
# ``ids[]`` are legal there (round-5 §3: gating them on the identifier regex
# only killed importable tools, and one skipped op even retracted the old row)
_PARAM_QUERY_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,64}\Z")


def _validate_parameters(parameters: list[dict[str, Any]]) -> None:
    """Gate what can poison the plugin config: only names that are rendered
    into Go templates (path placeholders ``{{.name}}``, body property keys)
    need Go-template identifier syntax. Query parameters stay verbatim.
    """
    for p in parameters or []:
        if not isinstance(p, dict):
            raise HTTPException(status_code=400, detail="each parameter must be an object")
        location = str(p.get("location", "query"))
        if location not in ("path", "query", "body"):
            raise HTTPException(
                status_code=400, detail="parameter location must be path/query/body"
            )
        name = str(p.get("name", ""))
        if location == "query":
            if not _PARAM_QUERY_RE.match(name):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "query parameter name must be 1-64 chars without "
                        "whitespace or control characters"
                    ),
                )
        elif not _PARAM_NAME_RE.match(name):
            raise HTTPException(
                status_code=400,
                detail="path/body parameter names must be identifiers (letter first, alphanumeric/underscore)",
            )


# Review round-6 §2: request_body / response_body are rendered by the SAME Go
# text/template engine as http_path (docstring promise: one broken template
# poisons the whole wasmplugins.mcp-server config) — but they are the biggest
# template in the config and had no gate at all. Import-generated bodies are
# safe by construction (_build_body_template); this gate is for PUT /tools and
# any future write path. Conservative whitelist, not a full Go parser.
_GO_KEYWORDS = {
    "if",
    "else",
    "end",
    "range",
    "with",
    "template",
    "block",
    "define",
    "break",
    "continue",
}
_GO_FUNCS = {
    "and",
    "or",
    "not",
    "eq",
    "ne",
    "lt",
    "le",
    "gt",
    "ge",
    "len",
    "print",
    "printf",
    "println",
    "index",
    "true",
    "false",
    "nil",
}
_GO_ACTION_RE = re.compile(r"\{\{-?\s*(.*?)\s*-?\}\}", re.DOTALL)
# A comment action is a no-op only when it is exactly one closed comment:
# ``/* ... */`` with nothing trailing (P1-7 — anything else is a Go parse
# error that would poison the whole plugin config).
_GO_COMMENT_RE = re.compile(r"^/\*.*\*/\Z", re.DOTALL)


def _check_action_tokens(tokens: list[str], field: str) -> None:
    """Validate one action's tokens against the whitelist.

    Parenthesized subexpressions (``(eq .a 1)``, ``and (or .a .b) .c``) are
    legal Go — strip the parens and validate the identifier inside
    (round-8 §5: bare ``(eq`` tokens used to be rejected as unknown).
    Template variables (``$x``) are INTENTIONALLY rejected (round-4 N5: the
    plugin engine's support for ``{{$v := …}}`` assignments is unverified) —
    that also rejects the legal ``{{range $i, $v := .items}}`` form; this is
    a documented limitation, not a bug.
    """
    for tok in tokens:
        core = tok.strip("()")
        if core != tok and not core:
            continue  # pure '(' / ')' operators
        if (
            core.startswith(".")
            or (core.startswith('"') and core.endswith('"') and len(core) >= 2)
            or core in _GO_KEYWORDS
            or core in _GO_FUNCS
            or re.fullmatch(r"-?\d+(\.\d+)?", core)
            or re.fullmatch(r"[=!<>|&()+,\-]", core)
        ):
            continue
        raise HTTPException(
            status_code=400,
            detail=f"{field}: unknown template token '{tok}' (whitelist: keywords, "
            "and/or/not/eq/ne/lt/le/gt/ge/len/index/printf, .field refs, literals)",
        )


def _validate_go_template(body: str, field: str) -> None:
    """Gate a Go text/template body on the same terms as http_path:
    balanced ``{{ }}`` actions, every action starts with a known
    keyword/function or a ``.field`` reference, blocks nest (if/range/with
    need end), and literal braces outside actions balance (JSON bodies).
    Template variables (``$x``) stay rejected — the plugin engine's support
    for assignments is unverified (round-4 N5 decision).
    """
    if not body:
        return
    if body.count("{{") != body.count("}}"):
        raise HTTPException(status_code=400, detail=f"{field}: unbalanced {{{{ }}}}")
    stack: list[str] = []
    parts: list[str] = []
    pos = 0
    for m in _GO_ACTION_RE.finditer(body):
        parts.append(body[pos : m.start()])
        pos = m.end()
        action = m.group(1)
        # {{/* comment */}} is a legal Go template no-op (round-7 §3) — skip.
        # Review 2026-09-26 P1-7: only a *fully closed* comment with nothing
        # after ``*/`` is a no-op. An unclosed {{/* x }} is a Go parse error
        # ("unclosed comment"), and {{/* a */ badfunc .x}} fails Go's
        # "comment ends before closing delimiter" — both would poison the
        # whole plugin config, so they face the gate like any other token.
        if action.startswith("/*"):
            if not _GO_COMMENT_RE.match(action):
                raise HTTPException(
                    status_code=400,
                    detail=f"{field}: comment must be closed as {{{{/* */}}}} "
                    "with nothing after */",
                )
            continue
        tokens = action.split()
        if not tokens:
            raise HTTPException(status_code=400, detail=f"{field}: empty {{{{ }}}} action")
        head = tokens[0]
        if head in ("if", "range", "with"):
            stack.append(head)
            # review round-7 §3: the CONDITION is parsed by Go too — {{if @@@}}
            # / {{if (badfunc .x)}} are parse errors that poison the whole
            # plugin config, so condition tokens face the same whitelist
            _check_action_tokens(tokens[1:], field)
        elif head == "else":
            if not stack:
                raise HTTPException(status_code=400, detail=f"{field}: 'else' outside a block")
            if len(tokens) > 1:
                if tokens[1] != "if":
                    raise HTTPException(status_code=400, detail=f"{field}: 'else' supports only 'if'")
                _check_action_tokens(tokens[2:], field)
            # {{else if …}} closes with the SAME end as its parent if
            # (Go if/else-if chain) — deliberately NOT pushed
        elif head == "end":
            if not stack:
                raise HTTPException(status_code=400, detail=f"{field}: unmatched 'end'")
            stack.pop()
        else:
            _check_action_tokens(tokens, field)
    parts.append(body[pos:])
    literal = re.sub(r'"(?:\\.|[^"\\])*"', '""', "".join(parts))
    if literal.count("{") != literal.count("}"):
        raise HTTPException(
            status_code=400, detail=f"{field}: literal braces must balance (JSON body)"
        )
    if stack:
        raise HTTPException(
            status_code=400, detail=f"{field}: unclosed '{stack[-1]}' block (missing end)"
        )


def _check_path_conflict(repo: CatalogRepo, service_id: str, path: str) -> None:
    """Reject two services claiming the exact same gateway path.

    Review round-2 N2: the original bidirectional ``startswith`` check
    blocked the *live* topology — octop-pilot serves ``/api/internal/mcp``
    as its fallback while summer-cmc nests at ``/api/internal/mcp/summer-cmc``
    — and a PATCH to display_name would 409. Higress route matching is
    most-specific-wins for Prefix paths (verified live: summer-cmc traffic
    reaches summer-cmc, not the pilot fallback), so nesting is legal and
    only exact duplication is genuinely ambiguous.
    """
    for other in repo.list_services():
        if other.service_id == service_id:
            continue
        other_path = other.resolved_mcp_path
        if path == other_path:
            raise HTTPException(
                status_code=409,
                detail=(f"mcp_path '{path}' is already claimed by service '{other.service_id}'"),
            )


def _check_registry_live(fields: dict[str, Any]) -> None:
    """v2 P2: a non-default registry must exist in the LIVE mcpbridges.default
    — fail fast at write time (400 with the exact reason) instead of at
    publish time. The default 'nacos' keeps the legacy path (no lookup)."""
    name = fields.get("registry_name")
    if not name or name == "nacos":
        return
    try:
        resolve_registry(gateway_client_from_env(), str(name))
    except GatewayClientError as e:
        raise HTTPException(
            status_code=502, detail=f"cannot verify registry '{name}': {e}"
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/sources")
def list_sources(_: Any = Depends(require_admin())) -> dict[str, Any]:
    """Service sources for the form dropdown — LIVE-READ from Higress.

    v2 design (mcp-catalog-env-registry design §〇-2): the dropdown mirrors
    ``mcpbridges.default.spec.registries[]`` exactly; no snapshot is stored,
    no registry is managed here. Fail-closed: nacos unreachable / malformed
    bridge → 502, never a silently empty (or stale) list.
    Each entry carries the P2.3 verification flag: an unverified type is
    shown tagged 「实验」 and the publisher refuses to publish it.
    """
    try:
        sources = sources_mod.list_service_sources(gateway_client_from_env())
    except GatewayClientError as e:
        raise HTTPException(status_code=502, detail=f"higress sources unavailable: {e}") from e
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"service sources malformed: {e}") from e
    for s in sources:
        s["verified"] = registry_is_verified(str(s.get("type") or ""))
    return {"sources": sources}


def _registry_entry_or_error(registry_name: str) -> dict[str, Any]:
    try:
        return resolve_registry(gateway_client_from_env(), registry_name)
    except GatewayClientError as e:
        raise HTTPException(
            status_code=502, detail=f"cannot resolve registry '{registry_name}': {e}"
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/sources/{registry_name}/services")
def list_source_services(
    registry_name: str, _: Any = Depends(require_admin())
) -> dict[str, Any]:
    """v2 P3.1: registered service names in one registry — the form's
    AutoComplete source. Unsupported types answer 400 (the frontend falls
    back to manual input); backend failures answer 502 — never fabricated
    options."""
    entry = _registry_entry_or_error(registry_name)
    rtype = str(entry.get("type") or "")
    domain = str(entry.get("domain") or "")
    try:
        if rtype in ("nacos2", "nacos3"):
            if not _NACOS_ADDR:
                raise HTTPException(
                    status_code=400,
                    detail="MCP_CATALOG_NACOS_ADDR not configured — nacos naming unavailable",
                )
            client = NacosConfigClient(_NACOS_ADDR, *_nacos_creds())
            names = naming_mod.list_nacos_services(
                client,
                namespace=str(entry.get("nacos_namespace_id") or "public"),
                group=str(entry.get("nacos_group") or "DEFAULT_GROUP"),
            )
        elif rtype == "eureka":
            names = naming_mod.list_eureka_services(domain)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"naming query not supported for registry type '{rtype}'",
            )
    except NacosConfigError as e:
        raise HTTPException(status_code=502, detail=f"naming query failed: {e}") from e
    except naming_mod.NamingError as e:
        raise HTTPException(status_code=502, detail=f"naming query failed: {e}") from e
    return {"services": names, "registry_type": rtype}


@router.get("/sources/{registry_name}/services/{service_name}/instance")
def get_source_instance(
    registry_name: str, service_name: str, _: Any = Depends(require_admin())
) -> dict[str, Any]:
    """v2 P3.2: one service's instance (ip/port) for the form auto-fill."""
    entry = _registry_entry_or_error(registry_name)
    rtype = str(entry.get("type") or "")
    domain = str(entry.get("domain") or "")
    try:
        if rtype in ("nacos2", "nacos3"):
            if not _NACOS_ADDR:
                raise HTTPException(
                    status_code=400,
                    detail="MCP_CATALOG_NACOS_ADDR not configured — nacos naming unavailable",
                )
            client = NacosConfigClient(_NACOS_ADDR, *_nacos_creds())
            instance = naming_mod.nacos_instance(
                client,
                namespace=str(entry.get("nacos_namespace_id") or "public"),
                group=str(entry.get("nacos_group") or "DEFAULT_GROUP"),
                service_name=service_name,
            )
        elif rtype == "eureka":
            instance = naming_mod.eureka_instance(domain, service_name)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"naming query not supported for registry type '{rtype}'",
            )
    except NacosConfigError as e:
        raise HTTPException(status_code=502, detail=f"naming query failed: {e}") from e
    except naming_mod.NamingError as e:
        raise HTTPException(status_code=502, detail=f"naming query failed: {e}") from e
    return {"instance": instance}


@router.get("/services")
def list_services(
    _: Any = Depends(require_admin()), server: Any = Depends(get_server)
) -> dict[str, Any]:
    repo = _repo(server)
    tool_counts = repo.count_tools_by_service()
    return {
        "services": [
            {
                "service_id": s.service_id,
                "display_name": s.display_name,
                "description": s.description,
                "status": s.status,
                "nacos_namespace": s.nacos_namespace,
                "nacos_group": s.nacos_group,
                "nacos_service_name": s.nacos_service_name,
                "backend_port": s.backend_port,
                "openapi_url": s.openapi_url,
                "mcp_path": s.resolved_mcp_path,
                "registry_name": s.registry_name,
                "allowed_roles": s.allowed_roles,
                "org_id": s.org_id,
                "tool_count": tool_counts.get(s.service_id, 0),
            }
            for s in repo.list_services()
        ]
    }


@router.post("/services", status_code=201)
def create_service(
    body: ServiceBody, _: Any = Depends(require_admin()), server: Any = Depends(get_server)
) -> dict[str, Any]:
    repo = _repo(server)
    if not _SERVICE_ID_RE.match(body.service_id):
        raise HTTPException(status_code=400, detail="service_id must be a lowercase slug")
    if repo.get_service(body.service_id) is not None:
        raise HTTPException(status_code=409, detail="service_id already exists")
    _validate_gateway_fields(body.model_dump())
    _check_registry_live(body.model_dump())
    _check_path_conflict(
        repo, body.service_id, body.mcp_path or f"/api/internal/mcp/{body.service_id}"
    )
    fields = body.model_dump()
    fields["org_id"] = _validate_service_org(server, body.org_id)
    row = repo.create_service(fields)
    return {"service_id": row.service_id, "status": row.status}


@router.patch("/services/{service_id}")
def patch_service(
    service_id: str,
    body: ServicePatchBody,
    user: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    repo = _repo(server)
    _service_or_404(repo, service_id)
    fields = body.model_dump(exclude_none=True)
    if "status" in fields:
        # review 2.2: flipping status directly would let an unpublished (no
        # gateway config) service enter _global_acl — publish/unpublish are
        # the only writers of status
        raise HTTPException(
            status_code=400, detail="status is managed by the publish/unpublish endpoints"
        )
    if "org_id" in body.model_fields_set:
        # exclude_none already dropped an explicit null; re-add it here so
        # re-attributing to platform-private (None) stays expressible.
        fields["org_id"] = _validate_service_org(server, body.org_id)
    _validate_gateway_fields(fields)
    _check_registry_live(fields)
    _check_path_conflict(
        repo,
        service_id,
        fields.get("mcp_path") or repo.get_service(service_id).resolved_mcp_path,  # type: ignore[union-attr]
    )
    row = repo.update_service(service_id, fields)
    if "allowed_roles" in fields:
        # 2026-10-01 subset rule: narrowing the service default must pull the
        # explicit tool configs back into the subset (the narrowing intent is
        # blanket), and — for a published service — re-distribute the hub ACL.
        pruned = prune_tools_to_service_roles(repo, service_id, row.allowed_roles)  # type: ignore[union-attr]
        if getattr(row, "status", None) == "published":
            try:
                with _publish_lock(repo) as fence:
                    _publisher().push_acl(_global_acl(repo), fence=fence)
            except Exception as e:  # noqa: BLE001 — surfaced, not swallowed
                repo.log_publish(
                    service_id,
                    "acl-repush",
                    "error",
                    {"error": str(e), "pruned": pruned},
                    actor=user.username,
                )
                raise HTTPException(
                    status_code=502,
                    detail="roles updated but the hub ACL re-distribution failed; retry or run reconcile",
                ) from e
            repo.log_publish(
                service_id,
                "acl-repush",
                "ok",
                {"pruned": pruned},
                actor=user.username,
            )
    # Tenant attachments ride the service definition one-way (URL, roles,
    # display name) — refresh them on every edit so a re-publish never
    # leaves stale endpoints mounted.
    try:
        from octop.mcp_catalog.tenant_share import sync_service_attachments

        conn_svc = _connector_service(server)
        sync_service_attachments(conn_svc, conn_svc.catalog_attachment_repo(), row)
    except Exception:  # noqa: BLE001 — best-effort; the catalog edit already landed
        logger.exception("attachment sync failed for service %s", service_id)
    return {"service_id": row.service_id, "status": row.status}  # type: ignore[union-attr]


@router.delete("/services/{service_id}")
def delete_service(
    service_id: str, _: Any = Depends(require_admin()), server: Any = Depends(get_server)
) -> dict[str, Any]:
    repo = _repo(server)
    svc = _service_or_404(repo, service_id)
    if svc.status == "published":
        raise HTTPException(status_code=409, detail="unpublish before deleting a published service")
    repo.delete_service(service_id)
    try:
        from octop.mcp_catalog.tenant_share import delete_service_attachments

        delete_service_attachments(_connector_service(server)._repo, service_id)
    except Exception:  # noqa: BLE001 — best-effort cleanup
        logger.exception("attachment cleanup failed for deleted service %s", service_id)
    return {"deleted": service_id}


@router.get("/services/{service_id}/tools")
def list_tools(
    service_id: str, _: Any = Depends(require_admin()), server: Any = Depends(get_server)
) -> dict[str, Any]:
    repo = _repo(server)
    _service_or_404(repo, service_id)
    return {
        "tools": [
            {
                "tool_name": t.tool_name,
                "description": t.description,
                "http_method": t.http_method,
                "http_path": t.http_path,
                "parameters": t.parameters,
                "request_body": t.request_body,
                "response_body": t.response_body,
                "allowed_roles": t.allowed_roles,
                "enabled": t.enabled,
            }
            for t in repo.list_tools(service_id)
        ]
    }


def _validate_tool_body(body: ToolBody) -> None:
    """Shared gate for PUT /tools and the preview endpoint — one broken
    template poisons the whole wasmplugins.mcp-server config, so every
    write/preview path faces the same validation.
    """
    if not _TOOL_NAME_RE.match(body.tool_name):
        raise HTTPException(status_code=400, detail="tool_name must be snake_case")
    if body.http_method.upper() not in ("GET", "POST", "PUT", "DELETE"):
        raise HTTPException(status_code=400, detail="only GET/POST/PUT/DELETE are supported")
    _validate_http_path(body.http_path)
    _validate_parameters(body.parameters)
    # review round-6 §2: manual bodies hit the same Go template engine as the
    # path — a broken template poisons the whole plugin config
    if body.request_body:
        _validate_go_template(body.request_body, "request_body")
    if body.response_body:
        _validate_go_template(body.response_body, "response_body")


@router.put("/services/{service_id}/tools/{tool_name}")
def upsert_tool(
    service_id: str,
    tool_name: str,
    body: ToolBody,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    repo = _repo(server)
    svc = _service_or_404(repo, service_id)
    if tool_name != body.tool_name:
        raise HTTPException(status_code=400, detail="tool_name mismatch")
    _validate_tool_body(body)
    # 2026-10-01 subset rule: a tool may only ever narrow the service-level
    # roles, never widen them — otherwise "who can see the service" (the
    # connector gate) and "who can call this tool" diverge confusingly.
    try:
        validate_tool_roles_subset(svc.allowed_roles, body.allowed_roles)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    fields = body.model_dump()
    fields["http_method"] = fields["http_method"].upper()
    fields["service_id"] = service_id
    fields["source"] = "manual"  # hand-defined: import must never prune it
    row = repo.upsert_tool(fields)
    return {"tool_name": row.tool_name, "allowed_roles": row.allowed_roles}


@router.post("/services/{service_id}/tools/preview")
def preview_tool(
    service_id: str,
    body: ToolBody,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Preview the ``tools[]`` entry this DRAFT would render into the
    wasmplugins.mcp-server config — same ``build_plugin_tool`` used by
    publish (single source of truth, the frontend never re-implements the
    mapping). No DB write, no nacos write. Validation errors surface as 400
    so the UI can show the exact reason next to the draft.
    """
    repo = _repo(server)
    _service_or_404(repo, service_id)
    _validate_tool_body(body)
    return {"plugin_tool": build_plugin_tool(body)}


@router.delete("/services/{service_id}/tools/{tool_name}")
def delete_tool(
    service_id: str,
    tool_name: str,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    repo = _repo(server)
    _service_or_404(repo, service_id)
    if not repo.delete_tool(service_id, tool_name):
        raise HTTPException(status_code=404, detail="unknown tool")
    return {"deleted": tool_name}


@router.post("/services/{service_id}/tools/batch-delete")
def batch_delete_tools(
    service_id: str,
    body: ToolsBatchDeleteBody,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Batch delete the UI's multi-selected tools. Unknown names are skipped
    (stale selection after a concurrent edit must not fail the whole batch);
    the response lists what actually went away so the UI can toast the real
    count. Same caveat as the single delete: a published service needs a
    re-publish before the gateway stops serving these tools."""
    repo = _repo(server)
    _service_or_404(repo, service_id)
    deleted = repo.delete_tools(service_id, body.tool_names)
    return {"deleted": deleted}


@router.post("/services/{service_id}/import-openapi")
def import_openapi(
    service_id: str,
    body: ImportOpenApiBody,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    repo = _repo(server)
    svc = _service_or_404(repo, service_id)
    # v2 P4.1: inline document wins over the URL — an admin can paste/upload
    # a spec the backend cannot reach itself. Exactly one source is required.
    if body.openapi_body:
        try:
            doc = json.loads(body.openapi_body)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"openapi_body is not valid JSON: {e}") from e
    else:
        url = body.openapi_url or svc.openapi_url
        if not url:
            raise HTTPException(status_code=400, detail="no openapi_url on service and none provided")
        _validate_openapi_url(url)
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                doc = json.load(resp)
        except (OSError, ValueError) as e:
            raise HTTPException(status_code=502, detail=f"cannot fetch OpenAPI: {e}") from e
    try:
        report = openapi_import.import_openapi(doc)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    imported: list[str] = []
    overwritten: list[str] = []  # v2 P4.2: tools that already exist and would be replaced
    renamed: list[dict[str, Any]] = []  # cross-service bare-name collisions
    for t in report["tools"]:
        # review round-4 §6: imported tools are EXTERNAL input (the OpenAPI
        # document, served by any internal service) — apply the same
        # http_path / parameter gate as PUT /tools, otherwise one malformed
        # path poisons the whole wasmplugins.mcp-server config. An invalid
        # operation lands in ``skipped`` (nothing silently dropped) instead
        # of failing the whole import.
        try:
            # P1-8 (review 2026-09-26): imported tool names come from external
            # operationIds — apply the same whitelist as PUT /tools, otherwise
            # an over-long or digit-leading name violates the module invariant
            # and lands in ToolAcl / plugin config unvalidated.
            if not _TOOL_NAME_RE.match(t["tool_name"]):
                raise HTTPException(
                    status_code=400,
                    detail=f"tool_name '{t['tool_name']}' must be snake_case "
                    "(leading letter, 2-64 chars of [a-z0-9_])",
                )
            _validate_http_path(t["http_path"])
            _validate_parameters(t["parameters"])
            if t.get("request_body"):
                _validate_go_template(t["request_body"], "request_body")
            if t.get("response_body"):
                _validate_go_template(t["response_body"], "response_body")
        except HTTPException as e:
            report["skipped"].append(
                {
                    "path": t["http_path"],
                    "method": t["http_method"],
                    "reason": f"invalid:{e.detail}",
                }
            )
            continue
        # Cross-service bare-name uniqueness (review 2026-10-02 P1-3): the hub
        # ACL projects ``tool_name`` across ALL services, so two services may
        # not share a name — but springdoc docs collide constantly (every
        # bank-info controller exports ``list_banks``). Failing the whole
        # one-click import on each collision is unworkable; instead rename the
        # INCOMING side deterministically — the same ``_n`` convention
        # springdoc itself uses for duplicate operationIds. Uniqueness (the
        # security invariant) is preserved and the rename is reported; a
        # colliding name that already belongs to THIS service just upserts.
        original_name = t["tool_name"]
        squatter = repo.tool_name_owner(original_name)
        if squatter is not None and squatter != service_id:
            candidate = None
            for suffix in range(1, 50):
                renamed_candidate = f"{original_name}_{suffix}"
                if len(renamed_candidate) > 64:
                    break  # _TOOL_NAME_RE upper bound — renaming would violate it
                owner = repo.tool_name_owner(renamed_candidate)
                # free OR already ours: the latter makes re-imports idempotent
                # (the previous import's renamed row is found and upserted,
                # preserving its roles/enabled, instead of marching to _2)
                if owner is None or owner == service_id:
                    candidate = renamed_candidate
                    break
            if candidate is None:
                report["skipped"].append(
                    {
                        "path": t["http_path"],
                        "method": t["http_method"],
                        "reason": f"cross-service tool name collision on '{original_name}' "
                        f"(owned by {squatter!r}); no free '_<n>' rename within 64 chars",
                    }
                )
                continue
            renamed.append(
                {
                    "from": original_name,
                    "to": candidate,
                    "owned_by": squatter,
                }
            )
            t["tool_name"] = candidate

        existing = repo.get_tool(service_id, t["tool_name"])
        (overwritten if existing is not None else imported).append(t["tool_name"])
        if body.dry_run:
            continue  # v2 P4.2: preview — nothing written
        fields = {**t, "service_id": service_id, "allowed_roles": [], "source": "openapi"}
        # re-import keeps the admin's role assignments AND enabled flag — a
        # manually disabled tool must not be silently revived (review 2.3)
        if existing is not None:
            fields["allowed_roles"] = existing.allowed_roles
            fields["enabled"] = existing.enabled
        repo.upsert_tool(fields)
    # sync semantics (review 2.3): operations removed from the spec disappear
    # from the catalog too — but ONLY import-managed tools (round-2 N3):
    # hand-defined tools (source='manual', design §8.3) must survive re-import.
    # keep = the operations actually importable; an op rejected by the gateway
    # validation above is no longer importable, so its stale row is retracted
    spec_names = set(imported) | set(overwritten)
    if body.dry_run:
        # pure computation — NEVER call delete_imported_tools here, it writes
        managed = {
            t.tool_name for t in repo.list_tools(service_id) if t.source == "openapi"
        }
        deleted = sorted(managed - spec_names)
        return {
            "dry_run": True,
            "imported": imported,
            "overwritten": overwritten,
            "renamed": renamed,
            "skipped": report["skipped"],
            "deleted": deleted,
        }
    deleted = repo.delete_imported_tools(service_id, spec_names)
    return {
        "imported": imported,
        "overwritten": overwritten,
        "renamed": renamed,
        "skipped": report["skipped"],
        "deleted": deleted,
    }


@router.get("/services/{service_id}/publish-diff")
def publish_diff(
    service_id: str, _: Any = Depends(require_admin()), server: Any = Depends(get_server)
) -> dict[str, Any]:
    """v2 P4.3: tool-level change list BEFORE publish — the governance
    backstop for dropping nacos3's version tree (design §4.2).

    Desired side = the DB (enabled tools rendered by ``build_plugin_tool``,
    the exact function publish uses). Live side = the route-scoped
    mcp-server plugin instance's ``configurations`` read from the Console.
    Compared canonically (sort_keys JSON), so the diff is exactly what
    publish would change.
    """
    repo = _repo(server)
    svc = _service_or_404(repo, service_id)
    desired = {
        t.tool_name: build_plugin_tool(t) for t in repo.list_tools(service_id) if t.enabled
    }
    live_tools: dict[str, dict[str, Any]] = {}
    published = False
    try:
        configurations = gateway_client_from_env().get_route_plugin(
            svc.ingress_name, "mcp-server"
        )
    except GatewayClientError as e:
        raise HTTPException(
            status_code=502, detail=f"cannot read live plugin config: {e}"
        ) from e
    if configurations:
        published = True
        for entry in (configurations or {}).get("tools") or []:
            if isinstance(entry, dict) and entry.get("name"):
                live_tools[str(entry["name"])] = entry
    def canon(d):
        return json.dumps(d, sort_keys=True, ensure_ascii=False)
    added = sorted(n for n in desired if n not in live_tools)
    removed = sorted(n for n in live_tools if n not in desired)
    updated = sorted(
        n for n in desired if n in live_tools and canon(desired[n]) != canon(live_tools[n])
    )
    unchanged = sorted(n for n in desired if n in live_tools and n not in updated)
    return {
        "published": published,
        "added": added,
        "updated": updated,
        "removed": removed,
        "unchanged": unchanged,
    }


@router.post("/services/{service_id}/publish")
def publish_service(
    service_id: str,
    body: PublishBody,
    server: Any = Depends(get_server),
    user: Any = Depends(require_admin()),
    # Default None keeps direct positional calls (tests) source-compatible;
    # FastAPI still injects a real BackgroundTasks on every request.
    background_tasks: BackgroundTasks = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    repo = _repo(server)
    svc = _service_or_404(repo, service_id)
    if svc.status == "disabled":
        raise HTTPException(status_code=409, detail="service is disabled")
    tools = repo.list_tools(service_id)
    if not tools:
        raise HTTPException(status_code=409, detail="no tools to publish")
    # 2026-10-01 rule: a PLATFORM-SHARED service with no service-level roles
    # would publish into a dead end — tenant admins cannot widen a shared
    # row, so only the platform could ever repair the "nobody can use it"
    # state. Block it at the door; tenant-attributed services may publish
    # role-less (the owning tenant configures access afterwards).
    if svc.org_id == PLATFORM_SHARED_ORG and not svc.allowed_roles:
        raise HTTPException(
            status_code=400,
            detail="platform-shared services must carry service-level roles before publishing",
        )
    action = "dry_run" if body.dry_run else "publish"
    try:
        if body.dry_run:
            detail = _publisher().publish(svc, tools, _global_acl(repo, svc, tools), dry_run=True)
        else:
            # serialize gateway read-modify-write (review 2.7): two concurrent
            # publishes would silently drop each other's plugin rule. The ACL
            # and the tool list are (re)computed INSIDE the lock (review
            # round-4 §7): computing the ACL outside let a slower publisher
            # write its stale snapshot over a newer one — the N1 race,
            # relocated from the nacos config to the ACL dataId.
            with _publish_lock(repo) as fence:
                # locked snapshot (round-4 §7 / round-5 §6): re-read svc and
                # tools under the lock, mirroring unpublish
                svc = repo.get_service(service_id) or svc
                if svc.status == "disabled":
                    raise HTTPException(status_code=409, detail="service is disabled")
                tools = repo.list_tools(service_id)
                if not tools:
                    raise HTTPException(status_code=409, detail="no tools to publish")
                detail = _publisher().publish(
                    svc, tools, _global_acl(repo, svc, tools), dry_run=False, fence=fence
                )
                repo.update_service(service_id, {"status": "published"})
    except (RuntimeError, ValueError, json.JSONDecodeError) as e:
        repo.log_publish(service_id, action, "error", {"error": str(e)}, actor=user.username)
        raise HTTPException(status_code=502, detail=f"publish failed: {e}") from e
    repo.log_publish(
        service_id,
        action,
        "ok",
        {"acl": detail["acl"], "written": detail["written"]},
        actor=user.username,
    )
    if not body.dry_run:
        # T4 pluggable hook (octop.mcp_catalog.tenant_share): reactivate
        # attachments parked offline by a previous unpublish (same one-way
        # sync the service-edit path runs) so preserved mount keys remount.
        try:
            from octop.mcp_catalog.tenant_share import sync_service_attachments

            conn_svc = _connector_service(server)
            sync_service_attachments(conn_svc, conn_svc.catalog_attachment_repo(), svc)
        except Exception:  # noqa: BLE001 — best-effort; the publish already landed
            logger.exception("attachment reactivation failed for service %s", service_id)
        # T4 pluggable hook (octop.infra.agents.publish_refresh): drop the
        # stale tool-schema cache + rebuild mounting agents after republish.
        from octop.infra.agents.publish_refresh import schedule_refresh  # noqa: PLC0415

        schedule_refresh(server, service_id, background_tasks)
    return {
        "dry_run": body.dry_run,
        "acl": detail["acl"],
        "route": detail["route"],
        "plugins": detail["plugins"],
    }


@router.post("/services/{service_id}/unpublish")
def unpublish_service(
    service_id: str,
    body: PublishBody,
    server: Any = Depends(get_server),
    user: Any = Depends(require_admin()),
    background_tasks: BackgroundTasks = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    repo = _repo(server)
    svc = _service_or_404(repo, service_id)
    # P1-9 (review 2026-09-26): only a published service can be unpublished.
    # Without this guard, unpublishing a draft service that then fails in
    # nacos flipped the row to status="published" on the rollback path — a
    # service that never wrote any gateway config, with unreviewed roles,
    # entering the _global_acl published query (and every future publish).
    if svc.status != "published":
        raise HTTPException(status_code=409, detail="service is not published")
    action = "dry_run" if body.dry_run else "unpublish"
    try:
        if body.dry_run:
            remaining_acl = _global_acl(repo, svc, [])
            detail = _publisher().unpublish(svc, remaining_acl, dry_run=True)
        else:
            with _publish_lock(repo) as fence:
                # flip first so the recomputed ACL drops this service's tools
                repo.update_service(service_id, {"status": "draft"})
                svc = repo.get_service(service_id)
                if svc is None:
                    raise RuntimeError(f"service {service_id} vanished during unpublish")
                remaining_acl = _global_acl(repo, svc, [])
                detail = _publisher().unpublish(svc, remaining_acl, dry_run=False, fence=fence)
    except (RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
        repo.log_publish(service_id, action, "error", {"error": str(e)}, actor=user.username)
        # roll the status back so the DB still says published
        if not body.dry_run:
            repo.update_service(service_id, {"status": "published"})
        raise HTTPException(status_code=502, detail=f"unpublish failed: {e}") from e
    repo.log_publish(service_id, action, "ok", {"written": detail["written"]}, actor=user.username)
    # Unpublish tears the gateway endpoint down. Tenant attachments are
    # parked offline (rows kept, mount keys stable) instead of deleted, so
    # experts keep their default connector across an unpublish → publish
    # cycle; re-publishing reactivates them (see tenant_share module doc).
    if not body.dry_run:
        try:
            from octop.mcp_catalog.tenant_share import mark_service_attachments_offline

            parked = mark_service_attachments_offline(
                _connector_service(server).catalog_attachment_repo(), service_id
            )
            if parked:
                logger.info(
                    "unpublish parked %d tenant attachment(s) of %s offline",
                    parked,
                    service_id,
                )
        except Exception:  # noqa: BLE001 — gateway state already flipped
            logger.exception("attachment offline-park failed for service %s", service_id)
        # T4 pluggable hook (octop.infra.agents.publish_refresh): rebuild
        # agents still mounting the now-dead URL so their tool set drops it.
        from octop.infra.agents.publish_refresh import schedule_refresh  # noqa: PLC0415

        schedule_refresh(server, service_id, background_tasks)
    return {"dry_run": body.dry_run, "written": detail["written"]}


@router.post("/services/{service_id}/reconcile")
def reconcile_service(
    service_id: str,
    server: Any = Depends(get_server),
    user: Any = Depends(require_admin()),
    background_tasks: BackgroundTasks = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Re-derive every gateway config from the DB and push it (review 2.8).

    Recovers a half-published state (a crashed publish left a route with
    partial plugin instances): every write is an idempotent object PUT
    (route upsert, instance replace), so re-pushing the expected state
    converges. Status is not touched.
    """
    repo = _repo(server)
    svc = _service_or_404(repo, service_id)
    if svc.status != "published":
        raise HTTPException(status_code=409, detail="only published services can be reconciled")
    try:
        with _publish_lock(repo) as fence:
            # recompute the tool list and ACL inside the lock (review round-4 §7)
            tools = repo.list_tools(service_id)
            detail = _publisher().publish(
                svc, tools, _global_acl(repo, svc, tools), dry_run=False, fence=fence
            )
    except (RuntimeError, ValueError, json.JSONDecodeError) as e:
        repo.log_publish(service_id, "reconcile", "error", {"error": str(e)}, actor=user.username)
        raise HTTPException(status_code=502, detail=f"reconcile failed: {e}") from e
    repo.log_publish(
        service_id,
        "reconcile",
        "ok",
        {"acl": detail["acl"], "written": detail["written"]},
        actor=user.username,
    )
    # T4 pluggable hook (octop.infra.agents.publish_refresh): same semantics
    # as a publish — the re-pushed gateway config carries the current schema.
    from octop.infra.agents.publish_refresh import schedule_refresh  # noqa: PLC0415

    schedule_refresh(server, service_id, background_tasks)
    return {"reconciled": service_id, "acl": detail["acl"]}


@router.get("/services/{service_id}/publish-log")
def publish_log(
    service_id: str, _: Any = Depends(require_admin()), server: Any = Depends(get_server)
) -> dict[str, Any]:
    repo = _repo(server)
    _service_or_404(repo, service_id)
    return {
        "log": [
            {
                "action": r.action,
                "result": r.result,
                "actor": r.actor,
                "created_at": r.created_at,
                "detail": r.detail,
            }
            for r in repo.list_publish_log(service_id)
        ]
    }


# ---------------------------------------------------------------- connector attach
#
# v2 follow-up (2026-09-29): one-click "attach as connector" — a published
# catalog service becomes a custom MCP server in the ADMIN's OWN connector
# list with delegate_identity=True, so no manual re-typing in Connectors.
# The Higress ingress base URL is a deployment-topology fact (Higress and
# Octop may not share a docker network in production), so it is stored in
# the settings KV (key below), NOT hardcoded to the docker-gateway IP.


_HIGRESS_BASE_KEY = "mcp_catalog_higress_base"


class ConnectorSettingsBody(BaseModel):
    # Base URL only — e.g. "http://172.17.0.1" or "https://gw.example.com".
    # The per-service mcp_path is appended at attach time.
    higress_base: str


class AttachConnectorBody(BaseModel):
    display_name: str | None = None
    enabled: bool = True


def _settings_repo(server: Any) -> Any:
    return server.services.settings_repo


def _connector_service(server: Any) -> Any:
    # Same construction as api/routers/connectors.py — kept local so this
    # module stays out of infra/db/services.py (upstream-merge hygiene).
    from octop.infra.connectors.service import ConnectorService

    return ConnectorService(
        repo=server.services.repos.connector_repo,
        secret_repo=server.services.secret_repo,
        settings_repo=server.services.settings_repo,
        config=server.services.config,
    )


def _normalize_higress_base(url: str) -> str:
    """Accept only an http(s) origin (+optional path prefix), no trailing '/'."""
    text = (url or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail="higress_base must be an http(s) URL, e.g. http://172.17.0.1",
        )
    if parsed.query or parsed.fragment:
        raise HTTPException(status_code=400, detail="higress_base must not contain ? or #")
    return text


@router.get("/connector-settings")
def get_connector_settings(_: Any = Depends(require_admin()), server: Any = Depends(get_server)) -> dict[str, Any]:
    return {"higress_base": _settings_repo(server).get(_HIGRESS_BASE_KEY) or ""}


@router.put("/connector-settings")
def put_connector_settings(
    body: ConnectorSettingsBody,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    base = _normalize_higress_base(body.higress_base)
    _settings_repo(server).set(_HIGRESS_BASE_KEY, base)
    return {"higress_base": base}


def _schedule_connector_reload(server: Any, user_id: int) -> None:
    """Best-effort agent reload after writing custom MCP servers (mirrors the
    connectors router). Sync-context safe: no running loop → skip (the
    connectors UI reloads on its own next save anyway)."""
    runtime = getattr(server, "app_runtime", None)
    if runtime is None:
        return

    async def _run() -> None:
        try:
            await runtime.agent_registry.reload_connectors_for_user(user_id)
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "connector reload failed after catalog attach (user %s)", user_id
            )

    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_run())


@router.post("/services/{service_id}/attach-connector")
def attach_connector(
    service_id: str,
    body: AttachConnectorBody,
    user: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Attach a PUBLISHED service as the caller's own catalog connector.

    Creates the same per-service attachment row the tenant flow uses — the
    row carries the service's org attribution and allowed_roles (kept in
    sync on every service edit) and its spec opts into ``delegate_identity``
    so outbound calls carry the caller's own short-lived JWT (hub filters
    tools by the caller's rls roles). Re-attach refreshes in place and
    reports ``action: updated``; a legacy container-row self-mount left by
    the pre-2026-10-05 shape is removed on the way.
    """
    repo = _repo(server)
    svc = _service_or_404(repo, service_id)
    if svc.status != "published":
        raise HTTPException(
            status_code=409, detail="publish the service before attaching it as a connector"
        )
    base = _settings_repo(server).get(_HIGRESS_BASE_KEY) or ""
    if not base:
        raise HTTPException(
            status_code=400,
            detail="Higress base URL not configured — set it first (PUT /connector-settings)",
        )
    # 2026-10-05 reshaped: one-click attach mints the SAME per-service catalog
    # attachment row the tenant flow uses (kind custom-mcp:{sid}), so the
    # connector row CARRIES the service's attribution and allowed_roles and
    # stays in sync (sync_service_attachments) — the previous shape, an entry
    # inside the admin's manual custom-mcp container row, had no home for
    # org/roles and left a dead mount behind on unpublish. org_id mirrors the
    # service exactly; '*' is lowered to NULL so the admin's own-use row never
    # becomes platform-shared behind a tenant's explicit opt-in.
    from octop.mcp_catalog.tenant_share import attach_service_to_org

    conn = _connector_service(server)
    result = attach_service_to_org(
        conn,
        conn.catalog_attachment_repo(),
        svc,
        org_id=svc.org_id if svc.org_id and svc.org_id != "*" else None,
        owner_user_id=user.id,
        higress_base=base,
    )
    # Migrate the legacy self-mount: drop the old container entry so a
    # re-attach does not leave both shapes mounted.
    legacy_key = f"mcp-{service_id}"
    try:
        existing = conn.get_custom_servers(user.id)
        if legacy_key in existing:
            conn.put_custom_servers(
                user.id, {k: v for k, v in existing.items() if k != legacy_key}
            )
    except Exception:  # noqa: BLE001 — cleanup is best-effort, attach already landed
        logger.exception(
            "legacy catalog mount cleanup failed for %s (user %s)", service_id, user.id
        )
    _schedule_connector_reload(server, user.id)
    return {
        "server_name": f"mcp-{service_id}",
        "url": f"{base}{svc.resolved_mcp_path}",
        "action": result["action"],
        "instance_id": result["instance_id"],
        "display_name": f"目录接入 · {svc.display_name or svc.service_id}",
    }
