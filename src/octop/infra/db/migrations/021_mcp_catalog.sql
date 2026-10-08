-- Schema v21: MCP catalog — tool definitions as the single source of truth.
--
-- Owns three NEW tables (mcp_services / mcp_tools / mcp_publish_log).
-- Deliberately does NOT touch any community-owned table: the catalog is an
-- isolated module (src/octop/mcp_catalog/) and upstream merges must never
-- conflict here. Repo + router + publisher all live inside that package; the
-- only touch point outside it is a two-line router mount in api/app.py.
--
--   mcp_services      one row per business system onboarded to the MCP gateway
--                     (nacos coordinates + optional OpenAPI source + publish
--                     target derived from service_id)
--   mcp_tools         one row per exposed tool (REST endpoint or hand-written);
--                     allowed_roles feeds hub ToolAcl / rls filtering
--   mcp_publish_log   audit trail of every publish (dry-run included)
--
-- Timestamps are epoch seconds everywhere, matching orgs/connectors.

CREATE TABLE IF NOT EXISTS mcp_services (
  service_id         TEXT PRIMARY KEY,          -- opaque slug, e.g. 'summer-cmc'
  display_name       TEXT NOT NULL,
  description        TEXT NOT NULL DEFAULT '',
  nacos_namespace    TEXT NOT NULL DEFAULT 'local',
  nacos_group        TEXT NOT NULL DEFAULT 'DEFAULT_GROUP',
  nacos_service_name TEXT NOT NULL,
  backend_port       INTEGER NOT NULL,
  openapi_url        TEXT NOT NULL DEFAULT '',
  mcp_path           TEXT,                      -- NULL => derived /api/internal/mcp/{service_id}
  status             TEXT NOT NULL DEFAULT 'draft',  -- draft | published | disabled
  created_at         INTEGER NOT NULL,
  updated_at         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_tools (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  service_id     TEXT NOT NULL REFERENCES mcp_services(service_id) ON DELETE CASCADE,
  tool_name      TEXT NOT NULL,
  description    TEXT NOT NULL DEFAULT '',
  http_method    TEXT NOT NULL DEFAULT 'GET',
  http_path      TEXT NOT NULL,
  parameters_json TEXT NOT NULL DEFAULT '[]',  -- mcp-server plugin args: [{name, description, ...}]
  request_body   TEXT,                         -- literal body template; NULL => no body
  response_body  TEXT,                         -- optional responseTemplate body
  allowed_roles  TEXT NOT NULL DEFAULT '[]',   -- JSON array of role_keys; ["*"] = all roles
  enabled        INTEGER NOT NULL DEFAULT 1,
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mcp_tools_service_name ON mcp_tools(service_id, tool_name);

CREATE TABLE IF NOT EXISTS mcp_publish_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  service_id TEXT NOT NULL,
  action     TEXT NOT NULL,               -- publish | unpublish | dry_run
  result     TEXT NOT NULL,               -- ok | error
  detail     TEXT NOT NULL DEFAULT '',    -- JSON: resources written / errors
  actor      TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);

UPDATE _schema_version SET version = 21;
