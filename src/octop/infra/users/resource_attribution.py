"""Resource-creation attribution rule (2026-09-27 redesign, pluggable).

User decision 2026-09-27: platform admins publish *unattributed* resources.
A row created by a platform admin gets ``org_id=None`` (visible to nobody
until an admin assigns attribution on the 资源授权范围 face); a tenant user
stamps their own tenant, exactly as before. Attribution can afterwards be
changed per row through the admin resource-acl endpoints — creation is no
longer the final word.

Delete this module together with the call-site hooks and the attribution
parameter on the resource-acl endpoints to restore the pre-redesign
behaviour (creator's org stamped unconditionally at creation).
"""

from __future__ import annotations

from typing import Any

from octop.infra.errors import ErrorCode, OctopError

__all__ = ["creator_org_id", "creator_org_id_checked"]


def creator_org_id(user: Any) -> str | None:
    """The ``org_id`` a newly created resource row should carry.

    Platform admins (``is_admin``) → ``None`` (unattributed draft; see the
    module docstring). Everyone else → their own tenant, or ``None`` when
    they have none (legacy/admin-less bootstrap paths).
    """
    if getattr(user, "is_admin", False):
        return None
    return getattr(user, "org_id", None) or None


def creator_org_id_checked(user: Any) -> str | None:
    """``creator_org_id`` with a fail-fast guard for unattributed callers.

    Migration 029 removed the ``org_id IS NULL`` branch from the RLS WITH
    CHECK on the resource tables: an unattributed row is only writable
    through the platform-admin bypass scope. A non-admin caller without a
    tenant would therefore fail at INSERT time with a raw RLS
    ``InsufficientPrivilege`` (HTTP 500); reject up front with an
    actionable 403 instead (「无权限，请联系管理员归位」).
    """
    if not getattr(user, "is_admin", False) and getattr(user, "org_id", None) is None:
        raise OctopError(
            ErrorCode.USER_NOT_ATTRIBUTED,
            "account has no tenant attribution; ask an administrator to assign one",
        )
    return creator_org_id(user)
