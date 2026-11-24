#!/usr/bin/env python3
"""Consistency audit over the whole store (2026-11-09, per Chase: the
resolver must return consistent dapper object content for every class, and
claims minted on query must be exactly the claims their data implies).

Checks:
  1. every stored object parses, matches its row (id, class_name), carries
     its class's minimum slots, and every dapper reference in it resolves;
  2. object_refs equals the references recomputed from every document
     (no missing, no stale rows) — the index behind /v1/objects/search;
  3. EVERY template-backed index row's claim synthesizes from the row + its
     synthesis_inputs to exactly its precomputed content address and its
     recorded proposition_id; when the claim is already stored (asserted), the
     stored document is identical to the synthesized one. Index rows written
     by REST deposit hints (no synthesis_inputs) must name a stored claim.
  4. every claim_memberships edge is backed by a roster row or a stored claim,
     and an edge citing a loader-written roster (loaded roster claim, deposited
     claim on a loaded roster's proposition, or has_members claim about a
     loaded GeneSet) is for its own gene set and names a gene that GeneSet
     lists.

Runs against whichever backend the CLAIMS_* env selects (SQLite by default,
Aurora with CLAIMS_MYSQL_*). Exits non-zero on any violation.
--repair-refs rebuilds object_refs from the stored documents first (the
repair path for a reference index that drifted, e.g. after out-of-band
writes); stale rows are removed, missing ones added.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service import db  # noqa: E402
from service import pigean_template as tpl  # noqa: E402
from service.app import CLAIM_SOURCES, ClaimSynthesisError, synthesize_claim  # noqa: E402
from service.refs import dapper_refs  # noqa: E402
from service.text_index import text_rows  # noqa: E402

# Minimum slots a stored instance of each class must carry — the floor, not
# the full profile (profile validation belongs to the dapper tooling).
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


def main() -> int:
    if "--repair-refs" in sys.argv[1:]:
        from service.refs import rebuild_object_refs

        repair = db.connect()
        try:
            with repair:
                repair.execute("DELETE FROM object_refs")
                written = rebuild_object_refs(repair)
            print(f"repaired object_refs: {written} references rebuilt from stored documents")
        finally:
            repair.close()
    con = db.connect()
    problems: list[str] = []
    counts: Counter = Counter()
    ids = {r["id"] for r in con.execute("SELECT id FROM dapper_objects")}
    print(f"objects: {len(ids)}")

    # 1 + 2: documents and the reference index.
    expected_refs: set[tuple[str, str]] = set()
    expected_text: set[tuple[str, str, str, str]] = set()
    checked_refs = 0
    for row in con.execute("SELECT id, class_name, payload FROM dapper_objects"):
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
        # A plain DAPPER node (DAPPER-ID-1: a deposit, a collection claim) carries no class_name; its class is
        # in its id. A legacy document carries one, and it must agree with the row.
        declared = payload.get("class_name")
        if declared is None:
            declared = oid.partition(":")[2].partition(".")[0] if oid.startswith("dapper:") else None
        if declared != cls:
            problems.append(f"{oid}: payload.class_name mismatch ({payload.get('class_name')})")
        # References first: every stored object's refs belong in the index,
        # whatever its class.
        expected_text.update(text_rows(oid, cls, payload))
        for ref in dapper_refs(payload):
            checked_refs += 1
            expected_refs.add((oid, ref))
            if ref not in ids:
                problems.append(f"{oid}: dangling reference {ref}")
        if cls not in REQUIRED_SLOTS:
            problems.append(f"{oid}: unknown class {cls}")
            continue
        missing = REQUIRED_SLOTS[cls] - payload.keys()
        if missing:
            problems.append(f"{oid}: missing slots {sorted(missing)}")
        if cls == "EvidenceItem" and not any(payload.get(g) for g in EVIDENCE_GROUNDINGS):
            problems.append(f"{oid}: EvidenceItem has no grounding ({'/'.join(EVIDENCE_GROUNDINGS)})")

    stored_refs = {(r["from_id"], r["to_id"]) for r in con.execute("SELECT from_id, to_id FROM object_refs")}
    for from_id, to_id in sorted(expected_refs - stored_refs)[:20]:
        problems.append(f"object_refs missing {from_id} -> {to_id}")
    for from_id, to_id in sorted(stored_refs - expected_refs)[:20]:
        problems.append(f"object_refs stale {from_id} -> {to_id}")
    if expected_refs != stored_refs:
        problems.append(
            f"object_refs differs from recomputed refs: {len(expected_refs - stored_refs)} missing, "
            f"{len(stored_refs - expected_refs)} stale"
        )

    # 2b: the text index equals the words recomputed from every document.
    stored_text = {(r["object_id"], r["class_name"], r["field"], r["words"])
                   for r in con.execute("SELECT object_id, class_name, field, words FROM object_text")}
    for object_id, _, field, _ in sorted(expected_text - stored_text)[:20]:
        problems.append(f"object_text missing {object_id} {field}")
    for object_id, _, field, _ in sorted(stored_text - expected_text)[:20]:
        problems.append(f"object_text stale {object_id} {field}")
    if expected_text != stored_text:
        problems.append(
            f"object_text differs from recomputed words: {len(expected_text - stored_text)} missing, "
            f"{len(stored_text - expected_text)} stale (repair: scripts/migrate.py --index-text adds missing rows)"
        )

    # 3: every template-backed index row's claim synthesizes to its address.
    synthesized = Counter()
    deposited_rows = 0
    asserted_identical = 0
    roster_claims = set()
    for family, table in CLAIM_SOURCES:
        for row in con.execute(f"SELECT * FROM {table}").fetchall():
            if family == "roster":
                roster_claims.add(row["claim_id"])
            if row["synthesis_inputs"] is None:
                # Written by a REST deposit hint: the claim was deposited.
                deposited_rows += 1
                if row["claim_id"] not in ids:
                    problems.append(f"{table} row names deposited claim {row['claim_id']}, which is not stored")
                continue
            try:
                claim = synthesize_claim(family, row)
            except ClaimSynthesisError as exc:
                problems.append(f"{family} {row['claim_id']}: {exc}")
                continue
            synthesized[family] += 1
            if claim["proposition"] != row["proposition_id"]:
                problems.append(f"{row['claim_id']}: proposition_id {row['proposition_id']} != synthesized {claim['proposition']}")
            if row["claim_id"] in ids:
                stored = con.execute("SELECT payload FROM dapper_objects WHERE id = ?",
                                     (row["claim_id"],)).fetchone()["payload"]
                if json.loads(stored) != json.loads(tpl.canonical(claim)):
                    problems.append(f"{row['claim_id']}: stored claim differs from its synthesis")
                else:
                    asserted_identical += 1
            for ref in dapper_refs(claim):
                checked_refs += 1
                if ref not in ids:
                    problems.append(f"{row['claim_id']} (synthesized): dangling reference {ref}")

    # 4: membership edges are backed by a roster row or a stored claim. An
    # edge citing a LOADER-WRITTEN roster — a loaded roster claim, a deposited
    # claim on a loaded roster's proposition, or a has_members claim about a
    # loaded GeneSet — must be for the edge's own gene set (and, for a loaded
    # roster, collection) and name a gene that GeneSet lists. Other deposited
    # rosters' members have no fixed identifier form: counted, not checked.
    def payload_of(object_id):
        if not isinstance(object_id, str):
            return None
        row = con.execute("SELECT payload FROM dapper_objects WHERE id = ?", (object_id,)).fetchone()
        doc = json.loads(row["payload"]) if row else None
        return doc if isinstance(doc, dict) else None

    def string_members(entity):
        members = (entity or {}).get("members")
        if isinstance(members, list) and all(isinstance(m, str) for m in members):
            return set(members)
        return None

    loaded_rosters, roster_by_prop, loaded_entities = {}, {}, {}
    for r in con.execute("SELECT claim_id, collection, gene_set, proposition_id, synthesis_inputs "
                         "FROM pigean_gene_set_rosters").fetchall():
        inputs = r["synthesis_inputs"] if isinstance(r["synthesis_inputs"], dict) else json.loads(r["synthesis_inputs"])
        entity = payload_of(inputs["gene_set_entity"]) or {}
        roster = {"gene_set": r["gene_set"], "collection": r["collection"], "members": string_members(entity) or set()}
        loaded_rosters[r["claim_id"]] = roster
        roster_by_prop[r["proposition_id"]] = roster
        # A loaded GeneSet entity belongs to one collection (its description
        # and in_gene_set_collection name it).
        loaded_entities[inputs["gene_set_entity"]] = {"gene_set": entity.get("name"), "collection": r["collection"],
                                                      "members": roster["members"]}

    # Member-less GeneSets the loader wrote (association subjects with no
    # roster): an edge citing a has_members claim about one is for that set
    # and collection, and lists no gene at all.
    for r in con.execute("SELECT collection, gene_set, synthesis_inputs FROM pigean_gene_set_associations "
                         "WHERE synthesis_inputs IS NOT NULL").fetchall():
        inputs = r["synthesis_inputs"] if isinstance(r["synthesis_inputs"], dict) else json.loads(r["synthesis_inputs"])
        subject = inputs.get("subject")
        if isinstance(subject, str) and subject not in loaded_entities:
            entity = payload_of(subject) or {}
            loaded_entities[subject] = {"gene_set": entity.get("name", r["gene_set"]), "collection": r["collection"],
                                        "members": string_members(entity) or set()}

    def check_edge(row, roster, what):
        if roster["gene_set"] != row["gene_set"] or (roster["collection"] not in (None, row["collection"])):
            problems.append(f"membership edge {row['gene']} in {row['collection']}/{row['gene_set']} cites {what} "
                            f"{row['claim_id']} for a different gene set ({roster['collection'] or '?'}/"
                            f"{roster['gene_set']})")
        elif row["gene"] not in roster["members"]:
            problems.append(f"membership edge {row['gene']} in {row['gene_set']} cites {what} "
                            f"{row['claim_id']}, whose GeneSet does not list that gene")

    deposited_roster_of = {}  # deposited claim id -> loader-written roster it asserts (None: not checkable)
    unverifiable_edges = 0
    for row in con.execute("SELECT gene, collection, gene_set, claim_id FROM claim_memberships").fetchall():
        claim_id = row["claim_id"]
        if claim_id in loaded_rosters:
            check_edge(row, loaded_rosters[claim_id], "roster")
            continue
        if claim_id not in ids:
            problems.append(f"claim_memberships edge names {claim_id}: no roster row and no stored claim")
            continue
        if claim_id not in deposited_roster_of:
            claim = payload_of(claim_id) or {}
            prop_id = claim.get("proposition")
            roster = roster_by_prop.get(prop_id) if isinstance(prop_id, str) else None
            if roster is None:
                prop = payload_of(prop_id) or {}
                if prop.get("relation") == "has_members":
                    subject = prop.get("subject_entity")
                    roster = loaded_entities.get(subject) if isinstance(subject, str) else None
                    if roster is None:
                        roster = False  # a roster, but not loader-written: not checkable
            deposited_roster_of[claim_id] = roster
        roster = deposited_roster_of[claim_id]
        if roster:
            check_edge(row, roster, "deposited roster claim")
        elif roster is False:
            unverifiable_edges += 1

    con.close()
    for cls in sorted(counts):
        print(f"  {cls}: {counts[cls]}")
    print(f"object refs: {len(stored_refs)} indexed, {len(expected_refs)} recomputed")
    print(f"claims synthesized and digest-verified: {sum(synthesized.values())} "
          f"({', '.join(f'{k}: {v}' for k, v in sorted(synthesized.items()))}); "
          f"already asserted and identical to synthesis: {asserted_identical}; "
          f"deposited (non-template) index rows: {deposited_rows}")
    print(f"references checked: {checked_refs}")
    if unverifiable_edges:
        print(f"membership edges citing deposited rosters about gene sets the loader did not write (members not checked): "
              f"{unverifiable_edges}")
    if problems:
        print(f"\nPROBLEMS ({len(problems)}):")
        for p in problems[:50]:
            print(" ", p)
        if len(problems) > 50:
            print(f"  ... and {len(problems) - 50} more")
        return 1
    print("consistent: every object is well-formed with resolvable references, the reference "
          "index matches the documents, and every claim the data implies synthesizes to its "
          "precomputed address")
    return 0


if __name__ == "__main__":
    sys.exit(main())
