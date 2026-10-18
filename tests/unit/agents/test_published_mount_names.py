"""Mount-name translation for published-expert installs."""

from __future__ import annotations

from octop.infra.agents.experts.published_mount_names import (
    custom_server_name,
    rewrite_installer_mount_names,
)

PARENT_A = "01M52GPZ7BYF6Z6B0J638SJ5C0"
PARENT_B = "01M4Z7G2D9B617M1ZC5K5KS3FH"


def test_bare_name_maps_to_the_installers_shared_mount_key() -> None:
    """The 2026-10-18 incident: publisher's bare ``wren-summer`` was unbindable."""
    visible = {f"custom__{PARENT_A}__wren-summer", "weknora__01M4HQ1M5K125VY381FC4ZXMSN"}

    assert rewrite_installer_mount_names(["wren-summer"], visible) == [
        f"custom__{PARENT_A}__wren-summer"
    ]


def test_publisher_parent_key_maps_onto_the_tenants_parent_key() -> None:
    """Catalog attachments mount per tenant, so the parent id differs too."""
    visible = {f"custom__{PARENT_B}__mcp-summer-laac"}

    assert rewrite_installer_mount_names(
        [f"custom__{PARENT_A}__mcp-summer-laac"], visible
    ) == [f"custom__{PARENT_B}__mcp-summer-laac"]


def test_already_visible_names_are_untouched() -> None:
    visible = {f"custom__{PARENT_A}__wren-summer", "wren"}

    assert rewrite_installer_mount_names(
        ["wren", f"custom__{PARENT_A}__wren-summer"], visible
    ) == ["wren", f"custom__{PARENT_A}__wren-summer"]


def test_ambiguous_server_name_is_left_to_the_callers_filter() -> None:
    """Two shared parents exposing the same name: never guess."""
    visible = {f"custom__{PARENT_A}__wren-summer", f"custom__{PARENT_B}__wren-summer"}

    assert rewrite_installer_mount_names(["wren-summer"], visible) == ["wren-summer"]


def test_builtin_kind_names_are_never_rewritten_by_suffix() -> None:
    visible = {"weknora__01M4HQ1M5K125VY381FC4ZXMSN"}

    assert rewrite_installer_mount_names(
        ["weknora__01M4AAAAA5K125VY381FC4ZXMSN"], visible
    ) == ["weknora__01M4AAAAA5K125VY381FC4ZXMSN"]


def test_unknown_names_pass_through_so_fail_fast_still_applies() -> None:
    assert rewrite_installer_mount_names(["nope"], {"wren"}) == ["nope"]


def test_order_preserved_and_duplicates_collapsed() -> None:
    visible = {f"custom__{PARENT_A}__wren-summer"}

    assert rewrite_installer_mount_names(
        ["wren-summer", "other", f"custom__{PARENT_A}__wren-summer", "other"],
        visible,
    ) == [f"custom__{PARENT_A}__wren-summer", "other"]


def test_empty_input() -> None:
    assert rewrite_installer_mount_names([], {"wren"}) == []


def test_custom_server_name_parsing() -> None:
    assert custom_server_name(f"custom__{PARENT_A}__wren-summer") == "wren-summer"
    assert custom_server_name("wren-summer") == "wren-summer"
    assert custom_server_name(f"custom:{PARENT_A}:wren-summer") == "wren-summer"
    assert custom_server_name("weknora__01M4HQ1M5K125VY381FC4ZXMSN") is None
    assert custom_server_name("") is None
