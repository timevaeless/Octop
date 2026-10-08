-- 032: published expert templates carry the publisher's composer defaults
-- (knowledge bases / MCP connectors / skill packages, 2026-09-29).
--
-- The installer-owned policy (5b6c9130) predates tenant attribution: back
-- then a publisher's KBs/connectors were invisible to installers, so
-- shipping them in the template was moot. With same-tenant sharing being
-- the primary flow, templates must carry the author's defaults; installs
-- still validate against the installer's own visibility (fail-closed).
--
-- Stored as JSON id-list strings, mirroring the agents profile columns
-- and the published_experts.allowed_roles style. Empty list = no default.
-- Mirrors 032_published_expert_defaults.sql.

ALTER TABLE published_experts ADD COLUMN IF NOT EXISTS default_knowledge_base_ids TEXT NOT NULL DEFAULT '[]';
ALTER TABLE published_experts ADD COLUMN IF NOT EXISTS default_mcp_servers TEXT NOT NULL DEFAULT '[]';
ALTER TABLE published_experts ADD COLUMN IF NOT EXISTS default_skill_package_ids TEXT NOT NULL DEFAULT '[]';

UPDATE _schema_version SET version = 32;
