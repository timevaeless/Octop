-- 019: resource-level ACL columns + SSO provider namespace.
--
-- Part of the resource-authorization design (docs/resource-authorization-design.md):
--   * agents / knowledge_bases gain ``allowed_roles`` (published_experts and
--     connectors already got theirs in 016).
--   * ``sso_providers.idp_alias`` names the upstream namespace so JIT
--     resolution can look up ``idp_org_bindings`` with a namespaced key
--     ``(idp_alias, match_type, match_value)``. Empty string means "not set"
--     and disables claim/static resolution for that provider (fail-closed).
--
-- allowed_roles semantics: JSON array of ability-package keys (``roles.role_key``).
-- ``[]`` = no role restriction (visibility is then decided by tenancy alone).
-- The column is NOT NULL with a default on purpose: a NULL would make every
-- intersection check degenerate and silently widen access.

ALTER TABLE agents ADD COLUMN IF NOT EXISTS allowed_roles TEXT NOT NULL DEFAULT '[]';

ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS allowed_roles TEXT NOT NULL DEFAULT '[]';

ALTER TABLE sso_providers ADD COLUMN IF NOT EXISTS idp_alias TEXT NOT NULL DEFAULT '';

-- Backfill tenancy for pre-existing agents from their owner's default org.
UPDATE agents a SET org_id = u.default_org_id
  FROM users u
  WHERE a.user_id = u.id AND a.org_id IS NULL AND u.default_org_id IS NOT NULL;

UPDATE _schema_version SET version = 19;
