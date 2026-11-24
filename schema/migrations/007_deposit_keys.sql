-- 007_deposit_keys.sql — deposit keys and deposit records (2026-11-12, per
-- Chase). Deposit (POST /v1/deposits) requires a depositor's X-API-Key
-- (service/api_keys.py). Only the key's
-- SHA-256 is stored. Every accepted bundle is one deposits row, one
-- deposit_objects row per object and one deposit_edges row per document
-- edge, so "which depositor sent this object" is answerable. Additive only.
--
-- Apply this BEFORE deploying code that enforces the key: that code reads
-- api_keys on every deposit. Until a key is issued (scripts/api_key.py),
-- every deposit is refused with 401.

CREATE TABLE IF NOT EXISTS api_keys (
  key_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  name VARCHAR(120) NOT NULL,
  key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  created_at DATETIME NOT NULL,
  created_by VARCHAR(320) NULL,
  revoked_at DATETIME NULL,
  revoked_by VARCHAR(320) NULL,
  PRIMARY KEY (key_id),
  UNIQUE KEY idx_ak_hash (key_sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS deposits (
  deposit_id VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  key_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  received_at DATETIME NOT NULL,
  object_count INT NOT NULL,
  PRIMARY KEY (deposit_id),
  KEY idx_deposits_key (key_id, received_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS deposit_objects (
  deposit_id VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  object_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  PRIMARY KEY (deposit_id, object_id),
  KEY idx_dobj_object (object_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS deposit_edges (
  deposit_id VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  seq INT NOT NULL,
  class_name VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  subject_id VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  object_id VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  payload JSON NOT NULL,
  PRIMARY KEY (deposit_id, seq),
  KEY idx_dedge_subject (subject_id(191)),
  KEY idx_dedge_object (object_id(191))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
