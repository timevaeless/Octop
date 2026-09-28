"""Stage-0 multi-replica leader gating (cluster plan D4).

Local replicas scale by booting N full ``octop run`` nodes behind an LB.
Background singletons (cron / IM / proactive / knowledge recovery) must run
on exactly one node: replicas opted out via ``OCTOP_LEADER=0`` stay pure HTTP
servers. Explicit ``leader=`` always wins; the single-node default (no env,
no arg) is unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from octop.infra.server import OctopServer, _StandaloneBackgroundGate


@pytest.fixture(autouse=True)
def _clean_octop_leader_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("OCTOP_LEADER", raising=False)
    yield


@pytest.mark.parametrize("env", ["0", "false", "no", "off", " 0 "])
def test_octop_leader_zero_opts_out(env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCTOP_LEADER", env)
    srv = OctopServer(home=Path("/tmp/unused"))
    assert srv._leader_enabled is False


@pytest.mark.parametrize("env", [None, "1", "true", "yes", "", "garbage"])
def test_octop_leader_unset_or_truthy_is_leader(
    env: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if env is None:
        monkeypatch.delenv("OCTOP_LEADER", raising=False)
    else:
        monkeypatch.setenv("OCTOP_LEADER", env)
    srv = OctopServer(home=Path("/tmp/unused"))
    assert srv._leader_enabled is True


def test_explicit_leader_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCTOP_LEADER", "0")
    assert OctopServer(home=Path("/tmp/unused"), leader=True)._leader_enabled is True
    monkeypatch.setenv("OCTOP_LEADER", "1")
    assert OctopServer(home=Path("/tmp/unused"), leader=False)._leader_enabled is False


def test_standby_gate_runs_no_singletons() -> None:
    """A gated-out node must not start routed singletons (D4 core behaviour)."""
    started: list[str] = []
    gate = _StandaloneBackgroundGate(enabled=False)

    def _start() -> str:
        started.append("cron")
        return "handle"

    import asyncio

    asyncio.run(gate.route("cron", _start, lambda: None))
    assert started == [], "background singleton started on a gated-out replica"

    gate_on = _StandaloneBackgroundGate(enabled=True)
    asyncio.run(gate_on.route("cron", _start, lambda: None))
    assert started == ["cron"]
