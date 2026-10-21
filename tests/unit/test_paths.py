"""tests/unit/test_paths.py"""

from __future__ import annotations

from pathlib import Path

from octop.infra.utils.paths import PathLayout


def test_root_paths(tmp_path: Path):
    p = PathLayout(tmp_path / ".octop")
    assert p.root == tmp_path / ".octop"
    assert p.db == tmp_path / ".octop" / "octop.db"
    assert p.logs_dir == tmp_path / ".octop" / "logs"
    assert p.log == tmp_path / ".octop" / "logs" / "octop.log"
    assert p.config == tmp_path / ".octop" / "config.json"


def test_log_dir_creation(tmp_path: Path):
    p = PathLayout(tmp_path / ".octop")
    assert not (tmp_path / ".octop" / "logs").exists()
    assert p.ensure_log() == tmp_path / ".octop" / "logs" / "octop.log"
    assert (tmp_path / ".octop" / "logs").is_dir()
    assert p.log == tmp_path / ".octop" / "logs" / "octop.log"


def test_user_dir_uses_username(tmp_path: Path):
    p = PathLayout(tmp_path / ".octop")
    assert p.user_dir("alice") == tmp_path / ".octop" / "users" / "alice"


def test_agent_workspace_platform_adopts_legacy(tmp_path: Path):
    p = PathLayout(tmp_path / ".octop")
    # No legacy dir → platform agents resolve into the shared tree.
    ws = p.agent_workspace("agent01")
    assert ws == tmp_path / ".octop" / "shared" / "agents" / "agent01"
    # Existing legacy dir (pre-T2-3 install) keeps working (adopt-legacy).
    legacy = tmp_path / ".octop" / "agents" / "agent01"
    legacy.mkdir(parents=True)
    assert p.agent_workspace("agent01") == legacy


def test_agent_workspace_ignores_org_id(tmp_path: Path):
    # 2026-10-08 simplification: attribution never moves directories —
    # ``org_id`` is accepted for call-site compatibility and ignored.
    p = PathLayout(tmp_path / ".octop")
    assert p.agent_workspace("agent01", org_id="org-a") == (
        tmp_path / ".octop" / "shared" / "agents" / "agent01"
    )
    # Pre-existing legacy flat dir keeps working (adopt-legacy), tenant or not.
    legacy = tmp_path / ".octop" / "agents" / "agent01"
    legacy.mkdir(parents=True)
    assert p.agent_workspace("agent01", org_id="org-a") == legacy


def test_org_id_segment_never_reaches_filesystem(tmp_path: Path):
    # There is no per-org directory plane anymore, so even a hostile
    # org_id must not influence path resolution.
    p = PathLayout(tmp_path / ".octop")
    assert p.agent_workspace("agent01", org_id="../evil") == (
        tmp_path / ".octop" / "shared" / "agents" / "agent01"
    )
    assert p.published_expert_snapshot_dir("e1", org_id="../evil") == (
        tmp_path / ".octop" / "shared" / "published_experts" / "e1"
    )
    assert p.knowledge_base_dir("kb1", org_id="../evil") == (
        tmp_path / ".octop" / "shared" / "knowledge" / "kb1"
    )


def test_published_expert_snapshot_dir(tmp_path: Path):
    p = PathLayout(tmp_path / ".octop")
    # Any attribution → shared tree; legacy flat dir adopted when present.
    assert p.published_expert_snapshot_dir("e1") == (
        tmp_path / ".octop" / "shared" / "published_experts" / "e1"
    )
    assert p.published_expert_snapshot_dir("e2", org_id="org-b") == (
        tmp_path / ".octop" / "shared" / "published_experts" / "e2"
    )
    legacy = tmp_path / ".octop" / "published_experts" / "e1"
    legacy.mkdir(parents=True)
    assert p.published_expert_snapshot_dir("e1") == legacy


def test_knowledge_base_dir(tmp_path: Path):
    p = PathLayout(tmp_path / ".octop")
    assert p.knowledge_base_dir("kb1") == (
        tmp_path / ".octop" / "shared" / "knowledge" / "kb1"
    )
    assert p.knowledge_base_dir("kb2", org_id="org-c") == (
        tmp_path / ".octop" / "shared" / "knowledge" / "kb2"
    )
    # Adopt-legacy is per kb_id: once the flat dir exists it wins for that KB.
    legacy = tmp_path / ".octop" / "knowledge" / "kb1"
    legacy.mkdir(parents=True)
    assert p.knowledge_base_dir("kb1", org_id="org-c") == legacy


def test_ensure_root_creates_dir(tmp_path: Path):
    p = PathLayout(tmp_path / ".octop")
    p.ensure_root()
    assert (tmp_path / ".octop").is_dir()


def test_ensure_agent_workspace(tmp_path: Path):
    p = PathLayout(tmp_path / ".octop")
    # New platform (shared) path
    out = p.ensure_agent_workspace("a1")
    assert out.is_dir()
    assert out == tmp_path / ".octop" / "shared" / "agents" / "a1"
    # Tenant-scoped call sites land in the shared tree too (org_id ignored).
    out = p.ensure_agent_workspace("a2", org_id="org-a")
    assert out.is_dir()
    assert out == tmp_path / ".octop" / "shared" / "agents" / "a2"


def test_backups_dir(tmp_path: Path) -> None:
    p = PathLayout(tmp_path / ".octop")
    assert p.backups_dir == tmp_path / ".octop" / "backups"
    assert p.ensure_backups_dir().is_dir()


def test_knowledge_dir(tmp_path: Path) -> None:
    p = PathLayout(tmp_path / ".octop")
    assert p.knowledge_dir == tmp_path / ".octop" / "knowledge"


def test_path_layout_from_env_defaults_to_dot_octop(monkeypatch) -> None:
    monkeypatch.delenv("OCTOP_HOME", raising=False)
    assert PathLayout.from_env().root == Path.home() / ".octop"


def test_path_layout_from_env_honors_octop_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path))
    assert PathLayout.from_env().root == tmp_path
