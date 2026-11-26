"""012_login_deposits: a deposit or an object release made by a `claims login` has no key id. The MySQL
migration is additive (one effect per statement, the key columns only made nullable); a local SQLite file made
before it is rebuilt in place on connect, keeping every row."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from service import db as service_db

ROOT = Path(__file__).resolve().parent.parent

OLD_DEPOSITS = """CREATE TABLE deposits (
  deposit_id TEXT PRIMARY KEY,
  key_id TEXT NOT NULL,
  received_at TEXT NOT NULL,
  object_count INTEGER NOT NULL,
  collection_key TEXT,
  release_version TEXT
);
CREATE INDEX idx_deposits_key ON deposits (key_id, received_at);
CREATE TABLE object_releases (
  collection_key TEXT NOT NULL,
  version TEXT NOT NULL,
  state TEXT NOT NULL,
  definition TEXT NOT NULL,
  release_id TEXT,
  opened_at TEXT NOT NULL,
  opened_by TEXT NOT NULL,
  closed_at TEXT,
  PRIMARY KEY (collection_key, version)
);"""


def _columns(con, table):
    return {r[1]: r for r in con.execute(f"PRAGMA table_info({table})")}


def test_a_sqlite_file_from_before_012_is_rebuilt_keeping_its_rows(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(OLD_DEPOSITS)
    raw.execute("INSERT INTO deposits VALUES ('dep_1', 'key_0123456789abcdef', '2026-11-01 00:00:00', 3, 'c', '1')")
    raw.execute("INSERT INTO object_releases VALUES ('c', '1', 'open', '{}', NULL, '2026-11-01 00:00:00', "
                "'key_0123456789abcdef', NULL)")
    raw.commit()
    raw.close()

    con = service_db.connect(str(path))
    try:
        dep = _columns(con.raw, "deposits")
        assert not dep["key_id"][3] and {"publisher_token_id", "publisher_email"} <= set(dep)
        rel = _columns(con.raw, "object_releases")
        assert not rel["opened_by"][3]
        assert {"opened_by_token_id", "opened_by_email", "closed_by", "closed_by_token_id", "closed_by_email"} <= set(rel)
        row = dict(con.execute("SELECT * FROM deposits").fetchone())
        assert row["deposit_id"] == "dep_1" and row["key_id"] == "key_0123456789abcdef" and row["object_count"] == 3
        assert row["publisher_token_id"] is None
        assert con.execute("SELECT opened_by FROM object_releases").fetchone()["opened_by"] == "key_0123456789abcdef"
        indexes = {r[0] for r in con.raw.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND "
                                                 "tbl_name = 'deposits'")}
        assert "idx_deposits_key" in indexes
        # A login deposit now fits; the old table name is gone.
        con.execute("INSERT INTO deposits (deposit_id, key_id, received_at, object_count, publisher_token_id, "
                    "publisher_email) VALUES ('dep_2', NULL, '2026-11-02 00:00:00', 1, 'tok_0123456789abcdef', 'a@b.c')")
        con.commit()
        assert not con.raw.execute("SELECT 1 FROM sqlite_master WHERE name LIKE '%_012'").fetchall()
    finally:
        con.close()
    con = service_db.connect(str(path))  # connecting again changes nothing
    try:
        assert con.execute("SELECT COUNT(*) AS n FROM deposits").fetchone()["n"] == 2
    finally:
        con.close()


def test_migration_012_is_one_effect_per_statement_and_verified():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import migrate
    finally:
        sys.path.pop(0)
    statements, requires_empty = migrate.parse_migration(ROOT / "schema" / "migrations" / "012_login_deposits.sql")
    assert not requires_empty and len(statements) == 9
    for s in statements:
        assert s.startswith("ALTER TABLE ") and s.count(" COLUMN ") == 1 and "," not in s, s
        if " MODIFY " in s:  # only NOT NULL is dropped: the type and collation are 007's / 008's
            assert s.endswith("VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL")
    assert {"publisher_token_id", "publisher_email"} <= migrate.REQUIRED_COLUMNS["deposits"]
    assert migrate.REQUIRED_NULLABLE == {"deposits": {"key_id"}, "object_releases": {"opened_by"}}

    class Cursor:  # information_schema as a database migrated up to 011 would answer
        def __init__(self, nullable):
            self.nullable, self.rows = nullable, []

        def execute(self, sql, params):
            if "information_schema.columns" in sql:
                self.rows = [(t, c, "YES" if (t, c) in self.nullable else "NO")
                             for t, cols in migrate.REQUIRED_COLUMNS.items() for c in cols]
            else:
                table = params[1]
                self.rows = [(c,) for c in migrate.REQUIRED_PRIMARY_KEYS[table]]

        def fetchall(self):
            return self.rows

    assert migrate.verify_schema(Cursor({("deposits", "key_id"), ("object_releases", "opened_by")}), "db") == []
    problems = migrate.verify_schema(Cursor({("deposits", "key_id")}), "db")
    assert problems == ["object_releases: columns ['opened_by'] must accept NULL (012_login_deposits.sql)"]
