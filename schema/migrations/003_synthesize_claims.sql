-- 003_synthesize_claims.sql — claims are minted when queried, by synthesis
-- (2026-11-09, per Chase). Replaces 002's staging table: each index row names
-- its claim's precomputed content address and carries the synthesis inputs
-- (reference IDs) the claim needs, so the service synthesizes the claim from
-- the row alone when the query surface serves it.
--
-- Data loaded before 003 stored claims eagerly and lacks per-row synthesis
-- inputs, so 003 refuses to run unless the store is EMPTY — index tables,
-- the claims registry, and stored objects alike (emptying only some would
-- leave eager-era claims behind). Recreate the database, migrate, then load
-- (scripts/load_kpn_traits.py, then scripts/load_pigean.py).
-- requires-empty: pigean_gene_associations, pigean_gene_set_associations, claim_memberships, claims, dapper_objects
--
-- ONE EFFECT PER STATEMENT: scripts/migrate.py treats "already exists" errors
-- (1050 table, 1060 column, 1061 key) as already applied on a re-run, which
-- is only safe when the statement has no other effect. migrate.py then
-- verifies the resulting schema.

DROP TABLE IF EXISTS pending_claims;

ALTER TABLE pigean_gene_associations ADD COLUMN proposition_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NULL;
ALTER TABLE pigean_gene_associations ADD COLUMN synthesis_inputs JSON NULL;
ALTER TABLE pigean_gene_associations ADD KEY idx_pga_claim (claim_id);
ALTER TABLE pigean_gene_associations ADD KEY idx_pga_prop (proposition_id);

ALTER TABLE pigean_gene_set_associations ADD COLUMN proposition_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NULL;
ALTER TABLE pigean_gene_set_associations ADD COLUMN synthesis_inputs JSON NULL;
ALTER TABLE pigean_gene_set_associations ADD KEY idx_pgsa_claim (claim_id);
ALTER TABLE pigean_gene_set_associations ADD KEY idx_pgsa_prop (proposition_id);

-- Membership join edges are scoped to the phenotype whose membership file
-- they were observed in ('' = collection-wide, e.g. a REST deposit hint).
ALTER TABLE claim_memberships ADD COLUMN phenotype VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '' FIRST;
-- Idempotent on a re-run: dropping and re-adding the same key succeeds.
ALTER TABLE claim_memberships DROP PRIMARY KEY, ADD PRIMARY KEY (phenotype, collection, gene, gene_set);
ALTER TABLE claim_memberships ADD KEY idx_cm_gene (gene, collection);
ALTER TABLE claim_memberships ADD KEY idx_cm_claim (claim_id);

-- One roster claim per gene set, keyed by the claim's own content address so
-- a row can never be overwritten with different inputs.
CREATE TABLE IF NOT EXISTS pigean_gene_set_rosters (
  claim_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  phenotype VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  collection VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  gene_set VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  proposition_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  synthesis_inputs JSON NOT NULL,
  PRIMARY KEY (claim_id),
  KEY idx_pgsr_set (collection, gene_set),
  KEY idx_pgsr_prop (proposition_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- Reference index behind GET /v1/objects/search?references=<id>.
CREATE TABLE IF NOT EXISTS object_refs (
  from_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  to_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  PRIMARY KEY (from_id, to_id),
  KEY idx_refs_to (to_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
