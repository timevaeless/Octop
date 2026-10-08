-- 029: resource-attribution redesign (2026-09-27) — SQLite companion.
--
-- SQLite has no row-level security; the attribution semantics live entirely
-- in the application predicates (tenancy.py / resource_acl.py /
-- token_tenancy.py, migration 025's SQLite counterpart was likewise a
-- version bump). See 029_resource_attribution.pg.sql for the full rationale:
--
--   org_id IS NULL → unattributed (admin-draft, bypass-only at the PG layer)
--   org_id = '*'   → explicit platform share
--   otherwise      → that tenant's hard boundary

UPDATE _schema_version SET version = 29;
