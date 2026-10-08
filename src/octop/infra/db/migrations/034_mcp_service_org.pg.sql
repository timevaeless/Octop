-- 034: three-state attribution for MCP catalog services (two-plane authz).
--
-- mcp_services gains ``org_id`` with the 2026-09-27 attribution semantics:
--   NULL      = unattributed platform-private (admin drafts / admin-only tools)
--   '*'       = explicit platform share (visible to every tenant)
--   tenant id = hard boundary, attributed to that tenant
-- Why the platform plane must NOT pre-fill roles (the 建服务时点 problem): role
-- keys are now tenant-namespaced (``t_{org}_``), so a platform admin creating
-- a shared service cannot reference tenant packages that do not exist yet.
-- Attribution decides WHO configures the ACL (platform vs the tenant admin
-- via /api/org/{org}/resource-acl/mcp-services); the ``allowed_roles`` column
-- (033) decides WHAT roles pass — platform creates default ['*'] (open),
-- tenants narrow to their own packages afterwards.
--
-- Mirrors 034_mcp_service_org.sql.

ALTER TABLE mcp_services ADD COLUMN IF NOT EXISTS org_id TEXT;

CREATE INDEX IF NOT EXISTS idx_mcp_services_org ON mcp_services(org_id);

UPDATE _schema_version SET version = 34;
