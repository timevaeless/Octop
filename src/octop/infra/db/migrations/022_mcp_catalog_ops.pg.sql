-- Schema v22: MCP catalog ops hardening (review batch 2, 2026-09-25).
-- * mcp_locks: publish-path serialization (review 2.7 — read-modify-write on
--   nacos plugin configs must not run concurrently; TTL guards a crashed
--   holder, holder-tagged delete guards stealing an expired lock).
-- * publish-log index: list_publish_log filtered by service_id (review 3.15).

CREATE TABLE IF NOT EXISTS mcp_locks (
  lock_key   TEXT PRIMARY KEY,
  holder     TEXT NOT NULL,
  expires_at BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mcp_publish_log_service
  ON mcp_publish_log(service_id);

UPDATE _schema_version SET version = 22;
