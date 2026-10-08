-- Schema v36 (upstream 020, renumbered: fork 020 is role_mapping_provenance) per-package copy policy for skill packages (#770).

ALTER TABLE skill_packages ADD COLUMN IF NOT EXISTS copy_policy TEXT NOT NULL DEFAULT 'snapshot';

UPDATE _schema_version SET version = 36;
