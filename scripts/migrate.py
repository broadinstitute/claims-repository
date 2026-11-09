#!/usr/bin/env python3
"""Create the project database (cyaka-prefixed, per the shared-cluster grant)
and apply schema/migrations/*.sql in order, recording each in
schema_migrations. Idempotent: applied versions are skipped.

Connection comes from the same env vars the service uses:
  CLAIMS_MYSQL_HOST / CLAIMS_MYSQL_PORT / CLAIMS_MYSQL_USER /
  CLAIMS_MYSQL_PASSWORD / CLAIMS_MYSQL_DATABASE / CLAIMS_MYSQL_CA_FILE

Run from a machine that can reach the cluster (the laptop can; QA tasks can).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "schema" / "migrations"


def connect(database: str | None):
    kwargs = dict(
        host=os.environ["CLAIMS_MYSQL_HOST"],
        port=int(os.environ.get("CLAIMS_MYSQL_PORT", "3306")),
        user=os.environ.get("CLAIMS_MYSQL_USER", "cyaka"),
        password=os.environ["CLAIMS_MYSQL_PASSWORD"],
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=10,
    )
    if database:
        kwargs["database"] = database
    ca = os.environ.get("CLAIMS_MYSQL_CA_FILE")
    if ca:
        kwargs["ssl"] = {"ca": ca}
    return pymysql.connect(**kwargs)


def main() -> int:
    database = os.environ.get("CLAIMS_MYSQL_DATABASE", "cyaka_claims_repository")
    if not database.startswith("cyaka_"):
        print(f"refusing: {database!r} is outside the cyaka_ grant prefix", file=sys.stderr)
        return 1

    con = connect(None)
    with con.cursor() as cur:
        # utf8mb4_bin default per the cluster's readiness guidance: exact,
        # case-sensitive comparisons for opaque IDs and digests.
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS `{database}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"
        )
    con.close()

    con = connect(database)
    applied = set()
    with con.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  version VARCHAR(128) NOT NULL PRIMARY KEY,"
            "  applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ") ENGINE=InnoDB"
        )
        cur.execute("SELECT version FROM schema_migrations")
        applied = {row[0] for row in cur.fetchall()}

    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name in applied:
            print(f"skip   {path.name} (applied)")
            continue
        sql = "\n".join(
            line for line in path.read_text().splitlines()
            if not line.lstrip().startswith("--")
        )
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        with con.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
            cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
        print(f"applied {path.name} ({len(statements)} statements)")

    with con.cursor() as cur:
        cur.execute("SHOW TABLES")
        print("tables:", ", ".join(sorted(row[0] for row in cur.fetchall())))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
