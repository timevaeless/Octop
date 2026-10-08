-- 024 (SQLite): structured tenant dimension on the audit log (T2-6).
--
-- SQLite has no ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``, so the actual
-- column addition is performed by ``_ensure_audit_org_schema`` in migrate.py
-- (idempotent, re-applied on every startup, on both dialects) — the 019/020
-- convention. This file only advances the watermark.

UPDATE _schema_version SET version = 24;
