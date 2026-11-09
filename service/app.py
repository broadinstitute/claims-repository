"""Claims repository v0 service.

Composes api/graphql/core.graphql + api/graphql/methods/*.graphql into one
executable schema (docs/method-modules.md) served at /v1/graphql (GET: GraphiQL
explorer, POST: GraphQL-over-HTTP), alongside the REST endpoints from
api/openapi.yaml. The PIGEAN resolvers here play the role the binding layer
will generalize; they contain the only method-specific code and compile
filter/order/limit to SQL on the method's index tables (anchor limits apply
before joins).
"""
from __future__ import annotations

import json
from pathlib import Path

from ariadne import ObjectType, QueryType, ScalarType, graphql_sync, make_executable_schema
from ariadne.explorer import ExplorerGraphiQL
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.requests import Request

from . import db

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_VERSION = "0.2.0"
DAPPER_PROFILE = "0.2.0-a1"
DEFAULT_COLLECTION = "cfde-inc-v2"

PROVISIONAL_WARNING = (
    "Metric interpretations are provisional until confirmed by the method leads (Day-2 decision)."
)

# One source of truth for metric semantics in this service; the toolkit's
# template.yaml owns this table once module generation lands.
PIGEAN_METRICS = {
    "combined": {
        "score_kind": "SCORE",
        "scale": "log posterior odds",
        "better": "higher",
        "confirmed": False,
        "description": "Combined gene-phenotype support from direct GWAS evidence plus gene-set prior evidence. Not a probability.",
    },
    "log_bf": {
        "score_kind": "SCORE",
        "scale": "log Bayes factor",
        "better": "higher",
        "confirmed": False,
        "description": "Direct GWAS support for the gene-phenotype association. Not a probability.",
    },
    "prior": {
        "score_kind": "SCORE",
        "scale": "log prior odds",
        "better": "higher",
        "confirmed": False,
        "description": "Indirect support contributed by gene-set membership evidence.",
    },
    "beta": {
        "score_kind": "EFFECT_ESTIMATE",
        "scale": "log-odds effect",
        "better": "higher",
        "confirmed": False,
        "description": "Gene-set effect estimated jointly with the other selected gene sets (corrected).",
    },
    "beta_uncorrected": {
        "score_kind": "EFFECT_ESTIMATE",
        "scale": "log-odds effect",
        "better": "higher",
        "confirmed": False,
        "description": "Marginal gene-set effect, uncorrected for other gene sets.",
    },
}

GENE_FILTER_COLS = {"prior": "prior", "combined": "combined", "logBf": "log_bf"}
GENE_ORDER_COLS = {"COMBINED": "combined", "LOG_BF": "log_bf", "PRIOR": "prior"}
SET_FILTER_COLS = {"beta": "beta", "betaUncorrected": "beta_uncorrected"}
SET_ORDER_COLS = {"BETA": "beta", "BETA_UNCORRECTED": "beta_uncorrected"}
FILTER_OPS = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">=", "eq": "=", "ne": "!="}


def _load_type_defs() -> str:
    parts = [(ROOT / "api" / "graphql" / "core.graphql").read_text()]
    for mod in sorted((ROOT / "api" / "graphql" / "methods").glob("*.graphql")):
        parts.append(mod.read_text())
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Shared fetch/view helpers


