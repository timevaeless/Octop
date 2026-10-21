"""Unit tests for the macOS AppleDouble zip-entry filter."""

from __future__ import annotations

import pytest

from octop.infra.skills.macos_zip_junk import is_macos_junk_entry


@pytest.mark.parametrize(
    "name",
    [
        "__MACOSX/._pkg",
        "__MACOSX/pkg/._manifest.json",
        "__MACOSX/pkg/._SOUL.md",
        "a/__MACOSX/b/._c",
        "._stray",  # AppleDouble outside __MACOSX (robustness)
        "pkg/._hidden",
    ],
)
def test_junk_entries_are_matched(name: str) -> None:
    assert is_macos_junk_entry(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "manifest.json",
        "SOUL.md",
        "skills/demo/SKILL.md",
        ".gitignore",  # normal dotfile is not AppleDouble
        ".env",
        "skills/demo/scripts/build.py",
        "pkg/__macosx/keep.md",  # case-sensitive: only uppercase is junk
        "",
    ],
)
def test_normal_entries_are_kept(name: str) -> None:
    assert is_macos_junk_entry(name) is False
