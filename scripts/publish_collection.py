#!/usr/bin/env python3
"""Publish a collection release into the claims DB.

    .venv/bin/python scripts/publish_collection.py \\
        ~/src/research/kpn-dapper-toolkit/collections/pigean \\
        --release ~/src/research/kpn-dapper-toolkit/collections/pigean/releases/2026.11.0.yaml

Checks the manifest, templates and queries (api/collections/*.schema.json),
reads each dataset's result files, and records a DAPPER ResultCollection
release with its rows, edges, metrics and mintable claims. Load the KPN trait
registry first (scripts/load_kpn_traits.py) so trait dimensions resolve.
--document writes the release's DAPPER document, which dapper's linter can
check (`--profile result-collection`).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service import db  # noqa: E402
from service.collections.manifest import ManifestError, load_collection, load_release  # noqa: E402
from service.collections.publish import PublishError, publish  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("collection", type=Path, help="collection directory (holds collection.yaml)")
    parser.add_argument("--release", type=Path, required=True, help="release specification YAML")
    parser.add_argument("--db", default=None, help="SQLite path (default: the service default)")
    parser.add_argument("--document", type=Path, help="write the release's DAPPER document here")
    parser.add_argument("--require-clean", action="store_true",
                        help="refuse when the collection directory has uncommitted changes")
    args = parser.parse_args()
    try:
        col = load_collection(args.collection)
        spec, base = load_release(args.release)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    con = db.connect(args.db)
    started = time.time()
    try:
        result = publish(con, col, spec, base, allow_dirty=not args.require_clean)
    except (PublishError, ManifestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        con.close()
    for w in result.warnings:
        print(f"warning: {w}")
    print(f"published {col.key} {result.version} as {result.release_id} in {time.time() - started:.1f}s")
    for k, v in result.stats.items():
        print(f"  {k}: {v}")
    if args.document:
        args.document.write_text(yaml.safe_dump(result.document, sort_keys=False, width=100))
        print(f"wrote {args.document}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
