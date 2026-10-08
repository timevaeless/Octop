"""Admin router — the "pending users" backlog (T5-4, pluggable module).

Mounted under ``/api/admin``. Platform-admin only.

Design §7.5 / plan:274: a level-⑤ SSO login creates the user but lands
them in NO tenant (fail-closed), which historically left no trace at all.
T4-3 added the trace (``lifecycle.record_pending_resolution`` + the derived
``list_pending_sso_users`` view); this router is the management face on
top:

* ``GET  /pending-users`` — the derived backlog, enriched with the IdP
  alias for display.
* ``POST /pending-users/{user_id}/assign`` — drop the user into a tenant:
  an ``active`` manual membership, ``default_org_id`` claimed when still
  NULL (which is what removes them from the derived backlog), an epoch
  bump so outstanding tokens pick the tenant up on re-login, and an audit
  event.

This is a standalone module on purpose (user convention 2026-09-26:
extract, don't weave): registration is one line in ``app.py``; deleting
the module and that line restores community behavior verbatim. It reuses
``roles._validate_role_keys`` so ability-package validation cannot drift
between the two admin faces.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from octop.api.deps import get_server, require_admin
from octop.infra.users.lifecycle import list_pending_sso_users

router = APIRouter()


class AssignBody(BaseModel):
    org_id: str = Field(min_length=1, max_length=64)
    role_keys: list[str] = Field(default_factory=list)


def _alias_by_provider(server: Any) -> dict[int, str]:
    out: dict[int, str] = {}
    for provider in server.services.sso_repo.list():
        out[provider.id] = provider.idp_alias or provider.display_name or str(provider.id)
    return out


@router.get("/pending-users", summary="SSO users still without a tenant")
async def list_pending_users(
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> list[dict[str, Any]]:
    aliases = _alias_by_provider(server)
    return [
        {
            "id": row.id,
            "username": row.username,
            "display_name": row.display_name,
            "email": row.email,
            "idp_alias": aliases.get(row.sso_provider_id, str(row.sso_provider_id)),
            "created_at": row.created_at,
        }
        for row in list_pending_sso_users(server.services)
    ]


@router.post("/pending-users/{user_id}/assign", summary="Assign a pending user to a tenant")
async def assign_pending_user(
    user_id: int,
    body: AssignBody,
    _: Any = Depends(require_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    from octop.api.routers.roles import _validate_role_keys
    from octop.infra.users.lifecycle import bump_user_epoch

    row = server.services.user_repo.get(user_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"user {user_id} not found")
    # get_org only returns status='active' tenants — assigning into a
    # disabled tenant must fail closed, not silently strand the user.
    if server.services.org_repo.get_org(body.org_id) is None:
        raise HTTPException(status_code=404, detail=f"org {body.org_id!r} not found")
    role_keys = _validate_role_keys(server, body.role_keys)

    existing = server.services.org_repo.get_membership(user_id, body.org_id)
    server.services.org_repo.upsert_membership(
        user_id,
        body.org_id,
        role_keys=role_keys,
        status="active",
        source="manual" if existing is None else existing.source,
    )
    # 028: keep the assigned packages across logins (JIT unions grants with
    # mapped keys instead of wiping them).
    server.services.org_repo.replace_member_grants(
        user_id, body.org_id, role_keys, granted_by="admin"
    )
    org_claimed = False
    if row.default_org_id is None:
        server.services.user_repo.set_default_org_id(user_id, body.org_id)
        org_claimed = True
    # Outstanding tokens carry no tenant (oid claim empty) — force the next
    # request to re-login so the resolution chain lands them (level ③).
    bump_user_epoch(server.services, user_id, reason="pending.assigned")
    server.services.audit_repo.write(
        actor="admin",
        action="user.assign_org",
        target=row.username,
        payload=f"org={body.org_id} roles={','.join(role_keys) or '-'}"
        + (" default_org=claimed" if org_claimed else ""),
        org_id=body.org_id,
    )
    return {
        "user_id": user_id,
        "username": row.username,
        "org_id": body.org_id,
        "role_keys": role_keys,
        "default_org_claimed": org_claimed,
    }
