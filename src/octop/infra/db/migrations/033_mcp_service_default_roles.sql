-- 033: service-level default allowed_roles for MCP tools (inheritance model).
-- Mirrors 033_mcp_service_default_roles.pg.sql. SQLite has no RLS; the
-- paired .pg.sql migration is the authoritative variant.

ALTER TABLE mcp_services ADD COLUMN allowed_roles TEXT NOT NULL DEFAULT '[]';

UPDATE _schema_version SET version = 33;
