"""Published expert templates — one row per user-published expert."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, now_ts


def _parse_role_list(raw: object) -> list[str]:
    # T2 pluggable hook (octop.infra.users.resource_acl): shared ACL parser
    # (was one of four byte-identical copies woven into the repos).
    from octop.infra.users.resource_acl import parse_role_list

    return parse_role_list(raw)


def _parse_id_list(raw: object) -> list[str]:
    """Parse a JSON id-list column (agents profile style) defensively.

    Accepts a JSON string, an already-parsed list, or anything falsy; anything
    malformed degrades to an empty list rather than failing the read path.
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw if item is not None]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if isinstance(parsed, list):
            return [str(item) for item in parsed if item is not None]
    return []


@dataclass(frozen=True)
class PublishedExpertRow:
    id: str
    pk: int
    slug: str
    name: str
    description: str
    created_by: str
    source_agent_id: str | None
    icon_name: str
    color: str
    created_at: int
    updated_at: int
    # ``None`` = platform-wide template, visible to every tenant.
    org_id: str | None = None
    allowed_roles: list[str] = field(default_factory=list)
    # Publisher's composer defaults (2026-09-29): knowledge bases / MCP
    # connectors / skill packages stamped from the source agent at publish
    # time. Installs fall back to these when the installer does not pass
    # their own lists, still validated against the installer's visibility.
    default_knowledge_base_ids: list[str] = field(default_factory=list)
    default_mcp_servers: list[str] = field(default_factory=list)
    default_skill_package_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, r: DbRow) -> PublishedExpertRow:
        keys = set(r.keys())
        return cls(
            id=str(r["published_expert_id"]),
            pk=int(r["id"]),
            slug=r["slug"],
            name=r["name"],
            description=r["description"],
            created_by=r["created_by"],
            source_agent_id=r["source_agent_id"],
            icon_name=r["icon_name"],
            color=r["color"],
            created_at=int(r["created_at"]),
            updated_at=int(r["updated_at"]),
            org_id=(str(r["org_id"]) if r["org_id"] not in (None, "") else None)
            if "org_id" in keys
            else None,
            allowed_roles=_parse_role_list(r["allowed_roles"] if "allowed_roles" in keys else None),
            default_knowledge_base_ids=_parse_id_list(
                r["default_knowledge_base_ids"] if "default_knowledge_base_ids" in keys else None
            ),
            default_mcp_servers=_parse_id_list(
                r["default_mcp_servers"] if "default_mcp_servers" in keys else None
            ),
            default_skill_package_ids=_parse_id_list(
                r["default_skill_package_ids"] if "default_skill_package_ids" in keys else None
            ),
        )


class PublishedExpertRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def list_visible(
        self,
        org_id: str | None,
        role_keys: Sequence[str] | None = None,
    ) -> list[PublishedExpertRow]:
        """Templates visible from inside ``org_id`` (2026-09-27 rule).

        Own-tenant rows plus explicit platform shares (``org_id='*'``).
        Unattributed rows (``org_id IS NULL``) are admin drafts — they are
        never returned here; admins go through :meth:`list_all_orgs`.

        ``role_keys``: ability-package keys of the viewer. ``None`` is not
        used by tenant callers any more (admins take ``list_all_orgs``); a
        list applies the resource ACL — only rows whose ``allowed_roles`` is
        empty or intersects the viewer's keys survive.
        """
        from octop.infra.users.resource_acl import PLATFORM_SHARED_ORG

        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM published_experts WHERE org_id = ? OR org_id = ? ORDER BY name",
                (org_id, PLATFORM_SHARED_ORG),
            ).fetchall()
        mapped = map_rows(rows, PublishedExpertRow)
        from octop.infra.users.resource_acl import roles_allow

        return [row for row in mapped if roles_allow(row.allowed_roles, role_keys or [])]

    def list_all_orgs(self) -> list[PublishedExpertRow]:
        """Every row, across all tenants.

        Reserved for platform-admin, backup, and migration callers. It is
        deliberately named so a tenant-scoped route cannot reach it by accident.
        """
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM published_experts ORDER BY name").fetchall()
        return map_rows(rows, PublishedExpertRow)

    def get(self, expert_id: str) -> PublishedExpertRow | None:
        # Deliberately unscoped at the repo level (admin paths, slug ops);
        # tenant + role enforcement for user-facing reads happens in the
        # routers via ``can_access_resource`` (L2 of the ACL design).
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM published_experts WHERE published_expert_id = ?",
                (expert_id,),
            ).fetchone()
        return PublishedExpertRow.from_row(r) if r else None

    def get_by_slug(self, slug: str) -> PublishedExpertRow | None:
        # Intentionally NOT tenant-scoped: the slug uniqueness index is global,
        # so slug generation must see every tenant's rows to stay collision-free.
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM published_experts WHERE slug = ?",
                (slug,),
            ).fetchone()
        return PublishedExpertRow.from_row(r) if r else None

    def get_by_source_agent_id(self, source_agent_id: str) -> PublishedExpertRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM published_experts WHERE source_agent_id = ? "
                "ORDER BY created_at LIMIT 1",
                (source_agent_id,),
            ).fetchone()
        return PublishedExpertRow.from_row(r) if r else None

    def create(
        self,
        *,
        id: str,
        slug: str,
        name: str,
        description: str = "",
        created_by: str,
        source_agent_id: str | None = None,
        icon_name: str = "",
        color: str = "",
        org_id: str | None = None,
        allowed_roles: list[str] | None = None,
        default_knowledge_base_ids: list[str] | None = None,
        default_mcp_servers: list[str] | None = None,
        default_skill_package_ids: list[str] | None = None,
    ) -> PublishedExpertRow:
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO published_experts("
                "published_expert_id, slug, name, description, created_by, "
                "source_agent_id, icon_name, color, created_at, updated_at, "
                "org_id, allowed_roles, "
                "default_knowledge_base_ids, default_mcp_servers, default_skill_package_ids"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    id,
                    slug,
                    name,
                    description,
                    created_by,
                    source_agent_id,
                    icon_name,
                    color,
                    ts,
                    ts,
                    org_id,
                    json.dumps(list(allowed_roles or []), ensure_ascii=False),
                    json.dumps(list(default_knowledge_base_ids or []), ensure_ascii=False),
                    json.dumps(list(default_mcp_servers or []), ensure_ascii=False),
                    json.dumps(list(default_skill_package_ids or []), ensure_ascii=False),
                ),
            )
        row = self.get(id)
        if row is None:
            raise RuntimeError(f"published expert insert failed: {id}")
        return row

    def update_snapshot_meta(
        self,
        expert_id: str,
        *,
        icon_name: str | None = None,
        color: str | None = None,
        description: str | None = None,
        name: str | None = None,
    ) -> PublishedExpertRow:
        """Refresh listing metadata and bump ``updated_at`` after a snapshot rewrite."""
        fields: list[str] = ["updated_at = ?"]
        values: list[object] = [now_ts()]
        if icon_name is not None:
            fields.append("icon_name = ?")
            values.append(icon_name)
        if color is not None:
            fields.append("color = ?")
            values.append(color)
        if description is not None:
            fields.append("description = ?")
            values.append(description)
        if name is not None:
            fields.append("name = ?")
            values.append(name)
        values.append(expert_id)
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE published_experts SET {', '.join(fields)} WHERE published_expert_id = ?",
                tuple(values),
            )
        row = self.get(expert_id)
        if row is None:
            raise RuntimeError(f"published expert update failed: {expert_id}")
        return row

    def set_session_defaults(
        self,
        expert_id: str,
        *,
        knowledge_base_ids: list[str],
        mcp_servers: list[str],
        skill_package_ids: list[str],
    ) -> PublishedExpertRow:
        """Replace the publisher's composer defaults (publish/refresh time)."""
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE published_experts SET "
                "default_knowledge_base_ids = ?, default_mcp_servers = ?, "
                "default_skill_package_ids = ?, updated_at = ? "
                "WHERE published_expert_id = ?",
                (
                    json.dumps(list(knowledge_base_ids), ensure_ascii=False),
                    json.dumps(list(mcp_servers), ensure_ascii=False),
                    json.dumps(list(skill_package_ids), ensure_ascii=False),
                    now_ts(),
                    expert_id,
                ),
            )
            if int(cur.rowcount or 0) == 0:
                raise KeyError(f"published expert not found: {expert_id}")
        row = self.get(expert_id)
        if row is None:
            raise RuntimeError(f"published expert defaults update failed: {expert_id}")
        return row

    def set_allowed_roles(self, expert_id: str, allowed_roles: list[str]) -> PublishedExpertRow:
        """Replace the resource ACL (admin surface)."""
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE published_experts SET allowed_roles = ?, updated_at = ? "
                "WHERE published_expert_id = ?",
                (json.dumps(list(allowed_roles), ensure_ascii=False), now_ts(), expert_id),
            )
            if int(cur.rowcount or 0) == 0:
                raise KeyError(f"published expert not found: {expert_id}")
        row = self.get(expert_id)
        if row is None:
            raise RuntimeError(f"published expert acl update failed: {expert_id}")
        return row

    def set_org_id(self, expert_id: str, org_id: str | None) -> PublishedExpertRow:
        """Assign the row's attribution (admin surface; 2026-09-27).

        ``None`` = unattributed, ``'*'`` = explicit platform share, anything
        else must be an existing tenant id (validated by the router).
        """
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE published_experts SET org_id = ?, updated_at = ? "
                "WHERE published_expert_id = ?",
                (org_id, now_ts(), expert_id),
            )
            if int(cur.rowcount or 0) == 0:
                raise KeyError(f"published expert not found: {expert_id}")
        row = self.get(expert_id)
        if row is None:
            raise RuntimeError(f"published expert org update failed: {expert_id}")
        return row

    def delete(self, expert_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "DELETE FROM published_experts WHERE published_expert_id = ?",
                (expert_id,),
            )
