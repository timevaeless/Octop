"""Tenant-member management router (T4-2, pluggable module).

Mounted under ``/api/org``. Gate = platform admin (``is_admin``) **or** an
active membership in the path org carrying the ``org_admin`` ability
package — deliberately *not* ``role=admin`` (ADR I2: that role is a global
bypass and would break tenancy for everyone else). Every endpoint re-reads
the caller's membership from the repo (authoritative source), fails closed
on a missing or disabled org, and returns 403 (not 404) for an
authenticated caller that simply lacks the package, so the existence of an
org is not leaked to strangers.

Face (design §7.5 members tab):

* ``GET    /{org_id}/members``                  — roster with user fields
* ``PUT    /{org_id}/members/{user_id}/roles``  — grant/revoke packages
* ``POST   /{org_id}/members/{user_id}/suspend``    — disable the account
* ``POST   /{org_id}/members/{user_id}/reactivate`` — re-enable it

Semantics worth pinning down:

* **Suspension lives on the account** (``users.disabled``), not the
  membership: the JIT re-expansion would resurrect a merely-suspended
  membership on the next login, so a membership-level flag would be a
  placebo. Reuses ``user_manager.disable/enable`` (cache eviction, audit,
  and the T4-3 epoch-bump hook all come along).
* **Grantable packages are visibility-scoped** (2026-09-30 split): a tenant
  admin may hand out platform packages (``org_id IS NULL``) and its own
  ``t_{org}_``-prefixed rows, never ``is_system`` ones — ``org_admin`` itself
  is the platform's designation channel, so granting it from inside the
  tenant would bypass "the platform admin designates tenant admins".
* **Sole-admin guard**: the last active ``org_admin`` of an org cannot be
  demoted or suspended, so a tenant can never lock itself out.

Standalone module per user convention 2026-09-26 (extract, don't weave):
registration is one line in ``app.py``; deleting the module and that line
restores community behaviour verbatim.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from octop.api.deps import current_user, get_server

router = APIRouter()

_ORG_ADMIN_PACKAGE = "org_admin"


class RolesBody(BaseModel):
    role_keys: list[str] = Field(default_factory=list)


def _require_org_admin(server: Any, user: Any, org_id: str) -> None:
    """Fail-closed gate: platform admin, or active ``org_admin`` in *this* org."""
    org = server.services.org_repo.get_org(org_id)
    if user.is_admin:
        if org is None:
            raise HTTPException(status_code=404, detail=f"org {org_id!r} not found")
        return
    # No existence oracle (module docstring promise, review 2026-10-02 P1-9):
    # for a non-admin the response must be indistinguishable whether the org
    # is unknown, disabled, or simply not theirs — one 403 either way. The
    # previous shape (unknown org → 404 *before* the membership check) let an
    # authenticated caller probe which tenant ids exist.
    membership = server.services.org_repo.get_membership(user.id, org_id)
    if (
        org is None
        or membership is None
        or membership.status != "active"
        or _ORG_ADMIN_PACKAGE not in membership.role_keys
    ):
        raise HTTPException(status_code=403, detail="org_admin required for this tenant")


def _count_org_admins(server: Any, org_id: str) -> int:
    return sum(
        1
        for m in server.services.org_repo.list_org_members(org_id)
        if m["status"] == "active" and not m["disabled"] and _ORG_ADMIN_PACKAGE in m["role_keys"]
    )


def _load_member(server: Any, org_id: str, user_id: int) -> dict[str, Any]:
    target = server.services.user_repo.get(user_id)
    if target is not None and target.role == "admin":
        # Platform admins are outside tenant purview regardless of membership.
        raise HTTPException(status_code=403, detail="platform admins are outside tenant administration")
    for m in server.services.org_repo.list_org_members(org_id):
        if m["user_id"] == user_id:
            return m
    raise HTTPException(status_code=404, detail=f"user {user_id} is not a member of {org_id!r}")


@router.get("/{org_id}/members", summary="List a tenant's members")
async def list_org_members(
    org_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    _require_org_admin(server, user, org_id)
    return server.services.org_repo.list_org_members(org_id)


@router.put("/{org_id}/members/{user_id}/roles", summary="Grant/revoke ability packages")
async def set_member_roles(
    org_id: str,
    user_id: int,
    body: RolesBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    from octop.api.routers.roles import _validate_role_keys
    from octop.infra.db.repos.tenant_roles import tenant_role_repo_for
    from octop.infra.users.lifecycle import bump_user_epoch

    _require_org_admin(server, user, org_id)
    member = _load_member(server, org_id, user_id)
    if member["user_role"] == "admin":
        raise HTTPException(status_code=403, detail="platform admins are outside tenant administration")
    role_keys = _validate_role_keys(server, body.role_keys)
    if not user.is_admin:
        # Tenant-admin callers: platform packages ∪ own packages, minus the
        # system ones (org_admin is platform-designated only). System rows the
        # member already holds (``member``/``org_admin`` — the platform's
        # designation + JIT machinery) are passed through untouched: a
        # wholesale PUT that merely keeps them must not 400 just because they
        # are not grantable, and the UI ships them as locked options. Platform
        # admins passing through this endpoint keep full authority.
        _repo = tenant_role_repo_for(server.services.org_repo)
        grantable = {r.role_key for r in _repo.list_grantable_roles(org_id)}
        system_keys = {
            r.role_key for r in _repo.list_visible_roles(org_id) if r.is_system
        }
        held_system = system_keys & set(member["role_keys"])
        denied = [k for k in role_keys if k not in grantable and k not in held_system]
        if denied:
            raise HTTPException(
                status_code=400,
                detail=f"packages are not tenant-grantable: {','.join(denied)}",
            )
        role_keys = [k for k in role_keys if k not in held_system] + sorted(held_system)
        # The passthrough keys MUST land in per-user grants too: JIT
        # re-expansion computes effective = mappings ∪ grants, and a
        # designation that lives in neither would be wiped on the member's
        # next SSO login (an org_admin editing their own packages would lose
        # the designation). ``replace_member_grants`` is a wholesale replace,
        # so a platform-side revocation (a PUT whose keys omit org_admin)
        # overwrites the residue — no resurrection path.
    if (
        _ORG_ADMIN_PACKAGE in member["role_keys"]
        and _ORG_ADMIN_PACKAGE not in role_keys
        and _count_org_admins(server, org_id) <= 1
    ):
        # Fast pre-check; the authoritative guard re-runs inside the write
        # transaction (review 2026-10-02 P1-6) — two concurrent PUTs could
        # both pass this count and demote each other.
        raise HTTPException(status_code=409, detail="cannot demote the last org_admin of this tenant")

    existing = server.services.org_repo.get_membership(user_id, org_id)
    assert existing is not None  # listed as a member a moment ago
    updated = server.services.org_repo.upsert_membership_keeping_last_admin(
        user_id,
        org_id,
        role_keys=role_keys,
        status=existing.status,
        source=existing.source,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="cannot demote the last org_admin of this tenant")
    # 028: remember the requested set as per-user grants so the JIT
    # re-expansion on the member's next login unions them with the
    # mapping-derived keys instead of wiping them.
    server.services.org_repo.replace_member_grants(
        user_id, org_id, role_keys, granted_by=user.username
    )
    bump_user_epoch(server.services, user_id, reason="org.member_roles_updated")
    server.services.audit_repo.write(
        actor=user.username,
        action="org.member_roles_updated",
        target=member["username"],
        payload=f"org={org_id} roles={','.join(role_keys) or '-'}",
        org_id=org_id,
    )
    return {"user_id": user_id, "org_id": org_id, "role_keys": role_keys}


async def _set_account_disabled(
    org_id: str,
    user_id: int,
    disabled: bool,
    user: Any,
    server: Any,
) -> dict[str, Any]:
    _require_org_admin(server, user, org_id)
    member = _load_member(server, org_id, user_id)
    if member["user_role"] == "admin":
        raise HTTPException(status_code=403, detail="platform admins are outside tenant administration")
    if member["disabled"] == disabled:
        return {"user_id": user_id, "username": member["username"], "disabled": disabled}
    if disabled and _ORG_ADMIN_PACKAGE in member["role_keys"] and _count_org_admins(server, org_id) <= 1:
        raise HTTPException(status_code=409, detail="cannot suspend the last org_admin of this tenant")
    if disabled:
        await server.user_manager.disable(member["username"])
    else:
        await server.user_manager.enable(member["username"])
    server.services.audit_repo.write(
        actor=user.username,
        action="org.member_suspended" if disabled else "org.member_reactivated",
        target=member["username"],
        payload=f"org={org_id}",
        org_id=org_id,
    )
    return {"user_id": user_id, "username": member["username"], "disabled": disabled}


@router.post("/{org_id}/members/{user_id}/suspend", summary="Suspend a member's account")
async def suspend_member(
    org_id: str,
    user_id: int,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    return await _set_account_disabled(org_id, user_id, True, user, server)


@router.post("/{org_id}/members/{user_id}/reactivate", summary="Re-activate a member's account")
async def reactivate_member(
    org_id: str,
    user_id: int,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    return await _set_account_disabled(org_id, user_id, False, user, server)


@router.get("/{org_id}/refs", summary="Tenant info + visible packages for the roster faces")
async def org_refs(
    org_id: str,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Roster-face reference data (028, additive).

    ``GET /api/roles`` and ``GET /api/orgs`` are platform-admin-only, so an
    ``org_admin`` could not even populate the member roster's pickers. This
    endpoint hands out only *this* org's identity and the **visible** packages
    (platform rows plus the tenant's own, ``is_system`` flagged) — nothing
    about other tenants leaks, unlike the global lists.

    System rows (``org_admin``/``member``) ride along on purpose: existing
    memberships reference their keys, and the roster renders them with their
    Chinese display names instead of raw keys. Granting remains restricted —
    the client picker filters ``is_system`` and the write path re-validates
    against the grantable set server-side.
    """
    from octop.infra.db.repos.tenant_roles import tenant_role_repo_for

    _require_org_admin(server, user, org_id)
    org = server.services.org_repo.get_org(org_id)
    assert org is not None  # _require_org_admin already 404s on unknown orgs
    return {
        "org": {"org_id": org.org_id, "name": org.name, "status": org.status},
        "roles": [
            {
                "role_key": r.role_key,
                "name_zh": r.name_zh,
                "org_id": r.org_id,
                "is_default": r.is_default,
                "is_system": r.is_system,
                "description": r.description,
            }
            for r in tenant_role_repo_for(server.services.org_repo).list_visible_roles(org_id)
        ],
    }
