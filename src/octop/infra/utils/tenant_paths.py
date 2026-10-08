"""Workspace path plane (T2-3, pluggable module).

User convention 2026-09-26 (extract, don't weave): the path mixin lives here
as a module. The only upstream touch point is one line in
``infra/utils/paths.py`` — the import and the mixin base of ``PathLayout``:

    from octop.infra.utils.tenant_paths import TenantPathLayoutMixin

    @dataclass(frozen=True)
    class PathLayout(TenantPathLayoutMixin):

Deleting that module together with those two lines restores the community
flat layout verbatim (``agents/<id>``, ``knowledge/<id>``,
``published_experts/<id>``).

Layout (simplified 2026-10-08, user decision)
---------------------------------------------
All content lives under ``shared/...`` regardless of the row's ``org_id``;
the legacy flat dirs (``agents/<id>`` etc.) keep working through
adopt-legacy resolution (:func:`_scoped_dir`) for community-layout installs.

Tenant visibility is enforced purely at the DB layer — every tenant-scoped
table (``published_experts``, ``agents``, ``knowledge_bases``, ...) carries
the ``tenant_isolation`` RLS policy, and no tenant-side process ever reads
these directories directly (agent file tools are rooted at each agent's own
workspace).  Attribution changes are therefore a pure DB update with no
filesystem counterpart: re-attributing a published expert no longer
requires relocating its snapshot (2026-10-08 "未找到。" incident).
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["TenantPathLayoutMixin"]


def _scoped_dir(scoped: Path, legacy: Path) -> Path:
    """Adopt-legacy resolution for the community flat layout.

    Prefer the ``shared/`` path; keep using the legacy flat path when it
    already exists (existing installs keep working without a bulk data
    move); create the ``shared/`` path otherwise.
    """
    if scoped.is_dir():
        return scoped
    if legacy.is_dir():
        return legacy
    return scoped


class TenantPathLayoutMixin:
    """Path plane mixed into the community :class:`PathLayout`.

    Reads ``self.root`` plus the legacy ``agents_dir`` /
    ``published_experts_dir`` / ``knowledge_dir`` properties from the host
    class — it adds methods only, never fields.
    """

    @property
    def shared_dir(self) -> Path:
        """Platform-wide content root: ``~/.octop/shared/``."""
        return self.root / "shared"

    def agent_workspace(self, agent_id: str, org_id: str | None = None) -> Path:
        """Agent workspace: ``shared/agents/<agent_id>``.

        ``org_id`` is accepted for call-site compatibility and ignored —
        attribution never moves directories (see module docstring).  Falls
        back to the legacy ``agents/<agent_id>`` when that already exists.
        """
        return _scoped_dir(
            self.shared_dir / "agents" / agent_id,
            self.agents_dir / agent_id,
        )

    def ensure_agent_workspace(self, agent_id: str, org_id: str | None = None) -> Path:
        """Agent workspace, mkdir -p."""
        out = self.agent_workspace(agent_id, org_id=org_id)
        out.mkdir(parents=True, exist_ok=True)
        return out

    def published_expert_snapshot_dir(
        self, expert_id: str, org_id: str | None = None
    ) -> Path:
        """Published expert snapshot dir: ``shared/published_experts/<id>``.

        ``org_id`` is accepted for call-site compatibility and ignored —
        who may see (and install) a published expert is decided by the
        ``tenant_isolation`` RLS policy on the row, never by the directory
        layout.  Falls back to the legacy flat ``published_experts/<id>``
        when that already exists.
        """
        return _scoped_dir(
            self.shared_dir / "published_experts" / expert_id,
            self.published_experts_dir / expert_id,
        )

    def knowledge_base_dir(self, kb_id: str, org_id: str | None = None) -> Path:
        """Knowledge base file dir: ``shared/knowledge/<kb_id>``.

        ``org_id`` is accepted for call-site compatibility and ignored
        (see module docstring).  Falls back to the legacy flat
        ``knowledge/<kb_id>`` when that already exists.
        """
        return _scoped_dir(
            self.shared_dir / "knowledge" / kb_id,
            self.knowledge_dir / kb_id,
        )
