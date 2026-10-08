-- 019 (SQLite): resource-level ACL columns + SSO provider namespace.
--
-- SQLite has no ``ADD COLUMN IF NOT EXISTS``; the actual column additions are
-- performed by ``_ensure_resource_acl_schema`` in migrate.py (idempotent,
-- mirrors 017_resource_acl.pg.sql). This file only advances the watermark.

UPDATE _schema_version SET version = 19;
