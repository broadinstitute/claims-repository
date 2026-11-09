-- 001_init.sql — claims-repository schema for Aurora MySQL 8
-- (cyaka_claims_repository on the shared aurora-giant-bioindex cluster).
-- Mirrors service/db.py's SQLite shape; design: docs/query-graph-design.md §5.
-- Key columns are ascii_bin: DAPPER content addresses and portal keys are
-- ASCII and case-sensitive, and ascii keeps composite PKs under the InnoDB
-- 3072-byte index limit (longest observed gene-set name: 158 chars).

CREATE TABLE IF NOT EXISTS dapper_objects (
  id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  class_name VARCHAR(64) NOT NULL,
  identity_profile VARCHAR(32) NOT NULL,
  payload JSON NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen_at TIMESTAMP NULL DEFAULT NULL,
  -- Mint counter: 1 on first deposit, +1 per idempotent re-deposit.
  times_seen INT NOT NULL DEFAULT 1,
  PRIMARY KEY (id),
  KEY idx_do_class (class_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS claims (
  claim_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  proposition_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  method VARCHAR(32) NOT NULL DEFAULT '',
  deposited_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (claim_id),
  KEY idx_claims_prop (proposition_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS pigean_gene_associations (
  phenotype VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  collection VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  gene VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  prior DOUBLE NOT NULL,
  combined DOUBLE NOT NULL,
  log_bf DOUBLE NOT NULL,
  n INT NULL,
  trait_group VARCHAR(64) NULL,
  claim_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  PRIMARY KEY (phenotype, collection, gene),
  KEY idx_pga_combined (phenotype, collection, combined)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS pigean_gene_set_associations (
  phenotype VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  collection VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  gene_set VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source VARCHAR(128) NULL,
  beta DOUBLE NOT NULL,
  beta_uncorrected DOUBLE NOT NULL,
  rs_score DOUBLE NULL,
  n INT NULL,
  trait_group VARCHAR(64) NULL,
  claim_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  PRIMARY KEY (phenotype, collection, gene_set),
  KEY idx_pgsa_bu (phenotype, collection, beta_uncorrected)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS claim_memberships (
  collection VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  gene VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  gene_set VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  claim_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  PRIMARY KEY (collection, gene, gene_set),
  KEY idx_cm_set (collection, gene_set)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS gene_set_entities (
  collection VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  name VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  dapper_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  n_genes INT NULL,
  PRIMARY KEY (collection, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
