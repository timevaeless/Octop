"""Connector database access."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import UNSET, DbRow, bool_int, map_rows, now_ts


def _parse_role_list(raw: object) -> list[str]:
    # T2 pluggable hook (octop.infra.users.resource_acl): shared ACL parser
    # (was one of four byte-identical copies woven into the repos).
    from octop.infra.users.resource_acl import parse_role_list

    return parse_role_list(raw)


@dataclass(frozen=True)
class ConnectorRow:
    id: int
    instance_id: str
    user_id: int
    kind: str
    display_name: str
    status: str
    shared: bool
    mcp_server_name: str
    credential_blob: bytes | None
    credential_expires_at: int | None
    credential_rotated_at: int | None
    config_json: str | None
    created_at: int
    updated_at: int
    # Tenancy + resource ACL (016/017). ``org_id=None`` = platform-shared.
    org_id: str | None = None
    allowed_roles: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, r: DbRow) -> ConnectorRow:
        blob = r["credential_blob"]
        keys = set(r.keys())
        return cls(
            id=r["id"],
            instance_id=r["instance_id"],
            user_id=r["user_id"],
            kind=r["kind"],
            display_name=r["display_name"],
            status=r["status"],
            shared=bool(r["shared"]),
            mcp_server_name=r["mcp_server_name"],
            credential_blob=bytes(blob) if blob is not None else None,
            credential_expires_at=r["credential_expires_at"],
            credential_rotated_at=r["credential_rotated_at"],
            config_json=r["config_json"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            org_id=(str(r["org_id"]) if r["org_id"] not in (None, "") else None)
            if "org_id" in keys
            else None,
            allowed_roles=_parse_role_list(r["allowed_roles"] if "allowed_roles" in keys else None),
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self.credential_blob)


@dataclass(frozen=True)
class ConnectorOAuthStateRow:
    id: int
    state_id: str
    state: str
    user_id: int
    kind: str
    code_verifier: str
    redirect_after: str | None
    created_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> ConnectorOAuthStateRow:
        return cls(
            id=r["id"],
            state_id=r["state_id"],
            state=r["state"],
            user_id=r["user_id"],
            kind=r["kind"],
            code_verifier=r["code_verifier"],
            redirect_after=r["redirect_after"],
            created_at=r["created_at"],
        )


class ConnectorRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(
        self,
        *,
        instance_id: str,
        user_id: int,
        kind: str,
        display_name: str,
        mcp_server_name: str,
        config_json: str | None = None,
        shared: bool = False,
        org_id: str | None = None,
    ) -> str:
        """Create an instance row.

        ``org_id`` stamps the creator's tenant at creation time (mirrors
        ``published_experts`` publishing): once shared, the row is invisible
        outside that tenant no matter what ``allowed_roles`` says. ``None``
        means platform-shared — only an org-less admin should land here.
        """
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO connectors("
                "instance_id, user_id, kind, display_name, status, shared, mcp_server_name, "
                "config_json, org_id, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)",
                (
                    instance_id,
                    user_id,
                    kind,
                    display_name,
                    bool_int(shared),
                    mcp_server_name,
                    config_json,
                    org_id,
                    ts,
                    ts,
                ),
            )
        return instance_id

    def get(self, instance_id: str) -> ConnectorRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM connectors WHERE instance_id = ?", (instance_id,)
            ).fetchone()
        return ConnectorRow.from_row(r) if r else None

    def get_by_user_kind(self, user_id: int, kind: str) -> ConnectorRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM connectors WHERE user_id = ? AND kind = ?",
                (user_id, kind),
            ).fetchone()
        return ConnectorRow.from_row(r) if r else None

    def list_by_user_kind(self, user_id: int, kind: str) -> list[ConnectorRow]:
        """ALL rows of (user, kind) — the multi-instance custom-MCP base.

        Stable order (created_at, instance_id) keeps merged reads and label
        projections deterministic regardless of row insertion timing.
        """
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM connectors WHERE user_id = ? AND kind = ? "
                "ORDER BY created_at, instance_id",
                (user_id, kind),
            ).fetchall()
        return map_rows(rows, ConnectorRow)

    def list_by_user(self, user_id: int) -> list[ConnectorRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM connectors WHERE user_id = ? ORDER BY display_name",
                (user_id,),
            ).fetchall()
        return map_rows(rows, ConnectorRow)

    def list_visible(
        self,
        user_id: int,
        *,
        org_id: str | None = None,
        role_keys: list[str] | None = None,
    ) -> list[ConnectorRow]:
        """Connectors the user may see.

        Own instances are always visible (they hold the user's credentials).
        Shared instances are gated by tenancy + role ACL: a shared row with
        ``org_id`` set is invisible outside that tenant, and a non-empty
        ``allowed_roles`` only shows to holders of those ability packages.
        ``role_keys=None`` disables the role filter (platform-admin paths).
        """
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM connectors "
                "WHERE user_id = ? OR shared = 1 "
                "ORDER BY display_name, instance_id",
                (user_id,),
            ).fetchall()
        mapped = map_rows(rows, ConnectorRow)
        # T2 pluggable hook (octop.infra.users.resource_acl): tenancy + ACL
        # gate for shared rows. Lazy import; deleting the module restores
        # community behaviour verbatim.
        from octop.infra.users.resource_acl import visible_shared_row

        out: list[ConnectorRow] = []
        for row in mapped:
            if row.user_id == user_id:
                out.append(row)
                continue
            if visible_shared_row(
                row.org_id, row.allowed_roles, viewer_org_id=org_id, role_keys=role_keys
            ):
                out.append(row)
        return out

    def list_by_kind(self, kind: str) -> list[ConnectorRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM connectors WHERE kind = ? ORDER BY id",
                (kind,),
            ).fetchall()
        return map_rows(rows, ConnectorRow)

    def list_by_kind_prefix(self, prefix: str) -> list[ConnectorRow]:
        """Rows whose kind starts with ``prefix`` (catalog attachments use
        ``custom-mcp:{service_id}`` kinds; one prefix scan serves them all).
        """
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM connectors WHERE kind LIKE ? ORDER BY id",
                (prefix + "%",),
            ).fetchall()
        return map_rows(rows, ConnectorRow)

    def name_exists(
        self,
        user_id: int,
        display_name: str,
        *,
        exclude_instance_id: str | None = None,
    ) -> bool:
        sql = (
            "SELECT 1 FROM connectors "
            "WHERE user_id = ? AND display_name = ? AND kind <> 'custom-mcp'"
        )
        params: list[object] = [user_id, display_name]
        if exclude_instance_id is not None:
            sql += " AND instance_id <> ?"
            params.append(exclude_instance_id)
        with self._db.connect() as conn:
            return conn.execute(sql, tuple(params)).fetchone() is not None

    def update_status(self, instance_id: str, status: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE connectors SET status = ?, updated_at = ? WHERE instance_id = ?",
                (status, now_ts(), instance_id),
            )

    def update_config_json(self, instance_id: str, config_json: str | None) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE connectors SET config_json = ?, updated_at = ? WHERE instance_id = ?",
                (config_json, now_ts(), instance_id),
            )

    def update_metadata(
        self,
        instance_id: str,
        *,
        display_name: str | None = None,
        shared: bool | None = None,
        org_id: str | None | object = UNSET,
        allowed_roles: list[str] | None | object = UNSET,
    ) -> None:
        updates: list[str] = []
        params: list[object] = []
        if display_name is not None:
            updates.append("display_name = ?")
            params.append(display_name)
        if shared is not None:
            updates.append("shared = ?")
            params.append(bool_int(shared))
        if org_id is not UNSET:
            updates.append("org_id = ?")
            params.append(org_id)
        if allowed_roles is not UNSET:
            updates.append("allowed_roles = ?")
            # UNSET sentinel typing leaves this as ``object`` post-narrow.
            params.append(json.dumps(list(allowed_roles or []), ensure_ascii=False))  # type: ignore[call-overload]
        if not updates:
            return
        updates.append("updated_at = ?")
        params.extend((now_ts(), instance_id))
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE connectors SET {', '.join(updates)} WHERE instance_id = ?",
                tuple(params),
            )

    def upsert_credentials(
        self,
        *,
        instance_id: str,
        blob: bytes,
        expires_at: int | None = None,
    ) -> None:
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE connectors SET credential_blob = ?, credential_expires_at = ?, "
                "credential_rotated_at = ?, updated_at = ? WHERE instance_id = ?",
                (blob, expires_at, ts, ts, instance_id),
            )

    def delete(self, instance_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM connectors WHERE instance_id = ?", (instance_id,))

    def list_active_mcp_server_names_for_user(self, user_id: int) -> list[str]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT mcp_server_name FROM connectors "
                "WHERE user_id = ? AND status = 'active' AND credential_blob IS NOT NULL "
                "ORDER BY display_name",
                (user_id,),
            ).fetchall()
        return [str(r["mcp_server_name"]) for r in rows]

    def validate_mcp_servers_for_user(self, user_id: int, names: list[str]) -> list[str]:
        allowed = set(self.list_active_mcp_server_names_for_user(user_id))
        unknown = sorted(set(names) - allowed)
        if unknown:
            raise ValueError(f"mcp_servers not available for user: {unknown}")
        return list(names)

    def create_oauth_state(
        self,
        *,
        state_id: str,
        state: str,
        user_id: int,
        kind: str,
        code_verifier: str,
        redirect_after: str | None = None,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO connector_oauth_states("
                "state_id, state, user_id, kind, code_verifier, redirect_after, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (state_id, state, user_id, kind, code_verifier, redirect_after, now_ts()),
            )

    def consume_oauth_state(self, state: str) -> ConnectorOAuthStateRow | None:
        with self._db.transaction() as conn:
            r = conn.execute(
                "SELECT * FROM connector_oauth_states WHERE state = ?", (state,)
            ).fetchone()
            if not r:
                return None
            conn.execute("DELETE FROM connector_oauth_states WHERE state = ?", (state,))
        return ConnectorOAuthStateRow.from_row(r)

    def prune_old_oauth_states(self, older_than_ts: int) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "DELETE FROM connector_oauth_states WHERE created_at < ?", (older_than_ts,)
            )

    @staticmethod
    def parse_config_json(row: ConnectorRow) -> dict[str, object]:
        if not row.config_json:
            return {}
        try:
            data = json.loads(row.config_json)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
