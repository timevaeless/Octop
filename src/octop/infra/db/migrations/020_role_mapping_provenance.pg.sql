-- 020: provenance for ``org_role_mappings``.
--
-- Why: ``org_role_mappings`` rows auto-created by JIT provisioning were written
-- against the ``'*'`` scope, which is *correct* for the translation semantics
-- (a role code means the same thing in every tenant) but threw away the one
-- piece of context the resolver actually held: **which tenant's first login
-- triggered the row**. Two consequences, both reported by an admin:
--
--   * the row looked unattributed ("why is this synced role not cheryfs?"), and
--   * nothing distinguished an auto-created default from a reviewed,
--     hand-written policy — an admin could edit a ``'*'`` row believing it
--     belonged to the tenant they were looking at, and thereby change every
--     tenant at once.
--
-- The columns below record that context without changing where a row applies
-- (the resolution predicate ``org_id IN ('*', ?)`` and the union in
-- ``expand_role_keys`` are untouched — see ADR / the probe in
-- ``tests/unit/db/test_orgs_repo.py``).
--
--   origin      'manual' | 'jit' | 'legacy'
--               admin/seed | auto-provisioned | predates provenance tracking
--   source_org  the tenant whose login triggered a JIT row (NULL otherwise)
--   created_at  epoch seconds
--
-- ``origin`` is NOT NULL with a default so the column is never ambiguous. The
-- default is ``'manual'`` because that is the only value a hand-written
-- ``INSERT`` should ever mean (the app always states it explicitly).

ALTER TABLE org_role_mappings ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'manual';

ALTER TABLE org_role_mappings ADD COLUMN IF NOT EXISTS source_org TEXT;

ALTER TABLE org_role_mappings ADD COLUMN IF NOT EXISTS created_at BIGINT NOT NULL DEFAULT 0;

-- Pre-existing rows predate the column; stamp them so the UI never shows 1970.
-- They are all ``'*'`` rows today, so ``source_org`` stays NULL on purpose:
-- asserting a trigger tenant we never recorded would be a guess.
UPDATE org_role_mappings
   SET created_at = EXTRACT(EPOCH FROM now())::BIGINT
 WHERE created_at = 0;

-- ...and they are genuinely unattributable, so they get their own value rather
-- than the ``'manual'`` the ALTER just handed out. ``'manual'`` would claim an
-- admin review that never happened, and these rows *do* include every JIT row
-- created before this migration — the very "is this synced role really ours?"
-- question that motivated the column. They are indistinguishable after the
-- fact, so the UI says "unknown" and offers the way out (delete + next login
-- re-provisions them with provenance). A blanket UPDATE is safe here: the
-- migration runs at startup, before the app serves any write.
UPDATE org_role_mappings SET origin = 'legacy';

CREATE INDEX IF NOT EXISTS idx_org_role_mappings_origin ON org_role_mappings(origin);

UPDATE _schema_version SET version = 20;
