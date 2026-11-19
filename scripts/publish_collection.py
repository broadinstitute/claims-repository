#!/usr/bin/env python3
"""Publish a collection release into the claims DB.

    .venv/bin/python scripts/publish_collection.py \\
        ~/src/research/kpn-dapper-toolkit/collections/pigean \\
        --release ~/src/research/kpn-dapper-toolkit/collections/pigean/releases/2026.11.0.yaml

Checks the manifest, templates and queries (api/collections/*.schema.json),
reads each dataset's result files, and records a DAPPER ResultCollection
release with its rows, edges, metrics and mintable claims. Load the KPN trait
registry first (scripts/load_kpn_traits.py) if a dimension declares `resolve: kpn-traits`; a collection
with its own mappings/<dimension>.tsv (as pigean has) needs no registry.
--document writes the release's DAPPER document, which dapper's linter can
check (`--profile result-collection`).

A release may give a result file by `location` (an s3:// URI) with its
`sha256` and `size` instead of a local `path`. Such files are downloaded with
the aws CLI into --workdir when their dataset is ingested, checked against the
sha256, and removed. Each dataset is stored in its own transaction and the
release is recorded last, so an interrupted publish resumes when re-run.

Target: SQLite at --db (or CLAIMS_DB), or MySQL when CLAIMS_MYSQL_HOST is set.
A MySQL target must be named again with --mysql-database, so a stray
environment variable never sends a publish to a shared database.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service import db  # noqa: E402
from service.collections.manifest import ManifestError, load_collection, load_release  # noqa: E402
from service.collections.publish import PublishError, publish  # noqa: E402


def fetch_s3(location: str, dest: Path) -> None:
    if not location.startswith("s3://"):
        raise PublishError(f"cannot fetch {location}: only s3:// locations are supported")
    for attempt in range(1, 5):  # a download can fail in passing; the sha256 check still guards what arrives
        out = subprocess.run(["aws", "s3", "cp", "--only-show-errors", location, str(dest)], capture_output=True, text=True)
        if out.returncode == 0:
            return
        if attempt < 4:
            time.sleep(5 * attempt)
    raise PublishError(f"aws s3 cp {location} failed after 4 attempts (exit {out.returncode}): {out.stderr.strip()[:500]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("collection", type=Path, help="collection directory (holds collection.yaml)")
    parser.add_argument("--release", type=Path, help="release specification YAML (file-based collections; an object "
                        "collection, kind: objects, is only registered: its releases are built from deposits)")
    parser.add_argument("--db", default=None, help="SQLite path (default: the service default)")
    parser.add_argument("--document", type=Path, help="write the release's DAPPER document here")
    parser.add_argument("--require-clean", action="store_true",
                        help="refuse when the collection directory has uncommitted changes")
    parser.add_argument("--mysql-database", help="confirms the MySQL database CLAIMS_MYSQL_* points at")
    parser.add_argument("--workdir", type=Path, help="where files given by location are downloaded "
                        "(default: a temp directory)")
    parser.add_argument("--quiet", action="store_true", help="do not print each phase")
    parser.add_argument("--max-datasets", type=int, help="ingest at most this many datasets, then stop without recording "
                        "the release (nothing is served from it); run again to continue")
    parser.add_argument("--shard", help="I/N: handle only every N-th dataset starting at the I-th (0-based), so N "
                        "processes can ingest one release side by side; a final run without --shard records it")
    args = parser.parse_args()
    if db.MYSQL_HOST:
        if args.db:
            parser.error("--db names a SQLite file, but CLAIMS_MYSQL_HOST is set: unset it, or drop --db")
        if args.mysql_database != db.MYSQL_DATABASE:
            parser.error(f"CLAIMS_MYSQL_HOST is set: this would write to MySQL database {db.MYSQL_DATABASE!r} on "
                         f"{db.MYSQL_HOST}. Name it with --mysql-database to confirm, or unset CLAIMS_MYSQL_HOST.")
        print(f"target: MySQL {db.MYSQL_DATABASE} on {db.MYSQL_HOST}")
    else:
        print(f"target: SQLite {args.db or db.DB_PATH}")
    try:
        col = load_collection(args.collection)
        if col.is_objects:
            from service.collections import objects
            from service.collections.publish import _git_info

            if args.release:
                parser.error("an object collection has no release file: register it, then open, fill and close "
                             "releases with deposits (docs/collections.md)")
            commit, dirty, _ = _git_info(col.manifest.path)
            con = db.connect(args.db)
            try:
                done = objects.register(con, col, f"{commit}{'-dirty' if dirty else ''}" if commit else None)
            finally:
                con.close()
            print(f"registered object collection {done['collection']}: queries {', '.join(done['queries']) or 'none'}")
            return 0
        if not args.release:
            parser.error("--release is required for a file-based collection")
        spec, base = load_release(args.release)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    con = db.connect(args.db)
    started = time.time()
    try:
        workdir = args.workdir or Path(tempfile.mkdtemp(prefix="collection-publish-"))
        shard = None
        if args.shard:
            i, _, n = args.shard.partition("/")
            shard = (int(i), int(n))
            if not 0 <= shard[0] < shard[1]:
                parser.error("--shard takes I/N with 0 <= I < N")
        def progress(phase: str) -> None:
            if not args.quiet and phase.startswith(("Ingesting", "Storing", "Recording")):
                print(f"[{time.time() - started:7.0f}s] {phase}", flush=True)

        result = publish(con, col, spec, base, allow_dirty=not args.require_clean, progress=progress,
                         fetch=fetch_s3, workdir=workdir, max_ingest=args.max_datasets, shard=shard)
    except (PublishError, ManifestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        con.close()
    for w in result.warnings:
        print(f"warning: {w}")
    if result.complete:
        print(f"published {col.key} {result.version} as {result.release_id} in {time.time() - started:.1f}s")
    else:
        print(f"NOT YET PUBLISHED: {col.key} {result.version} is partly ingested ({time.time() - started:.1f}s)")
    for k, v in result.stats.items():
        print(f"  {k}: {v}")
    if args.document:
        args.document.write_text(yaml.safe_dump(result.document, sort_keys=False, width=100))
        print(f"wrote {args.document}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
