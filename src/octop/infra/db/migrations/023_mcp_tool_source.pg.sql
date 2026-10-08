-- Schema v23: MCP catalog tool provenance (review round-2 N3, 2026-09-25).
-- * mcp_tools.source distinguishes import-managed tools from hand-defined
--   ones. Design doc §8.3 supports manual tools as a first-class channel;
--   the round-1 "sync import deletes ghost tools" fix would otherwise wipe
--   them on every re-import. import-openapi stamps 'openapi' and only prunes
--   'openapi' rows; PUT /tools stamps 'manual' (never auto-deleted).

ALTER TABLE mcp_tools ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'manual'
  CHECK (source IN ('openapi', 'manual'));

UPDATE _schema_version SET version = 23;
