"""Tenant-scoped agent workspace resolution (T2-3, pluggable module).

User convention 2026-09-26 (extract, don't weave). The community
``api/common/agent_workspace.py`` ends with ``paths.ensure_agent_workspace
(agent_id)`` (flat ``agents/<id>``); the only upstream touch point is a
three-line lazy hook delegating the default-layout branch here:

    # T2-3 pluggable hook — octop.api.common.agent_workspace_tenancy
    from octop.api.common.agent_workspace_tenancy import scoped_workspace_dir

    return scoped_workspace_dir(server, agent_id, paths)

Deleting the module together with that hook restores the community flat
layout verbatim.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, cast

__all__ = ["scoped_workspace_dir"]


def _ensure_workspace_compat(paths: Any, agent_id: str, org_id: str | None) -> Path:
    """Call ``ensure_agent_workspace`` with org_id when the target accepts it.

    Test doubles and alternate layouts may only implement the legacy
    single-argument form; probe the signature instead of guessing.
    """
    ensure = paths.ensure_agent_workspace
    try:
        params = inspect.signature(ensure).parameters
        accepts_org = "org_id" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except (TypeError, ValueError):  # pragma: no cover - builtins / mocks
        accepts_org = False
    if accepts_org:
        return cast(Path, ensure(agent_id, org_id=org_id))
    return cast(Path, ensure(agent_id))


def scoped_workspace_dir(server: Any, agent_id: str, paths: Any) -> Path:
    """Default workspace under the tenant layout: ``orgs/<org>/agents/<id>``.

    Resolves the agent's org from the repo (defensively — the layout must
    never 500) and lands the workspace under ``orgs/<org>/agents/<id>``;
    platform agents go to ``shared/agents/<id>``. Legacy flat
    ``agents/<id>`` directories keep working through adopt-legacy
    resolution in :class:`octop.infra.utils.tenant_paths`.
    """
    org_id = None
    try:
        repos = getattr(server, "services", None)
        repos = getattr(repos, "repos", repos)
        agent_row = repos.agent_repo.get(agent_id) if repos is not None else None
        org_id = getattr(agent_row, "org_id", None) if agent_row is not None else None
    except Exception:  # pragma: no cover - defensive: layout must not 500
        org_id = None
    return _ensure_workspace_compat(paths, agent_id, org_id)
