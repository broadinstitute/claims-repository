"""The query catalog: everything a client (a person or an agent) needs to call
runQuery without reading anything else.

For every query template of every collection's current release it gives the
parameters (with the values a dimension parameter takes, and where to list
them all), the shape of the rows the query returns (kinds, entities, metrics
and their meaning, joins), the claim keys each row serves, and a request that
runs as given. Everything is derived from the stored release manifest and the
release's datasets, so it is always in step with what runQuery will do.
"""
from __future__ import annotations

from .serve import CollectionNotFound, _json, release_row

SAMPLE_VALUES = 10
MAX_VALUES = 5000

ROW_SHAPE = {
    "kind": "the row kind (see `returns.rows.kind`)",
    "dataset": "{id, dimensions: {name: value}}: the dataset the row comes from",
    "subject": "{id, value, label}: id is the canonical identifier, value as the result file gives it",
    "object": "{id, label}",
    "metrics": "{metric: number}: the row kind's metrics (see `returns.rows.metrics`)",
    "columns": "{column: value}: extra columns the collection keeps",
    "claims": "{claim key: claim envelope}: the claims the row stands for, limited by the `claims` argument",
}
JOIN_ITEM_SHAPE = {
    "edge": "{kind, dataset_id, from: {id, value}, to: {id, value}, claims: {claim key: claim envelope}}",
    "row": "the joined row, shaped like an anchor row",
}
ENVELOPE_SHAPE = {
    "id": "dapper:Claim id",
    "resolverUrl": "a URL that returns the claim document",
    "claim": "the DAPPER Claim document",
    "proposition": "the DAPPER Proposition document",
    "triple": "{subject, predicate, object}",
    "scores": "[{metric, value, scoreKind, interpretation}]",
    "evidence": "[{item, valueLocators}]: for measurements, the exact bytes of each number in its result file",
    "timesDeposited": "how many times the repository has asserted the claim",
    "firstDepositedAt": "UTC time of the first assertion", "lastDepositedAt": "UTC time of the latest assertion",
    "provenanceRefs": "{derivedFromFiles, geneSetRefs, resolveVia, resolution}: where the claim came from",
}


def guide() -> dict:
    """How to run any query in the catalog; the same for every collection."""
    return {
        "summary": (
            "Every query the repository answers is a collection's query template, run with runQuery. "
            "Pick a query below, fill its params (dimension params list their values; GET the `values` link "
            "for all of them), and send one of the two requests in its `run` block."),
        "graphql": {
            "endpoint": "POST /v1/graphql",
            "field": "runQuery(collection: String!, query: String!, variables: JSON, release: String, "
                     "claims: [String!]): JSON",
        },
        "rest": {"endpoint": "POST /v1/collections/{collection}/queries/{query}:run",
                 "body": {"variables": "{...}", "claims": "[...] (optional)", "release": "(optional)"}},
        "arguments": {
            "variables": "The query's params. Unknown names, wrong types and out-of-range values are refused "
                         "with a message that lists the params. Omitted params take their default.",
            "claims": "Which claim keys to serve with each row (see each query's `claims`). Omit for all keys; "
                      "[] serves none and returns rows only. Serving a claim asserts it: the repository counts "
                      "it, and mints it the first time.",
            "release": "A version or release id; omit for the current release.",
        },
        "result": {"collection": "the collection key", "release": "{id, version}", "query": "the query name",
                   "variables": "the params as bound, defaults filled",
                   "rows": "[row]", "warnings": "[string]: e.g. a join whose data is not loaded"},
        "row": ROW_SHAPE,
        "join_item": JOIN_ITEM_SHAPE,
        "claim_envelope": ENVELOPE_SHAPE,
        "dimension_values": "GET /v1/collections/{collection}/dimensions/{dimension}?search=&first= "
                            "(GraphQL: dimensionValues)",
    }


