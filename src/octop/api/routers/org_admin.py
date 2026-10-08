"""Tenant-admin router — packages, job mappings, resource ACLs (T4, pluggable
module).

Mounted under ``/api/org`` next to :mod:`org_members`, with the same
fail-closed gate (platform admin **or** an active membership in the path org
carrying the ``org_admin`` package, re-read from the repo on every request).

2026-09-30 split — what the tenant admin plane owns:

* **Ability packages**: CRUD over ``org_id = <path org>`` rows only. Storage
  keys are minted here with the reserved ``t_{org}_`` prefix
  (:func:`~octop.infra.db.repos.tenant_roles.tenant_role_key`), so tenants
  never collide on the global ``role_key`` PK. ``is_system`` rows
  (``org_admin`` / ``member``) are platform property and untouchable here.
* **Job-code mappings**: ``(org, code) → key`` rows scoped to the path org.
  The ``'*'`` global rows are platform property and are neither listed nor
  writable from this face.
* **Resource ACLs**: ``allowed_roles`` on the five resource families, for
  rows *attributed to this tenant*. The body has no ``org_id`` field at all —
  attribution stays a platform decision, so it cannot even be expressed.
  Exception (034 two-plane authz): ``mcp-services`` also LISTS the
  platform-shared (``'*'``) services read-only — visibility, not edit rights;
  the global hub ACL means one tenant can never narrow a shared service.

Everything the tenant plane can reference (packages it may grant or put into
an ACL) is validated against the visibility scope — platform packages plus
its own — by :class:`~octop.infra.db.repos.tenant_roles.TenantRoleRepo`;
anything else is a 400, fail-closed.

Standalone module per user convention (extract, don't weave): registration is
one line in ``app.py``; deleting the module and that line restores the
pre-split behaviour verbatim.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from octop.api.deps import current_user, get_server
from octop.api.routers.org_members import _require_org_admin
from octop.infra.connectors.custom_mcp import resolve_acl_connector_parent
from octop.infra.db.repos.tenant_roles import TenantRoleRepo, tenant_role_repo_for

router = APIRouter()

#: resource-acl ``kind`` → (table, id column, name column). The tenant list is
#: a plain projection of rows attributed to the path org; the write paths go
#: through the repos so JSON parsing / caching stay in one place.
#: (``mcp-services`` retired 2026-10-01: MCP roles are configured in the
#: tenant MCP catalog — one resource, one config point.)
_ACL_KINDS: dict[str, tuple[str, str, str]] = {
    "experts": ("published_experts", "published_expert_id", "name"),
    "connectors": ("connectors", "instance_id", "display_name"),
    "agents": ("agents", "agent_id", "name"),
    "knowledge-bases": ("knowledge_bases", "knowledge_base_id", "name"),
}

#: Catalog-attached connectors (``custom-mcp:{service_id}`` kinds) are
#: projections of the MCP catalog — their roles are catalog-owned, so they
#: never appear as independently configurable rows in the connectors family.
#: The manual ``custom-mcp`` doc (no suffix) stays configurable here.
#: The pattern is a bound parameter: an inline ``%`` breaks psycopg (its
#: paramstyle treats any ``%`` in the statement as a placeholder) while the
#: same statement also runs on SQLite in tests.
_CONNECTORS_FAMILY_EXCLUDE = "AND kind NOT LIKE ?"
_CONNECTORS_EXCLUDE_PATTERN = "custom-mcp:%"


def _tenant_roles(server: Any) -> TenantRoleRepo:
    # Built over the org repo's pool via the sanctioned hook, so tests that
    # stub ``services`` only need an ``org_repo``.
    return tenant_role_repo_for(server.services.org_repo)


class TenantRoleBody(BaseModel):
    """Create/update a tenant package. The key is the *display* key — the
    ``t_{org}_`` storage prefix is minted server-side and never accepted
    from the client."""

    bare_key: str = Field(min_length=1, max_length=64)
    name_zh: str = Field(min_length=1)
    description: str = ""
    is_default: bool = False


class TenantRoleUpdateBody(BaseModel):
    name_zh: str | None = None
    description: str | None = None
    is_default: bool | None = None


class TenantMappingBody(BaseModel):
    """Replace one role code's package set for this tenant.

    Mirrors the platform ``ReplaceRoleMappingBody`` minus any ``org_id`` —
    the scope is the path org, full stop.
    """

    job_code: str = Field(min_length=1, max_length=64)
    role_keys: list[str] = Field(default_factory=list)
    previous_job_code: str | None = None


class TenantResourceAclBody(BaseModel):
    allowed_roles: list[str] = Field(default_factory=list)
    # No ``org_id``: attribution is platform-only by construction.


def _validate_tenant_keys(
    server: Any, org_id: str, keys: list[str]
) -> list[str]:
    """Dedupe + visibility check; 400 on anything the tenant cannot reference."""
    out: list[str] = []
    for key in keys:
        normalized = key.strip()
        if not normalized:
            raise HTTPException(status_code=400, detail="role key must not be empty")
        if normalized not in out:
            out.append(normalized)
    invalid = _tenant_roles(server).validate_keys_visible(org_id, out)
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"unknown or out-of-tenant ability packages: {','.join(invalid)}",
        )
    return out


def _role_payload(r: Any) -> dict[str, Any]:
    return {
        "role_key": r.role_key,
        "bare_key": r.bare_key,
        "org_id": r.org_id,
        "name_zh": r.name_zh,
        "is_default": r.is_default,
        "is_system": r.is_system,
        "description": r.description,
    }


# ----- ability packages (tenant-owned) -----


@router.get("/{org_id}/roles", summary="List packages visible to this tenant")
async def list_tenant_roles(
    org_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    """Platform packages (``org_id IS NULL``) plus the tenant's own rows."""
    _require_org_admin(server, user, org_id)
    return [_role_payload(r) for r in _tenant_roles(server).list_visible_roles(org_id)]


