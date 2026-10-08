-- 024: structured tenant dimension on the audit log (T2-6).
--
-- Why: ``audit_log`` carried ``(actor, action, target, payload)`` only. The
-- acting tenant was at best embedded in free-form ``payload`` text (e.g.
-- ``roles.py`` prepending ``org=...``), so "what did tenant X's people do"
-- was unanswerable by query — the T2-6 acceptance.
--
-- ``org_id`` records the tenant the action belongs to. Population rule:
--
--   * request-layer writes: the acting principal's org (for tenant-scoped
--     resource writes actor org == target org; an admin's cross-tenant write
--     is a platform action and records the *target* org where the handler
--     knows it);
--   * subsystem / system events (cron, backup auto, offline ops): NULL for
--     now — those paths gain tenant context with plan T2-5 and are filled
--     in there. NULL means "no tenant context recorded", never "all
--     tenants".
--
-- Purely additive: the column is nullable, no backfill is possible (history
-- predates the dimension), and existing readers are untouched.

ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS org_id TEXT;

CREATE INDEX IF NOT EXISTS idx_audit_log_org ON audit_log(org_id);

UPDATE _schema_version SET version = 24;
