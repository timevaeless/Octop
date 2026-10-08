"""The expert composer-defaults connector picker must offer exactly what saving accepts.

Regression (2026-09-29): ``GET /api/connector-instances`` lists the **viewer's**
own connectors, while ``PATCH /api/agents/{id}`` validates ``mcp_servers``
against the **agent owner's** scope (``AgentManager.validate_mcp_servers`` →
``ConnectorService.validate_mcp_servers_for_user``). An admin editing someone
else's expert therefore saw connectors in the dropdown that the save then
rejected with ``CONNECTOR_NOT_BOUND`` — typically an unattributed
(``org_id=None``) shared row, which only the admin's ``role_keys=None`` bypass
can reach, never the tenant owner's.

``GET /api/connector-instances/assignable`` is the picker's endpoint: it reuses
the owner scope, so the two can no longer drift. It also stays declared before
``/connector-instances/{instance_id}``, which would otherwise swallow
``assignable`` as a path parameter.

Instances are created over HTTP (``tencent-docs`` + a static token needs no
network), which is what stamps ``has_credentials`` / ``status='active'`` /
``org_id`` exactly as production does.
"""

from __future__ import annotations

from typing import Any

from tests.support.auth import create_agent, create_user


async def _create_instance(
    client: Any, auth: dict[str, str], *, name: str, shared: bool = False
) -> str:
    r = await client.post(
        "/api/connector-instances",
        headers=auth,
        json={
            "kind": "tencent-docs",
            "display_name": name,
            "credentials": {"token": f"token-{name}"},
            "shared": shared,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["mcp_server_name"]


async def _names(client: Any, auth: dict[str, str], url: str) -> set[str]:
    r = await client.get(url, headers=auth)
    assert r.status_code == 200, r.text
    return {row["mcp_server_name"] for row in r.json()}


async def test_assignable_never_offers_what_saving_rejects(env: Any) -> None:
    client, _server, admin_auth = env
    owner_auth = await create_user(client, admin_auth, username="conn_scope_owner")

    # Unattributed admin draft: the admin's own listing shows it, while the
    # tenant owner fails closed on it (``org_id=None`` + non-None role_keys).
    admin_shared = await _create_instance(client, admin_auth, name="platform-shared", shared=True)
    # The owner's private connector: absent from the admin's own listing, yet
    # exactly what the save accepts for an expert this owner holds.
    owner_own = await _create_instance(client, owner_auth, name="owner-private")

    expert_id = await create_agent(client, owner_auth, name="conn-scope-bot")

    # The viewer listing legitimately shows the admin's own connector...
    listed = await _names(client, admin_auth, "/api/connector-instances")
    assert admin_shared in listed
    assert owner_own not in listed

    # ...but the picker for an expert the *owner* holds must not offer it, and
    # must offer what the viewer listing never showed.
    assignable = await _names(
        client, admin_auth, f"/api/connector-instances/assignable?agent_id={expert_id}"
    )
    assert admin_shared not in assignable
    assert owner_own in assignable

    # Proof the dropped option really was dead: saving it is rejected.
    rejected = await client.patch(
        f"/api/agents/{expert_id}",
        headers=admin_auth,
        json={"mcp_servers": [admin_shared]},
    )
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["error"]["code"] == "CONNECTOR_NOT_BOUND"

    # ...while what the picker did offer saves cleanly.
    accepted = await client.patch(
        f"/api/agents/{expert_id}",
        headers=admin_auth,
        json={"mcp_servers": [owner_own]},
    )
    assert accepted.status_code == 200, accepted.text

    # The invariant in full: *every* offered name is accepted.
    for name in sorted(assignable):
        ok = await client.patch(
            f"/api/agents/{expert_id}",
            headers=admin_auth,
            json={"mcp_servers": [name]},
        )
        assert ok.status_code == 200, f"{name}: {ok.text}"


async def test_assignable_without_agent_id_scopes_to_the_caller(env: Any) -> None:
    """Creating an expert: the owner is the caller, so that is the scope."""
    client, _server, admin_auth = env
    owner_auth = await create_user(client, admin_auth, username="conn_scope_creator")

    admin_shared = await _create_instance(client, admin_auth, name="creator-shared", shared=True)
    owner_own = await _create_instance(client, owner_auth, name="creator-private")

    admin_names = await _names(client, admin_auth, "/api/connector-instances/assignable")
    assert admin_shared in admin_names
    assert owner_own not in admin_names

    owner_names = await _names(client, owner_auth, "/api/connector-instances/assignable")
    assert owner_own in owner_names
    assert admin_shared not in owner_names


async def test_assignable_rejects_non_owners(env: Any) -> None:
    client, _server, admin_auth = env
    owner_auth = await create_user(client, admin_auth, username="conn_scope_holder")
    other_auth = await create_user(client, admin_auth, username="conn_scope_other")
    expert_id = await create_agent(client, owner_auth, name="conn-scope-owner-bot")

    denied = await client.get(
        f"/api/connector-instances/assignable?agent_id={expert_id}", headers=other_auth
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "FORBIDDEN"

    # Also proves the route is not swallowed by ``/connector-instances/{id}``,
    # which would answer with CONNECTOR_NOT_FOUND instead.
    missing = await client.get(
        "/api/connector-instances/assignable?agent_id=NOPE01", headers=admin_auth
    )
    assert missing.status_code == 404, missing.text
    assert missing.json()["error"]["code"] == "AGENT_NOT_FOUND"
