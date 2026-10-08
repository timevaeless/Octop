"""Unit tests for the octop-harness resume configurable patch.

Regression: upstream resume_hitl rebuilds only ``thread_id`` in
configurable, so MCPToolMiddleware strips every MCP tool after a HITL
resume (``mcp=7->0``). The patch restores ``mcp_servers`` from the
agent's default servers.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any

import pytest

from octop.config import OctopConfig
from octop.infra.agents.harness_resume_patch import (
    apply_harness_resume_patch,
    resume_mcp_configurable,
)
from octop_harness.agent import HarnessAgent


class _FakeProto:
    name = "langgraph"

    def __init__(self, *, with_resume: bool = True) -> None:
        self.captured: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
        self._with_resume = with_resume

    async def resume_stream(
        self,
        thread_id: str,
        decisions: list[dict[str, Any]],
        config: dict[str, Any],
        **_kwargs: Any,
    ):
        self.captured.append((thread_id, decisions, config))
        yield {"type": "done"}


def _fake_agent(proto: _FakeProto) -> SimpleNamespace:
    @contextlib.asynccontextmanager
    async def _invocation():
        yield

    return SimpleNamespace(
        _get_protocol=lambda protocol: proto,
        _invocation=_invocation,
    )


async def _drain(agen: Any) -> list[Any]:
    return [chunk async for chunk in agen]


def test_resume_mcp_configurable_empty_is_none() -> None:
    assert resume_mcp_configurable(None) is None
    assert resume_mcp_configurable([]) is None
    assert resume_mcp_configurable(["", "  "]) is None


def test_resume_mcp_configurable_returns_server_list() -> None:
    extra = resume_mcp_configurable(["custom__a__mcp-1", "custom__b__mcp-2"])
    assert extra == {"mcp_servers": ["custom__a__mcp-1", "custom__b__mcp-2"]}


def test_patch_applies_once_and_is_idempotent() -> None:
    # Other tests may have constructed an AgentManager first, which already
    # applied the patch; normalize before asserting the once-only semantics.
    if not getattr(HarnessAgent, "_octop_resume_configurable_patched", False):
        assert apply_harness_resume_patch() is True
    assert apply_harness_resume_patch() is False


@pytest.mark.asyncio
async def test_patched_resume_injects_extra_configurable() -> None:
    apply_harness_resume_patch()
    proto = _FakeProto()
    agen = HarnessAgent.resume_hitl(
        _fake_agent(proto),
        "thread-1",
        [{"type": "approve"}],
        extra_configurable={"mcp_servers": ["custom__a__mcp-1"]},
    )
    chunks = await _drain(agen)

    assert chunks == [{"type": "done"}]
    thread_id, decisions, config = proto.captured[0]
    assert thread_id == "thread-1"
    assert decisions == [{"type": "approve"}]
    assert config["configurable"]["thread_id"] == "thread-1"
    assert config["configurable"]["mcp_servers"] == ["custom__a__mcp-1"]


@pytest.mark.asyncio
async def test_patched_resume_without_extra_matches_upstream_shape() -> None:
    apply_harness_resume_patch()
    proto = _FakeProto()
    agen = HarnessAgent.resume_hitl(
        _fake_agent(proto),
        "thread-2",
        [{"type": "reject"}],
    )
    await _drain(agen)

    _, _, config = proto.captured[0]
    assert config == {"configurable": {"thread_id": "thread-2"}}


@pytest.mark.asyncio
async def test_patched_resume_raises_without_resume_stream() -> None:
    apply_harness_resume_patch()
    proto = _FakeProto(with_resume=False)
    proto.resume_stream = None  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="does not support HITL resume"):
        await _drain(
            HarnessAgent.resume_hitl(
                _fake_agent(proto),
                "thread-3",
                [{"type": "approve"}],
            )
        )


@pytest.mark.asyncio
async def test_manager_resume_forwards_default_mcp_servers(tmp_path: Any) -> None:
    """AgentManager.resume_hitl passes the agent's default servers through."""
    from octop.infra.agents.manager import AgentManager
    from octop.infra.agents.settings.profile import dump_id_list
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.services import build_shared_services
    from octop.infra.utils.paths import PathLayout

    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    manager = AgentManager(repos=services.repos, paths=services.paths)

    uid = services.repos.user_repo.create(
        username="resume-u", password_hash="x", role="user"
    )
    services.repos.agent_repo.create(agent_id="agent-mcp", user_id=uid, name="a1")
    services.repos.agent_repo.update_config(
        "agent-mcp",
        mcp_servers=dump_id_list(["custom__a__mcp-1", "custom__b__mcp-2"]),
    )

    captured: dict[str, Any] = {}

    class _FakeHarnessManager:
        async def resume_hitl(self, agent_id: str, thread_id: str, decisions: list, **kwargs: Any):
            captured.update({"agent_id": agent_id, "thread_id": thread_id, **kwargs})
            yield {"type": "done"}

    manager._harness_manager = _FakeHarnessManager()  # type: ignore[assignment]

    chunks = [
        chunk
        async for chunk in manager.resume_hitl("agent-mcp", "thread-9", [{"type": "approve"}])
    ]

    assert chunks == [{"type": "done"}]
    assert captured["agent_id"] == "agent-mcp"
    assert captured["thread_id"] == "thread-9"
    assert captured["extra_configurable"] == {
        "mcp_servers": ["custom__a__mcp-1", "custom__b__mcp-2"]
    }


@pytest.mark.asyncio
async def test_manager_resume_without_defaults_passes_none(tmp_path: Any) -> None:
    """Agents without MCP attached keep the exact upstream resume call shape."""
    from octop.infra.agents.manager import AgentManager
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.services import build_shared_services
    from octop.infra.utils.paths import PathLayout

    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    manager = AgentManager(repos=services.repos, paths=services.paths)

    uid = services.repos.user_repo.create(
        username="resume-nomcp", password_hash="x", role="user"
    )
    services.repos.agent_repo.create(agent_id="agent-bare", user_id=uid, name="a2")

    captured: dict[str, Any] = {}

    class _FakeHarnessManager:
        async def resume_hitl(self, agent_id: str, thread_id: str, decisions: list, **kwargs: Any):
            captured.update({**kwargs})
            yield {"type": "done"}

    manager._harness_manager = _FakeHarnessManager()  # type: ignore[assignment]

    async for _ in manager.resume_hitl("agent-bare", "thread-8", [{"type": "approve"}]):
        pass

    assert captured == {"extra_configurable": None}
