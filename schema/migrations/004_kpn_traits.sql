-- 004_kpn_traits.sql — the KPN trait registry (2026-11-09, per Chase).
-- Canonical KPN.TRAIT IDs from broadinstitute/kpn-data-models, the legacy
-- portal / PIGEAN phenotype keys they replace (IBD -> KPN.TRAIT:0000505), and
-- their cross-ontology mappings (SSSOM: EFO, MONDO, MESH, DOID, ...). Loaded
-- by scripts/load_kpn_traits.py from a pinned release.

CREATE TABLE IF NOT EXISTS kpn_traits (
  trait_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  legacy_id VARCHAR(255) NOT NULL,
  pigean_id VARCHAR(255) NULL,
  source_category VARCHAR(64) NULL,
  name TEXT NOT NULL,
  description TEXT NULL,
  legacy_trait_group VARCHAR(128) NULL,
  trait_group VARCHAR(128) NULL,
  trait_type VARCHAR(64) NULL,
  primary_concept_id VARCHAR(128) NULL,
  primary_concept_label TEXT NULL,
  primary_predicate VARCHAR(64) NULL,
  PRIMARY KEY (trait_id),
  UNIQUE KEY idx_kt_legacy (legacy_id),
  KEY idx_kt_pigean (pigean_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS kpn_trait_mappings (
  trait_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  target_id VARCHAR(128) NOT NULL,
  ontology VARCHAR(64) NOT NULL,
  predicate VARCHAR(64) NOT NULL,
  label TEXT NULL,
  confidence DOUBLE NULL,
  justification VARCHAR(128) NULL,
  source TEXT NULL,
  PRIMARY KEY (trait_id, target_id, predicate),
  KEY idx_ktm_target (target_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS kpn_trait_release (
  release_key VARCHAR(32) NOT NULL,
  version VARCHAR(32) NOT NULL,
  commit_sha VARCHAR(64) NULL,
  source_url TEXT NOT NULL,
  file_sha256 CHAR(64) NOT NULL,
  trait_count INT NOT NULL,
  mapping_count INT NOT NULL,
  loaded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (release_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
