#!/usr/bin/env python3
"""Vendor dapper's identity module and schema at a pinned commit.

The service mints DAPPER-ID-1 ids with dapper's own `dapper_identity.py`
against dapper's own schema, so its ids are the ids dapper's tooling computes.
Copies, from a local dapper checkout at a git ref, into vendor/dapper/ with
the same relative layout (dapper_identity.py finds dapper.yaml one directory
up), and records the ref and commit in vendor/dapper/VENDOR.json.

    .venv/bin/python scripts/vendor_dapper.py --dapper ~/src/research/dapper --ref 0.2.1-a1
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "vendor" / "dapper"
FILES = ["schema/dapper.yaml", "schema/claims.yaml", "schema/identity/dapper_identity.py",
         "schema/identity/test_vectors.json", "LICENSE"]


def git(dapper: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(dapper), *args], check=True, capture_output=True).stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dapper", type=Path, required=True, help="local broadinstitute/dapper checkout")
    parser.add_argument("--ref", required=True, help="tag, branch or commit to vendor")
    args = parser.parse_args()
    commit = git(args.dapper, "rev-parse", f"{args.ref}^{{commit}}").decode().strip()
    files = {}
    for rel in FILES:
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
