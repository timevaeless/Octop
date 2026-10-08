-- 029: resource-attribution redesign at the RLS layer (2026-09-27).
--
-- The application predicate (tenancy.py / resource_acl.py / token_tenancy)
-- moved to three-state attribution:
--
--   org_id IS NULL   → unattributed admin draft: reachable ONLY through the
--                      explicit bypass scope (platform-admin request paths,
--                      boot, migrations) — never by a tenant scope;
--   org_id = '*'     → explicit platform share: every tenant scope may read it;
--   otherwise        → the row's tenant (own-tenant scope only).
--
-- Migration 025 encoded the OLD semantics (``org_id IS NULL`` = platform-
-- shared) directly in the policies. Leaving those in place would keep NULL
-- rows tenant-visible at the database layer even after the Python predicates
-- flip — a silent half-migrated rule. This migration rewrites all four
-- policies to the new shape (same policy names, same tables, same FORCE).
--
-- Compatibility notes:
-- * '*' rows were previously invisible to tenant scopes (no branch matched);
--   they now resolve as platform shares — that is the new designed state,
--   and no pre-existing row carries '*' (the value is newly meaningful).
-- * Rows created NULL by the new admin stamping rule rely on the bypass
--   scope for admin visibility; tenant scopes (including unbound, whose
--   ``app.org_id`` is the empty string) can no longer read them. That is
--   exactly the fail-closed intent.
-- * SQLite has no RLS; the paired .sql migration only bumps the version.

DROP POLICY IF EXISTS tenant_isolation ON published_experts;
CREATE POLICY tenant_isolation ON published_experts
  USING (
    org_id = current_setting('app.org_id', true)
    OR org_id = '*'
    OR current_setting('app.bypass_org', true) = 'on'
  )
  WITH CHECK (
    org_id = current_setting('app.org_id', true)
    OR org_id = '*'
    OR current_setting('app.bypass_org', true) = 'on'
  );

DROP POLICY IF EXISTS tenant_isolation ON agents;
CREATE POLICY tenant_isolation ON agents
  USING (
    org_id = current_setting('app.org_id', true)
    OR org_id = '*'
    OR current_setting('app.bypass_org', true) = 'on'
  )
  WITH CHECK (
    org_id = current_setting('app.org_id', true)
    OR org_id = '*'
    OR current_setting('app.bypass_org', true) = 'on'
  );

DROP POLICY IF EXISTS tenant_isolation ON knowledge_bases;
CREATE POLICY tenant_isolation ON knowledge_bases
  USING (
    org_id = current_setting('app.org_id', true)
    OR org_id = '*'
    OR current_setting('app.bypass_org', true) = 'on'
  )
  WITH CHECK (
    org_id = current_setting('app.org_id', true)
    OR org_id = '*'
    OR current_setting('app.bypass_org', true) = 'on'
  );

DROP POLICY IF EXISTS tenant_isolation ON connectors;
CREATE POLICY tenant_isolation ON connectors
  USING (
    org_id = current_setting('app.org_id', true)
    OR org_id = '*'
    OR current_setting('app.bypass_org', true) = 'on'
  )
  WITH CHECK (
    org_id = current_setting('app.org_id', true)
    OR org_id = '*'
    OR current_setting('app.bypass_org', true) = 'on'
  );

UPDATE _schema_version SET version = 29;
