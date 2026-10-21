"""Unit tests for the library-defaults admin editor (T4 pluggable module)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from octop.infra.agents.experts import library_defaults as ld
from octop.infra.errors import OctopError


class _FakeCatalog:
    def __init__(self, root: Path) -> None:
        self.root = root

    def refresh(self) -> None:  # pragma: no cover - asserted via flag
        raise AssertionError("refresh flag should be patched")


def _make_expert(root: Path, expert_id: str, manifest: dict[str, Any]) -> Path:
    exp = root / expert_id
    (exp / "skills").mkdir(parents=True)
    (exp / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return exp


@pytest.fixture()
def catalog(tmp_path: Path) -> _FakeCatalog:
    return _FakeCatalog(tmp_path)


def _rows(*pairs: tuple[str, str]) -> list[dict[str, Any]]:
    return [{"mcp_server_name": key, "display_name": label, "kind": "x"} for key, label in pairs]


def test_normalize_strips_mount_keys() -> None:
    assert ld.normalize_semantic_ref("custom__a1f9__wren-summer") == "wren-summer"
    assert ld.normalize_semantic_ref("weknora__01ABC") == "weknora"
    assert ld.normalize_semantic_ref("wren-summer") == "wren-summer"
    assert ld.normalize_semantic_ref("  ") == ""
    assert ld.normalize_semantic_ref(None) == ""


def test_normalize_refs_dedup_and_cap() -> None:
    values = ["custom__a__s1", "s1", "weknora__i1", "", 42, "x" * 500]
    out = ld.normalize_semantic_refs(values)
    assert out == ["s1", "weknora"]
    assert ld.normalize_semantic_refs([f"s{i}" for i in range(30)]) == [f"s{i}" for i in range(16)]


def test_read_and_write_roundtrip(catalog: _FakeCatalog, tmp_path: Path) -> None:
    _make_expert(Path(catalog.root), "demo", {"id": "demo", "label": "Demo"})
    refreshed: list[bool] = []
    catalog.refresh = lambda: refreshed.append(True)  # type: ignore[method-assign]

    payload = ld.write_defaults(catalog, "demo", ["custom__a1__wren-summer", "weknora__i9"])
    assert payload == ["wren-summer", "weknora"]
    assert refreshed == [True]

    data = json.loads((Path(catalog.root) / "demo" / "manifest.json").read_text())
    assert data["default_mcp_servers"] == ["wren-summer", "weknora"]
    assert data["label"] == "Demo"
    assert ld.read_defaults(catalog, "demo") == ["wren-summer", "weknora"]
    # No staging residue inside the expert dir (discover_seed_paths packages
    # every file there, dotfiles included) nor beside it in the library root.
    assert sorted(p.name for p in (Path(catalog.root) / "demo").iterdir()) == [
        "manifest.json",
        "skills",
    ]
    assert not [p for p in Path(catalog.root).iterdir() if p.is_file() and p.name.startswith(".")]


def test_write_empty_removes_key(catalog: _FakeCatalog, tmp_path: Path) -> None:
    _make_expert(
        Path(catalog.root),
        "demo",
        {"id": "demo", "default_mcp_servers": ["wren-summer"]},
    )
    catalog.refresh = lambda: None  # type: ignore[method-assign]
    assert ld.write_defaults(catalog, "demo", []) == []
    data = json.loads((Path(catalog.root) / "demo" / "manifest.json").read_text())
    assert "default_mcp_servers" not in data


def test_write_rejects_unknown_expert_and_bad_manifest(
    catalog: _FakeCatalog, tmp_path: Path
) -> None:
    _make_expert(Path(catalog.root), "demo", {"id": "demo"})
    catalog.refresh = lambda: None  # type: ignore[method-assign]

    with pytest.raises(OctopError):
        ld.write_defaults(catalog, "../escape", ["x"])
    with pytest.raises(OctopError):
        ld.write_defaults(catalog, "missing", ["x"])
    with pytest.raises(OctopError):
        ld.write_defaults(catalog, "demo", "not-a-list")

    (Path(catalog.root) / "demo" / "manifest.json").write_text("{broken")
    with pytest.raises(OctopError):
        ld.write_defaults(catalog, "demo", ["x"])


def test_defaults_payload_options_collapse_and_label(catalog: _FakeCatalog, tmp_path: Path) -> None:
    _make_expert(
        Path(catalog.root),
        "demo",
        {"id": "demo", "default_mcp_servers": ["wren-summer"]},
    )
    rows = _rows(
        ("custom__p1__wren-summer", "海外舆情检索"),
        ("custom__p2__wren-summer", "别名同源"),
        ("weknora__i1", "企业知识库"),
    )
    payload = ld.defaults_payload(catalog, rows, "demo")
    assert payload["default_mcp_servers"] == ["wren-summer"]
    assert payload["options"] == [
        {"value": "wren-summer", "label": "海外舆情检索", "kind": "x"},
        {"value": "weknora", "label": "企业知识库", "kind": "x"},
    ]


def test_no_manifest_template_rejected(catalog: _FakeCatalog, tmp_path: Path) -> None:
    (Path(catalog.root) / "bare").mkdir(parents=True)
    with pytest.raises(OctopError):
        ld.read_defaults(catalog, "bare")
