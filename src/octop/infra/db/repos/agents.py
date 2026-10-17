"""Agent table access."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import (
    UNSET,
    DbRow,
    bool_int,
    map_rows,
    now_ts,
    optional_updates,
    # (roles_allow moved to octop.infra.users.resource_acl — T2 pluggable hook)
)


def _unix_ts(value: object) -> int:
    """Coerce a DB timestamp to unix seconds.

    ``agents.created_at`` / ``updated_at`` are INTEGER, but SQLite will store
    a text datetime if one was written. Listing must not crash on that.
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _opt_str(r: DbRow, key: str) -> str | None:
    try:
        value = r[key]
    except (KeyError, IndexError):
        return None
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _parse_role_list(raw: object) -> list[str]:
    # T2 pluggable hook (octop.infra.users.resource_acl): shared ACL parser
    # (was one of four byte-identical copies woven into the repos).
    from octop.infra.users.resource_acl import parse_role_list

    return parse_role_list(raw)


@dataclass(frozen=True)
class AgentRow:
    id: int
    agent_id: str
    user_id: int | None
    name: str
    description: str | None
    persona_mbti: str | None
    default_model: str | None
    system_prompt: str | None
    enabled: int
    config_json: str | None
    last_state: str | None
    last_error: str | None
    created_at: int
    updated_at: int
    icon: str | None = None
    template_name: str | None = None
    is_shared: int = 0
    color: str | None = None
    icon_name: str | None = None
    icon_url: str | None = None
    skill_package_ids: str | None = None
    published_expert_id: str | None = None
    welcome_message: str | None = None
    knowledge_base_ids: str | None = None
    mcp_servers: str | None = None
    # Tenancy + resource ACL (016/017). ``org_id=None`` = platform-wide.
    org_id: str | None = None
    allowed_roles: list[str] = field(default_factory=list)
    kind: str = "expert"

    @classmethod
    def from_row(cls, r: DbRow) -> AgentRow:
        try:
            is_shared = int(r["is_shared"])
        except KeyError:
            is_shared = 0
        keys = set(r.keys())
        try:
            kind_raw = r["kind"]
        except (KeyError, IndexError):
            kind_raw = None
        kind = str(kind_raw).strip() if kind_raw else "expert"
        if kind not in {"expert", "team"}:
            kind = "expert"
        return cls(
            id=r["id"],
            agent_id=r["agent_id"],
            user_id=r["user_id"],
            name=r["name"],
            description=r["description"],
            persona_mbti=r["persona_mbti"],
            default_model=r["default_model"],
            system_prompt=r["system_prompt"],
            enabled=r["enabled"],
            config_json=r["config_json"],
            last_state=r["last_state"],
            last_error=r["last_error"],
            created_at=_unix_ts(r["created_at"]),
            updated_at=_unix_ts(r["updated_at"]),
            icon=r["icon"],
            template_name=r["template_name"],
            is_shared=is_shared,
            color=_opt_str(r, "color"),
            icon_name=_opt_str(r, "icon_name"),
            icon_url=_opt_str(r, "icon_url"),
            skill_package_ids=_opt_str(r, "skill_package_ids"),
            published_expert_id=_opt_str(r, "published_expert_id"),
            welcome_message=_opt_str(r, "welcome_message"),
            knowledge_base_ids=_opt_str(r, "knowledge_base_ids"),
            mcp_servers=_opt_str(r, "mcp_servers"),
            org_id=_opt_str(r, "org_id") if "org_id" in keys else None,
            allowed_roles=_parse_role_list(r["allowed_roles"] if "allowed_roles" in keys else None),
            kind=kind,
        )


class AgentRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(
        self,
        *,
        agent_id: str,
        user_id: int | None,
        name: str,
        description: str | None = None,
        persona_mbti: str | None = None,
        default_model: str | None = None,
        system_prompt: str | None = None,
        config_json: str | None = None,
        icon: str | None = None,
        template_name: str | None = None,
        color: str | None = None,
        icon_name: str | None = None,
        icon_url: str | None = None,
        skill_package_ids: str | None = None,
        published_expert_id: str | None = None,
        welcome_message: str | None = None,
        knowledge_base_ids: str | None = None,
        mcp_servers: str | None = None,
        org_id: str | None = None,
        kind: str = "expert",
    ) -> str:
        """Create an agent row.

        ``org_id`` stamps the creator's tenant at creation time; once the
        agent is shared, that tenant is the hard visibility boundary
        (``list_shared``) and the by-id access gate. ``None`` means
        platform-shared — share-time stamping can still scope it later.
        """
        ts = now_ts()
        agent_kind = kind if kind in {"expert", "team"} else "expert"
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO agents(agent_id, user_id, name, description, "
                "persona_mbti, default_model, system_prompt, enabled, config_json, icon, "
                "template_name, color, icon_name, icon_url, skill_package_ids, "
                "published_expert_id, welcome_message, knowledge_base_ids, mcp_servers, "
                "org_id, kind, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    agent_id,
                    user_id,
                    name,
                    description,
                    persona_mbti,
                    default_model,
                    system_prompt,
                    config_json,
                    icon,
                    template_name,
                    color,
                    icon_name,
                    icon_url,
                    skill_package_ids,
                    published_expert_id,
                    welcome_message,
                    knowledge_base_ids,
                    mcp_servers,
                    org_id,
                    agent_kind,
                    ts,
                    ts,
                ),
            )
        return agent_id

    def get(self, agent_id: str) -> AgentRow | None:
        with self._db.connect() as conn:
            r = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        return AgentRow.from_row(r) if r else None

    def list_by_user(self, user_id: int, *, include_disabled: bool = True) -> list[AgentRow]:
        sql = "SELECT * FROM agents WHERE user_id = ?"
        if not include_disabled:
            sql += " AND enabled = 1"
        sql += " ORDER BY created_at ASC, id ASC"
        with self._db.connect() as conn:
            rows = conn.execute(sql, (user_id,)).fetchall()
        return map_rows(rows, AgentRow)

    def count_by_user(
        self,
        user_id: int,
        *,
        kind: str | None = None,
        include_disabled: bool = True,
    ) -> int:
        sql = "SELECT COUNT(*) AS c FROM agents WHERE user_id = ?"
        params: list[object] = [user_id]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        if not include_disabled:
            sql += " AND enabled = 1"
        with self._db.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["c"]) if row is not None else 0

    def list_all(self, *, include_disabled: bool = True) -> list[AgentRow]:
        sql = "SELECT * FROM agents"
        if not include_disabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY created_at ASC, id ASC"
        with self._db.connect() as conn:
            rows = conn.execute(sql).fetchall()
        return map_rows(rows, AgentRow)

    def set_enabled(self, agent_id: str, enabled: bool) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE agents SET enabled = ?, updated_at = ? WHERE agent_id = ?",
                (bool_int(enabled), now_ts(), agent_id),
            )

    def set_shared(self, agent_id: str, shared: bool) -> None:
        """Persist whether other users may access this agent.

        2026-09-27: share-time tenant stamping retired — attribution lives on
        the row (creation stamp / resource-acl assignment) and is the sole
        authority; the old ``COALESCE(org_id, sharer)`` hook would silently
        re-assign unattributed admin drafts into the sharer's tenant.
        """
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE agents SET is_shared = ?, updated_at = ? WHERE agent_id = ?",
                (bool_int(shared), now_ts(), agent_id),
            )

    def list_shared(
        self,
        *,
        exclude_user_id: int | None = None,
        org_id: str | None = None,
        role_keys: Sequence[str] | None = None,
    ) -> list[AgentRow]:
        """Shared agents visible to the viewer.

        Historically this returned EVERY shared agent platform-wide — a
        structural cross-tenant leak. Tenancy is now the hard boundary: a
        shared row with ``org_id`` set is invisible outside that tenant, and
        ``role_keys`` (when not ``None``) additionally applies the resource
        ACL. ``role_keys=None`` disables the role filter (admin paths).
        """
        sql = "SELECT * FROM agents WHERE is_shared = 1 AND enabled = 1"
        params: list[object] = []
        if exclude_user_id is not None:
            sql += " AND user_id != ?"
            params.append(exclude_user_id)
        sql += " ORDER BY created_at ASC, id ASC"
        with self._db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        mapped = map_rows(rows, AgentRow)
        # T2 pluggable hook (octop.infra.users.resource_acl): tenancy + ACL
        # gate for shared rows. Lazy import; deleting the module restores
        # community behaviour verbatim (i.e. re-opens the cross-tenant leak).
        from octop.infra.users.resource_acl import visible_shared_row

        return [
            row
            for row in mapped
            if visible_shared_row(
                row.org_id, row.allowed_roles, viewer_org_id=org_id, role_keys=role_keys
            )
        ]

    def set_allowed_roles(self, agent_id: str, allowed_roles: list[str]) -> None:
        """Replace the shared-agent resource ACL (admin surface)."""
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE agents SET allowed_roles = ?, updated_at = ? WHERE agent_id = ?",
                (json.dumps(list(allowed_roles), ensure_ascii=False), now_ts(), agent_id),
            )
            if int(cur.rowcount or 0) == 0:
                raise KeyError(f"agent not found: {agent_id}")

    def set_org_id(self, agent_id: str, org_id: str | None) -> None:
        """Assign the row's attribution (admin surface; 2026-09-27).

        ``None`` = unattributed, ``'*'`` = explicit platform share, anything
        else must be an existing tenant id (validated by the router).
        """
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE agents SET org_id = ?, updated_at = ? WHERE agent_id = ?",
                (org_id, now_ts(), agent_id),
            )
            if int(cur.rowcount or 0) == 0:
                raise KeyError(f"agent not found: {agent_id}")

    def set_state(self, agent_id: str, state: str, *, error: str | None = None) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE agents SET last_state = ?, last_error = ?, updated_at = ? "
                "WHERE agent_id = ?",
                (state, error, now_ts(), agent_id),
            )

    def update_config(
        self,
        agent_id: str,
        *,
        name: str | None | object = UNSET,
        description: str | None | object = UNSET,
        persona_mbti: str | None | object = UNSET,
        default_model: str | None | object = UNSET,
        system_prompt: str | None | object = UNSET,
        config_json: str | None | object = UNSET,
        icon: str | None | object = UNSET,
        template_name: str | None | object = UNSET,
        color: str | None | object = UNSET,
        icon_name: str | None | object = UNSET,
        icon_url: str | None | object = UNSET,
        skill_package_ids: str | None | object = UNSET,
        published_expert_id: str | None | object = UNSET,
        welcome_message: str | None | object = UNSET,
        knowledge_base_ids: str | None | object = UNSET,
        mcp_servers: str | None | object = UNSET,
    ) -> None:
        fields, params = optional_updates(
            [
                ("name", name),
                ("description", description),
                ("persona_mbti", persona_mbti),
                ("default_model", default_model),
                ("system_prompt", system_prompt),
                ("config_json", config_json),
                ("icon", icon),
                ("template_name", template_name),
                ("color", color),
                ("icon_name", icon_name),
                ("icon_url", icon_url),
                ("skill_package_ids", skill_package_ids),
                ("published_expert_id", published_expert_id),
                ("welcome_message", welcome_message),
                ("knowledge_base_ids", knowledge_base_ids),
                ("mcp_servers", mcp_servers),
            ]
        )
        if not fields:
            return
        fields.append("updated_at = ?")
        params.append(now_ts())
        params.append(agent_id)
        with self._db.transaction() as conn:
            conn.execute(f"UPDATE agents SET {', '.join(fields)} WHERE agent_id = ?", params)

    def delete(self, agent_id: str) -> None:
        with self._db.transaction() as conn:
            # No foreign key. Thread and usage rows cascade from ``agents``.
            conn.execute("DELETE FROM care_push_records WHERE agent_id = ?", (agent_id,))
            conn.execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,))
