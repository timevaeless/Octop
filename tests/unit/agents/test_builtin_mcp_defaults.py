"""Builtin-expert manifest composer defaults (semantic MCP references).

Manifest values are semantic references (bare server name, catalog server
name, or built-in kind) — mount keys are per-user data and can never live in
a bundled manifest. Resolution happens per viewer against the visible set.
"""

from __future__ import annotations

import json
from pathlib import Path

from octop.infra.agents.experts.builtin_mcp_defaults import (
    filter_installer_scoped,
    manifest_default_mcp_servers,
    resolve_installer_scoped,
)

PARENT_A = "01M52GPZ7BYF6Z6B0J638SJ5C0"
PARENT_B = "01M4Z7G2D9B617M1ZC5K5KS3FH"
WEKNORA = "01M4HQ1M5K125VY381FC4ZXMSN"


def _write_manifest(tmp_path: Path, data: object) -> Path:
    (tmp_path / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


def test_manifest_defaults_tolerant(tmp_path: Path) -> None:
    assert manifest_default_mcp_servers(_write_manifest(tmp_path, {})) == []
    assert (
        manifest_default_mcp_servers(_write_manifest(tmp_path, {"default_mcp_servers": []})) == []
    )
    assert (
        manifest_default_mcp_servers(_write_manifest(tmp_path, {"default_mcp_servers": "x"})) == []
    )
    assert manifest_default_mcp_servers(
        _write_manifest(tmp_path, {"default_mcp_servers": [1, None, " a ", "a"]})
    ) == ["a"]
    (tmp_path / "manifest.json").write_text("not json{", encoding="utf-8")
    assert manifest_default_mcp_servers(tmp_path) == []
    assert manifest_default_mcp_servers(tmp_path / "missing") == []


def test_exact_visible_key_is_kept() -> None:
    visible = {f"custom__{PARENT_B}__wren-summer", f"weknora__{WEKNORA}"}

    assert resolve_installer_scoped([f"custom__{PARENT_B}__wren-summer"], visible) == [
        f"custom__{PARENT_B}__wren-summer"
    ]


def test_bare_server_name_maps_to_unique_custom_mount() -> None:
    visible = {f"custom__{PARENT_B}__wren-summer", f"weknora__{WEKNORA}"}

    assert resolve_installer_scoped(["wren-summer"], visible) == [
        f"custom__{PARENT_B}__wren-summer"
    ]


def test_builtin_kind_maps_to_the_viewers_single_instance() -> None:
    visible = {f"weknora__{WEKNORA}", f"custom__{PARENT_B}__wren-summer"}

    assert resolve_installer_scoped(["weknora"], visible) == [f"weknora__{WEKNORA}"]


def test_stale_kind_key_heals_onto_the_viewers_instance() -> None:
    """A concrete old key still binds by kind when exactly one instance exists."""
    visible = {f"weknora__{WEKNORA}"}

    assert resolve_installer_scoped(["weknora__01M4AAAAA5K125VY381FC4ZXMSN"], visible) == [
        f"weknora__{WEKNORA}"
    ]


def test_ambiguous_kind_is_left_for_the_guard() -> None:
    visible = {f"weknora__{WEKNORA}", "weknora__01M4BBBBB5K125VY381FC4ZXMSN"}

    assert resolve_installer_scoped(["weknora"], visible) == ["weknora"]


def test_unresolvable_reference_passes_through() -> None:
    assert resolve_installer_scoped(["no-such-server"], set()) == ["no-such-server"]


def test_order_preserved_and_duplicates_collapsed() -> None:
    visible = {f"custom__{PARENT_B}__wren-summer", f"weknora__{WEKNORA}"}

    assert resolve_installer_scoped(["weknora", "wren-summer", "weknora", "other"], visible) == [
        f"weknora__{WEKNORA}",
        f"custom__{PARENT_B}__wren-summer",
        "other",
    ]


def test_filter_drops_unresolvable_references() -> None:
    visible = {f"weknora__{WEKNORA}"}

    assert filter_installer_scoped(["weknora", "ghost"], visible) == [f"weknora__{WEKNORA}"]
    assert filter_installer_scoped([], visible) == []
