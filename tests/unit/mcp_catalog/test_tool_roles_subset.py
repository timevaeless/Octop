"""Tests for the 2026-10-01 tool-roles subset rule (user decision).

One resource, one config point: a tool's explicit configuration can only
NARROW the service-level default — never widen it. Covered here at the
publisher-function layer and through the real CatalogRepo on sqlite
(prune round-trip).
"""

from __future__ import annotations

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.mcp_catalog.models import McpToolRow
from octop.mcp_catalog.publisher import (
    collect_acl_map,
    effective_tool_roles,
    prune_tools_to_service_roles,
    validate_tool_roles_subset,
)
from octop.mcp_catalog.repo import CatalogRepo


# ------------------------------------------------------------ effective_tool_roles


def test_empty_tool_config_inherits_service_default():
    assert effective_tool_roles([], ["r1", "r2"]) == ["r1", "r2"]
    assert effective_tool_roles(None, []) == []


def test_wildcard_service_default_keeps_explicit_tool_config():
    # '*' in the service default = the superset; the tool config stands
    assert effective_tool_roles(["r2"], ["*"]) == ["r2"]


def test_explicit_tool_config_is_intersected_never_widens():
    assert effective_tool_roles(["r1", "r3"], ["r1", "r2"]) == ["r1"]
    # previously an override — the ambiguity the subset rule removes
    assert effective_tool_roles(["r9"], ["r1"]) == []


def test_both_empty_fail_closed():
    assert effective_tool_roles([], []) == []


# ------------------------------------------------------ validate_tool_roles_subset


def test_subset_validation_passes():
    validate_tool_roles_subset(["r1", "r2"], ["r1"])
    validate_tool_roles_subset(["r1", "r2"], [])
    validate_tool_roles_subset(["*"], ["anything", "goes"])
    validate_tool_roles_subset([], [])  # service roleless, tool empty


def test_subset_validation_rejects_outside_keys():
    with pytest.raises(ValueError) as ei:
        validate_tool_roles_subset(["r1"], ["r1", "r9"])
    assert "r9" in str(ei.value)


# ------------------------------------------------ prune_tools_to_service_roles


@pytest.fixture
def db(tmp_path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


def _tool(name: str, roles: list[str]) -> McpToolRow:
    return McpToolRow(
        id=0,
        created_at=0,
        updated_at=0,
        service_id="svc-1",
        tool_name=name,
        description="",
        http_method="GET",
        http_path=f"/x/{name}",
        parameters="{}",
        request_body="",
        response_body="",
        allowed_roles=roles,
        enabled=True,
        source="manual",
    )


def _seed(db: SqlitePool, tools: list[McpToolRow]) -> CatalogRepo:
    repo = CatalogRepo(db)
    repo.create_service(
        {
            "service_id": "svc-1",
            "display_name": "s",
            "nacos_service_name": "svc-1",
            "backend_port": 80,
            "org_id": "org-a",
            "allowed_roles": ["r1", "r2"],
        }
    )
    for t in tools:
        repo.upsert_tool(
            {
                "service_id": t.service_id,
                "tool_name": t.tool_name,
                "description": t.description,
                "http_method": t.http_method,
                "http_path": t.http_path,
                "parameters": t.parameters,
                "request_body": t.request_body,
                "response_body": t.response_body,
                "allowed_roles": t.allowed_roles,
                "enabled": t.enabled,
                "source": t.source,
            }
        )
    return repo


def test_narrowing_service_default_prunes_tools(db: SqlitePool) -> None:
    repo = _seed(db, [_tool("keep", ["r1"]), _tool("shrink", ["r1", "r2"]), _tool("inherit", [])])
    pruned = prune_tools_to_service_roles(repo, "svc-1", ["r1"])
    # only tools whose config actually shrinks are rewritten; "keep" already
    # fits the new default and is left untouched
    assert pruned == ["shrink"]
    assert {t.tool_name: t.allowed_roles for t in repo.list_tools("svc-1")} == {
        "keep": ["r1"],
        "shrink": ["r1"],
        "inherit": [],
    }


def test_collect_acl_map_intersects_never_widens() -> None:
    tools = [_tool("a", ["r1"]), _tool("b", []), _tool("c", ["r9"])]
    acl = collect_acl_map(tools, service_default_roles=["r1"])
    # explicit r9 was an override before; the subset rule intersects it away
    assert acl == {"r1": ["a", "b"]}
    # no service default + explicit config → still empty (fail-closed)
    assert collect_acl_map([_tool("a", ["r1"])], service_default_roles=[]) == {}
    # wildcard service default: tool config stands
    assert collect_acl_map([_tool("a", ["r1"])], service_default_roles=["*"]) == {"r1": ["a"]}
