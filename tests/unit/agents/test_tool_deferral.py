"""Tests for the T4 tool-deferral hook (octop.infra.agents.tool_deferral)."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.tools import StructuredTool

from octop.infra.agents.tool_deferral import (
    ToolSearchSettings,
    _McpToolNameView,
    tool_search_middleware_for,
)


def _tool(name: str, description: str = "demo tool") -> StructuredTool:
    return StructuredTool.from_function(
        func=lambda: "ok",
        name=name,
        description=description,
    )


class _FakeRequest:
    def __init__(self, tools: list[Any], state: dict[str, Any] | None = None) -> None:
        self.tools = tools
        self.state = state if state is not None else {}
        self.overridden: list[Any] | None = None

    def override(self, *, tools: list[Any]) -> "_FakeRequest":
        self.overridden = tools
        return self


# ---------------------------------------------------------------- settings


class TestToolSearchSettings:
    def test_absent_is_off(self) -> None:
        assert ToolSearchSettings.from_config(None) is None

    def test_false_is_off(self) -> None:
        assert ToolSearchSettings.from_config(False) is None

    def test_true_uses_defaults(self) -> None:
        s = ToolSearchSettings.from_config(True)
        assert s is not None
        assert s.mode == "client"
        assert s.fallback == "eager"
        assert s.eager_tools == frozenset()

    def test_dict_options(self) -> None:
        s = ToolSearchSettings.from_config(
            {"mode": "native", "fallback": "error", "eager_tools": ["svc_hot"]}
        )
        assert s is not None
        assert s.mode == "native"
        assert s.fallback == "error"
        assert s.eager_tools == frozenset({"svc_hot"})

    def test_enabled_false_wins(self) -> None:
        assert ToolSearchSettings.from_config({"enabled": False}) is None

    def test_invalid_mode_falls_back_to_client(self) -> None:
        s = ToolSearchSettings.from_config({"mode": "warp"})
        assert s is not None
        assert s.mode == "client"

    def test_garbage_stays_off(self) -> None:
        assert ToolSearchSettings.from_config("yes") is None
        assert ToolSearchSettings.from_config(123) is None


# ---------------------------------------------------------------- name view


class TestMcpToolNameView:
    def test_prefix_match(self) -> None:
        view = _McpToolNameView(["custom__X__mcp-svc"])
        assert "custom__X__mcp-svc_queryDealer" in view

    def test_builtin_tool_not_matched(self) -> None:
        view = _McpToolNameView(["svc"])
        assert "read_file" not in view
        assert "tool_search" not in view

    def test_eager_tools_excluded(self) -> None:
        view = _McpToolNameView(["svc"], eager_tools=frozenset({"svc_hot"}))
        assert "svc_hot" in _McpToolNameView(["svc"])
        assert "svc_hot" not in view

    def test_always_truthy_keeps_live_reference(self) -> None:
        # The harness constructor does `mcp_tool_names or frozenset()`; an
        # empty view must stay truthy or the live reference would be dropped.
        assert bool(_McpToolNameView([])) is True

    def test_non_string_never_matches(self) -> None:
        view = _McpToolNameView(["svc"])
        assert 123 not in view
        assert None not in view

    def test_ignores_blank_server_names(self) -> None:
        view = _McpToolNameView(["svc", "", None])
        assert "svc_t" in view
        assert "_t" not in view


class TestMcpToolNameViewSetAlgebra:
    """Native tool-search mode intersects the view with visible tool names
    (harness ``tool_search.py`` ``_prepare_native``: ``view & visible``) —
    both operand orders must work and honour the prefix/eager semantics."""

    def test_view_and_set(self) -> None:
        view = _McpToolNameView(["custom__X__mcp-svc"])
        result = view & {"custom__X__mcp-svc_queryDealer", "read_file"}
        assert result == frozenset({"custom__X__mcp-svc_queryDealer"})

    def test_set_and_view_reflected(self) -> None:
        view = _McpToolNameView(["svc"])
        result = {"svc_a", "other"} & view
        assert result == frozenset({"svc_a"})

    def test_eager_tools_excluded_from_intersection(self) -> None:
        view = _McpToolNameView(["svc"], eager_tools=frozenset({"svc_hot"}))
        result = view & {"svc_hot", "svc_lazy"}
        assert result == frozenset({"svc_lazy"})

    def test_intersection_is_frozenset(self) -> None:
        view = _McpToolNameView(["svc"])
        assert isinstance(view & {"svc_t"}, frozenset)
        assert isinstance({"svc_t"} & view, frozenset)

    def test_non_iterable_operand_yields_empty(self) -> None:
        view = _McpToolNameView(["svc"])
        assert view & 123 == frozenset()  # type: ignore[operator]

    def test_no_servers_yields_empty_intersection(self) -> None:
        view = _McpToolNameView([])
        assert view & {"anything"} == frozenset()


# ---------------------------------------------------------------- factory


class TestToolSearchMiddlewareFor:
    def test_default_on_without_config(self) -> None:
        mw = tool_search_middleware_for({}, {"svc": {}})
        assert mw is not None
        assert mw._defer_mcp_tools is True
        assert mw._mode == "client"

    def test_explicit_false_opts_out(self) -> None:
        assert tool_search_middleware_for({"tool_search": False}, {"svc": {}}) is None

    def test_explicit_enabled_false_opts_out(self) -> None:
        assert tool_search_middleware_for(
            {"tool_search": {"enabled": False}}, {"svc": {}}
        ) is None

    def test_global_kill_switch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OCTOP_TOOL_SEARCH_DEFAULT", "off")
        assert tool_search_middleware_for({}, {"svc": {}}) is None
        # explicit opt-in still wins over the kill switch
        mw = tool_search_middleware_for({"tool_search": True}, {"svc": {}})
        assert mw is not None

    def test_enabled_without_servers_returns_none(self) -> None:
        assert tool_search_middleware_for({}, {}) is None
        assert tool_search_middleware_for({}, None) is None

    def test_constructs_middleware(self) -> None:
        mw = tool_search_middleware_for(
            {"tool_search": True}, {"custom__X__mcp-svc": {"transport": "http"}}
        )
        assert mw is not None
        assert mw._defer_mcp_tools is True
        assert mw._mode == "client"
        assert "custom__X__mcp-svc_query" in mw._mcp_tool_names
        assert "read_file" not in mw._mcp_tool_names


# ------------------------------------------------------- request rewriting


class TestPrepareClient:
    def _middleware(self, eager: frozenset[str] = frozenset()) -> Any:
        section: dict[str, Any] = {"tool_search": True}
        if eager:
            section = {"tool_search": {"eager_tools": sorted(eager)}}
        return tool_search_middleware_for(section, {"svc": {}})

    def test_mcp_tool_deferred_builtin_eager(self) -> None:
        mw = self._middleware()
        req = _FakeRequest([_tool("read_file"), _tool("svc_query")])
        out = mw.wrap_model_call(req, lambda r: r)
        tools = out.overridden
        assert tools is not None
        by_name = {getattr(t, "name", None): t for t in tools}
        # builtin keeps its full schema
        assert "read_file" in by_name
        # mcp tool becomes a deferred reference (empty schema + suffix)
        ref = by_name["svc_query"]
        assert "[Deferred reference:" in (ref.description or "")
        # search meta-tool is injected when something is deferred
        assert "tool_search" in by_name

    def test_eager_listed_tool_keeps_schema(self) -> None:
        mw = self._middleware(eager=frozenset({"svc_query"}))
        req = _FakeRequest([_tool("svc_query")])
        out = mw.wrap_model_call(req, lambda r: r)
        tools = out.overridden
        assert tools is not None
        assert not any(
            "[Deferred reference:" in (getattr(t, "description", "") or "") for t in tools
        )
        # nothing deferred -> no search tool injected
        assert not any(getattr(t, "name", None) == "tool_search" for t in tools)

    def test_loaded_tool_restores_full_schema(self) -> None:
        mw = self._middleware()
        req = _FakeRequest(
            [_tool("svc_query")], state={"loaded_tool_names": ["svc_query"]}
        )
        out = mw.wrap_model_call(req, lambda r: r)
        tools = out.overridden
        assert tools is not None
        assert not any(
            "[Deferred reference:" in (getattr(t, "description", "") or "") for t in tools
        )

    def test_no_mcp_tools_no_search_tool(self) -> None:
        mw = self._middleware()
        req = _FakeRequest([_tool("read_file")])
        out = mw.wrap_model_call(req, lambda r: r)
        tools = out.overridden
        assert tools is not None
        assert [getattr(t, "name", None) for t in tools] == ["read_file"]
