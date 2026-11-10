"""PIGEAN claim template — the one definition of the dapper documents this
repository mints for PIGEAN results (decision 2026-11-09, Chase: claims are
minted when the scientific query surface serves them).

Pure functions, no database access. Imported by BOTH:
  - scripts/load_pigean.py, which builds the eager data plane (propositions,
    scores, evidence + locators, entities) and writes each index row with its
    claim's precomputed ID and its synthesis_inputs — without storing the
    claim; and
  - service/app.py, which synthesizes the claim document from the row + its
    inputs on every serve and refuses it unless its content address equals
    the row's precomputed ID (the drift guardrail).
Because both sides call the same functions, byte identity holds by
construction; the digest check catches a stale vendored copy or a tampered
index row.

Numeric metrics are normalized to float before hashing or formatting: the
result files carry some integer-typed values (e.g. 1,149 `"beta": 0`), which
the index tables return as floats — hashing the raw JSON int at load time
would make those claims unreproducible at serve time.

LOCAL-DEV IDENTITY: IDs are deterministic sha256 digests in DAPPER shape
(dapper:Class.local<digest>), identity_profile 'local-dev-sha256-1' — NOT
DAPPER-ID-1 content addresses (real minting belongs to the toolkit).
"""
from __future__ import annotations

import hashlib
import json

IDENTITY_PROFILE = "local-dev-sha256-1"
METHOD = "pigean"

GENE_METRICS = ("combined", "log_bf", "prior")
SET_METRICS = ("beta", "beta_uncorrected")

INTERP = {
    "combined": ("SCORE", "PIGEAN combined log posterior odds of gene-phenotype association (direct GWAS plus gene-set prior evidence); log scale, larger is stronger. PROVISIONAL interpretation pending method-lead confirmation."),
    "log_bf": ("SCORE", "PIGEAN log Bayes factor from direct GWAS evidence; log scale, larger is stronger. PROVISIONAL."),
    "prior": ("SCORE", "PIGEAN log prior odds contributed by gene-set membership evidence; log scale. PROVISIONAL."),
    "beta": ("EFFECT_ESTIMATE", "PIGEAN gene-set effect estimated jointly with other selected gene sets (corrected); log-odds scale. PROVISIONAL."),
    "beta_uncorrected": ("EFFECT_ESTIMATE", "PIGEAN marginal gene-set effect, not corrected for other gene sets; log-odds scale. PROVISIONAL."),
}

# Synthesis inputs (2026-11-09 review fix): each index row carries, in its
# `synthesis_inputs` column, the reference IDs its claim needs that the row's
# own values cannot recompute (evidence items, provenance activity/agent/file,
# the gene-set entity). A row and the claim it names therefore always move
# together: no later load, deposit, or lookup-table update can change what an
# existing row synthesizes. Synthesis is a pure function of (row, inputs).


def canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def mint(payload: dict) -> dict:
    digest = hashlib.sha256(canonical(payload).encode()).hexdigest()[:32]
    return {"id": f"dapper:{payload['class_name']}.local{digest}", **payload}


def num(value) -> float:
    """Canonical float for hashing and formatting: JSON ints and floats hash
    alike, -0.0 hashes as 0.0 (SQLite returns -0.0 as 0.0), and non-finite
    values are refused rather than hashed into unreproducible claims."""
    v = float(value)
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError(f"non-finite metric value {value!r}")
    return 0.0 if v == 0 else v


def local_dev_address_ok(obj: dict) -> bool:
    """True when a local-dev-profile object's id is the digest of its content
    (everything except the id itself) — the content-address check deposit
    applies to every dapper:{Class}.local<hex> identifier."""
    return mint({k: v for k, v in obj.items() if k != "id"})["id"] == obj.get("id")


def score(metric: str, value) -> dict:
    kind, interpretation = INTERP[metric]
    return mint({
        "class_name": "ClaimScore",
        "score_kind": kind,
        "metric": metric,
        "value": num(value),
        "interpretation": interpretation,
    })


# --- propositions (eager: deposited at load) -------------------------------

# Phenotypes: `phenotype` is the canonical trait ID (a KPN.TRAIT CURIE from
# broadinstitute/kpn-data-models when the key is registered, else the key as
# loaded); `phenotype_label` is the human name written into statements. Both
# are pinned per row (index column + synthesis_inputs), so a later registry
# release cannot change an existing row's claim.

def gene_proposition(gene: str, phenotype: str, phenotype_label: str, collection: str,
                     trait_group: str) -> dict:
    return mint({
        "class_name": "Proposition",
        "statement": f"The gene {gene} is associated with {phenotype_label}.",
        "subject_entity": gene,
        "relation": "associated_with",
        "object_entity": phenotype,
        "scope": f"PIGEAN gene-level assessment; trait_group={trait_group}; gene-set collection {collection}.",
        "proposition_kind": "RESULT",
    })


