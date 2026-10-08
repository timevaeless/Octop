-- Schema v21: MCP catalog — tool definitions as the single source of truth.
-- Mirrors 021_mcp_catalog.sql; see that file for the design notes.

CREATE TABLE IF NOT EXISTS mcp_services (
  service_id         TEXT PRIMARY KEY,
  display_name       TEXT NOT NULL,
  description        TEXT NOT NULL DEFAULT '',
  nacos_namespace    TEXT NOT NULL DEFAULT 'local',
  nacos_group        TEXT NOT NULL DEFAULT 'DEFAULT_GROUP',
  nacos_service_name TEXT NOT NULL,
  backend_port       INTEGER NOT NULL,
  openapi_url        TEXT NOT NULL DEFAULT '',
  mcp_path           TEXT,
  status             TEXT NOT NULL DEFAULT 'draft',
  created_at         BIGINT NOT NULL,
  updated_at         BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_tools (
  id             BIGSERIAL PRIMARY KEY,
  service_id     TEXT NOT NULL REFERENCES mcp_services(service_id) ON DELETE CASCADE,
  tool_name      TEXT NOT NULL,
  description    TEXT NOT NULL DEFAULT '',
  http_method    TEXT NOT NULL DEFAULT 'GET',
  http_path      TEXT NOT NULL,
  parameters_json TEXT NOT NULL DEFAULT '[]',
  request_body   TEXT,
  response_body  TEXT,
  allowed_roles  TEXT NOT NULL DEFAULT '[]',
  enabled        INTEGER NOT NULL DEFAULT 1,
  created_at     BIGINT NOT NULL,
  updated_at     BIGINT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mcp_tools_service_name
  ON mcp_tools(service_id, tool_name);

CREATE TABLE IF NOT EXISTS mcp_publish_log (
  id         BIGSERIAL PRIMARY KEY,
  service_id TEXT NOT NULL,
  action     TEXT NOT NULL,
  result     TEXT NOT NULL,
  detail     TEXT NOT NULL DEFAULT '',
  actor      TEXT NOT NULL DEFAULT '',
  created_at BIGINT NOT NULL
);

UPDATE _schema_version SET version = 21;
