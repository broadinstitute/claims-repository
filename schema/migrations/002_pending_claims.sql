-- 002_pending_claims.sql — lazy claim assertion (2026-11-09, per Chase).
-- Loader-built claims stage here, precomputed but unasserted; a claim moves
-- to dapper_objects (and the claims registry) the first time the scientific
-- query surface serves it. times_seen on dapper_objects then counts
-- assertions.

CREATE TABLE IF NOT EXISTS pending_claims (
  claim_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  proposition_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  method VARCHAR(32) NOT NULL DEFAULT '',
  identity_profile VARCHAR(32) NOT NULL,
  payload JSON NOT NULL,
  PRIMARY KEY (claim_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
