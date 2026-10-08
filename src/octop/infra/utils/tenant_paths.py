"""Tenant-scoped filesystem layout (T2-3, pluggable module).

User convention 2026-09-26 (extract, don't weave): the whole tenant path
plane lives here as a mixin. The only upstream touch point is one line in
``infra/utils/paths.py`` — the import and the mixin base of ``PathLayout``:

    from octop.infra.utils.tenant_paths import TenantPathLayoutMixin

    @dataclass(frozen=True)
    class PathLayout(TenantPathLayoutMixin):

Deleting that module together with those two lines restores the community
flat layout verbatim (``agents/<id>``, ``knowledge/<id>``,
``published_experts/<id>``).

Layout
------
Tenant-scoped content lives under ``orgs/<org_id>/...``, platform-shared
content under ``shared/...``.  Legacy flat dirs keep working through
adopt-legacy resolution (:func:`_scoped_dir`): an existing install keeps
reading its old directories without a bulk data move, new content is
created scoped.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["TenantPathLayoutMixin"]


def _safe_segment(value: str) -> str:
    """Filesystem-safe path segment for tenant ids (org ids are ULIDs today)."""
    seg = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)[:80]
    return seg or "_"


def _scoped_dir(scoped: Path, legacy: Path) -> Path:
    """Adopt-legacy resolution for the T2-3 tenant layout.

    Prefer the org-scoped path; keep using the legacy flat path when it
    already exists (existing installs keep working without a bulk data
    move); create the scoped path otherwise.
    """
    if scoped.is_dir():
        return scoped
    if legacy.is_dir():
        return legacy
    return scoped


class TenantPathLayoutMixin:
    """Tenant path plane mixed into the community :class:`PathLayout`.

    Reads ``self.root`` plus the legacy ``agents_dir`` /
    ``published_experts_dir`` / ``knowledge_dir`` properties from the host
    class — it adds methods only, never fields.
    """

    @property
    def shared_dir(self) -> Path:
        """Platform-wide content every tenant may read: ``~/.octop/shared/``."""
        return self.root / "shared"

    @property
    def orgs_dir(self) -> Path:
        """Tenant-scoped content root: ``~/.octop/orgs/``."""
        return self.root / "orgs"

    def org_dir(self, org_id: str) -> Path:
        return self.orgs_dir / _safe_segment(org_id)

    def agent_workspace(self, agent_id: str, org_id: str | None = None) -> Path:
        """Agent workspace, tenant-scoped when the agent belongs to an org.

        Tenant agent: ``orgs/<org>/agents/<agent_id>``; platform agent:
        ``shared/agents/<agent_id>``.  Either falls back to the legacy
        ``agents/<agent_id>`` when that directory already exists.
        """
        if org_id:
            return _scoped_dir(
                self.org_dir(org_id) / "agents" / agent_id,
                self.agents_dir / agent_id,
            )
        return _scoped_dir(
            self.shared_dir / "agents" / agent_id,
            self.agents_dir / agent_id,
        )

    def ensure_agent_workspace(self, agent_id: str, org_id: str | None = None) -> Path:
        """Agent workspace (tenant-scoped), mkdir -p."""
        out = self.agent_workspace(agent_id, org_id=org_id)
        out.mkdir(parents=True, exist_ok=True)
        return out

    def published_expert_snapshot_dir(
        self, expert_id: str, org_id: str | None = None
    ) -> Path:
        """Published expert snapshot dir, tenant-scoped when the row has an org.

        Tenant expert: ``orgs/<org>/published_experts/<id>``; platform-wide
        expert (``org_id IS NULL``): ``shared/published_experts/<id>`` —
        the shared area every tenant may read.  Both adopt the legacy flat
        ``published_experts/<id>`` when it already exists.
        """
        if org_id:
            return _scoped_dir(
                self.org_dir(org_id) / "published_experts" / expert_id,
                self.published_experts_dir / expert_id,
            )
        return _scoped_dir(
            self.shared_dir / "published_experts" / expert_id,
            self.published_experts_dir / expert_id,
        )

    def knowledge_base_dir(self, kb_id: str, org_id: str | None = None) -> Path:
        """Knowledge base file dir, tenant-scoped when the KB row has an org.

        Tenant KB: ``orgs/<org>/knowledge/<kb_id>``; platform KB:
        ``shared/knowledge/<kb_id>``.  Both adopt the legacy flat
        ``knowledge/<kb_id>`` when it already exists.
        """
        if org_id:
            return _scoped_dir(
                self.org_dir(org_id) / "knowledge" / kb_id,
                self.knowledge_dir / kb_id,
            )
        return _scoped_dir(
            self.shared_dir / "knowledge" / kb_id,
            self.knowledge_dir / kb_id,
        )
