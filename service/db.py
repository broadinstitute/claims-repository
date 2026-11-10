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
    return Connection(raw, "sqlite")


def _connect_mysql() -> Connection:
    import pymysql
    import pymysql.cursors

    if not MYSQL_DATABASE:
        # No default: an unset name must not silently select (or a migration
        # create) some other database.
        raise RuntimeError("CLAIMS_MYSQL_DATABASE must be set when CLAIMS_MYSQL_HOST is")
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
