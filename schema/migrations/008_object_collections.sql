-- Object collections (docs/collections.md): collections whose releases are built from deposits of DAPPER objects.
-- A deposit may name a collection and an open release of it; a key needs a grant for that collection.

ALTER TABLE deposits ADD COLUMN collection_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL;
ALTER TABLE deposits ADD COLUMN release_version VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL;

CREATE TABLE IF NOT EXISTS api_key_grants (
  key_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  collection_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  granted_at DATETIME NOT NULL,
  granted_by VARCHAR(255) NULL,
  PRIMARY KEY (key_id, collection_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS object_collections (
  collection_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  definition LONGTEXT NOT NULL,
  git_commit VARCHAR(64) NULL,
  registered_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (collection_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS object_releases (
  collection_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  version VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  state VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  definition LONGTEXT NOT NULL,
  release_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NULL,
  opened_at DATETIME NOT NULL,
  opened_by VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  closed_at DATETIME NULL,
  PRIMARY KEY (collection_key, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS release_objects (
  collection_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  version VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  object_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  class_name VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  deposit_id VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  PRIMARY KEY (collection_key, version, object_id),
  KEY idx_robj_class (collection_key, version, class_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS release_object_fields (
  collection_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  version VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  object_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  field VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  value VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  num DOUBLE NULL,
  PRIMARY KEY (collection_key, version, object_id, field, value),
  KEY idx_rof_value (collection_key, version, field, value)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