def _claim_info(claim: dict, relations: dict) -> dict:
    prop = claim.get("proposition") or {}
    rel = prop.get("relation")
    out = {"relation": relations.get(rel, rel)}
    if claim.get("classify"):
        out.update({"tier_rule": claim["classify"], "focus": claim.get("focus")})
        out["description"] = (f"Evidence tier: classifies {claim.get('focus')} under the {claim['classify']} "
                              "rule (see the collection's tier_rules).")
    elif claim.get("scores"):
        out["scores"] = claim["scores"]
        out["description"] = (f"Measurement: carries the scores {', '.join(claim['scores'])}"
                              + (", each with its evidence in the result file." if claim.get("evidence") else "."))
    else:
        out["description"] = "Asserts the proposition in statement_template, filled from the row or edge group."
    out["statement_template"] = " ".join((prop.get("statement") or "").split())
    return out


def _claims_by_kind(record: dict, relations: dict) -> dict:
    out: dict[str, dict] = {}
    for t in record["templates"]:
        doc = t["doc"]
        out.setdefault(doc["for"], {}).update({c["key"]: _claim_info(c, relations) for c in doc["claims"]})
    return out


def _kind_shape(manifest: dict, kind: str, claims: dict) -> dict:
    spec = manifest["row_kinds"][kind]
    entities = manifest.get("entities", {})

    def entity(side):
        e = spec.get(side) or {}
        name = e.get("entity")
        out = {"entity": name}
        if name in entities and entities[name].get("id"):
            out["id"] = entities[name]["id"]
        if "column" in e:
            out["column"] = e["column"]
        if name in entities and entities[name].get("dimension"):
            out["dimension"] = entities[name]["dimension"]
        return out

    metrics = manifest.get("metrics", {})
    return {
        "kind": kind,
        "subject": entity("subject"),
        "object": entity("object"),
        "metrics": {m: {k: metrics.get(m, {}).get(k) for k in ("description", "scale", "better", "score_kind",
                                                                 "confirmed")}
                    for m in spec["metrics"]},
        "columns": spec.get("keep", []),
        "claims": claims.get(kind, {}),
    }


def _step_text(step: dict) -> dict:
    out = {}
    if step.get("where"):
        out["where"] = step["where"]
    out["order"] = f"{step['order']['metric']} {step['order'].get('dir', 'desc')}, then subject ascending"
    return out


def _dimension_params(qdoc: dict) -> dict:
    """param name -> the dataset dimension it selects."""
    out = {}
    for dim, raw in (qdoc["plan"][0]["anchor"].get("dimensions") or {}).items():
        if isinstance(raw, str) and raw.startswith("$"):
            out[raw[1:]] = dim
    return out


def _entry(con, collection: str, rel, record: dict, name: str, q: dict) -> dict:
    manifest = record["manifest"]
    doc = q["doc"]
    relations = manifest.get("relations", {})
    claims = _claims_by_kind(record, relations)
    dims = _dimension_params(doc)
    params = {}
    notes = []
    for pname, spec in doc["params"].items():
        p = dict(spec)
        if pname in dims:
            vals = dimension_values(con, collection, dims[pname], release=rel["release_id"])
            p["dimension"] = dims[pname]
            p["values"] = {
                "count": vals["count"],
                "sample": vals["values"][:SAMPLE_VALUES],
                "all": f"/v1/collections/{collection}/dimensions/{dims[pname]}",
            }
            chosen = doc["example"].get(pname, spec.get("default"))
            known = {k for v in vals["values"] for k in (v["value"], *v.get("also_accepts", ()))}
            if chosen is not None and chosen not in known:
                notes.append(f"The example's {pname} {chosen!r} is not in this release, so the example returns no "
                             f"rows; pick a value from params.{pname}.values.")
        params[pname] = p
    anchor = doc["plan"][0]["anchor"]
    rows = {**_kind_shape(manifest, anchor["kind"], claims), **_step_text(anchor), "limit": anchor["limit"]}
    joins = {}
    for step in doc["plan"][1:]:
        j = step["join"]
        edge = manifest.get("edge_kinds", {}).get(j["via"], {})
        joins[j["as"]] = {
            "via": j["via"],
            "edge": {"from": (edge.get("from") or {}).get("entity"), "to": (edge.get("to") or {}).get("entity"),
                     "claims": claims.get(j["via"], {})},
            "row": _kind_shape(manifest, j["kind"], claims),
            **_step_text(j),
            "limit_per_anchor": j["limit_per_anchor"],
        }
    keys = sorted({k for kind in [anchor["kind"], *[s["join"]["kind"] for s in doc["plan"][1:]],
                                  *[s["join"]["via"] for s in doc["plan"][1:]]]
                   for k in claims.get(kind, {})})
    example = doc["example"]
    gql = (f'query Run($variables: JSON) {{ runQuery(collection: "{collection}", query: "{name}", '
           'variables: $variables) }')
    return {
        "collection": collection,
        "release": {"id": rel["release_id"], "version": rel["version"]},
        "name": name,
        "description": " ".join(doc["description"].split()),
        "params": params,
        "returns": {"rows": rows, "joins": joins},
        "claims": keys,
        "plan": doc["plan"],
        **({"notes": notes} if notes else {}),
        "run": {
            "graphql": {"query": gql, "variables": {"variables": example}},
            "rest": {"method": "POST", "path": f"/v1/collections/{collection}/queries/{name}:run",
                     "body": {"variables": example}},
        },
    }


