"""Expert template upgrade propagation (conservative, manual-trigger only).

Pluggable module (2026-10-09 decision): installed expert instances keep
private snapshot copies of their template files, and neither re-publishing
a template nor the agent reload button ever touches those copies. This
module propagates template updates into installed instances ONLY when an
admin explicitly calls the manual endpoints, and ONLY for authoritative
template files — user-private workspace content is never written.

Conflict policy is conservative (user-approved 2026-10-09):

* fingerprint ledger ``<system-files>/expert-upgrade.json`` records, per
  authoritative relative path, the sha256 of the content last synced;
* file unchanged since last sync  → overwrite with the new template content;
* file modified since last sync   → SKIPPED and reported, never overwritten;
* no fingerprint (legacy install) → sync only when the instance content is
  byte-identical to the new template content, otherwise reported as a
  conflict (we cannot prove the owner did not customise it);
* template files absent from the instance → synced as new additions;
* template files the new template no longer ships are left untouched.

Deleting this module together with the two thin router endpoints restores
upstream behaviour verbatim.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from octop.infra.agents.experts.catalog import (
    DEFAULT_SYSTEM_FILES_PATH,
    WORKSPACE_MANIFEST_PATH,
)

__all__ = [
    "AUTHORITATIVE_FILES",
    "AUTHORITATIVE_PREFIXES",
    "FINGERPRINT_REL",
    "UpgradeReport",
    "upgrade_for_expert_id",
    "upgrade_for_template_name",
]

AUTHORITATIVE_FILES = {"SOUL.md", "BOOTSTRAP.md", "IDENTITY.md", "manifest.json"}
AUTHORITATIVE_PREFIXES = ("skills/",)
FINGERPRINT_REL = f"{DEFAULT_SYSTEM_FILES_PATH}/expert-upgrade.json"
ENV_GUARD = "OCTOP_EXPERT_UPGRADE"


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_authoritative(rel: str) -> bool:
    if rel in AUTHORITATIVE_FILES:
        return True
    return any(rel == p.rstrip("/") or rel.startswith(p) for p in AUTHORITATIVE_PREFIXES)


@dataclass
class InstanceUpgradeReport:
    agent_id: str
    name: str
    synced: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def touched(self) -> bool:
        return bool(self.synced or self.added)

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "synced": self.synced,
            "added": self.added,
            "unchanged": self.unchanged,
            "conflicts": self.conflicts,
        }


@dataclass
class UpgradeReport:
    expert_id: str
    env_disabled: bool = False
    instances: list[InstanceUpgradeReport] = field(default_factory=list)

    @property
    def touched_instances(self) -> int:
        return sum(1 for item in self.instances if item.touched)

    def as_dict(self) -> dict[str, Any]:
        return {
            "expert_id": self.expert_id,
            "env_disabled": self.env_disabled,
            "instances": [item.as_dict() for item in self.instances],
            "touched_instances": self.touched_instances,
        }


def _collect_source_pairs(source_dir: Path) -> list[tuple[str, bytes]]:
    """Snapshot the authoritative subset of the template source directory."""
    pairs: list[tuple[str, bytes]] = []
    for fpath in sorted(source_dir.rglob("*")):
        if not fpath.is_file():
            continue
        rel = fpath.relative_to(source_dir).as_posix()
        if not _is_authoritative(rel):
            continue
        # Seed semantics (catalog.seed_expert_directory): the template root
        # manifest lands at the workspace system-files welcome manifest path.
        if rel == "manifest.json":
            rel = WORKSPACE_MANIFEST_PATH
        pairs.append((rel, fpath.read_bytes()))
    return pairs


async def _load_fingerprints(workspace: Any) -> dict[str, str]:
    raw = await workspace.adownload_bytes(FINGERPRINT_REL)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


async def _store_fingerprints(workspace: Any, fingerprints: dict[str, str]) -> None:
    payload = json.dumps(fingerprints, ensure_ascii=False).encode("utf-8")
    await workspace.aupload_many([(FINGERPRINT_REL, payload)])


async def upgrade_instance(
    *,
    agent_id: str,
    name: str,
    source_dir: Path,
    workspace: Any,
) -> InstanceUpgradeReport:
    """Sync authoritative template files into one instance workspace."""
    report = InstanceUpgradeReport(agent_id=agent_id, name=name)
    fingerprints = await _load_fingerprints(workspace)
    dirty = False
    uploads: list[tuple[str, bytes]] = []

    for rel, data in _collect_source_pairs(source_dir):
        new_hash = _sha256(data)
        current = await workspace.adownload_bytes(rel)
        if current is not None and current == data:
            # Instance already matches the new template content.
            if fingerprints.get(rel) != new_hash:
                fingerprints[rel] = new_hash
                dirty = True
            report.unchanged.append(rel)
            continue
        previous = fingerprints.get(rel)
        current_hash = _sha256(current) if current is not None else None
        if previous is not None:
            if previous != current_hash:
                # Owner modified (or deleted) the file since the last sync.
                report.conflicts.append(rel)
                continue
        elif current is not None:
            # Legacy install without a fingerprint ledger: the instance
            # content differs from the template and provenance is unknown.
            report.conflicts.append(rel)
            continue
        uploads.append((rel, data))
        fingerprints[rel] = new_hash
        dirty = True
        if current is None:
            report.added.append(rel)
        else:
            report.synced.append(rel)

    if uploads:
        await workspace.aupload_many(uploads)
    if dirty:
        await _store_fingerprints(workspace, fingerprints)
    return report


def _env_disabled() -> bool:
    return os.environ.get(ENV_GUARD, "1").strip().lower() in {"0", "false", "off"}


async def _upgrade_rows(
    registry: Any,
    source_dir: Path,
    rows: list[Any],
    expert_id: str,
) -> UpgradeReport:
    report = UpgradeReport(expert_id=expert_id)
    if _env_disabled():
        report.env_disabled = True
        return report
    if not source_dir.is_dir():
        return report

    reload_ids: list[str] = []
    for row in rows:
        agent_id = str(row.agent_id)
        workspace = registry.workspace_for_agent(agent_id)
        if workspace is None:
            continue
        item = await upgrade_instance(
            agent_id=agent_id,
            name=str(getattr(row, "name", "") or ""),
            source_dir=source_dir,
            workspace=workspace,
        )
        report.instances.append(item)
        if item.touched:
            reload_ids.append(agent_id)

    reload = getattr(registry, "_reload_agents", None)
    if reload_ids and callable(reload):
        await reload(reload_ids)
    return report


def _installed_rows(server: Any, *, predicate: Any) -> list[Any]:
    repo = server.services.agent_repo
    return [row for row in repo.list_all(include_disabled=True) if predicate(row)]


async def upgrade_for_expert_id(server: Any, expert_id: str) -> UpgradeReport:
    """Upgrade instances installed from a published expert snapshot."""
    repo = server.services.published_expert_repo
    row = repo.get(expert_id)
    if row is None:
        return UpgradeReport(expert_id=expert_id)
    source_dir = Path(
        str(
            server.services.paths.published_expert_snapshot_dir(
                expert_id, org_id=getattr(row, "org_id", None)
            )
        )
    )
    installed = _installed_rows(
        server, predicate=lambda r: getattr(r, "published_expert_id", None) == expert_id
    )
    return await _upgrade_rows(
        server.app_runtime.agent_registry, source_dir, installed, expert_id
    )


async def upgrade_for_template_name(server: Any, template_name: str) -> UpgradeReport:
    """Upgrade instances installed from an expert-library template."""
    catalog = getattr(server, "expert_catalog", None)
    if catalog is None:
        return UpgradeReport(expert_id=template_name)
    source_dir = Path(str(catalog.expert_dir(template_name)))
    installed = _installed_rows(
        server, predicate=lambda r: getattr(r, "template_name", None) == template_name
    )
    return await _upgrade_rows(
        server.app_runtime.agent_registry, source_dir, installed, template_name
    )
