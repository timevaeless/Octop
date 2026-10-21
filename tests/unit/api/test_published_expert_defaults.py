"""Installer-scoped rewrite of published-expert default_mcp_servers.

The detail endpoint must hand the composer publisher-mount names already
translated onto the viewer's visible keys — otherwise the template-install
guard drops defaults the installer can actually use (2026-10-21: 「已跳过N项
资源」 while the picker still offered the same connector).
"""

from __future__ import annotations

from typing import Any

import pytest

from octop.api.routers.experts import _installer_scoped_mcp_defaults

PARENT_A = "01M52GPZ7BYF6Z6B0J638SJ5C0"  # publisher-side parent
PARENT_B = "01M4Z7G2D9B617M1ZC5K5KS3FH"  # installer-side parent


def _fake_visible(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> None:
    def _visible(registry: Any, user_id: int) -> list[dict[str, Any]]:
        assert registry is not None
        return [{"mcp_server_name": name} for name in names]

    monkeypatch.setattr("octop.api.routers.experts.visible_mcp_servers", _visible)


def test_bare_publisher_name_maps_to_installer_mount_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_visible(monkeypatch, [f"custom__{PARENT_B}__wren-summer"])

    assert _installer_scoped_mcp_defaults(["wren-summer"], registry=object(), user_id=7) == [
        f"custom__{PARENT_B}__wren-summer"
    ]


def test_publisher_parent_key_maps_onto_installer_parent_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_visible(monkeypatch, [f"custom__{PARENT_B}__mcp-summer-laac"])

    assert _installer_scoped_mcp_defaults(
        [f"custom__{PARENT_A}__mcp-summer-laac"], registry=object(), user_id=7
    ) == [f"custom__{PARENT_B}__mcp-summer-laac"]


def test_already_visible_name_is_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = f"custom__{PARENT_B}__wren-summer"
    _fake_visible(monkeypatch, [key, "wren"])

    assert _installer_scoped_mcp_defaults([key, "wren"], registry=object(), user_id=7) == [
        key,
        "wren",
    ]


def test_dead_key_passes_through_for_the_guard_to_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No visible equivalent: keep the raw value — the composer guard keeps
    its skip-and-warn behaviour, and the install path's soft filter decides."""
    _fake_visible(monkeypatch, [])

    assert _installer_scoped_mcp_defaults(
        ["weknora__01M4HQ1M5K125VY381FC4ZXMSN"], registry=object(), user_id=7
    ) == ["weknora__01M4HQ1M5K125VY381FC4ZXMSN"]


def test_builtin_kind_name_is_never_rewritten_by_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_visible(monkeypatch, ["weknora__01M4HQ1M5K125VY381FC4ZXMSN"])

    assert _installer_scoped_mcp_defaults(
        ["weknora__01M4AAAAA5K125VY381FC4ZXMSN"], registry=object(), user_id=7
    ) == ["weknora__01M4AAAAA5K125VY381FC4ZXMSN"]


def test_empty_defaults_stay_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_visible(monkeypatch, [f"custom__{PARENT_B}__wren-summer"])

    assert _installer_scoped_mcp_defaults([], registry=object(), user_id=7) == []
