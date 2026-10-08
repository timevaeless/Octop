-- Schema v18: multi-tenant ("org") planes.
--
-- Adds five tables -- orgs / idp_org_bindings / org_memberships / roles /
-- org_role_mappings -- plus the per-resource org_id column and the per-user
-- session pointers (default_org_id / token_epoch).
--
-- Decisions behind this shape live in the harness repo:
--   ARCHITECTURE.md ADR D12 (naming: org_id) and ADR D14 (tenant resolution is
--   a platform capability, never bound to an upstream field).
--
-- Conventions that must not drift:
--   * org_id is TEXT and holds OUR slug ('cheryfs'), never an upstream value.
--   * Upstream identifiers live ONLY in idp_org_bindings and are ALWAYS
--     namespaced by idp_alias -- a bare upstream value is never a lookup key,
--     because two platforms may both report tenant_id = '1'.
--   * org_id IS NULL on a resource row means "platform-wide / shared".
--   * statements here are idempotent and are re-applied by
--     ``_ensure_multi_tenant_schema`` for databases whose version watermark
--     already moved past 16.
--
-- NOT in this migration: ROW LEVEL SECURITY. Policies need a dedicated
-- non-superuser application role; enabling them without it silently does
-- nothing (see plan T2-4). The application-layer filter is what protects
-- tenants until then.

CREATE TABLE IF NOT EXISTS orgs (
  org_id           TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  kind             TEXT NOT NULL DEFAULT 'internal',
  status           TEXT NOT NULL DEFAULT 'active',
  keycloak_org_id  TEXT,
  created_at       BIGINT NOT NULL,
  updated_at       BIGINT NOT NULL
);

-- Upstream platform -> org binding. The single source of truth for resolution.
CREATE TABLE IF NOT EXISTS idp_org_bindings (
  idp_alias         TEXT NOT NULL,
  match_type        TEXT NOT NULL,
  match_value       TEXT NOT NULL DEFAULT '',
  org_id            TEXT NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,
  requires_approval INTEGER NOT NULL DEFAULT 0,
  note              TEXT NOT NULL DEFAULT '',
  created_at        BIGINT NOT NULL,
  updated_at        BIGINT NOT NULL,
  -- match_value is NOT NULL on purpose. A nullable column would make the
  -- primary key below useless for match_type='static' rows: PostgreSQL treats
  -- NULLs as distinct, so several static bindings for one IdP would all be
  -- accepted and the resolution chain would hit an ambiguous row.
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

-- User x org membership. role_keys holds ability-package keys (JSON array),
-- resolved from the job mapping -- it is NOT related to users.role.
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

-- Ability packages (capability names, never job titles). ``org_id = NULL``
-- rows are platform packages (seeded, cross-tenant); a tenant id marks a
-- tenant-owned package, managed by that tenant's org_admin. Storage keys of
-- tenant packages carry the reserved ``t_{org}_`` prefix (app-enforced, and
-- the CHECK below keeps the namespace platform-reserved), so role_key stays
-- globally unique and every bare reference downstream -- membership JSON,
-- resource allowed_roles, the rls claim, hub ToolAcl snapshots -- resolves
-- unambiguously.
CREATE TABLE IF NOT EXISTS roles (
  role_key    TEXT PRIMARY KEY,
  org_id      TEXT REFERENCES orgs(org_id) ON DELETE CASCADE,
  name_zh     TEXT NOT NULL,
  permissions TEXT NOT NULL DEFAULT '[]',
  is_default  INTEGER NOT NULL DEFAULT 0,
  is_system   INTEGER NOT NULL DEFAULT 0,
  description TEXT NOT NULL DEFAULT '',
  -- Platform packages (org_id IS NULL) must never squat the tenant prefix.
  CONSTRAINT chk_roles_platform_key CHECK (
    org_id IS NOT NULL OR substr(role_key, 1, 2) <> 't_'
  )
);
CREATE INDEX IF NOT EXISTS idx_roles_org ON roles(org_id);

-- Seeded platform packages. ``org_admin`` is the tenant-administrator marker
-- granted only by platform admins; ``member`` is the is_default minimal
-- package JIT auto-provisioning falls back to. ``DO UPDATE`` re-asserts the
-- system invariants (mirrors tenancy_schema._ROLE_SEEDS, which re-applies on
-- every boot so pre-is_system upgrades get repaired too).
INSERT INTO roles (role_key, org_id, name_zh, permissions, is_default, is_system, description)
VALUES ('org_admin', NULL, '租户管理员', '[]', 0, 1,
        '平台预置：租户管理员，管理本租户成员、能力包与资源授权')
ON CONFLICT (role_key) DO UPDATE SET is_system = 1, is_default = 0, org_id = NULL;

INSERT INTO roles (role_key, org_id, name_zh, permissions, is_default, is_system, description)
VALUES ('member', NULL, '成员', '[]', 1, 1,
        '平台预置：JIT 归位的默认最小能力包')
ON CONFLICT (role_key) DO UPDATE SET is_system = 1, is_default = 1, org_id = NULL;

-- Job code -> ability package, per org. org_id = '*' means "default mapping
-- for every org"; because '*' is not a real org this column deliberately has
-- NO foreign key to orgs.
CREATE TABLE IF NOT EXISTS org_role_mappings (
  org_id   TEXT NOT NULL,
  job_code TEXT NOT NULL,
  role_key TEXT NOT NULL REFERENCES roles(role_key) ON DELETE CASCADE,
  PRIMARY KEY (org_id, job_code, role_key)
);
CREATE INDEX IF NOT EXISTS idx_org_role_mappings_job ON org_role_mappings(job_code);

-- Per-user session pointers.
ALTER TABLE users ADD COLUMN IF NOT EXISTS default_org_id TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS token_epoch BIGINT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_users_default_org ON users(default_org_id);

-- Resource tenancy. Leading org_id keeps the composite indexes usable once a
-- tenant predicate becomes mandatory on every read.
ALTER TABLE published_experts ADD COLUMN IF NOT EXISTS org_id TEXT;
ALTER TABLE published_experts ADD COLUMN IF NOT EXISTS allowed_roles TEXT NOT NULL DEFAULT '[]';
CREATE INDEX IF NOT EXISTS idx_published_experts_org ON published_experts(org_id, name);

ALTER TABLE agents ADD COLUMN IF NOT EXISTS org_id TEXT;
CREATE INDEX IF NOT EXISTS idx_agents_org ON agents(org_id, user_id);

ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS org_id TEXT;
CREATE INDEX IF NOT EXISTS idx_knowledge_bases_org ON knowledge_bases(org_id, owner_user_id);

ALTER TABLE connectors ADD COLUMN IF NOT EXISTS org_id TEXT;
ALTER TABLE connectors ADD COLUMN IF NOT EXISTS allowed_roles TEXT NOT NULL DEFAULT '[]';
CREATE INDEX IF NOT EXISTS idx_connectors_org ON connectors(org_id, user_id);

UPDATE _schema_version SET version = 18;
