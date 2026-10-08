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


def test_agent_workspace_tenant_scoped(tmp_path: Path):
    p = PathLayout(tmp_path / ".octop")
    ws = p.agent_workspace("agent01", org_id="org-a")
    assert ws == tmp_path / ".octop" / "orgs" / "org-a" / "agents" / "agent01"
    # Tenant agent adopts its pre-existing legacy flat dir too.
    legacy = tmp_path / ".octop" / "agents" / "agent01"
    legacy.mkdir(parents=True)
    assert p.agent_workspace("agent01", org_id="org-a") == legacy
    # Scoped dir wins once it exists.
    scoped = tmp_path / ".octop" / "orgs" / "org-a" / "agents" / "agent01"
    scoped.mkdir(parents=True)
    assert p.agent_workspace("agent01", org_id="org-a") == scoped


def test_org_dir_sanitizes_segment(tmp_path: Path):
    p = PathLayout(tmp_path / ".octop")
    assert p.org_dir("cheryfs") == tmp_path / ".octop" / "orgs" / "cheryfs"
    assert p.org_dir("../evil") == tmp_path / ".octop" / "orgs" / "___evil"


def test_published_expert_snapshot_dir(tmp_path: Path):
    p = PathLayout(tmp_path / ".octop")
    # Platform expert → shared tree; legacy flat dir adopted when present.
    assert p.published_expert_snapshot_dir("e1") == (
        tmp_path / ".octop" / "shared" / "published_experts" / "e1"
    )
    legacy = tmp_path / ".octop" / "published_experts" / "e1"
    legacy.mkdir(parents=True)
    assert p.published_expert_snapshot_dir("e1") == legacy
    # Tenant expert → orgs tree.
    assert p.published_expert_snapshot_dir("e2", org_id="org-b") == (
        tmp_path / ".octop" / "orgs" / "org-b" / "published_experts" / "e2"
    )


def test_knowledge_base_dir(tmp_path: Path):
    p = PathLayout(tmp_path / ".octop")
    assert p.knowledge_base_dir("kb1") == (
        tmp_path / ".octop" / "shared" / "knowledge" / "kb1"
    )
    # Adopt-legacy is per kb_id: once the flat dir exists it wins for that KB.
    legacy = tmp_path / ".octop" / "knowledge" / "kb1"
    legacy.mkdir(parents=True)
    assert p.knowledge_base_dir("kb1", org_id="org-c") == legacy
    # A KB without a legacy dir resolves into its org tree.
    assert p.knowledge_base_dir("kb2", org_id="org-c") == (
        tmp_path / ".octop" / "orgs" / "org-c" / "knowledge" / "kb2"
    )


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
    # Tenant-scoped path
    out = p.ensure_agent_workspace("a2", org_id="org-a")
    assert out.is_dir()
    assert out == tmp_path / ".octop" / "orgs" / "org-a" / "agents" / "a2"


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
