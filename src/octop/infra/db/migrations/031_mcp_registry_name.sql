-- Schema v31: mcp_services.registry_name — which mcpbridge registry entry
-- backs this service (design §3.2, v2 P2). 'nacos' keeps the legacy path:
-- destination composed as nacos2 without a bridge lookup, byte-identical to
-- pre-P2 publishes. Any other name MUST resolve in the live mcpbridges.default
-- and its type must be in VERIFIED_REGISTRY_TYPES before publish (fail-closed).

ALTER TABLE mcp_services ADD COLUMN registry_name TEXT NOT NULL DEFAULT 'nacos';

UPDATE _schema_version SET version = 31;
