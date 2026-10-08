"""Multi-tenant / resource-ACL schema bootstrap (T2, pluggable module).

User convention 2026-09-26 (extract, don't weave): every tenant-specific
piece of the migration machinery lives here. The only upstream touch points
in ``migrate.py`` are:

* one import line re-exporting the four ``ensure_*_schema`` callables under
  their historical ``_ensure_*`` names (call sites and tests unchanged);
* the SQLite fast-path branches for versions 18/19/20 calling
  :func:`ensure_multi_tenant_schema` / :func:`ensure_resource_acl_schema`;
* the four calls at the tail of ``_run_migrations_inner`` (ordering
  comments preserved there);
* the ``run_migrations`` RLS-bypass wrapper (lazy import, stays upstream —
  it wraps the migration runner itself, not tenant schema).

Deleting this module together with those hooks restores the community
migration machinery verbatim. The helpers ``_table_exists`` /
``_ensure_column`` remain upstream property — imported lazily here so the
module never imports ``migrate`` at load time (no cycle, and the module can
be dropped without import ripples).
"""

from __future__ import annotations

import logging
from typing import Any

from octop.infra.db.pool import DatabasePool

logger = logging.getLogger(__name__)

__all__ = [
    "ensure_audit_org_schema",
    "ensure_invite_org_schema",
    "ensure_multi_tenant_schema",
    "ensure_resource_acl_schema",
]


def _table_exists(db: DatabasePool, table: str) -> bool:
    # Lazy import: migrate.py owns the helper; importing it lazily keeps this
    # module load-order independent from migrate.py (which imports us).
    from octop.infra.db.migrate import _table_exists as _upstream

    return _upstream(db, table)


def _ensure_column(db: DatabasePool, table: str, column: str, definition: str) -> bool:
    from octop.infra.db.migrate import _ensure_column as _upstream

    return _upstream(db, table, column, definition)


