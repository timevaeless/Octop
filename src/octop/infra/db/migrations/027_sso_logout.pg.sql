-- 027: SSO single-logout hints (RP-Initiated Logout). See 027_sso_logout.sql.

CREATE TABLE IF NOT EXISTS sso_logout_hints (
    user_id INTEGER PRIMARY KEY,
    provider_id INTEGER NOT NULL,
    id_token TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

UPDATE _schema_version SET version = 27;
