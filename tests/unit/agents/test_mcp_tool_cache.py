"""Unit tests for custom MCP deferred load + user-level tool cache."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.tools import BaseTool, StructuredTool

from octop.infra.connectors.delegation import (
    DELEGATION_REFRESH_MARGIN_SECONDS,
    mint_delegated_token,
    spec_token_exp,
)
from octop.infra.connectors.mcp_tool_cache import (
    fingerprint_mcp_spec,
    wrap_tools_for_shared_use,
)


def test_fingerprint_stable_and_ignores_enabled() -> None:
    a = fingerprint_mcp_spec(
        {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "pkg"],
            "enabled": True,
        }
    )
    b = fingerprint_mcp_spec(
        {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "pkg"],
            "enabled": False,
        }
    )
    assert a == b
    c = fingerprint_mcp_spec(
        {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "other"],
        }
    )
    assert a != c


@pytest.mark.asyncio
async def test_wrap_tools_serialize_ainvoke() -> None:
    order: list[str] = []

    class _FakeTool:
        name = "demo_tool"
        description = "demo"
        args_schema = None

        async def ainvoke(self, *_a: Any, **_k: Any) -> str:
            order.append("start")
            await asyncio.sleep(0.05)
            order.append("end")
            return "ok"

        def invoke(self, *_a: Any, **_k: Any) -> str:
            return "ok"

    lock = asyncio.Lock()
    wrapped = wrap_tools_for_shared_use([_FakeTool()], lock)
    assert len(wrapped) == 1
    assert isinstance(wrapped[0], BaseTool)
    assert isinstance(wrapped[0], StructuredTool)

    async def _call() -> Any:
        return await wrapped[0].ainvoke({})

    await asyncio.gather(_call(), _call())
    assert order == ["start", "end", "start", "end"]


@pytest.mark.asyncio
async def test_get_or_load_caches_and_reuses() -> None:
    from octop.infra.agents.manager import AgentManager

    mgr = object.__new__(AgentManager)
    mgr._mcp_tool_cache = {}
    mgr._mcp_tool_cache_locks = {}
    mgr._mcp_tool_cache_guard = asyncio.Lock()
    mgr._paths = MagicMock(root=MagicMock())

    fake = MagicMock()
    fake.name = "s_tool1"
    fake.description = "t"
    fake.args_schema = None
    fake.metadata = None
    fake.ainvoke = AsyncMock(return_value="ok")
    aload = AsyncMock(return_value=[fake])

    spec = {"transport": "stdio", "command": "npx", "args": []}
    with patch("octop_harness.mcp.aload_mcp_tools", aload):
        first = await mgr._get_or_load_mcp_tools(1, "s", spec)
        second = await mgr._get_or_load_mcp_tools(1, "s", spec)

    assert aload.await_count == 1
    assert first is second
    assert isinstance(first[0], StructuredTool)
    assert first[0].name == "s_tool1"


@pytest.mark.asyncio
async def test_get_or_load_misses_on_fingerprint_change() -> None:
    from octop.infra.agents.manager import AgentManager

    mgr = object.__new__(AgentManager)
    mgr._mcp_tool_cache = {}
    mgr._mcp_tool_cache_locks = {}
    mgr._mcp_tool_cache_guard = asyncio.Lock()
    mgr._paths = MagicMock(root=MagicMock())

    def _tool(name: str) -> MagicMock:
        t = MagicMock()
        t.name = name
        t.description = "t"
        t.args_schema = None
        t.metadata = None
        t.ainvoke = AsyncMock(return_value="ok")
        return t

    aload = AsyncMock(side_effect=[[_tool("s_a")], [_tool("s_b")]])

    with patch("octop_harness.mcp.aload_mcp_tools", aload):
        a = await mgr._get_or_load_mcp_tools(
            1, "s", {"transport": "stdio", "command": "npx", "args": ["a"]}
        )
        b = await mgr._get_or_load_mcp_tools(
            1, "s", {"transport": "stdio", "command": "npx", "args": ["b"]}
        )

    assert aload.await_count == 2
    assert a[0].name == "s_a"
    assert b[0].name == "s_b"
    assert len(mgr._mcp_tool_cache) == 1


@pytest.mark.asyncio
async def test_prepare_chat_mcp_injects_custom_from_cache() -> None:
    from octop.infra.agents.manager import AgentManager

    mgr = object.__new__(AgentManager)
    mgr._mcp_tool_cache = {}
    mgr._mcp_tool_cache_locks = {}
    mgr._mcp_tool_cache_guard = asyncio.Lock()
    mgr._paths = MagicMock(root=MagicMock())

    tool = MagicMock()
    tool.name = "deepwiki_ask"
    tool.description = "ask"
    tool.args_schema = None
    tool.metadata = None
    tool.ainvoke = AsyncMock(return_value="ok")
    aload = AsyncMock(return_value=[tool])

    agent = MagicMock()
    agent._mcp_tool_name_set = frozenset()
    agent.config.mcp_server_configs = {"deepwiki": {}}
    agent.append_mcp_tools = MagicMock(
        side_effect=lambda tools: setattr(
            agent,
            "_mcp_tool_name_set",
            frozenset(getattr(t, "name", "") for t in tools) | frozenset(agent._mcp_tool_name_set),
        )
    )

    row = MagicMock()
    mgr.get_agent = MagicMock(return_value=agent)
    mgr.get_row = MagicMock(return_value=row)
    mgr._connector_uid_for = MagicMock(return_value=7)
    mgr._connector_svc = MagicMock()
    mgr._connector_svc.custom_harness_configs.return_value = {
        "deepwiki": {
            "transport": "streamable_http",
            "url": "https://mcp.deepwiki.com/mcp",
            "headers": {"Accept": "application/json, text/event-stream"},
        }
    }
    mgr.reload_connectors = AsyncMock()

    with patch("octop_harness.mcp.aload_mcp_tools", aload):
        failed = await mgr.prepare_chat_mcp("A1", ["deepwiki"], connector_user_id=7)
        failed2 = await mgr.prepare_chat_mcp("A1", ["deepwiki"], connector_user_id=7)

    assert failed == []
    assert failed2 == []
    assert aload.await_count == 1
    assert agent.append_mcp_tools.call_count == 1
    mgr.reload_connectors.assert_not_awaited()
    assert agent.config.mcp_server_configs["deepwiki"]["transport"] == "streamable_http"
    injected = agent.append_mcp_tools.call_args.args[0]
    assert isinstance(injected[0], StructuredTool)


@pytest.mark.asyncio
async def test_prepare_chat_mcp_attaches_gateway_tools_without_reload() -> None:
    """Gateway connectors inject in-process; rebuilding would drop the runtime.

    A rebuild ends in the very same injection, but first closes the harness
    agent (and the checkpointer pool an in-flight turn still writes to) and
    holds ``AgentManager._lock`` across workspace I/O.
    """
    from octop.infra.agents.manager import AgentManager
    from octop.infra.connectors.builder import mcp_server_name

    instance_id = "01TESTGATEWAYINSTANCE0001"
    server = mcp_server_name("qq-mail", instance_id)

    inst = MagicMock()
    inst.instance_id = instance_id
    inst.kind = "qq-mail"
    inst.status = "active"
    inst.mcp_server_name = server

    agent = MagicMock()
    agent._mcp_tools = []
    agent._mcp_tool_name_set = frozenset()
    agent.config.mcp_server_configs = {}

    def _inject(tools: list[Any]) -> None:
        agent._mcp_tools = [*tools, *agent._mcp_tools]
        agent._mcp_tool_name_set = frozenset(str(getattr(t, "name", "")) for t in agent._mcp_tools)

    agent.inject_mcp_tools = MagicMock(side_effect=_inject)

    mgr = object.__new__(AgentManager)
    mgr._repos = MagicMock()
    mgr._repos.connector_repo.list_visible.return_value = [inst]
    mgr._connector_svc = MagicMock()
    mgr._connector_svc.custom_harness_configs.return_value = {}
    mgr._connector_svc.decrypt.return_value = {"email": "a@qq.com", "password": "code"}
    mgr._connector_svc.ensure_fresh_credentials = AsyncMock(return_value={})
    mgr.get_agent = MagicMock(return_value=agent)
    mgr.get_row = MagicMock(return_value=MagicMock())
    mgr._connector_uid_for = MagicMock(return_value=7)
    mgr.reload_connectors = AsyncMock()

    failed = await mgr.prepare_chat_mcp("A1", [server], connector_user_id=7)

    # Asserted before the tool checks so a reintroduced rebuild reports itself
    # rather than surfacing as "tools missing".
    mgr.reload_connectors.assert_not_awaited()
    assert failed == []
    assert agent.inject_mcp_tools.call_count == 1
    assert all(name.startswith(f"{server}_") for name in agent._mcp_tool_name_set)
    assert server in agent.config.mcp_server_configs
    mgr._connector_svc.ensure_fresh_credentials.assert_awaited_once_with(instance_id, "qq-mail")


def test_wrap_tools_for_shared_use() -> None:
    lock = asyncio.Lock()
    inner = MagicMock()
    inner.name = "x"
    inner.description = "d"
    inner.args_schema = None
    inner.metadata = None
    wrapped = wrap_tools_for_shared_use([inner], lock)
    assert len(wrapped) == 1
    assert isinstance(wrapped[0], StructuredTool)


# ------------------------------------------- delegated-token refresh (T3)


def _fresh_mgr() -> Any:
    from octop.infra.agents.manager import AgentManager

    mgr = object.__new__(AgentManager)
    mgr._mcp_tool_cache = {}
    mgr._mcp_tool_cache_locks = {}
    mgr._mcp_tool_cache_guard = asyncio.Lock()
    mgr._paths = MagicMock(root=MagicMock())
    return mgr


def _delegated_spec() -> dict[str, Any]:
    token = mint_delegated_token(b"k" * 32, uid=7, org_id="cheryfs", role_keys=["r"])
    return {
        "transport": "streamable_http",
        "url": "http://192.168.181.1:8090/mcp",
        "headers": {"Authorization": f"Bearer {token}"},
    }


def _fake_tool(name: str) -> MagicMock:
    t = MagicMock()
    t.name = name
    t.description = "t"
    t.args_schema = None
    t.metadata = None
    t.ainvoke = AsyncMock(return_value="ok")
    return t


@pytest.mark.asyncio
async def test_get_or_load_records_delegation_expiry() -> None:
    mgr = _fresh_mgr()
    spec = _delegated_spec()
    with patch("octop_harness.mcp.aload_mcp_tools", AsyncMock(return_value=[_fake_tool("hub_t")])):
        await mgr._get_or_load_mcp_tools(7, "hub", spec)
        assert mgr._mcp_token_expiry[(7, "hub")] == pytest.approx(spec_token_exp(spec))
        # Static-auth connection records None and is never refreshed.
        await mgr._get_or_load_mcp_tools(
            7, "static", {"transport": "streamable_http", "url": "http://x/mcp"}
        )
    assert mgr._mcp_token_expiry[(7, "static")] is None


def test_mcp_delegation_expiring_margins() -> None:
    from octop.infra.agents.manager import AgentManager

    mgr = _fresh_mgr()
    assert not AgentManager._mcp_delegation_expiring(mgr, None, "hub")  # no user
    assert not mgr._mcp_delegation_expiring(7, "absent")  # nothing cached
    mgr._mcp_token_expiry = {}  # store materializes lazily (object.__new__ doubles)
    mgr._mcp_token_expiry[(7, "static")] = None
    assert not mgr._mcp_delegation_expiring(7, "static")  # not delegated
    mgr._mcp_token_expiry[(7, "fresh")] = time.time() + 1700
    assert not mgr._mcp_delegation_expiring(7, "fresh")  # outside margin
    mgr._mcp_token_expiry[(7, "hub")] = time.time() + DELEGATION_REFRESH_MARGIN_SECONDS / 2
    assert mgr._mcp_delegation_expiring(7, "hub")  # inside margin


def test_replace_server_mcp_tools_swaps_only_that_server() -> None:
    from octop.infra.agents.manager import AgentManager

    hub_old, other, hub_stale = _fake_tool("hub_a"), _fake_tool("other_b"), _fake_tool("hub_c")
    new_a, new_b = _fake_tool("hub_a2"), _fake_tool("hub_b2")
    agent = MagicMock()
    agent._mcp_tools = [hub_old, other, hub_stale]
    AgentManager._replace_server_mcp_tools(agent, "hub", [new_a, new_b])
    replaced = agent.replace_mcp_tools.call_args.args[0]
    assert replaced == [other, new_a, new_b]


@pytest.mark.asyncio
async def test_prepare_chat_mcp_refreshes_expiring_delegated_token() -> None:
    """Mounted tools on an expiring delegated token are swapped, not appended."""
    mgr = _fresh_mgr()
    mgr._mcp_token_expiry = {(7, "hub"): time.time() + DELEGATION_REFRESH_MARGIN_SECONDS / 2}

    old_tool, fresh_tool = _fake_tool("hub_tool1"), _fake_tool("hub_tool1")
    agent = MagicMock()
    agent._mcp_tools = [old_tool]
    agent._mcp_tool_name_set = frozenset({"hub_tool1"})
    agent.config.mcp_server_configs = {}
    agent.replace_mcp_tools = MagicMock(
        side_effect=lambda tools: (
            setattr(agent, "_mcp_tools", list(tools)),
            setattr(
                agent,
                "_mcp_tool_name_set",
                frozenset(str(getattr(t, "name", "")) for t in tools),
            ),
        )
    )

    fresh_spec = _delegated_spec()
    mgr.get_agent = MagicMock(return_value=agent)
    mgr.get_row = MagicMock(return_value=MagicMock())
    mgr._connector_uid_for = MagicMock(return_value=7)
    mgr._connector_svc = MagicMock()
    mgr._connector_svc.custom_harness_configs.return_value = {"hub": fresh_spec}

    with patch("octop_harness.mcp.aload_mcp_tools", AsyncMock(return_value=[fresh_tool])):
        failed = await mgr.prepare_chat_mcp("A1", ["hub"], connector_user_id=7)

    assert failed == []
    agent.append_mcp_tools.assert_not_called()  # append dedupes by name → must not be used
    agent.replace_mcp_tools.assert_called_once()
    replaced = agent.replace_mcp_tools.call_args.args[0]
    # aload's mock tool comes back wrapped as a StructuredTool named hub_tool1;
    # the stale-token wrapper must be gone.
    assert len(replaced) == 1
    assert isinstance(replaced[0], StructuredTool)
    assert replaced[0].name == "hub_tool1"
    assert old_tool not in replaced
    assert agent.config.mcp_server_configs["hub"] == fresh_spec
    # Expiry bookkeeping follows the fresh token (≈ now + TTL).
    assert mgr._mcp_token_expiry[(7, "hub")] == pytest.approx(spec_token_exp(fresh_spec))
    assert time.time() + 1700 < mgr._mcp_token_expiry[(7, "hub")] <= time.time() + 1800


@pytest.mark.asyncio
async def test_prepare_chat_mcp_skips_refresh_while_token_fresh() -> None:
    mgr = _fresh_mgr()
    mgr._mcp_token_expiry = {(7, "hub"): time.time() + 1700}

    agent = MagicMock()
    agent._mcp_tool_name_set = frozenset({"hub_tool1"})
    mgr.get_agent = MagicMock(return_value=agent)
    mgr.get_row = MagicMock(return_value=MagicMock())
    mgr._connector_uid_for = MagicMock(return_value=7)
    mgr._connector_svc = MagicMock()

    failed = await mgr.prepare_chat_mcp("A1", ["hub"], connector_user_id=7)

    assert failed == []
    mgr._connector_svc.custom_harness_configs.assert_not_called()  # early return, no DB hit


def test_mcp_delegation_expiring_is_per_agent() -> None:
    """P1-5: renewal judgment follows the spec mounted on *this* agent.

    The global (user, server) store is updated by whichever agent renewed
    first; the old logic read only that store, so a second agent holding a
    stale mounted token was told "fresh" and never self-healed.
    """
    from octop.infra.agents.manager import AgentManager

    mgr = _fresh_mgr()
    # The store says a fresh token was just recorded (agent A renewed).
    mgr._mcp_token_expiry = {(7, "hub"): time.time() + 1700}

    fresh_spec = _delegated_spec()  # exp ≈ now + 1800 → outside margin
    # Agent B still carries the old mount: a token minted with a 10s TTL.
    stale_token = mint_delegated_token(b"k" * 32, uid=7, org_id="cheryfs", role_keys=["r"], ttl_seconds=10)
    stale_spec = {
        "transport": "streamable_http",
        "url": "http://x/mcp",
        "headers": {"Authorization": f"Bearer {stale_token}"},
    }

    agent_a = MagicMock()
    agent_a.config.mcp_server_configs = {"hub": dict(fresh_spec)}
    agent_b = MagicMock()
    agent_b.config.mcp_server_configs = {"hub": dict(stale_spec)}

    # Same user, same server, different mounted generations:
    assert not AgentManager._mcp_delegation_expiring(mgr, 7, "hub", agent=agent_a)
    assert AgentManager._mcp_delegation_expiring(mgr, 7, "hub", agent=agent_b)

    # No per-agent stamp (legacy mount) → falls back to the store.
    agent_legacy = MagicMock()
    agent_legacy.config.mcp_server_configs = {}
    assert not AgentManager._mcp_delegation_expiring(mgr, 7, "hub", agent=agent_legacy)

    # No agent context at all → store fallback only (old behavior).
    assert not AgentManager._mcp_delegation_expiring(mgr, 7, "hub")
