"""Tenant-aware token issuing and resource-ACL decisions (T2, pluggable
module).

User convention 2026-09-26 (extract, don't weave): everything that puts the
tenant context *into* a JWT — or decides resource visibility *from* one —
lives here. The community ``deps.sign_token`` stays verbatim; its claims
(``oid`` / ``rls`` / ``ver``) are added by this module's own signing path.

Upstream touch points (all one-to-three-line hooks in ``api/deps.py``):

* ``issue_access_token`` / ``can_access_resource`` — one-line lazy
  delegates so the five historical call sites keep their import name;
* ``resolve_user_from_token`` — one lazy ``assert_token_epoch`` call (the
  ``ver`` gate that kills pre-change tokens after an epoch bump).

Deleting this module together with those hooks restores community
behaviour verbatim: tokens go back to carrying no tenant context and the
epoch gate disappears.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import jwt

from octop.infra.errors import ErrorCode, OctopError

if TYPE_CHECKING:  # pragma: no cover - import for typing only
    from octop.infra.server import OctopServer
    from octop.infra.users.identity import User

__all__ = [
    "assert_token_epoch",
    "can_access_resource",
    "issue_access_token",
]


def _sign_claims(secret: bytes, user: "User", *, ttl_seconds: int) -> str:
    """Sign a token carrying the tenant claims (``oid`` / ``rls`` / ``ver``).

    ``ver`` is always present (defaults to 0) so a membership change that
    bumps ``users.token_epoch`` invalidates every token issued before it.
    """
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(user.id),
        "uname": user.username,
        "role": str(user.role),
        "ver": int(user.token_epoch or 0),
        "iat": now,
        "exp": now + ttl_seconds,
    }
    if user.org_id is not None:
        payload["oid"] = user.org_id
    if user.role_keys:
        payload["rls"] = list(user.role_keys)
    return jwt.encode(payload, secret, algorithm="HS256")


def issue_access_token(
    server: "OctopServer",
    user: "User",
    *,
    ttl_seconds: int | None = None,
) -> str:
    """Single token-issuing path for login / setup / invite / OIDC / sliding renew.

    Deriving ``oid`` / ``rls`` / ``ver`` from the ``User`` object here — instead
    of at each call site — is what keeps the five historical ``sign_token``
    call sites from drifting apart.
    """
    assert server.services is not None
    secret = server.services.secret_repo.get("jwt")
    if secret is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "jwt secret missing")
    ttl = int(ttl_seconds or server.services.config.access_token_ttl_seconds)
    return _sign_claims(secret, user, ttl_seconds=ttl)


def can_access_resource(
    user: "User",
    *,
    resource_org_id: str | None,
    allowed_roles: list[str] | None,
) -> bool:
    """Server-side resource ACL decision (design: resource-authorization-design.md §3.2).

    Three-state attribution (2026-09-27): ``None`` = unattributed (admins
    only), ``'*'`` = explicit platform share, otherwise the hard tenancy
    boundary. ``allowed_roles`` can only narrow *within* a reachable row,
    never widen across it. ``allowed_roles == []`` means "no role
    restriction" (tenancy alone decides). Fail-closed: an empty
    ``user.role_keys`` only ever sees role-unrestricted resources.
    """
    if user.is_admin:
        return True
    if resource_org_id is None:
        return False
    from octop.infra.users.resource_acl import PLATFORM_SHARED_ORG

    if resource_org_id != PLATFORM_SHARED_ORG and resource_org_id != user.org_id:
        return False
    if user.is_admin or "org_admin" in (user.role_keys or ()):
        # Tenant-admin lift (2026-10-01): within the org gate that already
        # passed, the ``org_admin`` holder manages every resource of their
        # tenant (and reads platform-shared rows) regardless of their own
        # ability packages — mirroring AuthzContext.is_org_admin.
        return True
    if not allowed_roles:
        return True
    return bool(set(allowed_roles) & set(user.role_keys))


def assert_token_epoch(
    server: "OctopServer", payload: dict[str, Any], user: "User"
) -> None:
    """The ``ver`` gate: reject tokens issued before the last epoch bump.

    Round-10 review fix (2026-09-26): the epoch is read **from the database**,
    not from the in-memory ``User`` cache. The cache is only refreshed on some
    bump paths (``roles.set_membership``, ``users.disable``); on the others
    (``org_members.set_member_roles``, ``pending_users.assign``,
    ``bump_org_epochs``) a cache-first comparison accepted stale tokens and
    rejected freshly re-issued ones — the exact inverse of the intended
    semantics. DB is the single authority here; the cached ``user`` object
    only carries identity fields. Fail-closed: a row that disappeared or was
    disabled between caching and this check is rejected too.
    """
    assert server.services is not None
    row = server.services.user_repo.get(int(payload["sub"]))
    if row is None or int(getattr(row, "disabled", 0) or 0):
        raise OctopError(ErrorCode.USER_DISABLED, "user not active")
    ver = int(payload.get("ver", 0) or 0)
    if ver != int(row.token_epoch or 0):
        raise OctopError(ErrorCode.TOKEN_EXPIRED, "token stale: authorization context changed")
