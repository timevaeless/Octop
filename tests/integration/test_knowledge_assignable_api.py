"""The expert composer-defaults picker must offer exactly what saving accepts.

Regression (2026-09-29): ``GET /api/knowledge-bases`` is a management view and
hands platform admins ``list_all()``, while ``PATCH /api/agents/{id}``
validates ``knowledge_base_ids`` against the **agent owner's** scope
(``AgentManager.validate_knowledge_base_ids``). An admin therefore saw bases in
the dropdown that the save then rejected with ``KNOWLEDGE_NOT_FOUND``
("未找到知识库或文档。") — worst case a base another user owns and never shared,
which nobody but that owner can attach at all.

``GET /api/knowledge-bases/assignable`` is the picker's endpoint: it reuses the
owner scope, so the two can no longer drift.

Bases are seeded through the repo rather than ``POST /api/knowledge-bases``:
creating one over HTTP needs the embedding stack ready, which is unrelated to
the visibility rule under test.
"""

from __future__ import annotations

from typing import Any

from tests.support.auth import create_agent, create_user, resolve_user_id


def _seed_base(
    server: Any,
    *,
    owner_user_id: int,
    name: str,
    shared: bool,
    org_id: str | None = None,
) -> str:
    row = server.services.knowledge_repo.create_base(
        owner_user_id=owner_user_id,
        name=name,
        shared=shared,
        org_id=org_id,
    )
    return row.id


async def _ids(client: Any, auth: dict[str, str], url: str) -> set[str]:
    r = await client.get(url, headers=auth)
    assert r.status_code == 200, r.text
    return {b["knowledge_base_id"] for b in r.json()}


async def test_assignable_never_offers_what_saving_rejects(env: Any) -> None:
    client, server, admin_auth = env
    await create_user(client, admin_auth, username="kb_scope_tenant")
    tenant_id = await resolve_user_id(client, admin_auth, "kb_scope_tenant")
    admin_id = await resolve_user_id(client, admin_auth, "admin")

    # The "another user owns it and never shared it" shape: only its owner can
    # ever attach it, yet the admin's management view still lists it.
    tenant_kb = _seed_base(server, owner_user_id=tenant_id, name="tenant-private", shared=False)
    # An admin draft: unattributed, shared, owned by the admin.
    admin_kb = _seed_base(
        server, owner_user_id=admin_id, name="admin-shared", shared=True, org_id=None
    )
    agent_id = await create_agent(client, admin_auth, name="kb-scope-bot")

    # The management view legitimately shows the admin every base.
    listed = await _ids(client, admin_auth, "/api/knowledge-bases")
    assert tenant_kb in listed and admin_kb in listed

    # The picker for an expert the admin owns must not offer the tenant's base.
    assignable = await _ids(
        client, admin_auth, f"/api/knowledge-bases/assignable?agent_id={agent_id}"
    )
    assert tenant_kb not in assignable
    assert admin_kb in assignable

    # Proof the dropped option really was dead: saving it is rejected.
    rejected = await client.patch(
        f"/api/agents/{agent_id}",
        headers=admin_auth,
        json={"knowledge_base_ids": [tenant_kb]},
    )
    assert rejected.status_code == 404, rejected.text
    assert rejected.json()["error"]["code"] == "KNOWLEDGE_NOT_FOUND"

    # ...while what the picker did offer saves cleanly.
    accepted = await client.patch(
        f"/api/agents/{agent_id}",
        headers=admin_auth,
        json={"knowledge_base_ids": [admin_kb]},
    )
    assert accepted.status_code == 200, accepted.text


async def test_assignable_without_agent_id_scopes_to_the_caller(env: Any) -> None:
    """Creating an expert: the owner is the caller, so that is the scope."""
    client, server, admin_auth = env
    tenant_auth = await create_user(client, admin_auth, username="kb_scope_creator")
    tenant_id = await resolve_user_id(client, admin_auth, "kb_scope_creator")
    admin_id = await resolve_user_id(client, admin_auth, "admin")

    tenant_kb = _seed_base(server, owner_user_id=tenant_id, name="creator-private", shared=False)
    admin_kb = _seed_base(
        server, owner_user_id=admin_id, name="creator-shared", shared=True, org_id=None
    )

    admin_ids = await _ids(client, admin_auth, "/api/knowledge-bases/assignable")
    assert admin_kb in admin_ids
    assert tenant_kb not in admin_ids

    tenant_ids = await _ids(client, tenant_auth, "/api/knowledge-bases/assignable")
    assert tenant_kb in tenant_ids


async def test_assignable_rejects_non_owners(env: Any) -> None:
    client, _server, admin_auth = env
    tenant_auth = await create_user(client, admin_auth, username="kb_scope_other")
    agent_id = await create_agent(client, admin_auth, name="kb-scope-owner-bot")

    denied = await client.get(
        f"/api/knowledge-bases/assignable?agent_id={agent_id}", headers=tenant_auth
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "FORBIDDEN"

    missing = await client.get(
        "/api/knowledge-bases/assignable?agent_id=NOPE01", headers=admin_auth
    )
    assert missing.status_code == 404, missing.text
    assert missing.json()["error"]["code"] == "AGENT_NOT_FOUND"
