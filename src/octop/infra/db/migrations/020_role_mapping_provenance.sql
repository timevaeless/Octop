-- 020 (SQLite): provenance for ``org_role_mappings``.
--
-- SQLite has no ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``, so the actual
-- column additions are performed by adding the three columns to
-- ``_MULTI_TENANT_TABLES`` / ``_MULTI_TENANT_COLUMNS`` in migrate.py (both
-- idempotent and re-applied on every startup, on both dialects). The
-- ``'legacy'`` marking of pre-existing rows is the one piece that must NOT be
-- re-applied, so it lives in ``_mark_role_mappings_legacy`` and runs only on the
-- startup that creates the ``origin`` column — the mirror of the blanket UPDATE
-- in the PostgreSQL file. This file only advances the watermark, mirroring how
-- 016 and 017 are maintained.

UPDATE _schema_version SET version = 20;
