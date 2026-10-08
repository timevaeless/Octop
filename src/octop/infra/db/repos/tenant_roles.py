"""Tenant-scoped ability packages (roles) — pluggable module (T4).

User convention 2026-09-30: the ability-package plane gains tenant ownership
without weaving tenant logic into the upstream :mod:`orgs` repo. This module
owns everything tenant-admin-facing about ``roles``:

* visibility rule — a tenant sees **platform packages (org_id IS NULL) plus
  its own rows**;
* the reserved ``t_{org}_`` storage-key prefix — tenant packages are created
  through :func:`tenant_role_key` so role_key stays globally unique (the PK is
  a single column on purpose: membership JSON, resource ``allowed_roles``
  JSON, the ``rls`` claim and the hub ToolAcl snapshot all reference bare
  keys), while two tenants can freely pick the same display key;
* tenant-side mapping reads that deliberately exclude the ``'*'`` global rows
  (those stay platform-admin property).

``is_system`` rows (the seeded ``org_admin`` / ``member`` platform packages)
are managed by the platform only: tenants can neither edit, delete, nor grant
them. The upstream :class:`OrgRepo` keeps the plain CRUD; this module composes
it instead of subclassing, so deleting the module restores the old shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import map_rows
from octop.infra.db.repos.orgs import OrgRepo, OrgRoleMappingRow, RoleRow

#: Reserved storage-key namespace for tenant packages. Platform packages are
#: barred from it at both the app layer and by a CHECK constraint in 018.
TENANT_ROLE_PREFIX = "t_"

#: Display-key shape accepted from tenants (before prefixing).
_BARE_KEY_RE = re.compile(r"^[a-z0-9_]+$")


def tenant_role_key(org_id: str, bare_key: str) -> str:
    """Storage key for a tenant package: ``t_{org}_{bare}``.

    Raises ``ValueError`` when the display key is malformed — callers turn
    that into a 400/422, never a driver error. The storage key may exceed 64
    characters (``role_key`` TEXT has no schema cap); only the display key is
    bounded.
    """
    if not bare_key or len(bare_key) > 64 or not _BARE_KEY_RE.match(bare_key):
        raise ValueError(f"invalid role key: {bare_key!r}")
    return f"{TENANT_ROLE_PREFIX}{org_id}_{bare_key}"


def is_tenant_role_key(role_key: str, org_id: str) -> bool:
    """True when ``role_key`` is a storage key owned by ``org_id``."""
    return role_key.startswith(f"{TENANT_ROLE_PREFIX}{org_id}_")


@dataclass(frozen=True)
class TenantRoleView:
    """A role as the tenant plane sees it, plus who owns it."""

    role_key: str  # full storage key
    bare_key: str  # display key: prefix stripped for tenant-owned rows
    org_id: str | None  # None = platform package
    name_zh: str
    is_default: bool
    is_system: bool
    description: str

    @classmethod
    def from_row(cls, r: RoleRow) -> TenantRoleView:
        bare = r.role_key
        if r.org_id is not None and is_tenant_role_key(r.role_key, r.org_id):
            bare = r.role_key[len(TENANT_ROLE_PREFIX) + len(r.org_id) + 1 :]
        return cls(
            role_key=r.role_key,
            bare_key=bare,
            org_id=r.org_id,
            name_zh=r.name_zh,
            is_default=r.is_default,
            is_system=r.is_system,
            description=r.description,
        )


class TenantRoleRepo:
    """Tenant-side reads/writes over ``roles`` + ``org_role_mappings``.

    Composes :class:`OrgRepo` for the operations that are already org-agnostic
    (mapping writes, grants); adds the visibility-scoped role queries here so
    the upstream repo never needs to know about the prefix convention.
    """

    def __init__(self, db: DatabasePool) -> None:
        self._db = db
        self._orgs = OrgRepo(db)

    # ----- roles (visibility-scoped) -----

    def list_visible_roles(self, org_id: str) -> list[TenantRoleView]:
        """Platform packages (org_id IS NULL) plus the tenant's own rows."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM roles WHERE org_id IS NULL OR org_id = ? "
                "ORDER BY (org_id IS NULL) DESC, role_key",
                (org_id,),
            ).fetchall()
        return [TenantRoleView.from_row(r) for r in map_rows(rows, RoleRow)]

    def list_grantable_roles(self, org_id: str) -> list[TenantRoleView]:
        """Visible packages a tenant admin may hand out — system ones excluded.

        ``org_admin`` is the platform's designation channel: if a tenant admin
        could grant it, the "platform admin designates tenant admins" rule
        would be bypassable from inside the tenant.
        """
        return [r for r in self.list_visible_roles(org_id) if not r.is_system]

    def get_visible_role(self, org_id: str, role_key: str) -> TenantRoleView | None:
        row = self._orgs.get_role(role_key)
        if row is None:
            return None
        view = TenantRoleView.from_row(row)
        if view.org_id is not None and view.org_id != org_id:
            return None  # another tenant's package — does not exist here
        return view

    def create_tenant_role(
        self,
        org_id: str,
        *,
        bare_key: str,
        name_zh: str,
        is_default: bool = False,
        description: str = "",
    ) -> TenantRoleView:
        """Create a package owned by ``org_id``; ``None``-return impossible.

        Returns the created view. ``ValueError`` for a malformed key; a
        globally-clashing storage key surfaces as ``KeyError`` from the
        insert check below, which the API layer maps to 409.
        """
        storage_key = tenant_role_key(org_id, bare_key)
        if self._orgs.get_role(storage_key) is not None:
            raise KeyError(storage_key)
        self._ensure_key_not_ambiguous(org_id, storage_key)
        row = self._orgs.create_role(
            role_key=storage_key,
            name_zh=name_zh,
            org_id=org_id,
            is_default=is_default,
            description=description,
        )
        return TenantRoleView.from_row(row)

    def _ensure_key_not_ambiguous(self, org_id: str, storage_key: str) -> None:
        """Reject a composed key that also parses as another tenant's (P1-10).

        ``t_{org}_`` composition is ambiguous when org ids themselves contain
        ``_``: ``t_a_b_c`` is org ``a``/key ``b_c`` *and* org ``a_b``/key
        ``c``. If another existing org's namespace prefix matches the composed
        key, that tenant would see the package as its own (cross-tenant
        visibility) or be permanently locked out of the key (create → 409
        forever). Fail the creation with ``ValueError`` — the API layer maps
        it to 400 alongside the malformed-key case.
        """
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT org_id FROM orgs WHERE org_id != ?", (org_id,)
            ).fetchall()
        for r in rows:
            other_prefix = f"{TENANT_ROLE_PREFIX}{r['org_id']}_"
            if storage_key.startswith(other_prefix):
                raise ValueError(
                    f"role key {storage_key!r} is ambiguous: it also parses as "
                    f"tenant {r['org_id']!r}'s package namespace — pick a "
                    "different display key"
                )

    def update_tenant_role(
        self,
        org_id: str,
        role_key: str,
        *,
        name_zh: str | None = None,
        description: str | None = None,
        is_default: bool | None = None,
    ) -> TenantRoleView | None:
        """Patch a package; only tenant-owned, non-system rows qualify."""
        current = self.get_visible_role(org_id, role_key)
        if current is None or current.org_id != org_id or current.is_system:
            return None
        row = self._orgs.update_role(
            role_key, name_zh=name_zh, description=description, is_default=is_default
        )
        return TenantRoleView.from_row(row) if row else None

    def delete_tenant_role(self, org_id: str, role_key: str) -> bool:
        """Drop a tenant-owned, non-system package.

        ``org_role_mappings`` / ``org_member_grants`` reference ``roles`` with
        ``ON DELETE CASCADE``, so mapped/granted rows go with it; stale keys
        inside ``org_memberships.role_keys`` JSON are pruned on the member's
        next login (tenant_hydration). Returns ``False`` when the package
        does not exist for this tenant or is system-protected.
        """
        current = self.get_visible_role(org_id, role_key)
        if current is None or current.org_id != org_id or current.is_system:
            return False
        with self._db.transaction() as conn:
            cur = conn.execute("DELETE FROM roles WHERE role_key = ?", (role_key,))
            return int(cur.rowcount or 0) > 0

    def validate_keys_visible(self, org_id: str, keys: list[str]) -> list[str]:
        """Return the subset of ``keys`` the tenant may not reference.

        An empty result means every key resolves to a platform package or one
        owned by ``org_id``. Anything else is a tenant-isolation violation and
        the caller must reject with 400 — fail-closed.
        """
        visible = {r.role_key for r in self.list_visible_roles(org_id)}
        return [k for k in keys if k not in visible]

    # ----- org_role_mappings (tenant view) -----

    def list_tenant_mappings(
        self, org_id: str, *, job_codes: list[str] | None = None
    ) -> list[OrgRoleMappingRow]:
        """Only this tenant's ``(org, code, key)`` rows — never ``'*'``.

        Global rows are platform property; showing them here would let a
        tenant admin "edit" a policy they cannot actually change (the write
        path locks org_id to the path tenant).
        """
        params: list[object] = [org_id]
        sql = "SELECT * FROM org_role_mappings WHERE org_id = ?"
        if job_codes:
            placeholders = ", ".join("?" for _ in job_codes)
            sql += f" AND job_code IN ({placeholders})"
            params.extend(job_codes)
        sql += " ORDER BY job_code, role_key"
        with self._db.connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return map_rows(rows, OrgRoleMappingRow)


def tenant_role_repo_for(org_repo: OrgRepo) -> TenantRoleRepo:
    """Build a :class:`TenantRoleRepo` sharing ``org_repo``'s pool.

    The one sanctioned touch point between the two repos: callers that already
    hold an :class:`OrgRepo` (the JIT chain) get a tenant-scoped view over the
    same database without a second pool or DI plumbing.
    """
    return TenantRoleRepo(org_repo._db)
