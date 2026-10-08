-- 034: three-state attribution for MCP catalog services (two-plane authz).
-- Mirrors 034_mcp_service_org.pg.sql. SQLite has no RLS; the paired .pg.sql
-- migration is the authoritative variant. See that file for the semantics of
-- mcp_services.org_id (NULL / '*' / tenant id).

ALTER TABLE mcp_services ADD COLUMN org_id TEXT;

CREATE INDEX IF NOT EXISTS idx_mcp_services_org ON mcp_services(org_id);

UPDATE _schema_version SET version = 34;
