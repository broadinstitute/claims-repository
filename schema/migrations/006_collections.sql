-- 006_collections.sql — collections: any method's results, registered as
-- data (2026-11-10, per Chase; docs/collections.md). Additive: the PIGEAN
-- tables stay until the collection path reproduces them.
--
-- A release is a DAPPER ResultCollection (its id is the release id). Rows,
-- edges and metrics belong to datasets, which are content-addressed DAPPER
-- Datasets shared by every release that lists them. Claims are minted when
-- served: result_claims names each claim's address and the mint context
-- (template, file, manifest semantics) that re-renders it.

CREATE TABLE IF NOT EXISTS collections (
  collection_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  title TEXT NOT NULL,
  description TEXT NULL,
  current_release_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (collection_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS collection_releases (
  release_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  collection_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  version VARCHAR(64) NOT NULL,
  manifest JSON NOT NULL,
  document JSON NOT NULL,
  git_commit VARCHAR(64) NULL,
  published_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (release_id),
  UNIQUE KEY idx_cr_version (collection_key, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS release_datasets (
  release_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  dataset_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  PRIMARY KEY (release_id, dataset_id),
  KEY idx_rd_dataset (dataset_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS dataset_dimensions (
  dataset_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  dimension VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  value VARCHAR(255) NOT NULL,
  label TEXT NULL,
  PRIMARY KEY (dataset_id, dimension),
  KEY idx_dd_value (dimension, value)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS dataset_files (
  dataset_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  role VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  file_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  PRIMARY KEY (dataset_id, role)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS mint_contexts (
  activity_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  template_file_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  result_file_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  context JSON NOT NULL,
  PRIMARY KEY (activity_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS release_mint_contexts (
  release_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  activity_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  dataset_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  PRIMARY KEY (release_id, activity_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS result_rows (
  row_id BIGINT NOT NULL AUTO_INCREMENT,
  dataset_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  kind VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  subject_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  subject_value VARCHAR(255) NOT NULL,
  subject_label TEXT NULL,
  object_id VARCHAR(255) NOT NULL,
  object_label TEXT NULL,
  file_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  row_json JSON NOT NULL,
  spans JSON NOT NULL,
  PRIMARY KEY (row_id),
  UNIQUE KEY idx_rr_subject (dataset_id, kind, subject_id),
  KEY idx_rr_value (kind, subject_value)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS result_metrics (
  row_id BIGINT NOT NULL,
  dataset_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  kind VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  metric VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  value DOUBLE NOT NULL,
  PRIMARY KEY (row_id, metric),
  KEY idx_rm_order (dataset_id, kind, metric, value)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS result_edges (
  dataset_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  kind VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  from_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  to_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  from_value VARCHAR(255) NOT NULL,
  to_value VARCHAR(255) NOT NULL,
  PRIMARY KEY (dataset_id, kind, from_id, to_id),
  KEY idx_re_to (dataset_id, kind, to_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS result_claims (
  claim_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  activity_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  claim_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  dataset_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  row_id BIGINT NULL,
  edge_kind VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  group_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NULL,
  proposition_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  PRIMARY KEY (claim_id),
  KEY idx_rc_row (row_id),
  KEY idx_rc_group (dataset_id, edge_kind, group_id),
  KEY idx_rc_prop (proposition_id),
  KEY idx_rc_activity (activity_id, claim_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