def _fetch_payload(con, object_id):
    row = con.execute("SELECT payload FROM dapper_objects WHERE id = ?", (object_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def _fetch_payloads(con, object_ids):
    """Batch dereference: one IN query instead of one round trip per object
    (the difference between ~1 ms and ~25 ms per object against Aurora)."""
    ids = [i for i in object_ids if i]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = con.execute(
        f"SELECT id, payload FROM dapper_objects WHERE id IN ({marks})", ids
    ).fetchall()
    return {r["id"]: json.loads(r["payload"]) for r in rows}


def _dapper_refs(values, prefix):
    return [v for v in values if isinstance(v, str) and v.startswith(prefix)]


def _ts(value):
    """Timestamps come back as str from SQLite and datetime from MySQL;
    GraphQL String fields need str."""
    return None if value is None else str(value)


def envelope(con, claim_id):
    row = con.execute(
        "SELECT payload, times_seen, created_at, last_seen_at FROM dapper_objects WHERE id = ?",
        (claim_id,),
    ).fetchone()
    if row is None:
        return None
    claim = json.loads(row["payload"])
    proposition = _fetch_payload(con, claim.get("proposition")) or {}
    score_docs = _fetch_payloads(con, claim.get("has_score", []))
    scores = []
    for sid in claim.get("has_score", []):
        s = score_docs.get(sid)
        if s:
            scores.append(
                {
                    "id": s.get("id", sid),
                    "scoreKind": s["score_kind"],
                    "metric": s["metric"],
                    "value": s["value"],
                    "interpretation": s["interpretation"],
                }
            )
    entity_refs = [proposition.get("subject_entity"), proposition.get("object_entity")]
    gene_set_refs = _dapper_refs(entity_refs, "dapper:GeneSet.")
    file_refs = _dapper_refs(claim.get("was_derived_from", []), "dapper:")
    refs_to_resolve = gene_set_refs + file_refs
    resolution = "UNRESOLVED"
    if refs_to_resolve:
        placeholders = ",".join("?" * len(refs_to_resolve))
        found = con.execute(
            f"SELECT COUNT(*) AS c FROM dapper_objects WHERE id IN ({placeholders})",
            refs_to_resolve,
        ).fetchone()["c"]
        resolution = "RESOLVED" if found == len(refs_to_resolve) else "UNRESOLVED"
    triple = None
    if proposition.get("relation"):
        triple = {
            "subject": proposition.get("subject_entity", ""),
            "predicate": proposition["relation"],
            "object": proposition.get("object_entity", ""),
        }
    evidence = []
    evidence_docs = _fetch_payloads(con, claim.get("has_evidence", []))
    items = [evidence_docs[eid] for eid in claim.get("has_evidence", []) if eid in evidence_docs]
    locator_docs = _fetch_payloads(
        con, [vid for item in items for vid in item.get("value_locators", [])]
    )
    for item in items:
        locators = [
            locator_docs[vid] for vid in item.get("value_locators", []) if vid in locator_docs
        ]
        evidence.append({"item": item, "valueLocators": locators})
    return {
        "id": claim.get("id", claim_id),
        "claim": claim,
        "proposition": proposition,
        "triple": triple,
        "scores": scores,
        "evidence": evidence,
        "timesDeposited": row["times_seen"],
        "firstDepositedAt": _ts(row["created_at"]),
        "lastDepositedAt": _ts(row["last_seen_at"] or row["created_at"]),
        "provenanceRefs": {
            "derivedFromFiles": claim.get("was_derived_from", []),
            "geneSetRefs": gene_set_refs,
            "resolveVia": "provenance-indexer",
            "resolution": resolution,
        },
    }


def _phenotype_view(phenotype_id, trait_group):
    return {"id": phenotype_id, "name": None, "traitGroup": trait_group, "conceptId": None}


def _gene_assoc_view(con, row):
    return {
        "gene": {"symbol": row["gene"]},
        "phenotype": _phenotype_view(row["phenotype"], row["trait_group"]),
        "collection": row["collection"],
        "combined": row["combined"],
        "logBf": row["log_bf"],
        "prior": row["prior"],
        "claim": envelope(con, row["claim_id"]),
    }


def _dapper_gene_set_id(con, collection, name):
    row = con.execute(
        "SELECT dapper_id FROM gene_set_entities WHERE collection = ? AND name = ?",
        (collection, name),
    ).fetchone()
    return row["dapper_id"] if row else None


def _set_view(con, row):
    return {
        "name": row["gene_set"],
        "source": row["source"] or "",
        "collection": row["collection"],
        "dapperGeneSetId": _dapper_gene_set_id(con, row["collection"], row["gene_set"]),
    }


def _set_assoc_view(con, row):
    return {
        "geneSet": _set_view(con, row),
        "phenotype": _phenotype_view(row["phenotype"], row["trait_group"]),
        "collection": row["collection"],
        "beta": row["beta"],
        "betaUncorrected": row["beta_uncorrected"],
        "claim": envelope(con, row["claim_id"]),
    }


def _where_sql(where, colmap):
    frags, params = [], []
    for field, conds in (where or {}).items():
        col = colmap[field]
        for op, value in (conds or {}).items():
            frags.append(f"AND {col} {FILTER_OPS[op]} ?")
            params.append(value)
    return " ".join(frags), params


def _gene_assoc_rows(con, phenotype, collection, where, order_by, limit):
    where_sql, params = _where_sql(where, GENE_FILTER_COLS)
    field = (order_by or {}).get("field", "COMBINED")
    direction = "DESC" if (order_by or {}).get("dir", "DESC") == "DESC" else "ASC"
    sql = (
        "SELECT * FROM pigean_gene_associations WHERE phenotype = ? AND collection = ? "
        f"{where_sql} ORDER BY {GENE_ORDER_COLS[field]} {direction}, gene ASC LIMIT ?"
    )
    return con.execute(sql, [phenotype, collection, *params, limit]).fetchall()


def _set_assoc_rows(con, phenotype, collection, where, order_by, limit):
    where_sql, params = _where_sql(where, SET_FILTER_COLS)
    field = (order_by or {}).get("field", "BETA_UNCORRECTED")
    direction = "DESC" if (order_by or {}).get("dir", "DESC") == "DESC" else "ASC"
    sql = (
        "SELECT * FROM pigean_gene_set_associations WHERE phenotype = ? AND collection = ? "
        f"{where_sql} ORDER BY {SET_ORDER_COLS[field]} {direction}, gene_set ASC LIMIT ?"
    )
    return con.execute(sql, [phenotype, collection, *params, limit]).fetchall()


# ---------------------------------------------------------------------------
# GraphQL resolvers

query = QueryType()
phenotype_type = ObjectType("Phenotype")
gene_type = ObjectType("Gene")
gene_set_type = ObjectType("GeneSet")
json_scalar = ScalarType("JSON")


def _con(info):
    return info.context["con"]


@query.field("health")
def resolve_health(*_):
    return {"status": "ok", "contractVersion": CONTRACT_VERSION, "dapperProfile": DAPPER_PROFILE}


@query.field("methods")
def resolve_methods(*_):
    return [
        {
            "name": "pigean",
            "metrics": [
                {
                    "metric": name,
                    "scoreKind": sem["score_kind"],
                    "scale": sem["scale"],
                    "better": sem["better"].upper(),
                    "confirmed": sem["confirmed"],
                    "description": sem["description"],
                }
                for name, sem in PIGEAN_METRICS.items()
            ],
        }
    ]


@query.field("phenotype")
def resolve_phenotype(_, info, id):
    row = info.context["con"].execute(
        "SELECT phenotype, trait_group FROM pigean_gene_associations WHERE phenotype = ? "
        "UNION SELECT phenotype, trait_group FROM pigean_gene_set_associations WHERE phenotype = ? "
        "LIMIT 1",
        (id, id),
    ).fetchone()
    return _phenotype_view(row["phenotype"], row["trait_group"]) if row else None


@query.field("phenotypes")
def resolve_phenotypes(_, info, search=None, first=20):
    like = f"%{search}%" if search else "%"
    rows = info.context["con"].execute(
        "SELECT DISTINCT phenotype, trait_group FROM pigean_gene_associations "
        "WHERE phenotype LIKE ? ORDER BY phenotype LIMIT ?",
        (like, first),
    ).fetchall()
    return [_phenotype_view(r["phenotype"], r["trait_group"]) for r in rows]


@query.field("gene")
def resolve_gene(_, info, symbol):
    row = info.context["con"].execute(
        "SELECT 1 FROM pigean_gene_associations WHERE gene = ? LIMIT 1", (symbol,)
    ).fetchone()
    return {"symbol": symbol} if row else None


@query.field("geneSet")
def resolve_gene_set(_, info, name, collection=None):
    sql = "SELECT * FROM pigean_gene_set_associations WHERE gene_set = ?"
    params = [name]
    if collection:
        sql += " AND collection = ?"
        params.append(collection)
    row = info.context["con"].execute(sql + " LIMIT 1", params).fetchone()
    return _set_view(info.context["con"], row) if row else None


@query.field("claim")
def resolve_claim(_, info, id):
    return envelope(info.context["con"], id)


@query.field("dapperObject")
def resolve_dapper_object(_, info, id):
    return _fetch_payload(info.context["con"], id)


@query.field("proposition")
def resolve_proposition(_, info, id):
    con = _con(info)
    payload = _fetch_payload(con, id)
    if payload is None or payload.get("class_name") != "Proposition":
        return None
    claim_ids = [
        r["claim_id"]
        for r in con.execute(
            "SELECT claim_id FROM claims WHERE proposition_id = ? ORDER BY claim_id", (id,)
        )
    ]
    envelopes = [e for e in (envelope(con, cid) for cid in claim_ids) if e]
    return {
        "id": id,
        "proposition": payload,
        "triple": _triple_of(payload),
        "claims": envelopes,
    }


@phenotype_type.field("pigeanGeneAssociations")
def resolve_pheno_gene_assocs(obj, info, collection=DEFAULT_COLLECTION, where=None, orderBy=None, first=20):
    con = _con(info)
    return [_gene_assoc_view(con, r) for r in _gene_assoc_rows(con, obj["id"], collection, where, orderBy, first)]


@phenotype_type.field("pigeanGeneSetAssociations")
def resolve_pheno_set_assocs(obj, info, collection=DEFAULT_COLLECTION, where=None, orderBy=None, first=20):
    con = _con(info)
    return [_set_assoc_view(con, r) for r in _set_assoc_rows(con, obj["id"], collection, where, orderBy, first)]


@gene_type.field("pigeanAssociation")
def resolve_gene_assoc(obj, info, phenotype, collection=DEFAULT_COLLECTION):
    con = _con(info)
    row = con.execute(
        "SELECT * FROM pigean_gene_associations WHERE phenotype = ? AND collection = ? AND gene = ?",
        (phenotype, collection, obj["symbol"]),
    ).fetchone()
    return _gene_assoc_view(con, row) if row else None


@gene_set_type.field("pigeanAssociation")
def resolve_set_assoc(obj, info, phenotype, collection=DEFAULT_COLLECTION):
    con = _con(info)
    row = con.execute(
        "SELECT * FROM pigean_gene_set_associations WHERE phenotype = ? AND collection = ? AND gene_set = ?",
        (phenotype, collection, obj["name"]),
    ).fetchone()
    return _set_assoc_view(con, row) if row else None


def _membership_view(con, membership_row, set_row=None):
    gene_set = (
        _set_view(con, set_row)
        if set_row is not None
        else {
            "name": membership_row["gene_set"],
            "source": "",
            "collection": membership_row["collection"],
            "dapperGeneSetId": _dapper_gene_set_id(
                con, membership_row["collection"], membership_row["gene_set"]
            ),
        }
    )
    return {
        "gene": {"symbol": membership_row["gene"]},
        "geneSet": gene_set,
        "claim": envelope(con, membership_row["claim_id"]),
    }


@gene_type.field("memberships")
def resolve_gene_memberships(obj, info, collection=None, first=100):
    con = _con(info)
    sql = "SELECT * FROM claim_memberships WHERE gene = ?"
    params = [obj["symbol"]]
    if collection:
        sql += " AND collection = ?"
        params.append(collection)
    rows = con.execute(sql + " ORDER BY gene_set LIMIT ?", [*params, first]).fetchall()
    return [_membership_view(con, r) for r in rows]


@gene_set_type.field("members")
def resolve_set_members(obj, info, first=100):
    con = _con(info)
    rows = con.execute(
        "SELECT * FROM claim_memberships WHERE gene_set = ? AND collection = ? ORDER BY gene LIMIT ?",
        (obj["name"], obj["collection"], first),
    ).fetchall()
    return [_membership_view(con, r) for r in rows]


@query.field("pigeanValidationChallenge")
def resolve_challenge(
    _,
    info,
    phenotype,
    collection=DEFAULT_COLLECTION,
    where=None,
    orderBy=None,
    topGenes=20,
    setsPerGene=10,
    setOrder=None,
    first=100,
):
    con = _con(info)
    warnings = [PROVISIONAL_WARNING]
    anchors = _gene_assoc_rows(con, phenotype, collection, where, orderBy, topGenes)

    membership_count = con.execute(
        "SELECT COUNT(*) AS c FROM claim_memberships WHERE collection = ?", (collection,)
    ).fetchone()["c"]
    if membership_count == 0:
        warnings.append(
            f"No gene-set membership claims are deposited for collection '{collection}', so the "
            "(gene)-[member_of]->(gene_set) join produced no rows. This reflects a missing data "
            "source (membership ingest pending for this collection), not an empty scientific result."
        )
        return {"rows": [], "truncated": False, "warnings": warnings}

    truncated = len(anchors) > first
    kept = anchors[:first]
    sets_by_gene = {r["gene"]: [] for r in kept}
    if kept:
        rfield = SET_ORDER_COLS[(setOrder or {}).get("field", "BETA_UNCORRECTED")]
        rdir = "DESC" if (setOrder or {}).get("dir", "DESC") == "DESC" else "ASC"
        placeholders = ",".join("?" * len(kept))
        # Per-gene window: each anchor gene keeps its own top-N sets, so one
        # gene's strong sets can never crowd another gene out of the result.
        joined = con.execute(
            "SELECT * FROM ("
            "SELECT m.gene AS m_gene, m.claim_id AS m_claim_id, m.collection AS m_collection, s.*, "
            f"ROW_NUMBER() OVER (PARTITION BY m.gene ORDER BY s.{rfield} {rdir}, m.gene_set ASC) AS set_rank "
            "FROM claim_memberships m "
            "JOIN pigean_gene_set_associations s "
            "  ON s.gene_set = m.gene_set AND s.collection = m.collection AND s.phenotype = ? "
            f"WHERE m.collection = ? AND m.gene IN ({placeholders})"
            ") AS ranked WHERE set_rank <= ? ORDER BY m_gene ASC, set_rank ASC",
            [phenotype, collection, *sets_by_gene.keys(), setsPerGene],
        ).fetchall()
        for r in joined:
            membership_row = {
                "gene": r["m_gene"],
                "gene_set": r["gene_set"],
                "collection": r["m_collection"],
                "claim_id": r["m_claim_id"],
            }
            sets_by_gene[r["m_gene"]].append(
                {
                    "membership": _membership_view(con, membership_row, set_row=r),
                    "geneSetAssociation": _set_assoc_view(con, r),
                }
            )

    rows = [
        {"geneAssociation": _gene_assoc_view(con, r), "sets": sets_by_gene[r["gene"]]}
        for r in kept
    ]
    return {"rows": rows, "truncated": truncated, "warnings": warnings}


schema = make_executable_schema(
    _load_type_defs(), query, phenotype_type, gene_type, gene_set_type, json_scalar
)

# ---------------------------------------------------------------------------
# HTTP app

app = FastAPI(title="Claims Repository", version=CONTRACT_VERSION)

DEFAULT_EXPLORER_QUERY = """\
# Claims repository — PIGEAN IBD examples (schema docs via the sidebar).
# Top IBD genes whose direct GWAS evidence outruns their gene-set support:
{
  phenotype(id: "IBD") {
    pigeanGeneAssociations(
      where: {prior: {lt: 0.5}}
      orderBy: {field: COMBINED, dir: DESC}
      first: 5
    ) {
      gene { symbol }
      combined
      logBf
      prior
      claim {
        id
        triple { subject predicate object }
        scores { metric value interpretation }
        provenanceRefs { derivedFromFiles resolution }
      }
    }
  }
}
"""

EXPLORER_HTML = ExplorerGraphiQL(
    title="Claims Repository", default_query=DEFAULT_EXPLORER_QUERY
).html(None)


@app.get("/")
def root(request: Request):
    # root_path keeps the redirect correct when the app is mounted under a
    # platform prefix (/api/claims on dig-service-platform).
    return RedirectResponse(f"{request.scope.get('root_path', '')}/v1/graphql")


@app.get("/v1/health")
def health():
    return {"status": "ok", "contract_version": CONTRACT_VERSION, "dapper_profile": DAPPER_PROFILE}


@app.get("/v1/vocabulary")
def vocabulary():
    con = db.connect()
    try:
        phenotypes = [
            {"id": r["phenotype"], "name": None, "trait_group": r["trait_group"], "concept_id": None}
            for r in con.execute(
                "SELECT DISTINCT phenotype, trait_group FROM pigean_gene_associations ORDER BY phenotype"
            ).fetchall()
        ]
        collections = [
            r["collection"]
            for r in con.execute(
                "SELECT DISTINCT collection FROM pigean_gene_associations ORDER BY collection"
            ).fetchall()
        ]
    finally:
        con.close()
    return {
        "contract_version": CONTRACT_VERSION,
        "categories": ["gene", "phenotype", "gene_set"],
        "predicates": [
            {"subject_category": "gene", "predicate": "associated_with", "object_category": "phenotype", "methods": ["pigean"]},
            {"subject_category": "gene_set", "predicate": "associated_with", "object_category": "phenotype", "methods": ["pigean"]},
            {"subject_category": "gene", "predicate": "member_of", "object_category": "gene_set", "methods": ["pigean"]},
        ],
        "methods": ["pigean"],
        "metrics": {f"pigean.{name}": sem for name, sem in PIGEAN_METRICS.items()},
        "phenotypes": phenotypes,
        "collections": collections,
    }


@app.get("/v1/graphql", response_class=HTMLResponse)
def graphql_explorer():
    return EXPLORER_HTML


@app.post("/v1/graphql")
async def graphql_endpoint(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be JSON.")
    con = db.connect()
    try:
        success, result = graphql_sync(schema, data, context_value={"con": con}, debug=True)
    finally:
        con.close()
    return JSONResponse(result, status_code=200 if success else 400)


def _index_hint(con, hint, bundle_objects):
    claim_id = hint.get("claim_id")
    claim = bundle_objects.get(claim_id) or _fetch_payload(con, claim_id)
    if claim is None:
        raise HTTPException(status_code=400, detail=f"index hint references unknown claim {claim_id}")
    scores = {}
    for sid in claim.get("has_score", []):
        s = bundle_objects.get(sid) or _fetch_payload(con, sid)
        if s:
            scores[s["metric"]] = s["value"]
    subject, obj = hint.get("subject") or {}, hint.get("object") or {}
    collection = hint.get("collection", DEFAULT_COLLECTION)
    trait_group = hint.get("trait_group")
    predicate = hint.get("predicate")
    upsert = con.dialect.insert_or_replace
    con.execute(
        f"{upsert} claims (claim_id, proposition_id, method) VALUES (?, ?, ?)",
        (claim_id, claim.get("proposition", ""), hint.get("method", "")),
    )
    if predicate == "associated_with" and subject.get("category") == "gene":
        missing = [m for m in ("prior", "combined", "log_bf") if m not in scores]
        if missing:
            raise HTTPException(status_code=400, detail=f"claim {claim_id} missing scores {missing} for gene association index")
        con.execute(
            f"{upsert} pigean_gene_associations "
            "(phenotype, collection, gene, prior, combined, log_bf, n, trait_group, claim_id) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (obj.get("id"), collection, subject.get("id"), scores["prior"], scores["combined"], scores["log_bf"], trait_group, claim_id),
        )
    elif predicate == "associated_with" and subject.get("category") == "gene_set":
        missing = [m for m in ("beta", "beta_uncorrected") if m not in scores]
        if missing:
            raise HTTPException(status_code=400, detail=f"claim {claim_id} missing scores {missing} for gene-set association index")
        con.execute(
            f"{upsert} pigean_gene_set_associations "
            "(phenotype, collection, gene_set, source, beta, beta_uncorrected, rs_score, n, trait_group, claim_id) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (obj.get("id"), collection, subject.get("id"), hint.get("source"), scores["beta"], scores["beta_uncorrected"], trait_group, claim_id),
        )
        if subject.get("dapper_id"):
            con.execute(
                f"{upsert} gene_set_entities (collection, name, dapper_id, n_genes) VALUES (?, ?, ?, NULL)",
                (collection, subject.get("id"), subject.get("dapper_id")),
            )
    elif predicate == "member_of":
        con.execute(
            f"{upsert} claim_memberships (collection, gene, gene_set, claim_id) VALUES (?, ?, ?, ?)",
            (collection, subject.get("id"), obj.get("id"), claim_id),
        )
        if obj.get("dapper_id"):
            con.execute(
                f"{upsert} gene_set_entities (collection, name, dapper_id, n_genes) VALUES (?, ?, ?, NULL)",
                (collection, obj.get("id"), obj.get("dapper_id")),
            )
    else:
        raise HTTPException(status_code=400, detail=f"unsupported index hint predicate {predicate!r}")


@app.post("/v1/claims")
async def deposit(request: Request):
    # v0: structural checks + idempotent storage. Full DAPPER-profile validation
    # and content-address verification are Task 5 hardening (see openapi.yaml).
    body = await request.json()
    objects = body.get("objects") or []
    if not objects:
        raise HTTPException(status_code=400, detail="objects is required and must be non-empty")
    con = db.connect()
    try:
        deposited = []
        bundle_objects = {}
        with con:
            for obj in objects:
                oid, cls = obj.get("id"), obj.get("class_name")
                if not oid or not cls:
                    raise HTTPException(status_code=400, detail=f"object missing id or class_name: {json.dumps(obj)[:200]}")
                bundle_objects[oid] = obj
                exists = con.execute("SELECT 1 FROM dapper_objects WHERE id = ?", (oid,)).fetchone()
                if exists:
                    # Idempotent re-deposit of the same content address: count
                    # how often this document keeps being minted (2026-11-09).
                    con.execute(
                        "UPDATE dapper_objects SET times_seen = times_seen + 1, "
                        f"last_seen_at = {con.dialect.now} WHERE id = ?",
                        (oid,),
                    )
                    times = con.execute(
                        "SELECT times_seen FROM dapper_objects WHERE id = ?", (oid,)
                    ).fetchone()["times_seen"]
                else:
                    con.execute(
                        "INSERT INTO dapper_objects (id, class_name, identity_profile, payload) VALUES (?, ?, ?, ?)",
                        (oid, cls, obj.get("identity_profile", "unverified"), json.dumps(obj)),
                    )
                    times = 1
                if cls == "Claim":
                    # Register every deposited Claim with its proposition so
                    # the fan-in (/v1/propositions/{id}, Query.proposition)
                    # sees claims that carry no graph-edge index hint, e.g.
                    # interpretive tier claims. A later index_hint overwrites
                    # this row with the claim's method.
                    con.execute(
                        f"{con.dialect.insert_or_ignore} claims (claim_id, proposition_id, method) VALUES (?, ?, ?)",
                        (oid, obj.get("proposition", ""), ""),
                    )
                deposited.append(
                    {
                        "id": oid,
                        "class_name": cls,
                        "status": "exists" if exists else "created",
                        "times_deposited": times,
                    }
                )
            for hint in body.get("index_hints") or []:
                _index_hint(con, hint, bundle_objects)
        return {"deposited": deposited}
    finally:
        con.close()


@app.get("/v1/objects/{object_id}")
def get_object(object_id: str):
    """Trace any deposited dapper object (GeneSet, GeneSetCollection, Activity,
    File, ...) by its ID — companion to ClaimEnvelope.provenanceRefs."""
    con = db.connect()
    try:
        payload = _fetch_payload(con, object_id)
    finally:
        con.close()
    if payload is None:
        raise HTTPException(status_code=404, detail=f"no deposited object with id {object_id}")
    return payload


def _expand_entity_ids(con, value):
    """A gene-set NAME also matches claims whose propositions reference the
    set's dapper:GeneSet ID (how set propositions are encoded)."""
    ids = [value]
    for r in con.execute("SELECT dapper_id FROM gene_set_entities WHERE name = ?", (value,)):
        ids.append(r["dapper_id"])
    return ids


def _proposition_filters(con, *, predicate=None, subject=None, object_=None,
                         entity=None, q=None):
    """WHERE fragments over a proposition payload aliased `p` — shared by the
    claim and proposition search endpoints so both speak the same language."""
    je = con.dialect.je
    where, params = [], []
    if predicate:
        where.append(f"{je('p.payload', 'relation')} = ?")
        params.append(predicate)
    for field, value in (("subject_entity", subject), ("object_entity", object_)):
        if value:
            ids = _expand_entity_ids(con, value)
            where.append(f"{je('p.payload', field)} IN ({','.join('?' * len(ids))})")
            params.extend(ids)
    if entity:
        ids = _expand_entity_ids(con, entity)
        marks = ",".join("?" * len(ids))
        where.append(
            f"({je('p.payload', 'subject_entity')} IN ({marks}) "
            f"OR {je('p.payload', 'object_entity')} IN ({marks}))"
        )
        params.extend(ids * 2)
    if q:
        where.append(f"{je('p.payload', 'statement')} LIKE ?")
        params.append(f"%{q}%")
    return where, params


def _triple_of(prop: dict):
    if not prop.get("relation"):
        return None
    return {
        "subject": prop.get("subject_entity", ""),
        "predicate": prop["relation"],
        "object": prop.get("object_entity", ""),
    }


def _claim_search(con, *, method=None, predicate=None, subject=None, object_=None,
                  entity=None, q=None, limit=50, offset=0):
    """Shared engine for the REST list/search endpoints: deterministic
    claim_id order, filters ANDed, total computed over the same WHERE."""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    where, params = _proposition_filters(
        con, predicate=predicate, subject=subject, object_=object_, entity=entity, q=q
    )
    if method:
        where.append("c.method = ?")
        params.append(method)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    base = (
        "FROM claims c "
        "JOIN dapper_objects p ON p.id = c.proposition_id "
        "JOIN dapper_objects d ON d.id = c.claim_id" + where_sql
    )
    total = con.execute(f"SELECT COUNT(*) AS c {base}", params).fetchone()["c"]
    rows = con.execute(
        f"SELECT c.claim_id, c.method, p.payload AS proposition, d.times_seen, d.created_at "
        f"{base} ORDER BY c.claim_id ASC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    claims = []
    for r in rows:
        prop = json.loads(r["proposition"])
        claims.append(
            {
                "claim_id": r["claim_id"],
                "method": r["method"],
                "triple": {
                    "subject": prop.get("subject_entity"),
                    "predicate": prop.get("relation"),
                    "object": prop.get("object_entity"),
                },
                "statement": prop.get("statement"),
                "times_deposited": r["times_seen"],
                "deposited_at": _ts(r["created_at"]),
                "url": f"/v1/claims/{r['claim_id']}",
            }
        )
    return {"total": total, "limit": limit, "offset": offset, "claims": claims}


@app.get("/v1/claims")
def list_claims(method: str | None = None, predicate: str | None = None,
                limit: int = 50, offset: int = 0):
    """Browse deposited claims: compact summaries (triple, statement, mint
    count), deterministic claim_id order, limit/offset pagination."""
    con = db.connect()
    try:
        return _claim_search(con, method=method, predicate=predicate, limit=limit, offset=offset)
    finally:
        con.close()


@app.get("/v1/claims/search")
def search_claims(q: str | None = None, subject: str | None = None,
                  object: str | None = None, entity: str | None = None,
                  predicate: str | None = None, method: str | None = None,
                  limit: int = 50, offset: int = 0):
    """Search claims by proposition content: `entity` matches subject or
    object (a gene-set name also matches its dapper:GeneSet ID), `subject` /
    `object` pin one side, `q` substring-matches the proposition statement.
    Filters are ANDed; same response shape as the list endpoint."""
    con = db.connect()
    try:
        return _claim_search(
            con, method=method, predicate=predicate, subject=subject,
            object_=object, entity=entity, q=q, limit=limit, offset=offset,
        )
    finally:
        con.close()


def _proposition_search(con, *, predicate=None, subject=None, object_=None,
                        entity=None, q=None, limit=50, offset=0):
    """Same search language as _claim_search, over propositions; each summary
    carries n_claims, the fan-in of assessments on that statement."""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    where, params = _proposition_filters(
        con, predicate=predicate, subject=subject, object_=object_, entity=entity, q=q
    )
    where_sql = " AND ".join(["p.class_name = 'Proposition'", *where])
    total = con.execute(
        f"SELECT COUNT(*) AS c FROM dapper_objects p WHERE {where_sql}", params
    ).fetchone()["c"]
    rows = con.execute(
        "SELECT p.id, p.payload, p.times_seen, "
        "(SELECT COUNT(*) FROM claims c WHERE c.proposition_id = p.id) AS n_claims "
        f"FROM dapper_objects p WHERE {where_sql} ORDER BY p.id ASC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    propositions = []
    for r in rows:
        prop = json.loads(r["payload"])
        propositions.append(
            {
                "proposition_id": r["id"],
                "triple": _triple_of(prop),
                "statement": prop.get("statement"),
                "proposition_kind": prop.get("proposition_kind"),
                "n_claims": r["n_claims"],
                "times_deposited": r["times_seen"],
                "url": f"/v1/propositions/{r['id']}",
            }
        )
    return {"total": total, "limit": limit, "offset": offset, "propositions": propositions}


def _claim_brief(con, claim_id, method=None):
    """Compact claim view for a proposition's fan-in: what distinguishes the
    claims of one proposition is the assessment, not the triple."""
    row = con.execute(
        "SELECT payload, times_seen FROM dapper_objects WHERE id = ?", (claim_id,)
    ).fetchone()
    if row is None:
        return None
    claim = json.loads(row["payload"])
    scores = []
    for sid in claim.get("has_score", []):
        s = _fetch_payload(con, sid)
        if s:
            scores.append({"metric": s["metric"], "value": s["value"]})
    return {
        "claim_id": claim_id,
        "method": method or None,
        "statement": claim.get("statement"),
        "direction": claim.get("direction"),
        "scores": scores,
        "times_deposited": row["times_seen"],
        "url": f"/v1/claims/{claim_id}",
    }


@app.get("/v1/propositions")
def list_propositions(predicate: str | None = None, limit: int = 50, offset: int = 0):
    """Browse deposited propositions: compact summaries with the structured
    triple and n_claims (how many claims assess each statement)."""
    con = db.connect()
    try:
        return _proposition_search(con, predicate=predicate, limit=limit, offset=offset)
    finally:
        con.close()


@app.get("/v1/propositions/search")
def search_propositions(q: str | None = None, subject: str | None = None,
                        object: str | None = None, entity: str | None = None,
                        predicate: str | None = None, limit: int = 50, offset: int = 0):
    """Search propositions in the same fashion as /v1/claims/search: `entity`
    matches either side of the triple (gene-set names expand to their
    dapper:GeneSet IDs), `q` substring-matches the statement."""
    con = db.connect()
    try:
        return _proposition_search(
            con, predicate=predicate, subject=subject, object_=object,
            entity=entity, q=q, limit=limit, offset=offset,
        )
    finally:
        con.close()


@app.get("/v1/propositions/{proposition_id}")
def get_proposition(proposition_id: str):
    """One proposition with EVERY claim that assesses it — the fan-in the
    proposition/claim split exists for (measurement, tiers, other methods)."""
    con = db.connect()
    try:
        payload = _fetch_payload(con, proposition_id)
        if payload is None or payload.get("class_name") != "Proposition":
            raise HTTPException(status_code=404, detail=f"no deposited Proposition with id {proposition_id}")
        rows = con.execute(
            "SELECT claim_id, method FROM claims WHERE proposition_id = ? ORDER BY claim_id",
            (proposition_id,),
        ).fetchall()
        briefs = [b for b in (_claim_brief(con, r["claim_id"], r["method"]) for r in rows) if b]
        return {"proposition": payload, "triple": _triple_of(payload), "claims": briefs}
    finally:
        con.close()


@app.get("/v1/claims/{claim_id}")
def get_claim(claim_id: str):
    con = db.connect()
    try:
        env = envelope(con, claim_id)
    finally:
        con.close()
    if env is None:
        raise HTTPException(status_code=404, detail=f"no deposited object with id {claim_id}")
    return {
        "claim": env["claim"],
        "proposition": env["proposition"],
        "triple": env["triple"],
        "scores": env["scores"],
        "evidence": env["evidence"],
        "deposits": {
            "times": env["timesDeposited"],
            "first_at": env["firstDepositedAt"],
            "last_at": env["lastDepositedAt"],
        },
        "provenance_refs": {
            "derived_from_files": env["provenanceRefs"]["derivedFromFiles"],
            "gene_set_refs": env["provenanceRefs"]["geneSetRefs"],
            "resolve_via": "provenance-indexer",
            "resolution": env["provenanceRefs"]["resolution"].lower(),
        },
    }
