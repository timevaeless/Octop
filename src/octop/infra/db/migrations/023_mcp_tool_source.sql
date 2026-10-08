-- Schema v23: MCP catalog tool provenance (review round-2 N3, 2026-09-25).
-- Mirrors 023_mcp_tool_source.pg.sql; see that file for the design notes.

ALTER TABLE mcp_tools ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'
  CHECK (source IN ('openapi', 'manual'));

UPDATE _schema_version SET version = 23;
