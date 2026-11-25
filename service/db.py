"""Storage layer with two backends behind one tiny wrapper:

- SQLite (default): local development and tests; tables created on connect.
- Aurora MySQL 8 (when CLAIMS_MYSQL_HOST is set): the QA/prod deployment via
  dig-service-platform, on the shared cluster, in the database named by
  CLAIMS_MYSQL_DATABASE (required; set in the deploy's service.yaml — see
  docs/query-graph-design.md §5/§6). Schema is applied by
  scripts/migrate.py from schema/migrations/*.sql — the MySQL path never runs
  DDL on connect.

The service's SQL is written once in SQLite style (`?` placeholders,
dict-style rows); the Connection wrapper translates placeholders for PyMySQL
and the Dialect object carries the few fragments that genuinely differ
(json_extract, upserts, now()).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("CLAIMS_DB", str(ROOT / "claims.db"))

MYSQL_HOST = os.environ.get("CLAIMS_MYSQL_HOST")
MYSQL_PORT = int(os.environ.get("CLAIMS_MYSQL_PORT", "3306"))
MYSQL_USER = os.environ.get("CLAIMS_MYSQL_USER", "cyaka")
MYSQL_PASSWORD = os.environ.get("CLAIMS_MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.environ.get("CLAIMS_MYSQL_DATABASE")
MYSQL_CA_FILE = os.environ.get("CLAIMS_MYSQL_CA_FILE")

SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS dapper_objects (
  id TEXT PRIMARY KEY,
  class_name TEXT NOT NULL,
  identity_profile TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now')),
  last_seen_at TEXT,
  -- Mint counter (2026-11-09, Chase): content addressing makes re-deposits
  -- idempotent, so this records how often the same document keeps being
  -- minted from the underlying data: 1 on first deposit, +1 per re-deposit.
  times_seen INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS claims (
  claim_id TEXT PRIMARY KEY REFERENCES dapper_objects(id),
  proposition_id TEXT NOT NULL,
  method TEXT NOT NULL,
  deposited_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_claims_prop ON claims (proposition_id);
-- PIGEAN index tables. Claims are minted when queried (2026-11-09, Chase):
-- each row names its claim's precomputed content address (claim_id) and
-- carries synthesis_inputs (JSON: the reference IDs the claim needs that the
-- row cannot recompute), so the service synthesizes the claim from the row
-- alone and a row always moves together with the claim it names.
-- synthesis_inputs is NULL on rows written by REST deposit index_hints: their
-- claims were deposited, not template-built.
CREATE TABLE IF NOT EXISTS pigean_gene_associations (
  phenotype TEXT NOT NULL,
  collection TEXT NOT NULL,
  gene TEXT NOT NULL,
  prior REAL NOT NULL,
  combined REAL NOT NULL,
  log_bf REAL NOT NULL,
  n INTEGER,
  trait_group TEXT,
  claim_id TEXT NOT NULL,
  proposition_id TEXT,
  synthesis_inputs TEXT,
  PRIMARY KEY (phenotype, collection, gene)
);
CREATE INDEX IF NOT EXISTS idx_pga_combined
  ON pigean_gene_associations (phenotype, collection, combined);
CREATE INDEX IF NOT EXISTS idx_pga_claim ON pigean_gene_associations (claim_id);
CREATE INDEX IF NOT EXISTS idx_pga_prop ON pigean_gene_associations (proposition_id);
CREATE TABLE IF NOT EXISTS pigean_gene_set_associations (
  phenotype TEXT NOT NULL,
  collection TEXT NOT NULL,
  gene_set TEXT NOT NULL,
  source TEXT,
  beta REAL NOT NULL,
  beta_uncorrected REAL NOT NULL,
  rs_score REAL,
  n INTEGER,
  trait_group TEXT,
  claim_id TEXT NOT NULL,
  proposition_id TEXT,
  synthesis_inputs TEXT,
  PRIMARY KEY (phenotype, collection, gene_set)
);
CREATE INDEX IF NOT EXISTS idx_pgsa_bu
  ON pigean_gene_set_associations (phenotype, collection, beta_uncorrected);
CREATE INDEX IF NOT EXISTS idx_pgsa_claim ON pigean_gene_set_associations (claim_id);
CREATE INDEX IF NOT EXISTS idx_pgsa_prop ON pigean_gene_set_associations (proposition_id);
-- One roster claim per gene set (2026-11-09). Keyed by the claim's own
-- content address, so a row can never be overwritten with different inputs.
CREATE TABLE IF NOT EXISTS pigean_gene_set_rosters (
  claim_id TEXT PRIMARY KEY,
  phenotype TEXT NOT NULL,
  collection TEXT NOT NULL,
  gene_set TEXT NOT NULL,
  proposition_id TEXT NOT NULL,
  synthesis_inputs TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pgsr_set ON pigean_gene_set_rosters (collection, gene_set);
CREATE INDEX IF NOT EXISTS idx_pgsr_prop ON pigean_gene_set_rosters (proposition_id);
-- Per-(gene, set) join edges, scoped to the phenotype whose membership file
-- they were observed in ('' = collection-wide, e.g. a REST deposit hint).
-- claim_id names the backing claim: a set's roster claim, or a deposited one.
CREATE TABLE IF NOT EXISTS claim_memberships (
  phenotype TEXT NOT NULL DEFAULT '',
  collection TEXT NOT NULL,
  gene TEXT NOT NULL,
  gene_set TEXT NOT NULL,
  claim_id TEXT NOT NULL,
  PRIMARY KEY (phenotype, collection, gene, gene_set)
);
CREATE INDEX IF NOT EXISTS idx_cm_set ON claim_memberships (collection, gene_set);
CREATE INDEX IF NOT EXISTS idx_cm_gene ON claim_memberships (gene, collection);
CREATE INDEX IF NOT EXISTS idx_cm_claim ON claim_memberships (claim_id);
-- Latest GeneSet entity per (collection, name): a display/lookup aid only —
-- claim synthesis never reads it.
CREATE TABLE IF NOT EXISTS gene_set_entities (
  collection TEXT NOT NULL,
  name TEXT NOT NULL,
  dapper_id TEXT NOT NULL,
  n_genes INTEGER,
  PRIMARY KEY (collection, name)
);
-- Reference index (from_id's document points at to_id): backs
-- GET /v1/objects/search?references=<id>.
CREATE TABLE IF NOT EXISTS object_refs (
  from_id TEXT NOT NULL,
  to_id TEXT NOT NULL,
  PRIMARY KEY (from_id, to_id)
);
CREATE INDEX IF NOT EXISTS idx_refs_to ON object_refs (to_id);
-- Text index (service/text_index.py): each stored object's text fields as
-- lowercased words; backs GET /v1/objects/search?q=.
CREATE TABLE IF NOT EXISTS object_text (
  object_id TEXT NOT NULL,
  field TEXT NOT NULL,
  class_name TEXT NOT NULL,
  words TEXT NOT NULL,
  PRIMARY KEY (object_id, field)
);
CREATE INDEX IF NOT EXISTS idx_ot_class ON object_text (class_name);
CREATE INDEX IF NOT EXISTS idx_do_class ON dapper_objects (class_name);
-- KPN trait registry (broadinstitute/kpn-data-models release, loaded by
-- scripts/load_kpn_traits.py): canonical KPN.TRAIT IDs, the legacy portal /
-- PIGEAN keys they replace, and their cross-ontology mappings (SSSOM).
CREATE TABLE IF NOT EXISTS kpn_traits (
  trait_id TEXT PRIMARY KEY,
  legacy_id TEXT NOT NULL,
  pigean_id TEXT,
  source_category TEXT,
  name TEXT NOT NULL,
  description TEXT,
  legacy_trait_group TEXT,
  trait_group TEXT,
  trait_type TEXT,
  primary_concept_id TEXT,
  primary_concept_label TEXT,
  primary_predicate TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kt_legacy ON kpn_traits (legacy_id);
CREATE INDEX IF NOT EXISTS idx_kt_pigean ON kpn_traits (pigean_id);
CREATE TABLE IF NOT EXISTS kpn_trait_mappings (
  trait_id TEXT NOT NULL,
  target_id TEXT NOT NULL,
  ontology TEXT NOT NULL,
  predicate TEXT NOT NULL,
  label TEXT,
  confidence REAL,
  justification TEXT,
  source TEXT,
  PRIMARY KEY (trait_id, target_id, predicate)
);
CREATE INDEX IF NOT EXISTS idx_ktm_target ON kpn_trait_mappings (target_id);
CREATE TABLE IF NOT EXISTS kpn_trait_release (
  release_key TEXT PRIMARY KEY,
  version TEXT NOT NULL,
  commit_sha TEXT,
  source_url TEXT NOT NULL,
  file_sha256 TEXT NOT NULL,
  trait_count INTEGER NOT NULL,
  mapping_count INTEGER NOT NULL,
  loaded_at TEXT DEFAULT (datetime('now'))
);
-- Deposit keys (007_deposit_keys.sql, service/api_keys.py): who may write, and
-- which key sent which bundle. Only a key's SHA-256 is stored.
CREATE TABLE IF NOT EXISTS api_keys (
  key_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  key_sha256 TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  created_by TEXT,
  revoked_at TEXT,
  revoked_by TEXT
);
CREATE TABLE IF NOT EXISTS deposits (
  deposit_id TEXT PRIMARY KEY,
  key_id TEXT NOT NULL,
  received_at TEXT NOT NULL,
  object_count INTEGER NOT NULL,
  collection_key TEXT,
  release_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_deposits_key ON deposits (key_id, received_at);
-- Object collections (008_object_collections.sql): releases built from deposits.
CREATE TABLE IF NOT EXISTS api_key_grants (
  key_id TEXT NOT NULL,
  collection_key TEXT NOT NULL,
  granted_at TEXT NOT NULL,
  granted_by TEXT,
  PRIMARY KEY (key_id, collection_key)
);
CREATE TABLE IF NOT EXISTS object_collections (
  collection_key TEXT PRIMARY KEY,
  definition TEXT NOT NULL,
  git_commit TEXT,
  registered_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS object_releases (
  collection_key TEXT NOT NULL,
  version TEXT NOT NULL,
  state TEXT NOT NULL,
  definition TEXT NOT NULL,
  release_id TEXT,
  opened_at TEXT NOT NULL,
  opened_by TEXT NOT NULL,
  closed_at TEXT,
  PRIMARY KEY (collection_key, version)
);
CREATE TABLE IF NOT EXISTS release_objects (
  collection_key TEXT NOT NULL,
  version TEXT NOT NULL,
  object_id TEXT NOT NULL,
  class_name TEXT NOT NULL,
  deposit_id TEXT NOT NULL,
  PRIMARY KEY (collection_key, version, object_id)
);
CREATE INDEX IF NOT EXISTS idx_robj_class ON release_objects (collection_key, version, class_name);
CREATE TABLE IF NOT EXISTS release_object_fields (
  collection_key TEXT NOT NULL,
  version TEXT NOT NULL,
  object_id TEXT NOT NULL,
  field TEXT NOT NULL,
  value TEXT NOT NULL,
  num REAL,
  PRIMARY KEY (collection_key, version, object_id, field, value)
);
CREATE INDEX IF NOT EXISTS idx_rof_value ON release_object_fields (collection_key, version, field, value);
CREATE TABLE IF NOT EXISTS deposit_objects (
  deposit_id TEXT NOT NULL,
  object_id TEXT NOT NULL,
  status TEXT NOT NULL,
  PRIMARY KEY (deposit_id, object_id)
);
CREATE INDEX IF NOT EXISTS idx_dobj_object ON deposit_objects (object_id);
CREATE TABLE IF NOT EXISTS deposit_edges (
  deposit_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  class_name TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  object_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (deposit_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_dedge_subject ON deposit_edges (subject_id);
CREATE INDEX IF NOT EXISTS idx_dedge_object ON deposit_edges (object_id);
-- Admin console sign-in audit (written by the console's server through
-- POST /v1/admin/api/auth-events; service/admin.py).
CREATE TABLE IF NOT EXISTS admin_auth_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  event TEXT NOT NULL,
  email TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_aae_at ON admin_auth_events (at);
-- CLI device-code sign-in and publisher tokens (010_device_login.sql; service/device_login.py).
CREATE TABLE IF NOT EXISTS device_codes (
  device_code_sha256 TEXT PRIMARY KEY,
  user_code TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  client TEXT,
  host TEXT,
  ip TEXT,
  requested_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  last_polled_at TEXT,
  decided_by TEXT,
  decided_at TEXT,
  token_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_dc_ip ON device_codes (ip, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_dc_expires ON device_codes (expires_at);
CREATE TABLE IF NOT EXISTS publisher_tokens (
  token_id TEXT PRIMARY KEY,
  token_sha256 TEXT NOT NULL UNIQUE,
  email TEXT NOT NULL,
  host TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  last_used_at TEXT,
  revoked_at TEXT,
  revoked_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_pt_email ON publisher_tokens (email);
-- Collections (006_collections.sql): any method's results, registered as data.
CREATE TABLE IF NOT EXISTS collections (
  collection_key TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT,
  current_release_id TEXT,
  updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS collection_releases (
  release_id TEXT PRIMARY KEY,
  collection_key TEXT NOT NULL,
  version TEXT NOT NULL,
  manifest TEXT NOT NULL,
  document TEXT NOT NULL,
  git_commit TEXT,
  published_at TEXT DEFAULT (datetime('now')),
  published_by TEXT,
  publisher_token_id TEXT,
  UNIQUE (collection_key, version)
);
CREATE TABLE IF NOT EXISTS publish_bundles (
  bundle_sha256 TEXT PRIMARY KEY,
  collection_key TEXT NOT NULL,
  files TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS release_datasets (
  release_id TEXT NOT NULL,
  dataset_id TEXT NOT NULL,
  PRIMARY KEY (release_id, dataset_id)
);
CREATE INDEX IF NOT EXISTS idx_rd_dataset ON release_datasets (dataset_id);
CREATE TABLE IF NOT EXISTS dataset_dimensions (
  dataset_id TEXT NOT NULL,
  dimension TEXT NOT NULL,
  value TEXT NOT NULL,
  label TEXT,
  PRIMARY KEY (dataset_id, dimension)
);
CREATE INDEX IF NOT EXISTS idx_dd_value ON dataset_dimensions (dimension, value);
CREATE TABLE IF NOT EXISTS dataset_files (
  dataset_id TEXT NOT NULL,
  role TEXT NOT NULL,
  file_id TEXT NOT NULL,
  PRIMARY KEY (dataset_id, role)
);
CREATE TABLE IF NOT EXISTS mint_contexts (
  activity_id TEXT PRIMARY KEY,
  template_file_id TEXT NOT NULL,
  result_file_id TEXT NOT NULL,
  context TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS release_mint_contexts (
  release_id TEXT NOT NULL,
  activity_id TEXT NOT NULL,
  dataset_id TEXT NOT NULL,
  PRIMARY KEY (release_id, activity_id)
);
CREATE TABLE IF NOT EXISTS result_rows (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  dataset_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  subject_value TEXT NOT NULL,
  subject_label TEXT,
  object_id TEXT NOT NULL,
  object_label TEXT,
  file_id TEXT NOT NULL,
  row_json TEXT NOT NULL,
  spans TEXT NOT NULL,
  UNIQUE (dataset_id, kind, subject_id)
);
CREATE INDEX IF NOT EXISTS idx_rr_value ON result_rows (kind, subject_value);
CREATE TABLE IF NOT EXISTS result_metrics (
  row_id INTEGER NOT NULL,
  dataset_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  metric TEXT NOT NULL,
  value REAL NOT NULL,
  PRIMARY KEY (row_id, metric)
);
CREATE INDEX IF NOT EXISTS idx_rm_order ON result_metrics (dataset_id, kind, metric, value);
CREATE TABLE IF NOT EXISTS result_edges (
  dataset_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  from_id TEXT NOT NULL,
  to_id TEXT NOT NULL,
  from_value TEXT NOT NULL,
  to_value TEXT NOT NULL,
  PRIMARY KEY (dataset_id, kind, from_id, to_id)
);
CREATE INDEX IF NOT EXISTS idx_re_to ON result_edges (dataset_id, kind, to_id);
CREATE TABLE IF NOT EXISTS result_claims (
  claim_id TEXT PRIMARY KEY,
  activity_id TEXT NOT NULL,
  claim_key TEXT NOT NULL,
  dataset_id TEXT NOT NULL,
  row_id INTEGER,
  edge_kind TEXT,
  group_id TEXT,
  proposition_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rc_row ON result_claims (row_id);
CREATE INDEX IF NOT EXISTS idx_rc_group ON result_claims (dataset_id, edge_kind, group_id);
CREATE INDEX IF NOT EXISTS idx_rc_prop ON result_claims (proposition_id);
CREATE INDEX IF NOT EXISTS idx_rc_activity ON result_claims (activity_id, claim_key);
"""


