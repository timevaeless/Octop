-- Schema v18: multi-tenant ("org") planes -- SQLite mirror of
-- 018_multi_tenant.pg.sql.
--
-- SQLite has no ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``, so the actual
-- upgrade path for existing databases goes through
-- ``_ensure_multi_tenant_schema`` in migrate.py (which is idempotent on both
-- dialects). This file keeps the SQLite dialect readable next to its
-- PostgreSQL twin, matching how 005/007 are maintained.
--
-- Fresh SQLite installs reach v16 through the same helper, so the statements
-- below are the reference shape, not a second implementation.

CREATE TABLE IF NOT EXISTS orgs (
  org_id           TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  kind             TEXT NOT NULL DEFAULT 'internal',
  status           TEXT NOT NULL DEFAULT 'active',
  keycloak_org_id  TEXT,
  created_at       BIGINT NOT NULL,
  updated_at       BIGINT NOT NULL
);

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
);
CREATE INDEX IF NOT EXISTS idx_idp_org_bindings_org ON idp_org_bindings(org_id);

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
);
CREATE INDEX IF NOT EXISTS idx_org_memberships_org ON org_memberships(org_id);
CREATE INDEX IF NOT EXISTS idx_org_memberships_status ON org_memberships(status);

-- Tenant-scoped ability packages (see the PostgreSQL twin for the full
-- rationale). org_id NULL = platform package; tenant packages carry the
-- reserved ``t_{org}_`` key prefix, keeping role_key globally unique.
CREATE TABLE IF NOT EXISTS roles (
  role_key    TEXT PRIMARY KEY,
  org_id      TEXT REFERENCES orgs(org_id) ON DELETE CASCADE,
  name_zh     TEXT NOT NULL,
  permissions TEXT NOT NULL DEFAULT '[]',
  is_default  INTEGER NOT NULL DEFAULT 0,
  is_system   INTEGER NOT NULL DEFAULT 0,
  description TEXT NOT NULL DEFAULT '',
  CONSTRAINT chk_roles_platform_key CHECK (
    org_id IS NOT NULL OR substr(role_key, 1, 2) <> 't_'
  )
);
CREATE INDEX IF NOT EXISTS idx_roles_org ON roles(org_id);

INSERT OR IGNORE INTO roles (role_key, org_id, name_zh, permissions, is_default, is_system, description)
VALUES ('org_admin', NULL, '租户管理员', '[]', 0, 1,
        '平台预置：租户管理员，管理本租户成员、能力包与资源授权');

INSERT OR IGNORE INTO roles (role_key, org_id, name_zh, permissions, is_default, is_system, description)
VALUES ('member', NULL, '成员', '[]', 1, 1,
        '平台预置：JIT 归位的默认最小能力包');

CREATE TABLE IF NOT EXISTS org_role_mappings (
  org_id   TEXT NOT NULL,
  job_code TEXT NOT NULL,
  role_key TEXT NOT NULL REFERENCES roles(role_key) ON DELETE CASCADE,
  PRIMARY KEY (org_id, job_code, role_key)
);
CREATE INDEX IF NOT EXISTS idx_org_role_mappings_job ON org_role_mappings(job_code);

-- Column additions are applied by ``_ensure_multi_tenant_schema``:
--   users.default_org_id, users.token_epoch,
--   published_experts.org_id, published_experts.allowed_roles,
--   agents.org_id, knowledge_bases.org_id,
--   connectors.org_id, connectors.allowed_roles

UPDATE _schema_version SET version = 18;
