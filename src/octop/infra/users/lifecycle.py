"""Account / tenant lifecycle reactions that keep outstanding JWTs honest.

T4-3 (multi-tenant plan:258, design §7.4). Extracted as a **pluggable
module**: the only upstream touch points are one-line hooks (user disable
in the users router, org disable/delete in the roles router, the JIT
pending branch in the user manager). Deleting this file together with the
hooks restores community behaviour verbatim — nothing else depends on it,
and every hook imports lazily so the module can be dropped without import
ripples.

Why epoch bumps matter
----------------------
``users.token_epoch`` is embedded as the ``ver`` claim of every access
token and re-checked against the DB on every request
(:func:`octop.api.deps.resolve_user_from_token`). Bumping the column makes
every outstanding token stale, so the client is told to re-login.

* Disabling a user already evicts the manager cache, so their token is
  rejected as ``USER_DISABLED`` — but without a bump, a later ``enable``
  would resurrect the *pre-disable* token verbatim (same epoch). Bumping
  on disable closes that cycle.
* Disabling or deleting a tenant previously did nothing to outstanding
  member tokens: they carried the org context until natural expiry, and
  the fail-closed resolution chain only kicks in on the *next* login.
  Tenant-level bumps close that window.

Pending trace (level ⑤)
-----------------------
A level-⑤ resolution produces NO membership row — ``org_memberships.org_id``
is NOT NULL and there is no org to reference — so the design's "pending
membership row" is physically impossible. The admin backlog is instead:

* one ``user.jit_pending`` audit event on the *first* pending login (later
  logins stay silent; the audit trail must not fill up with retries), and
* a derived live view, :func:`list_pending_sso_users` — SSO-provisioned,
  enabled users whose ``default_org_id`` is still NULL.

T5-4's management face can consume both without any new table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import for typing only
    from octop.infra.db.repos.users import UserRow

__all__ = [
    "bump_org_epochs",
    "bump_user_epoch",
    "list_pending_sso_users",
    "record_pending_resolution",
]

PENDING_AUDIT_ACTION = "user.jit_pending"


def bump_user_epoch(services: Any, user_id: int, *, reason: str) -> int:
    """Invalidate every outstanding token of one user; return the new epoch."""
    epoch = services.user_repo.bump_token_epoch(user_id)
    services.audit_repo.write(
        actor="admin",
        action="user.token_epoch_bump",
        target=str(user_id),
        payload=f"reason={reason} epoch={epoch}",
    )
    return epoch


def bump_org_epochs(services: Any, org_id: str, *, reason: str) -> int:
    """Invalidate every outstanding token of ``org_id``'s current members.

    Must be called while the memberships still exist (before a cascade
    delete). Returns the number of members bumped; writes one audit event
    under the target org.
    """
    count = services.user_repo.bump_token_epoch_for_org(org_id)
    services.audit_repo.write(
        actor="admin",
        action="org.token_epoch_bump",
        target=org_id,
        payload=f"reason={reason} users={count}",
        org_id=org_id,
    )
    return count


def record_pending_resolution(services: Any, user_row: Any, *, idp_alias: str) -> None:
    """Leave the discoverable trace for a level-⑤ (no tenant) login.

    Once per user: the derived backlog (:func:`list_pending_sso_users`) is
    the live view, so repeating the event on every re-login would only
    churn the audit log. Nothing here blocks or redirects the login — the
    user stays in fail-closed pending state exactly as before.
    """
    prior = services.audit_repo.query(actor=user_row.username, action=PENDING_AUDIT_ACTION, limit=1)
    if prior:
        return
    services.audit_repo.write(
        actor=user_row.username,
        action=PENDING_AUDIT_ACTION,
        target=user_row.username,
        payload=f"idp={idp_alias or '-'}",
        org_id=None,
    )


def list_pending_sso_users(services: Any) -> "list[UserRow]":
    """Return the live pending backlog: SSO users still without a tenant.

    Derived, stateless, pluggable — SSO-provisioned, enabled users whose
    ``default_org_id`` is NULL. Local (password) users are admin-created
    and never enter the resolution chain, so they are not pending.
    """
    pending: list[UserRow] = []
    for row in services.user_repo.list(include_disabled=False):
        if row.default_org_id is None and getattr(row, "sso_provider_id", None):
            pending.append(row)
    return pending
