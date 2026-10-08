-- 025 (SQLite): row-level security on the four resource tables (T2-4).
--
-- SQLite has no row-level security; the SQLite side of this migration is a
-- watermark bump only (the 019/020 convention). The Python scope binding
-- (tenancy_context) is dialect-independent and stays inert here.

UPDATE _schema_version SET version = 25;
