"""Unit tests for in-process document indexing jobs."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.knowledge import KnowledgeRepo
from octop.infra.db.repos.settings import SettingsRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.knowledge import jobs
from octop.infra.knowledge.files import write_document
from octop.infra.knowledge.index import KnowledgeIndex


def test_enqueue_index_document_limits_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs.reset_index_semaphore_for_tests()
    monkeypatch.setattr(jobs, "INDEX_CONCURRENCY", 1)

    lock = threading.Lock()
    active = 0
    peak = 0
    entered = threading.Event()
    hold = threading.Event()

    def blocking(_services: object, _kb: str, _doc: str) -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        entered.set()
        assert hold.wait(timeout=2)
        with lock:
            active -= 1

    monkeypatch.setattr(jobs, "process_document", blocking)

    async def run() -> None:
        first = jobs.enqueue_index_document(object(), "kb", "d1")
        second = jobs.enqueue_index_document(object(), "kb", "d2")
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert peak == 1
        hold.set()
        await asyncio.gather(first, second)

    asyncio.run(run())
    assert peak == 1


def test_process_document_marks_empty_extract_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path / "home"))
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    repo = KnowledgeRepo(pool)
    owner_id = UserRepo(pool).create(username="owner", password_hash="h", role="user")
    kb = repo.create_base(
        owner_user_id=owner_id, name="Docs", embedding_model="BAAI/bge-small-zh-v1.5"
    )
    doc = repo.create_document(
        kb_id=kb.id, filename="blank.md", content_type="text/markdown", byte_size=0
    )
    write_document(kb.id, doc.id, doc.filename, b"   \n")
    services = SimpleNamespace(knowledge_repo=repo, settings_repo=SettingsRepo(pool))
    monkeypatch.setattr(jobs, "assert_knowledge_usable", lambda *_args: None)
    monkeypatch.setattr(
        jobs, "embed_knowledge_texts", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError())
    )

    with pytest.raises(ValueError, match="no extractable text"):
        jobs.process_document(services, kb.id, doc.id)

    updated = repo.get_document(doc.id)
    assert updated is not None
    assert updated.status == "failed"
    assert updated.chunk_count == 0


def test_process_document_indexes_chunks_and_marks_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path / "home"))
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    repo = KnowledgeRepo(pool)
    owner_id = UserRepo(pool).create(username="owner", password_hash="h", role="user")
    kb = repo.create_base(
        owner_user_id=owner_id, name="Docs", embedding_model="BAAI/bge-small-zh-v1.5"
    )
    doc = repo.create_document(
        kb_id=kb.id, filename="notes.md", content_type="text/markdown", byte_size=5
    )
    write_document(kb.id, doc.id, doc.filename, b"hello")
    services = SimpleNamespace(knowledge_repo=repo, settings_repo=SettingsRepo(pool))
    monkeypatch.setattr(jobs, "assert_knowledge_usable", lambda *_args: None)
    monkeypatch.setattr(
        jobs, "embed_knowledge_texts", lambda _services, texts: [[1.0, 0.0] for _ in texts]
    )

    jobs.process_document(services, kb.id, doc.id)

    updated = repo.get_document(doc.id)
    assert updated is not None
    assert updated.status == "ready"
    assert updated.chunk_count == 1
    assert KnowledgeIndex(kb.id).search([1.0, 0.0], 1)[0].doc_id == doc.id


def test_process_document_indexes_ocr_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path / "home"))
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    repo = KnowledgeRepo(pool)
    owner_id = UserRepo(pool).create(username="owner", password_hash="h", role="user")
    kb = repo.create_base(owner_user_id=owner_id, name="Scans", embedding_model="model")
    doc = repo.create_document(
        kb_id=kb.id, filename="scan.png", content_type="image/png", byte_size=5
    )
    write_document(kb.id, doc.id, doc.filename, b"image")
    services = SimpleNamespace(knowledge_repo=repo, settings_repo=SettingsRepo(pool))
    monkeypatch.setattr(jobs, "assert_knowledge_usable", lambda *_args: None)
    monkeypatch.setattr(jobs, "optional_ocr_extractor", lambda _services: lambda _path: "发票")
    monkeypatch.setattr(jobs, "embed_knowledge_texts", lambda _services, _texts: [[1.0, 0.0]])

    jobs.process_document(services, kb.id, doc.id)

    updated = repo.get_document(doc.id)
    assert updated is not None
    assert updated.status == "ready"
    assert KnowledgeIndex(kb.id).search([1.0, 0.0], 1)[0].text == "发票"


def test_reindex_all_updates_base_model_marks_documents_pending_and_enqueues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    repo = KnowledgeRepo(pool)
    owner_id = UserRepo(pool).create(username="owner", password_hash="h", role="user")
    kb = repo.create_base(owner_user_id=owner_id, name="Docs", embedding_model="old-model")
    doc = repo.create_document(
        kb_id=kb.id, filename="notes.md", content_type="text/markdown", byte_size=5
    )
    repo.update_document(doc.id, status="ready", chunk_count=1)
    services = SimpleNamespace(knowledge_repo=repo)
    enqueued: list[tuple[str, str]] = []
    monkeypatch.setattr(
        jobs,
        "enqueue_index_document",
        lambda _services, kb_id, doc_id: enqueued.append((kb_id, doc_id)),
    )

    jobs.reindex_all_documents(services, "new-model")

    assert repo.get_base(kb.id).embedding_model == "new-model"
    assert repo.get_base(kb.id).embedding_dim == 0
    assert repo.get_document(doc.id).status == "pending"
    assert enqueued == [(kb.id, doc.id)]


def test_resume_pending_index_jobs_resets_processing_and_enqueues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    repo = KnowledgeRepo(pool)
    owner_id = UserRepo(pool).create(username="owner", password_hash="h", role="user")
    kb = repo.create_base(owner_user_id=owner_id, name="Docs")
    doc = repo.create_document(
        kb_id=kb.id, filename="notes.md", content_type="text/markdown", byte_size=5
    )
    repo.update_document(doc.id, status="processing")
    services = SimpleNamespace(knowledge_repo=repo)
    enqueued: list[tuple[str, str]] = []
    monkeypatch.setattr(
        jobs,
        "enqueue_index_document",
        lambda _services, kb_id, doc_id: enqueued.append((kb_id, doc_id)),
    )

    jobs.resume_pending_index_jobs(services)

    assert repo.get_document(doc.id).status == "pending"
    assert enqueued == [(kb.id, doc.id)]


def test_process_document_binds_kb_tenant_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 2026-09-26 P1-1: the executor thread has no tenant scope.

    run_in_executor does not copy contextvars, so process_document must
    resolve the KB through a bypass window and then bind the KB's org —
    otherwise PostgreSQL RLS hides the tenant row and indexing fails.
    """
    from octop.infra.users import tenancy_context

    monkeypatch.setenv("OCTOP_HOME", str(tmp_path / "home"))
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    repo = KnowledgeRepo(pool)
    owner_id = UserRepo(pool).create(username="owner", password_hash="h", role="user")
    kb = repo.create_base(
        owner_user_id=owner_id,
        name="Docs",
        embedding_model="BAAI/bge-small-zh-v1.5",
        org_id="org-scope",
    )
    doc = repo.create_document(
        kb_id=kb.id, filename="notes.md", content_type="text/markdown", byte_size=5
    )
    write_document(kb.id, doc.id, doc.filename, b"hello", org_id="org-scope")

    seen: dict[str, tenancy_context.TenantScope | None] = {}
    real_get_base = repo.get_base
    real_update_document = repo.update_document

    def spy_get_base(kb_id: str):
        seen["get_base"] = tenancy_context.current_scope()
        return real_get_base(kb_id)

    def spy_update_document(*args: object, **kwargs: object):
        seen["update_document"] = tenancy_context.current_scope()
        return real_update_document(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(repo, "get_base", spy_get_base)
    monkeypatch.setattr(repo, "update_document", spy_update_document)

    services = SimpleNamespace(knowledge_repo=repo, settings_repo=SettingsRepo(pool))
    monkeypatch.setattr(jobs, "assert_knowledge_usable", lambda *_a, **_k: None)
    monkeypatch.setattr(
        jobs, "embed_knowledge_texts", lambda _services, texts: [[1.0, 0.0] for _ in texts]
    )

    prior_scope = tenancy_context.current_scope()
    jobs.process_document(services, kb.id, doc.id)

    # Resolution happens inside the explicit bypass window...
    resolve_scope = seen["get_base"]
    assert resolve_scope is not None
    assert resolve_scope.bypass is True
    assert resolve_scope.org_id is None
    # ...then the job itself runs under the KB's tenant.
    work_scope = seen["update_document"]
    assert work_scope is not None
    assert work_scope.org_id == "org-scope"
    assert work_scope.bypass is False
    # And nothing leaks: the surrounding context is exactly as it was before
    # the job ran (earlier tests in this thread may have left a scope bound;
    # tolerate that — the token reset restores it verbatim).
    assert tenancy_context.current_scope() == prior_scope
