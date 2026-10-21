-- Schema v37 (upstream v21 renumbered): skill packages can disable ZIP download for non-owners.

ALTER TABLE skill_packages ADD COLUMN allow_download INTEGER NOT NULL DEFAULT 1;

UPDATE _schema_version SET version = 37;
