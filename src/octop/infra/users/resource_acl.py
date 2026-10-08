"""Resource-ACL predicates shared by every visible-row query (T2, pluggable
module).

User convention 2026-09-26 (extract, don't weave): the ability-package ACL
logic lives here instead of being woven into the community repos. Before
this module the same helpers existed as four byte-identical
``_parse_role_list`` copies (agents / connectors / knowledge /
published_experts repos) plus ``roles_allow`` on the community-shared
``repos/_base.py``; the per-repo visibility loops now delegate to
:func:`visible_shared_row`.

``_base.py`` is back to community verbatim — that is the whole point.

Upstream touch points remaining (all one-to-two-line hooks):

* ``repos/agents.py`` ``list_shared`` — one comprehension calling
  :func:`visible_shared_row`;
* ``repos/connectors.py`` / ``repos/knowledge.py`` ``list_visible`` — the
  owner-bypass branch stays, the shared-row gate is one
  :func:`visible_shared_row` call;
* ``repos/published_experts.py`` ``list_visible`` — one comprehension;
* ``infra/users/tenancy.py`` — imports :func:`roles_allow` from here.

Deleting this module together with those hooks restores community
behaviour verbatim (and re-breaks the cross-tenant share leak, which is
exactly what the hooks are for).
"""

from __future__ import annotations

import json
from collections.abc import Sequence

__all__ = [
    "PLATFORM_SHARED_ORG",
    "parse_role_list",
    "roles_allow",
    "visible_shared_row",
]

# 2026-09-27 attribution redesign (user decision): ``org_id`` carries three
# states. ``None`` = *unattributed* (a platform-admin draft: only the
# platform-admin request paths may reach it, never a tenant); ``'*'`` =
# *explicitly platform-shared* (every tenant, ``allowed_roles`` narrowing
# applies across tenants); anything else = that tenant (hard boundary).
# The old semantics — ``NULL`` meaning "platform-shared" — is retired: an
# admin creation must not silently become world-visible.
PLATFORM_SHARED_ORG = "*"


def parse_role_list(raw: object) -> list[str]:
    """Parse an ``allowed_roles`` JSON column value into string keys."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "[]")
        except (ValueError, TypeError):
            return []
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    return []


def roles_allow(allowed_roles: object, role_keys: object) -> bool:
    """Resource-ACL predicate for ``allowed_roles`` JSON columns.

    Empty/absent allow-list = role-unrestricted (tenancy alone decides).
    Otherwise at least one ability-package key must intersect. Fail-closed:
    a caller with no role keys only ever sees unrestricted resources.
    """
    if not allowed_roles:
        return True
    if not role_keys:
        return False
    # JSON column values are typed ``object`` by design; runtime shape is a
    # list of strings (guarded by the truthiness checks above).
    return bool(set(map(str, allowed_roles)) & set(map(str, role_keys)))  # type: ignore[call-overload]


def visible_shared_row(
    row_org_id: str | None,
    row_allowed_roles: object,
    *,
    viewer_org_id: str | None,
    role_keys: Sequence[str] | None,
) -> bool:
    """Tenancy + ACL gate for one shared row (three-state attribution).

    * ``row_org_id is None`` — unattributed draft: only the platform-admin
      paths (``role_keys=None``) may see it; a tenant never does.
    * ``row_org_id == PLATFORM_SHARED_ORG`` — explicit platform share: no
      tenant boundary, the resource ACL narrows across tenants.
    * anything else — that tenant's hard boundary; the ACL narrows within.

    ``role_keys=None`` disables the role filter (platform-admin paths); any
    list applies the resource ACL.
    """
    if row_org_id is None:
        return role_keys is None
    if row_org_id != PLATFORM_SHARED_ORG and row_org_id != viewer_org_id:
        return False
    if role_keys is None:
        return True
    return roles_allow(row_allowed_roles, role_keys)
