"""Tests for expert template upgrade propagation (conservative, manual)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from octop.infra.agents.expert_upgrade import (
    FINGERPRINT_REL,
    UpgradeReport,
    upgrade_for_expert_id,
    upgrade_for_template_name,
    upgrade_instance,
)


class MemWorkspace:
    """In-memory backend workspace: enough surface for the upgrade module."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files or {})

    async def adownload_bytes(self, rel: str) -> bytes | None:
        return self.files.get(rel)

    async def aupload_many(self, pairs: list[tuple[str, bytes]]) -> None:
        for rel, data in pairs:
            self.files[rel] = data


class FakeRegistry:
    def __init__(self, workspaces: dict[str, MemWorkspace]) -> None:
        self.workspaces = workspaces
        self.reloaded: list[str] = []

    def workspace_for_agent(self, agent_id: str) -> MemWorkspace | None:
        return self.workspaces.get(agent_id)

    async def _reload_agents(self, agent_ids: list[str]) -> None:
        self.reloaded.extend(agent_ids)


def _row(agent_id: str, *, template_name: str | None = None, published_expert_id: str | None = None):
    return SimpleNamespace(
        agent_id=agent_id,
        name=f"agent-{agent_id}",
        template_name=template_name,
        published_expert_id=published_expert_id,
    )


def _server(
    registry: FakeRegistry,
    rows: list,
    *,
    published_rows: dict | None = None,
    snapshot_dir: Path | None = None,
    catalog_dir: Path | None = None,
) -> SimpleNamespace:
    published_rows = published_rows or {}

    class Paths:
        def published_expert_snapshot_dir(self, expert_id: str, *, org_id=None) -> Path:
            assert snapshot_dir is not None
            return snapshot_dir

    class Catalog:
        def expert_dir(self, template_name: str) -> Path:
            assert catalog_dir is not None
            return catalog_dir

    return SimpleNamespace(
        services=SimpleNamespace(
            agent_repo=SimpleNamespace(list_all=lambda *, include_disabled: rows),
            published_expert_repo=SimpleNamespace(
                get=lambda expert_id: published_rows.get(expert_id)
            ),
            paths=Paths(),
        ),
        app_runtime=SimpleNamespace(agent_registry=registry),
        expert_catalog=Catalog(),
    )


@pytest.fixture()
def template_dir(tmp_path: Path) -> Path:
    d = tmp_path / "template"
    (d / "skills").mkdir(parents=True)
    (d / "SOUL.md").write_text("soul-v1", encoding="utf-8")
    (d / "USER.md").write_text("template-user-placeholder", encoding="utf-8")
    (d / "skills" / "main.md").write_text("skill-v1", encoding="utf-8")
    return d


async def test_first_sync_when_instance_matches_template(template_dir: Path) -> None:
    ws = MemWorkspace({"SOUL.md": b"soul-v1", "skills/main.md": b"skill-v1"})
    report = await upgrade_instance(
        agent_id="a1", name="n", source_dir=template_dir, workspace=ws
    )
    assert report.conflicts == [] and report.synced == [] and report.added == []
    assert sorted(report.unchanged) == ["SOUL.md", "skills/main.md"]
    # USER.md from the template is never touched or recorded.
    assert "USER.md" not in ws.files
    ledger = json.loads(ws.files[FINGERPRINT_REL])
    assert set(ledger) == {"SOUL.md", "skills/main.md"}


async def test_template_update_syncs_unmodified_files(template_dir: Path) -> None:
    ws = MemWorkspace({"SOUL.md": b"soul-v1", "skills/main.md": b"skill-v1"})
    await upgrade_instance(agent_id="a1", name="n", source_dir=template_dir, workspace=ws)
    (template_dir / "SOUL.md").write_text("soul-v2", encoding="utf-8")
    (template_dir / "skills" / "extra.md").write_text("extra", encoding="utf-8")

    report = await upgrade_instance(agent_id="a1", name="n", source_dir=template_dir, workspace=ws)

    assert report.synced == ["SOUL.md"]
    assert report.added == ["skills/extra.md"]
    assert ws.files["SOUL.md"] == b"soul-v2"
    assert report.conflicts == []


