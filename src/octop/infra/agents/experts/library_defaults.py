"""Admin UI editing of builtin-expert MCP composer defaults (Octop extension).

Builtin expert packages live on the library filesystem (the deployment-side
directory is bind-mounted, so edits persist without an image rebuild) — there
is no DB table behind them. This module backs the admin "default connectors"
editor: it reads/writes the ``default_mcp_servers`` key of a package's
``manifest.json`` and builds the picker options from the admin's own
assignable set.

Stored values are **semantic references**, never per-user mount keys
(see :mod:`octop.infra.agents.experts.builtin_mcp_defaults` for the read-time
resolution). A picker built on ``/connector-instances/assignable`` yields
mount keys (``custom__<parent>__<server>``, ``<kind>__<instance>``), so
:func:`normalize_semantic_refs` strips them back to the stable part before
persisting — instance ids must never reach the manifest. An empty list
*removes* the key so uploaded manifests stay clean.

Deleting this module together with the two router hooks restores the prior
behaviour (defaults could then only be edited by hand in the manifest).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from octop.infra.agents.experts.builtin_mcp_defaults import (
    MANIFEST_KEY,
    manifest_default_mcp_servers,
)
from octop.infra.agents.experts.published_mount_names import custom_server_name
from octop.infra.errors import ErrorCode, OctopError

__all__ = [
    "MAX_DEFAULT_MCP_SERVERS",
    "library_expert_dir",
    "normalize_semantic_ref",
    "normalize_semantic_refs",
    "read_defaults",
    "write_defaults",
    "picker_options",
    "defaults_payload",
]

_MAX_DEFAULT_MCP_SERVERS = 16
_MAX_REF_LENGTH = 200
MAX_DEFAULT_MCP_SERVERS = _MAX_DEFAULT_MCP_SERVERS

_ID_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def library_expert_dir(catalog: Any, expert_id: str) -> Path:
    """The library directory of *expert_id*, path-traversal safe.

    Must live directly under the catalog's primary root (the bind-mounted
    library), and its ``manifest.json`` must exist — packages without one are
    not editable templates.
    """
    if not expert_id or not _ID_PART_RE.fullmatch(expert_id) or ".." in expert_id:
        raise OctopError(ErrorCode.NOT_FOUND, f"expert {expert_id!r} not found")
    root = Path(catalog.root).resolve()
    target = (root / expert_id).resolve()
    if target.parent != root or not target.is_dir():
        raise OctopError(ErrorCode.NOT_FOUND, f"expert {expert_id!r} not found")
    if not (target / "manifest.json").is_file():
        raise OctopError(
            ErrorCode.EXPERT_PACKAGE_INVALID,
            f"expert {expert_id!r} has no manifest.json to edit",
        )
    return target


def normalize_semantic_ref(value: Any) -> str:
    """Reduce a picked mount key (or already-semantic ref) to its stable part.

    ``custom__<parent>__<server>`` → ``<server>``; ``<kind>__<instance>`` →
    ``<kind>``; bare values pass through. Instance ids — which differ per
    user and die on re-import — must never be persisted. Non-string values
    (junk from a malformed client) are dropped, mirroring the tolerant
    manifest reader.
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    server = custom_server_name(text)
    if server is not None:
        return server
    return text.split("__", 1)[0]


def normalize_semantic_refs(values: Iterable[Any]) -> list[str]:
    """Sanitize a picker payload into the persisted form (ordered, deduped)."""
    out: list[str] = []
    for value in values:
        text = normalize_semantic_ref(value)
        if not text or len(text) > _MAX_REF_LENGTH or text in out:
            continue
        out.append(text)
        if len(out) >= _MAX_DEFAULT_MCP_SERVERS:
            break
    return out


def _read_manifest(expert_dir: Path) -> dict[str, Any]:
    try:
        raw = (expert_dir / "manifest.json").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise OctopError(
            ErrorCode.EXPERT_PACKAGE_INVALID,
            f"manifest.json is not readable: {exc}",
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OctopError(
            ErrorCode.EXPERT_PACKAGE_INVALID,
            f"manifest.json is not valid JSON: {exc}",
        ) from exc
    if not isinstance(data, dict):
        raise OctopError(ErrorCode.EXPERT_PACKAGE_INVALID, "manifest.json must be a JSON object")
    return data


def read_defaults(catalog: Any, expert_id: str) -> list[str]:
    """Current persisted semantic refs (tolerant, same reader as resolution)."""
    return manifest_default_mcp_servers(library_expert_dir(catalog, expert_id))


def write_defaults(catalog: Any, expert_id: str, values: Iterable[Any]) -> list[str]:
    """Persist normalized refs into the manifest (atomic) and return them.

    Sibling-temp + ``os.replace`` — a mid-write failure never corrupts the
    package (same discipline as :func:`install_expert_package`). An empty
    list removes the key entirely. The caller is responsible for the admin
    gate; a ``catalog.refresh()`` afterwards makes nothing stale (defaults
    are read lazily from disk at detail/install time), but keeps the market
    metadata view in sync if the manifest changed shape.
    """
    expert_dir = library_expert_dir(catalog, expert_id)
    if not isinstance(values, (list, tuple)):
        raise OctopError(
            ErrorCode.EXPERT_PACKAGE_INVALID,
            "default_mcp_servers must be a list of strings",
        )
    normalized = normalize_semantic_refs(values)
    data = _read_manifest(expert_dir)
    manifest_path = expert_dir / "manifest.json"
    # Stage OUTSIDE the expert dir: discover_seed_paths() packages every file
    # under the expert dir (dotfiles included), so a temp file crashing
    # mid-write there would be seeded into fresh workspaces. The library root
    # scan only recurses into package directories — a stray file beside them
    # is invisible to the catalog.
    staging = expert_dir.with_name(f".{expert_id}.defaults-tmp-{os.getpid()}")
    try:
        if normalized:
            data[MANIFEST_KEY] = normalized
        else:
            data.pop(MANIFEST_KEY, None)
        staging.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(staging, manifest_path)
    except OSError as exc:
        raise OctopError(
            ErrorCode.EXPERT_PACKAGE_INVALID,
            f"failed to write manifest.json: {exc}",
        ) from exc
    finally:
        staging.unlink(missing_ok=True)
    try:
        catalog.refresh()
    except Exception as exc:  # pragma: no cover - defensive, write already durable
        raise OctopError(
            ErrorCode.EXPERT_PACKAGE_INVALID,
            f"library re-scan failed after defaults update: {exc}",
        ) from exc
    return normalized


def picker_options(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assignable connector rows rendered as semantic picker options.

    ``rows`` is the same listing the save-time check validates against
    (``visible_mcp_servers``); collapse each mount key to its semantic ref —
    two mounts resolving to the same ref become one option, keeping the
    first display label.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        value = normalize_semantic_ref(row.get("mcp_server_name"))
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(
            {
                "value": value,
                "label": str(row.get("display_name") or value),
                "kind": str(row.get("kind") or ""),
            }
        )
    return out


def defaults_payload(
    catalog: Any, rows: Iterable[dict[str, Any]], expert_id: str
) -> dict[str, Any]:
    """GET/PUT response shape: persisted refs + this admin's picker options."""
    return {
        "expert_id": expert_id,
        "default_mcp_servers": read_defaults(catalog, expert_id),
        "options": picker_options(rows),
    }
