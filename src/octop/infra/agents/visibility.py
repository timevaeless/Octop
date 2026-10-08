"""Agent-mount visibility scopes for knowledge bases and MCP servers.

Isolated module (upstream-merge safe): everything here is Octop-owned. These
scopes used to live as methods on ``AgentManager``; moving them out of the
shared upstream file keeps the upstream diff minimal — deleting this module
and its call sites restores the upstream behaviour entirely.

Functions take the :class:`~octop.infra.agents.manager.AgentManager` instance
as first parameter to reuse its repo and connector wiring (private attribute
access is deliberate — same package, single owner of those dependencies).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from octop.infra.users.context import resolve_authz_context

if TYPE_CHECKING:  # pragma: no cover
    from octop.infra.agents.manager import AgentManager

__all__ = [
    "visible_knowledge_bases",
    "assignable_knowledge_bases",
    "visible_mcp_servers",
    "assignable_mcp_servers",
]


def visible_knowledge_bases(manager: AgentManager, user_id: int) -> list[Any]:
    """Knowledge bases *user_id* may attach — the save-time scope.

    Single source of truth for ``validate_knowledge_base_ids`` and for the
    composer-defaults picker. An id outside this set is rejected on save, so
    the picker must never offer one.
    """
    authz = resolve_authz_context(manager._repos.user_repo, manager._repos.org_repo, user_id)
    return list(
        manager._repos.knowledge_repo.list_visible(
            user_id, org_id=authz.org_id, role_keys=authz.role_filter
        )
    )


def assignable_knowledge_bases(
    manager: AgentManager, agent_id: str | None, *, user_id: int
) -> list[Any] | None:
    """Bases attachable to an agent; ``None`` when *agent_id* is unknown.

    ``agent_id=None`` describes an agent about to be created, so the scope
    is *user_id*'s own. Editing scopes to the agent's **owner** — the same
    owner ``persist_knowledge_base_ids`` validates against.
    """
    if agent_id is None:
        return visible_knowledge_bases(manager, user_id)
    row = manager._repos.agent_repo.get(agent_id)
    if row is None:
        return None
    if row.user_id is None:
        # Ownerless rows skip validation on save, so nothing is out of reach.
        return list(manager._repos.knowledge_repo.list_all())
    return visible_knowledge_bases(manager, row.user_id)


def visible_mcp_servers(manager: AgentManager, user_id: int) -> list[dict[str, Any]]:
    """MCP servers *user_id* may attach — the save-time scope.

    Single source of truth for ``validate_mcp_servers`` and for the
    composer-defaults connector picker. A name outside this set is rejected
    on save, so the picker must never offer one.
    """
    authz = resolve_authz_context(manager._repos.user_repo, manager._repos.org_repo, user_id)
    return list(manager._connector_svc.list_assignable_instances_for_api(user_id, authz))


def assignable_mcp_servers(
    manager: AgentManager, agent_id: str | None, *, user_id: int
) -> list[dict[str, Any]] | None:
    """MCP servers attachable to an agent; ``None`` when *agent_id* is unknown.

    ``agent_id=None`` describes an agent about to be created, so the scope is
    *user_id*'s own. Editing scopes to the agent's **owner** — the same owner
    ``persist_mcp_servers`` validates against.

    Ownerless rows are the one asymmetry with :func:`assignable_knowledge_bases`
    (which can fall back to ``list_all()``): the connector scope is built per
    user, so there is no global listing to fall back to. The caller's own set
    is the safe answer — saving skips validation for those rows, so under-
    offering is the only direction that cannot produce a rejected pick.
    """
    if agent_id is None:
        return visible_mcp_servers(manager, user_id)
    row = manager._repos.agent_repo.get(agent_id)
    if row is None:
        return None
    if row.user_id is None:
        return visible_mcp_servers(manager, user_id)
    return visible_mcp_servers(manager, row.user_id)
