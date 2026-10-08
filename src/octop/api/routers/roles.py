"""Admin router — ability packages (roles), memberships, and resource ACLs.

Mounted under ``/api/admin``. Platform-admin only: ability-package and
membership management is the authorization control plane, so it must not be
reachable through tenant-level permissions (ADR I2 — ``org_admin`` is a
capability package, never a bypass).
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from octop.api.deps import current_user, get_server, require_admin
from octop.infra.connectors.custom_mcp import resolve_acl_connector_parent

router = APIRouter()

# Shape an ``org_id`` may have. An org_id ends up in JWT claims (``oid``) and in
# per-tenant storage paths, so it stays an opaque slug — never free text.
# No ``.``: the filesystem tenant layout sanitizes ``.`` to ``_``
# (``_safe_segment``), so ``team.a`` and ``team_b`` would collide onto the
# same ``orgs/team_a`` directory — cross-tenant file mixing. Keep this rule
# and that sanitizer character-aligned (review 2026-09-26 P1-2).
_ORG_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

# ``kind`` has no CHECK in the schema; these are the two the product uses and
# the only ones the admin page offers.
_ORG_KINDS = ("internal", "partner")

# Only ``active`` resolves (see ``OrgRepo.get_org``); anything else fails the
# five-level chain closed, so a typo must not silently disable a tenant.
_ORG_STATUSES = ("active", "disabled")

# Mirrors ``chk_idp_org_bindings_match_type`` in the schema.
_BINDING_MATCH_TYPES = ("claim", "static", "domain")


def _role_key_list(value: str) -> str:
    value = value.strip()
    if not value:
        raise HTTPException(status_code=400, detail="role_key must not be empty")
    return value


class CreateRoleBody(BaseModel):
    role_key: str = Field(min_length=1, max_length=64)
    name_zh: str = Field(min_length=1)
    description: str = ""
    is_default: bool = False


class UpdateRoleBody(BaseModel):
    name_zh: str | None = None
    description: str | None = None
    is_default: bool | None = None


class MembershipBody(BaseModel):
    org_id: str = Field(min_length=1)
    role_keys: list[str] = Field(default_factory=list)
    status: str = "active"


class RoleMappingBody(BaseModel):
    org_id: str = Field(min_length=1)
    job_code: str = Field(min_length=1)
    role_key: str = Field(min_length=1)


class ReplaceRoleMappingBody(BaseModel):
    """One role code and the complete set of ability packages it grants."""

    org_id: str = Field(min_length=1)
    job_code: str = Field(min_length=1, max_length=64)
    role_keys: list[str] = Field(default_factory=list)
    # The group being edited. Sent when the role code or its scope is renamed,
    # so the old group is dropped in the same transaction (never a client-side
    # delete-then-add, which would briefly grant nothing).
    previous_org_id: str | None = None
    previous_job_code: str | None = None


class ResourceAclBody(BaseModel):
    allowed_roles: list[str] = Field(default_factory=list)
    # 2026-09-27 attribution: only applied when the key is present in the
    # payload (``model_fields_set``), so the ACL-only callers are untouched.
    # ``None`` = unattributed (admin draft), ``'*'`` = explicit platform
    # share, otherwise an existing tenant id.
    org_id: str | None = None


class CreateOrgBody(BaseModel):
    org_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    kind: str = "internal"
    status: str = "active"


class UpdateOrgBody(BaseModel):
    """Tri-state patch: ``None`` leaves a field alone."""

    name: str | None = None
    kind: str | None = None
    status: str | None = None


class CreateOrgBindingBody(BaseModel):
    """One upstream value → tenant resolution key."""

    idp_alias: str = Field(min_length=1, max_length=64)
    match_type: str
    # Required for ``claim``/``domain`` and necessarily empty for ``static``.
    # The DB CHECK owns this pairing; this mirrors it so a bad pair is a 400
    # rather than a driver error surfacing as a 500.
    match_value: str = ""
    org_id: str = Field(min_length=1, max_length=64)
    requires_approval: bool = False
    note: str = ""


def _validate_org_id(value: str) -> str:
    value = value.strip()
    if not _ORG_ID_RE.match(value):
        raise HTTPException(
            status_code=400,
            detail="org_id must be 1-64 characters of [A-Za-z0-9_-] (no dots)",
        )
    return value


def _validate_org_kind(value: str) -> str:
    if value not in _ORG_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {', '.join(_ORG_KINDS)}")
    return value


def _validate_org_status(value: str) -> str:
    if value not in _ORG_STATUSES:
        raise HTTPException(
            status_code=400, detail=f"status must be one of {', '.join(_ORG_STATUSES)}"
        )
    return value


def _validate_binding(body: CreateOrgBindingBody) -> str:
    """Validate the ``match_type``/``match_value`` pair; return the value.

    ``static`` means "every user from this IdP belongs to one tenant", so it
    deliberately carries no value; ``claim`` and ``domain`` must carry one.
    Allowing either slip through would hit the DB CHECK and 500.
    """
    if body.match_type not in _BINDING_MATCH_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"match_type must be one of {', '.join(_BINDING_MATCH_TYPES)}",
        )
    value = body.match_value.strip()
    if body.match_type == "static":
        if value:
            raise HTTPException(
                status_code=400,
                detail="match_value must be empty for match_type 'static'",
            )
        return ""
    if not value:
        raise HTTPException(
            status_code=400,
            detail=f"match_value is required for match_type {body.match_type!r}",
        )
    return value


def _org_payload(row: Any) -> dict[str, Any]:
    """Serialize an ``OrgRow`` for the admin page (counts are added on list)."""
    return {
        "org_id": row.org_id,
        "name": row.name,
        "kind": row.kind,
        "status": row.status,
    }


def _validate_role_keys(server: Any, keys: list[str]) -> list[str]:
    """Deduplicate and verify every key exists in ``roles`` (fail-closed 400)."""
    out: list[str] = []
    for key in keys:
        normalized = _role_key_list(key)
        if normalized not in out:
            out.append(normalized)
    for key in out:
        if server.services.org_repo.get_role(key) is None:
            raise HTTPException(status_code=400, detail=f"unknown ability package: {key!r}")
    return out


def _require_global_scope(org_id: str) -> str:
    """Platform mapping writes are ``'*'``-only (2026-09-30 split).

    Tenant-scoped ``(org, code)`` rows belong to the tenant's org_admin
    (``/api/org/{org}/role-mappings``); the platform face must not silently
    set policy inside a tenant, so a tenant id here is a 403, not a 400 —
    the request shape is valid, the authority is not.
    """
    if org_id != "*":
        raise HTTPException(
            status_code=403,
            detail="platform admins manage only the '*' global mappings; "
            f"tenant rows for {org_id!r} belong to the tenant admin",
        )
    return org_id


@router.get("/roles", summary="List ability packages")
async def list_roles(
    _: Any = Depends(require_admin()), server: Any = Depends(get_server)
) -> list[dict[str, Any]]:
    """Platform overview: every package, platform- and tenant-owned alike.

    ``org_id=None`` marks a platform package (seeded or admin-created); a
    tenant id marks a package that tenant's org_admin manages. The platform
    face lists all of them for governance but only creates platform rows —
    tenant packages are born through ``/api/org/{org}/roles``.
    """
    return [
        {
            "role_key": r.role_key,
            "name_zh": r.name_zh,
            "org_id": r.org_id,
            "is_default": r.is_default,
            "is_system": r.is_system,
            "description": r.description,
        }
        for r in server.services.org_repo.list_roles()
    ]


@router.post("/roles", status_code=201, summary="Create a platform ability package")
async def create_role(
    body: CreateRoleBody,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Create a **platform** package (``org_id=NULL``).

    Tenant-owned packages are created by the tenant's org_admin via
    ``/api/org/{org}/roles`` and always carry the reserved ``t_{org}_`` key
    prefix — the platform plane must not squat that namespace, otherwise a
    later tenant package could collide or a platform row could be mistaken
    for tenant-owned.
    """
    role_key = _role_key_list(body.role_key)
    if role_key.startswith("t_"):
        raise HTTPException(
            status_code=400,
            detail="role keys with the 't_' prefix are reserved for tenant packages",
        )
    if server.services.org_repo.get_role(role_key) is not None:
        raise HTTPException(status_code=409, detail=f"role {role_key!r} already exists")
    row = server.services.org_repo.create_role(
        role_key=role_key,
        name_zh=body.name_zh,
        org_id=None,
        is_default=body.is_default,
        description=body.description,
    )
    server.services.audit_repo.write(actor="admin", action="role.create", target=role_key)
    return {
        "role_key": row.role_key,
        "name_zh": row.name_zh,
        "org_id": row.org_id,
        "is_system": row.is_system,
    }


