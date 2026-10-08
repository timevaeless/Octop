"""Tenant-scoped visibility rules for shared resources (Octop extension).

Upstream Octop has no tenancy concept. Everything in this module is the
multi-tenant layer (migrations 016–018) concentrated in ONE place, so an
upstream merge only ever conflicts at thin call sites — the rule itself has a
single definition here.

The canonical predicate (docs/resource-authorization-design.md; 2026-09-27
three-state attribution redesign — user decision):

    admin bypass  OR  ( org match  AND  ( allowed_roles empty  OR  intersect ) )

``org`` is the *hard* boundary: no role grant can cross it. The row's
``org_id`` carries three states:

* ``None`` — *unattributed* (a platform-admin draft): invisible to every
  tenant; only the admin request paths (the bypass above) reach it.
* ``'*'`` (:data:`PLATFORM_SHARED_ORG`) — *explicitly platform-shared*:
  visible to every tenant, with ``allowed_roles`` narrowing across tenants.
* a tenant id — visible inside that tenant only.

Roles only ever *narrow within* a reachable row: an empty/absent
``allowed_roles`` means "unrestricted", a non-empty one requires an
intersection with the viewer's ability-package keys (fail-closed for a
viewer holding no keys).

Two enforcement flavors share this predicate, selected by
``admin_cross_tenant``:

* ``True`` — request/router rule, mirroring ``can_access_resource``: an admin
  is platform-wide. Used by by-id router gates.
* ``False`` — runtime/listing rule, mirroring ``repo.list_visible``: the org
  boundary holds even for admins (the full admin bypass lives at the request
  layer). Used by runtime mounting, where a cross-tenant mount would leak the
  owner's business-system credentials into another tenant's agents.
"""

from __future__ import annotations

from typing import Any

from octop.infra.users.context import AuthzContext
from octop.infra.users.resource_acl import PLATFORM_SHARED_ORG, roles_allow

__all__ = ["PLATFORM_SHARED_ORG", "shared_resource_visible", "shared_row_visible", "viewer_authz"]


def viewer_authz(user: Any) -> AuthzContext:
    """Build an :class:`AuthzContext` from any user-like object.

    Request handlers receive a full ``User``, but tests and a few internal
    callers pass lightweight fakes; every field is read tolerantly so the
    gate degrades to "no tenant, no roles" instead of raising.
    """
    return AuthzContext(
        user_id=user.id,
        org_id=getattr(user, "org_id", None),
        role_keys=tuple(getattr(user, "role_keys", None) or ()),
        is_admin=bool(getattr(user, "is_admin", False)),
        is_org_admin=(
            not bool(getattr(user, "is_admin", False))
            and "org_admin" in (getattr(user, "role_keys", None) or ())
        ),
    )


def shared_resource_visible(
    resource_org_id: str | None,
    resource_allowed_roles: list[str] | None,
    viewer: AuthzContext,
    *,
    admin_cross_tenant: bool = True,
) -> bool:
    """Whether ``viewer`` may see / mount a shared resource row.

    ``resource_org_id`` / ``resource_allowed_roles`` come straight off the row
    (``None`` = unattributed, admin-reachable only; ``'*'`` = explicit
    platform share; otherwise the tenant boundary). See the module docstring
    for the two admin flavors.
    """
    if viewer.is_admin and admin_cross_tenant:
        return True
    if resource_org_id is None:
        # Unattributed drafts: the request-flavor bypass above is the only
        # way in; tenants — and the runtime flavor, whose cross-tenant admin
        # bypass is deliberately withheld — never reach them.
        return False
    if resource_org_id != PLATFORM_SHARED_ORG and resource_org_id != viewer.org_id:
        return False
    if viewer.is_admin or viewer.is_org_admin:
        # Same tenant (or platform-shared) admin: the role gate is lifted —
        # AuthzContext.role_filter is None for admins and must never reach
        # roles_allow, which fails closed on None keys. Platform admins are
        # platform-wide; tenant admins (``org_admin``) are the maximally
        # privileged role *within their own tenant* (2026-10-01 decision) —
        # the org boundary above still binds them.
        return True
    return roles_allow(resource_allowed_roles, viewer.role_filter)


def shared_row_visible(row: Any, authz: Any) -> bool:
    """Gate one shared *row* (with ``org_id`` / ``allowed_roles``) for a viewer.

    Thin adapter used by shared-expansion loops (custom-MCP parents et al.):
    the row's ``org_id`` is the hard boundary, a non-empty ``allowed_roles``
    only narrows *within* it. ``authz=None`` keeps the legacy unfiltered
    behavior for internal callers with no user context.
    """
    if authz is None:
        return True
    return shared_resource_visible(
        getattr(row, "org_id", None),
        getattr(row, "allowed_roles", None),
        authz,
        admin_cross_tenant=False,
    )
