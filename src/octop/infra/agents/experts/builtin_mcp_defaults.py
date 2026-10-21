"""Builtin-expert manifest composer defaults for MCP connectors (Octop extension).

A bundled expert's ``manifest.json`` may declare composer defaults:

    "default_mcp_servers": ["wren-summer", "weknora"]

The values are **semantic references**, never per-user mount keys — mount
keys encode *who attached what* (``custom__<parent>__<server>``,
``<kind>__<instance>``), and that differs for every viewer/tenant. The
published-template flow has the same shape problem one level down (its DB
row stores the publisher's concrete keys); this module resolves the manifest
references onto the viewer's concrete mount keys at read time.

Resolution (per value, against the viewer's visible/assignable set):

1. already a visible key → keep;
2. exactly ONE visible custom mount with the same server name → rewrite
   (bare ``wren-summer`` → ``custom__<parent>__wren-summer``);
3. exactly ONE visible ``kind__<instance>`` of the same kind → rewrite
   (``weknora`` → the viewer's own ``weknora__<instance>``);
4. ambiguous or unresolvable → pass through untouched — the composer's
   template-install guard skips it with the standard warning, and the
   install path soft-filters, exactly like the published flow.

Two hook shapes are provided: :func:`detail_defaults` (unresolved refs pass
through so the guard can warn) for ``GET /experts/{id}``, and
:func:`install_defaults` (unresolved refs dropped) for the
``/agents/from-expert/{id}`` defaults fallback. Deleting this module
together with the two router hooks restores the prior behaviour (builtin
experts carry no composer defaults).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Set
from pathlib import Path
from typing import Any

from octop.infra.agents.experts.catalog import MANIFEST_FILENAME
from octop.infra.agents.experts.published_mount_names import custom_server_name

logger = logging.getLogger(__name__)

__all__ = [
    "MANIFEST_KEY",
    "manifest_default_mcp_servers",
    "visible_server_names",
    "resolve_installer_scoped",
    "filter_installer_scoped",
    "detail_defaults",
    "install_defaults",
]

MANIFEST_KEY = "default_mcp_servers"


def manifest_default_mcp_servers(expert_dir: Path) -> list[str]:
    """Tolerant read of ``default_mcp_servers`` from the expert's manifest.

    Malformed/absent values degrade to an empty list so a bad manifest never
    breaks the detail or install endpoints.
    """
    manifest_path = expert_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return []
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get(MANIFEST_KEY)
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        # Strings only: stringify-coercing junk (numbers, dicts) would leak
        # unreadable code values into the composer form.
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text and text not in out:
            out.append(text)
    return out


def visible_server_names(registry: Any, user_id: int) -> set[str]:
    """The viewer's assignable mount keys (same source as the save-time check)."""
    from octop.infra.agents.visibility import visible_mcp_servers  # noqa: PLC0415

    return {
        str(s.get("mcp_server_name"))
        for s in visible_mcp_servers(registry, user_id)
        if s.get("mcp_server_name")
    }


def _kind_part(value: str) -> str | None:
    """The kind segment of a mount key, or the value itself when bare.

    ``weknora__01ABC`` → ``weknora``; bare ``weknora`` → ``weknora``;
    custom forms (``custom__…`` / ``custom:…``) → ``None`` — those match by
    server name, not by kind.
    """
    if value.startswith("custom__") or value.startswith("custom:"):
        return None
    return value.split("__", 1)[0] or None


def _candidates(value: str, visible: Set[str]) -> list[str]:
    """Visible keys the semantic reference *value* could bind to."""
    server = custom_server_name(value)
    kind = _kind_part(value)
    out: list[str] = []
    for key in visible:
        if server is not None and custom_server_name(key) == server:
            out.append(key)
            continue
        if kind is not None:
            key_kind = _kind_part(key)
            if key_kind is not None and key_kind == kind:
                out.append(key)
    return out


def resolve_installer_scoped(values: Iterable[str], visible: Set[str] | Iterable[str]) -> list[str]:
    """Map semantic references onto the viewer's mount keys, order-preserving.

    Exact visible keys are kept; each other value is rewritten only when it
    binds to exactly ONE visible mount. Ambiguous and unresolvable values
    pass through untouched so the caller's guard/filter decides.
    """
    seen_visible = set(visible)
    out: list[str] = []
    for value in values:
        resolved = value
        if value not in seen_visible:
            candidates = _candidates(value, seen_visible)
            if len(candidates) == 1:
                resolved = candidates[0]
        if resolved not in out:
            out.append(resolved)
    return out


def filter_installer_scoped(values: Iterable[str], visible: Set[str] | Iterable[str]) -> list[str]:
    """Resolve references, then keep only keys the viewer can actually bind.

    The install-path shape: an unresolvable default must not reach
    ``validate_mcp_servers`` — a template is a suggestion, not a promise.
    """
    seen_visible = set(visible)
    resolved = resolve_installer_scoped(values, seen_visible)
    return [value for value in resolved if value in seen_visible]


def _manifest_values(catalog: Any, expert_id: str) -> list[str]:
    return manifest_default_mcp_servers(Path(catalog.expert_dir(expert_id)))


def detail_defaults(catalog: Any, expert_id: str, registry: Any, user_id: int) -> list[str]:
    """``GET /experts/{id}`` hook: manifest refs resolved for the viewer.

    Unresolvable references pass through so the composer guard warns with
    the standard 「已跳过N项资源」 instead of silently dropping them.
    """
    values = _manifest_values(catalog, expert_id)
    if not values:
        return []
    return resolve_installer_scoped(values, visible_server_names(registry, user_id))


def install_defaults(catalog: Any, expert_id: str, registry: Any, user_id: int) -> list[str]:
    """``/agents/from-expert/{id}`` hook: resolved refs, visible-only."""
    values = _manifest_values(catalog, expert_id)
    if not values:
        return []
    return filter_installer_scoped(values, visible_server_names(registry, user_id))
