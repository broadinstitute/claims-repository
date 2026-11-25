-- 011_publish_push.sql — publishing a release into the deployed service with a
-- `claims login` token (2026-11-24; design: docs/publishing-to-a-deployment.md).
-- A release records who pushed it (NULL for one published with the database
-- password), and publish_bundles keeps the collection files each push sent,
-- once per digest, so every request of a push is checked against the same
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
