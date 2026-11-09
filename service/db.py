"""Storage layer with two backends behind one tiny wrapper:

- SQLite (default): local development and tests; tables created on connect.
- Aurora MySQL 8 (when CLAIMS_MYSQL_HOST is set): the QA/prod deployment via
  dig-service-platform, database `cyaka_claims_repository` on the shared
  cluster (docs/query-graph-design.md §5/§6). Schema is applied by
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
MYSQL_DATABASE = os.environ.get("CLAIMS_MYSQL_DATABASE", "cyaka_claims_repository")
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
  PRIMARY KEY (phenotype, collection, gene)
);
CREATE INDEX IF NOT EXISTS idx_pga_combined
  ON pigean_gene_associations (phenotype, collection, combined);
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
  PRIMARY KEY (phenotype, collection, gene_set)
);
CREATE INDEX IF NOT EXISTS idx_pgsa_bu
  ON pigean_gene_set_associations (phenotype, collection, beta_uncorrected);
CREATE TABLE IF NOT EXISTS claim_memberships (
  collection TEXT NOT NULL,
  gene TEXT NOT NULL,
  gene_set TEXT NOT NULL,
  claim_id TEXT NOT NULL,
  PRIMARY KEY (collection, gene, gene_set)
);
CREATE INDEX IF NOT EXISTS idx_cm_set ON claim_memberships (collection, gene_set);
CREATE TABLE IF NOT EXISTS gene_set_entities (
  collection TEXT NOT NULL,
  name TEXT NOT NULL,
  dapper_id TEXT NOT NULL,
  n_genes INTEGER,
  PRIMARY KEY (collection, name)
);
-- Lazy assertion (2026-11-09, Chase): loader-built claims stage here,
-- precomputed but unasserted. A claim moves to dapper_objects (and the
-- claims registry) the first time the scientific query surface serves it —
-- minting the claim to justify the query response. times_seen then counts
-- assertions, not re-deposits.
CREATE TABLE IF NOT EXISTS pending_claims (
  claim_id TEXT PRIMARY KEY,
  proposition_id TEXT NOT NULL,
  method TEXT NOT NULL,
  identity_profile TEXT NOT NULL,
  payload TEXT NOT NULL
);
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

    def je(self, col: str, path: str) -> str:
        """json_extract returning a comparable string."""
        if self.sqlite:
            return f"json_extract({col}, '$.{path}')"
        return f"JSON_UNQUOTE(JSON_EXTRACT({col}, '$.{path}'))"


class Connection:
    """Uniform surface over sqlite3 / PyMySQL: execute with `?` placeholders,
    dict-style rows, `with con:` transactions."""

    EXECUTEMANY_CHUNK = 1000  # MySQL: stay well under max_allowed_packet

    def __init__(self, raw, backend: str):
        self.raw = raw
        self.dialect = Dialect(backend)

    def _sql(self, sql: str) -> str:
        return sql if self.dialect.sqlite else sql.replace("?", "%s")

    def execute(self, sql: str, params=()):
        if self.dialect.sqlite:
            return self.raw.execute(sql, params)
        cur = self.raw.cursor()
        cur.execute(self._sql(sql), params)
        return cur

    def executemany(self, sql: str, rows):
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

    def close(self):
        if not self.dialect.sqlite:
            # Autocommit is off; make sure read-only work never leaves a
            # transaction (and its consistent snapshot) open on the server.
            self.raw.rollback()
        self.raw.close()


def _connect_sqlite(path: str | None) -> Connection:
    raw = sqlite3.connect(path or DB_PATH)
    raw.row_factory = sqlite3.Row
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
