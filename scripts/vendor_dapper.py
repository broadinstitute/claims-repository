#!/usr/bin/env python3
"""Vendor dapper's identity module and schema at a pinned commit.

The service mints DAPPER-ID-1 ids with dapper's own `dapper_identity.py`
against dapper's own schema, so its ids are the ids dapper's tooling computes.
Copies, from a local dapper checkout at a git ref, into vendor/dapper/ with
the same relative layout (identity/dapper_identity.py finds schema/dapper.yaml
one directory up), and records the ref and commit in vendor/dapper/VENDOR.json.
Every schema module at the ref is copied (0.3 splits the schema into modules
that dapper.yaml imports), and the vendor directory is cleared first so no file
from an older layout survives. The version being replaced is first copied to
vendor/dapper-archive/<its version>/, so ids minted under it can still be
recomputed (docs/dapper-version-migration.md).

    .venv/bin/python scripts/vendor_dapper.py --dapper ~/src/research/dapper --ref 0.3.0-a1
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "vendor" / "dapper"
ARCHIVE = ROOT / "vendor" / "dapper-archive"


def archive_current() -> str | None:
    """Copy the vendored version being replaced to vendor/dapper-archive/<version>/."""
    schema = DEST / "schema" / "dapper.yaml"
    if not schema.exists():
        return None
    version = re.search(r"(?m)^version:\s*(\S+)", schema.read_text()).group(1)
    target = ARCHIVE / version
    if not target.exists():
        shutil.copytree(DEST, target, ignore=shutil.ignore_patterns("__pycache__"))
    return version
FILES = ["identity/dapper_identity.py", "identity/test_vectors.json", "LICENSE"]


def git(dapper: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(dapper), *args], check=True, capture_output=True).stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dapper", type=Path, required=True, help="local broadinstitute/dapper checkout")
    parser.add_argument("--ref", required=True, help="tag, branch or commit to vendor")
    args = parser.parse_args()
    commit = git(args.dapper, "rev-parse", f"{args.ref}^{{commit}}").decode().strip()
    modules = [p for p in git(args.dapper, "ls-tree", "--name-only", commit, "schema/").decode().split()
               if p.endswith(".yaml")]
    if "schema/dapper.yaml" not in modules:
        raise SystemExit(f"{args.ref} has no schema/dapper.yaml")
    archived = archive_current()
    if archived:
        print(f"kept {archived} in {(ARCHIVE / archived).relative_to(ROOT)}")
    if DEST.exists():
        shutil.rmtree(DEST)
    files = {}
    for rel in sorted(modules) + FILES:
        data = git(args.dapper, "show", f"{commit}:{rel}")
        out = DEST / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        files[rel] = hashlib.sha256(data).hexdigest()
    (DEST / "VENDOR.json").write_text(json.dumps(
        {"repository": "broadinstitute/dapper", "ref": args.ref, "commit": commit, "files": files},
        indent=2) + "\n")
    print(f"vendored dapper {args.ref} ({commit[:10]}) into {DEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
