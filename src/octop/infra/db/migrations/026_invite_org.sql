-- 026: invite → tenant binding (T4-1).
--
-- ``user_invites`` gains a nullable ``org_id``: when set, redeeming the
-- invite drops the created user into that tenant (``org_memberships`` with
-- ``source='invite'``, plus ``users.default_org_id`` when still NULL).
-- NULL keeps the community behaviour of a tenant-less invite verbatim.
--
-- No FK on SQLite (the column is added by ``_ensure_invite_org_schema`` in
-- migrate.py, the 024 convention); the PG file owns the real constraint.

UPDATE _schema_version SET version = 26;
