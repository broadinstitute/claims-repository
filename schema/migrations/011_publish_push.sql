-- 011_publish_push.sql — publishing a release into the deployed service with a
-- `claims login` token (2026-11-24; design: docs/publishing-to-a-deployment.md).
-- A release records who pushed it (NULL for one published with the database
-- password). dataset_indexes records, per pushed dataset, where its Parquet
-- index lives (a bucket the data's owner keeps; the rows are never stored
-- here), each index file's sha256, size and S3 version, its counts, and who
-- pushed it for which collection, so a release's members are bound to its
-- collection. publish_bundles keeps the collection files each push sent, once
-- per digest, so every request of a push is checked against the same
-- collection. Additive only: the previous build keeps working on the migrated
-- database (prod shares it).

ALTER TABLE collection_releases ADD COLUMN published_by VARCHAR(320) NULL;
ALTER TABLE collection_releases ADD COLUMN publisher_token_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL;

CREATE TABLE IF NOT EXISTS publish_bundles (
  bundle_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  collection_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  files LONGTEXT NOT NULL,
  created_by VARCHAR(320) NOT NULL,
  created_at DATETIME NOT NULL,
  PRIMARY KEY (bundle_sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS dataset_indexes (
  dataset_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  collection_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  location VARCHAR(512) NOT NULL,
  files TEXT NOT NULL,
  counts TEXT NOT NULL,
  bundle_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  published_by VARCHAR(320) NOT NULL,
  publisher_token_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  stored_at DATETIME NOT NULL,
  PRIMARY KEY (dataset_id),
  KEY idx_di_collection (collection_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
