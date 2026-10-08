"""By-id shared-agent tenancy gate (T2, pluggable module).

User convention 2026-09-26 (extract, don't weave). The two functions here
used to live in the community ``api/common/agent.py``; upstream now keeps
only one lazy hook line inside ``require_agent_row`` (plus the same
one-line gate in ``routers/agents.py``'s listing endpoint). Deleting this
module together with those hooks restores community behaviour verbatim —
which re-opens the by-id cross-tenant share leak, exactly what the hooks
prevent.
"""

from __future__ import annotations

from typing import Any

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.tenancy import shared_resource_visible, viewer_authz

__all__ = ["assert_shared_agent_within_tenant"]


def assert_shared_agent_within_tenant(row: Any, user: Any, *, owner_org: str | None) -> None:
    """Tenancy + role gate for *someone else's* shared agent, reached by id.

    ``agent_repo.list_shared`` already hides cross-tenant shared agents from
    listings, but chat turns / history / cron / workspace reach agents by id —
    without this gate a foreign-tenant user could invoke a shared agent and,
    through it, its owner's connectors and knowledge bases (the agent's MCP
    configs are built from the *owner's* authz).

    Delegates to :func:`octop.infra.users.tenancy.shared_resource_visible`
    (the single source of truth) with ``admin_cross_tenant=True``: the router
    layer mirrors ``can_access_resource``, so admins are platform-wide here.
    The gate reads the *row's* attribution (``org_id``), never a value
    derived from the owner's current org: under the 2026-09-27 three-state
    redesign the row is the single authority, and ``None`` means
    *unattributed* (admins only) — deriving it from the owner would silently
    re-assign admin drafts into their home tenant. The row's
    ``allowed_roles`` narrows exactly as the listing does. Fail-closed: a
    tenant-less viewer sees only explicit platform shares.
    """
    from octop.api.common.agent import agent_is_shared, user_owns_agent

    if user_owns_agent(row, user) or not agent_is_shared(row):
        return
    visible = shared_resource_visible(
        getattr(row, "org_id", None),
        getattr(row, "allowed_roles", None),
        viewer_authz(user),
        admin_cross_tenant=True,
    )
    if not visible:
        raise OctopError(ErrorCode.FORBIDDEN, "agent not available to you")


def agent_owner_org(server: Any, row: Any) -> str | None:
    """The agent owner's tenant, or None when ownerless / legacy test fakes.

    ⚠️ Not the visibility authority any more (2026-09-27): the *row's*
    ``org_id`` is. This helper remains only for callers that genuinely want
    the owner's home tenant (e.g. audit context), never for gating.
    """
    if row.user_id is None:
        return None
    services = getattr(server, "services", None)
    user_repo = getattr(services, "user_repo", None)
    if user_repo is None:
        return None
    owner = user_repo.get(int(row.user_id))
    return owner.default_org_id if owner is not None else None
