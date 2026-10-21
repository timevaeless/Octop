"""Admin upload of expert packages into the built-in library root.

An expert package is a ZIP with the library directory layout at its root
(single top-level wrapper directory is tolerated):

``manifest.json`` + ``SOUL.md`` + optional ``skills/`` / ``agents/`` files.

Hard gates (2026-10-20):
- zip transport safety is shared with the SkillHub client
  (:func:`octop.infra.skills.skillhub_market.parse_zip_entries`);
- ``BOOTSTRAP.md`` is rejected anywhere in the package (PFZTFP: a shipped
  bootstrap file re-triggers first-run injection on every install);
- ``__pycache__/`` entries are stripped (they would otherwise be seeded into
  workspaces as template files);
- ``manifest.json`` must parse and carry an ``id`` used as the library
  directory name (slug-validated).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from octop.infra.agents.experts.catalog import MANIFEST_FILENAME, parse_task_examples
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.skills.skillhub_market import (
    SkillHubPackageError,
    parse_zip_entries,
)

_EXPERT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_FORBIDDEN_NAMES = {"bootstrap.md"}
_SKIPPED_DIRS = {"__pycache__"}
# Library convention: task_examples must be 3 or 6 entries per locale.
_TASK_EXAMPLE_ALLOWED_COUNTS = (3, 6)


def _validate_expert_id(expert_id: str) -> str:
    if not expert_id or not _EXPERT_ID_RE.fullmatch(expert_id) or ".." in expert_id:
        raise OctopError(
            ErrorCode.EXPERT_PACKAGE_INVALID,
            "expert package manifest.id is missing or invalid",
        )
    return expert_id


def _clean_entries(
    files: list[tuple[str, bytes]],
) -> list[tuple[str, bytes]]:
    """Drop forbidden/skipped entries; return the rest."""
    cleaned: list[tuple[str, bytes]] = []
    for name, content in files:
        parts = [part for part in name.split("/") if part]
        lowered = {part.lower() for part in parts}
        if lowered & _FORBIDDEN_NAMES:
            raise OctopError(
                ErrorCode.EXPERT_PACKAGE_INVALID,
                "expert package must not contain BOOTSTRAP.md",
            )
        if any(part in _SKIPPED_DIRS for part in parts):
            continue
        cleaned.append((name, content))
    return cleaned


def parse_expert_package(payload: bytes) -> tuple[str, list[tuple[str, bytes]]]:
    """Validate an uploaded expert package ZIP.

    Returns ``(expert_id, files)`` where *files* are workspace-relative
    payloads (manifest excluded — it is re-serialised into the target dir).
    """
    if not payload:
        raise OctopError(
            ErrorCode.EXPERT_PACKAGE_INVALID, "expert package archive is empty"
        )
    try:
        files = _clean_entries(parse_zip_entries(payload))
    except SkillHubPackageError as exc:
        raise OctopError(
            ErrorCode.EXPERT_PACKAGE_INVALID, f"invalid expert package: {exc}"
        ) from exc

    manifest_raw = next(
        (content for name, content in files if name == MANIFEST_FILENAME), None
    )
    if manifest_raw is None:
        raise OctopError(
            ErrorCode.EXPERT_PACKAGE_INVALID,
            "expert package must contain a root manifest.json",
        )
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OctopError(
            ErrorCode.EXPERT_PACKAGE_INVALID,
            f"expert package manifest.json is not valid JSON: {exc}",
        ) from exc
    if not isinstance(manifest, dict):
        raise OctopError(
            ErrorCode.EXPERT_PACKAGE_INVALID,
            "expert package manifest.json must be a JSON object",
        )
    expert_id = str(manifest.get("id") or "").strip()
    return _validate_expert_id(expert_id), files


def _package_warnings(manifest_raw: bytes) -> list[str]:
    """Best-effort convention warnings (non-blocking)."""
    warnings: list[str] = []
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return warnings
    if not isinstance(manifest, dict):
        return warnings
    try:
        examples = parse_task_examples(manifest)
    except Exception:  # pragma: no cover - warning path must never raise
        return warnings
    for locale, items in (examples or {}).items():
        if len(items) not in _TASK_EXAMPLE_ALLOWED_COUNTS:
            warnings.append(
                f"task_examples.{locale} has {len(items)} entries;"
                " library convention expects 3 or 6"
            )
    return warnings


def install_expert_package(
    catalog: object,
    payload: bytes,
    *,
    force: bool = False,
) -> dict[str, object]:
    """Unpack a validated expert package into the library root and re-scan.

    ``catalog`` is the running :class:`ExpertCatalog` — its ``root`` is the
    deployment-side library directory (bind-mounted in the harness), so
    uploads persist without an image rebuild.
    """
    expert_id, files = parse_expert_package(payload)
    root = Path(catalog.root)  # type: ignore[attr-defined]
    target = root / expert_id
    if target.exists() and not force:
        raise OctopError(
            ErrorCode.EXPERT_PACKAGE_EXISTS,
            f"expert template {expert_id!r} already exists (force=True to overwrite)",
        )
    # Stage into a sibling temp dir and swap in only after every file has
    # been written, so a mid-write failure never corrupts an existing
    # template (force=True overwrite semantics).
    staging = target.with_name(f".{expert_id}.upload-tmp")
    manifest_raw = b""
    try:
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        for name, content in files:
            dest = staging / name
            if not dest.resolve().is_relative_to(staging.resolve()):
                raise OctopError(
                    ErrorCode.EXPERT_PACKAGE_INVALID,
                    f"unsafe package path: {name}",
                )
            dest.parent.mkdir(parents=True, exist_ok=True)
            if name == MANIFEST_FILENAME:
                manifest_raw = content
            dest.write_bytes(content)
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
    except OctopError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise OctopError(
            ErrorCode.EXPERT_PACKAGE_INVALID,
            f"failed to write expert package: {exc}",
        ) from exc
    try:
        catalog.refresh()  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover - defensive
        raise OctopError(
            ErrorCode.EXPERT_PACKAGE_INVALID,
            f"library re-scan failed after upload: {exc}",
        ) from exc
    return {
        "id": expert_id,
        "files": len(files),
        "warnings": _package_warnings(manifest_raw) if manifest_raw else [],
    }


def remove_expert_package(catalog: object, expert_id: str) -> None:
    """Delete an uploaded template directory and re-scan (best-effort helper)."""
    _validate_expert_id(expert_id)
    target = Path(catalog.root) / expert_id  # type: ignore[attr-defined]
    shutil.rmtree(target, ignore_errors=True)
    catalog.refresh()  # type: ignore[attr-defined]


__all__ = [
    "install_expert_package",
    "parse_expert_package",
    "remove_expert_package",
]
