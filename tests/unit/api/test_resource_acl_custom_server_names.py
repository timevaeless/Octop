"""Tenant resource-ACL listing — custom-MCP rows carry their own server names.

One connector = one document = one resource-ACL row (T4 multi-doc): the row's
``display_name`` is the user-chosen connector name and the listing projects
the server keys inside THAT document. Nothing else about the specs (urls,
headers, credentials) may leave the backend.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from octop.api.routers.org_admin import list_tenant_resource_acl

ORG = "cheryfs"

_DOC_A = "01M52GPZ7BYF6Z6B0J638SJ5C0"
_DOC_B = "01M52GPZ7BYF6Z6B0J638SJ5C99"


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


def _server(
    rows: list[dict[str, Any]],
    connector_repo: Any | None = None,
) -> SimpleNamespace:
    services = SimpleNamespace(
        org_repo=SimpleNamespace(get_org=lambda _org: object()),
        db=_FakeDb(rows),
        # Only *stored* by ConnectorService.__init__ — decrypt is
        # monkeypatched in each test; connector_repo is a stub exposing
        # ``get`` (the per-document label projection reads the row first).
        connector_repo=connector_repo if connector_repo is not None else object(),
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


def _patch_docs(
    monkeypatch: pytest.MonkeyPatch, docs: dict[str, Any]
) -> Any:
    """``decrypt(instance_id)`` → the per-document payload (or raise).

    Also returns a connector-repo stub whose ``get`` yields lightweight rows
    for the known documents.
    """

    def decrypt(_self: Any, instance_id: str) -> dict[str, Any]:
        payload = docs[instance_id]
        if isinstance(payload, Exception):
            raise payload
        return payload

    def repo_get(*args: Any) -> Any:
        instance_id = args[-1]
        if instance_id not in docs:
            return None
        return SimpleNamespace(
            instance_id=instance_id,
            kind="custom-mcp",
            has_credentials=True,
        )

    monkeypatch.setattr(
        "octop.infra.connectors.service.ConnectorService.decrypt", decrypt
    )
    from octop.infra.db.repos.connectors import ConnectorRepo

    monkeypatch.setattr(ConnectorRepo, "get", repo_get)
    return SimpleNamespace(get=repo_get)


@pytest.mark.asyncio
async def test_each_connector_row_lists_its_own_server_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two connectors render as two rows, each with only ITS servers."""
    repo_stub = _patch_docs(
        monkeypatch,
        {
            _DOC_A: {
                "servers": {
                    "wren-summer": {"url": "https://wren.example/mcp"},
                    "summer-laac": {"display_name": "Summer LAAC"},
                }
            },
            _DOC_B: {"servers": {"solo": {"url": "https://solo.example/mcp"}}},
        },
    )
    server = _server(
        [
            _row(_DOC_A, name="WeKnora 零售变革问答专家"),
            _row(_DOC_B, name="Wren Summer"),
            _row("01M4HQ1M5K125VY381FC4ZXMSN", name="WeKnora 知识库", kind="weknora"),
        ],
        connector_repo=repo_stub,
    )

    rows = await list_tenant_resource_acl(
        org_id=ORG,
        kind="connectors",
        user=SimpleNamespace(is_admin=True, id=1),
        server=server,
    )

    assert rows[0]["name"] == "WeKnora 零售变革问答专家"
    assert rows[0]["server_names"] == ["Summer LAAC", "wren-summer"]
    assert rows[1]["name"] == "Wren Summer"
    assert rows[1]["server_names"] == ["solo"]
    # Built-in rows have nothing to disambiguate — no empty key emitted.
    assert "server_names" not in rows[2]


@pytest.mark.asyncio
async def test_unreadable_document_degrades_to_no_names_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_stub = _patch_docs(
        monkeypatch,
        {_DOC_A: RuntimeError("credential blob (rotated key)")},
    )
    server = _server([_row(_DOC_A)], connector_repo=repo_stub)

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
    repo_stub = _patch_docs(monkeypatch, {_DOC_A: {"servers": {"wren-summer": {}}}})
    server = _server([_row(_DOC_A)], connector_repo=repo_stub)

    await list_tenant_resource_acl(
        org_id=ORG,
        kind="connectors",
        user=SimpleNamespace(is_admin=True, id=1),
        server=server,
    )

    (sql,) = server.services.db.statements
    assert "kind" in sql
    assert "user_id" in sql