_MULTI_TENANT_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS orgs (
      org_id           TEXT PRIMARY KEY,
      name             TEXT NOT NULL,
      kind             TEXT NOT NULL DEFAULT 'internal',
      status           TEXT NOT NULL DEFAULT 'active',
      keycloak_org_id  TEXT,
      created_at       BIGINT NOT NULL,
      updated_at       BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS idp_org_bindings (
      idp_alias         TEXT NOT NULL,
      match_type        TEXT NOT NULL,
      match_value       TEXT NOT NULL DEFAULT '',
      org_id            TEXT NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,
      requires_approval INTEGER NOT NULL DEFAULT 0,
      note              TEXT NOT NULL DEFAULT '',
      created_at        BIGINT NOT NULL,
      updated_at        BIGINT NOT NULL,
      PRIMARY KEY (idp_alias, match_type, match_value),
      CONSTRAINT chk_idp_org_bindings_match_type
        CHECK (match_type IN ('claim', 'static', 'domain')),
      CONSTRAINT chk_idp_org_bindings_match_value CHECK (
        (match_type = 'claim' AND match_value <> '')
        OR (match_type = 'static' AND match_value = '')
        OR (match_type = 'domain' AND match_value <> '')
      )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS org_memberships (
      user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      org_id     TEXT NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,
      role_keys  TEXT NOT NULL DEFAULT '[]',
      status     TEXT NOT NULL DEFAULT 'active',
      is_primary INTEGER NOT NULL DEFAULT 0,
      source     TEXT NOT NULL DEFAULT 'jit',
      created_at BIGINT NOT NULL,
      updated_at BIGINT NOT NULL,
      PRIMARY KEY (user_id, org_id),
      CONSTRAINT chk_org_memberships_status
        CHECK (status IN ('active', 'pending', 'suspended')),
      CONSTRAINT chk_org_memberships_source
        CHECK (source IN ('jit', 'manual', 'invite'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS roles (
      role_key    TEXT PRIMARY KEY,
      org_id      TEXT REFERENCES orgs(org_id) ON DELETE CASCADE,
      name_zh     TEXT NOT NULL,
      permissions TEXT NOT NULL DEFAULT '[]',
      is_default  INTEGER NOT NULL DEFAULT 0,
      is_system   INTEGER NOT NULL DEFAULT 0,
      description TEXT NOT NULL DEFAULT '',
      -- Platform packages (org_id IS NULL) must never squat the reserved
      -- ``t_{org}_`` tenant prefix.
      CONSTRAINT chk_roles_platform_key CHECK (
        org_id IS NOT NULL OR substr(role_key, 1, 2) <> 't_'
      )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS org_role_mappings (
      org_id     TEXT NOT NULL,
      job_code   TEXT NOT NULL,
      role_key   TEXT NOT NULL REFERENCES roles(role_key) ON DELETE CASCADE,
      origin     TEXT NOT NULL DEFAULT 'manual',
      source_org TEXT,
      created_at BIGINT NOT NULL DEFAULT 0,
      PRIMARY KEY (org_id, job_code, role_key)
    )
    """,
    # 028: per-user grants made from the admin faces (member roster, user
    # membership, pending assign). Mirrors 028_member_grants; the JIT
    # re-expansion unions these with mapped keys instead of wiping them.
    """
    CREATE TABLE IF NOT EXISTS org_member_grants (
      user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      org_id     TEXT NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,
      role_key   TEXT NOT NULL REFERENCES roles(role_key) ON DELETE CASCADE,
      granted_by TEXT NOT NULL DEFAULT '',
      created_at BIGINT NOT NULL DEFAULT 0,
      PRIMARY KEY (user_id, org_id, role_key)
    )
    """,
)

# ``agents`` / ``knowledge_bases`` / ``connectors`` / ``published_experts`` are
# present in every supported install, but very old or partial ones only carry
# ``users`` -- ``_ensure_column`` would then hit a missing table, so each column
# is guarded by ``_table_exists``.
_MULTI_TENANT_COLUMNS = (
    ("users", "default_org_id", "TEXT"),
    ("users", "token_epoch", "BIGINT NOT NULL DEFAULT 0"),
    # Tenant ownership for ability packages (2026-09-30 split). NULL (the
    # ALTER default) keeps pre-existing rows platform-scoped, matching their
    # old global meaning.
    ("roles", "org_id", "TEXT REFERENCES orgs(org_id) ON DELETE CASCADE"),
    ("roles", "is_system", "INTEGER NOT NULL DEFAULT 0"),
    ("published_experts", "org_id", "TEXT"),
    ("published_experts", "allowed_roles", "TEXT NOT NULL DEFAULT '[]'"),
    ("agents", "org_id", "TEXT"),
    ("knowledge_bases", "org_id", "TEXT"),
    ("connectors", "org_id", "TEXT"),
    ("connectors", "allowed_roles", "TEXT NOT NULL DEFAULT '[]'"),
    # 018: provenance so an auto-provisioned default is distinguishable from a
    # reviewed policy, and a JIT row remembers which tenant's login created it.
    # ``'legacy'`` is reserved for rows that predate the column (see
    # ``mark_role_mappings_legacy``); the app never writes it.
    ("org_role_mappings", "origin", "TEXT NOT NULL DEFAULT 'manual'"),
    ("org_role_mappings", "source_org", "TEXT"),
    ("org_role_mappings", "created_at", "BIGINT NOT NULL DEFAULT 0"),
)

_MULTI_TENANT_INDEXES = (
    (
        "idp_org_bindings",
        "CREATE INDEX IF NOT EXISTS idx_idp_org_bindings_org ON idp_org_bindings(org_id)",
    ),
    (
        "org_memberships",
        "CREATE INDEX IF NOT EXISTS idx_org_memberships_org ON org_memberships(org_id)",
    ),
    (
        "org_memberships",
        "CREATE INDEX IF NOT EXISTS idx_org_memberships_status ON org_memberships(status)",
    ),
    (
        "org_role_mappings",
        "CREATE INDEX IF NOT EXISTS idx_org_role_mappings_job ON org_role_mappings(job_code)",
    ),
    (
        "org_role_mappings",
        "CREATE INDEX IF NOT EXISTS idx_org_role_mappings_origin ON org_role_mappings(origin)",
    ),
    (
        "org_member_grants",
        "CREATE INDEX IF NOT EXISTS idx_org_member_grants_org ON org_member_grants(org_id)",
    ),
    ("users", "CREATE INDEX IF NOT EXISTS idx_users_default_org ON users(default_org_id)"),
    ("roles", "CREATE INDEX IF NOT EXISTS idx_roles_org ON roles(org_id)"),
    (
        "published_experts",
        "CREATE INDEX IF NOT EXISTS idx_published_experts_org ON published_experts(org_id, name)",
    ),
    ("agents", "CREATE INDEX IF NOT EXISTS idx_agents_org ON agents(org_id, user_id)"),
    (
        "knowledge_bases",
        "CREATE INDEX IF NOT EXISTS idx_knowledge_bases_org "
        "ON knowledge_bases(org_id, owner_user_id)",
    ),
    ("connectors", "CREATE INDEX IF NOT EXISTS idx_connectors_org ON connectors(org_id, user_id)"),
)


# Seeded platform packages (mirrors the INSERTs at the tail of
# 018_multi_tenant): the tenant-admin marker and the JIT fallback package.
# ``ON CONFLICT DO UPDATE`` re-asserts the invariants on EVERY boot, so a
# database upgraded from a pre-is_system build (whose org_admin/member rows
# carry is_system=0 after the ALTER) is repaired on the next start instead of
# leaving them grantable by tenant admins. Display fields stay user-owned.
_ROLE_SEEDS = (
    (
        "INSERT INTO roles (role_key, org_id, name_zh, permissions, is_default,"
        " is_system, description) VALUES"
        " ('org_admin', NULL, '租户管理员', '[]', 0, 1,"
        " '平台预置：租户管理员，管理本租户成员、能力包与资源授权')"
        " ON CONFLICT (role_key) DO UPDATE SET is_system = 1, is_default = 0, org_id = NULL"
    ),
    (
        "INSERT INTO roles (role_key, org_id, name_zh, permissions, is_default,"
        " is_system, description) VALUES"
        " ('member', NULL, '成员', '[]', 1, 1,"
        " '平台预置：JIT 归位的默认最小能力包')"
        " ON CONFLICT (role_key) DO UPDATE SET is_system = 1, is_default = 1, org_id = NULL"
    ),
)


def ensure_multi_tenant_schema(db: DatabasePool) -> None:
    """Create/upgrade the multi-tenant tables, columns, and indexes.

    Idempotent on both dialects and mirrors ``016_multi_tenant.pg.sql``. It is
    the only upgrade path for SQLite, which has no
    ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``, and it also re-applies the
    shape for databases whose watermark already passed 16.
    """
    with db.connect() as conn:
        for statement in _MULTI_TENANT_TABLES:
            conn.execute(statement)
    added_provenance_column = False
    added_resource_org_columns: list[str] = []
    for table, column, definition in _MULTI_TENANT_COLUMNS:
        if not (_table_exists(db, table) and _ensure_column(db, table, column, definition)):
            continue
        if (table, column) == ("org_role_mappings", "origin"):
            # This startup is the one that upgraded a pre-018 database, so
            # whatever rows it already holds are unattributable.
            added_provenance_column = True
        elif column == "org_id" and table in _RESOURCE_ORG_OWNER_COLUMN:
            # This startup is the one that upgraded a pre-018/019 database:
            # its legacy NULL rows are exactly what the one-shot ownership
            # backfill below may attribute.
            added_resource_org_columns.append(table)
    # Seeds come after the column pass: a database upgrading from a pre-018
    # shape only gains roles.org_id/is_system right above, and the INSERTs
    # reference both.
    if _table_exists(db, "roles"):
        with db.connect() as conn:
            for statement in _ROLE_SEEDS:
                conn.execute(statement)
            _audit_platform_tenant_key_squat(conn)
    for table, statement in _MULTI_TENANT_INDEXES:
        if _table_exists(db, table):
            with db.connect() as conn:
                conn.execute(statement)
    if added_provenance_column:
        mark_role_mappings_legacy(db)
    backfill_role_mapping_created_at(db)
    # One-shot (same discipline as mark_role_mappings_legacy): attribute
    # pre-018/019 rows to their owner's default org, but only on the startup
    # that actually adds the org_id column. Re-running it on every boot would
    # overwrite org_id=NULL rows the three-state attribution model treats as
    # authoritative (NULL = deliberately unattributed, admin-only) — one
    # restart would silently widen those resources to the owner's tenant.
    backfill_resource_org_ownership(db, added_resource_org_columns)


def _audit_platform_tenant_key_squat(conn: Any) -> None:
    """Startup inspection for the CHECK the CREATE TABLE carries (P1-11).

    ``chk_roles_platform_key`` (platform rows must not squat the reserved
    ``t_`` namespace) only exists on databases created by the 018 CREATE
    TABLE — the ``_ensure_column`` upgrade path adds ``roles.org_id`` without
    the constraint, leaving the app-layer guard as the only defense there.
    SQLite cannot add a CHECK to an existing table, so both dialects get a
    portable inspection instead: violations are startup-loud errors naming
    the rows, for an operator to move or delete (no silent auto-rewrite of
    authorization data).
    """
    rows = conn.execute(
        "SELECT role_key FROM roles WHERE org_id IS NULL AND substr(role_key, 1, 2) = 't_'"
    ).fetchall()
    for row in rows:
        logger.error(
            "roles row %r is platform-owned (org_id IS NULL) but squats the "
            "reserved tenant 't_' namespace — the upgrade path carries no CHECK "
            "constraint; move or delete this row (it is invisible to tenants "
            "until resolved)",
            row["role_key"],
        )


def mark_role_mappings_legacy(db: DatabasePool) -> None:
    """Tag ``org_role_mappings`` rows that predate provenance tracking.

    Only called from the startup that *adds* the ``origin`` column, so it can
    only ever see pre-018 rows. They get ``'legacy'`` rather than the ``'manual'``
    the ``ALTER`` handed them: among them are every JIT row created before the
    column existed, and after the fact a JIT row and a hand-written one are
    indistinguishable — saying ``'manual'`` would claim a review nobody did.
    """
    with db.connect() as conn:
        conn.execute("UPDATE org_role_mappings SET origin = 'legacy'")


def backfill_role_mapping_created_at(db: DatabasePool) -> None:
    """Stamp ``created_at`` on ``org_role_mappings`` rows that predate 018.

    PostgreSQL gets this from ``018_role_mapping_provenance.pg.sql``; SQLite
    only reaches the schema through :func:`ensure_multi_tenant_schema`, so the
    same backfill has to happen here. ``origin`` is handled separately by
    :func:`mark_role_mappings_legacy`, which must run exactly once (when the
    column is created) instead of on every startup.
    """
    if not _table_exists(db, "org_role_mappings"):
        return
    now_expr = (
        "EXTRACT(EPOCH FROM now())::BIGINT"
        if db.dialect == "postgresql"
        else "CAST(strftime('%s', 'now') AS INTEGER)"
    )
    with db.connect() as conn:
        conn.execute(f"UPDATE org_role_mappings SET created_at = {now_expr} WHERE created_at = 0")


_RESOURCE_ACL_COLUMNS = (
    ("agents", "allowed_roles", "TEXT NOT NULL DEFAULT '[]'"),
    ("knowledge_bases", "allowed_roles", "TEXT NOT NULL DEFAULT '[]'"),
    ("sso_providers", "idp_alias", "TEXT NOT NULL DEFAULT ''"),
)


def ensure_resource_acl_schema(db: DatabasePool) -> None:
    """Create/upgrade the resource-ACL columns (migration 017).

    Idempotent on both dialects and mirrors ``017_resource_acl.pg.sql``. It is
    the only upgrade path for SQLite, which has no
    ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``.
    """
    for table, column, definition in _RESOURCE_ACL_COLUMNS:
        if _table_exists(db, table):
            _ensure_column(db, table, column, definition)
    # Owner-default-org backfill used to live here and run every startup; it
    # moved to :func:`backfill_resource_org_ownership` as a one-shot gated on
    # the org_id column actually being added (see ensure_multi_tenant_schema).
    # Rationale: since the three-state attribution refactor an explicit
    # org_id=NULL means *unattributed, admin-only* — re-adopting it on every
    # boot silently widened those rows to the owner's tenant.


_RESOURCE_ORG_OWNER_COLUMN = {
    "agents": "user_id",
    "knowledge_bases": "owner_user_id",
    "connectors": "user_id",
}


def backfill_resource_org_ownership(db: DatabasePool, tables: list[str]) -> None:
    """Attribute NULL-org rows to their owner's default org — exactly once.

    Mirrors the UPDATE in ``019_resource_acl.pg.sql`` (which only covered
    agents) and extends it to knowledge_bases/connectors. Subquery form works
    on both dialects and leaves org_id NULL when the owner has no default org
    — i.e. orgless admins keep publishing platform rows on purpose.

    *tables* must list only resource tables whose ``org_id`` column was added
    *this* startup (computed by :func:`ensure_multi_tenant_schema` via
    ``_ensure_column``). Rows created afterwards carry their owner's org from
    the create path, and an explicit NULL is authoritative under the
    three-state attribution model — adopting it again would be a privilege
    widening, not a heal.
    """
    for table in tables:
        owner_col = _RESOURCE_ORG_OWNER_COLUMN[table]
        if not (_table_exists(db, table) and _table_exists(db, "users")):
            continue
        with db.connect() as conn:
            conn.execute(
                f"UPDATE {table} SET org_id = ("
                "  SELECT u.default_org_id FROM users u WHERE u.id = "
                f"{table}.{owner_col})"
                f" WHERE org_id IS NULL AND {owner_col} IS NOT NULL"
            )


def ensure_audit_org_schema(db: DatabasePool) -> None:
    """Create the audit-log tenant column (migration 024, T2-6).

    Idempotent on both dialects; the only upgrade path for SQLite. The index
    and the (impossible) backfill rationale are documented in
    ``024_audit_org_id.pg.sql``.
    """
    if _table_exists(db, "audit_log"):
        _ensure_column(db, "audit_log", "org_id", "TEXT")
        with db.connect() as conn:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_log_org ON audit_log(org_id)"
            )


def ensure_invite_org_schema(db: DatabasePool) -> None:
    """Create the invite tenant column (migration 026, T4-1).

    Idempotent on both dialects; the only upgrade path for SQLite. The FK
    (PG only) and the ``NULL``-means-community behaviour are documented in
    ``026_invite_org.pg.sql``.
    """
    if _table_exists(db, "user_invites"):
        _ensure_column(db, "user_invites", "org_id", "TEXT")
