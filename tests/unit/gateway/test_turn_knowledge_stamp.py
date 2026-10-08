"""Regression: gateway turn processor must expose user/org repos to KB stamping.

Bug (2026-09-28): ``GlobalProcessor`` built ``_knowledge_services`` without
``user_repo`` / ``org_repo``, so ``_stamp_turn_knowledge_config`` fell into the
legacy per-user branch for every non-admin turn. There, tenant-scoped shared
knowledge bases (``org_id`` = a tenant id) are invisible, the selected catalog
came back empty, and ``KnowledgeSearchHintMiddleware`` hid ``search_knowledge``
even though the dashboard composer had explicitly sent the KB id.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from octop.infra.gateway.process.processor import GlobalProcessor


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


def _make_processor(org_repo: object | None) -> GlobalProcessor:
    return GlobalProcessor(
        agent_manager=MagicMock(),
        thread_registry=MagicMock(),
        audit_repo=MagicMock(),
        agent_repo=MagicMock(),
        user_repo=MagicMock(),
        org_repo=org_repo,
        connector_repo=MagicMock(),
        knowledge_repo=MagicMock(),
        settings_repo=MagicMock(),
        provider_repo=MagicMock(),
        dispatcher=MagicMock(),
    )


def test_knowledge_services_carry_user_and_org_repos() -> None:
    """The stamping branch requires both repos; they must ride on the namespace."""
    org_repo = MagicMock()
    processor = _make_processor(org_repo)
    services = processor._knowledge_services
    assert services is not None
    assert services.user_repo is not None
    assert services.org_repo is org_repo


@pytest.mark.asyncio
async def test_turn_stamp_sees_tenant_scoped_kb_for_non_admin() -> None:
    """A non-admin turn with an explicit tenant-scoped KB must keep it in catalog."""
    org_repo = MagicMock()
    user_repo = MagicMock()
    user_row = SimpleNamespace(id=7, default_org_id="cheryfs", role="user")
    user_repo.get.return_value = user_row
    membership = SimpleNamespace(role_keys=["expert_user"], status="active")
    org_repo.get_membership.return_value = membership

    kb_rows = [
        _kb("QSWPPC", "cheryfs", []),  # tenant-scoped shared KB
        _kb("6ATVEB", "*", ["expert_user"]),
    ]

    class FakeKnowledgeRepo:
        def list_visible(self, user_id, *, org_id=None, role_keys=None):
            from octop.infra.users.resource_acl import visible_shared_row

            out = []
            for row in kb_rows:
                if row.owner_user_id == user_id:
                    out.append(row)
                    continue
                if visible_shared_row(
                    row.org_id,
                    row.allowed_roles,
                    viewer_org_id=org_id,
                    role_keys=role_keys,
                ):
                    out.append(row)
            return out

        def list_all(self):
            return list(kb_rows)

    processor = _make_processor(org_repo)
    processor._knowledge_services = SimpleNamespace(
        knowledge_repo=FakeKnowledgeRepo(),
        settings_repo=MagicMock(),
        provider_repo=MagicMock(),
        user_repo=user_repo,
        org_repo=org_repo,
    )

    request: dict = {}
    processor._attach_turn_knowledge_config(
        request,
        user_id=7,
        is_admin=False,
        explicit_ids=["QSWPPC"],
        locale="zh",
        agent_id=None,
    )

    configurable = request["configurable"]
    assert configurable["knowledge_base_ids"] == ["QSWPPC"]
    catalog_ids = [item["id"] for item in configurable["knowledge_base_catalog"]]
    assert catalog_ids == ["QSWPPC"], (
        "tenant-scoped KB vanished from the turn catalog; "
        "search_knowledge would be hidden for this turn"
    )


def test_catalog_drops_ids_missing_from_visible_bases() -> None:
    """Documents the failure shape: selected ids not visible yield an empty catalog."""
    from octop.infra.knowledge.hint import catalog_for_selected_bases

    visible = [_kb("6ATVEB", "*", ["expert_user"])]
    catalog = catalog_for_selected_bases(visible, ["QSWPPC"])
    assert catalog == []