class Dialect:
    """The SQL fragments that differ between SQLite and MySQL 8."""

    def __init__(self, backend: str):
        self.backend = backend

    @property
    def sqlite(self) -> bool:
        return self.backend == "sqlite"

    @property
    def now(self) -> str:
        return "datetime('now')" if self.sqlite else "UTC_TIMESTAMP()"

    @property
    def lock_share(self) -> str:
        """Suffix that makes a SELECT a locking read of the latest committed row. SQLite has no row locks: a
        transaction that has written already holds the database's single write lock."""
        return "" if self.sqlite else " FOR SHARE"

    @property
    def insert_or_replace(self) -> str:
        return "INSERT OR REPLACE INTO" if self.sqlite else "REPLACE INTO"

    @property
    def insert_or_ignore(self) -> str:
        return "INSERT OR IGNORE INTO" if self.sqlite else "INSERT IGNORE INTO"

    @property
    def upsert_object(self) -> str:
        """Insert a dapper object; an existing content address counts as a
        re-mint (times_seen += 1) instead of being silently replaced."""
        base = (
            "INSERT INTO dapper_objects (id, class_name, identity_profile, payload) "
            "VALUES (?, ?, ?, ?) "
        )
        if self.sqlite:
            return base + (
                "ON CONFLICT(id) DO UPDATE SET times_seen = times_seen + 1, "
                "last_seen_at = datetime('now')"
            )
        return base + (
            "ON DUPLICATE KEY UPDATE times_seen = times_seen + 1, "
            "last_seen_at = UTC_TIMESTAMP()"
        )

    @property
    def insert_ref(self) -> str:
        """Idempotent reference-index insert that does NOT swallow real errors
        (MySQL INSERT IGNORE would silently truncate over-long IDs)."""
        if self.sqlite:
            return "INSERT OR IGNORE INTO object_refs (from_id, to_id) VALUES (?, ?)"
        return (
            "INSERT INTO object_refs (from_id, to_id) VALUES (?, ?) "
            "ON DUPLICATE KEY UPDATE to_id = to_id"
        )

    @property
    def insert_text(self) -> str:
        """Idempotent text-index insert (service/text_index.py): a stored
        object's document never changes, so an existing row stands."""
        if self.sqlite:
            return "INSERT OR IGNORE INTO object_text (object_id, class_name, field, words) VALUES (?, ?, ?, ?)"
        return (
            "INSERT INTO object_text (object_id, class_name, field, words) VALUES (?, ?, ?, ?) "
            "ON DUPLICATE KEY UPDATE object_id = object_id"
        )

    def json_is_string(self, col: str, path: str) -> str:
        """True only when the top-level slot holds a JSON string, so text
        search and slot matching behave identically on both backends (MySQL's
        JSON_UNQUOTE would otherwise stringify numbers and JSON null)."""
        if self.sqlite:
            return f"json_type({col}, '$.{path}') = 'text'"
        return f"JSON_TYPE(JSON_EXTRACT({col}, '$.{path}')) = 'STRING'"

    def like_ci(self, expr: str) -> str:
        """Case-insensitive substring match with '!' as the LIKE escape, the
        same on both backends (MySQL compares utf8mb4_bin case-sensitively
        unless both sides are lowered)."""
        return f"LOWER({expr}) LIKE LOWER(?) ESCAPE '!'"

    def je(self, col: str, path: str) -> str:
        """json_extract returning a comparable string."""
        if self.sqlite:
            return f"json_extract({col}, '$.{path}')"
        return f"JSON_UNQUOTE(JSON_EXTRACT({col}, '$.{path}'))"