def gene_set_proposition(gene_set: str, subject_id: str, phenotype: str, phenotype_label: str,
                         collection: str, trait_group: str) -> dict:
    """subject_id: the set's dapper:GeneSet ID when its entity exists, else its name."""
    return mint({
        "class_name": "Proposition",
        "statement": f"The gene set {gene_set} is associated with {phenotype_label}.",
        "subject_entity": subject_id,
        "relation": "associated_with",
        "object_entity": phenotype,
        "scope": f"PIGEAN gene-set-level assessment; trait_group={trait_group}; gene-set collection {collection}.",
        "proposition_kind": "RESULT",
    })


def roster_proposition(gene_set: str, gene_set_id: str, collection: str, collection_id: str,
                       n_genes: int) -> dict:
    """One roster per gene set (2026-11-09): the subject is the
    content-addressed GeneSet entity, whose hashable `members` list pins the
    exact roster."""
    return mint({
        "class_name": "Proposition",
        "statement": (
            f"In the {collection} gene-set collection, the gene set {gene_set} comprises "
            f"{n_genes} member genes; the exact roster is recorded in its "
            "content-addressed GeneSet entity."
        ),
        "subject_entity": gene_set_id,
        "relation": "has_members",
        "object_entity": collection_id,
        "scope": f"PIGEAN gene-set collection {collection} membership (observed roster).",
        "proposition_kind": "RESULT",
    })


# --- claims (minted when the query surface serves them) -------------------
#
# Each takes the index row (a mapping with the row's columns: gene / gene_set,
# phenotype, collection, trait_group, metric values) and the row's
# synthesis_inputs, and returns the minted claim document.

def gene_inputs(activity: str, agent: str, source_file: str, evidence: dict,
                phenotype_label: str) -> dict:
    return {"activity": activity, "agent": agent, "source_file": source_file,
            "evidence": {m: evidence[m] for m in GENE_METRICS}, "phenotype_label": phenotype_label}


def gene_set_inputs(activity: str, agent: str, source_file: str, subject: str, evidence: dict,
                    phenotype_label: str) -> dict:
    return {"activity": activity, "agent": agent, "source_file": source_file, "subject": subject,
            "evidence": {m: evidence[m] for m in SET_METRICS}, "phenotype_label": phenotype_label}


def roster_inputs(activity: str, agent: str, source_file: str, gene_set_entity: str,
                  collection_entity: str, n_genes: int) -> dict:
    return {"activity": activity, "agent": agent, "source_file": source_file,
            "gene_set_entity": gene_set_entity, "collection_entity": collection_entity,
            "n_genes": int(n_genes)}


def gene_claim(row, inputs: dict) -> dict:
    label = inputs["phenotype_label"]
    prop = gene_proposition(row["gene"], row["phenotype"], label, row["collection"], row["trait_group"])
    v = {m: num(row[m]) for m in GENE_METRICS}
    return mint({
        "class_name": "Claim",
        "proposition": prop["id"],
        "statement": (
            f"PIGEAN assessed the association of {row['gene']} with {label} "
            f"(combined {v['combined']}; log BF {v['log_bf']}; prior {v['prior']}) over the "
            f"{row['collection']} gene-set collection."
        ),
        "has_score": [score(m, v[m])["id"] for m in GENE_METRICS],
        "has_evidence": [inputs["evidence"][m] for m in GENE_METRICS],
        "was_generated_by": inputs["activity"],
        "was_attributed_to": [inputs["agent"]],
        "was_derived_from": [inputs["source_file"]],
        "source_locator": f"combined_gene_stats.json#gene={row['gene']}",
    })


def gene_set_claim(row, inputs: dict) -> dict:
    label = inputs["phenotype_label"]
    prop = gene_set_proposition(row["gene_set"], inputs["subject"], row["phenotype"], label,
                                row["collection"], row["trait_group"])
    v = {m: num(row[m]) for m in SET_METRICS}
    return mint({
        "class_name": "Claim",
        "proposition": prop["id"],
        "statement": (
            f"PIGEAN assessed the association of gene set {row['gene_set']} with {label} "
            f"(beta {v['beta']}; beta_uncorrected {v['beta_uncorrected']})."
        ),
        "has_score": [score(m, v[m])["id"] for m in SET_METRICS],
        "has_evidence": [inputs["evidence"][m] for m in SET_METRICS],
        "was_generated_by": inputs["activity"],
        "was_attributed_to": [inputs["agent"]],
        "was_derived_from": [inputs["source_file"]],
        "source_locator": f"combined_gene_set_stats.json#gene_set={row['gene_set']}",
    })


def roster_claim(row, inputs: dict) -> dict:
    prop = roster_proposition(row["gene_set"], inputs["gene_set_entity"], row["collection"],
                              inputs["collection_entity"], inputs["n_genes"])
    return mint({
        "class_name": "Claim",
        "proposition": prop["id"],
        "statement": (
            f"The {row['collection']} gene-set collection used by PIGEAN defines the membership "
            f"of {row['gene_set']}: {inputs['n_genes']} genes observed in gene_gene_set_stats.json."
        ),
        "was_generated_by": inputs["activity"],
        "was_attributed_to": [inputs["agent"]],
        "was_derived_from": [inputs["source_file"]],
        "source_locator": f"gene_gene_set_stats.json#gene_set={row['gene_set']}",
    })


CLAIM_BUILDERS = {"gene": gene_claim, "gene_set": gene_set_claim, "roster": roster_claim}
