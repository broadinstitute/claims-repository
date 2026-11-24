#!/usr/bin/env python3
"""Load PIGEAN combined_* NDJSON result files into the claims DB.

CLAIMS ARE MINTED WHEN QUERIED (decision 2026-11-09, Chase). This loader
builds the DATA PLANE only:
  - the PIGEAN index tables (one row per result-file row). Each row names the
    content address of the claim it implies (claim_id, precomputed, NOT
    stored), its proposition_id, and its synthesis_inputs — the reference IDs
    the claim needs that the row's values cannot recompute (evidence items,
    provenance activity/agent/file, the gene-set entity). A row and the claim
    it names therefore always move together;
  - the dapper objects that are observations rather than assessments:
    Propositions, ClaimScores, EvidenceItems + ValueLocators (byte offsets
    into the result file, which only exist at load time), GeneSet /
    GeneSetCollection entities, Files, Activities, Agents;
  - object_refs, the reference index behind GET /v1/objects/search.
A load REPLACES the (phenotype, collection) slices it covers: no stale
loader-written association rows or membership edges survive a reload (a
REST-indexed row is never deleted by the slice, but a loaded row at the same
key supersedes it). Superseded roster rows stay (keyed by their immutable
claim address) but are no longer implied — a roster is implied only while a
membership edge cites it; one already minted stays a stored claim. The service synthesizes a claim from its row + the
shared template (service/pigean_template.py) whenever the scientific query
surface serves it, and refuses it unless the result's content address equals
the row's precomputed one.

Gene-set membership comes from gene_gene_set_stats.json: ONE roster claim per
gene set (2026-11-09, Chase) — proposition (dapper:GeneSet, has_members,
dapper:GeneSetCollection), the exact roster pinned by the entity's
content-addressed `members` list. claim_memberships keeps per-pair rows for
joining, all pointing at their set's roster claim. Per the 2026-11-06
decision only names + genes are ingested, never the per-pair metrics.

rs_score and n are excluded from claims (meanings unconfirmed); n is kept as
an unexposed index column.

A load may keep only part of a run (Keep: --min-combined,
--min-beta-uncorrected). Rows below a threshold are not ingested at all: no
proposition, score, evidence or index row. The File objects still describe the
whole upstream files (kept rows' byte offsets point into them), a kept gene
set's GeneSet entity still lists its full observed roster, and only the
membership pairs of kept genes are indexed. A missing row in a filtered slice
therefore means "below the load's threshold or not assessed", never "assessed
as unassociated".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service import db  # noqa: E402
from service import pigean_template as tpl  # noqa: E402
from service.refs import dapper_refs  # noqa: E402

TOOLKIT_DATA = Path("/Users/cyakaboski/src/research/kpn-dapper-toolkit/data/pigean/IBD")
S3_PREFIX = "s3://dig-analysis-cfde/catalog_v2/out/pigean"
mint = tpl.mint


class Source(NamedTuple):
    """Where a load's result files live upstream and how its run and gene-set
    library are described. Every string here enters a content address, so the
    defaults are exactly the strings the IBD / cfde-inc-v2 load was made with
    (an unchanged reload re-addresses nothing)."""
    gene_uri: str = f"{S3_PREFIX}/combined_gene_stats/cfde-inc-v2/portal/IBD/combined_gene_stats.json"
    set_uri: str = f"{S3_PREFIX}/combined_gene_set_stats/cfde-inc-v2/portal/IBD/combined_gene_set_stats.json"
    membership_uri: str = f"{S3_PREFIX}/gene_gene_set_stats/cfde-inc-v2/portal/IBD/gene_gene_set_stats.json"
    run_description: str = "PIGEAN aggregation run (local development load; companion provenance sidecar pending Task 2)."
    library_origin: str = ("CFDE DCC-built sets, assembled upstream of s3://dig-analysis-bin; see kpn-dapper-toolkit "
                           "docs/pigean-geneset-lineage.md")

    @classmethod
    def under(cls, prefix: str, library: str, trait_group: str, phenotype_key: str, *,
              run_description: str, library_origin: str) -> "Source":
        """The aggregator layout: <prefix>/<kind>/<library>/<trait_group>/<phenotype>/<kind>.json"""
        def uri(kind):
            return f"{prefix.rstrip('/')}/{kind}/{library}/{trait_group}/{phenotype_key}/{kind}.json"
        return cls(uri("combined_gene_stats"), uri("combined_gene_set_stats"), uri("gene_gene_set_stats"),
                   run_description, library_origin)


def file_object(path: Path, source_uri: str) -> dict:
    data = path.read_bytes()
    return mint({"class_name": "File", "source_uri": source_uri, "sha256": hashlib.sha256(data).hexdigest(), "byte_size": len(data)})


class Keep(NamedTuple):
    """Which rows of a run a load ingests (strict lower bounds; None keeps all)."""
    min_combined: float | None = None           # gene rows: combined > this
    min_beta_uncorrected: float | None = None   # gene-set rows: beta_uncorrected > this

    def gene(self, row: dict) -> bool:
        return self.min_combined is None or tpl.num(row["combined"]) > self.min_combined

    def gene_set(self, row: dict) -> bool:
        return self.min_beta_uncorrected is None or tpl.num(row["beta_uncorrected"]) > self.min_beta_uncorrected


_PY_NONE = re.compile(r'(?<=": )None(?=[,}])')
PY_NONE_LINES: dict[str, int] = {}  # file name -> lines repaired


def ndjson(path: Path, python_none: bool = False):
    """Rows of an NDJSON file. python_none: accept a bare Python `None` as a
    value (read as null) — some aggregator runs write gene_gene_set_stats.json
    that way. Only for files whose VALUES are never ingested (membership:
    names and genes only); repaired lines are counted in PY_NONE_LINES."""
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                if not python_none:
                    raise
                PY_NONE_LINES[path.name] = PY_NONE_LINES.get(path.name, 0) + 1
                yield json.loads(_PY_NONE.sub("null", line))


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
    """Mint the (ValueLocator, EvidenceItem) pair grounding one metric at the
    result-file boundary. value_text is the file's own serialization (byte-
    exact), independent of the float normalization used for scores."""
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


class TraitResolver:
    """PIGEAN phenotype key -> (canonical trait id, label) via the KPN trait
    registry (scripts/load_kpn_traits.py): pigean_id first, then legacy_id,
    then an already-canonical KPN.TRAIT id. Unregistered keys pass through
    unchanged (and are reported)."""

    def __init__(self, con):
        self.mysql = not con.dialect.sqlite
        self.by_key: dict[str, tuple[str, str]] = {}
        rows = con.execute("SELECT trait_id, legacy_id, pigean_id, name FROM kpn_traits").fetchall()
        for r in rows:  # legacy first so a pigean_id takes precedence on a clash
            self.by_key[r["legacy_id"]] = (r["trait_id"], r["name"])
        self.aliases: dict[str, list[str]] = {}
        for r in rows:
            if r["pigean_id"]:
                self.by_key[r["pigean_id"]] = (r["trait_id"], r["name"])
            self.by_key[r["trait_id"]] = (r["trait_id"], r["name"])
            self.aliases[r["trait_id"]] = [k for k in (r["trait_id"], r["pigean_id"], r["legacy_id"]) if k]
        self.unresolved: set[str] = set()

    def keys_of(self, phenotype: str) -> list[str]:
        """Every key rows of this (canonical) phenotype may be stored under —
        rows loaded before the registry carry the legacy key. On MySQL only
        the keys its ascii VARCHAR(64) phenotype columns can hold (comparing
        a non-ASCII key against them is a collation error, and no row can
        carry one); SQLite stores any key, so every alias counts there."""
        aliases = self.aliases.get(phenotype, [phenotype])
        if not self.mysql:
            return aliases
        return [k for k in aliases if k.isascii() and len(k) <= 64] or [phenotype]

    def __call__(self, key: str) -> tuple[str, str]:
        if key in self.by_key:
            return self.by_key[key]
        self.unresolved.add(key)
        return key, key


def _check_against_store(con, documents: dict[str, dict]) -> None:
    """Refuse to load over a stored document that differs from what this load
    builds at the same address (data plane or implied claim): content
    addressing makes that impossible for genuine data, so it is either a
    forgery planted at a future address or corruption."""
    ids = sorted(documents)
    mismatched = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for row in con.execute(f"SELECT id, payload FROM dapper_objects WHERE id IN ({marks})", chunk).fetchall():
            if json.loads(row["payload"]) != documents[row["id"]]:
                mismatched.append(row["id"])
    if mismatched:
        raise SystemExit(
            f"refusing to load: {len(mismatched)} stored document(s) differ from the content this load "
            f"addresses identically (forged or corrupted), e.g. {mismatched[:3]}; remove them first"
        )


def load(con, gene_stats: Path, gene_set_stats: Path, membership_stats: Path | None = None,
         source: Source = Source(), keep: Keep = Keep()) -> dict:
    resolve = TraitResolver(con)
    PY_NONE_LINES.clear()
    skipped = {"gene_rows": 0, "gene_set_rows": 0, "membership_pairs": 0, "rosters": 0}
    activity = mint({"class_name": "Activity", "description": source.run_description})
    agent = mint({"class_name": "Agent", "description": "DIG aggregator pipeline (local development placeholder)."})
    gene_file = file_object(gene_stats, source.gene_uri)
    set_file = file_object(gene_set_stats, source.set_uri)

    objects = [activity, agent, gene_file, set_file]
    claims: dict[str, dict] = {}  # implied claims, built only to check the store
    gene_rows, set_rows, roster_rows, membership_rows = [], [], [], []

    # --- Gene-set entities, keyed by (collection, name): a set name may recur
    # across collections. One GeneSetCollection (+ creation Activity) per
    # collection. Minted first so propositions can reference their IDs.
    # Under a gene-set filter only the sets it keeps get an entity, a roster
    # and membership edges; without one, every set either file names does.
    kept_sets = {(r["gene_set_size"], r["gene_set"]) for r in ndjson(gene_set_stats) if keep.gene_set(r)}
    filter_sets = keep.min_beta_uncorrected is not None
    membership_file = None
    members: dict[tuple, set] = {}          # (collection, name) -> genes
    membership_phenotype: dict[str, str] = {}  # collection -> phenotype of its membership file
    sources: dict[tuple, str] = {}          # (collection, name) -> source tag
    if membership_stats is not None:
        membership_file = file_object(membership_stats, source.membership_uri)
        objects.append(membership_file)
        for row in ndjson(membership_stats, python_none=True):
            key = (row["gene_set_size"], row["gene_set"])
            if filter_sets and key not in kept_sets:
                skipped["membership_pairs"] += 1
                continue
            members.setdefault(key, set()).add(row["gene"])
            sources.setdefault(key, row.get("source"))
            trait_id = resolve(row["phenotype"])[0]
            seen = membership_phenotype.setdefault(row["gene_set_size"], trait_id)
            if seen != trait_id:
                raise ValueError(
                    f"membership file mixes phenotypes {seen!r} and {trait_id!r} in collection "
                    f"{row['gene_set_size']!r}: PIGEAN membership is per phenotype run; load each "
                    "phenotype's gene_gene_set_stats.json separately"
                )
    for row in ndjson(gene_set_stats):
        if keep.gene_set(row):
            sources.setdefault((row["gene_set_size"], row["gene_set"]), row.get("source"))

    collection_objs: dict[str, dict] = {}
    gene_set_ids: dict[tuple, str] = {}
    memberless: dict[tuple, dict] = {}
    memberless_neutral: dict[tuple, dict] = {}
    entity_rows = []
    # Which GeneSet a gene-set association names (its subject), per
    # (phenotype, collection) slice — a pure function of this load's inputs
    # plus, only where this load brings no membership for the slice, the
    # slice's live rosters:
    #   - this load's membership file covers the slice: it is authoritative
    #     (it replaces the slice's edges). A set it lists is the member-ful
    #     entity; a set it omits is the member-less entity — so an identical
    #     reload names the identical subject;
    #   - no membership for the slice in this load: the member-ful entity the
    #     slice's live roster names (its edges stay, so association and
    #     roster agree), else the member-less entity. Another phenotype's
    #     observation never leaks in.
    # Live rosters are found through the phenotype of the EDGES that keep
    # them live (the roster row's own phenotype column is first-writer-wins).
    set_phenotypes = sorted({(resolve(r["phenotype"])[0], r["gene_set_size"])
                             for r in ndjson(gene_set_stats) if keep.gene_set(r)})
    for collection, observed in sorted(membership_phenotype.items()):
        if (observed, collection) not in set_phenotypes:
            raise ValueError(
                f"membership file observes {observed!r} in collection {collection!r}, but the gene-set "
                "file has no rows for it there: pass that phenotype's own gene_gene_set_stats.json "
                "(--membership-stats), or --no-membership"
            )
    known_entities = {}
    for phenotype, collection in set_phenotypes:
        if membership_phenotype.get(collection) == phenotype:
            continue  # this load's membership replaces the slice
        keys = resolve.keys_of(phenotype)
        marks = ",".join("?" * len(keys))
        for r in con.execute(
            "SELECT DISTINCT r.gene_set, r.synthesis_inputs FROM pigean_gene_set_rosters r "
            "JOIN claim_memberships m ON m.claim_id = r.claim_id "
            f"WHERE m.collection = ? AND m.phenotype IN ({marks})",
            (collection, *keys),
        ).fetchall():
            inputs = r["synthesis_inputs"]
            inputs = inputs if isinstance(inputs, dict) else json.loads(inputs)
            known_entities[(phenotype, collection, r["gene_set"])] = inputs["gene_set_entity"]
    for collection in sorted({c for c, _ in sources}):
        creation = mint({
            "class_name": "Activity",
            "description": (
                f"Construction of the {collection} gene-set collection ({source.library_origin}). "
                "Placeholder creation object so gene-set IDs are "
                "traceable; full sidecar provenance is build-plan Task 2."
            ),
        })
        collection_objs[collection] = mint({
            "class_name": "GeneSetCollection",
            "name": collection,
            "description": (
                f"The {collection} gene-set collection used by this PIGEAN run. Member GeneSets "
                "link back via in_gene_set_collection; the complete membership manifest lives upstream."
            ),
            "member_type": "gene_set",
            "was_generated_by": creation["id"],
            "was_attributed_to": [agent["id"]],
        })
        objects.extend([creation, collection_objs[collection]])
    def gene_set_entity(collection, name, source, genes, derived=True):
        payload = {
            "class_name": "GeneSet",
            "name": name,
            "description": (
                f"Gene set {name} from the {collection} collection (source tag: {source}). "
                "Record reconstructed from PIGEAN's gene_gene_set_stats output; members are the "
                f"genes observed there{'' if genes else ' (none observed for this set)'}."
            ),
            "member_type": "gene",
            "in_gene_set_collection": [collection_objs[collection]["id"]],
            "was_generated_by": collection_objs[collection]["was_generated_by"],
            "was_attributed_to": [agent["id"]],
        }
        if genes:
            payload["members"] = genes
            payload["n_genes"] = len(genes)
        if derived and membership_file is not None:
            payload["was_derived_from"] = [membership_file["id"]]
        return mint(payload)

    for (collection, name), source in sorted(sources.items()):
        genes = sorted(members.get((collection, name), ()))
        if genes:  # never replace a member-ful latest entity with a member-less one
            gene_set_obj = gene_set_entity(collection, name, source, genes)
            objects.append(gene_set_obj)
            gene_set_ids[(collection, name)] = gene_set_obj["id"]
            entity_rows.append((collection, name, gene_set_obj["id"], len(genes)))
        # Every set also has member-less variants, stored only if some
        # association row names one (see below): one citing the membership
        # file, for a set that file omits for its OWN phenotype; and a
        # phenotype-neutral one (no membership provenance — exactly what a
        # membership-less load mints) for every other phenotype, so another
        # phenotype's membership file never decides its subject.
        memberless[(collection, name)] = gene_set_entity(collection, name, source, [])
        memberless_neutral[(collection, name)] = gene_set_entity(collection, name, source, [], derived=False)

    # --- Gene-phenotype rows: eager proposition/scores/evidence; the claim is
    # only named (claim_id) and made synthesizable (synthesis_inputs).
    kept_genes: set[tuple] = set()  # (collection, gene)
    for raw, line, offset in ndjson_spans(gene_stats):
        if not keep.gene(raw):
            skipped["gene_rows"] += 1
            continue
        phenotype, label = resolve(raw["phenotype"])
        row = {**raw, "phenotype": phenotype, "collection": raw["gene_set_size"]}
        gene, collection = row["gene"], row["collection"]
        kept_genes.add((collection, gene))
        proposition = tpl.gene_proposition(gene, phenotype, label, collection, row["trait_group"])
        scores = [tpl.score(m, row[m]) for m in tpl.GENE_METRICS]
        evidence = {}
        for m in tpl.GENE_METRICS:
            locator, item = evidence_for(m, gene, label, gene_file, "combined_gene_stats.json",
                                         line, offset, f"gene={gene}", proposition["id"])
            objects.extend([locator, item])
            evidence[m] = item["id"]
        inputs = tpl.gene_inputs(activity["id"], agent["id"], gene_file["id"], evidence, label)
        claim = tpl.gene_claim(row, inputs)
        claims[claim["id"]] = claim
        objects.extend([proposition, *scores])
        gene_rows.append((phenotype, collection, gene, tpl.num(row["prior"]), tpl.num(row["combined"]),
                          tpl.num(row["log_bf"]), row.get("n"), row["trait_group"], claim["id"],
                          proposition["id"], tpl.canonical(inputs)))

    # --- Gene-set-phenotype rows.
    for raw, line, offset in ndjson_spans(gene_set_stats):
        if not keep.gene_set(raw):
            skipped["gene_set_rows"] += 1
            continue
        phenotype, label = resolve(raw["phenotype"])
        row = {**raw, "phenotype": phenotype, "collection": raw["gene_set_size"]}
        gene_set, collection = row["gene_set"], row["collection"]
        own_slice = membership_phenotype.get(collection) == phenotype
        if own_slice and (collection, gene_set) in gene_set_ids:
            subject = gene_set_ids[(collection, gene_set)]  # observed for THIS phenotype in this load
        else:
            subject = known_entities.get((phenotype, collection, gene_set))
            if subject is None:
                obj = (memberless if own_slice else memberless_neutral)[(collection, gene_set)]
                objects.append(obj)
                subject = obj["id"]
        proposition = tpl.gene_set_proposition(gene_set, subject, phenotype, label, collection, row["trait_group"])
        scores = [tpl.score(m, row[m]) for m in tpl.SET_METRICS]
        evidence = {}
        for m in tpl.SET_METRICS:
            locator, item = evidence_for(m, gene_set, label, set_file, "combined_gene_set_stats.json",
                                         line, offset, f"gene_set={gene_set}", proposition["id"])
            objects.extend([locator, item])
            evidence[m] = item["id"]
        inputs = tpl.gene_set_inputs(activity["id"], agent["id"], set_file["id"], subject, evidence, label)
        claim = tpl.gene_set_claim(row, inputs)
        claims[claim["id"]] = claim
        objects.extend([proposition, *scores])
        set_rows.append((phenotype, collection, gene_set, row["source"], tpl.num(row["beta"]),
                         tpl.num(row["beta_uncorrected"]), row.get("rs_score"), row.get("n"),
                         row["trait_group"], claim["id"], proposition["id"], tpl.canonical(inputs)))

    # --- Membership: one roster claim per gene set with observed members.
    if membership_file is not None:
        for (collection, name), genes in sorted(members.items()):
            genes = sorted(genes)
            phenotype = membership_phenotype[collection]
            # Under a gene filter only kept genes' pairs are indexed (the
            # GeneSet entity above keeps the full roster). A roster no edge
            # would cite is never implied, so its row is not written.
            indexed = genes if keep.min_combined is None else [g for g in genes if (collection, g) in kept_genes]
            skipped["membership_pairs"] += len(genes) - len(indexed)
            if not indexed:
                skipped["rosters"] += 1
                continue
            inputs = tpl.roster_inputs(activity["id"], agent["id"], membership_file["id"],
                                       gene_set_ids[(collection, name)], collection_objs[collection]["id"],
                                       len(genes))
            proposition = tpl.roster_proposition(name, inputs["gene_set_entity"], collection,
                                                 inputs["collection_entity"], inputs["n_genes"])
            claim = tpl.roster_claim({"gene_set": name, "collection": collection}, inputs)
            claims[claim["id"]] = claim
            objects.append(proposition)
            roster_rows.append((claim["id"], phenotype, collection, name, proposition["id"],
                                tpl.canonical(inputs)))
            membership_rows.extend((phenotype, collection, gene, name, claim["id"]) for gene in indexed)

    unique_objects = {o["id"]: o for o in objects}
    _check_against_store(con, {**unique_objects, **claims})
    ref_rows = sorted({(oid, ref) for oid, o in unique_objects.items() for ref in dapper_refs(o)})
    gene_slices = sorted({(r[0], r[1]) for r in gene_rows})
    set_slices = sorted({(r[0], r[1]) for r in set_rows})
    membership_slices = sorted({(p, c) for c, p in membership_phenotype.items()})

    with con:
        replace = con.dialect.insert_or_replace
        # A load replaces the (phenotype, collection) slices it covers. The
        # slice DELETEs skip rows indexed by REST deposits (no synthesis
        # inputs; non-roster membership claims); a loaded row written at the
        # same key below still supersedes one.
        # Under EVERY key of the phenotype: rows loaded before the trait
        # registry carry the legacy key, and must not survive beside the
        # canonical reload.
        def slice_keys(phenotype):
            keys = resolve.keys_of(phenotype)
            return keys, ",".join("?" * len(keys))

        for phenotype, collection in gene_slices:
            keys, marks = slice_keys(phenotype)
            con.execute(f"DELETE FROM pigean_gene_associations WHERE phenotype IN ({marks}) AND collection = ? "
                        "AND synthesis_inputs IS NOT NULL", (*keys, collection))
        for phenotype, collection in set_slices:
            keys, marks = slice_keys(phenotype)
            con.execute(f"DELETE FROM pigean_gene_set_associations WHERE phenotype IN ({marks}) AND collection = ? "
                        "AND synthesis_inputs IS NOT NULL", (*keys, collection))
        for phenotype, collection in membership_slices:
            keys, marks = slice_keys(phenotype)
            con.execute(f"DELETE FROM claim_memberships WHERE phenotype IN ({marks}) AND collection = ? "
                        "AND claim_id IN (SELECT claim_id FROM pigean_gene_set_rosters)", (*keys, collection))
        # Upsert: a re-run re-deposits the same content addresses; count it
        # (times_seen) instead of silently replacing. Deduped within the run.
        con.executemany(
            con.dialect.upsert_object,
            [(o["id"], o["class_name"], tpl.IDENTITY_PROFILE, tpl.canonical(o)) for o in unique_objects.values()],
        )
        con.executemany(con.dialect.insert_ref, ref_rows)
        con.executemany(
            f"{replace} pigean_gene_associations (phenotype, collection, gene, prior, combined, log_bf, n, trait_group, claim_id, proposition_id, synthesis_inputs) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            gene_rows,
        )
        con.executemany(
            f"{replace} pigean_gene_set_associations (phenotype, collection, gene_set, source, beta, beta_uncorrected, rs_score, n, trait_group, claim_id, proposition_id, synthesis_inputs) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            set_rows,
        )
        # Roster rows are keyed by their claim's content address: immutable.
        con.executemany(
            f"{con.dialect.insert_or_ignore} pigean_gene_set_rosters (claim_id, phenotype, collection, gene_set, proposition_id, synthesis_inputs) VALUES (?, ?, ?, ?, ?, ?)",
            roster_rows,
        )
        con.executemany(
            f"{replace} claim_memberships (phenotype, collection, gene, gene_set, claim_id) VALUES (?, ?, ?, ?, ?)",
            membership_rows,
        )
        con.executemany(
            f"{replace} gene_set_entities (collection, name, dapper_id, n_genes) VALUES (?, ?, ?, ?)",
            entity_rows,
        )

    stats = {
        "objects": len(unique_objects),
        "refs": len(ref_rows),
        "gene_set_entities": len(entity_rows),
        "gene_claims": len(gene_rows),
        "gene_set_claims": len(set_rows),
        "roster_claims": len(roster_rows),
        "membership_pairs": len(membership_rows),
        "skipped": skipped,
    }
    print(f"dapper objects (data plane): {stats['objects']} (gene-set entities: {stats['gene_set_entities']}); "
          f"object refs: {stats['refs']}")
    print(f"claims synthesizable on serve (none stored): "
          f"{stats['gene_claims'] + stats['gene_set_claims'] + stats['roster_claims']} "
          f"(gene: {stats['gene_claims']}, gene-set: {stats['gene_set_claims']}, set rosters: {stats['roster_claims']})")
    print(f"membership pairs indexed: {stats['membership_pairs']}")
    if any(skipped.values()):
        print(f"not ingested (below --min-combined {keep.min_combined} / --min-beta-uncorrected "
              f"{keep.min_beta_uncorrected}): {skipped['gene_rows']} gene rows, {skipped['gene_set_rows']} gene-set rows, "
              f"{skipped['membership_pairs']} membership pairs, {skipped['rosters']} rosters with no kept gene")
    if PY_NONE_LINES:
        print(f"WARNING: read Python None as null on {sum(PY_NONE_LINES.values())} membership line(s): {dict(PY_NONE_LINES)}")
    if resolve.unresolved:
        print(f"WARNING: {len(resolve.unresolved)} phenotype key(s) not in the KPN trait registry (load it "
              f"first with scripts/load_kpn_traits.py); kept as-is: {sorted(resolve.unresolved)[:5]}")
    stats["unresolved_phenotypes"] = sorted(resolve.unresolved)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gene-stats", type=Path, default=TOOLKIT_DATA / "combined_gene_stats.json")
    parser.add_argument("--gene-set-stats", type=Path, default=TOOLKIT_DATA / "combined_gene_set_stats.json")
    parser.add_argument("--membership-stats", type=Path, default=TOOLKIT_DATA / "gene_gene_set_stats.json",
                        help="gene_gene_set_stats.json of the SAME PIGEAN run (default: the toolkit's IBD file; "
                             "a file for a phenotype the result files lack is refused); skipped if absent")
    parser.add_argument("--no-membership", action="store_true",
                        help="load no membership (keeps each slice's live rosters)")
    parser.add_argument("--db", default=None, help="SQLite path (default: service default)")
    parser.add_argument("--source-prefix", help="upstream prefix of the aggregator output, e.g. s3://bucket/out/falcon; "
                        "with --library/--trait-group/--phenotype-key it replaces the default IBD / cfde-inc-v2 "
                        "file locations recorded on the File objects")
    parser.add_argument("--library", help="gene-set library folder under the prefix, e.g. mouse_msigdb")
    parser.add_argument("--trait-group", default="portal")
    parser.add_argument("--phenotype-key", help="phenotype folder under the prefix, e.g. IBD")
    parser.add_argument("--run-description", help="description of the PIGEAN run (the claims' Activity)")
    parser.add_argument("--library-origin", help="one phrase on where the gene-set library comes from")
    parser.add_argument("--min-combined", type=float, default=None,
                        help="ingest only gene rows with combined > this (default: all)")
    parser.add_argument("--min-beta-uncorrected", type=float, default=None,
                        help="ingest only gene-set rows with beta_uncorrected > this (default: all)")
    args = parser.parse_args()
    source = Source()
    if args.source_prefix:
        missing = [n for n in ("library", "phenotype_key", "run_description", "library_origin") if not getattr(args, n)]
        if missing:
            parser.error(f"--source-prefix also needs --{', --'.join(m.replace('_', '-') for m in missing)}")
        source = Source.under(args.source_prefix, args.library, args.trait_group, args.phenotype_key,
                              run_description=args.run_description, library_origin=args.library_origin)
    membership = None if args.no_membership else args.membership_stats
    if membership is not None and not membership.exists():
        print(f"WARNING: membership stats not found at {membership}; skipping membership load")
        membership = None
    elif membership is None:
        print("--no-membership: skipping membership load")
    con = db.connect(args.db)
    try:
        load(con, args.gene_stats, args.gene_set_stats, membership, source,
             Keep(args.min_combined, args.min_beta_uncorrected))
    finally:
        con.close()


if __name__ == "__main__":
    main()
