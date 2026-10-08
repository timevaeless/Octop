"""Invite → tenant binding (T4-1, pluggable module).

Design §7.1 / t4-inventory: ``org_memberships.source`` reserved ``'invite'``
since migration 018, but nothing ever wrote it — ``InviteService.redeem``
created a bare user with no tenant, so invited users never landed anywhere
(a dead enum). This module closes that gap:

* an admin creates an invite **bound to a tenant** (``user_invites.org_id``,
  migration 026, nullable — ``NULL`` keeps the community behaviour of a
  tenant-less invite verbatim);
* at redeem time the user is dropped into that tenant **inside the same
  transaction** that flips the invite to ``used``: an ``active`` membership
  with ``source='invite'`` and empty role keys (packages are granted later
  through the membership admin endpoints — an invite must never be a
  privilege grant), plus ``users.default_org_id`` claimed when still NULL.

Fail-closed: if the tenant was deleted or disabled between invite creation
and redemption, redeem raises (the whole transaction — user creation
included — rolls back). A stale invite must not strand a user outside
every tenant while pretending the binding succeeded.

Pluggable per user convention 2026-09-26 (extract, don't weave): the only
upstream touch points are one-line lazy hooks — ``redeem_creating_user``
(repo, the in-transaction binding), ``InviteService.create`` (validation)
and ``InviteService.redeem`` (the audit echo). Deleting this file and the
hooks restores community behaviour verbatim.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from octop.infra.errors import ErrorCode, OctopError

if TYPE_CHECKING:  # pragma: no cover - import for typing only
    from octop.infra.db.repos.invites import InviteRow

__all__ = [
    "apply_invite_membership",
    "record_invite_org_audit",
    "validate_invite_org",
]


def validate_invite_org(services: Any, org_id: str | None) -> str | None:
    """Clean and check the tenant an invite is being bound to.

    Uses ``get_org`` (active tenants only): binding an invite to a disabled
    or unknown tenant would only produce a guaranteed-to-fail redemption.
    """
    cleaned = (org_id or "").strip()
    if not cleaned:
        return None
    if services.org_repo.get_org(cleaned) is None:
        raise OctopError(
            ErrorCode.NOT_FOUND,
            f"org {cleaned!r} not found or not active",
            status=404,
        )
    return cleaned


def apply_invite_membership(conn: Any, *, user_id: int, invite: "InviteRow") -> str | None:
    """Bind the freshly-created user into the invite's tenant (in-transaction).

    Called from ``InviteRepo.redeem_creating_user`` right after the user row
    is inserted and the invite flipped to ``used`` — same transaction, so a
    failure below rolls the whole redemption back. Returns the bound
    ``org_id`` or ``None`` for a tenant-less (community) invite.

    Raises ``OctopError(409)`` when the tenant vanished or was disabled
    after the invite was issued — fail-closed, per the module docstring.
    """
    org_id = (getattr(invite, "org_id", None) or "").strip() or None
    if org_id is None:
        return None
    row = conn.execute("SELECT status FROM orgs WHERE org_id = ?", (org_id,)).fetchone()
    if row is None or str(row["status"]) != "active":
        raise OctopError(
            ErrorCode.INVITE_INVALID,
            f"invite tenant {org_id!r} is no longer available",
            status=409,
        )
    ts = int(invite.used_at or 0)
    conn.execute(
        "INSERT INTO org_memberships("
        "user_id, org_id, role_keys, status, is_primary, source, created_at, updated_at"
        ") VALUES (?, ?, ?, 'active', 0, 'invite', ?, ?)",
        (user_id, org_id, json.dumps([], ensure_ascii=False), ts, ts),
    )
    conn.execute(
        "UPDATE users SET default_org_id = ? WHERE id = ? AND default_org_id IS NULL",
        (org_id, user_id),
    )
    return org_id


def record_invite_org_audit(services: Any, *, username: str, org_id: str | None) -> None:
    """Audit echo for the binding applied at redeem time (no-op when unbound)."""
    if not org_id:
        return
    services.audit_repo.write(
        actor=username,
        action="user.invite_org",
        target=username,
        payload=f"org={org_id} source=invite",
        org_id=org_id,
    )
