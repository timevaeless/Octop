-- 026: invite → tenant binding (T4-1).
--
-- ``user_invites.org_id`` (nullable) — see 026_invite_org.sql for the
-- rationale. FK is SET NULL so deleting a tenant never blocks on dangling
-- invites: they simply degrade back to community tenant-less invites (and
-- ``apply_invite_membership`` fails their redemption closed anyway, since
-- the org row is gone).

ALTER TABLE user_invites ADD COLUMN IF NOT EXISTS org_id TEXT
    REFERENCES orgs(org_id) ON DELETE SET NULL;

UPDATE _schema_version SET version = 26;
