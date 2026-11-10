#!/usr/bin/env python3
"""Create the project database (cyaka-prefixed, per the shared-cluster grant)
and apply schema/migrations/*.sql in order, recording each in
schema_migrations. Idempotent: applied versions are skipped.

Safety rules:
  - Each migration statement has ONE effect, so on a re-run "already exists"
    errors (1050 table, 1060 column, 1061 key) mean "already applied" and are
    tolerated; any other error stops the run.
  - A migration may declare `-- requires-empty: table, table, ...`; it is
    refused (not applied) if any of those tables holds rows.
  - After applying, the resulting schema is VERIFIED against the columns the
    service needs (information_schema); a mismatch is an error.
  - object_refs is backfilled from stored documents when it is empty but
    objects exist (or always, with --rebuild-refs).

Connection comes from the same env vars the service uses:
  CLAIMS_MYSQL_HOST / CLAIMS_MYSQL_PORT / CLAIMS_MYSQL_USER /
  CLAIMS_MYSQL_PASSWORD / CLAIMS_MYSQL_DATABASE / CLAIMS_MYSQL_CA_FILE
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "schema" / "migrations"
sys.path.insert(0, str(ROOT))

# MySQL errors meaning a single-effect DDL statement's effect already exists.
ALREADY_APPLIED = {1050, 1060, 1061}

# Post-condition: table -> columns the service requires.
REQUIRED_COLUMNS = {
    "dapper_objects": {"id", "class_name", "payload", "times_seen", "last_seen_at"},
    "claims": {"claim_id", "proposition_id", "method"},
    "pigean_gene_associations": {"phenotype", "collection", "gene", "claim_id", "proposition_id", "synthesis_inputs"},
    "pigean_gene_set_associations": {"phenotype", "collection", "gene_set", "claim_id", "proposition_id", "synthesis_inputs"},
    "pigean_gene_set_rosters": {"claim_id", "phenotype", "collection", "gene_set", "proposition_id", "synthesis_inputs"},
    "claim_memberships": {"phenotype", "collection", "gene", "gene_set", "claim_id"},
    "gene_set_entities": {"collection", "name", "dapper_id", "n_genes"},
    "object_refs": {"from_id", "to_id"},
    "kpn_traits": {"trait_id", "legacy_id", "pigean_id", "name", "primary_concept_id"},
    "kpn_trait_mappings": {"trait_id", "target_id", "ontology", "predicate"},
    "kpn_trait_release": {"version", "commit_sha", "file_sha256"},
    "admin_auth_events": {"at", "event", "email", "detail"},
}
REQUIRED_PRIMARY_KEYS = {
    "claim_memberships": ["phenotype", "collection", "gene", "gene_set"],
}


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


def parse_migration(path: Path) -> tuple[list[str], list[str]]:
    """(statements, requires_empty_tables)."""
    requires_empty: list[str] = []
    body = []
    for line in path.read_text().splitlines():
        stripped = line.lstrip()
        if stripped.startswith("--"):
            directive = stripped[2:].strip()
            if directive.startswith("requires-empty:"):
                requires_empty += [t.strip() for t in directive.split(":", 1)[1].split(",") if t.strip()]
            continue
        body.append(line)
    statements = [s.strip() for s in "\n".join(body).split(";") if s.strip()]
    return statements, requires_empty


def verify_schema(cur, database: str) -> list[str]:
    problems = []
    cur.execute(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = %s",
        (database,),
    )
    columns: dict[str, set] = {}
    for table, column in cur.fetchall():
        columns.setdefault(table, set()).add(column)
    for table, required in REQUIRED_COLUMNS.items():
        missing = required - columns.get(table, set())
        if missing:
            problems.append(f"{table}: missing columns {sorted(missing)}")
    for table, key in REQUIRED_PRIMARY_KEYS.items():
        cur.execute(
            "SELECT column_name FROM information_schema.key_column_usage "
            "WHERE table_schema = %s AND table_name = %s AND constraint_name = 'PRIMARY' "
            "ORDER BY ordinal_position",
            (database, table),
        )
        actual = [r[0] for r in cur.fetchall()]
        if actual != key:
            problems.append(f"{table}: primary key is {actual}, expected {key}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rebuild-refs", action="store_true",
                        help="recompute object_refs from every stored document")
    args = parser.parse_args()

    database = os.environ.get("CLAIMS_MYSQL_DATABASE")
    if not database:
        print("refusing: set CLAIMS_MYSQL_DATABASE (no default, so an unset name never creates or "
              "migrates some other database)", file=sys.stderr)
        return 1
    print(f"target database: {database}")
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
        statements, requires_empty = parse_migration(path)
        with con.cursor() as cur:
            for table in requires_empty:
                cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
                            (database, table))
                if cur.fetchone()[0]:
                    cur.execute(f"SELECT COUNT(*) FROM `{table}`")
                    rows = cur.fetchone()[0]
                    if rows:
                        print(f"refusing {path.name}: it requires an empty `{table}` ({rows} rows). Data loaded "
                              "before it predates the new model (claims stored eagerly, no synthesis inputs), "
                              "and emptying only the index tables would leave those claims behind: recreate "
                              "the database, migrate, then reload (load_kpn_traits.py, then load_pigean.py).",
                              file=sys.stderr)
                        con.close()
                        return 1
            for statement in statements:
                try:
                    cur.execute(statement)
                except (pymysql.err.OperationalError, pymysql.err.InternalError) as exc:
                    if exc.args and exc.args[0] in ALREADY_APPLIED:
                        print(f"  already applied ({exc.args[0]}): {statement.splitlines()[0][:80]}")
                        continue
                    raise
            cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
        print(f"applied {path.name} ({len(statements)} statements)")

    with con.cursor() as cur:
        problems = verify_schema(cur, database)
        cur.execute("SHOW TABLES")
        tables = {row[0] for row in cur.fetchall()}
    con.close()
    print("tables:", ", ".join(sorted(tables)))
    if problems:
        print("SCHEMA VERIFICATION FAILED:", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        return 1
    print("schema verified")

    from service import db
    from service.refs import rebuild_object_refs

    svc = db.connect()
    try:
        objects = svc.execute("SELECT COUNT(*) AS c FROM dapper_objects").fetchone()["c"]
        refs = svc.execute("SELECT COUNT(*) AS c FROM object_refs").fetchone()["c"]
        if objects and (args.rebuild_refs or not refs):
            with svc:
                written = rebuild_object_refs(svc)
            print(f"rebuilt object_refs: {written} references from {objects} objects")
    finally:
        svc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
