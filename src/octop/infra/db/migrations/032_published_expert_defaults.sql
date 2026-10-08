-- 032: published expert templates carry the publisher's composer defaults
-- (knowledge bases / MCP connectors / skill packages, 2026-09-29).
-- Mirrors 032_published_expert_defaults.pg.sql. SQLite has no RLS; the
-- paired .pg.sql migration rewrites the policies only.

ALTER TABLE published_experts ADD COLUMN default_knowledge_base_ids TEXT NOT NULL DEFAULT '[]';
ALTER TABLE published_experts ADD COLUMN default_mcp_servers TEXT NOT NULL DEFAULT '[]';
ALTER TABLE published_experts ADD COLUMN default_skill_package_ids TEXT NOT NULL DEFAULT '[]';

UPDATE _schema_version SET version = 32;
