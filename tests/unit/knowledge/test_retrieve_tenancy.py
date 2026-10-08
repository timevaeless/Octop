"""search_knowledge retrieval must honor tenancy for non-admin callers.

Bug (2026-09-28): the tool's ``services`` namespace was built without
``user_repo`` / ``org_repo``, so the per-query visibility re-derivation fell
back to the legacy per-user branch. Tenant-scoped shared KBs (``org_id`` set
to a tenant id) were invisible there and retrieval silently returned no
passages — even though the turn had mounted the KB and the composer showed it.
"""

from types import SimpleNamespace

import pytest

from octop.infra.knowledge.retrieve import _retrieve_context_sync


def _kb(kb_id: str, org_id: str, allowed_roles: list[str] | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=kb_id,
        name=f"kb-{kb_id}",
        description="",
        org_id=org_id,
        allowed_roles=allowed_roles,
        owner_user_id=1,
        shared=1,
        default_open=0,
    )


class FakeKnowledgeRepo:
    def __init__(self, rows: list[SimpleNamespace], documents: list[SimpleNamespace]):
        self._rows = rows
        self._documents = documents

    def list_visible(self, user_id, *, org_id=None, role_keys=None):
        from octop.infra.users.resource_acl import visible_shared_row

        out = []
        for row in self._rows:
            if row.owner_user_id == user_id:
                out.append(row)
                continue
            if visible_shared_row(
                row.org_id, row.allowed_roles, viewer_org_id=org_id, role_keys=role_keys
            ):
                out.append(row)
        return out

    def list_all(self):
        return list(self._rows)

    def list_documents(self, kb_id):
        return [d for d in self._documents if d.kb_id == kb_id]


class FakeUserRepo:
    def get(self, user_id):
        if user_id == 7:
            return SimpleNamespace(id=7, default_org_id="cheryfs", role="user")
        return None


class FakeOrgRepo:
    def get_membership(self, user_id, org_id):
        if user_id == 7 and org_id == "cheryfs":
            return SimpleNamespace(role_keys=["expert_user"], status="active")
        return None


def _services(with_tenancy_repos: bool) -> SimpleNamespace:
    kb_rows = [_kb("QSWPPC", "cheryfs", [])]
    documents = [
        SimpleNamespace(id="doc1", kb_id="QSWPPC", status="ready", is_dir=False,
                        filename="demo5.txt")
    ]
    repo = FakeKnowledgeRepo(kb_rows, documents)
    services = SimpleNamespace(
        knowledge_repo=repo,
        settings_repo=MagicStubSettings(),
        provider_repo=None,
    )
    if with_tenancy_repos:
        services.user_repo = FakeUserRepo()
        services.org_repo = FakeOrgRepo()
    return services


class MagicStubSettings:
    """settings_repo.get returning values that pass assert_knowledge_usable."""

    _values = {
        "knowledge_bases_enabled": "true",
        "knowledge_embedding_backend": "onnx",
        "knowledge_embedding_model": "BAAI/bge-small-zh-v1.5",
    }

    def get(self, key):
        return self._values.get(key)


@pytest.fixture
def patch_prereqs_and_embed(monkeypatch):
    """Make prerequisites pass and embeddings deterministic, skipping ONNX."""
    import octop.infra.knowledge.retrieve as retrieve_mod

    monkeypatch.setattr(
        retrieve_mod,
        "assert_knowledge_usable",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        retrieve_mod,
        "embed_knowledge_texts",
        lambda services, texts: [[1.0, 0.0]],
    )
    monkeypatch.setattr(
        retrieve_mod,
        "KnowledgeIndex",
        _FakeIndex,
    )
    return retrieve_mod


class _FakeIndex:
    def __init__(self, kb_id: str):
        self.kb_id = kb_id

    def search(self, vector, *, k: int = 8):
        from octop.infra.knowledge.retrieve import Hit

        return [Hit(doc_id="doc1", chunk_id="doc1:0", text="条款 D5SECRET-5210：24 个月。", score=0.9, ordinal=0, metadata={})]


def test_tenant_scoped_kb_retrievable_for_non_admin_with_repos(
    patch_prereqs_and_embed,
) -> None:
    services = _services(with_tenancy_repos=True)
    context = _retrieve_context_sync(
        services,
        user_id=7,
        is_admin=False,
        query="D5SECRET-5210",
        knowledge_base_ids=["QSWPPC"],
        k=8,
        char_budget=4000,
        locale="zh",
        visible_bases=None,
    )
    assert "D5SECRET-5210" in context


def test_tenant_scoped_kb_silently_dropped_without_repos(
    patch_prereqs_and_embed,
) -> None:
    """Documents the pre-fix wiring failure shape (kept as a canary)."""
    services = _services(with_tenancy_repos=False)
    context = _retrieve_context_sync(
        services,
        user_id=7,
        is_admin=False,
        query="D5SECRET-5210",
        knowledge_base_ids=["QSWPPC"],
        k=8,
        char_budget=4000,
        locale="zh",
        visible_bases=None,
    )
    assert context == ""
