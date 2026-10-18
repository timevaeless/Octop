"""Tenant resource-ACL listing — custom-MCP rows must carry their server names.

Every manual custom-MCP connector is the owner's whole *document*, stored with
the same `display_name` 「自定义 MCP」, so the name column alone cannot say
which connector a row is (2026-10-18: two documents were indistinguishable on
the tenant «资源授权范围» page). The listing projects the server keys inside
the document; nothing else about the specs (urls, headers, credentials) may
leave the backend.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from octop.api.routers.org_admin import list_tenant_resource_acl

ORG = "cheryfs"


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]], statements: list[str]) -> None:
        self._rows = rows
        self._statements = statements

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, sql: str, *_args: Any, **_kwargs: Any) -> _FakeCursor:
        self._statements.append(sql)
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeDb:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.statements: list[str] = []

    def connect(self) -> _FakeCursor:
        return _FakeCursor(self._rows, self.statements)


def _server(rows: list[dict[str, Any]]) -> SimpleNamespace:
    services = SimpleNamespace(
        org_repo=SimpleNamespace(get_org=lambda _org: object()),
        db=_FakeDb(rows),
        # Only *stored* by ConnectorService.__init__ — the decrypting method is
        # monkeypatched in each test.
        connector_repo=object(),
        secret_repo=object(),
        settings_repo=object(),
        config=object(),
    )
    return SimpleNamespace(services=services)


def _row(
    instance_id: str,
    name: str = "自定义 MCP",
    kind: str = "custom-mcp",
    user_id: int = 1,
    allowed_roles: str = "[]",
) -> dict[str, Any]:
    return {
        "id": instance_id,
        "name": name,
        "allowed_roles": allowed_roles,
        "kind": kind,
        "user_id": user_id,
    }


@pytest.mark.asyncio
async def test_custom_mcp_rows_list_the_server_names_inside_the_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "octop.infra.connectors.service.ConnectorService.get_custom_servers",
        lambda _self, _user_id: {
            "wren-summer": {"url": "https://wren.example/mcp"},
            "summer-laac": {"display_name": "Summer LAAC"},
        },
    )
    server = _server(
        [
            _row("01M52GPZ7BYF6Z6B0J638SJ5C0"),
            _row("01M4HQ1M5K125VY381FC4ZXMSN", name="WeKnora 知识库", kind="weknora"),
        ]
    )

    rows = await list_tenant_resource_acl(
        org_id=ORG,
        kind="connectors",
        user=SimpleNamespace(is_admin=True, id=1),
        server=server,
    )

    assert rows[0]["name"] == "自定义 MCP"  # stored name untouched
    assert rows[0]["server_names"] == ["Summer LAAC", "wren-summer"]
    # Built-in rows have nothing to disambiguate — no empty key emitted.
    assert "server_names" not in rows[1]


@pytest.mark.asyncio
async def test_unreadable_document_degrades_to_no_names_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_self: Any, _user_id: int) -> dict[str, Any]:
        raise RuntimeError("credential blob (rotated key)")

    monkeypatch.setattr(
        "octop.infra.connectors.service.ConnectorService.get_custom_servers", _boom
    )
    server = _server([_row("01M52GPZ7BYF6Z6B0J638SJ5C0")])

    rows = await list_tenant_resource_acl(
        org_id=ORG,
        kind="connectors",
        user=SimpleNamespace(is_admin=True, id=1),
        server=server,
    )

    assert rows[0]["server_names"] == []
    assert rows[0]["allowed_roles"] == []


@pytest.mark.asyncio
async def test_connectors_query_selects_kind_and_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without these two columns the projection cannot be driven at all."""
    monkeypatch.setattr(
        "octop.infra.connectors.service.ConnectorService.get_custom_servers",
        lambda _self, _user_id: {"wren-summer": {}},
    )
    server = _server([_row("01M52GPZ7BYF6Z6B0J638SJ5C0")])

    await list_tenant_resource_acl(
        org_id=ORG,
        kind="connectors",
        user=SimpleNamespace(is_admin=True, id=1),
        server=server,
    )

    (sql,) = server.services.db.statements
    assert "kind" in sql
    assert "user_id" in sql
