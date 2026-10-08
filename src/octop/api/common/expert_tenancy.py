"""Published-expert tenancy + ACL gates (T2, pluggable module).

User convention 2026-09-26 (extract, don't weave). Woven pieces that used
to live in ``api/routers/experts.py``:

* tenant-scoped snapshot dir resolution (``orgs/<org>/published_experts/<id>``
  or ``shared/...``, legacy flat adopted);
* the L2 tenancy+ACL gate for detail reads and snapshot installs;
* the visible-rows list for the catalog endpoint;
* the publish-time org stamping rule.

Upstream keeps one-to-three-line lazy hooks at the five call sites and the
additive ``PublishExpertBody.allowed_roles`` / payload keys. Deleting this
module together with those hooks restores community behaviour verbatim
(which re-opens the by-id published-template leak the gate closes).
"""

from __future__ import annotations

from typing import Any

from octop.infra.errors import ErrorCode, OctopError

__all__ = [
    "publish_org_for",
    "published_snapshot_dir",
    "require_accessible_published_expert",
    "visible_published_rows",
]


def published_snapshot_dir(server: Any, expert_id: str) -> Any:
    """Tenant-scoped snapshot dir (T2-3): orgs/<org>/ or shared/ for platform rows."""
    org_id = None
    try:
        row = server.services.published_expert_repo.get(expert_id)
        org_id = getattr(row, "org_id", None) if row is not None else None
    except Exception:  # pragma: no cover - defensive: layout must not 500
        org_id = None
    return server.services.paths.published_expert_snapshot_dir(expert_id, org_id=org_id)


def require_accessible_published_expert(server: Any, expert_id: str, user: Any) -> Any:
    """Fetch a published template AND enforce tenancy + role ACL (L2).

    Detail reads and snapshot installs must not trust the id alone (the
    historic ``TODO(multi-tenant T2-1)``): knowing an id must not reveal a
    resource the caller's tenant or ability packages cannot see.
    """
    from octop.infra.agents.experts.published_creation import require_published_expert
    from octop.infra.users.token_tenancy import can_access_resource

    row = require_published_expert(server.services, expert_id)
    if not can_access_resource(user, resource_org_id=row.org_id, allowed_roles=row.allowed_roles):
        raise OctopError(ErrorCode.FORBIDDEN, "resource not available for your role")
    return row


def visible_published_rows(server: Any, user: Any) -> list[Any]:
    """Rows visible from the caller's tenant and roles.

    Admin callers (the resource-acl face) see every row, including
    unattributed drafts. A tenant caller sees own-tenant rows plus explicit
    platform shares (``org_id='*'``); the resource ACL narrows as usual.
    """
    repo = server.services.published_expert_repo
    if user.is_admin:
        return repo.list_all_orgs()
    return repo.list_visible(user.org_id, list(user.role_keys))


def publish_org_for(user: Any) -> str | None:
    """The org a published template belongs to (2026-09-27 rule).

    A platform admin publishes *unattributed* (``None``) — the template is
    admin-reachable only until attribution is assigned on the resource-acl
    face. A tenant user publishes into their own org, as before.
    """
    return None if user.is_admin else (user.org_id or None)