def catalog(con, collection: str | None = None, release: str | None = None) -> dict:
    """The guide and every query of every collection (or one collection)."""
    if collection is None:
        keys = [r["collection_key"] for r in con.execute(
            "SELECT collection_key FROM collections WHERE current_release_id IS NOT NULL ORDER BY collection_key")]
    else:
        keys = [collection]
    queries = []
    for key in keys:
        rel = release_row(con, key, release if collection is not None else None)
        record = _json(rel["manifest"])
        for name, q in sorted(record["queries"].items()):
            queries.append(_entry(con, key, rel, record, name, q))
    return {"guide": guide(), "queries": queries}


def dimension_values(con, collection: str, dimension: str, *, release: str | None = None,
                     search: str | None = None, first: int | None = None) -> dict:
    """The values a dataset dimension takes in a release, with their labels,
    the other keys a query accepts for them, and how many datasets each has."""
    rel = release_row(con, collection, release)
    record = _json(rel["manifest"])
    manifest = record["manifest"]
    spec = next((d for d in manifest["dataset_dimensions"] if d["name"] == dimension), None)
    if spec is None:
        raise CollectionNotFound(f"collection {collection!r} has no dimension {dimension!r}; it has "
                                 f"{[d['name'] for d in manifest['dataset_dimensions']]}")
    rows = con.execute(
        "SELECT d.value, MAX(d.label) AS label, COUNT(*) AS n FROM dataset_dimensions d "
        "JOIN release_datasets rd ON rd.dataset_id = d.dataset_id AND rd.release_id = ? "
        "WHERE d.dimension = ? GROUP BY d.value ORDER BY d.value", (rel["release_id"], dimension)).fetchall()
    aliases: dict[str, set] = {}
    for key, target in (record.get("mappings") or {}).get(dimension, {}).items():
        aliases.setdefault(target[0] if isinstance(target, (list, tuple)) else target, set()).add(key)
    if spec.get("resolve") == "kpn-traits":
        # kpn_traits.trait_id is an ASCII column: MySQL refuses to compare it with a non-ASCII key.
        values = [r["value"] for r in rows if r["value"].isascii()]
        for i in range(0, len(values), 500):
            chunk = values[i:i + 500]
            for t in con.execute("SELECT trait_id, pigean_id, legacy_id FROM kpn_traits WHERE trait_id IN "
                                 f"({','.join('?' * len(chunk))})", chunk).fetchall():
                aliases.setdefault(t["trait_id"], set()).update(
                    k for k in (t["legacy_id"], t["pigean_id"]) if k and k != t["trait_id"])
    out = []
    needle = (search or "").lower()
    for r in rows:
        also = sorted(aliases.get(r["value"], ()))
        if needle and not any(needle in (s or "").lower() for s in (r["value"], r["label"], *also)):
            continue
        item = {"value": r["value"], "label": r["label"], "datasets": r["n"]}
        if also:
            item["also_accepts"] = also
        out.append(item)
    limit = min(first if first is not None else MAX_VALUES, MAX_VALUES)
    return {"collection": collection, "release": {"id": rel["release_id"], "version": rel["version"]},
            "dimension": dimension, "count": len(out), "values": out[:limit], "truncated": len(out) > limit}
