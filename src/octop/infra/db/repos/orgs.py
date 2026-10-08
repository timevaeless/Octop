"""Multi-tenant organisation tables.

Covers ``orgs``, ``idp_org_bindings``, ``org_memberships``, ``roles`` and
``org_role_mappings`` — the storage half of ADR D9~D14. The decision logic
(five-level resolution chain) lives in
:mod:`octop.infra.users.org_resolution`; this module is deliberately dumb SQL.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, now_ts

# Sole-admin guard needles (upsert_membership_keeping_last_admin). The JSON
# form is a CONSTANT — no SQL wildcard chars — so the LIKE needs no escaping
# and cannot over-match: '"org_admin"' never occurs inside '"org_admin_v2"'
# (no closing quote) nor inside any other realistic key.
_ORG_ADMIN_ROLE = "org_admin"
_ORG_ADMIN_JSON = '"org_admin"'
_ORG_ADMIN_LIKE = '%"org_admin"%'


def _parse_role_keys(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "[]")
        except (ValueError, TypeError):
            return []
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    return []


@dataclass(frozen=True)
class OrgRow:
    org_id: str
    name: str
    kind: str
    status: str
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> OrgRow:
        return cls(
            org_id=str(r["org_id"]),
            name=str(r["name"]),
            kind=str(r["kind"]),
            status=str(r["status"]),
            created_at=int(r["created_at"]),
            updated_at=int(r["updated_at"]),
        )


@dataclass(frozen=True)
class IdpOrgBindingRow:
    idp_alias: str
    match_type: str  # 'claim' | 'static' | 'domain'
    match_value: str
    org_id: str
    requires_approval: bool
    note: str

    @classmethod
    def from_row(cls, r: DbRow) -> IdpOrgBindingRow:
        return cls(
            idp_alias=str(r["idp_alias"]),
            match_type=str(r["match_type"]),
            match_value=str(r["match_value"] or ""),
            org_id=str(r["org_id"]),
            requires_approval=bool(r["requires_approval"]),
            note=str(r["note"] or ""),
        )


@dataclass(frozen=True)
class OrgMembershipRow:
    user_id: int
    org_id: str
    role_keys: list[str] = field(default_factory=list)
    status: str = "active"  # 'active' | 'pending' | 'suspended'
    is_primary: bool = False
    source: str = "jit"  # 'jit' | 'manual' | 'invite'
    created_at: int = 0
    updated_at: int = 0

    @classmethod
    def from_row(cls, r: DbRow) -> OrgMembershipRow:
        return cls(
            user_id=int(r["user_id"]),
            org_id=str(r["org_id"]),
            role_keys=_parse_role_keys(r["role_keys"]),
            status=str(r["status"]),
            is_primary=bool(r["is_primary"]),
            source=str(r["source"]),
            created_at=int(r["created_at"]),
            updated_at=int(r["updated_at"]),
        )


@dataclass(frozen=True)
class RoleRow:
    role_key: str
    name_zh: str
    # NULL = platform package (seeded / admin-created); a tenant id marks a
    # tenant-owned package (storage key carries the reserved ``t_{org}_``
    # prefix, see repos.tenant_roles).
    org_id: str | None = None
    permissions: list[str] = field(default_factory=list)
    is_default: bool = False
    is_system: bool = False
    description: str = ""

    @classmethod
    def from_row(cls, r: DbRow) -> RoleRow:
        return cls(
            role_key=str(r["role_key"]),
            name_zh=str(r["name_zh"]),
            org_id=str(r["org_id"]) if r["org_id"] is not None else None,
            permissions=_parse_role_keys(r["permissions"]),
            is_default=bool(r["is_default"]),
            is_system=bool(r["is_system"]),
            description=str(r["description"] or ""),
        )


@dataclass(frozen=True)
class OrgRoleMappingRow:
    org_id: str
    job_code: str
    role_key: str
    origin: str = (
        "manual"  # 'manual' (admin / seed) | 'jit' (auto-provisioned) | 'legacy' (predates 018)
    )
    source_org: str | None = None
    created_at: int = 0

    @classmethod
    def from_row(cls, r: DbRow) -> OrgRoleMappingRow:
        return cls(
            org_id=str(r["org_id"]),
            job_code=str(r["job_code"]),
            role_key=str(r["role_key"]),
            origin=str(r["origin"] or "manual"),
            source_org=str(r["source_org"]) if r["source_org"] is not None else None,
            created_at=int(r["created_at"] or 0),
        )

    @property
    def is_jit(self) -> bool:
        """True when the row was auto-created by a first login (JIT)."""
        return self.origin == "jit"


class OrgRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    # ----- orgs -----

    def list_orgs(self) -> list[OrgRow]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM orgs ORDER BY org_id").fetchall()
        return map_rows(rows, OrgRow)

    def get_org(self, org_id: str) -> OrgRow | None:
        """Active org only — what the resolution chain is allowed to see.

        A missing *or disabled* org must not resolve (``_finish`` fails closed),
        so this predicate deliberately hides non-active rows. The admin plane
        needs the opposite view and uses :meth:`get_org_any`.
        """
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM orgs WHERE org_id = ? AND status = 'active'", (org_id,)
            ).fetchone()
        return OrgRow.from_row(r) if r else None

    def get_org_any(self, org_id: str) -> OrgRow | None:
        """Fetch an org regardless of ``status`` (admin plane).

        Without this the admin page could neither display nor re-enable a
        disabled tenant: ``get_org`` would report it as absent.
        """
        with self._db.connect() as conn:
            r = conn.execute("SELECT * FROM orgs WHERE org_id = ?", (org_id,)).fetchone()
        return OrgRow.from_row(r) if r else None

    def tenant_keyspace_squatters(self, org_id: str) -> list[str]:
        """Existing role keys that would fall inside ``t_{org_id}_`` (P1-10).

        Before creating org ``O``, any ``t_{O}_...`` key owned by ANOTHER org
        (possible only through underscore ambiguity, e.g. org ``a`` owns
        ``t_a_b_x`` and ``O = a_b``) would become visible — and manageable —
        as ``O``'s own package the moment the org exists. The caller must
        refuse the org id while squatters exist. ``substr`` comparison keeps
        it portable and immune to LIKE wildcard escaping.
        """
        prefix = f"t_{org_id}_"
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT role_key, org_id FROM roles WHERE substr(role_key, 1, ?) = ?",
                (len(prefix), prefix),
            ).fetchall()
        return [str(r["role_key"]) for r in rows if str(r["org_id"]) != org_id]

    def create_org(
        self,
        *,
        org_id: str,
        name: str,
        kind: str = "internal",
        status: str = "active",
    ) -> OrgRow | None:
        """Create one tenant; ``None`` when ``org_id`` is already taken.

        Returning ``None`` instead of raising keeps the duplicate decision in
        the API layer, where it becomes a 409 rather than a driver error.
        """
        ts = now_ts()
        with self._db.transaction() as conn:
            exists = conn.execute("SELECT 1 FROM orgs WHERE org_id = ?", (org_id,)).fetchone()
            if exists is not None:
                return None
            conn.execute(
                "INSERT INTO orgs(org_id, name, kind, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (org_id, name, kind, status, ts, ts),
            )
        return self.get_org_any(org_id)

    def update_org(
        self,
        org_id: str,
        *,
        name: str | None = None,
        kind: str | None = None,
        status: str | None = None,
    ) -> OrgRow | None:
        """Patch a tenant; ``None`` when it does not exist.

        Tri-state like :meth:`update_role`: ``None`` means "leave alone", so
        clearing a field is impossible by omission. Disabling a tenant is
        ``status='disabled'``, which makes :meth:`get_org` return ``None`` and
        therefore fails the resolution chain closed from the next login on —
        existing memberships are deliberately left untouched.
        """
        fields: list[str] = []
        params: list[object] = []
        if name is not None:
            fields.append("name = ?")
            params.append(name)
        if kind is not None:
            fields.append("kind = ?")
            params.append(kind)
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if not fields:
            return self.get_org_any(org_id)
        fields.append("updated_at = ?")
        params.append(now_ts())
        params.append(org_id)
        with self._db.transaction() as conn:
            cur = conn.execute(
                f"UPDATE orgs SET {', '.join(fields)} WHERE org_id = ?", tuple(params)
            )
            if int(cur.rowcount or 0) == 0:
                return None
        return self.get_org_any(org_id)

    def delete_org(self, org_id: str) -> bool:
        """Hard-delete a tenant; ``False`` when it did not exist.

        Both ``idp_org_bindings`` and ``org_memberships`` reference ``orgs``
        ``ON DELETE CASCADE``, so this also drops the tenant's resolution keys
        *and* every user's authorization in it. Callers must consult
        :meth:`count_org_dependents` first.
        """
        with self._db.transaction() as conn:
            cur = conn.execute("DELETE FROM orgs WHERE org_id = ?", (org_id,))
            return int(cur.rowcount or 0) > 0

    def count_org_dependents(self, org_id: str) -> tuple[int, int]:
        """``(bindings, memberships)`` that deleting ``org_id`` would cascade.

        Exists so a delete can be refused (or explicitly forced) instead of
        silently revoking access for everyone in the tenant.
        """
        with self._db.connect() as conn:
            bindings = conn.execute(
                "SELECT COUNT(*) AS n FROM idp_org_bindings WHERE org_id = ?",
                (org_id,),
            ).fetchone()
            memberships = conn.execute(
                "SELECT COUNT(*) AS n FROM org_memberships WHERE org_id = ?",
                (org_id,),
            ).fetchone()
        return int(bindings["n"]), int(memberships["n"])

    def count_memberships_by_org(self) -> dict[str, int]:
        """Membership count per tenant, for the admin listing."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT org_id, COUNT(*) AS n FROM org_memberships GROUP BY org_id"
            ).fetchall()
        return {str(r["org_id"]): int(r["n"]) for r in rows}

    def count_bindings_by_org(self) -> dict[str, int]:
        """Resolution-key count per tenant, for the admin listing."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT org_id, COUNT(*) AS n FROM idp_org_bindings GROUP BY org_id"
            ).fetchall()
        return {str(r["org_id"]): int(r["n"]) for r in rows}

    # ----- idp_org_bindings -----

    def list_bindings(self, idp_alias: str) -> list[IdpOrgBindingRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM idp_org_bindings WHERE idp_alias = ? ORDER BY match_type",
                (idp_alias,),
            ).fetchall()
        return map_rows(rows, IdpOrgBindingRow)

    def list_all_bindings(self) -> list[IdpOrgBindingRow]:
        """Every binding, across all providers — the admin listing.

        :meth:`list_bindings` is scoped to one alias because resolution only
        ever consults the provider that is logging in. The admin page has to
        show the whole table, otherwise a key belonging to another provider
        looks like it does not exist.
        """
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM idp_org_bindings ORDER BY idp_alias, match_type, match_value"
            ).fetchall()
        return map_rows(rows, IdpOrgBindingRow)

    def create_binding(
        self,
        *,
        idp_alias: str,
        match_type: str,
        match_value: str,
        org_id: str,
        requires_approval: bool = False,
        note: str = "",
    ) -> bool:
        """Insert one resolution key; ``False`` when the triple already exists.

        The ``(match_type, match_value)`` pairing is enforced by a CHECK
        constraint, so callers must validate before getting here — an invalid
        pair raises rather than returning ``False``.
        """
        ts = now_ts()
        with self._db.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM idp_org_bindings "
                "WHERE idp_alias = ? AND match_type = ? AND match_value = ?",
                (idp_alias, match_type, match_value),
            ).fetchone()
            if exists is not None:
                return False
            conn.execute(
                "INSERT INTO idp_org_bindings("
                "idp_alias, match_type, match_value, org_id, requires_approval, note, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    idp_alias,
                    match_type,
                    match_value,
                    org_id,
                    1 if requires_approval else 0,
                    note,
                    ts,
                    ts,
                ),
            )
        return True

    def delete_binding(self, idp_alias: str, match_type: str, match_value: str) -> bool:
        """Remove one resolution key; ``False`` when it was not there.

        Dropping the only key that maps a provider's users to a tenant sends
        them all to *pending* on their next login — that is the intended,
        fail-closed behaviour, not a bug.
        """
        with self._db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM idp_org_bindings "
                "WHERE idp_alias = ? AND match_type = ? AND match_value = ?",
                (idp_alias, match_type, match_value),
            )
            return int(cur.rowcount or 0) > 0

    # ----- org_memberships -----

    def get_membership(self, user_id: int, org_id: str | None) -> OrgMembershipRow | None:
        if org_id is None:
            return None
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT * FROM org_memberships WHERE user_id = ? AND org_id = ?",
                (user_id, org_id),
            ).fetchone()
        return OrgMembershipRow.from_row(r) if r else None

    def list_memberships(self, user_id: int) -> list[OrgMembershipRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM org_memberships WHERE user_id = ? ORDER BY org_id",
                (user_id,),
            ).fetchall()
        return map_rows(rows, OrgMembershipRow)

    def list_org_members(self, org_id: str) -> list[dict[str, Any]]:
        """Additive (T4-2, org_members router): one org's members with user fields.

        Joined read for the tenant admin face — the membership row stays the
        authority for roles/status/source; ``users`` contributes display
        fields and the account-level ``disabled`` flag (account suspension
        lives there, since the JIT re-expansion would otherwise resurrect a
        merely-suspended membership on the next login).
        """
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT m.user_id, m.role_keys, m.status, m.source, m.created_at, "
                "m.updated_at, u.username, u.display_name, u.email, u.disabled, "
                "u.default_org_id, u.role AS user_role "
                "FROM org_memberships m JOIN users u ON u.id = m.user_id "
                "WHERE m.org_id = ? ORDER BY m.created_at, m.user_id",
                (org_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                role_keys = list(json.loads(r["role_keys"] or "[]"))
            except (ValueError, TypeError):
                role_keys = []
            out.append(
                {
                    "user_id": int(r["user_id"]),
                    "username": str(r["username"]),
                    "display_name": r["display_name"],
                    "email": r["email"],
                    "user_role": str(r["user_role"]),
                    "disabled": bool(int(r["disabled"] or 0)),
                    "is_default_org": r["default_org_id"] == org_id,
                    "role_keys": role_keys,
                    "status": str(r["status"]),
                    "source": str(r["source"]),
                    "created_at": int(r["created_at"]),
                    "updated_at": int(r["updated_at"]),
                }
            )
        return out

    def upsert_membership(
        self,
        user_id: int,
        org_id: str,
        *,
        role_keys: list[str],
        status: str = "active",
        source: str = "jit",
    ) -> OrgMembershipRow:
        """Insert or refresh one membership row; returns the stored row.

        Role keys are replaced wholesale (the mapping table is the authority —
        every login re-expands and corrects, never merges).
        """
        ts = now_ts()
        payload = json.dumps(list(role_keys), ensure_ascii=False)
        with self._db.transaction() as conn:
            existing = conn.execute(
                "SELECT created_at FROM org_memberships WHERE user_id = ? AND org_id = ?",
                (user_id, org_id),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO org_memberships("
                    "user_id, org_id, role_keys, status, is_primary, source, "
                    "created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                    (user_id, org_id, payload, status, source, ts, ts),
                )
            else:
                conn.execute(
                    "UPDATE org_memberships "
                    "SET role_keys = ?, status = ?, updated_at = ? "
                    "WHERE user_id = ? AND org_id = ?",
                    (payload, status, ts, user_id, org_id),
                )
        row = self.get_membership(user_id, org_id)
        assert row is not None
        return row

    def upsert_membership_keeping_last_admin(
        self,
        user_id: int,
        org_id: str,
        *,
        role_keys: list[str],
        status: str = "active",
        source: str = "jit",
    ) -> OrgMembershipRow | None:
        """:meth:`upsert_membership` with the sole-admin guard INSIDE the write.

        Review 2026-10-02 P1-6: the router used to count active org_admins
        *before* the upsert — two concurrent PUTs could both pass the count
        and demote each other, locking the tenant out. Here the count and the
        write share one transaction, so the guard is authoritative: if the
        write would demote this member's ``org_admin`` while no other active
        org_admin remains, no row is touched and ``None`` is returned.
        ``"org_admin"`` is matched via a constant-quoted LIKE (the needle has
        no SQL wildcard chars), which works on both dialects without
        ``json_each``.
        """
        ts = now_ts()
        payload = json.dumps(list(role_keys), ensure_ascii=False)
        with self._db.transaction() as conn:
            existing = conn.execute(
                "SELECT created_at FROM org_memberships WHERE user_id = ? AND org_id = ?",
                (user_id, org_id),
            ).fetchone()
            demotes_self = (
                existing is not None
                and _ORG_ADMIN_ROLE not in role_keys
                and str(
                    conn.execute(
                        "SELECT role_keys FROM org_memberships WHERE user_id = ? AND org_id = ?",
                        (user_id, org_id),
                    ).fetchone()[0]
                ).find(_ORG_ADMIN_JSON)
                >= 0
            )
            if demotes_self:
                others = conn.execute(
                    "SELECT COUNT(*) FROM org_memberships m JOIN users u ON u.id = m.user_id "
                    "WHERE m.org_id = ? AND m.user_id != ? AND m.status = 'active' "
                    "AND u.disabled = 0 AND m.role_keys LIKE ?",
                    (org_id, user_id, _ORG_ADMIN_LIKE),
                ).fetchone()[0]
                if not others:
                    return None
            if existing is None:
                conn.execute(
                    "INSERT INTO org_memberships("
                    "user_id, org_id, role_keys, status, is_primary, source, "
                    "created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                    (user_id, org_id, payload, status, source, ts, ts),
                )
            else:
                conn.execute(
                    "UPDATE org_memberships "
                    "SET role_keys = ?, status = ?, updated_at = ? "
                    "WHERE user_id = ? AND org_id = ?",
                    (payload, status, ts, user_id, org_id),
                )
        row = self.get_membership(user_id, org_id)
        assert row is not None
        return row

    # ----- org_member_grants (028, additive) -----

    def list_member_grants(self, user_id: int, org_id: str) -> list[str]:
        """Per-user ability packages an admin granted (028).

        Read by the JIT re-expansion: effective keys = mapped ∪ granted, so
        an admin grant survives logins instead of being wiped by the
        mapping-table recompute. Deterministic ``(created_at, role_key)``
        ordering; rows written in one call share a timestamp.
        """
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT role_key FROM org_member_grants "
                "WHERE user_id = ? AND org_id = ? "
                "ORDER BY created_at, role_key",
                (user_id, org_id),
            ).fetchall()
        return [str(r["role_key"]) for r in rows]

    def replace_member_grants(
        self,
        user_id: int,
        org_id: str,
        role_keys: list[str],
        *,
        granted_by: str,
    ) -> None:
        """Rewrite the grant set for one (user, org) — admin writes replace.

        Called by every admin grant path (member roster PUT, user-membership
        PUT, pending assign) with the *requested* key list: keys absent from
        the list lose their grant row, so an uncheck revokes what that face
        granted earlier. Mapping-derived keys are not touched here — they
        stay mapping-revocable via the 角色映射 page.
        """
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "DELETE FROM org_member_grants WHERE user_id = ? AND org_id = ?",
                (user_id, org_id),
            )
            for role_key in dict.fromkeys(role_keys):  # dedupe, keep order
                conn.execute(
                    "INSERT INTO org_member_grants("
                    "user_id, org_id, role_key, granted_by, created_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (user_id, org_id, role_key, granted_by, ts),
                )

    # ----- roles -----

    def list_roles(self) -> list[RoleRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM roles ORDER BY (org_id IS NULL) DESC, role_key"
            ).fetchall()
        return map_rows(rows, RoleRow)

    def get_role(self, role_key: str) -> RoleRow | None:
        with self._db.connect() as conn:
            r = conn.execute("SELECT * FROM roles WHERE role_key = ?", (role_key,)).fetchone()
        return RoleRow.from_row(r) if r else None

    def create_role(
        self,
        *,
        role_key: str,
        name_zh: str,
        org_id: str | None = None,
        is_default: bool = False,
        is_system: bool = False,
        description: str = "",
    ) -> RoleRow:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO roles(role_key, org_id, name_zh, permissions, is_default, "
                "is_system, description) VALUES (?, ?, ?, '[]', ?, ?, ?) "
                "ON CONFLICT(role_key) DO NOTHING",
                (
                    role_key,
                    org_id,
                    name_zh,
                    1 if is_default else 0,
                    1 if is_system else 0,
                    description,
                ),
            )
        row = self.get_role(role_key)
        if row is None:
            raise RuntimeError(f"role insert failed: {role_key}")
        return row

    def update_role(
        self,
        role_key: str,
        *,
        name_zh: str | None = None,
        description: str | None = None,
        is_default: bool | None = None,
    ) -> RoleRow | None:
        fields: list[str] = []
        params: list[object] = []
        if name_zh is not None:
            fields.append("name_zh = ?")
            params.append(name_zh)
        if description is not None:
            fields.append("description = ?")
            params.append(description)
        if is_default is not None:
            fields.append("is_default = ?")
            params.append(1 if is_default else 0)
        if not fields:
            return self.get_role(role_key)
        params.append(role_key)
        with self._db.transaction() as conn:
            conn.execute(f"UPDATE roles SET {', '.join(fields)} WHERE role_key = ?", tuple(params))
        return self.get_role(role_key)

    # ----- org_role_mappings -----

    def add_role_mapping(
        self,
        org_id: str,
        job_code: str,
        role_key: str,
        *,
        origin: str = "manual",
        source_org: str | None = None,
    ) -> None:
        """Insert one mapping triple, ignoring a duplicate.

        ``origin``/``source_org`` are provenance only — they never change which
        tenants the row applies to (that is ``org_id``, where ``'*'`` means
        every tenant and is unioned with tenant-specific rows). They exist so
        the admin page can tell an auto-provisioned default apart from a
        reviewed policy, and so a JIT row can name the tenant whose first login
        created it instead of silently losing that context.
        """
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO org_role_mappings"
                "(org_id, job_code, role_key, origin, source_org, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(org_id, job_code, role_key) DO NOTHING",
                (org_id, job_code, role_key, origin, source_org, now_ts()),
            )

    def remove_role_mapping(self, org_id: str, job_code: str, role_key: str) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM org_role_mappings WHERE org_id = ? AND job_code = ? AND role_key = ?",
                (org_id, job_code, role_key),
            )
            return int(cur.rowcount or 0) > 0

    def set_role_mappings(
        self,
        org_id: str,
        job_code: str,
        role_keys: list[str],
        *,
        previous_org_id: str | None = None,
        previous_job_code: str | None = None,
    ) -> None:
        """Replace the whole ability-package set for one role code, atomically.

        ``(previous_org_id, previous_job_code)`` names the group being edited;
        when it differs from the target the old group is deleted in the same
        transaction, which is what makes "edit the role code itself" safe: a
        client-side delete-then-add would leave a window where the role code
        grants nothing, and a failed second call would lose the mapping.

        The target group is always cleared first — including when it is the
        same group — so renaming code A onto an existing code B replaces B's
        packages instead of silently unioning them.

        Rows written here are stamped ``origin='manual'`` with no
        ``source_org``: an admin edited this group, so any earlier "auto-created
        by tenant X" provenance is deliberately dropped rather than carried
        over. Leaving a stale ``source_org`` would make a reviewed policy look
        like an unreviewed default.
        """
        src_org = previous_org_id if previous_org_id is not None else org_id
        src_code = previous_job_code if previous_job_code is not None else job_code
        groups = {(src_org, src_code), (org_id, job_code)}
        with self._db.transaction() as conn:
            for group_org, group_code in sorted(groups):
                conn.execute(
                    "DELETE FROM org_role_mappings WHERE org_id = ? AND job_code = ?",
                    (group_org, group_code),
                )
            ts = now_ts()
            for role_key in role_keys:
                conn.execute(
                    "INSERT INTO org_role_mappings"
                    "(org_id, job_code, role_key, origin, source_org, created_at) "
                    "VALUES (?, ?, ?, 'manual', NULL, ?) "
                    "ON CONFLICT(org_id, job_code, role_key) DO NOTHING",
                    (org_id, job_code, role_key, ts),
                )

    def list_role_mappings(
        self, *, org_id: str | None = None, job_codes: list[str] | None = None
    ) -> list[OrgRoleMappingRow]:
        """Mappings for ``org_id`` (plus the ``'*'`` defaults), optionally restricted.

        ``org_id=None`` means *every* row — the admin listing. It used to be
        spelled as the same ``IN ('*', ?)`` predicate with a NULL parameter,
        which silently matched only the ``'*'`` rows (``org_id = NULL`` is
        never true), so a tenant-scoped mapping existed but could not be seen
        on the admin page. Every current row happens to be ``'*'``, which is
        why it went unnoticed.
        """
        params: list[object] = []
        if org_id is None:
            sql = "SELECT * FROM org_role_mappings WHERE 1 = 1"
        else:
            sql = "SELECT * FROM org_role_mappings WHERE org_id IN ('*', ?)"
            params.append(org_id)
        if job_codes:
            placeholders = ", ".join("?" for _ in job_codes)
            sql += f" AND job_code IN ({placeholders})"
            params.extend(job_codes)
        sql += " ORDER BY job_code, role_key"
        with self._db.connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return map_rows(rows, OrgRoleMappingRow)