async def test_owner_modified_file_is_skipped(template_dir: Path) -> None:
    ws = MemWorkspace({"SOUL.md": b"soul-v1", "skills/main.md": b"skill-v1"})
    await upgrade_instance(agent_id="a1", name="n", source_dir=template_dir, workspace=ws)
    ws.files["SOUL.md"] = b"owner customisation"
    (template_dir / "SOUL.md").write_text("soul-v2", encoding="utf-8")

    report = await upgrade_instance(agent_id="a1", name="n", source_dir=template_dir, workspace=ws)

    assert report.conflicts == ["SOUL.md"]
    assert ws.files["SOUL.md"] == b"owner customisation"
    # Other authoritative files still sync.
    assert report.synced == [] and report.unchanged == ["skills/main.md"]


async def test_legacy_instance_with_divergent_content_is_a_conflict(template_dir: Path) -> None:
    ws = MemWorkspace({"SOUL.md": b"some ancient fork"})
    report = await upgrade_instance(agent_id="a1", name="n", source_dir=template_dir, workspace=ws)
    assert report.conflicts == ["SOUL.md"]
    assert ws.files["SOUL.md"] == b"some ancient fork"


async def test_manifest_maps_to_workspace_manifest_path(tmp_path: Path) -> None:
    d = tmp_path / "t"
    d.mkdir()
    (d / "manifest.json").write_text("{}", encoding="utf-8")
    ws = MemWorkspace()
    report = await upgrade_instance(agent_id="a1", name="n", source_dir=d, workspace=ws)
    assert report.added == [".octop/manifest.json"]
    assert ws.files[".octop/manifest.json"] == b"{}"


async def test_batch_upgrade_filters_and_reloads_touched_only(tmp_path: Path) -> None:
    src = tmp_path / "tpl"
    src.mkdir()
    (src / "SOUL.md").write_text("v1", encoding="utf-8")
    wss = {
        "a1": MemWorkspace(),          # new file → added → reload
        "a2": MemWorkspace({"SOUL.md": b"v1"}),  # unchanged → no reload
    }
    rows = [_row("a1", template_name="fe"), _row("a2", template_name="fe"), _row("a3")]
    registry = FakeRegistry(wss)
    server = _server(registry, rows, catalog_dir=src)

    report = await upgrade_for_template_name(server, "fe")

    assert isinstance(report, UpgradeReport)
    assert report.touched_instances == 1
    assert registry.reloaded == ["a1"]
    by_id = {i.agent_id: i for i in report.instances}
    assert set(by_id) == {"a1", "a2"}
    assert by_id["a1"].added == ["SOUL.md"]
    assert by_id["a2"].unchanged == ["SOUL.md"]


async def test_published_expert_anchor_and_snapshot_source(tmp_path: Path) -> None:
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "SOUL.md").write_text("snapshot-v1", encoding="utf-8")
    rows = [_row("a9", published_expert_id="exp1"), _row("a8", template_name="fe")]
    registry = FakeRegistry({"a9": MemWorkspace(), "a8": MemWorkspace()})
    server = _server(
        registry,
        rows,
        published_rows={"exp1": SimpleNamespace(org_id=None)},
        snapshot_dir=snap,
        catalog_dir=tmp_path / "unused",
    )

    report = await upgrade_for_expert_id(server, "exp1")

    assert {i.agent_id for i in report.instances} == {"a9"}
    assert report.instances[0].added == ["SOUL.md"]
    assert registry.reloaded == ["a9"]


async def test_env_guard_short_circuits(template_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCTOP_EXPERT_UPGRADE", "0")
    registry = FakeRegistry({"a1": MemWorkspace()})
    rows = [_row("a1", template_name="fe")]
    server = _server(registry, rows, catalog_dir=template_dir)

    report = await upgrade_for_template_name(server, "fe")

    assert report.env_disabled is True
    assert report.instances == [] and registry.reloaded == []


async def test_unknown_expert_id_is_a_clean_empty_report(tmp_path: Path) -> None:
    server = _server(FakeRegistry({}), [], snapshot_dir=tmp_path)
    report = await upgrade_for_expert_id(server, "missing")
    assert report.instances == [] and report.touched_instances == 0
