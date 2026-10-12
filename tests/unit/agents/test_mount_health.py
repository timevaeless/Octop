"""Unit tests for the mount_health T4 module (dead expert mount keys)."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from octop.infra.agents import mount_health as mh


class _FakeConnectorSvc:
    def __init__(self, active: list[str], error: Exception | None = None):
        self._active = set(active)
        self._error = error

    def list_active_mcp_server_names(self, user_id: int):
        if self._error is not None:
            raise self._error
        return list(self._active)


class _FakeAgentRepo:
    def __init__(self):
        self.saved: dict[str, str] = {}

    def update_config(self, agent_id: str, **kwargs):
        self.saved[agent_id] = kwargs["config_json"]


def _row(mcp_servers: list[str], *, config: dict | None = None, user_id: int = 7, kind: str = "expert"):
    return SimpleNamespace(
        agent_id="AG1",
        user_id=user_id,
        kind=kind,
        mcp_servers=json.dumps(mcp_servers),
        config_json=json.dumps(config or {}, ensure_ascii=False),
    )


# ---- dead_mount_keys -------------------------------------------------------


def test_dead_mount_keys_reports_missing():
    svc = _FakeConnectorSvc(["live-one"])
    dead = mh.dead_mount_keys(svc, 7, ["live-one", "custom__OLD__mcp-laac"])
    assert dead == ["custom__OLD__mcp-laac"]


def test_dead_mount_keys_fail_open_on_listing_error():
    svc = _FakeConnectorSvc([], error=RuntimeError("db down"))
    assert mh.dead_mount_keys(svc, 7, ["anything"]) == []


def test_dead_mount_keys_empty_or_ownerless():
    svc = _FakeConnectorSvc(["a"])
    assert mh.dead_mount_keys(svc, 7, []) == []
    assert mh.dead_mount_keys(svc, None, ["a"]) == []


# ---- audit_agent_mounts ----------------------------------------------------


def test_audit_persists_mount_warnings_on_reload():
    svc = _FakeConnectorSvc(["live-one"])
    repo = _FakeAgentRepo()
    dead = mh.audit_agent_mounts(
        "AG1", _row(["live-one", "custom__OLD__mcp-laac"]), connector_svc=svc, agent_repo=repo
    )
    assert dead == ["custom__OLD__mcp-laac"]
    cfg = json.loads(repo.saved["AG1"])
    assert cfg["mount_warnings"] == ["custom__OLD__mcp-laac"]


def test_audit_skips_write_when_unchanged():
    svc = _FakeConnectorSvc([])
    warnings = ["custom__OLD__mcp-laac"]
    repo = _FakeAgentRepo()
    row = _row(warnings, config={"mount_warnings": warnings})
    dead = mh.audit_agent_mounts("AG1", row, connector_svc=svc, agent_repo=repo)
    assert dead == warnings
    assert "AG1" not in repo.saved  # no pointless write


def test_audit_clears_stale_warning_after_recovery():
    svc = _FakeConnectorSvc(["live-one"])
    repo = _FakeAgentRepo()
    row = _row(["live-one"], config={"mount_warnings": ["custom__OLD__mcp-laac"]})
    assert mh.audit_agent_mounts("AG1", row, connector_svc=svc, agent_repo=repo) == []
    cfg = json.loads(repo.saved["AG1"])
    assert "mount_warnings" not in cfg


def test_audit_ignores_team_agents_and_ownerless_rows():
    svc = _FakeConnectorSvc([])
    repo = _FakeAgentRepo()
    assert (
        mh.audit_agent_mounts(
            "T1", _row(["x"], kind="team"), connector_svc=svc, agent_repo=repo
        )
        == []
    )
    assert (
        mh.audit_agent_mounts(
            "AG2", _row(["x"], user_id=None), connector_svc=svc, agent_repo=repo
        )
        == []
    )
    assert repo.saved == {}


def test_audit_never_raises(monkeypatch):
    svc = _FakeConnectorSvc(["a"])
    repo = _FakeAgentRepo()

    def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(mh, "dead_mount_keys", boom)
    assert (
        mh.audit_agent_mounts(
            "AG1", _row(["a"]), connector_svc=svc, agent_repo=repo
        )
        == []
    )
    assert repo.saved == {}


# ---- note_dropped_turn_picks ----------------------------------------------


def test_note_dropped_warns_then_rate_limits(caplog):
    mh._drop_warn_at.clear()
    with caplog.at_level(logging.WARNING, logger="octop.infra.agents.mount_health"):
        mh.note_dropped_turn_picks(7, ["custom__OLD__mcp-laac"], {"live-one"})
        assert "custom__OLD__mcp-laac" in caplog.text
        caplog.clear()
        mh.note_dropped_turn_picks(7, ["custom__OLD__mcp-laac"], {"live-one"})
        assert caplog.text == ""
    mh._drop_warn_at.clear()


def test_note_dropped_silent_when_all_picks_allowed(caplog):
    mh._drop_warn_at.clear()
    with caplog.at_level(logging.WARNING, logger="octop.infra.agents.mount_health"):
        mh.note_dropped_turn_picks(7, ["live-one", "live-two"], {"live-one", "live-two"})
        assert caplog.text == ""
    mh._drop_warn_at.clear()


# ---- upstream hooks wired ---------------------------------------------------


def test_reload_hook_wired_in_manager():
    import inspect

    from octop.infra.agents.manager import AgentManager

    src = inspect.getsource(AgentManager._reload_agent)
    assert "audit_agent_mounts" in src


def test_merge_hook_wired_in_service():
    import inspect

    from octop.infra.connectors.service import ConnectorService

    src = inspect.getsource(ConnectorService.merge_turn_mcp_servers)
    assert "note_dropped_turn_picks" in src


@pytest.mark.parametrize("module", ["octop.infra.agents.mount_health"])
def test_module_importable(module):
    __import__(module)
