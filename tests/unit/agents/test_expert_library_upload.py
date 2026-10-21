"""Admin expert-package upload: POST /api/experts/library/upload."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import UploadFile

from octop.api.routers.experts import (
    delete_expert_package,
    upload_expert_package,
)
from octop.infra.agents.experts.catalog import ExpertCatalog
from octop.infra.agents.experts.library_upload import (
    install_expert_package,
    parse_expert_package,
)
from octop.infra.errors import ErrorCode, OctopError


def _zip_bytes(files: dict[str, bytes | str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _manifest(expert_id: str = "my-expert", **extra: Any) -> str:
    data = {"id": expert_id, "label": {"zh": "测试", "en": "Test"}}
    data.update(extra)
    return json.dumps(data)


def _package(expert_id: str = "my-expert", **extra: Any) -> bytes:
    return _zip_bytes(
        {
            "manifest.json": _manifest(expert_id, **extra),
            "SOUL.md": "# soul\n",
            "skills/demo/SKILL.md": "---\nname: demo\n---\nbody\n",
        }
    )


def _upload(raw: bytes, name: str = "pkg.zip") -> UploadFile:
    return UploadFile(file=io.BytesIO(raw), filename=name)


# ── parse_expert_package ──────────────────────────────────────────


def test_parse_accepts_wrapped_package_and_extracts_id() -> None:
    raw = _zip_bytes(
        {
            "my-expert/manifest.json": _manifest("my-expert"),
            "my-expert/SOUL.md": "# soul\n",
        }
    )
    expert_id, files = parse_expert_package(raw)
    assert expert_id == "my-expert"
    assert {name for name, _ in files} == {"manifest.json", "SOUL.md"}


def test_parse_rejects_missing_manifest() -> None:
    raw = _zip_bytes({"SOUL.md": "# soul\n"})
    with pytest.raises(OctopError) as exc_info:
        parse_expert_package(raw)
    assert exc_info.value.code is ErrorCode.EXPERT_PACKAGE_INVALID


def test_parse_rejects_bootstrap_md() -> None:
    raw = _zip_bytes({"manifest.json": _manifest(), "BOOTSTRAP.md": "hi\n"})
    with pytest.raises(OctopError) as exc_info:
        parse_expert_package(raw)
    assert "BOOTSTRAP" in str(exc_info.value)


def test_parse_strips_pycache_entries() -> None:
    raw = _zip_bytes(
        {
            "manifest.json": _manifest(),
            "__pycache__/x.pyc": b"\x00",
            "skills/s/__pycache__/y.pyc": b"\x00",
            "SOUL.md": "# soul\n",
        }
    )
    _expert_id, files = parse_expert_package(raw)
    assert not any("__pycache__" in name for name, _ in files)


def test_parse_accepts_finder_zipped_package_with_macos_junk() -> None:
    # Regression 2026-10-21: a Finder-created zip carries __MACOSX/ AppleDouble
    # entries whose top level defeated wrapper stripping, so the upload failed
    # with "expert package must contain a root manifest.json".
    raw = _zip_bytes(
        {
            "overseas-biz-qa/manifest.json": _manifest("overseas-biz-qa"),
            "overseas-biz-qa/SOUL.md": "# soul\n",
            "overseas-biz-qa/skills/daily-report/SKILL.md": "---\nname: daily-report\n---\nbody\n",
            "__MACOSX/._overseas-biz-qa": b"\x00\x01appledouble",
            "__MACOSX/overseas-biz-qa/._manifest.json": b"\x00\x01appledouble",
            "__MACOSX/overseas-biz-qa/._SOUL.md": b"\x00\x01appledouble",
        }
    )
    expert_id, files = parse_expert_package(raw)
    assert expert_id == "overseas-biz-qa"
    names = {name for name, _ in files}
    assert "manifest.json" in names
    assert "skills/daily-report/SKILL.md" in names
    assert not any("__MACOSX" in name for name in names)


@pytest.mark.parametrize("bad_id", ["", "../evil", "a/b", ".hidden"])
def test_parse_rejects_invalid_ids(bad_id: str) -> None:
    raw = _zip_bytes({"manifest.json": _manifest(bad_id)})
    with pytest.raises(OctopError) as exc_info:
        parse_expert_package(raw)
    assert exc_info.value.code is ErrorCode.EXPERT_PACKAGE_INVALID


# ── install_expert_package ────────────────────────────────────────


def _catalog(tmp_path: Path) -> ExpertCatalog:
    catalog = ExpertCatalog(tmp_path)
    catalog.refresh()
    return catalog


def test_install_writes_package_and_rescans(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    report = install_expert_package(catalog, _package())
    assert report["id"] == "my-expert"
    assert (tmp_path / "my-expert" / "manifest.json").is_file()
    assert (tmp_path / "my-expert" / "skills" / "demo" / "SKILL.md").is_file()
    assert catalog.get("my-expert") is not None
    assert not any(
        "__pycache__" in p for p in catalog.get("my-expert").files  # type: ignore[union-attr]
    )


def test_install_rejects_duplicate_without_force(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    install_expert_package(catalog, _package())
    with pytest.raises(OctopError) as exc_info:
        install_expert_package(catalog, _package())
    assert exc_info.value.code is ErrorCode.EXPERT_PACKAGE_EXISTS
    # force overwrites in place
    report = install_expert_package(catalog, _package(), force=True)
    assert report["id"] == "my-expert"


def test_install_surfaces_task_example_convention_warning(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    report = install_expert_package(
        catalog,
        _package(
            task_examples={"zh": ["只有一条"], "en": ["only one"]}
        ),
    )
    warnings: list[str] = report["warnings"]  # type: ignore[assignment]
    assert warnings and "task_examples.zh" in warnings[0]


def test_install_failure_keeps_existing_template_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-write failure during a force overwrite must not corrupt the
    previously installed template and must leave no staging dir behind."""
    catalog = _catalog(tmp_path)
    install_expert_package(catalog, _package())
    original_soul = (tmp_path / "my-expert" / "SOUL.md").read_bytes()

    def _boom(self: Path, _data: bytes) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(Path, "write_bytes", _boom)
    with pytest.raises(OctopError) as exc_info:
        install_expert_package(catalog, _package(), force=True)
    assert exc_info.value.code is ErrorCode.EXPERT_PACKAGE_INVALID
    monkeypatch.undo()

    # Old template untouched, no staging leftovers.
    assert (tmp_path / "my-expert" / "SOUL.md").read_bytes() == original_soul
    assert (tmp_path / "my-expert" / "manifest.json").is_file()
    assert not list(tmp_path.glob(".*.upload-tmp"))
    assert catalog.get("my-expert") is not None


