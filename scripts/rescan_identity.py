#!/usr/bin/env python3
"""Recompute every stored object's id under the current DAPPER version. Read-only.

For each DAPPER-ID-1 object in dapper_objects (optionally one object collection,
class or sample), the report says:

  minted_under   the vendored versions (current and vendor/dapper-archive/<v>)
                 whose mint reproduces the stored id, oldest first, or "unknown"
  current_id     what the current version mints for the stored payload
  status         same | different | invalid_current | other_profile
  changed_slots  for different/invalid: the slots whose hashing differs between
                 minted_under and current (+slot newly hashed, -slot no longer
                 hashed, ?slot not a slot of the class under current)

The database is opened read-only (SQLite `mode=ro`). Nothing is written to it.

    .venv/bin/python scripts/rescan_identity.py --db claims.db --out-dir rescan/ [--collection cfde-genesets]
        [--class GeneSet] [--sample-per-class 200]
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from service.collections.identity_versions import DapperVersion, archived, current  # noqa: E402

PROFILE = "DAPPER-ID-1"
COLUMNS = ["id", "class", "profile", "minted_under", "current_id", "status", "changed_slots", "detail"]


def connect(db: str) -> sqlite3.Connection:
    if db.startswith("file:"):
        return sqlite3.connect(db, uri=True)
    return sqlite3.connect(f"file:{Path(db).resolve()}?mode=ro", uri=True)


def rows(con, collection: str | None, class_name: str | None, sample: int | None):
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    sql, args = "select o.id, o.class_name, o.identity_profile, o.payload from dapper_objects o", []
    where = []
    if collection:
        if "release_objects" not in tables:
            raise SystemExit("this database has no release_objects table, so it holds no object collections")
        sql += " join (select distinct object_id from release_objects where collection_key = ?) r on r.object_id = o.id"
        args.append(collection)
    if class_name:
        where.append("o.class_name = ?")
        args.append(class_name)
    if where:
        sql += " where " + " and ".join(where)
    sql += " order by o.class_name, o.id"
    seen: Counter = Counter()
    for row in con.execute(sql, args):
        if sample and seen[row[1]] >= sample:
            continue
        seen[row[1]] += 1
        yield row


def changed_slots(before: DapperVersion, after: DapperVersion, cls: str, payload: dict) -> list[str]:
    old, new = before.hashed(cls, payload), after.hashed(cls, payload) if after.has_class(cls) else {}
    out = [f"+{k}" for k in sorted(set(new) - set(old))] + [f"-{k}" for k in sorted(set(old) - set(new))]
    if after.has_class(cls):
        out += [f"?{k}" for k in after.unknown_slots(cls, payload)]
    return out


def examine(oid: str, cls: str, profile: str, payload: dict, now: DapperVersion,
            older: list[DapperVersion]) -> dict:
    row = {"id": oid, "class": cls, "profile": profile, "minted_under": "", "current_id": "",
           "status": "", "changed_slots": "", "detail": ""}
    if profile != PROFILE:
        row["status"] = "other_profile"
        return row
    reproduce = []
    for version in [*older, now]:
        try:
            if version.has_class(cls) and version.mint_id(cls, payload) == oid:
                reproduce.append(version)
        except Exception:
            continue
    origin = reproduce[0] if reproduce else None
    row["minted_under"] = " ".join(v.version for v in reproduce) or "unknown"
    if not now.has_class(cls):
        row.update(status="invalid_current", detail=f"{now.version} has no class {cls}")
    elif unknown := now.unknown_slots(cls, payload):
        row.update(status="invalid_current", detail=f"not slots of {cls} under {now.version}: {unknown}")
    else:
        try:
            row["current_id"] = now.mint_id(cls, payload)
            row["status"] = "same" if row["current_id"] == oid else "different"
        except Exception as exc:
            row.update(status="invalid_current", detail=f"mint failed: {exc}")
    if row["status"] != "same" and origin and origin is not now:
        row["changed_slots"] = " ".join(changed_slots(origin, now, cls, payload))
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(ROOT / "claims.db"), help="SQLite claims database (opened read-only)")
    parser.add_argument("--collection", help="only objects in this object collection's releases")
    parser.add_argument("--class", dest="class_name", help="only this class")
    parser.add_argument("--sample-per-class", type=int, help="at most this many objects per class")
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    now, older = current(), archived()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, Counter] = defaultdict(Counter)
    slot_changes: dict[str, Counter] = defaultdict(Counter)
    with open(args.out_dir / "rescan.tsv", "w", newline="") as fh:
        out = csv.DictWriter(fh, fieldnames=COLUMNS, delimiter="\t")
        out.writeheader()
        for oid, cls, profile, payload in rows(connect(args.db), args.collection, args.class_name,
                                               args.sample_per_class):
            row = examine(oid, cls, profile, json.loads(payload), now, older)
            out.writerow(row)
            counts[cls][row["status"]] += 1
            counts[cls][f"minted_under={row['minted_under'] or '-'}"] += 1
            for change in row["changed_slots"].split():
                slot_changes[cls][change] += 1
    summary = {"current": {"version": now.version, "commit": now.commit},
               "archived": [{"version": v.version, "commit": v.commit} for v in older],
               "filters": {"collection": args.collection, "class": args.class_name,
                           "sample_per_class": args.sample_per_class},
               "by_class": {c: dict(v) for c, v in sorted(counts.items())},
               "changed_slots": {c: dict(v) for c, v in sorted(slot_changes.items())},
               "totals": dict(sum(counts.values(), Counter()))}
    (args.out_dir / "rescan.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary["totals"], indent=2))


if __name__ == "__main__":
    main()