@router.delete("/roles/{role_key}", status_code=204, summary="Delete an ability package")
async def delete_role(
    role_key: str,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> Response:
    """Delete a package (platform governance — includes tenant-owned rows).

    ``is_system`` rows are protected: they carry the tenant-admin marker and
    the JIT default, and deleting the last ``org_admin`` holder's package
    would orphan a tenant. ``org_role_mappings`` / ``org_member_grants``
    cascade; stale keys inside membership JSON are pruned on the member's
    next login (tenant_hydration).
    """
    repo = server.services.org_repo
    row = repo.get_role(role_key)
    if row is None:
        raise HTTPException(status_code=404, detail=f"role {role_key!r} not found")
    if row.is_system:
        raise HTTPException(status_code=403, detail=f"role {role_key!r} is system-protected")
    with server.services.db.connect() as conn:
        conn.execute("DELETE FROM roles WHERE role_key = ?", (role_key,))
    # Review P1-3: bump exactly the members whose membership JSON still
    # carries the deleted key, so it cannot keep riding in their rls claim
    # until the next login (the hydration prune alone is login-gated).
    # org_id=None scans every tenant — a platform package may be held anywhere.
    holders = server.services.user_repo.bump_token_epoch_for_role_key(
        row.org_id, role_key
    )
    server.services.audit_repo.write(
        actor="admin",
        action="role.delete",
        target=role_key,
        payload=f"org={row.org_id or '-'} epochs_bumped={holders}",
        org_id=row.org_id,
    )
    return Response(status_code=204)


@router.put("/roles/{role_key}", summary="Update an ability package")
async def update_role(
    role_key: str,
    body: UpdateRoleBody,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    row = server.services.org_repo.get_role(role_key)
    if row is None:
        raise HTTPException(status_code=404, detail=f"role {role_key!r} not found")
    if row.is_system and body.is_default is not None and bool(body.is_default) != row.is_default:
        # The designation/JIT invariants are boot-reasserted (see
        # tenancy_schema._ROLE_SEEDS), but flipping them mid-flight silently
        # breaks the machinery: member must stay the is_default JIT fallback,
        # org_admin must never auto-provision. Display fields stay editable.
        raise HTTPException(
            status_code=400,
            detail=f"is_default of the system package {role_key!r} is fixed",
        )
    updated = server.services.org_repo.update_role(
        role_key,
        name_zh=body.name_zh,
        description=body.description,
        is_default=body.is_default,
    )
    assert updated is not None  # checked above
    return {
        "role_key": updated.role_key,
        "name_zh": updated.name_zh,
        "org_id": updated.org_id,
        "is_system": updated.is_system,
        "description": updated.description,
        "is_default": updated.is_default,
    }


@router.get("/orgs", summary="List orgs")
async def list_orgs(
    _: Any = Depends(require_admin()), server: Any = Depends(get_server)
) -> list[dict[str, Any]]:
    org_repo = server.services.org_repo
    members = org_repo.count_memberships_by_org()
    bindings = org_repo.count_bindings_by_org()
    return [
        {
            "org_id": o.org_id,
            "name": o.name,
            "kind": o.kind,
            "status": o.status,
            "member_count": members.get(o.org_id, 0),
            "binding_count": bindings.get(o.org_id, 0),
        }
        for o in org_repo.list_orgs()
    ]


@router.post("/orgs", status_code=201, summary="Create an org")
async def create_org(
    body: CreateOrgBody,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Create a tenant.

    Tenancy is Octop-owned: nothing else inserts into ``orgs``, so without this
    endpoint a new tenant only exists by re-running the seed or by hand. The
    resolution chain fails closed on an unknown org, so a tenant an upstream
    IdP reports must exist here *and* have a binding (:func:`create_org_binding`)
    before anyone can be routed to it.
    """
    org_id = _validate_org_id(body.org_id)
    # P1-10 (review 2026-10-02): refuse an org id whose ``t_{org}_`` package
    # namespace would shadow role keys another tenant already owns (underscore
    # ambiguity, e.g. existing key ``t_a_b_x`` from org ``a`` vs new org
    # ``a_b``) — otherwise those keys become visible and manageable as this
    # org's own the moment it exists.
    squatting = server.services.org_repo.tenant_keyspace_squatters(org_id)
    if squatting:
        raise HTTPException(
            status_code=400,
            detail=(
                f"org {org_id!r} would shadow existing tenant package keys: "
                f"{', '.join(sorted(squatting))}"
            ),
        )
    kind = _validate_org_kind(body.kind)
    status = _validate_org_status(body.status)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name must not be empty")
    row = server.services.org_repo.create_org(org_id=org_id, name=name, kind=kind, status=status)
    if row is None:
        raise HTTPException(status_code=409, detail=f"org {org_id!r} already exists")
    server.services.audit_repo.write(
        actor="admin",
        action="org.create",
        target=org_id,
        payload=f"name={name} kind={kind} status={status}",
        org_id=org_id,
    )
    return _org_payload(row)


@router.put("/orgs/{org_id}", summary="Update an org")
async def update_org(
    org_id: str,
    body: UpdateOrgBody,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Patch a tenant's name, kind or status.

    ``status='disabled'`` makes :meth:`OrgRepo.get_org` return ``None``, which
    fails the resolution chain closed from the next login on. Existing
    memberships are intentionally left alone — disabling stops new logins from
    landing here, it does not revoke authorization row by row.
    """
    if body.kind is not None:
        _validate_org_kind(body.kind)
    if body.status is not None:
        _validate_org_status(body.status)
    name = body.name.strip() if body.name is not None else None
    if name is not None and not name:
        raise HTTPException(status_code=400, detail="name must not be empty")
    row = server.services.org_repo.update_org(org_id, name=name, kind=body.kind, status=body.status)
    if row is None:
        raise HTTPException(status_code=404, detail=f"org {org_id!r} not found")
    if body.status == "disabled":
        # T4-3: outstanding member tokens carry the org context until natural
        # expiry; the fail-closed chain only helps the NEXT login. Pluggable
        # hook — see octop.infra.users.lifecycle.
        from octop.infra.users.lifecycle import bump_org_epochs

        bump_org_epochs(server.services, org_id, reason="org.disabled")
    server.services.audit_repo.write(
        actor="admin",
        action="org.update",
        target=org_id,
        payload=f"name={row.name} kind={row.kind} status={row.status}",
        org_id=org_id,
    )
    return _org_payload(row)


@router.delete("/orgs/{org_id}", status_code=204, summary="Delete an org")
async def delete_org(
    org_id: str,
    force: bool = False,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> Response:
    """Delete a tenant, refusing while it still has dependents.

    ``idp_org_bindings`` and ``org_memberships`` both reference ``orgs``
    ``ON DELETE CASCADE``, so a blind delete silently drops the tenant's
    resolution keys *and* revokes every user's access inside it. Both counts
    are reported in the 409 so the admin either cleans up first or passes
    ``force=true`` knowingly.
    """
    org_repo = server.services.org_repo
    if org_repo.get_org_any(org_id) is None:
        raise HTTPException(status_code=404, detail=f"org {org_id!r} not found")
    bindings, memberships = org_repo.count_org_dependents(org_id)
    if (bindings or memberships) and not force:
        raise HTTPException(
            status_code=409,
            detail=(
                f"org {org_id!r} still has {bindings} binding(s) and "
                f"{memberships} membership(s); repeat with force=true to cascade"
            ),
        )
    # T4-3: bump while the memberships still exist — the cascade delete will
    # remove them, and afterwards the members would be unfindable. Pluggable
    # hook — see octop.infra.users.lifecycle.
    from octop.infra.users.lifecycle import bump_org_epochs

    bump_org_epochs(server.services, org_id, reason="org.deleted")
    org_repo.delete_org(org_id)
    server.services.audit_repo.write(
        actor="admin",
        action="org.delete",
        target=org_id,
        payload=f"forced={force} bindings={bindings} memberships={memberships}",
        org_id=org_id,
    )
    return Response(status_code=204)


# ----- org-resolution bindings (upstream value → tenant) -----


@router.get("/org-bindings", summary="List org-resolution bindings")
async def list_org_bindings(
    _: Any = Depends(require_admin()), server: Any = Depends(get_server)
) -> list[dict[str, Any]]:
    """Every binding across all providers — the admin listing.

    Resolution only ever consults the provider that is logging in, so the page
    needs the unscoped view or a key belonging to another IdP looks absent.
    """
    return [
        {
            "idp_alias": b.idp_alias,
            "match_type": b.match_type,
            "match_value": b.match_value,
            "org_id": b.org_id,
            "requires_approval": b.requires_approval,
            "note": b.note,
        }
        for b in server.services.org_repo.list_all_bindings()
    ]


@router.post("/org-bindings", status_code=201, summary="Create an org-resolution binding")
async def create_org_binding(
    body: CreateOrgBindingBody,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Add one resolution key: how an upstream value maps to a tenant.

    Creating the tenant is not enough — the five-level chain needs a key to
    match, otherwise every user of that provider lands in *pending* with no
    tenant and no roles.
    """
    match_value = _validate_binding(body)
    idp_alias = body.idp_alias.strip()
    if not idp_alias:
        raise HTTPException(status_code=400, detail="idp_alias must not be empty")
    org_id = _validate_org_id(body.org_id)
    if server.services.org_repo.get_org_any(org_id) is None:
        raise HTTPException(status_code=404, detail=f"org {org_id!r} not found")
    created = server.services.org_repo.create_binding(
        idp_alias=idp_alias,
        match_type=body.match_type,
        match_value=match_value,
        org_id=org_id,
        requires_approval=body.requires_approval,
        note=body.note,
    )
    if not created:
        raise HTTPException(
            status_code=409,
            detail=(f"binding {idp_alias!r}/{body.match_type}/{match_value!r} already exists"),
        )
    server.services.audit_repo.write(
        actor="admin",
        action="org.binding.create",
        target=f"{idp_alias}/{body.match_type}/{match_value}",
        payload=f"org={org_id}",
        org_id=org_id,
    )
    return {
        "idp_alias": idp_alias,
        "match_type": body.match_type,
        "match_value": match_value,
        "org_id": org_id,
        "requires_approval": body.requires_approval,
        "note": body.note,
    }


@router.delete("/org-bindings", status_code=204, summary="Delete an org-resolution binding")
async def delete_org_binding(
    idp_alias: str,
    match_type: str,
    match_value: str = "",
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> Response:
    """Remove one resolution key.

    Dropping the last key for a provider sends every user it authenticates to
    *pending* on their next login — fail-closed by design, not a bug.
    ``match_value`` defaults to empty because that is the only legal value for
    a ``static`` binding.
    """
    removed = server.services.org_repo.delete_binding(idp_alias, match_type, match_value)
    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"binding {idp_alias!r}/{match_type}/{match_value!r} not found",
        )
    server.services.audit_repo.write(
        actor="admin",
        action="org.binding.delete",
        target=f"{idp_alias}/{match_type}/{match_value}",
    )
    return Response(status_code=204)


@router.get("/users/{username}/memberships", summary="List a user's org memberships")
async def list_user_memberships(
    username: str,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    row = server.services.user_repo.get_by_username(username)
    if row is None:
        raise HTTPException(status_code=404, detail=f"user {username!r} not found")
    return [
        {
            "org_id": m.org_id,
            "role_keys": m.role_keys,
            "status": m.status,
            "source": m.source,
        }
        for m in server.services.org_repo.list_memberships(row.id)
    ]


@router.put("/users/{username}/membership", summary="Assign org + ability packages to a user")
async def set_user_membership(
    username: str,
    body: MembershipBody,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Manual membership management (source='manual').

    Bumps ``users.token_epoch`` so outstanding tokens die and the new role
    set applies on the next request.
    """
    row = server.services.user_repo.get_by_username(username)
    if row is None:
        raise HTTPException(status_code=404, detail=f"user {username!r} not found")
    if server.services.org_repo.get_org(body.org_id) is None:
        raise HTTPException(status_code=404, detail=f"org {body.org_id!r} not found")
    if body.status not in ("active", "pending", "suspended"):
        raise HTTPException(status_code=400, detail="invalid membership status")
    role_keys = _validate_role_keys(server, body.role_keys)
    existing = server.services.org_repo.get_membership(row.id, body.org_id)
    server.services.org_repo.upsert_membership(
        row.id,
        body.org_id,
        role_keys=role_keys,
        status=body.status,
        source="manual" if existing is None else existing.source,
    )
    # 028: the requested set becomes per-user grants so the JIT
    # re-expansion on the next login keeps them (union with mapped keys).
    server.services.org_repo.replace_member_grants(
        row.id, body.org_id, role_keys, granted_by="admin"
    )
    if row.default_org_id is None:
        server.services.user_repo.set_default_org_id(row.id, body.org_id)
    server.services.user_repo.bump_token_epoch(row.id)
    refreshed = await server.user_manager.refresh_tenant_context(username)
    server.services.audit_repo.write(
        actor="admin",
        action="user.set_membership",
        target=username,
        payload=f"org={body.org_id} roles={','.join(role_keys) or '-'}",
        org_id=body.org_id,
    )
    return {
        "username": username,
        "org_id": refreshed.org_id if refreshed else body.org_id,
        "role_keys": list(refreshed.role_keys) if refreshed else role_keys,
    }


@router.get("/role-mappings", summary="List job → ability-package mappings")
async def list_role_mappings(
    org_id: str | None = None,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    """List mapping rows, optionally narrowed to one tenant.

    ``org_id`` means *effective for that tenant*: the repo predicate is
    ``org_id IN ('*', ?)``, i.e. the tenant's own rows plus the platform-wide
    ``'*'`` ones — the same two sets the resolver unions. That is why the 租户
    tab can show "what actually applies here" with no extra endpoint, and why
    the two are **added together** rather than the tenant row replacing the
    global one.

    ``origin``/``source_org`` are provenance, not scope: ``source_org`` records
    which tenant's first login auto-created a row, while ``org_id`` says where
    it applies. Auto-provisioning writes both as the triggering tenant, so for
    a ``'jit'`` row the two agree; a ``'*'`` row is an admin-authored
    platform-wide grant and never ``'jit'``. The admin page uses ``origin`` to
    tell an auto-provisioned mapping apart from a reviewed policy and to warn
    before an edit that changes every tenant at once. ``'legacy'`` marks the
    rows that predate the column (migration 018): they are unattributable by
    construction, and saying ``'manual'`` for them would assert a review that
    nobody performed.
    """
    return [
        {
            "org_id": m.org_id,
            "job_code": m.job_code,
            "role_key": m.role_key,
            "origin": m.origin,
            "source_org": m.source_org,
            "created_at": m.created_at,
        }
        for m in server.services.org_repo.list_role_mappings(org_id=org_id)
    ]


@router.post("/role-mappings", status_code=201, summary="Add a job → ability-package mapping")
async def add_role_mapping(
    body: RoleMappingBody,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    if server.services.org_repo.get_role(body.role_key) is None:
        raise HTTPException(status_code=400, detail=f"unknown ability package: {body.role_key!r}")
    org_id = _require_global_scope(body.org_id)
    org_repo = server.services.org_repo
    org_repo.add_role_mapping(org_id, body.job_code, body.role_key)
    server.services.audit_repo.write(
        actor="admin",
        action="role.mapping.add",
        target=body.job_code,
        payload=f"org={org_id} role={body.role_key}",
        org_id=org_id,
    )
    return _mapping_payload(
        org_repo,
        org_id=org_id,
        job_code=body.job_code,
        role_key=body.role_key,
    )


def _mapping_payload(org_repo: Any, *, org_id: str, job_code: str, role_key: str) -> dict[str, Any]:
    """Read one mapping triple back so the response carries its provenance.

    Echoing the request body would answer with ``manual``/``None`` even when the
    row already existed as a JIT default — ``INSERT … DO NOTHING`` leaves the
    original row (and its ``created_at``) untouched, so only a read-back tells
    the truth.
    """
    for m in org_repo.list_role_mappings(org_id=None, job_codes=[job_code]):
        if m.org_id == org_id and m.role_key == role_key:
            return {
                "org_id": m.org_id,
                "job_code": m.job_code,
                "role_key": m.role_key,
                "origin": m.origin,
                "source_org": m.source_org,
                "created_at": m.created_at,
            }
    # Unreachable unless the role was deleted concurrently; keep the shape stable.
    return {
        "org_id": org_id,
        "job_code": job_code,
        "role_key": role_key,
        "origin": "manual",
        "source_org": None,
        "created_at": 0,
    }


@router.put(
    "/role-mappings",
    summary="Replace the ability packages granted by one role code",
)
async def replace_role_mapping(
    body: ReplaceRoleMappingBody,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Replace a role code's whole package set in one transaction.

    Renaming the role code (or moving it to another scope) is expressed with
    ``previous_*`` rather than by deleting first: the admin page edits a *role
    code*, not individual triples, so a half-applied rename must not be
    observable. Passing an empty ``role_keys`` deletes the mapping — note that
    the JIT resolver re-creates it on the next login while a default-minimal
    ability package exists (see ``org_resolution.provision_missing_mappings``).
    """
    org_repo = server.services.org_repo
    role_keys = _validate_role_keys(server, body.role_keys)
    _require_global_scope(body.org_id)
    if body.previous_org_id is not None:
        _require_global_scope(body.previous_org_id)

    previous = None
    if body.previous_job_code is not None or body.previous_org_id is not None:
        previous = (
            body.previous_org_id if body.previous_org_id is not None else body.org_id,
            body.previous_job_code if body.previous_job_code is not None else body.job_code,
        )
        if not org_repo.list_role_mappings(org_id=previous[0], job_codes=[previous[1]]):
            raise HTTPException(
                status_code=404,
                detail=f"mapping {previous[0]!r}/{previous[1]!r} not found",
            )

    org_repo.set_role_mappings(
        body.org_id,
        body.job_code,
        role_keys,
        previous_org_id=previous[0] if previous else None,
        previous_job_code=previous[1] if previous else None,
    )
    audit = f"org={body.org_id} roles={','.join(role_keys) or '-'}"
    if previous is not None:
        audit += f" previous={previous[0]}/{previous[1]}"
    server.services.audit_repo.write(
        actor="admin", action="role.mapping.set", target=body.job_code, payload=audit,
        org_id=body.org_id,
    )
    return {
        "org_id": body.org_id,
        "job_code": body.job_code,
        "role_keys": role_keys,
    }


@router.delete("/role-mappings", status_code=204, summary="Remove a job → ability-package mapping")
async def delete_role_mapping(
    org_id: str,
    job_code: str,
    role_key: str,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> Response:
    _require_global_scope(org_id)
    removed = server.services.org_repo.remove_role_mapping(org_id, job_code, role_key)
    if not removed:
        raise HTTPException(status_code=404, detail="mapping not found")
    server.services.audit_repo.write(
        actor="admin",
        action="role.mapping.remove",
        target=job_code,
        payload=f"org={org_id} role={role_key}",
        org_id=org_id,
    )
    return Response(status_code=204)


# ----- resource ACL endpoints (allowed_roles on resources) -----


def _validate_attribution_org(server: Any, org_id: str | None) -> str | None:
    """Validate an attribution value for a resource-acl write (2026-09-27).

    ``None`` (unattributed) and ``'*'`` (explicit platform share) are
    literals; anything else must name an existing tenant — active or not
    (an admin may pre-assign to a paused tenant), hence ``get_org_any``.
    """
    if org_id is None or org_id == "*":
        return org_id
    if server.services.org_repo.get_org_any(org_id) is None:
        raise HTTPException(status_code=400, detail=f"unknown org: {org_id!r}")
    return org_id


@router.put(
    "/resource-acl/experts/{expert_id}",
    summary="Configure which ability packages can see an expert template",
)
async def set_expert_acl(
    expert_id: str,
    body: ResourceAclBody,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    roles = _validate_role_keys(server, body.allowed_roles)
    # Validate the attribution BEFORE any write (review 2026-10-02 P1-7):
    # validating after set_allowed_roles left a half-commit — a 400 on
    # org_id with the roles change already live. The flag distinguishes
    # "not sent" from "explicit null" (re-attributing to platform-private).
    set_org = "org_id" in body.model_fields_set
    new_org = _validate_attribution_org(server, body.org_id) if set_org else None
    try:
        row = server.services.published_expert_repo.set_allowed_roles(expert_id, roles)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if set_org:
        row = server.services.published_expert_repo.set_org_id(expert_id, new_org)
    return {"expert_id": expert_id, "allowed_roles": roles, "org_id": row.org_id}


@router.put(
    "/resource-acl/connectors/{instance_id}",
    summary="Configure which ability packages can use a shared connector",
)
async def set_connector_acl(
    instance_id: str,
    body: ResourceAclBody,
    user: Any = Depends(current_user),
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    roles = _validate_role_keys(server, body.allowed_roles)
    # Validate the attribution BEFORE any write (review 2026-10-02 P1-7) —
    # the previous shape 400'd on a bad org_id with the roles already live.
    set_org = "org_id" in body.model_fields_set
    new_org = _validate_attribution_org(server, body.org_id) if set_org else None
    repo = server.services.connector_repo
    row = repo.get(instance_id)
    if row is None:
        # Expanded custom-MCP children list under synthetic ids while the ACL
        # lives on the parent custom-mcp row — resolve before giving up.
        row = resolve_acl_connector_parent(repo, instance_id, user.id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"connector {instance_id!r} not found")
    if set_org:
        repo.update_metadata(row.instance_id, org_id=new_org)
    repo.update_metadata(row.instance_id, allowed_roles=roles)
    final = repo.get(row.instance_id)
    return {
        "instance_id": row.instance_id,
        "allowed_roles": roles,
        "org_id": getattr(final, "org_id", None) if final is not None else None,
    }


@router.put(
    "/resource-acl/agents/{agent_id}",
    summary="Configure which ability packages can see a shared agent",
)
async def set_agent_acl(
    agent_id: str,
    body: ResourceAclBody,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    roles = _validate_role_keys(server, body.allowed_roles)
    # Validate the attribution BEFORE any write (review 2026-10-02 P1-7).
    set_org = "org_id" in body.model_fields_set
    new_org = _validate_attribution_org(server, body.org_id) if set_org else None
    try:
        server.services.agent_repo.set_allowed_roles(agent_id, roles)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if set_org:
        server.services.agent_repo.set_org_id(agent_id, new_org)
    row = server.services.agent_repo.get(agent_id)
    return {
        "agent_id": agent_id,
        "allowed_roles": roles,
        "org_id": getattr(row, "org_id", None) if row is not None else None,
    }


@router.put(
    "/resource-acl/knowledge-bases/{kb_id}",
    summary="Configure which ability packages can see a knowledge base",
)
async def set_kb_acl(
    kb_id: str,
    body: ResourceAclBody,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    roles = _validate_role_keys(server, body.allowed_roles)
    # Validate the attribution BEFORE any write (review 2026-10-02 P1-7).
    set_org = "org_id" in body.model_fields_set
    new_org = _validate_attribution_org(server, body.org_id) if set_org else None
    try:
        server.services.knowledge_repo.set_allowed_roles(kb_id, roles)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if set_org:
        server.services.knowledge_repo.set_org_id(kb_id, new_org)
    row = server.services.knowledge_repo.get_base(kb_id)
    return {
        "knowledge_base_id": kb_id,
        "allowed_roles": roles,
        "org_id": getattr(row, "org_id", None) if row is not None else None,
    }