# ── router endpoint gates ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_requires_admin() -> None:
    catalog = _catalog(Path("/tmp/unused"))
    server = SimpleNamespace(expert_catalog=catalog)
    with pytest.raises(OctopError) as exc_info:
        await upload_expert_package(
            file=_upload(_package()),
            force=False,
            user=SimpleNamespace(is_admin=False),
            server=server,
        )
    assert exc_info.value.code is ErrorCode.FORBIDDEN


@pytest.mark.asyncio
async def test_upload_installs_and_reports(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    server = SimpleNamespace(expert_catalog=catalog)
    result = await upload_expert_package(
        file=_upload(_package()),
        force=False,
        user=SimpleNamespace(is_admin=True),
        server=server,
    )
    assert result["id"] == "my-expert"
    assert result["files"] == 3
    assert catalog.get("my-expert") is not None


@pytest.mark.asyncio
async def test_upload_rejects_oversized_payload() -> None:
    catalog = _catalog(Path("/tmp/unused"))
    server = SimpleNamespace(expert_catalog=catalog)
    oversized = b"\x00" * (32 * 1024 * 1024 + 1)
    with pytest.raises(OctopError) as exc_info:
        await upload_expert_package(
            file=_upload(oversized),
            force=False,
            user=SimpleNamespace(is_admin=True),
            server=server,
        )
    assert exc_info.value.code is ErrorCode.EXPERT_PACKAGE_INVALID


@pytest.mark.asyncio
async def test_delete_requires_bundled_template(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    install_expert_package(catalog, _package())
    server = SimpleNamespace(expert_catalog=catalog)

    with pytest.raises(OctopError) as exc_info:
        await delete_expert_package(
            expert_id="missing", user=SimpleNamespace(is_admin=True), server=server
        )
    assert exc_info.value.code is ErrorCode.NOT_FOUND

    await delete_expert_package(
        expert_id="my-expert", user=SimpleNamespace(is_admin=True), server=server
    )
    assert not (tmp_path / "my-expert").exists()
    assert catalog.get("my-expert") is None
