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
  - object_text (the text-search index, 009) is backfilled the same way when
    it is empty but objects exist, or always with --index-text. The backfill
    only adds rows that are missing, so it is safe to run again: do so after
    deploying the service that maintains the index, to cover objects stored
    between the migration and the deploy.

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
    # 006_collections.sql
    "collections": {"collection_key", "title", "current_release_id"},
    "collection_releases": {"release_id", "collection_key", "version", "manifest", "document",
                            "published_by", "publisher_token_id"},  # 011
    "publish_bundles": {"bundle_sha256", "collection_key", "files", "created_by", "created_at"},  # 011
    "dataset_indexes": {"dataset_id", "collection_key", "location", "files", "counts", "bundle_sha256",
                        "published_by", "publisher_token_id", "stored_at"},  # 011
    "result_rows": {"row_id", "dataset_id", "kind", "subject_id", "subject_value", "row_json", "spans"},
    "mint_contexts": {"activity_id", "context"},
    "dapper_objects": {"id", "class_name", "payload", "times_seen", "last_seen_at"},
    "claims": {"claim_id", "proposition_id", "method"},
    "pigean_gene_associations": {"phenotype", "collection", "gene", "claim_id", "proposition_id", "synthesis_inputs"},
    "pigean_gene_set_associations": {"phenotype", "collection", "gene_set", "claim_id", "proposition_id", "synthesis_inputs"},
    "pigean_gene_set_rosters": {"claim_id", "phenotype", "collection", "gene_set", "proposition_id", "synthesis_inputs"},
    "claim_memberships": {"phenotype", "collection", "gene", "gene_set", "claim_id"},
    "gene_set_entities": {"collection", "name", "dapper_id", "n_genes"},
    "object_refs": {"from_id", "to_id"},
    # 009_object_text.sql
    "object_text": {"object_id", "field", "class_name", "words"},
    "kpn_traits": {"trait_id", "legacy_id", "pigean_id", "name", "primary_concept_id"},
    "kpn_trait_mappings": {"trait_id", "target_id", "ontology", "predicate"},
    "kpn_trait_release": {"version", "commit_sha", "file_sha256"},
    "admin_auth_events": {"at", "event", "email", "detail"},
    # 007_deposit_keys.sql
    "api_keys": {"key_id", "name", "key_sha256", "created_at", "created_by", "revoked_at", "revoked_by"},
    "deposits": {"deposit_id", "key_id", "received_at", "object_count", "collection_key", "release_version",
                 "publisher_token_id", "publisher_email"},  # 012
    # 008_object_collections.sql
    "api_key_grants": {"key_id", "collection_key", "granted_at"},
    "object_collections": {"collection_key", "definition"},
    "object_releases": {"collection_key", "version", "state", "definition", "release_id", "opened_by",
                        "opened_by_token_id", "opened_by_email", "closed_by", "closed_by_token_id",
                        "closed_by_email"},  # 012
    "release_objects": {"collection_key", "version", "object_id", "class_name", "deposit_id"},
    "release_object_fields": {"collection_key", "version", "object_id", "field", "value", "num"},
    "deposit_objects": {"deposit_id", "object_id", "status"},
    "deposit_edges": {"deposit_id", "seq", "class_name", "subject_id", "object_id", "payload"},
    # 010_device_login.sql
    "device_codes": {"device_code_sha256", "user_code", "status", "client", "host", "ip", "requested_at", "expires_at",
                     "last_polled_at", "decided_by", "decided_at", "token_id"},
    "publisher_tokens": {"token_id", "token_sha256", "email", "host", "created_at", "expires_at", "last_used_at",
                         "revoked_at", "revoked_by"},
}
# Post-condition: columns that must accept NULL (012: a login deposit or release has no key id).
REQUIRED_NULLABLE = {
    "deposits": {"key_id"},
    "object_releases": {"opened_by"},
}
REQUIRED_PRIMARY_KEYS = {
    "claim_memberships": ["phenotype", "collection", "gene", "gene_set"],
    # 010_device_login.sql
    "device_codes": ["device_code_sha256"],
    "publisher_tokens": ["token_id"],
    # 006_collections.sql
    "collection_releases": ["release_id"],
    "result_claims": ["claim_id"],
    "result_edges": ["dataset_id", "kind", "from_id", "to_id"],
    "result_metrics": ["row_id", "metric"],
    # 007_deposit_keys.sql
    "api_keys": ["key_id"],
    "deposits": ["deposit_id"],
    "api_key_grants": ["key_id", "collection_key"],
    "object_releases": ["collection_key", "version"],
    "release_objects": ["collection_key", "version", "object_id"],
    "deposit_objects": ["deposit_id", "object_id"],
    "deposit_edges": ["deposit_id", "seq"],
    # 009_object_text.sql
    "object_text": ["object_id", "field"],
    # 011_publish_push.sql
    "publish_bundles": ["bundle_sha256"],
    "dataset_indexes": ["dataset_id"],
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
        "SELECT table_name, column_name, is_nullable FROM information_schema.columns WHERE table_schema = %s",
        (database,),
    )
    columns: dict[str, set] = {}
    nullable: dict[str, set] = {}
    for table, column, is_nullable in cur.fetchall():
        columns.setdefault(table, set()).add(column)
        if is_nullable == "YES":
            nullable.setdefault(table, set()).add(column)
    for table, required in REQUIRED_COLUMNS.items():
        missing = required - columns.get(table, set())
        if missing:
            problems.append(f"{table}: missing columns {sorted(missing)}")
    for table, wanted in REQUIRED_NULLABLE.items():
        strict = (wanted & columns.get(table, set())) - nullable.get(table, set())
        if strict:
            problems.append(f"{table}: columns {sorted(strict)} must accept NULL (012_login_deposits.sql)")
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
    parser.add_argument("--index-text", action="store_true",
                        help="add the object_text rows missing for stored documents (idempotent)")
    parser.add_argument("--index-text-since", default=None, metavar="UTC",
                        help="with --index-text: only objects first stored at or after this UTC time "
                             "('YYYY-MM-DD HH:MM:SS'), to cover what was stored while a backfill ran")
    parser.add_argument("--index-text-after", default="", metavar="ID",
                        help="with --index-text: resume an interrupted run after this object id (the last one a "
                             "progress line printed)")
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
        indexed = svc.execute("SELECT COUNT(*) AS c FROM object_text").fetchone()["c"]
        svc.commit()
        if objects and (args.index_text or not indexed):
            from service.text_index import rebuild_object_text

            batches = [0]

            def progress(last, offered):
                batches[0] += 1
                if batches[0] % 100 == 0:
                    print(f"  object_text: through {last} ({offered} rows offered)", flush=True)
            offered = rebuild_object_text(svc, progress=progress, after=args.index_text_after, since=args.index_text_since)
            now = svc.execute("SELECT COUNT(*) AS c FROM object_text").fetchone()["c"]
            print(f"indexed object_text: {offered} rows offered from {objects} objects; {indexed} rows before, {now} now")
    finally:
        svc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
