"""Filesystem layout and safe document persistence for knowledge bases.

Tenant-scoped since T2-3: KB file dirs resolve through
``PathLayout.knowledge_base_dir`` — ``orgs/<org>/knowledge/<kb_id>`` for
tenant KBs, ``shared/knowledge/<kb_id>`` for platform KBs, with the legacy
flat ``knowledge/<kb_id>`` adopted when it already exists.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from octop.infra.utils.paths import PathLayout


def knowledge_base_dir(kb_id: str, org_id: str | None = None) -> Path:
    return PathLayout.from_env().knowledge_base_dir(kb_id, org_id=org_id)


def documents_dir(kb_id: str, org_id: str | None = None) -> Path:
    path = knowledge_base_dir(kb_id, org_id=org_id) / "docs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def document_path(kb_id: str, doc_id: str, filename: str, org_id: str | None = None) -> Path:
    suffix = Path(filename).suffix.lower()
    return documents_dir(kb_id, org_id=org_id) / f"{doc_id}{suffix}"


def write_document(
    kb_id: str, doc_id: str, filename: str, content: bytes, org_id: str | None = None
) -> Path:
    path = document_path(kb_id, doc_id, filename, org_id=org_id)
    path.write_bytes(content)
    return path


def delete_document_file(
    kb_id: str, doc_id: str, filename: str, org_id: str | None = None
) -> None:
    document_path(kb_id, doc_id, filename, org_id=org_id).unlink(missing_ok=True)


def delete_knowledge_base_files(kb_id: str, org_id: str | None = None) -> None:
    shutil.rmtree(knowledge_base_dir(kb_id, org_id=org_id), ignore_errors=True)
