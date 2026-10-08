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

__all__ = ["creator_org_id"]


def creator_org_id(user: Any) -> str | None:
    """The ``org_id`` a newly created resource row should carry.

    Platform admins (``is_admin``) → ``None`` (unattributed draft; see the
    module docstring). Everyone else → their own tenant, or ``None`` when
    they have none (legacy/admin-less bootstrap paths).
    """
    if getattr(user, "is_admin", False):
        return None
    return getattr(user, "org_id", None) or None
