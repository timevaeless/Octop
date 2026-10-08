"""Shared knowledge trees in system backups (T2-3, pluggable module).

User convention 2026-09-26 (extract, don't weave). The community backup
only packs/restores the legacy flat ``knowledge/`` tree; this module adds
the ``shared/knowledge`` tree on both the create and restore sides.

2026-10-08 simplification (user decision): all content lives under
``shared/`` regardless of attribution — the per-tenant ``orgs/<org>/``
plane is gone, so there is nothing tenant-scoped left to pack. Attribution
visibility is DB/RLS-only (see ``infra/utils/tenant_paths.py``).

Upstream touch points (``infra/backup/system_archive.py``): one lazy hook
block on the create side, one on the restore side. Deleting this module
together with those hooks restores the community flat-only backup
verbatim.
"""

from __future__ import annotations

from typing import Any

__all__ = ["add_tenant_knowledge_trees", "restore_tenant_knowledge_trees"]

_SHARED_DIR = "shared"


def add_tenant_knowledge_trees(tf: Any, paths: Any, *, include_knowledge: bool) -> None:
    """Pack ``shared/knowledge`` into the archive.

    The legacy flat dir is handled by the community create path (this only
    adds the shared plane; adopt-legacy means both may coexist).
    """
    if not include_knowledge:
        return
    from octop.infra.backup.system_archive import _add_dir

    shared_kb = paths.shared_dir / "knowledge"
    if shared_kb.is_dir():
        _add_dir(tf, shared_kb, f"{_SHARED_DIR}/knowledge", skip_chats=False)


def restore_tenant_knowledge_trees(extracted: Any, paths: Any) -> int:
    """Restore ``shared/knowledge`` from the archive.

    Returns the number of restored knowledge files (adds to the community
    counter that the flat-tree restore produces).
    """
    from octop.infra.backup.system_archive import _replace_tree_from_archive

    restored = 0
    if (extracted / _SHARED_DIR / "knowledge").is_dir():
        restored += _replace_tree_from_archive(
            extracted,
            paths.shared_dir / "knowledge",
            f"{_SHARED_DIR}/knowledge",
        )
    return restored
