"""Dumb SQL for the mcp_* tables (migration 021).

Deliberately dumb, mirroring :mod:`octop.infra.db.repos.orgs`: decision logic
(OpenAPI conversion, nacos merge strategies) lives in sibling modules.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import now_ts
from octop.mcp_catalog.models import (
    McpServiceRow,
    McpToolRow,
    PublishLogRow,
)
from octop.mcp_catalog.models import (
    _load_json_list as _parse_json_list,
)

PUBLISH_LOCK_TTL_SECONDS = 120


class ToolNameConflict(ValueError):
    """A tool name is claimed by another service (hub ACL is name-keyed)."""

_SERVICE_FIELDS = (
    "display_name",
    "description",
    "nacos_namespace",
    "nacos_group",
    "nacos_service_name",
    "backend_port",
    "openapi_url",
    "mcp_path",
    "status",
    "registry_name",
    # 033: JSON id-list string in the DB — lists are serialized here so the
    # callers (router bodies) can pass plain python lists through.
    "allowed_roles",
    # 034: three-state attribution (NULL / '*' / tenant id).
    "org_id",
)


def _service_write_values(fields: dict[str, Any]) -> dict[str, Any]:
    values = {k: fields[k] for k in _SERVICE_FIELDS if k in fields}
    roles = values.get("allowed_roles")
    if roles is not None and not isinstance(roles, str):
        values["allowed_roles"] = json.dumps(roles, ensure_ascii=False)
    return values


class CatalogRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    # ---- services ----

    def list_services(self) -> list[McpServiceRow]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM mcp_services ORDER BY service_id").fetchall()
        return [McpServiceRow.from_row(r) for r in rows]

    def count_tools_by_service(self) -> dict[str, int]:
        """Tool counts for every service in one pass (catalog list column)."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT service_id, COUNT(*) AS tool_count FROM mcp_tools GROUP BY service_id"
            ).fetchall()
        return {str(r["service_id"]): int(r["tool_count"]) for r in rows}

    def get_service(self, service_id: str) -> McpServiceRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM mcp_services WHERE service_id = ?", (service_id,)
            ).fetchone()
        return McpServiceRow.from_row(row) if row is not None else None

    def create_service(self, fields: dict[str, Any]) -> McpServiceRow:
        values = _service_write_values(fields)
        ts = now_ts()
        with self._db.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO mcp_services
                  (service_id, {", ".join(values)}, created_at, updated_at)
                VALUES (?, {", ".join("?" for _ in values)}, ?, ?)
                """,
                (fields["service_id"], *values.values(), ts, ts),
            )
        return self.get_service(fields["service_id"])  # type: ignore[return-value]

    def update_service(self, service_id: str, fields: dict[str, Any]) -> McpServiceRow | None:
        updates = _service_write_values(fields)
        if not updates:
            return self.get_service(service_id)
        sets = ", ".join(f"{k} = ?" for k in updates)
        with self._db.connect() as conn:
            conn.execute(
                f"UPDATE mcp_services SET {sets}, updated_at = ? WHERE service_id = ?",
                (*updates.values(), now_ts(), service_id),
            )
        return self.get_service(service_id)

    def delete_service(self, service_id: str) -> bool:
        with self._db.connect() as conn:
            cur = conn.execute("DELETE FROM mcp_services WHERE service_id = ?", (service_id,))
        return getattr(cur, "rowcount", 0) > 0

    # ---- publish locks (review 2.7: read-modify-write on shared nacos
    # plugin configs must be serialized; TTL guards a crashed holder) ----

    def acquire_lock(self, lock_key: str, *, ttl_seconds: int = PUBLISH_LOCK_TTL_SECONDS) -> str:
        """Acquire (or refresh) a TTL lease. Returns the holder token to pass
        to :meth:`release_lock`. Raises :class:`RuntimeError` when another
        live holder owns the lock."""
        holder = uuid.uuid4().hex
        now = now_ts()
        expires = now + ttl_seconds
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT holder, expires_at FROM mcp_locks WHERE lock_key = ?", (lock_key,)
            ).fetchone()
            if row is None:
                try:
                    conn.execute(
                        "INSERT INTO mcp_locks (lock_key, holder, expires_at) VALUES (?, ?, ?)",
                        (lock_key, holder, expires),
                    )
                    return holder
                except Exception:
                    # lost an insert race — fall through to the takeover check
                    row = conn.execute(
                        "SELECT holder, expires_at FROM mcp_locks WHERE lock_key = ?",
                        (lock_key,),
                    ).fetchone()
            if row is not None and int(row["expires_at"]) < now:
                # expired lease: atomic takeover guarded on the old expiry
                cur = conn.execute(
                    "UPDATE mcp_locks SET holder = ?, expires_at = ? "
                    "WHERE lock_key = ? AND expires_at = ?",
                    (holder, expires, lock_key, int(row["expires_at"])),
                )
                if getattr(cur, "rowcount", 0) > 0:
                    return holder
            elif row is not None and str(row["holder"]) == holder:
                conn.execute(
                    "UPDATE mcp_locks SET expires_at = ? WHERE lock_key = ?",
                    (expires, lock_key),
                )
                return holder
        raise RuntimeError(f"another publish holds the lock: {lock_key}")

    def release_lock(self, lock_key: str, holder: str) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "DELETE FROM mcp_locks WHERE lock_key = ? AND holder = ?", (lock_key, holder)
            )

    def renew_lock(
        self, lock_key: str, holder: str, *, ttl_seconds: int = PUBLISH_LOCK_TTL_SECONDS
    ) -> bool:
        """Fencing check + lease renewal in one (review 2026-09-26 P1-10).

        True when ``holder`` still owns a live lease (and its expiry just
        moved forward); False once the lease expired and was taken over —
        the caller must stop writing nacos immediately, another publisher
        is mid-flight and its merged snapshot would otherwise be clobbered.
        """
        now = now_ts()
        with self._db.connect() as conn:
            cur = conn.execute(
                "UPDATE mcp_locks SET expires_at = ? "
                "WHERE lock_key = ? AND holder = ? AND expires_at >= ?",
                (now + ttl_seconds, lock_key, holder, now),
            )
        return cur.rowcount > 0

    # ---- tools ----

    def list_tools(self, service_id: str) -> list[McpToolRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM mcp_tools WHERE service_id = ? ORDER BY tool_name",
                (service_id,),
            ).fetchall()
        return [McpToolRow.from_row(r) for r in rows]

    def get_tool(self, service_id: str, tool_name: str) -> McpToolRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM mcp_tools WHERE service_id = ? AND tool_name = ?",
                (service_id, tool_name),
            ).fetchone()
        return McpToolRow.from_row(row) if row is not None else None

    def tool_name_owner(self, tool_name: str) -> str | None:
        """Service that currently owns ``tool_name`` globally, if any.

        Companion to the cross-service uniqueness enforced in
        :meth:`upsert_tool` (review 2026-10-02 P1-3): import uses this to
        auto-rename the incoming side instead of failing the whole import.
        """
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT service_id FROM mcp_tools WHERE tool_name = ?",
                (tool_name,),
            ).fetchone()
        return row["service_id"] if row is not None else None

    def upsert_tool(self, fields: dict[str, Any]) -> McpToolRow:
        """Insert or update by (service_id, tool_name).

        Single-statement ON CONFLICT (review 3.13) — the SELECT-then-write
        version raced on the unique index under concurrent imports.

        Review 2026-10-02 P1-3: the hub ACL is a bare ``tool_name`` → roles
        projection across ALL services, so a name collision between two
        services makes "revoke role X from service A" silently open service
        B's same-named tool. The (service_id, tool_name) unique index cannot
        see it. Enforce global uniqueness at the single write choke point:
        the caller gets :class:`ToolNameConflict` (a ValueError, so existing
        handlers map it to a 400) and must rename one side.
        """
        params = json.dumps(fields.get("parameters", []), ensure_ascii=False)
        roles = json.dumps(fields.get("allowed_roles", []), ensure_ascii=False)
        enabled = 1 if fields.get("enabled", True) else 0
        source = fields.get("source", "manual")
        ts = now_ts()
        with self._db.connect() as conn:
            squatter = conn.execute(
                "SELECT service_id FROM mcp_tools WHERE tool_name = ? AND service_id != ?",
                (fields["tool_name"], fields["service_id"]),
            ).fetchone()
            if squatter is not None:
                raise ToolNameConflict(
                    f"tool name {fields['tool_name']!r} is already used by service "
                    f"{squatter['service_id']!r}; the hub ACL is keyed by bare tool "
                    "name across services — rename one side"
                )
            conn.execute(
                """
                INSERT INTO mcp_tools
                  (service_id, tool_name, description, http_method, http_path,
                   parameters_json, request_body, response_body, allowed_roles,
                   enabled, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (service_id, tool_name) DO UPDATE SET
                  description = EXCLUDED.description,
                  http_method = EXCLUDED.http_method,
                  http_path = EXCLUDED.http_path,
                  parameters_json = EXCLUDED.parameters_json,
                  request_body = EXCLUDED.request_body,
                  response_body = EXCLUDED.response_body,
                  allowed_roles = EXCLUDED.allowed_roles,
                  enabled = EXCLUDED.enabled,
                  source = EXCLUDED.source,
                  updated_at = EXCLUDED.updated_at
                """,
                (
                    fields["service_id"],
                    fields["tool_name"],
                    fields.get("description", ""),
                    fields.get("http_method", "GET"),
                    fields["http_path"],
                    params,
                    fields.get("request_body"),
                    fields.get("response_body"),
                    roles,
                    enabled,
                    source,
                    ts,
                    ts,
                ),
            )
        return self.get_tool(fields["service_id"], fields["tool_name"])  # type: ignore[return-value]

    def delete_tool(self, service_id: str, tool_name: str) -> bool:
        with self._db.connect() as conn:
            cur = conn.execute(
                "DELETE FROM mcp_tools WHERE service_id = ? AND tool_name = ?",
                (service_id, tool_name),
            )
        return getattr(cur, "rowcount", 0) > 0

    def delete_tools(self, service_id: str, tool_names: list[str]) -> list[str]:
        """Batch delete (UI multi-select). Unknown / already-gone names are
        skipped rather than failing the whole batch — the caller reports the
        names that actually went away, so a stale selection after a concurrent
        edit never surfaces as a 500."""
        deleted: list[str] = []
        with self._db.connect() as conn:
            for name in tool_names:
                cur = conn.execute(
                    "DELETE FROM mcp_tools WHERE service_id = ? AND tool_name = ?",
                    (service_id, name),
                )
                if getattr(cur, "rowcount", 0) > 0:
                    deleted.append(name)
        return sorted(deleted)

    def delete_imported_tools(self, service_id: str, keep_names: set[str]) -> list[str]:
        """Prune import-managed (``source='openapi'``) tools absent from the
        current spec (review round-2 N3). Manual tools are a first-class
        channel (design §8.3) and are never touched here."""
        deleted: list[str] = []
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT tool_name FROM mcp_tools WHERE service_id = ? AND source = 'openapi'",
                (service_id,),
            ).fetchall()
            for r in rows:
                name = str(r["tool_name"])
                if name not in keep_names:
                    conn.execute(
                        "DELETE FROM mcp_tools WHERE service_id = ? AND tool_name = ?",
                        (service_id, name),
                    )
                    deleted.append(name)
        return sorted(deleted)

    def list_enabled_tools_for_acl(
        self, exclude_service: str | None = None
    ) -> list[tuple[str, list[str], McpToolRow]]:
        """All enabled tools of published services in ONE query (review
        round-2 §5: ``_global_acl`` used to issue per-service queries — a
        concurrent edit between them produced an ACL that never existed).
        ``exclude_service`` drops one service's tools (unpublish path).
        Each triple carries the service's own default roles (033) next to the
        tool so the ACL projection can implement tool→service inheritance
        without a second query."""
        sql = (
            "SELECT s.service_id AS svc_id, s.allowed_roles AS svc_allowed_roles, t.* "
            "FROM mcp_services s "
            "JOIN mcp_tools t ON t.service_id = s.service_id "
            "WHERE s.status = 'published' AND t.enabled = 1"
        )
        params: list[Any] = []
        if exclude_service is not None:
            sql += " AND s.service_id != ?"
            params.append(exclude_service)
        with self._db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            (
                str(r["svc_id"]),
                [str(x) for x in _parse_json_list(r["svc_allowed_roles"])],
                McpToolRow.from_row(r),
            )
            for r in rows
        ]

    # ---- publish log ----

    def log_publish(
        self, service_id: str, action: str, result: str, detail: dict[str, Any], actor: str
    ) -> None:
        with self._db.connect() as conn:
            conn.execute(
                """
                INSERT INTO mcp_publish_log
                  (service_id, action, result, detail, actor, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    service_id,
                    action,
                    result,
                    json.dumps(detail, ensure_ascii=False),
                    actor,
                    now_ts(),
                ),
            )

    def list_publish_log(self, service_id: str, limit: int = 50) -> list[PublishLogRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM mcp_publish_log WHERE service_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (service_id, limit),
            ).fetchall()
        return [PublishLogRow.from_row(r) for r in rows]
