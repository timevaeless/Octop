"""Unit tests for the post-publish tool-cache refresh (T4 pluggable module)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from octop.infra.agents.publish_refresh import (
    match_server_name,
    refresh_after_publish,
    refresh_enabled,
    schedule_refresh,
)

SID = "01M4D3J2FPK9C8JY2481PQGFQB"


def _mount_key(instance_id: str) -> str:
    return f"custom__{instance_id}__mcp-{SID}"


def _row(agent_id: str, mounts: list[str], kind: str = "expert") -> SimpleNamespace:
    return SimpleNamespace(agent_id=agent_id, mcp_servers=json.dumps(mounts), kind=kind)


class _FakeRegistry:
    def __init__(self, rows: list[SimpleNamespace] | None = None) -> None:
        self._mcp_tool_cache: dict[tuple[int, str, str], list[object]] = {}
        self._mcp_token_expiry: dict[tuple[int, str], float | None] = {}
        self._repos = SimpleNamespace(
            agent_repo=SimpleNamespace(list_all=lambda include_disabled: rows or [])
        )
        self._reload_agents = AsyncMock()


class _FakeRuntime:
    def __init__(self, registry: object) -> None:
        self.agent_registry = registry


def _fake_server(registry: object) -> SimpleNamespace:
    return SimpleNamespace(app_runtime=_FakeRuntime(registry))


# ------------------------------------------------------------ suffix match


def test_match_server_name_is_suffix_and_collision_proof() -> None:
    assert match_server_name(_mount_key("INST1"), SID)
    assert not match_server_name("custom__INST1__mcp-other", SID)
    # A sibling id that merely *ends with* the same characters does not match
    # (the __mcp- guard): "mcp-xabc" must not match service "abc".
    assert not match_server_name(f"custom__INST1__mcp-x{SID}", SID)
    assert not match_server_name("deepwiki", SID)


# ------------------------------------------------------------ kill switch


def test_refresh_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCTOP_MCP_PUBLISH_REFRESH", raising=False)
    assert refresh_enabled()
    for off in ("0", "false", "OFF", "no"):
        monkeypatch.setenv("OCTOP_MCP_PUBLISH_REFRESH", off)
        assert not refresh_enabled()
    monkeypatch.setenv("OCTOP_MCP_PUBLISH_REFRESH", "1")
    assert refresh_enabled()


# ------------------------------------------------------------ cache drop


def test_drop_cache_only_touches_target_service() -> None:
    reg = _FakeRegistry()
    hit = (7, _mount_key("INST1"), "fp")
    hit_other_user = (8, _mount_key("INST1"), "fp")
    other_service = (7, "custom__INST2__mcp-othersvc", "fp")
    reg._mcp_tool_cache = {hit: ["t"], hit_other_user: ["t"], other_service: ["t"]}
    reg._mcp_token_expiry = {
        (7, _mount_key("INST1")): 1.0,
        (7, "custom__INST2__mcp-othersvc"): 2.0,
    }

    from octop.infra.agents.publish_refresh import _drop_cache_for_service

    dropped = _drop_cache_for_service(reg, SID)

    assert dropped == [_mount_key("INST1")]
    assert hit not in reg._mcp_tool_cache
    # Other users' entries for the same service are dropped too (shared mount).
    assert hit_other_user not in reg._mcp_tool_cache
    assert other_service in reg._mcp_tool_cache
    # Token-expiry bookkeeping follows the same scope.
    assert (7, _mount_key("INST1")) not in reg._mcp_token_expiry
    assert (7, "custom__INST2__mcp-othersvc") in reg._mcp_token_expiry


# ------------------------------------------------------------ full refresh


@pytest.mark.asyncio
async def test_refresh_rebuilds_only_default_mounting_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "octop.infra.agents.publish_refresh._GATEWAY_PROPAGATION_DELAY_SECONDS", 0
    )
    key = _mount_key("INST1")
    reg = _FakeRegistry(
        rows=[
            _row("A1", [key]),  # default mount → rebuild
            _row("A2", ["custom__INST2__mcp-othersvc"]),  # unrelated → untouched
            _row("A3", [key], kind="team"),  # team host → untouched
            _row("A4", []),  # per-turn picker only → cache self-heals
        ]
    )
    reg._mcp_tool_cache = {(7, key, "fp"): ["t"]}

    await refresh_after_publish(_fake_server(reg), SID)

    reg._reload_agents.assert_awaited_once_with(["A1"])
    assert (7, key, "fp") not in reg._mcp_tool_cache


@pytest.mark.asyncio
async def test_refresh_noop_when_nothing_mounted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "octop.infra.agents.publish_refresh._GATEWAY_PROPAGATION_DELAY_SECONDS", 0
    )
    reg = _FakeRegistry(rows=[_row("A1", [])])

    await refresh_after_publish(_fake_server(reg), SID)

    reg._reload_agents.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refresh miss must never fail an already-successful publish."""
    monkeypatch.setattr(
        "octop.infra.agents.publish_refresh._GATEWAY_PROPAGATION_DELAY_SECONDS", 0
    )
    reg = _FakeRegistry()
    reg._mcp_tool_cache = {(7, _mount_key("INST1"), "fp"): ["t"]}
    reg._repos.agent_repo.list_all = lambda include_disabled: (_ for _ in ()).throw(
        RuntimeError("db down")
    )

    await refresh_after_publish(_fake_server(reg), SID)  # must not raise


# ------------------------------------------------------------ scheduling


def test_schedule_refresh_uses_background_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCTOP_MCP_PUBLISH_REFRESH", raising=False)

    added: list[tuple[object, tuple[object, ...]]] = []

    class _FakeBG:
        def add_task(self, fn: object, *args: object) -> None:
            added.append((fn, args))

    schedule_refresh(_fake_server(_FakeRegistry()), SID, _FakeBG())

    assert len(added) == 1
    fn, args = added[0]
    assert fn is refresh_after_publish
    assert args[1] == SID


def test_schedule_refresh_respects_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OCTOP_MCP_PUBLISH_REFRESH", "0")
    added: list[object] = []

    class _FakeBG:
        def add_task(self, fn: object, *args: object) -> None:
            added.append(fn)

    schedule_refresh(_fake_server(_FakeRegistry()), SID, _FakeBG())

    assert added == []
