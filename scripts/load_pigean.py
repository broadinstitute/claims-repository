#!/usr/bin/env python3
"""Load PIGEAN combined_* NDJSON result files into the local claims DB.

One result-file row becomes one Proposition + ClaimScores + Claim (the
kpn-dapper-toolkit template shape, docs/pigean-ibd-claims.md) plus a row in the
method's index table. rs_score and n are excluded from claims per the template
(meanings unconfirmed); n is retained as an unexposed index column.

Measurement claims carry an evidence layer (decision 2026-11-09, Chase): per
metric, one EvidenceItem grounded by a ValueLocator pinning the exact byte
span of the serialized number in the result file (the dapper ValueLocator
PROPOSAL, same structure the toolkit's tier claims use), so every number is
byte-exactly re-verifiable against the File's sha256.

Gene-set membership comes from gene_gene_set_stats.json: one score-less
member_of Proposition + Claim per (gene, gene_set) pair — per Chase
(2026-11-06) we ingest only the set name and its genes, not the per-pair
effect metrics the file also carries.

Gene sets are also first-class dapper entities (traceable-IDs example, per
Chase 2026-11-06): one creation Activity + one GeneSetCollection + one GeneSet
per distinct set (members = genes observed in gene_gene_set_stats, backlink
in_gene_set_collection). Propositions reference the dapper:GeneSet IDs as
subject/object entities, so ClaimEnvelope.provenanceRefs.geneSetRefs resolve.
NOTE: the local profile hashes the whole payload; DAPPER-ID-1 would exclude
the slots the schema marks unhashable (e.g. in_gene_set_collection).

LOCAL-DEV IDENTITY: IDs are deterministic sha256 digests in DAPPER shape
(dapper:Class.local<digest>) with identity_profile 'local-dev-sha256-1'. They
are NOT DAPPER-ID-1 content addresses — real minting belongs to the toolkit
(Task 1) via the dapper identity tooling. Deterministic: same input, same IDs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service import db  # noqa: E402

TOOLKIT_DATA = Path("/Users/cyakaboski/src/research/kpn-dapper-toolkit/data/pigean/IBD")
S3_PREFIX = "s3://dig-analysis-cfde/catalog_v2/out/pigean"
IDENTITY_PROFILE = "local-dev-sha256-1"

INTERP = {
    "combined": ("SCORE", "PIGEAN combined log posterior odds of gene-phenotype association (direct GWAS plus gene-set prior evidence); log scale, larger is stronger. PROVISIONAL interpretation pending method-lead confirmation."),
    "log_bf": ("SCORE", "PIGEAN log Bayes factor from direct GWAS evidence; log scale, larger is stronger. PROVISIONAL."),
    "prior": ("SCORE", "PIGEAN log prior odds contributed by gene-set membership evidence; log scale. PROVISIONAL."),
    "beta": ("EFFECT_ESTIMATE", "PIGEAN gene-set effect estimated jointly with other selected gene sets (corrected); log-odds scale. PROVISIONAL."),
    "beta_uncorrected": ("EFFECT_ESTIMATE", "PIGEAN marginal gene-set effect, not corrected for other gene sets; log-odds scale. PROVISIONAL."),
}


def canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def mint(payload: dict) -> dict:
    digest = hashlib.sha256(canonical(payload).encode()).hexdigest()[:32]
    return {"id": f"dapper:{payload['class_name']}.local{digest}", **payload}


def score(metric: str, value: float) -> dict:
    kind, interpretation = INTERP[metric]
    return mint({"class_name": "ClaimScore", "score_kind": kind, "metric": metric, "value": value, "interpretation": interpretation})


def file_object(path: Path, source_uri: str) -> dict:
    data = path.read_bytes()
    return mint({"class_name": "File", "source_uri": source_uri, "sha256": hashlib.sha256(data).hexdigest(), "byte_size": len(data)})


def ndjson(path: Path):
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def ndjson_spans(path: Path):
    """Yield (row, raw line bytes, absolute byte offset) so metric values can
    be located byte-exactly for ValueLocators."""
    offset = 0
    with open(path, "rb") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                yield json.loads(stripped), line, offset
            offset += len(line)


_VALUE_RE_CACHE: dict[str, re.Pattern] = {}


def locate_value(line: bytes, key: str) -> tuple[int, int, str] | None:
    """(start, length, text) of the serialized numeric value for `key` in an
    NDJSON line, offsets relative to the line start. Mirrors the toolkit's
    methods/pigean/claims.py helper."""
    pattern = _VALUE_RE_CACHE.get(key)
    if pattern is None:
        pattern = re.compile(rb'"' + key.encode() + rb'"\s*:\s*(-?[0-9][0-9eE+.\-]*)')
        _VALUE_RE_CACHE[key] = pattern
    match = pattern.search(line)
    if match is None:
        return None
    return match.start(1), match.end(1) - match.start(1), match.group(1).decode()


def evidence_for(metric: str, entity_label: str, phenotype: str, file_obj: dict, file_name: str,
                 line: bytes, line_offset: int, selector_key: str, proposition_id: str) -> tuple[dict, dict]:
    """Mint the (ValueLocator, EvidenceItem) pair grounding one metric of a
    measurement claim at the result-file boundary."""
    located = locate_value(line, metric)
    if located is None:
        raise ValueError(f"metric {metric} not found in {file_name} line for {entity_label}")
    start, length, value_text = located
    locator = mint({
        "class_name": "ValueLocator",
        "file": file_obj["id"],
        "byte_start": line_offset + start,
        "byte_length": length,
        "value_text": value_text,
        "selector": f"{selector_key}; key={metric}",
        "media_type": "application/x-ndjson",
    })
    item = mint({
        "class_name": "EvidenceItem",
        "name": f"PIGEAN {metric} evidence: {entity_label} - {phenotype}",
        "value_locators": [locator["id"]],
        "target_proposition": proposition_id,
        "explanation": (
            f"PIGEAN reported {metric} = {value_text} for this association in {file_name}; the "
            "value locator pins the serialized number's byte span, so the measurement is "
            "re-verifiable against the file's sha256 "
            "(file[byte_start : byte_start+byte_length] == value_text)."
        ),
        "evidence_source": f"PIGEAN result file {file_name}",
    })
    return locator, item


def load_memberships(con, membership_stats: Path, membership_file: dict, activity: dict,
                     agent: dict, gene_set_ids: dict[str, str]) -> None:
    """Ingest (gene, gene_set) pairs as score-less member_of claims, chunked."""
    total, chunk_objects, chunk_rows = 0, [], []

    def flush():
        # chunk_rows: (collection, gene, gene_set, claim_id, proposition_id)
        unique_objects = {o["id"]: o for o in chunk_objects}
        con.executemany(
            con.dialect.upsert_object,
            [(o["id"], o["class_name"], IDENTITY_PROFILE, canonical(o)) for o in unique_objects.values()],
        )
        con.executemany(
            f"{con.dialect.insert_or_replace} claims (claim_id, proposition_id, method) VALUES (?, ?, ?)",
            [(r[3], r[4], "pigean") for r in chunk_rows],
        )
        con.executemany(
            f"{con.dialect.insert_or_replace} claim_memberships (collection, gene, gene_set, claim_id) VALUES (?, ?, ?, ?)",
            [(r[0], r[1], r[2], r[3]) for r in chunk_rows],
        )
        chunk_objects.clear()
        chunk_rows.clear()

    for row in ndjson(membership_stats):
        gene, gene_set, collection = row["gene"], row["gene_set"], row["gene_set_size"]
        proposition = mint({
            "class_name": "Proposition",
            "statement": f"The gene {gene} is a member of the gene set {gene_set}.",
            "subject_entity": gene,
            "relation": "member_of",
            "object_entity": gene_set_ids.get(gene_set, gene_set),
            "scope": f"PIGEAN gene-set collection {collection} membership.",
            "proposition_kind": "RESULT",
        })
        claim = mint({
            "class_name": "Claim",
            "proposition": proposition["id"],
            "statement": f"The {collection} gene-set collection used by PIGEAN includes {gene} as a member of the gene set {gene_set}.",
            "was_generated_by": activity["id"],
            "was_attributed_to": [agent["id"]],
            "was_derived_from": [membership_file["id"]],
            "source_locator": f"gene_gene_set_stats.json#gene={gene};gene_set={gene_set}",
        })
        chunk_objects.extend([proposition, claim])
        chunk_rows.append((collection, gene, gene_set, claim["id"], proposition["id"]))
        total += 1
        if len(chunk_rows) >= 20000:
            flush()
    if chunk_objects or chunk_rows:
        flush()
    print(f"membership claims: {total}")


def load(con, gene_stats: Path, gene_set_stats: Path, membership_stats: Path | None = None) -> None:
    activity = mint({"class_name": "Activity", "description": "PIGEAN aggregation run (local development load; companion provenance sidecar pending Task 2)."})
    agent = mint({"class_name": "Agent", "description": "DIG aggregator pipeline (local development placeholder)."})
    gene_file = file_object(gene_stats, f"{S3_PREFIX}/combined_gene_stats/cfde-inc-v2/portal/IBD/combined_gene_stats.json")
    set_file = file_object(gene_set_stats, f"{S3_PREFIX}/combined_gene_set_stats/cfde-inc-v2/portal/IBD/combined_gene_set_stats.json")

    objects, claims, gene_rows, set_rows = [activity, agent, gene_file, set_file], [], [], []

    # --- Gene-set entities (dapper GeneSet / GeneSetCollection + creation
    # Activity), minted before any claim so propositions can reference their
    # computed IDs as subject/object entities.
    membership_file = None
    members_by_set: dict[str, set] = {}
    sets_meta: dict[str, tuple] = {}  # name -> (source, collection)
    if membership_stats is not None:
        membership_file = file_object(
            membership_stats, f"{S3_PREFIX}/gene_gene_set_stats/cfde-inc-v2/portal/IBD/gene_gene_set_stats.json"
        )
        objects.append(membership_file)
        for row in ndjson(membership_stats):
            members_by_set.setdefault(row["gene_set"], set()).add(row["gene"])
            sets_meta.setdefault(row["gene_set"], (row.get("source"), row["gene_set_size"]))
    for row in ndjson(gene_set_stats):
        sets_meta.setdefault(row["gene_set"], (row.get("source"), row["gene_set_size"]))

    gene_set_ids: dict[str, str] = {}
    entity_rows = []
    if sets_meta:
        collection_tag = next(iter(sets_meta.values()))[1]
        creation = mint({
            "class_name": "Activity",
            "description": (
                f"Construction of the {collection_tag} gene-set collection (CFDE DCC-built sets, "
                "assembled upstream of s3://dig-analysis-bin; see kpn-dapper-toolkit "
                "docs/pigean-geneset-lineage.md). Placeholder creation object so gene-set IDs are "
                "traceable; full sidecar provenance is build-plan Task 2."
            ),
        })
        collection_obj = mint({
            "class_name": "GeneSetCollection",
            "name": collection_tag,
            "description": (
                f"The {collection_tag} gene-set collection used by this PIGEAN run. Member GeneSets "
                "link back via in_gene_set_collection; the complete membership manifest lives upstream."
            ),
            "member_type": "gene_set",
            "was_generated_by": creation["id"],
            "was_attributed_to": [agent["id"]],
        })
        objects.extend([creation, collection_obj])
        for name, (source, collection) in sets_meta.items():
            genes = sorted(members_by_set.get(name, ()))
            payload = {
                "class_name": "GeneSet",
                "name": name,
                "description": (
                    f"Gene set {name} from the {collection} collection (source tag: {source}). "
                    "Record reconstructed from PIGEAN's gene_gene_set_stats output; members are the "
                    f"genes observed there{'' if genes else ' (none observed for this set)'}."
                ),
                "member_type": "gene",
                "in_gene_set_collection": [collection_obj["id"]],
                "was_generated_by": creation["id"],
                "was_attributed_to": [agent["id"]],
            }
            if genes:
                payload["members"] = genes
                payload["n_genes"] = len(genes)
            if membership_file is not None:
                payload["was_derived_from"] = [membership_file["id"]]
            gene_set_obj = mint(payload)
            objects.append(gene_set_obj)
            gene_set_ids[name] = gene_set_obj["id"]
            entity_rows.append((collection, name, gene_set_obj["id"], len(genes) or None))

    for row, line, offset in ndjson_spans(gene_stats):
        gene, phenotype = row["gene"], row["phenotype"]
        collection, trait_group = row["gene_set_size"], row["trait_group"]
        proposition = mint({
            "class_name": "Proposition",
            "statement": f"The gene {gene} is associated with {phenotype}.",
            "subject_entity": gene,
            "relation": "associated_with",
            "object_entity": phenotype,
            "scope": f"PIGEAN gene-level assessment; trait_group={trait_group}; gene-set collection {collection}.",
            "proposition_kind": "RESULT",
        })
        scores = [score(m, row[m]) for m in ("combined", "log_bf", "prior")]
        evidence_objs, evidence_ids = [], []
        for m in ("combined", "log_bf", "prior"):
            locator, item = evidence_for(m, gene, phenotype, gene_file, "combined_gene_stats.json",
                                         line, offset, f"gene={gene}", proposition["id"])
            evidence_objs.extend([locator, item])
            evidence_ids.append(item["id"])
        claim = mint({
            "class_name": "Claim",
            "proposition": proposition["id"],
            "statement": f"PIGEAN assessed the association of {gene} with {phenotype} (combined {row['combined']}; log BF {row['log_bf']}; prior {row['prior']}) over the {collection} gene-set collection.",
            "has_score": [s["id"] for s in scores],
            "has_evidence": evidence_ids,
            "was_generated_by": activity["id"],
            "was_attributed_to": [agent["id"]],
            "was_derived_from": [gene_file["id"]],
            "source_locator": f"combined_gene_stats.json#gene={gene}",
        })
        objects.extend([proposition, *scores, *evidence_objs, claim])
        claims.append((claim["id"], proposition["id"], "pigean"))
        gene_rows.append((phenotype, collection, gene, row["prior"], row["combined"], row["log_bf"], row.get("n"), trait_group, claim["id"]))

    for row, line, offset in ndjson_spans(gene_set_stats):
        gene_set, phenotype = row["gene_set"], row["phenotype"]
        collection, trait_group = row["gene_set_size"], row["trait_group"]
        proposition = mint({
            "class_name": "Proposition",
            "statement": f"The gene set {gene_set} is associated with {phenotype}.",
            "subject_entity": gene_set_ids.get(gene_set, gene_set),
            "relation": "associated_with",
            "object_entity": phenotype,
            "scope": f"PIGEAN gene-set-level assessment; trait_group={trait_group}; gene-set collection {collection}.",
            "proposition_kind": "RESULT",
        })
        scores = [score(m, row[m]) for m in ("beta", "beta_uncorrected")]
        evidence_objs, evidence_ids = [], []
        for m in ("beta", "beta_uncorrected"):
            locator, item = evidence_for(m, gene_set, phenotype, set_file, "combined_gene_set_stats.json",
                                         line, offset, f"gene_set={gene_set}", proposition["id"])
            evidence_objs.extend([locator, item])
            evidence_ids.append(item["id"])
        claim = mint({
            "class_name": "Claim",
            "proposition": proposition["id"],
            "statement": f"PIGEAN assessed the association of gene set {gene_set} with {phenotype} (beta {row['beta']}; beta_uncorrected {row['beta_uncorrected']}).",
            "has_score": [s["id"] for s in scores],
            "has_evidence": evidence_ids,
            "was_generated_by": activity["id"],
            "was_attributed_to": [agent["id"]],
            "was_derived_from": [set_file["id"]],
            "source_locator": f"combined_gene_set_stats.json#gene_set={gene_set}",
        })
        objects.extend([proposition, *scores, *evidence_objs, claim])
        claims.append((claim["id"], proposition["id"], "pigean"))
        set_rows.append((phenotype, collection, gene_set, row["source"], row["beta"], row["beta_uncorrected"], row.get("rs_score"), row.get("n"), trait_group, claim["id"]))

    with con:
        # Upsert: a re-run re-mints the same content addresses; count it
        # (times_seen) instead of silently replacing. Dedupe within the run
        # first — identical score payloads across rows share one address and
        # are one minting event, not a re-mint.
        replace = con.dialect.insert_or_replace
        unique_objects = {o["id"]: o for o in objects}
        con.executemany(
            con.dialect.upsert_object,
            [(o["id"], o["class_name"], IDENTITY_PROFILE, canonical(o)) for o in unique_objects.values()],
        )
        con.executemany(f"{replace} claims (claim_id, proposition_id, method) VALUES (?, ?, ?)", claims)
        con.executemany(
            f"{replace} pigean_gene_associations (phenotype, collection, gene, prior, combined, log_bf, n, trait_group, claim_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            gene_rows,
        )
        con.executemany(
            f"{replace} pigean_gene_set_associations (phenotype, collection, gene_set, source, beta, beta_uncorrected, rs_score, n, trait_group, claim_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            set_rows,
        )
        con.executemany(
            f"{replace} gene_set_entities (collection, name, dapper_id, n_genes) VALUES (?, ?, ?, ?)",
            entity_rows,
        )

    print(f"dapper objects: {len(objects)} (gene-set entities: {len(entity_rows)})")
    print(f"claims: {len(claims)} (gene associations: {len(gene_rows)}, gene-set associations: {len(set_rows)})")
    print(f"sample gene claim: {gene_rows[0][8]}")

    if membership_stats is not None:
        with con:
            load_memberships(con, membership_stats, membership_file, activity, agent, gene_set_ids)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gene-stats", type=Path, default=TOOLKIT_DATA / "combined_gene_stats.json")
    parser.add_argument("--gene-set-stats", type=Path, default=TOOLKIT_DATA / "combined_gene_set_stats.json")
    parser.add_argument("--membership-stats", type=Path, default=TOOLKIT_DATA / "gene_gene_set_stats.json",
                        help="gene_gene_set_stats.json; membership load is skipped if the file is absent")
    parser.add_argument("--db", default=None, help="SQLite path (default: service default)")
    args = parser.parse_args()
    membership = args.membership_stats if args.membership_stats.exists() else None
    if membership is None:
        print(f"membership stats not found at {args.membership_stats}; skipping membership load")
    con = db.connect(args.db)
    try:
        load(con, args.gene_stats, args.gene_set_stats, membership)
    finally:
        con.close()


if __name__ == "__main__":
    main()
