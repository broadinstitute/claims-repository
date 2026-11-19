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

import html
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from ariadne import QueryType, ScalarType, graphql_sync, make_executable_schema
from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from . import api_keys
from . import explorer
from . import dapper_validation as dval
from . import db
from . import pigean_template as tpl
from . import telemetry
from .collections import catalog as ccatalog
from .collections import identity as cidn
from .collections import serve as cserve
from .collections.manifest import ManifestError
from .refs import DAPPER_ID_RE, MAX_ID_LENGTH, dapper_refs

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_VERSION = "0.2.0"
DAPPER_PROFILE = "0.2.0-a1"
DEFAULT_COLLECTION = "cfde-inc-v2"  # deposit hints' default gene-set collection

# Identity vs location (design decision 2026-11-09, per Chase): the
# content-addressed CURIE (dapper:{Class}.{digest}) is the canonical,
# location-independent identifier; URLs are presentations of it and never
# enter hashed content. This service is a resolver: any dapper ID
# dereferences at {PUBLIC_BASE}/v1/objects/{id}. A permanent-IRI layer
# (w3id.org/dapper/object/...) is proposed to the dapper maintainers, phased
# with the DAPPER-ID-1 identity cutover.
PUBLIC_BASE = os.environ.get("CLAIMS_PUBLIC_BASE_URL", "").rstrip("/")

# Prefix expansions mirrored from the dapper schema (dapper.yaml prefixes):
# the vocabulary namespace (classes/slots) is deliberately distinct from the
# instance namespace this resolver serves.
DAPPER_PREFIXES = {
    "dapper": "https://broadinstitute.github.io/dapper/ns#",
    "dapper_class": "https://broadinstitute.github.io/dapper/model/reference/classes/",
    "dapper_slot": "https://broadinstitute.github.io/dapper/model/reference/slots/",
    "dapper_enum": "https://broadinstitute.github.io/dapper/model/reference/enums/",
    # KPN trait IDs (broadinstitute/kpn-data-models), the canonical phenotype
    # identifiers claims name (e.g. KPN.TRAIT:0000505 = IBD).
    "KPN.TRAIT": "https://broadinstitute.github.io/kpn-data-models/kpn.trait/",
}


def _resolver_url(object_id: str) -> str:
    """The shareable URL form of a dapper ID: this service's resolver.
    Absolute when CLAIMS_PUBLIC_BASE_URL is set (QA/prod), else same-origin."""
    return f"{PUBLIC_BASE}/v1/objects/{object_id}"


def identifier_resolution() -> dict:
    return {
        "canonical_form": "CURIE dapper:{Class}.{digest} — content-addressed, location-independent; URLs are presentations of it",
        "resolver_url_template": f"{PUBLIC_BASE}/v1/objects/{{id}}",
        "prefixes": DAPPER_PREFIXES,
    }

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

def _load_type_defs() -> str:
    return "\n".join((ROOT / "api" / "graphql" / name).read_text() for name in ("core.graphql", "collections.graphql"))


# ---------------------------------------------------------------------------
# Shared fetch/view helpers


