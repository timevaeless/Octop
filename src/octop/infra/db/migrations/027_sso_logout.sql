-- 027: SSO single-logout hints (RP-Initiated Logout).
--
-- One row per user: the latest verified id_token from the provider they
-- logged in with. Consumed exactly once by POST /api/auth/logout to build
-- the standard ``end_session_endpoint?id_token_hint=...`` redirect.
-- Provider-agnostic by design: the logout endpoint itself is resolved at
-- logout time (provider.extra override → discovery → local-only logout),
-- so swapping the IdP in production needs zero code changes.
--
-- The id_token is short-lived and already delivered to the browser by the
-- IdP; storing it here is a convenience for the hint, not a secret.

CREATE TABLE IF NOT EXISTS sso_logout_hints (
    user_id INTEGER PRIMARY KEY,
    provider_id INTEGER NOT NULL,
    id_token TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

UPDATE _schema_version SET version = 27;
