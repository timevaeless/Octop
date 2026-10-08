-- 025: row-level security on the four resource tables (T2-4).
--
-- Until now tenant isolation was enforced 100% by application-layer filters
-- (tenancy.py + repo list_visible). Correct code, but one missed WHERE clause
-- in any future query is a cross-tenant leak. RLS makes the database itself
-- the second line: a query without tenant context simply cannot see other
-- tenants' rows.
--
-- Policy shape (one per table, USING for reads/UPDATE-existing,
-- WITH CHECK for INSERT/UPDATE-new):
--
--   org_id IS NULL                                       → platform-shared
--   OR org_id = current_setting('app.org_id', true)      → own tenant
--   OR current_setting('app.bypass_org', true) = 'on'    → explicit bypass
--
-- The two settings are written by the application at connection checkout
-- (octop.infra.users.tenancy_context + pool.py). No scope ⇒ both settings
-- resolve to NULL/'' ⇒ only platform-shared rows are reachable (fail-closed).
--
--   ENABLE  — policies apply to non-owner roles.
--   FORCE   — policies apply to the table owner too. The application role
--             (``octop_app``) created these tables, so it IS the owner; FORCE
--             is what keeps a misconfigured owner connection from silently
--             bypassing isolation (plan T2-4 trap #2).
--
-- Notes:
-- * Migrations run wrapped in an explicit bypass scope (migrate.py), so the
--   backfill UPDATEs in earlier helpers stay subject-free.
-- * Superusers (the ops role ``octop_admin``) bypass RLS regardless of FORCE;
--   that is Postgres semantics, not something we can or should configure away.
-- * Deliberately NOT protected here: platform tables (users/orgs/roles/...)
--   which must be readable before a tenant context exists, and the MCP
--   catalog tables (admin-plane only, no tenant rows today). Extending RLS
--   to a new table = add it to this migration's follow-up, never silently.

ALTER TABLE published_experts ENABLE ROW LEVEL SECURITY;
ALTER TABLE published_experts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON published_experts
  USING (
    org_id IS NULL
    OR org_id = current_setting('app.org_id', true)
    OR current_setting('app.bypass_org', true) = 'on'
  )
  WITH CHECK (
    org_id IS NULL
    OR org_id = current_setting('app.org_id', true)
    OR current_setting('app.bypass_org', true) = 'on'
  );

ALTER TABLE agents ENABLE ROW LEVEL SECURITY;
ALTER TABLE agents FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON agents
  USING (
    org_id IS NULL
    OR org_id = current_setting('app.org_id', true)
    OR current_setting('app.bypass_org', true) = 'on'
  )
  WITH CHECK (
    org_id IS NULL
    OR org_id = current_setting('app.org_id', true)
    OR current_setting('app.bypass_org', true) = 'on'
  );

ALTER TABLE knowledge_bases ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_bases FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON knowledge_bases
  USING (
    org_id IS NULL
    OR org_id = current_setting('app.org_id', true)
    OR current_setting('app.bypass_org', true) = 'on'
  )
  WITH CHECK (
    org_id IS NULL
    OR org_id = current_setting('app.org_id', true)
    OR current_setting('app.bypass_org', true) = 'on'
  );

ALTER TABLE connectors ENABLE ROW LEVEL SECURITY;
ALTER TABLE connectors FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON connectors
  USING (
    org_id IS NULL
    OR org_id = current_setting('app.org_id', true)
    OR current_setting('app.bypass_org', true) = 'on'
  )
  WITH CHECK (
    org_id IS NULL
    OR org_id = current_setting('app.org_id', true)
    OR current_setting('app.bypass_org', true) = 'on'
  );

UPDATE _schema_version SET version = 25;