class QueryBudgetExceeded(RuntimeError):
    pass


class Connection:
    """Uniform surface over sqlite3 / PyMySQL: execute with `?` placeholders,
    dict-style rows, `with con:` transactions."""

    EXECUTEMANY_CHUNK = 1000  # MySQL: stay well under max_allowed_packet

    def __init__(self, raw, backend: str):
        self.raw = raw
        self.dialect = Dialect(backend)
        # Optional per-request query budget (set by the GraphQL executor):
        # bounds the work one request can do, aliases included.
        self.query_count = 0
        self.query_limit = None

    def _count(self):
        self.query_count += 1
        if self.query_limit is not None and self.query_count > self.query_limit:
            raise QueryBudgetExceeded(
                f"this request exceeded {self.query_limit} database queries; narrow it (fewer rows or aliases)"
            )

    def _sql(self, sql: str) -> str:
        return sql if self.dialect.sqlite else sql.replace("?", "%s")

    def execute(self, sql: str, params=()):
        self._count()
        if self.dialect.sqlite:
            return self.raw.execute(sql, params)
        cur = self.raw.cursor()
        cur.execute(self._sql(sql), params)
        return cur

    def executemany(self, sql: str, rows):
        self._count()
        if self.dialect.sqlite:
            return self.raw.executemany(sql, rows)
        rows = list(rows)
        cur = self.raw.cursor()
        for i in range(0, len(rows), self.EXECUTEMANY_CHUNK):
            cur.executemany(self._sql(sql), rows[i : i + self.EXECUTEMANY_CHUNK])
        return cur

    def __enter__(self):
        if self.dialect.sqlite:
            self.raw.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.dialect.sqlite:
            return self.raw.__exit__(exc_type, exc, tb)
        if exc_type is None:
            self.raw.commit()
        else:
            self.raw.rollback()
        return False

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        if not self.dialect.sqlite:
            # Autocommit is off; make sure read-only work never leaves a
            # transaction (and its consistent snapshot) open on the server.
            self.raw.rollback()
        self.raw.close()


