"""Tenant-layout knowledge trees in system backups (T2-3, pluggable
module).

User convention 2026-09-26 (extract, don't weave). The community backup
only packs/restores the legacy flat ``knowledge/`` tree; this module adds
the tenant plane on top — ``shared/knowledge`` (platform-wide) and
``orgs/<org>/knowledge`` (per tenant) — on both the create and restore
sides, adopt-legacy preserving.

Upstream touch points (``infra/backup/system_archive.py``): one lazy hook
block on the create side, one on the restore side. Deleting this module
together with those hooks restores the community flat-only backup
verbatim.
"""

from __future__ import annotations

from typing import Any

__all__ = ["add_tenant_knowledge_trees", "restore_tenant_knowledge_trees"]

_SHARED_DIR = "shared"
_ORGS_DIR = "orgs"


def add_tenant_knowledge_trees(tf: Any, paths: Any, *, include_knowledge: bool) -> None:
    """Pack ``shared/knowledge`` + ``orgs/<org>/knowledge`` into the archive.

    The legacy flat dir is handled by the community create path (this only
    adds the tenant plane; adopt-legacy means both may coexist).
    """
    if not include_knowledge:
        return
    from octop.infra.backup.system_archive import _add_dir

    shared_kb = paths.shared_dir / "knowledge"
    if shared_kb.is_dir():
        _add_dir(tf, shared_kb, f"{_SHARED_DIR}/knowledge", skip_chats=False)
    if paths.orgs_dir.is_dir():
        for org_child in sorted(paths.orgs_dir.iterdir()):
            org_kb = org_child / "knowledge"
            if org_child.is_dir() and org_kb.is_dir():
                _add_dir(tf, org_kb, f"{_ORGS_DIR}/{org_child.name}/knowledge", skip_chats=False)


def restore_tenant_knowledge_trees(extracted: Any, paths: Any) -> int:
    """Restore ``shared/knowledge`` + ``orgs/<org>/knowledge`` from the archive.

    Returns the number of restored knowledge files (adds to the community
    counter that the flat-tree restore produces).
    """
    from octop.infra.backup.system_archive import _replace_tree_from_archive

    restored = 0
    shared_kb = paths.shared_dir / "knowledge"
    if (extracted / _SHARED_DIR / "knowledge").is_dir():
        restored += _replace_tree_from_archive(
            extracted,
            shared_kb,
            f"{_SHARED_DIR}/knowledge",
        )
    orgs_arc = extracted / _ORGS_DIR
    if orgs_arc.is_dir():
        for org_child in sorted(orgs_arc.iterdir()):
            if org_child.is_dir() and (org_child / "knowledge").is_dir():
                restored += _replace_tree_from_archive(
                    extracted,
                    paths.orgs_dir / org_child.name / "knowledge",
                    f"{_ORGS_DIR}/{org_child.name}/knowledge",
                )
    return restored
