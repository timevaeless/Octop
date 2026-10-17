"""Unit tests for the octop/ext declarative patch registry."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from octop.ext.patches import (
    PatchError,
    _check_hook_sync,
    _Patch,
    _resolve,
    _wrap,
    install_all,
    registered_patches,
)
from octop.infra.errors import ErrorCode


# ---------------------------------------------------------------------------
# _resolve — fail-loud dotted target resolution


def test_resolve_rejects_malformed_target() -> None:
    with pytest.raises(PatchError, match="expected 'package.module:Owner.attr'"):
        _resolve("no-colon-target")
    with pytest.raises(PatchError, match="expected 'package.module:Owner.attr'"):
        _resolve(":Missing")
    with pytest.raises(PatchError, match="expected 'package.module:Owner.attr'"):
        _resolve("octop.ext.patches:")


def test_resolve_missing_module_fails_loud() -> None:
    with pytest.raises(PatchError, match="not importable"):
        _resolve("octop.no_such_module:Attr.method")


def test_resolve_missing_attribute_fails_loud() -> None:
    with pytest.raises(PatchError, match="upstream moved it"):
        _resolve("octop.ext.patches:NoSuchClass.method")
    with pytest.raises(PatchError, match="renamed or removed"):
        _resolve("octop.ext.patches:_Patch.no_such_field_method")


def test_resolve_returns_owner_and_attr() -> None:
    from octop.infra.agents.manager import AgentManager

    owner, attr = _resolve(
        "octop.infra.agents.manager:AgentManager.delete_thread_checkpoint"
    )
    assert owner is AgentManager
    assert attr == "delete_thread_checkpoint"


# ---------------------------------------------------------------------------
# _wrap — before/after semantics on sync and async targets


class _Dummy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def atrack(self, value: str) -> str:
        self.calls.append("target")
        return f"tracked:{value}"

    def strack(self, value: str) -> str:
        self.calls.append("target")
        return f"tracked:{value}"


async def test_before_patch_runs_before_async_target() -> None:
    dummy = _Dummy()
    seen: list[str] = []

    async def hook(self: Any, value: str) -> None:
        seen.append(f"hook:{value}")
        assert dummy.calls == []  # target not yet invoked

    wrapped = _wrap(_Dummy.atrack, _Patch("before", "t", hook, "m"))
    result = await wrapped(dummy, "x")
    assert result == "tracked:x"
    assert seen == ["hook:x"]
    assert dummy.calls == ["target"]


async def test_after_patch_preserves_result_async() -> None:
    dummy = _Dummy()
    order: list[str] = []

    async def hook(self: Any, value: str) -> None:
        order.append("hook")
        assert dummy.calls == ["target"]  # target already invoked

    wrapped = _wrap(_Dummy.atrack, _Patch("after", "t", hook, "m"))
    result = await wrapped(dummy, "x")
    assert result == "tracked:x"
    assert order == ["hook"]


def test_before_patch_sync_target() -> None:
    dummy = _Dummy()
    order: list[str] = []

    def hook(self: Any, value: str) -> None:
        order.append("hook")

    wrapped = _wrap(_Dummy.strack, _Patch("before", "t", hook, "m"))
    assert wrapped(dummy, "y") == "tracked:y"
    assert order == ["hook"]


def test_async_hook_on_sync_target_rejected() -> None:
    async def hook(self: Any, value: str) -> None:
        raise AssertionError("hook must never run")

    with pytest.raises(PatchError, match="declare the hook as a plain function"):
        _wrap(
            _Dummy.strack,
            _Patch("before", "t", hook, "m"),
        )


def test_sync_hook_on_async_target_allowed() -> None:
    def hook(self: Any, value: str) -> None: ...

    _check_hook_sync(_Dummy.atrack, _Patch("before", "t", hook, "m"))  # must not raise


# ---------------------------------------------------------------------------
# install_all — real target (the checkpointer-warmup pilot), idempotency


def _warmable_harness() -> Any:
    state: dict[str, bool] = {"setup_calls": 0, "deleted": False}

    class _Checkpointer:
        conn = object()

        async def setup(self) -> None:
            state["setup_calls"] += 1

    class _Harness:
        checkpointer = _Checkpointer()

        async def adelete_thread(self, thread_id: str) -> bool:
            state["deleted"] = True
            return True

    return _Harness(), state


async def test_install_all_applies_checkpointer_warmup_pilot(monkeypatch: pytest.MonkeyPatch) -> None:
    from octop.infra.agents.manager import AgentManager
    from octop.infra.errors import OctopError

    install_all()  # also idempotency guard for repeated test runs

    # Second call must not double-wrap.
    wrapped_once = AgentManager.delete_thread_checkpoint
    install_all()
    assert AgentManager.delete_thread_checkpoint is wrapped_once

    harness, state = _warmable_harness()
    manager = object.__new__(AgentManager)
    # Upstream 1.0.2b8 resolves the row first (store fallback afterwards); the
    # stub only needs get_row to report "no row" so the harness path runs.
    monkeypatch.setattr(
        manager,
        "_repos",
        SimpleNamespace(agent_repo=SimpleNamespace(get=lambda _agent_id: None)),
        raising=False,
    )
    monkeypatch.setattr(
        manager, "get_agent", lambda agent_id: harness, raising=False
    )

    # Upstream reshaped the method to ``-> None``; the warm-up before-patch
    # contract is unchanged: setup() runs before adelete_thread.
    assert await manager.delete_thread_checkpoint("a1", "t1") is None
    assert state["setup_calls"] == 1  # warm-up ran before deletion
    assert state["deleted"] is True

    # Non-running agent: hook must skip (get_agent raises OctopError) and the
    # upstream method re-raises AGENT_NOT_FOUND (no store fallback without a row).
    def _missing(agent_id: str) -> Any:
        raise OctopError(ErrorCode.AGENT_NOT_FOUND, "gone")

    monkeypatch.setattr(manager, "get_agent", _missing, raising=False)
    with pytest.raises(OctopError):
        await manager.delete_thread_checkpoint("a1", "t1")


def test_registered_patches_exposes_the_pilot() -> None:
    entries = registered_patches()
    targets = {target for _phase, target, _src in entries}
    assert "octop.infra.agents.manager:AgentManager.delete_thread_checkpoint" in targets