def like_escape(text: str) -> str:
    """Escape LIKE wildcards so user text matches literally (pairs with
    Dialect.like_ci's ESCAPE '!')."""
    return text.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _connect_sqlite(path: str | None) -> Connection:
    raw = sqlite3.connect(path or DB_PATH)
    raw.row_factory = sqlite3.Row
    existing = {r[1] for r in raw.execute("PRAGMA table_info(pigean_gene_associations)")}
    if existing and "synthesis_inputs" not in existing:
        raw.close()
        raise RuntimeError(
            f"{path or DB_PATH} predates schema 003 (claims minted when queried); delete it "
            "and rebuild with scripts/load_pigean.py"
        )
    raw.executescript(SQLITE_DDL)
    # Lightweight migration for DBs created before the mint counter existed.
    cols = {r[1] for r in raw.execute("PRAGMA table_info(dapper_objects)")}
    if "times_seen" not in cols:
        raw.execute("ALTER TABLE dapper_objects ADD COLUMN last_seen_at TEXT")
        raw.execute(
            "ALTER TABLE dapper_objects ADD COLUMN times_seen INTEGER NOT NULL DEFAULT 1"
        )
    # ... and for DBs created before deposits could name a collection (008).
    cols = {r[1] for r in raw.execute("PRAGMA table_info(deposits)")}
    if "collection_key" not in cols:
        raw.execute("ALTER TABLE deposits ADD COLUMN collection_key TEXT")
        raw.execute("ALTER TABLE deposits ADD COLUMN release_version TEXT")
    # ... and for DBs created before a release recorded who pushed it (011).
    cols = {r[1] for r in raw.execute("PRAGMA table_info(collection_releases)")}
    if "published_by" not in cols:
        raw.execute("ALTER TABLE collection_releases ADD COLUMN published_by TEXT")
        raw.execute("ALTER TABLE collection_releases ADD COLUMN publisher_token_id TEXT")
    return Connection(raw, "sqlite")


def _connect_mysql() -> Connection:
    if not MYSQL_DATABASE:
        # No default: an unset name must not silently select (or a migration
        # create) some other database.
        raise RuntimeError("CLAIMS_MYSQL_DATABASE must be set when CLAIMS_MYSQL_HOST is")

    import pymysql
    import pymysql.cursors
    kwargs = dict(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        # Writes go through `with con:` (commit/rollback in __exit__), keeping
        # a deposit bundle atomic; close() rolls back any read snapshot.
        autocommit=False,
        connect_timeout=10,
    )
    if MYSQL_CA_FILE:
        kwargs["ssl"] = {"ca": MYSQL_CA_FILE}
    raw = pymysql.connect(**kwargs)
    con = Connection(raw, "mysql")
    return con


def connect(path: str | None = None) -> Connection:
    """MySQL when CLAIMS_MYSQL_HOST is set (QA/prod); SQLite otherwise.
    `path` selects a SQLite file and is ignored for MySQL."""
    if MYSQL_HOST:
        return _connect_mysql()
    return _connect_sqlite(path)