@router.post("/{org_id}/roles", status_code=201, summary="Create a tenant package")
async def create_tenant_role(
    org_id: str,
    body: TenantRoleBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    _require_org_admin(server, user, org_id)
    repo = _tenant_roles(server)
    try:
        view = repo.create_tenant_role(
            org_id,
            bare_key=body.bare_key.strip(),
            name_zh=body.name_zh.strip(),
            is_default=body.is_default,
            description=body.description,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=409, detail=f"role {str(exc)!r} already exists"
        ) from exc
    server.services.audit_repo.write(
        actor=user.username,
        action="role.create",
        target=view.role_key,
        payload=f"org={org_id} tenant_package=1",
        org_id=org_id,
    )
    return _role_payload(view)


@router.put("/{org_id}/roles/{role_key}", summary="Update a tenant package")
async def update_tenant_role(
    org_id: str,
    role_key: str,
    body: TenantRoleUpdateBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    _require_org_admin(server, user, org_id)
    view = _tenant_roles(server).update_tenant_role(
        org_id,
        role_key,
        name_zh=body.name_zh,
        description=body.description,
        is_default=body.is_default,
    )
    if view is None:
        # Covers unknown keys, other tenants' packages, and system rows alike —
        # no need to disclose which.
        raise HTTPException(status_code=404, detail=f"role {role_key!r} not found")
    server.services.audit_repo.write(
        actor=user.username,
        action="role.update",
        target=role_key,
        payload=f"org={org_id}",
        org_id=org_id,
    )
    return _role_payload(view)


@router.delete("/{org_id}/roles/{role_key}", status_code=204, summary="Delete a tenant package")
async def delete_tenant_role(
    org_id: str,
    role_key: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> Response:
    _require_org_admin(server, user, org_id)
    if not _tenant_roles(server).delete_tenant_role(org_id, role_key):
        raise HTTPException(status_code=404, detail=f"role {role_key!r} not found")
    # Review P1-3: the FK cascade cleared the mapping/grant rows, but the
    # membership JSON still carries the key — bump exactly the holders so the
    # deleted key cannot keep riding in their rls claim until the next login.
    holders = server.services.user_repo.bump_token_epoch_for_role_key(org_id, role_key)
    server.services.audit_repo.write(
        actor=user.username,
        action="role.delete",
        target=role_key,
        payload=f"org={org_id} epochs_bumped={holders}",
        org_id=org_id,
    )
    return Response(status_code=204)


# ----- job-code mappings (tenant-scoped) -----


@router.get("/{org_id}/role-mappings", summary="List this tenant's job → package mappings")
async def list_tenant_mappings(
    org_id: str,
    job_code: str | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    """Own rows only — the ``'*'`` platform defaults are not shown here."""
    _require_org_admin(server, user, org_id)
    codes = [job_code] if job_code else None
    return [
        {
            "org_id": m.org_id,
            "job_code": m.job_code,
            "role_key": m.role_key,
            "origin": m.origin,
            "source_org": m.source_org,
            "created_at": m.created_at,
        }
        for m in _tenant_roles(server).list_tenant_mappings(org_id, job_codes=codes)
    ]


@router.put("/{org_id}/role-mappings", summary="Replace one role code's package set")
async def set_tenant_mapping(
    org_id: str,
    body: TenantMappingBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    _require_org_admin(server, user, org_id)
    role_keys = _validate_tenant_keys(server, org_id, body.role_keys)
    org_repo = server.services.org_repo
    previous_job_code = body.previous_job_code
    if previous_job_code is not None and not org_repo.list_role_mappings(
        org_id=org_id, job_codes=[previous_job_code]
    ):
        raise HTTPException(
            status_code=404, detail=f"mapping {org_id!r}/{previous_job_code!r} not found"
        )
    org_repo.set_role_mappings(
        org_id,
        body.job_code,
        role_keys,
        previous_org_id=org_id if previous_job_code is not None else None,
        previous_job_code=previous_job_code,
    )
    server.services.audit_repo.write(
        actor=user.username,
        action="role.mapping.set",
        target=body.job_code,
        payload=f"org={org_id} roles={','.join(role_keys) or '-'}",
        org_id=org_id,
    )
    return {"org_id": org_id, "job_code": body.job_code, "role_keys": role_keys}


@router.delete(
    "/{org_id}/role-mappings",
    status_code=204,
    summary="Remove a job → package mapping row",
)
async def delete_tenant_mapping(
    org_id: str,
    job_code: str,
    role_key: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> Response:
    _require_org_admin(server, user, org_id)
    removed = server.services.org_repo.remove_role_mapping(org_id, job_code, role_key)
    if not removed:
        raise HTTPException(status_code=404, detail="mapping not found")
    server.services.audit_repo.write(
        actor=user.username,
        action="role.mapping.remove",
        target=job_code,
        payload=f"org={org_id} role={role_key}",
        org_id=org_id,
    )
    return Response(status_code=204)


# ----- resource ACLs (allowed_roles only, attribution stays platform-side) -----


@router.get("/{org_id}/resource-acl/{kind}", summary="List this tenant's resources with ACLs")
async def list_tenant_resource_acl(
    org_id: str,
    kind: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    _require_org_admin(server, user, org_id)
    spec = _ACL_KINDS.get(kind)
    if spec is None:
        raise HTTPException(
            status_code=400, detail=f"kind must be one of {', '.join(sorted(_ACL_KINDS))}"
        )
    table, id_col, name_col = spec
    if kind == "connectors":
        extra_where = _CONNECTORS_FAMILY_EXCLUDE
        params: tuple[Any, ...] = (org_id, _CONNECTORS_EXCLUDE_PATTERN)
    else:
        extra_where = ""
        params = (org_id,)
    with server.services.db.connect() as conn:
        rows = conn.execute(
            f"SELECT {id_col} AS id, {name_col} AS name, allowed_roles"
            + (", default_mcp_servers" if kind == "experts" else "")
            + f" FROM {table} WHERE org_id = ? {extra_where} ORDER BY {name_col}",
            params,
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        row: dict[str, Any] = {
            "id": str(r["id"]),
            "name": str(r["name"]),
            "allowed_roles": _parse_roles(r["allowed_roles"]),
        }
        if kind == "experts":
            # Connector references (custom-mcp:{sid}__{instance}) let the
            # tenant MCP catalog advise on templates whose visible roles
            # exceed the referenced service's roles — read-only advisory.
            row["default_mcp_servers"] = _parse_roles(r["default_mcp_servers"])
        out.append(row)
    return out


def _parse_roles(raw: Any) -> list[str]:
    import json

    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in parsed] if isinstance(parsed, list) else []


def _load_acl_row(
    server: Any, kind: str, resource_id: str, org_id: str, user: Any
) -> tuple[Any, str]:
    """Fetch the row behind a resource-acl write and assert tenant ownership.

    Returns ``(row, resolved_id)``. Anything not owned by the path org —
    unknown id, another tenant's row, an unattributed platform draft — is a
    404, so the endpoint never confirms a foreign resource exists.
    """
    if kind == "experts":
        row = server.services.published_expert_repo.get(resource_id)
        rid = resource_id
    elif kind == "connectors":
        repo = server.services.connector_repo
        row = repo.get(resource_id)
        rid = resource_id
        if row is None:
            # Custom-MCP children live under synthetic ids; the ACL row is
            # the parent custom-mcp connector.
            parent = resolve_acl_connector_parent(repo, resource_id, user.id)
            if parent is not None:
                row = parent
                rid = parent.instance_id
        if row is not None and str(getattr(row, "kind", "")).startswith("custom-mcp:"):
            # Catalog-derived attachment: its roles are owned by the catalog
            # sync (org_mcp). A direct PUT here would bypass the tool-roles
            # ⊆ service-roles invariant — point at the config surface instead.
            raise HTTPException(
                status_code=400,
                detail="catalog-attached connector — configure its roles in the tenant MCP catalog",
            )
    elif kind == "agents":
        row = server.services.agent_repo.get(resource_id)
        rid = resource_id
    elif kind == "knowledge-bases":
        row = server.services.knowledge_repo.get_base(resource_id)
        rid = resource_id
    else:
        raise HTTPException(
            status_code=400, detail=f"kind must be one of {', '.join(sorted(_ACL_KINDS))}"
        )
    if row is None:
        raise HTTPException(status_code=404, detail=f"{kind}/{resource_id} not found")
    if getattr(row, "org_id", None) != org_id:
        raise HTTPException(status_code=404, detail=f"{kind}/{resource_id} not found")
    return row, rid


@router.put(
    "/{org_id}/resource-acl/{kind}/{resource_id}",
    summary="Set which packages can use one of this tenant's resources",
)
async def set_tenant_resource_acl(
    org_id: str,
    kind: str,
    resource_id: str,
    body: TenantResourceAclBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    _require_org_admin(server, user, org_id)
    row, rid = _load_acl_row(server, kind, resource_id, org_id, user)
    roles = _validate_tenant_keys(server, org_id, body.allowed_roles)
    if kind == "experts":
        server.services.published_expert_repo.set_allowed_roles(rid, roles)
    elif kind == "connectors":
        server.services.connector_repo.update_metadata(rid, allowed_roles=roles)
    elif kind == "agents":
        server.services.agent_repo.set_allowed_roles(rid, roles)
    else:
        server.services.knowledge_repo.set_allowed_roles(rid, roles)
    server.services.audit_repo.write(
        actor=user.username,
        action="resource.acl.set",
        target=f"{kind}/{rid}",
        payload=f"org={org_id} roles={','.join(roles) or '-'}",
        org_id=org_id,
    )
    return {"kind": kind, "id": rid, "allowed_roles": roles, "org_id": getattr(row, "org_id", None)}
