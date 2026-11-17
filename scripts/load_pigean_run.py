#!/usr/bin/env python3
"""Load a whole PIGEAN aggregator run, one phenotype at a time, from a run
specification (loads/*.json).

    .venv/bin/python scripts/load_pigean_run.py loads/<run>.json --db /tmp/scratch.db --only IBD,AD
    .venv/bin/python scripts/load_pigean_run.py loads/<run>.json --list

For each phenotype folder under
  <prefix>/combined_gene_stats/<library>/<trait_group>/<phenotype>/
it downloads that phenotype's three result files (aws CLI) into a working
directory, runs scripts/load_pigean.py's load() on them with the
specification's source strings and row thresholds, appends one line to a
receipt file, and deletes the downloads. A phenotype the receipt already
records as loaded is skipped, so an interrupted run resumes; a failure stops
the run.

The specification pins everything that enters a content address (file
locations, the run and library descriptions). The thresholds do NOT enter any
address: lowering one later and re-running adds rows and re-addresses nothing.

Phenotype folders the KPN trait registry does not know are skipped unless the
specification says `"unregistered_traits": "load"`; they are then stored under
the raw folder name, which must fit the phenotype columns (ASCII, at most 64
characters) or the folder is skipped and recorded. Load the registry first
(scripts/load_kpn_traits.py).

Target: SQLite at --db (or CLAIMS_DB), or MySQL when CLAIMS_MYSQL_HOST is set.
A MySQL target must be named again with --mysql-database, so a stray
environment variable never sends a load to a shared database.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from service import db  # noqa: E402

KINDS = ("combined_gene_stats", "combined_gene_set_stats", "gene_gene_set_stats")
PHENOTYPE_KEY_MAX = 64  # the ascii VARCHAR(64) phenotype columns (schema/migrations/001_init.sql)


def storable_key(key: str) -> bool:
    """A raw folder name can be a phenotype key only if the index columns hold it."""
    return key.isascii() and len(key) <= PHENOTYPE_KEY_MAX
SPEC_KEYS = {"prefix", "library", "trait_group", "run_description", "library_origin", "keep", "unregistered_traits"}


def loader():
    spec = importlib.util.spec_from_file_location("load_pigean", ROOT / "scripts" / "load_pigean.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_spec(path: Path) -> dict:
    spec = json.loads(path.read_text())
    unknown, missing = sorted(set(spec) - SPEC_KEYS), sorted(SPEC_KEYS - {"keep", "unregistered_traits"} - set(spec))
    if unknown or missing:
        raise SystemExit(f"{path}: unknown key(s) {unknown}, missing key(s) {missing}")
    keep = spec.get("keep") or {}
    if set(keep) - {"min_combined", "min_beta_uncorrected"}:
        raise SystemExit(f"{path}: keep takes only min_combined and min_beta_uncorrected")
    if spec.get("unregistered_traits", "skip") not in ("skip", "load"):
        raise SystemExit(f"{path}: unregistered_traits must be 'skip' or 'load'")
    spec["prefix"] = spec["prefix"].rstrip("/")
    return spec


def _aws(*args: str) -> str:
    out = subprocess.run(["aws", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args)} failed: {out.stderr.strip()[:500]}")
    return out.stdout


def list_phenotypes(spec: dict) -> list[str]:
    """Phenotype folders that hold a combined_gene_stats.json."""
    base = f"{spec['prefix']}/combined_gene_stats/{spec['library']}/{spec['trait_group']}/"
    found = []
    for line in _aws("s3", "ls", "--recursive", base).splitlines():
        key = line.split(None, 3)[3]
        if key.endswith("/combined_gene_stats.json"):
            found.append(key.split("/")[-2])
    return sorted(found)


def fetch(spec: dict, phenotype: str, into: Path) -> dict[str, Path]:
    into.mkdir(parents=True, exist_ok=True)
    paths = {}
    for kind in KINDS:
        paths[kind] = into / f"{kind}.json"
        _aws("s3", "cp", "--only-show-errors",
             f"{spec['prefix']}/{kind}/{spec['library']}/{spec['trait_group']}/{phenotype}/{kind}.json", str(paths[kind]))
    return paths


def receipt_done(path: Path, target: str, spec_name: str, keep: dict) -> set[str]:
    """Phenotypes the receipt records as loaded INTO THIS TARGET, from this
    specification, with these thresholds. A receipt line for another database
    or other thresholds never counts: re-running at a lower threshold reloads
    every phenotype (which only adds rows)."""
    done = set()
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                record = json.loads(line)
                if (record.get("status") == "loaded" and record.get("target") == target
                        and record.get("spec") == spec_name and record.get("keep") == keep):
                    done.add(record["phenotype"])
    return done


def target_description(args) -> str:
    if db.MYSQL_HOST:
        if args.db:
            raise SystemExit("--db names a SQLite file, but CLAIMS_MYSQL_HOST is set: unset it, or drop --db")
        if args.mysql_database != db.MYSQL_DATABASE:
            raise SystemExit(f"CLAIMS_MYSQL_HOST is set: this would write to MySQL database {db.MYSQL_DATABASE!r} on "
                             f"{db.MYSQL_HOST}. Name it with --mysql-database to confirm, or unset CLAIMS_MYSQL_HOST.")
        return f"MySQL {db.MYSQL_DATABASE} on {db.MYSQL_HOST}"
    return f"SQLite {args.db or db.DB_PATH}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("spec", type=Path, help="run specification (loads/*.json)")
    parser.add_argument("--db", default=None, help="SQLite path (default: the service default)")
    parser.add_argument("--mysql-database", help="confirms the MySQL database CLAIMS_MYSQL_* points at")
    parser.add_argument("--only", help="comma-separated phenotype folders (default: every folder of the run)")
    parser.add_argument("--limit", type=int, help="stop after this many phenotypes are loaded in this invocation")
    parser.add_argument("--receipt", type=Path, help="receipt file, one JSON line per phenotype "
                        "(default: <spec name>.receipt.jsonl in the working directory)")
    parser.add_argument("--workdir", type=Path, help="where result files are downloaded (default: a temp directory)")
    parser.add_argument("--list", action="store_true", help="list the run's phenotypes and how each resolves, then exit")
    args = parser.parse_args()

    spec = read_spec(args.spec)
    mod = loader()
    keep = mod.Keep(**(spec.get("keep") or {}))
    policy = spec.get("unregistered_traits", "skip")
    target = target_description(args)

    phenotypes = list_phenotypes(spec)
    if args.only:
        wanted = [p for p in args.only.split(",") if p]
        absent = sorted(set(wanted) - set(phenotypes))
        if absent:
            raise SystemExit(f"not in the run: {absent}")
        phenotypes = wanted
    con = db.connect(args.db)
    try:
        resolver = mod.TraitResolver(con)
    finally:
        con.close()
    if not resolver.by_key:
        raise SystemExit("the KPN trait registry is empty in the target: run scripts/load_kpn_traits.py first")
    registered = [p for p in phenotypes if p in resolver.by_key]
    unregistered = [p for p in phenotypes if p not in resolver.by_key]
    print(f"target: {target}")
    print(f"run: {spec['prefix']} library {spec['library']} trait group {spec['trait_group']}; keep {keep._asdict()}")
    unstorable = [p for p in unregistered if not storable_key(p)]
    print(f"phenotypes: {len(phenotypes)} ({len(registered)} in the trait registry, {len(unregistered)} not: "
          f"{'skipped' if policy == 'skip' else 'loaded under their folder names'})")
    if policy == "load" and unstorable:
        print(f"skipped, folder name does not fit the phenotype columns: {unstorable}")
    if args.list:
        for p in phenotypes:
            print(f"{p}\t{resolver.by_key[p][0] if p in resolver.by_key else '(unregistered)'}")
        return 0

    todo = [p for p in phenotypes if p not in unstorable] if policy == "load" else registered
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="pigean-run-"))
    workdir.mkdir(parents=True, exist_ok=True)
    receipt = args.receipt or workdir / f"{args.spec.stem}.receipt.jsonl"
    done = receipt_done(receipt, target, args.spec.name, keep._asdict())
    skips_recorded = receipt.exists() and '"skipped-unregistered"' in receipt.read_text()
    print(f"receipt: {receipt} ({len(done & set(todo))} already loaded)")
    loaded = 0
    started = time.time()
    with open(receipt, "a") as out:
        def record(entry):
            out.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "target": target,
                                  "spec": args.spec.name, "keep": keep._asdict(), **entry}) + "\n")
            out.flush()

        if policy == "skip" and not skips_recorded:
            for p in unregistered:
                record({"phenotype": p, "status": "skipped-unregistered"})
        if policy == "load" and '"skipped-unstorable-key"' not in (receipt.read_text() if receipt.exists() else ""):
            for p in unstorable:
                record({"phenotype": p, "status": "skipped-unstorable-key"})
        for n, phenotype in enumerate(todo, 1):
            if phenotype in done:
                continue
            if args.limit is not None and loaded >= args.limit:
                break
            folder = workdir / "files" / phenotype
            t0 = time.time()
            try:
                paths = fetch(spec, phenotype, folder)
                source = mod.Source.under(spec["prefix"], spec["library"], spec["trait_group"], phenotype,
                                          run_description=spec["run_description"], library_origin=spec["library_origin"])
                # A run writes an empty gene-set file for a phenotype with no gene set kept; its
                # membership file then has nothing to describe.
                membership = paths["gene_gene_set_stats"] if paths["combined_gene_set_stats"].stat().st_size else None
                con = db.connect(args.db)
                try:
                    with contextlib.redirect_stdout(io.StringIO()):  # the loader's own summary; the receipt has it
                        stats = mod.load(con, paths["combined_gene_stats"], paths["combined_gene_set_stats"],
                                         membership, source, keep)
                finally:
                    con.close()
            except BaseException as exc:
                record({"phenotype": phenotype, "status": "failed", "error": f"{type(exc).__name__}: {exc}"[:1000]})
                print(f"FAILED on {phenotype} ({n} of {len(todo)}): {type(exc).__name__}: {exc}", file=sys.stderr)
                raise
            finally:
                shutil.rmtree(folder, ignore_errors=True)
            loaded += 1
            record({"phenotype": phenotype, "status": "loaded", "trait_id": resolver(phenotype)[0],
                    "seconds": round(time.time() - t0, 1), "membership": membership is not None,
                    "python_none_lines": sum(mod.PY_NONE_LINES.values()), **stats})
            print(f"[{n}/{len(todo)}] {phenotype}: {stats['gene_claims']} gene rows, {stats['gene_set_claims']} gene-set rows, "
                  f"{stats['membership_pairs']} pairs, {stats['objects']} objects in {time.time() - t0:.1f}s", flush=True)
    print(f"loaded {loaded} phenotype(s) in {time.time() - started:.0f}s; receipt {receipt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
