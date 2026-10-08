-- 033: service-level default allowed_roles for MCP tools (inheritance model).
--
-- Three-layer semantics, fail-closed unchanged:
--   1. mcp_services.allowed_roles (JSON id-list string, mirroring
--      mcp_tools.allowed_roles / published_experts.allowed_roles style) is the
--      default role set for EVERY tool of the service;
--   2. a tool with explicit allowed_roles (including ["*"]) overrides the
--      service default;
--   3. a tool left empty AND an empty service default = no ACL entry written
--      for the tool → hub denies everyone (unchanged fail-closed behavior).
-- Mirrors 033_mcp_service_default_roles.sql.

ALTER TABLE mcp_services ADD COLUMN IF NOT EXISTS allowed_roles TEXT NOT NULL DEFAULT '[]';

UPDATE _schema_version SET version = 33;
