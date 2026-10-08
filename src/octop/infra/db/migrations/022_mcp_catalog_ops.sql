-- Schema v22: MCP catalog ops hardening (review batch 2, 2026-09-25).
-- Mirrors 022_mcp_catalog_ops.pg.sql; see that file for the design notes.

CREATE TABLE IF NOT EXISTS mcp_locks (
  lock_key   TEXT PRIMARY KEY,
  holder     TEXT NOT NULL,
  expires_at BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mcp_publish_log_service
  ON mcp_publish_log(service_id);

UPDATE _schema_version SET version = 22;
