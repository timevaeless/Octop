-- 028: admin-granted ability packages survive JIT re-expansion.
--
-- Why: T4-2/T5-4 let admins grant ability packages to a specific user
-- (member roster PUT, user-membership PUT, pending-user assign) by writing
-- ``org_memberships.role_keys``. But every SSO login re-expands those keys
-- from ``org_role_mappings`` and replaces them wholesale ("never merges",
-- T1-1) — the mapping table is the *policy* authority, so any per-user
-- grant was silently wiped on the member's next login.
--
-- Fix: this table is the per-user grant authority. ``apply_jit_resolution``
-- unions mapped keys with grants instead of replacing them. Semantics:
--   * mapping-derived keys stay mapping-revocable (delete the mapping row
--     on the 角色映射 page; a per-user uncheck cannot and should not);
--   * grants are replaced wholesale by every admin write path, so an
--     uncheck of a granted key revokes it (the grant row is deleted).

CREATE TABLE IF NOT EXISTS org_member_grants (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id     TEXT NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,
    role_key   TEXT NOT NULL REFERENCES roles(role_key) ON DELETE CASCADE,
    granted_by TEXT NOT NULL DEFAULT '',
    created_at BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, org_id, role_key)
);

CREATE INDEX IF NOT EXISTS idx_org_member_grants_org ON org_member_grants(org_id);

UPDATE _schema_version SET version = 28;
