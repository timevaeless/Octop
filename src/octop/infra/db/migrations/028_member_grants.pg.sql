-- 028: admin-granted ability packages survive JIT re-expansion. See
-- 028_member_grants.sql for the rationale.

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
