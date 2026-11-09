#!/usr/bin/env python3
"""Consistency audit over every stored dapper object (2026-11-09, per Chase:
the objects resolver must return consistent dapper object content for every
class — claims, propositions, gene sets, scores, evidence, locators, ...).

Checks, per object:
  1. payload parses as JSON and is an object
  2. payload.id == row id and payload.class_name == row class_name
  3. class_name is a known DAPPER class
  4. the class's minimum expected slots are present
  5. every dapper:-prefixed reference in the payload resolves to a stored
     object (referential integrity)

Runs against whichever backend the CLAIMS_* env selects (SQLite by default,
Aurora with CLAIMS_MYSQL_*). Exits non-zero on any violation.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service import db  # noqa: E402

DAPPER_ID_RE = re.compile(r"^dapper:[A-Za-z]+\.[A-Za-z0-9]+$")

# Minimum slots a stored instance of each class must carry. Deliberately the
# floor, not the full profile: profile validation belongs to the dapper
# tooling; this audit catches structurally broken or mislabeled records.
REQUIRED_SLOTS = {
    "Claim": {"proposition"},
    "Proposition": {"statement", "subject_entity", "relation", "object_entity"},
    "ClaimScore": {"score_kind", "metric", "value", "interpretation"},
    "EvidenceItem": set(),  # grounding checked separately (any-of)
    "ValueLocator": {"file", "byte_start", "byte_length", "value_text"},
    "GeneSet": {"name", "member_type"},
    "GeneSetCollection": {"name", "member_type"},
    "File": {"source_uri", "sha256"},
    "Activity": {"description"},
    "Agent": {"description"},
}
EVIDENCE_GROUNDINGS = ("source_claims", "value_locators", "reference", "snippet")


def dapper_refs(value, out):
    if isinstance(value, str):
        if DAPPER_ID_RE.match(value):
            out.append(value)
    elif isinstance(value, list):
        for v in value:
            dapper_refs(v, out)
    elif isinstance(value, dict):
        for v in value.values():
            dapper_refs(v, out)


def main() -> int:
    con = db.connect()
    problems = []
    counts: Counter = Counter()
    ids = {r["id"] for r in con.execute("SELECT id FROM dapper_objects")}
    print(f"objects: {len(ids)}")

    rows = con.execute("SELECT id, class_name, payload FROM dapper_objects")
    checked_refs = 0
    for row in rows:
        oid, cls = row["id"], row["class_name"]
        counts[cls] += 1
        try:
            payload = json.loads(row["payload"])
            assert isinstance(payload, dict)
        except Exception:
            problems.append(f"{oid}: payload is not a JSON object")
            continue
        if payload.get("id") != oid:
            problems.append(f"{oid}: payload.id mismatch ({payload.get('id')})")
        if payload.get("class_name") != cls:
            problems.append(f"{oid}: payload.class_name mismatch ({payload.get('class_name')})")
        if cls not in REQUIRED_SLOTS:
            problems.append(f"{oid}: unknown class {cls}")
            continue
        missing = REQUIRED_SLOTS[cls] - payload.keys()
        if missing:
            problems.append(f"{oid}: missing slots {sorted(missing)}")
        if cls == "EvidenceItem" and not any(payload.get(g) for g in EVIDENCE_GROUNDINGS):
            problems.append(f"{oid}: EvidenceItem has no grounding ({'/'.join(EVIDENCE_GROUNDINGS)})")
        refs: list[str] = []
        dapper_refs({k: v for k, v in payload.items() if k != "id"}, refs)
        for ref in refs:
            checked_refs += 1
            if ref not in ids:
                problems.append(f"{oid}: dangling reference {ref}")

    con.close()
    for cls in sorted(counts):
        print(f"  {cls}: {counts[cls]}")
    print(f"references checked: {checked_refs}")
    if problems:
        print(f"\nPROBLEMS ({len(problems)}):")
        for p in problems[:50]:
            print(" ", p)
        if len(problems) > 50:
            print(f"  ... and {len(problems) - 50} more")
        return 1
    print("consistent: every object parses, matches its row, carries its class's required slots, and every dapper reference resolves")
    return 0


if __name__ == "__main__":
    sys.exit(main())
