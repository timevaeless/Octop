"""Authorization context resolution for non-request code paths.

Request-scoped handlers get ``(org_id, role_keys, is_admin)`` from the
authenticated ``User`` object. Long-lived subsystems (gateway processor,
knowledge retrieval, cron delivery, connector reload) only hold a ``user_id``,
so they need a light resolver that reads the same authoritative sources:
``users`` for the role/tenant pointer and ``org_memberships`` for the
ability-package keys. Mirrors ``UserManager._user_from_row`` without building
a full ``User``.

Two binding flavors (P1-3 discipline, review 2026-09-26):

* :func:`resolve_authz_context` binds for the *remainder of the running
  context*. Right for turn-scoped operations that re-resolve per user
  (agent manager) — each per-user entry rebinds, so nothing leaks between
  users within a task.
* :func:`authz_binding` binds for a *bounded ``with`` window* and unbinds
  on exit. Right for narrow call sites that resolve, run one query, and
  move on (cron delivery, gateway knowledge stamping, validators) — an
  admin's ``bypass_org='on'`` must not outlive the admin's own resolution.
  Prefer this flavor unless the whole operation is turn-scoped.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from octop.infra.users.identity import User


@dataclass(frozen=True)
class AuthzContext:
    user_id: int
    org_id: str | None = None
    role_keys: tuple[str, ...] = ()
    is_admin: bool = False
    # 2026-10-01 tenant-admin visibility (user decision): the ``org_admin``
    # package holder is the tenant's maximally-privileged role — within their
    # own tenant they see every resource, exactly as a platform admin does
    # platform-wide. The tenant boundary itself is untouched (org_id still
    # scopes every listing); only the role narrowing is lifted.
    is_org_admin: bool = False
    # 2026-10-06 identity passthrough: the user's SSO subject at the IdP
    # (portal ``system_users.id``), minted into the delegation token's
    # ``ssu`` claim. ``None`` for local-password users.
    sso_subject: str | None = None

    @property
    def role_filter(self) -> list[str] | None:
        """Value for repo ``role_keys`` parameters.

        ``None`` disables the role filter entirely (platform-admin bypass,
        and the tenant-admin bypass above); any other value (including
        ``[]``) applies the resource ACL.
        """
        return None if (self.is_admin or self.is_org_admin) else list(self.role_keys)


def _is_org_admin(role_keys: Any) -> bool:
    return "org_admin" in (role_keys or ())


def authz_context_from_user(user: User) -> AuthzContext:
    """Build the context from an already-authenticated ``User``."""
    return AuthzContext(
        user_id=user.id,
        org_id=user.org_id,
        role_keys=tuple(user.role_keys),
        is_admin=user.is_admin,
        is_org_admin=(not user.is_admin) and _is_org_admin(user.role_keys),
        sso_subject=getattr(user, "sso_subject", None),
    )


def _resolve_and_bind(user_repo: Any, org_repo: Any, user_id: int) -> tuple[AuthzContext, Any]:
    """Resolve the context and bind its RLS scope; return both plus the token."""
    from octop.infra.users.tenancy_context import bind_tenant_scope

    row = user_repo.get(user_id)
    if row is None:
        # Bind the empty scope rather than leaving whatever was set before:
        # an unknown user must not inherit a prior context's tenant.
        token = bind_tenant_scope(None, bypass=False)
        return AuthzContext(user_id=user_id), token
    role_keys: tuple[str, ...] = ()
    if row.default_org_id is not None:
        membership = org_repo.get_membership(row.id, row.default_org_id)
        if membership is not None and membership.status == "active":
            role_keys = tuple(membership.role_keys)
    authz = AuthzContext(
        user_id=row.id,
        org_id=row.default_org_id,
        role_keys=role_keys,
        is_admin=str(row.role) == "admin",
        is_org_admin=(str(row.role) != "admin") and _is_org_admin(role_keys),
        sso_subject=getattr(row, "sso_subject", None),
    )
    token = bind_tenant_scope(authz.org_id, bypass=authz.is_admin)
    return authz, token


def resolve_authz_context(user_repo: Any, org_repo: Any, user_id: int) -> AuthzContext:
    """Resolve the context from storage and bind its scope for this context.

    Fail-closed: an unknown user resolves to a context with no tenant, no
    role keys and no admin bit, so every role-restricted / tenant-scoped
    resource stays invisible.

    Side effect (T2-4 contract): the resolved tenant is bound as the
    PostgreSQL RLS scope (:mod:`octop.infra.users.tenancy_context`) — every
    subsystem path that resolves a context also equips its subsequent queries
    with the session settings the row-level policies read. Admins get the
    bypass flavor, matching the explicit ``list_all()`` branches at the call
    sites; the org boundary holds for everyone else. Binding is inert on
    SQLite. Callers needing a *different* scope than the resolved user's own
    rebind explicitly via :func:`bind_tenant_scope`.
    """
    authz, _token = _resolve_and_bind(user_repo, org_repo, user_id)
    return authz


@contextmanager
def authz_binding(user_repo: Any, org_repo: Any, user_id: int) -> Iterator[AuthzContext]:
    """Resolve + bind the tenant scope for a bounded window; unbind on exit.

    Narrow call sites (resolve → one query → done) must not leave an
    admin's bypass bound for the rest of the running context. On exit the
    previous scope is restored verbatim via the bind token.
    """
    from octop.infra.users.tenancy_context import reset_tenant_scope

    authz, token = _resolve_and_bind(user_repo, org_repo, user_id)
    try:
        yield authz
    finally:
        reset_tenant_scope(token)