def _fetch_payload(con, object_id):
    row = con.execute("SELECT payload FROM dapper_objects WHERE id = ?", (object_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def _class_of(payload: dict) -> str | None:
    """A document's class: local-dev documents carry class_name; DAPPER-ID-1
    documents are plain DAPPER nodes whose class is part of the id."""
    return payload.get("class_name") or cidn.class_of(payload.get("id") or "")


def _present_object(payload: dict) -> dict:
    """Uniform presentation for every resolved dapper object, whatever its
    class or depositor: id and class_name first, remaining keys sorted.
    Content is untouched — this is ordering only."""
    rest = {k: payload[k] for k in sorted(payload) if k not in ("id", "class_name")}
    return {"id": payload.get("id"), "class_name": _class_of(payload), **rest}


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


def _id_list(value) -> list:
    """The string ids in a reference-list slot. Stored documents (external
    deposits) are not schema-validated, and a typed GraphQL field must never
    fail a whole response over one malformed slot."""
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _dapper_refs(values, prefix):
    return [v for v in values if isinstance(v, str) and v.startswith(prefix)]


def _ts(value):
    """Timestamps come back as str from SQLite and datetime from MySQL;
    GraphQL String fields need str."""
    return None if value is None else str(value)


# ---------------------------------------------------------------------------
# Claim synthesis — claims are minted when queried (2026-11-09, Chase).
#
# Each index row names its claim's precomputed content address (claim_id) and
# carries synthesis_inputs; the claim document is synthesized from the row +
# the shared template, purely (no queries). Synthesis runs on EVERY serve of
# a template-backed row, and the claim is refused unless its address equals
# the row's precomputed one and — once stored — it is identical to the stored
# document: template drift, a tampered row, or a forged deposit at the row's
# address are never served. GraphQL requests run in one transaction committed
# only when the whole response succeeded (see _execute_graphql), so a failure
# leaves no partial mints or counts behind.


class ClaimSynthesisError(RuntimeError):
    """The template no longer reproduces the claim its index row names, or
    the stored document at that address differs from it."""


# (family, index table) for every PIGEAN index row that implies a claim.
CLAIM_SOURCES = (
    ("gene", "pigean_gene_associations"),
    ("gene_set", "pigean_gene_set_associations"),
    ("roster", "pigean_gene_set_rosters"),
)


def _index_source(con, claim_id, families=None):
    """The template-backed index row that implies this claim, or None."""
    if not families or "collection" in families:
        rc = con.execute("SELECT * FROM result_claims WHERE claim_id = ?", (claim_id,)).fetchone()
        if rc is not None:
            return "collection", rc
    for family, table in CLAIM_SOURCES:
        if families and family not in families:
            continue
        row = con.execute(
            f"SELECT * FROM {table} t WHERE claim_id = ? AND synthesis_inputs IS NOT NULL{_live_roster(family)} LIMIT 1",
            (claim_id,),
        ).fetchone()
        if row is not None:
            return family, row
    return None


def _live_roster(family):
    """A roster claim is implied only while it backs a membership edge: a
    reload with a different roster supersedes it."""
    if family != "roster":
        return ""
    return " AND EXISTS (SELECT 1 FROM claim_memberships m WHERE m.claim_id = t.claim_id)"


def _inputs_of(row):
    raw = row["synthesis_inputs"]
    if raw is None:
        return None
    return raw if isinstance(raw, dict) else json.loads(raw)


def synthesize_claim(family, row):
    """The claim document a template-backed index row implies, verified
    against the row's precomputed content address. Pure: no database access."""
    inputs = _inputs_of(row)
    if inputs is None:
        raise ClaimSynthesisError(f"{row['claim_id']} is not template-backed (no synthesis inputs)")
    try:
        claim = tpl.CLAIM_BUILDERS[family](row, inputs)
    except (KeyError, IndexError, TypeError, ValueError) as exc:  # sqlite3.Row raises IndexError
        raise ClaimSynthesisError(
            f"synthesis inputs incomplete for {row['claim_id']} ({type(exc).__name__}: {exc})"
        ) from exc
    if claim["id"] != row["claim_id"]:
        raise ClaimSynthesisError(
            f"template drift: index row names {row['claim_id']} but the template synthesizes "
            f"{claim['id']}; refusing to serve a claim that differs from the one its data implies"
        )
    return claim


def _store_object(con, obj, identity_profile):
    """Upsert one dapper object (re-mint => times_seen += 1) plus its refs.
    No commit: callers own the transaction."""
    con.execute(
        con.dialect.upsert_object,
        (obj["id"], obj["class_name"], identity_profile, tpl.canonical(obj)),
    )
    refs = sorted(dapper_refs(obj))
    if refs:
        con.executemany(con.dialect.insert_ref, [(obj["id"], ref) for ref in refs])


_ENVELOPE_COLS = "payload, class_name, times_seen, created_at, last_seen_at"


# Assertions are RECORDED while a GraphQL response is resolved and written in
# one short batch after it succeeded (_flush_assertions), in sorted claim-id
# order: every request takes its row locks in the same order, so concurrent
# requests serving overlapping claims cannot deadlock, and a failed response
# writes nothing. The budget bounds what one request (aliases included) can
# assert.
MAX_ASSERTIONS_PER_REQUEST = 2000


def _now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def envelope(con, claim_id, assert_claim=True, source=None):
    """The claim envelope, or None when `claim_id` is not a claim.

    With `assert_claim` (the scientific query surface) serving ASSERTS the
    claim: a template-backed claim is synthesized and verified against its
    index row (and the stored document) on every serve, and the assertion —
    a first-serve mint, or a re-assertion — is recorded once per request for
    the end-of-request flush. `source` is the (family, index row) when the
    caller already has it. Counts in the envelope are as of this response."""
    row = con.execute(f"SELECT {_ENVELOPE_COLS} FROM dapper_objects WHERE id = ?", (claim_id,)).fetchone()
    if row is not None and row["class_name"] != "Claim":
        return None
    if not assert_claim:
        if row is None:
            return None
        claim = json.loads(row["payload"])
        times, first, last = row["times_seen"], _ts(row["created_at"]), _ts(row["last_seen_at"] or row["created_at"])
    else:
        collection = source is not None and source[0] == "collection"
        if source is None or (not collection and _inputs_of(source[1]) is None):
            source = _index_source(con, claim_id) or source
            collection = source is not None and source[0] == "collection"
        synthesized = None
        if collection:
            try:
                synthesized = cserve.render_claim(con, source[1])
            except (cserve.ClaimRenderError, cserve.engine.TemplateError, cidn.MintError) as exc:
                raise ClaimSynthesisError(str(exc)) from exc
        elif source is not None and _inputs_of(source[1]) is not None:
            synthesized = synthesize_claim(*source)
        if row is None and synthesized is None:
            return None
        if row is not None and synthesized is not None and json.loads(row["payload"]) != synthesized:
            raise ClaimSynthesisError(
                f"stored document at {claim_id} differs from the claim its index row implies; "
                "refusing to serve it"
            )
        assertions = getattr(con, "assertions", None)
        if assertions is None:
            assertions = con.assertions = {}
        if claim_id not in assertions:
            if len(assertions) >= MAX_ASSERTIONS_PER_REQUEST:
                raise ValueError(
                    f"this request would assert more than {MAX_ASSERTIONS_PER_REQUEST} claims; "
                    "narrow it (fewer rows or aliases)"
                )
            if row is None:
                assertions[claim_id] = {
                    "new": True, "payload": tpl.canonical(synthesized),
                    "identity_profile": cidn.PROFILE if collection else tpl.IDENTITY_PROFILE,
                    "method": cserve.collection_of(con, source[1]) if collection else tpl.METHOD,
                    "proposition": synthesized["proposition"], "refs": sorted(dapper_refs(synthesized)),
                }
            else:
                assertions[claim_id] = {"new": False, "payload": row["payload"], "identity_profile": "unverified"}
        claim = synthesized if row is None else json.loads(row["payload"])
        now = _now_ts()
        times = (row["times_seen"] if row is not None else 0) + 1
        first = _ts(row["created_at"]) if row is not None else now
        last = now
    proposition = (_fetch_payload(con, claim["proposition"]) if isinstance(claim.get("proposition"), str) else None) or {}
    score_ids = _id_list(claim.get("has_score"))
    score_docs = _fetch_payloads(con, score_ids)
    scores = []
    for sid in score_ids:
        s = score_docs.get(sid)
        if _well_formed_score(s):
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
    derived_from = _id_list(claim.get("was_derived_from"))
    file_refs = _dapper_refs(derived_from, "dapper:")
    refs_to_resolve = gene_set_refs + file_refs
    resolution = "UNRESOLVED"
    if refs_to_resolve:
        placeholders = ",".join("?" * len(refs_to_resolve))
        found = con.execute(
            f"SELECT COUNT(*) AS c FROM dapper_objects WHERE id IN ({placeholders})",
            refs_to_resolve,
        ).fetchone()["c"]
        resolution = "RESOLVED" if found == len(refs_to_resolve) else "UNRESOLVED"
    triple = _triple_of(proposition)
    evidence = []
    evidence_ids = _id_list(claim.get("has_evidence"))
    evidence_docs = _fetch_payloads(con, evidence_ids)
    items = [evidence_docs[eid] for eid in evidence_ids if isinstance(evidence_docs.get(eid), dict)]
    locator_docs = _fetch_payloads(
        con, [vid for item in items for vid in _id_list(item.get("value_locators"))]
    )
    for item in items:
        locators = [
            locator_docs[vid] for vid in _id_list(item.get("value_locators")) if vid in locator_docs
        ]
        evidence.append({"item": _present_object(item), "valueLocators": [_present_object(loc) for loc in locators]})
    return {
        "id": claim.get("id", claim_id),
        "resolverUrl": _resolver_url(claim_id),
        "claim": claim,
        "proposition": proposition,
        "triple": triple,
        "scores": scores,
        "evidence": evidence,
        "timesDeposited": times,
        "firstDepositedAt": first,
        "lastDepositedAt": last,
        "provenanceRefs": {
            "derivedFromFiles": derived_from,
            "geneSetRefs": gene_set_refs,
            "resolveVia": "provenance-indexer",
            "resolution": resolution,
        },
    }


# I-JSON (RFC 7493) / Number.MAX_SAFE_INTEGER, for integer LITERALS. A number
# written as a float is an IEEE-754 double: it must be finite, and digits
# beyond double precision are rounded on parse (9007199254740993.0 is stored
# as 9007199254740992.0), as every JSON consumer would.
MAX_EXACT_INT = 2 ** 53 - 1
MAX_JSON_DEPTH = 64  # well under MySQL's JSON depth limit (100) and every recursive walk
# The GraphQL ScoreKind enum (DAPPER ClaimScoreKindEnum): any other value
# cannot be serialized, so a score carrying one is not well-formed.
SCORE_KINDS = frozenset({"PROBABILITY", "POSTERIOR_PROBABILITY", "EFFECT_ESTIMATE", "LOADING", "SCORE"})


def _finite_number(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return abs(v) <= MAX_EXACT_INT
    return isinstance(v, float) and math.isfinite(v)


def _well_formed_score(s) -> bool:
    return (
        isinstance(s, dict)
        and all(isinstance(s.get(k), str) for k in ("score_kind", "metric", "interpretation"))
        and s["score_kind"] in SCORE_KINDS
        and _finite_number(s.get("value"))
    )


def _flush_assertions(con):
    """Write the request's recorded assertions in one batch, sorted by claim
    id (deterministic lock order). Upserts mint new claims and count
    re-assertions (times_seen += 1) alike; a claim minted concurrently by
    another request is simply counted."""
    assertions = getattr(con, "assertions", None) or {}
    if not assertions:
        return
    items = sorted(assertions.items())
    con.executemany(
        con.dialect.upsert_object,
        [(cid, "Claim", a["identity_profile"], a["payload"]) for cid, a in items],
    )
    new = [(cid, a) for cid, a in items if a["new"]]
    if new:
        con.executemany(
            f"{con.dialect.insert_or_ignore} claims (claim_id, proposition_id, method) VALUES (?, ?, ?)",
            [(cid, a["proposition"], a.get("method", tpl.METHOD)) for cid, a in new],
        )
        refs = [(cid, ref) for cid, a in new for ref in a["refs"]]
        if refs:
            con.executemany(con.dialect.insert_ref, refs)


def _is_lock_conflict(exc) -> bool:
    """MySQL deadlock / lock-wait timeout, or SQLite busy/locked: retryable."""
    if type(exc).__module__.startswith("pymysql"):
        return bool(exc.args) and exc.args[0] in (1205, 1213)
    return type(exc).__name__ == "OperationalError" and any(w in str(exc) for w in ("locked", "busy"))


def _commit_assertions(con, attempts=3):
    """Flush + commit with retry on lock conflicts. Returns an error message,
    or None on success."""
    for attempt in range(attempts):
        try:
            _flush_assertions(con)
            con.commit()
            return None
        except Exception as exc:  # noqa: BLE001 — classified below
            con.rollback()
            if _is_lock_conflict(exc) and attempt + 1 < attempts:
                time.sleep(0.05 * (attempt + 1))
                continue
            return f"could not record this response's claim assertions: {exc}"


# ---------------------------------------------------------------------------
# Phenotypes: canonical KPN trait IDs (2026-11-09, Chase). The KPN trait
# registry (scripts/load_kpn_traits.py, from broadinstitute/kpn-data-models)
# maps legacy portal / PIGEAN keys (IBD) to KPN.TRAIT IDs (KPN.TRAIT:0000505)
# and their ontology mappings. Every phenotype argument accepts any of those
# keys; queries match index rows under all of a trait's keys, so rows loaded
# before the registry still resolve.


def _trait(con, key):
    cache = getattr(con, "trait_cache", None)
    if cache is None:
        cache = con.trait_cache = {}
    if key not in cache:
        rec = None
        for col in ("trait_id", "pigean_id", "legacy_id"):
            if col == "trait_id" and not key.isascii():
                continue  # trait_id is an ascii column; MySQL would refuse the comparison
            rec = con.execute(f"SELECT * FROM kpn_traits WHERE {col} = ? LIMIT 1", (key,)).fetchone()
            if rec is not None:
                break
        cache[key] = rec
    return cache[key]


_NO_PHENOTYPE = "?none"  # an ASCII key no row carries, so IN () never occurs


def _storable_key(k) -> bool:
    """Index phenotype columns are VARCHAR(64) ascii on MySQL; other keys can
    never match a row (and would raise a collation error there)."""
    return bool(k) and k.isascii() and len(k) <= 64


def _phenotype_keys(con, key):
    """Every key index rows may carry for this phenotype, canonical first.
    On MySQL only keys its ascii VARCHAR(64) columns can hold; SQLite stores
    any key (as the loader's keys_of does)."""
    rec = _trait(con, key)
    keys = [rec["trait_id"], rec["pigean_id"], rec["legacy_id"], key] if rec is not None else [key]
    storable = (lambda k: bool(k)) if con.dialect.sqlite else _storable_key
    return list(dict.fromkeys(k for k in keys if storable(k))) or [_NO_PHENOTYPE]


def _in(keys):
    return "IN (" + ",".join("?" * len(keys)) + ")"


def _phenotype_view(con, key, trait_group=None):
    rec = _trait(con, key)
    if rec is None:
        return {"id": key, "legacyId": None, "name": None, "traitGroup": trait_group, "category": None,
                "traitType": None, "conceptId": None, "conceptLabel": None}
    return {
        "id": rec["trait_id"],
        "legacyId": rec["legacy_id"],
        "name": rec["name"],
        "traitGroup": trait_group,
        "category": rec["trait_group"],
        "traitType": rec["trait_type"],
        "conceptId": rec["primary_concept_id"],
        "conceptLabel": rec["primary_concept_label"],
    }


# ---------------------------------------------------------------------------
# GraphQL resolvers

query = QueryType()
json_scalar = ScalarType("JSON")


def _con(info):
    return info.context["con"]


@query.field("health")
def resolve_health(*_):
    return {"status": "ok", "contractVersion": CONTRACT_VERSION, "dapperProfile": DAPPER_PROFILE}


@query.field("claim")
def resolve_claim(_, info, id):
    return envelope(info.context["con"], id)


@query.field("dapperObject")
def resolve_dapper_object(_, info, id):
    payload = _fetch_payload(info.context["con"], id)
    return _present_object(payload) if payload else None


@query.field("identifierResolution")
def resolve_identifier_resolution(*_):
    return identifier_resolution()


@query.field("proposition")
def resolve_proposition(_, info, id):
    con = _con(info)
    payload = _fetch_payload(con, id)
    if payload is None or _class_of(payload) != "Proposition":
        return None
    # Every claim assessing it: deposited/minted ones from the registry, plus
    # the claims its index rows imply (served here, so asserted and minted).
    sources = {r["claim_id"]: None for r in con.execute(
        "SELECT claim_id FROM claims WHERE proposition_id = ?", (id,))}
    for family, table in CLAIM_SOURCES:
        for r in con.execute(
            f"SELECT * FROM {table} t WHERE proposition_id = ? AND synthesis_inputs IS NOT NULL{_live_roster(family)}",
            (id,),
        ).fetchall():
            sources[r["claim_id"]] = (family, r)
    for r in con.execute("SELECT * FROM result_claims WHERE proposition_id = ?", (id,)).fetchall():
        sources[r["claim_id"]] = ("collection", r)
    envelopes = [e for e in (envelope(con, cid, source=src) for cid, src in sorted(sources.items())) if e]
    return {
        "id": id,
        "proposition": payload,
        "triple": _triple_of(payload),
        "claims": envelopes,
    }


# ---------------------------------------------------------------------------
# Collections (docs/collections.md): discovery and the query plan executor.


def _collection_envelope(con, claim_id, source):
    return envelope(con, claim_id, source=source)


@query.field("collections")
def resolve_collections(_, info):
    return [{"id": c["id"], "title": c["title"], "description": c["description"], "methods": c["methods"],
             "currentRelease": c["current_release"], "version": c["version"], "datasets": c["datasets"]}
            for c in cserve.list_collections(_con(info))]


@query.field("collection")
def resolve_collection(_, info, id, release=None):
    try:
        return cserve.describe(_con(info), id, release)
    except cserve.CollectionNotFound:
        return None


@query.field("queries")
def resolve_queries(_, info, collection=None, release=None):
    try:
        return ccatalog.catalog(_con(info), collection, release)
    except cserve.CollectionNotFound as exc:
        raise ValueError(str(exc)) from exc


@query.field("collectionQueries")
def resolve_collection_queries(_, info, collection, release=None):
    try:
        return ccatalog.catalog(_con(info), collection, release)["queries"]
    except cserve.CollectionNotFound as exc:
        raise ValueError(str(exc)) from exc


@query.field("dimensionValues")
def resolve_dimension_values(_, info, collection, dimension, search=None, first=None, release=None):
    try:
        return ccatalog.dimension_values(_con(info), collection, dimension, release=release, search=search,
                                         first=first)
    except cserve.CollectionNotFound as exc:
        raise ValueError(str(exc)) from exc


@query.field("runQuery")
def resolve_run_query(_, info, collection, query, variables=None, release=None, claims=None):
    try:
        return cserve.run_query(_con(info), collection, query, variables, release=release, claims=claims,
                                envelope=_collection_envelope)
    except ManifestError as exc:
        raise ValueError(str(exc)) from exc


schema = make_executable_schema(
    _load_type_defs(), query, json_scalar
)

# ---------------------------------------------------------------------------
# HTTP app

# Auto-docs are off: the hand-written api/openapi.yaml is the contract
# authority, served at /v1/openapi.yaml with Swagger UI at /v1/docs.
app = FastAPI(
    title="Claims Repository",
    version=CONTRACT_VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Reads are open (QA decision) and browser UIs (the CFDE challenge UI, the
# Swagger page across localhost/127.0.0.1) call cross-origin; no credentials
# are used, so a permissive CORS policy discloses nothing new.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
# Process-local request telemetry for the admin console (service/telemetry.py).
app.add_middleware(telemetry.RequestTelemetry)

SWAGGER_HTML = """<!doctype html>
<html>
<head>
  <title>Claims Repository API</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5.17.14/swagger-ui.css">
</head>
<body>
<div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5.17.14/swagger-ui-bundle.js"></script>
<script>
  // Relative URL so the page works both locally (/v1/docs) and mounted under
  // the platform prefix (/api/claims/v1/docs).
  SwaggerUIBundle({
    url: "openapi.yaml",
    dom_id: "#swagger-ui",
    presets: [SwaggerUIBundle.presets.apis],
    deepLinking: true,
    defaultModelsExpandDepth: 0,
  });
</script>
</body>
</html>"""

# GraphiQL opened on the example queries in api/graphql/examples (service/explorer.py).
EXPLORER_HTML = explorer.render("Claims Repository")


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
        phenotypes, seen = [], set()
        for r in con.execute(
            "SELECT DISTINCT phenotype, trait_group FROM pigean_gene_associations ORDER BY phenotype"
        ).fetchall():
            view = _phenotype_view(con, r["phenotype"], r["trait_group"])
            if view["id"] not in seen:
                seen.add(view["id"])
                phenotypes.append({"id": view["id"], "legacy_id": view["legacyId"], "name": view["name"],
                                   "trait_group": r["trait_group"], "category": view["category"],
                                   "trait_type": view["traitType"], "concept_id": view["conceptId"]})
        release = con.execute("SELECT * FROM kpn_trait_release WHERE release_key = 'current'").fetchone()
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
        "identifier_resolution": identifier_resolution(),
        "categories": ["gene", "phenotype", "gene_set"],
        "predicates": [
            {"subject_category": "gene", "predicate": "associated_with", "object_category": "phenotype", "methods": ["pigean"]},
            {"subject_category": "gene_set", "predicate": "associated_with", "object_category": "phenotype", "methods": ["pigean"]},
            {"subject_category": "gene_set", "predicate": "has_members", "object_category": "gene_set_collection", "methods": ["pigean"]},
            {"subject_category": "gene", "predicate": "member_of", "object_category": "gene_set", "methods": ["deposit"]},
        ],
        "methods": ["pigean"],
        "metrics": {f"pigean.{name}": sem for name, sem in PIGEAN_METRICS.items()},
        "phenotypes": phenotypes,
        "trait_registry": (
            {"source": "broadinstitute/kpn-data-models", "version": release["version"],
             "published": not (release["source_url"] or "").startswith("unpublished:"),
             "commit": release["commit_sha"], "url": release["source_url"], "sha256": release["file_sha256"],
             "traits": release["trait_count"], "mappings": release["mapping_count"]}
            if release is not None else None
        ),
        "collections": collections,
    }


@app.get("/v1/collections")
def list_collections():
    con = db.connect()
    try:
        return {"collections": cserve.list_collections(con)}
    finally:
        con.close()


@app.get("/v1/collections/{collection}")
def describe_collection(collection: str, release: str | None = None):
    con = db.connect()
    try:
        return cserve.describe(con, collection, release)
    except cserve.CollectionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    finally:
        con.close()


@app.get("/v1/queries")
def query_catalog(collection: str | None = None, release: str | None = None):
    """Every query the repository answers (each collection's query templates),
    described so that a client can run any of them with runQuery: params and
    the values they take, the rows returned, the claims served, and a request
    that runs as given."""
    con = db.connect()
    try:
        return ccatalog.catalog(con, collection, release)
    except cserve.CollectionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    finally:
        con.close()


@app.get("/v1/collections/{collection}/dimensions/{dimension}")
def collection_dimension(collection: str, dimension: str, search: str | None = None,
                         first: int | None = Query(None, ge=0), release: str | None = None):
    """The values a dataset dimension (e.g. trait, library) takes, with labels,
    the other keys a query accepts for each, and dataset counts."""
    con = db.connect()
    try:
        return ccatalog.dimension_values(con, collection, dimension, release=release, search=search, first=first)
    except cserve.CollectionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    finally:
        con.close()


@app.get("/v1/collections/{collection}/queries")
def collection_queries(collection: str, release: str | None = None):
    con = db.connect()
    try:
        return {"collection": collection, "queries": ccatalog.catalog(con, collection, release)["queries"]}
    except cserve.CollectionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    finally:
        con.close()


@app.get("/v1/collections/{collection}/releases/{version}/document")
def collection_release_document(collection: str, version: str):
    """The release as a DAPPER document (ResultCollection, Datasets, Files,
    Activities), lintable with dapper's `result-collection` profile."""
    con = db.connect()
    try:
        return cserve.release_document(con, collection, version)
    except cserve.CollectionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    finally:
        con.close()


def _run_collection_query(collection, name, body):
    """Same contract as a GraphQL request: resolve, render, and write the
    recorded claim assertions only if the whole response succeeded."""
    con = db.connect()
    con.query_limit = MAX_QUERIES_PER_REQUEST
    try:
        try:
            result = cserve.run_query(con, collection, name, body.get("variables"), release=body.get("release"),
                                      claims=body.get("claims"), envelope=_collection_envelope)
            rendered = _render(result)
        except cserve.CollectionNotFound as exc:
            con.rollback()
            return 404, {"detail": str(exc)}
        except (ManifestError, ValueError, ClaimSynthesisError) as exc:
            con.rollback()
            return 400, {"detail": str(exc)}
        con.query_limit = None
        error = _commit_assertions(con)
        if error:
            return 409, {"detail": error}
        return 200, rendered
    finally:
        con.close()


@app.post("/v1/collections/{collection}/queries/{name}:run")
async def run_collection_query(collection: str, name: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be JSON.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    status, payload = await run_in_threadpool(_run_collection_query, collection, name, body)
    if status == 200:
        return Response(payload, media_type="application/json")
    return JSONResponse(payload, status_code=status)


@app.get("/v1/docs", response_class=HTMLResponse)
def swagger_docs():
    """Swagger UI over the hand-written REST contract (api/openapi.yaml) —
    every endpoint including GET /v1/objects/{id}, the dapper-object lookup."""
    return SWAGGER_HTML


@app.get("/v1/openapi.yaml", include_in_schema=False)
def openapi_contract(request: Request):
    """The hand-written contract, with THIS deployment injected as the first
    server entry — Swagger UI's try-it-out defaults to the first server, so
    requests stay same-origin whether the page is opened via localhost,
    127.0.0.1, or the QA/prod prefix. Deployed, CLAIMS_PUBLIC_BASE_URL wins:
    TLS ends at the load balancer, so the request itself reads as http and
    an https docs page would block try-it-out as mixed content."""
    text = (ROOT / "api" / "openapi.yaml").read_text()
    root_path = request.scope.get("root_path", "")
    base = PUBLIC_BASE or f"{request.url.scheme}://{request.url.netloc}{root_path}"
    injected = (
        "servers:\n"
        f"  - url: {base}\n"
        "    description: This deployment (same origin as this docs page).\n"
    )
    return Response(text.replace("servers:\n", injected, 1), media_type="application/yaml")


@app.get("/v1/graphql", response_class=HTMLResponse)
def graphql_explorer():
    return EXPLORER_HTML


GRAPHQL_DEBUG = os.environ.get("CLAIMS_DEBUG", "") == "1"
MAX_QUERIES_PER_REQUEST = 25_000


def _render(result) -> bytes:
    """The response bytes — exactly how JSONResponse renders (UTF-8, no
    NaN). Rendered BEFORE assertions are written, so a response that cannot
    be sent (non-finite number, lone surrogate) asserts nothing."""
    return json.dumps(result, ensure_ascii=False, allow_nan=False, indent=None,
                      separators=(",", ":")).encode("utf-8")


def _execute_graphql(data):
    """One GraphQL request: resolve (reads only; assertions are recorded),
    render, then — only if the WHOLE response succeeded — write the recorded
    mints and assertion counts in one short sorted batch. A failure anywhere
    writes nothing. Returns (success, rendered body)."""
    con = db.connect()
    con.query_limit = MAX_QUERIES_PER_REQUEST
    minted = reasserted = 0
    try:
        success, result = graphql_sync(schema, data, context_value={"con": con}, debug=GRAPHQL_DEBUG)
        body = None
        if success and not result.get("errors"):
            try:
                body = _render(result)
            except ValueError as exc:  # includes UnicodeEncodeError
                success, result = False, {"data": None, "errors": [{"message": f"unrenderable response: {exc}"}]}
        if success and not result.get("errors"):
            con.query_limit = None  # the write batch is not request work
            recorded = getattr(con, "assertions", None) or {}
            minted = sum(1 for a in recorded.values() if a["new"])
            reasserted = len(recorded) - minted
            error = _commit_assertions(con)
            if error:
                success, result = False, {"data": None, "errors": [{"message": error}]}
        else:
            # A failed response writes nothing — so it reports no data
            # either (partial data would show mints that were rolled back).
            con.rollback()
            success, result = False, {"data": None, "errors": result.get("errors") or []}
        if not success:
            body = _render(_renderable_errors(result))
    finally:
        con.close()
    telemetry.record_graphql(success, minted, reasserted, _root_fields(data))
    return success, body


def _root_fields(data) -> list[str]:
    """Top-level field names a GraphQL request asked for (telemetry only;
    an unparseable request counts as '(invalid)')."""
    try:
        from graphql import parse
        doc = parse(data.get("query") or "") if isinstance(data, dict) else None
        names = [sel.name.value for d in doc.definitions if getattr(d, "operation", None) is not None
                 for sel in d.selection_set.selections if getattr(sel, "name", None) is not None]
        return names or ["(empty)"]
    except Exception:  # noqa: BLE001 — telemetry must never fail a request
        return ["(invalid)"]


def _renderable_errors(result):
    """An error response that always renders (error text can carry the very
    value that failed to render)."""
    try:
        _render(result)
        return result
    except ValueError:
        return {"data": None, "errors": [{"message": json.dumps(e.get("message", ""), ensure_ascii=True)}
                                          for e in result.get("errors") or []]}


@app.post("/v1/graphql")
async def graphql_endpoint(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be JSON.")
    # graphql_sync blocks on the database; keep it off the event loop.
    success, body = await run_in_threadpool(_execute_graphql, data)
    return Response(body, status_code=200 if success else 400, media_type="application/json")


def _hint_str(hint, key, required=False):
    value = hint.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value):
        raise HTTPException(status_code=400, detail=f"index hint {key} must be a{' non-empty' if required else ''} string")
    return value


def _index_hint(con, hint, bundle_objects):
    """Index an externally deposited claim's graph edge. Hint rows carry no
    synthesis_inputs (their claims were deposited, not template-built) and may
    never overwrite a loaded, template-backed row."""
    if not isinstance(hint, dict):
        raise HTTPException(status_code=400, detail="each index hint must be an object")
    claim_id = _hint_str(hint, "claim_id", required=True)
    claim = bundle_objects.get(claim_id) or _fetch_payload(con, claim_id)
    if claim is None:
        raise HTTPException(status_code=400, detail=f"index hint references unknown claim {claim_id}")
    if _class_of(claim) != "Claim":
        raise HTTPException(status_code=400, detail=f"index hint claim_id {claim_id} is not a Claim")
    scores = {}
    for sid in claim.get("has_score", []):
        s = bundle_objects.get(sid) or _fetch_payload(con, sid)
        if s is None:
            continue
        if not _well_formed_score(s):
            raise HTTPException(
                status_code=400,
                detail=f"{sid} (cited by indexed claim {claim_id}) is not a well-formed ClaimScore "
                       "(score_kind a ScoreKind value, string metric/interpretation, and a finite "
                       "numeric value; an integer value within +/-(2^53-1))",
            )
        scores[s["metric"]] = s["value"]
    subject, obj = hint.get("subject") or {}, hint.get("object") or {}
    if not isinstance(subject, dict) or not isinstance(obj, dict):
        raise HTTPException(status_code=400, detail="index hint subject/object must be objects")
    for side in (subject, obj):
        _hint_str(side, "id", required=True)
        _hint_str(side, "category")
        dapper_id = _hint_str(side, "dapper_id")
        if dapper_id is not None and (not DAPPER_ID_RE.match(dapper_id) or len(dapper_id) > MAX_ID_LENGTH):
            raise HTTPException(status_code=400, detail=f"index hint dapper_id {dapper_id[:140]!r} is not a dapper identifier")
    collection = _hint_str(hint, "collection") or DEFAULT_COLLECTION
    trait_group = _hint_str(hint, "trait_group")
    predicate = _hint_str(hint, "predicate", required=True)
    method = _hint_str(hint, "method") or ""
    source = _hint_str(hint, "source")
    proposition_id = claim.get("proposition")
    upsert = con.dialect.insert_or_replace

    def refuse_if_loaded(table, key_col, key, phenotype_key):
        """Under ANY key of the phenotype: a loaded result stored under a
        legacy key must not be shadowed by a deposit under the canonical one."""
        keys = _phenotype_keys(con, phenotype_key)
        row = con.execute(
            f"SELECT 1 FROM {table} WHERE phenotype {_in(keys)} AND collection = ? AND {key_col} = ? "
            "AND synthesis_inputs IS NOT NULL LIMIT 1",
            (*keys, collection, key),
        ).fetchone()
        if row is not None:
            raise HTTPException(
                status_code=400,
                detail=f"index hint would overwrite a loaded, template-backed {table} row; deposits cannot replace loaded results",
            )

    con.execute(
        f"{upsert} claims (claim_id, proposition_id, method) VALUES (?, ?, ?)",
        (claim_id, proposition_id or "", method),
    )
    if predicate == "associated_with" and subject.get("category") == "gene":
        missing = [m for m in ("prior", "combined", "log_bf") if m not in scores]
        if missing:
            raise HTTPException(status_code=400, detail=f"claim {claim_id} missing scores {missing} for gene association index")
        phenotype = _canonical_phenotype(con, obj["id"])
        refuse_if_loaded("pigean_gene_associations", "gene", subject["id"], obj["id"])
        con.execute(
            f"{upsert} pigean_gene_associations "
            "(phenotype, collection, gene, prior, combined, log_bf, n, trait_group, claim_id, proposition_id, synthesis_inputs) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL)",
            (phenotype, collection, subject["id"], scores["prior"], scores["combined"],
             scores["log_bf"], trait_group, claim_id, proposition_id),
        )
    elif predicate == "associated_with" and subject.get("category") == "gene_set":
        missing = [m for m in ("beta", "beta_uncorrected") if m not in scores]
        if missing:
            raise HTTPException(status_code=400, detail=f"claim {claim_id} missing scores {missing} for gene-set association index")
        phenotype = _canonical_phenotype(con, obj["id"])
        refuse_if_loaded("pigean_gene_set_associations", "gene_set", subject["id"], obj["id"])
        con.execute(
            f"{upsert} pigean_gene_set_associations "
            "(phenotype, collection, gene_set, source, beta, beta_uncorrected, rs_score, n, trait_group, claim_id, proposition_id, synthesis_inputs) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, NULL)",
            (phenotype, collection, subject["id"], source, scores["beta"],
             scores["beta_uncorrected"], trait_group, claim_id, proposition_id),
        )
        _hint_entity(con, collection, subject)
    elif predicate == "member_of":
        raw_phenotype = _hint_str(hint, "phenotype") or ""
        phenotype = _canonical_phenotype(con, raw_phenotype) if raw_phenotype else ""
        if con.execute("SELECT 1 FROM pigean_gene_set_rosters WHERE claim_id = ?", (claim_id,)).fetchone():
            raise HTTPException(
                status_code=400,
                detail="index hint cites a loaded roster claim; roster edges are the loader's, not deposits'",
            )
        keys = _phenotype_keys(con, raw_phenotype) if raw_phenotype else [""]
        loaded = con.execute(
            f"SELECT 1 FROM claim_memberships m JOIN pigean_gene_set_rosters r ON r.claim_id = m.claim_id "
            f"WHERE m.phenotype {_in(keys)} AND m.collection = ? AND m.gene = ? AND m.gene_set = ? LIMIT 1",
            (*keys, collection, subject["id"], obj["id"]),
        ).fetchone()
        if loaded is not None:
            raise HTTPException(
                status_code=400,
                detail="index hint would overwrite a loaded membership edge; deposits cannot replace loaded results",
            )
        con.execute(
            f"{upsert} claim_memberships (phenotype, collection, gene, gene_set, claim_id) VALUES (?, ?, ?, ?, ?)",
            (phenotype, collection, subject["id"], obj["id"], claim_id),
        )
        _hint_entity(con, collection, obj)
    else:
        raise HTTPException(status_code=400, detail=f"unsupported index hint predicate {predicate!r}")


def _canonical_phenotype(con, key):
    """A deposited phenotype key, normalized to its KPN.TRAIT ID when registered."""
    rec = _trait(con, key)
    return rec["trait_id"] if rec is not None else key


def _hint_entity(con, collection, ref):
    """Record a hinted gene-set entity only where none is known: a deposit
    hint must never clobber a loaded entity (or its n_genes)."""
    if ref.get("dapper_id"):
        con.execute(
            f"{con.dialect.insert_or_ignore} gene_set_entities (collection, name, dapper_id, n_genes) VALUES (?, ?, ?, NULL)",
            (collection, ref.get("id"), ref.get("dapper_id")),
        )


_CLASS_RE = re.compile(r"^[A-Za-z]+\Z")


def _reject_constant(name):
    raise HTTPException(status_code=400, detail=f"numbers must be finite ({name} is not valid JSON)")


def _check_interoperable(body) -> None:
    """Refuse (400) a deposit body that is not interoperable JSON (I-JSON,
    RFC 7493): nesting deeper than MAX_JSON_DEPTH, a non-finite number, an
    integer beyond +/-(2^53-1), or a string (or key) that is not valid
    Unicode (a lone surrogate cannot be stored or rendered as UTF-8).
    Iterative, so no input can exhaust the stack here; the depth bound keeps
    every later recursive walk (comparison, refs, canonical JSON) safe."""
    stack = [(body, 0)]
    while stack:
        value, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise HTTPException(status_code=400, detail=f"document nested deeper than {MAX_JSON_DEPTH} levels")
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            if not _finite_number(value):
                raise HTTPException(status_code=400,
                                    detail="numbers must be finite, and integer literals within +/-(2^53-1) (I-JSON)")
        elif isinstance(value, str):
            _check_unicode(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                _check_unicode(key)
                stack.append((item, depth + 1))
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)


def _check_unicode(text: str) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise HTTPException(status_code=400, detail="strings must be valid Unicode (no lone surrogates; I-JSON)")


def _same_document(a, b) -> bool:
    """Document identity: numbers compare by value (2 == 2.0, which JSON
    round-trips may reformat), but booleans never equal numbers."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same_document(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same_document(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


MAX_DEPOSIT_OBJECTS = 1000
MAX_DEPOSIT_BYTES = 16 * 1024 * 1024
MAX_DEPOSIT_EDGES = 5000
_UNAUTHORIZED = {"WWW-Authenticate": "ApiKey"}


async def _deposit_body(request: Request) -> dict:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_DEPOSIT_BYTES:
        raise HTTPException(status_code=413, detail=f"A deposit is at most {MAX_DEPOSIT_BYTES:,} bytes; send a larger document in parts.")
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_DEPOSIT_BYTES:
            raise HTTPException(status_code=413, detail=f"A deposit is at most {MAX_DEPOSIT_BYTES:,} bytes; send a larger document in parts.")
    try:
        body = json.loads(bytes(raw), parse_constant=_reject_constant)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be JSON.")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    _check_interoperable(body)
    hints = body.get("index_hints") or []
    if not isinstance(hints, list):
        raise HTTPException(status_code=400, detail="index_hints must be a list")
    return body


def _write_deposit(depositor, prepared, hints, edges=()):
    """The write behind POST /v1/deposits. `prepared` is what validation accepted: for each object its id, class and
    the plain DAPPER document to store, every id verified as the DAPPER-ID-1 address of its content. Everything a
    deposit must do happens here, in one transaction: the reserved address rule, idempotent storage, the reference
    index, the claims registry, index hints, the key re-check and the deposit record. A bundle is accepted or
    rejected whole."""
    con = db.connect()
    try:
        deposited = []
        bundle_objects = {}
        with con:
            for item in prepared:
                oid, cls, obj = item["id"], item["class_name"], item["document"]
                bundle_objects[oid] = obj
                if cls == "Claim":
                    source = _index_source(con, oid)
                    if source is not None:
                        try:
                            if source[0] == "collection":
                                expected = cserve.render_claim(con, source[1])
                            else:
                                expected = synthesize_claim(*source)
                        except (ClaimSynthesisError, cserve.ClaimRenderError, cserve.engine.TemplateError, cidn.MintError) as exc:
                            raise HTTPException(status_code=400, detail=str(exc))
                        if not _same_document(obj, expected):
                            raise HTTPException(
                                status_code=400,
                                detail=(
                                    f"{oid} is reserved: the loaded data implies this claim, and the "
                                    "deposited document differs from it"
                                ),
                            )
                exists = con.execute("SELECT payload FROM dapper_objects WHERE id = ?", (oid,)).fetchone()
                differs = bool(exists) and _stored_differs(oid, json.loads(exists["payload"]), obj)
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
                    # Upsert, not insert: a concurrent mint/deposit of the same
                    # document is counted, not an error. Canonical storage
                    # (sorted keys) so every object reads back consistently.
                    con.execute(
                        con.dialect.upsert_object,
                        (oid, cls, cidn.PROFILE,
                         json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)),
                    )
                    stored = con.execute("SELECT payload, times_seen FROM dapper_objects WHERE id = ?", (oid,)).fetchone()
                    differs = _stored_differs(oid, json.loads(stored["payload"]), obj)
                    exists = exists or differs or stored["times_seen"] > 1  # a concurrent deposit stored it first
                    times = stored["times_seen"]
                if not differs:
                    refs = sorted(dapper_refs(obj))
                    if refs:
                        con.executemany(con.dialect.insert_ref, [(oid, ref) for ref in refs])
                if cls == "Claim":
                    # Register every deposited Claim with its proposition so
                    # the GraphQL fan-in (Query.proposition) sees claims that
                    # carry no graph-edge index hint, e.g. interpretive tier
                    # claims. A later index_hint overwrites this row with the
                    # claim's method. (External deposits are asserted by their
                    # depositor, so they land immediately.)
                    con.execute(
                        f"{con.dialect.insert_or_ignore} claims (claim_id, proposition_id, method) VALUES (?, ?, ?)",
                        (oid, obj.get("proposition", ""), ""),
                    )
                outcome = {
                    "id": oid,
                    "class_name": cls,
                    "status": "exists" if exists else "created",
                    "times_deposited": times,
                }
                if differs:
                    outcome["stored_differs"] = True
                deposited.append(outcome)
            for hint in hints:
                _index_hint(con, hint, bundle_objects)
            # Last, after this transaction's own writes and as a locking read: a key revoked before this point
            # deposits nothing, and a revocation that arrives now waits for this deposit to finish.
            if not api_keys.still_valid(con, depositor):
                raise HTTPException(status_code=401, detail=api_keys.UNAUTHORIZED, headers=_UNAUTHORIZED)
            # Same transaction as the objects: a rejected bundle records no deposit.
            deposit_id = api_keys.record_deposit(con, depositor, deposited, edges)
            unresolved = _unresolved_references(con, bundle_objects, edges)
        return {"deposit_id": deposit_id, "depositor": depositor, "deposited": deposited}, unresolved
    except HTTPException:
        raise
    except (TypeError, ValueError, KeyError, IndexError, OverflowError) as exc:  # sqlite3.Row raises IndexError
        raise HTTPException(status_code=400, detail=f"malformed deposit: {type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 — driver binding errors from malformed values
        if type(exc).__name__ in ("ProgrammingError", "InterfaceError", "DataError", "IntegrityError"):
            raise HTTPException(status_code=400, detail=f"malformed deposit: {type(exc).__name__}: {exc}")
        if type(exc).__name__ == "OperationalError":
            # A lock wait (a concurrent deposit or revocation) or a dropped connection: nothing was stored.
            raise HTTPException(status_code=503, detail="The store is busy; nothing was deposited. Retry the same request.",
                                headers={"Retry-After": "2"})
        raise
    finally:
        con.close()


def _plain(document: dict) -> dict:
    """A stored document as a plain DAPPER node: without the legacy envelope keys and null slots."""
    return {k: v for k, v in document.items() if k not in ("class_name", "identity_profile") and v is not None}


def _stored_differs(oid, stored, obj) -> bool:
    """Whether the document already at this address differs from the one being deposited, when that is allowed.

    The deposited id is the address of the document's hashed content. If the stored document also hashes to this
    address and agrees literally in every hashed slot, the two can differ only in slots identity ignores (a File's
    location, an Activity's timestamp): the first one stands and this deposit is `exists` with `stored_differs`.
    A stored document that does NOT hash to this address (written out of band, or by an earlier version of the
    service) is never taken for the object, and nothing stored is ever rewritten, so the deposit is refused."""
    if _same_document(stored, obj):
        return False
    plain = _plain(stored)
    if _same_document(plain, obj):
        return False  # the same node, stored earlier with the legacy class_name envelope
    try:
        stored_verifies = cidn.verify(plain)
    except Exception:  # noqa: BLE001 — a stored document dapper's identity code cannot canonicalize does not verify
        stored_verifies = False
    if not stored_verifies:
        raise HTTPException(
            status_code=400,
            detail=(f"{oid} is held by a stored document that does not hash to this address, so it cannot be "
                    "confirmed as the same object; an operator must resolve it before this object can be deposited"),
        )
    # Both hash to this address. dapper's identity replaces a reference by its bare digest before hashing, so two
    # spellings of one reference hash alike; only literal equality of every hashed slot shows the documents differ
    # in nothing but the slots identity ignores.
    hashed = dval.hashable_slots(cidn.class_of(oid))
    unequal = sorted(k for k in hashed if not _same_document(plain.get(k), obj.get(k)))
    if unequal:
        raise HTTPException(
            status_code=400,
            detail=(f"{oid} is already stored with different content in {', '.join(unequal[:5])}, although both "
                    "documents hash to this address (a reference is written two ways); nothing stored is rewritten"),
        )
    return True


def _unresolved_references(con, bundle_objects, edges) -> list[str]:
    """dapper ids the bundle points at that are neither in it nor in the store. Reported, not refused: Files and
    GeneSets may be described elsewhere, and a large document may arrive in several deposits."""
    wanted = set()
    for obj in bundle_objects.values():
        wanted |= dapper_refs(obj)
    for _, edge in edges:
        wanted |= dapper_refs(edge)
    wanted = sorted(ref for ref in wanted - set(bundle_objects) if len(ref) <= MAX_ID_LENGTH)
    found = set()
    for i in range(0, len(wanted), 500):
        chunk = wanted[i:i + 500]
        rows = con.execute(f"SELECT id FROM dapper_objects WHERE id IN ({', '.join('?' * len(chunk))})", chunk).fetchall()
        found |= {row["id"] for row in rows}
    return [ref for ref in wanted if ref not in found]


@app.post("/v1/deposits")
async def deposit(request: Request, depositor: dict = Depends(api_keys.require_depositor)):
    """The only external write path: deposit DAPPER objects of any class, validated. The depositor's X-API-Key is checked before
    the body is read. The body is either a DAPPER document (`document`: nodes and edges under dapper's group names,
    which is what dapper's tooling mints, e.g. a whole ScientificAccount) or a flat list (`objects`). Every node must
    be valid for its class in the vendored DAPPER schema and sit at the DAPPER-ID-1 address of its content
    (service/dapper_validation.py). The bundle is stored whole or not at all and recorded as one deposit by the key."""
    body = await _deposit_body(request)
    if ("document" in body) == ("objects" in body):
        raise HTTPException(status_code=400, detail="send exactly one of document (a DAPPER document) or objects (a list of DAPPER nodes)")

    def validate():
        try:
            if "document" in body:
                nodes, edges = dval.document_items(body["document"])
            else:
                if not isinstance(body["objects"], list):
                    raise dval.Invalid("objects must be a list")
                nodes, edges = [(None, node) for node in body["objects"]], []
            if not nodes:
                raise dval.Invalid("a deposit needs at least one object")
            if len(nodes) > MAX_DEPOSIT_OBJECTS or len(edges) > MAX_DEPOSIT_EDGES:
                raise dval.Invalid(f"a deposit holds at most {MAX_DEPOSIT_OBJECTS} objects and {MAX_DEPOSIT_EDGES} edges; send a larger document in parts")
            prepared = []
            for expected, node in nodes:
                oid, cls, document = dval.validate_node(node, expected)
                prepared.append({"id": oid, "class_name": cls, "document": document})
            return prepared, [(cls, dval.validate_edge(cls, edge)) for cls, edge in edges]
        except dval.Invalid as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    def run():
        prepared, edges = validate()
        result, unresolved = _write_deposit(depositor, prepared, body.get("index_hints") or [], edges)
        return {**result, "edges": len(edges), "unresolved_references": unresolved,
                "validation": {"identity_profile": cidn.PROFILE, "dapper_version": cidn.DAPPER_VERSION}}

    return await run_in_threadpool(run)


_DAPPER_ID_RE = re.compile(r"dapper:[A-Za-z]+\.[A-Za-z0-9_-]+")


def _href(path: str) -> str:
    """An attribute-safe URL: dapper IDs are percent-encoded into the path
    (':' kept readable) and the whole value is HTML-attribute escaped."""
    return html.escape(path, quote=True)


def _resolver_href(object_id: str) -> str:
    return _href(_resolver_url(quote(object_id, safe=":")))


def _object_landing_html(object_id: str, payload: dict) -> str:
    """Human landing page for a shared dapper ID: the document, with every
    referenced dapper ID linked through this same resolver, plus the reverse
    direction (objects that reference this one). Every interpolated value is
    escaped."""
    doc = json.dumps(payload, indent=2, sort_keys=True)
    refs = sorted({m for m in _DAPPER_ID_RE.findall(doc) if m != object_id})
    doc_html = html.escape(doc)
    for ref in refs:
        doc_html = doc_html.replace(ref, f'<a href="{_resolver_href(ref)}">{html.escape(ref)}</a>')
    ref_list = "".join(
        f'<li><a href="{_resolver_href(r)}">{html.escape(r)}</a></li>' for r in refs
    ) or "<li>none</li>"
    qid = quote(object_id, safe=":")
    referenced_by = _href(f"{PUBLIC_BASE}/v1/objects/search?references={qid}")
    claim_hint = ""
    if _class_of(payload) == "Proposition":
        claims_href = _href(f"{PUBLIC_BASE}/v1/objects/search?class_name=Claim&references={qid}")
        claim_hint = f'<p><a href="{claims_href}">Claims assessing this proposition</a></p>'
    elif _class_of(payload) == "Claim":
        claim_hint = (
            "<p>Dereferenced envelope (scores, evidence + locators, provenance): GraphQL "
            f'<code>claim(id: "{html.escape(object_id)}")</code></p>'
        )
    base = html.escape(PUBLIC_BASE or "")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(object_id)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font-family:system-ui,sans-serif;margin:2rem auto;max-width:60rem;padding:0 1rem;color:#222}}
pre{{background:#f6f8fa;padding:1rem;overflow-x:auto;border-radius:6px}}
code{{background:#f6f8fa;padding:0.1rem 0.3rem;border-radius:4px}}</style></head>
<body>
<h1>{html.escape(str(_class_of(payload) or "DapperObject"))}</h1>
<p><code>{html.escape(object_id)}</code> — a content-addressed DAPPER identifier; this page is its
resolver view (<code>GET {base}/v1/objects/{{id}}</code>, JSON by default).</p>
{claim_hint}
<p><a href="{referenced_by}">Objects that reference this one</a> ·
API: <a href="{_href(PUBLIC_BASE + "/v1/docs")}">REST docs</a> · <a href="{_href(PUBLIC_BASE + "/v1/graphql")}">GraphQL</a></p>
<h2>Referenced dapper objects</h2><ul>{ref_list}</ul>
<h2>Document</h2><pre>{doc_html}</pre>
</body></html>"""


# ---------------------------------------------------------------------------
# REST output: OBJECTS ONLY (2026-11-09, Chase). One resolver plus a general
# list and search over every dapper object, not tied to claims or
# propositions. Metric questions are GraphQL's (docs/inputs-and-outputs.md);
# the dereferenced claim envelope and proposition fan-in live there too.

_SLOT_RE = re.compile(r"^[a-z_][a-z0-9_]*\Z")
_TEXT_SLOTS = ("statement", "name", "description")


def _label(payload: dict) -> str | None:
    """One human-readable line for any object class."""
    for slot in _TEXT_SLOTS:
        if payload.get(slot):
            return payload[slot]
    cls = _class_of(payload)
    if cls == "ClaimScore":
        return f"{payload.get('metric')} = {payload.get('value')}"
    if cls == "ValueLocator":
        return f"{payload.get('selector')} -> {payload.get('value_text')!r}"
    if cls == "File":
        return payload.get("source_uri")
    return None


MAX_OFFSET = 10_000_000


def _object_search(con, *, class_name=None, q=None, references=None, match=None,
                   deposited_by=None, deposit=None, limit=50, offset=0):
    """Shared engine for GET /v1/objects and /v1/objects/search: filters
    ANDed, deterministic id order, total computed over the same filters.
    Text matching is case-insensitive and literal (wildcards escaped) on both
    backends."""
    limit = max(1, min(int(limit), 500))
    offset = max(0, min(int(offset), MAX_OFFSET))
    if class_name is not None and not _CLASS_RE.match(class_name):
        raise HTTPException(status_code=400, detail=f"class_name must be letters only, got {class_name!r}")
    if references is not None and (not DAPPER_ID_RE.match(references) or len(references) > MAX_ID_LENGTH):
        raise HTTPException(status_code=400, detail=f"references must be a dapper identifier, got {references[:140]!r}")
    if deposited_by is not None and not api_keys.KEY_ID_RE.match(deposited_by):
        raise HTTPException(status_code=400, detail="deposited_by must be a key id such as key_0123456789abcdef")
    if deposit is not None and not api_keys.DEPOSIT_ID_RE.match(deposit):
        raise HTTPException(status_code=400, detail="deposit must be a deposit id (dep_ followed by 32 hex characters)")
    je = con.dialect.je
    joins, where, params = "", [], []
    if references:
        joins = " JOIN object_refs r ON r.from_id = o.id AND r.to_id = ?"
        params.append(references)
    # Deposit provenance: objects one key sent, or one deposit carried. Minting on serve (GraphQL, collection
    # queries) and loaders record no deposit, so such an object matches only if someone also deposited it.
    if deposited_by:
        where.append("EXISTS (SELECT 1 FROM deposit_objects dx JOIN deposits dp ON dp.deposit_id = dx.deposit_id "
                     "WHERE dx.object_id = o.id AND dp.key_id = ?)")
        params.append(deposited_by)
    if deposit:
        where.append("EXISTS (SELECT 1 FROM deposit_objects dy WHERE dy.object_id = o.id AND dy.deposit_id = ?)")
        params.append(deposit)
    if class_name:
        where.append("o.class_name = ?")
        params.append(class_name)
    if q:
        where.append("(" + " OR ".join(
            f"({con.dialect.json_is_string('o.payload', s)} AND {con.dialect.like_ci(je('o.payload', s))})"
            for s in _TEXT_SLOTS) + ")")
        params.extend([f"%{db.like_escape(q)}%"] * len(_TEXT_SLOTS))
    for clause in match or []:
        slot, sep, value = clause.partition(":")
        if not sep or not _SLOT_RE.match(slot):
            raise HTTPException(
                status_code=400,
                detail=f"match must be 'slot:value' with a lowercase slot name, got {clause!r}",
            )
        where.append(f"({con.dialect.json_is_string('o.payload', slot)} AND {je('o.payload', slot)} = ?)")
        params.append(value)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    base = f"FROM dapper_objects o{joins}{where_sql}"
    total = con.execute(f"SELECT COUNT(*) AS c {base}", params).fetchone()["c"]
    rows = con.execute(
        f"SELECT o.id, o.class_name, o.payload, o.times_seen {base} ORDER BY o.id LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    objects = [
        {
            "id": r["id"],
            "class_name": r["class_name"],
            "label": _label(json.loads(r["payload"])),
            "times_seen": r["times_seen"],
            "resolver_url": _resolver_url(r["id"]),
        }
        for r in rows
    ]
    return {"total": total, "limit": limit, "offset": offset, "objects": objects}


@app.get("/v1/objects")
def list_objects(
    class_name: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
):
    """Browse every stored dapper object, optionally one class (Claim,
    Proposition, GeneSet, EvidenceItem, ...). Claims appear once asserted."""
    con = db.connect()
    try:
        return _object_search(con, class_name=class_name, limit=limit, offset=offset)
    finally:
        con.close()


@app.get("/v1/objects/search")
def search_objects(
    class_name: str | None = None,
    q: str | None = None,
    references: str | None = None,
    match: list[str] | None = Query(default=None),
    deposited_by: str | None = None,
    deposit: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
):
    """General search over every dapper object. `references` = objects whose
    document points at that dapper ID (claims of a proposition, evidence for
    a proposition, propositions about a GeneSet, ...); `q` substring-matches
    the statement / name / description text; `match=slot:value` (repeatable)
    is exact equality on a top-level string slot (e.g. relation:has_members,
    subject_entity:NOD2); `deposited_by=<key id>` = objects that depositor sent
    in a deposit, and `deposit=<deposit id>` = objects one deposit
    carried. No numeric filters: metric questions are GraphQL's."""
    con = db.connect()
    try:
        return _object_search(con, class_name=class_name, q=q, references=references, match=match,
                              deposited_by=deposited_by, deposit=deposit, limit=limit, offset=offset)
    finally:
        con.close()


@app.get("/v1/deposits")
def list_deposits(
    object: str | None = None,
    deposited_by: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
):
    """Who deposited what, newest first: every deposit, the deposits that
    carried one object (`object=<dapper id>`), or the deposits one depositor
    sent (`deposited_by=<key id>`). A depositor is a key id and the name it was
    issued under; the key itself is never served. Objects the service minted
    when serving a query, or a loader wrote, belong to no deposit."""
    if object is not None and (not DAPPER_ID_RE.match(object) or len(object) > MAX_ID_LENGTH):
        raise HTTPException(status_code=400, detail=f"object must be a dapper identifier, got {object[:140]!r}")
    if deposited_by is not None and not api_keys.KEY_ID_RE.match(deposited_by):
        raise HTTPException(status_code=400, detail="deposited_by must be a key id such as key_0123456789abcdef")
    con = db.connect()
    try:
        return api_keys.deposits(con, object_id=object, deposited_by=deposited_by, limit=limit, offset=offset)
    finally:
        con.close()


@app.get("/v1/deposits/{deposit_id}")
def get_deposit(deposit_id: str):
    """One deposit: its depositor, when it was received and every object it
    carried, with whether this deposit created the object or it already existed."""
    if not api_keys.DEPOSIT_ID_RE.match(deposit_id):
        raise HTTPException(status_code=404, detail="Unknown deposit.")
    con = db.connect()
    try:
        found = api_keys.deposit(con, deposit_id)
    finally:
        con.close()
    if found is None:
        raise HTTPException(status_code=404, detail="Unknown deposit.")
    return found


@app.get("/v1/objects/{object_id}")
def get_object(object_id: str, request: Request):
    """The resolver: dereference any dapper ID to its stored document.
    Content-negotiated: browsers (Accept: text/html) get a landing page with
    referenced IDs linked; everything else gets the JSON document."""
    con = db.connect()
    try:
        payload = _fetch_payload(con, object_id)
        unasserted = payload is None and _index_source(con, object_id) is not None
    finally:
        con.close()
    if payload is None:
        if unasserted:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"{object_id} is a claim this repository's data implies but has not yet "
                    "asserted — it is minted when the scientific query surface (GraphQL) first "
                    "serves the result behind it"
                ),
            )
        raise HTTPException(status_code=404, detail=f"no deposited object with id {object_id}")
    accept = request.headers.get("accept", "")
    if "text/html" in accept and accept.find("text/html") < (
        accept.find("application/json") if "application/json" in accept else len(accept)
    ):
        return HTMLResponse(_object_landing_html(object_id, payload))
    return _present_object(payload)


def _triple_of(prop) -> dict | None:
    """(subject, predicate, object) of a Proposition document — only when
    every part is a string (stored documents are not schema-validated, and
    ClaimTriple's String! fields must never fail a whole response)."""
    if not isinstance(prop, dict):
        return None
    parts = (prop.get("subject_entity", ""), prop.get("relation"), prop.get("object_entity", ""))
    if not parts[1] or not all(isinstance(p, str) for p in parts):
        return None
    return {"subject": parts[0], "predicate": parts[1], "object": parts[2]}


# Admin API (service/admin.py) for the admin console (/admin in frontend/): read-only
# telemetry on claims, templates, tables and API traffic, callable only with
# the console server's signed assertion. Imported last: it reads this
# module's helpers at request time.
from . import admin as _admin  # noqa: E402

app.include_router(_admin.router)
