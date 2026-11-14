"""Serving collections: re-rendering claims on serve, discovery, and the
query plan executor.

A collection claim is re-rendered from its row (or edge group) and its mint
context every time it is served, and refused unless the result has the id the
ingester recorded. The executor runs a query template's bounded plan: an
anchor step (filter, order and limit rows of one kind before anything else),
then a join through an edge kind with a per-anchor window. Each step filters
and orders only on its own kind's metrics, which the manifest checker enforces
when the collection is loaded.
"""
from __future__ import annotations

import json
from functools import lru_cache

from . import engine
from . import identity as idn
from .manifest import ManifestError, bind_params
from .publish import dataset_ns, group_inputs, row_inputs


class CollectionNotFound(LookupError):
    pass


class ClaimRenderError(RuntimeError):
    """The template no longer reproduces the claim a row names."""


FILTER_OPS = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">=", "eq": "=", "ne": "!="}


@lru_cache(maxsize=256)
def _context(activity_id: str, context_json: str) -> engine.MintContext:
    return engine.MintContext.from_record(json.loads(context_json))


def mint_context(con, activity_id: str) -> engine.MintContext:
    row = con.execute("SELECT context FROM mint_contexts WHERE activity_id = ?", (activity_id,)).fetchone()
    if row is None:
        raise ClaimRenderError(f"no mint context {activity_id}")
    raw = row["context"]
    return _context(activity_id, raw if isinstance(raw, str) else json.dumps(raw))


def collection_of(con, rc) -> str:
    return mint_context(con, rc["activity_id"]).minting["collection"]


def render_claim(con, rc) -> dict:
    """The claim a result_claims row names, re-rendered and checked."""
    ctx = mint_context(con, rc["activity_id"])
    dataset = dataset_ns(con, rc["dataset_id"])
    if rc["row_id"] is not None:
        r = con.execute("SELECT * FROM result_rows WHERE row_id = ?", (rc["row_id"],)).fetchone()
        if r is None:
            raise ClaimRenderError(f"row {rc['row_id']} behind {rc['claim_id']} is gone")
        out = engine.render_row(ctx, dataset=dataset, **row_inputs(ctx.minting, r["kind"], r))
    else:
        out = engine.render_group(ctx, dataset=dataset, group=group_inputs(
            con, ctx.minting, rc["dataset_id"], rc["edge_kind"], rc["group_id"]))
    claim = out.claims.get(rc["claim_key"])
    if claim is None or claim["id"] != rc["claim_id"]:
        raise ClaimRenderError(
            f"template drift: {rc['claim_id']} is recorded for this row, but its template now renders "
            f"{claim['id'] if claim else 'no such claim'}; refusing to serve it")
    return claim


# --- releases and discovery -------------------------------------------------

def _json(value):
    return value if isinstance(value, (dict, list)) else json.loads(value)


def release_row(con, collection: str, release: str | None = None):
    """The current release, or one named by version or release id."""
    if release:
        row = con.execute("SELECT * FROM collection_releases WHERE collection_key = ? AND (version = ? OR "
                          "release_id = ?)", (collection, release, release)).fetchone()
    else:
        row = con.execute("SELECT r.* FROM collections c JOIN collection_releases r ON "
                          "r.release_id = c.current_release_id WHERE c.collection_key = ?", (collection,)).fetchone()
    if row is None:
        raise CollectionNotFound(f"no collection {collection!r}" + (f" release {release!r}" if release else ""))
    return row


def list_collections(con) -> list[dict]:
    out = []
    for c in con.execute("SELECT c.*, r.version FROM collections c LEFT JOIN collection_releases r "
                         "ON r.release_id = c.current_release_id ORDER BY c.collection_key").fetchall():
        n = con.execute("SELECT COUNT(*) AS n FROM release_datasets WHERE release_id = ?",
                        (c["current_release_id"],)).fetchone()["n"]
        manifest = _json(release_row(con, c["collection_key"])["manifest"])["manifest"]
        out.append({"id": c["collection_key"], "title": c["title"], "description": c["description"],
                    "methods": manifest["methods"], "current_release": c["current_release_id"],
                    "version": c["version"], "datasets": n})
    return out


def describe(con, collection: str, release: str | None = None) -> dict:
    """What a collection holds and how to read it, from its release manifest."""
    rel = release_row(con, collection, release)
    record = _json(rel["manifest"])
    m = record["manifest"]
    releases = [{"id": r["release_id"], "version": r["version"], "published_at": str(r["published_at"]),
                 "git_commit": r["git_commit"]}
                for r in con.execute("SELECT release_id, version, published_at, git_commit FROM collection_releases "
                                     "WHERE collection_key = ? ORDER BY published_at, version", (collection,))]
    datasets = []
    for d in con.execute("SELECT dataset_id FROM release_datasets WHERE release_id = ? ORDER BY dataset_id",
                         (rel["release_id"],)).fetchall():
        dims = {r["dimension"]: {"value": r["value"], "label": r["label"]} for r in con.execute(
            "SELECT dimension, value, label FROM dataset_dimensions WHERE dataset_id = ?", (d["dataset_id"],))}
        files = {r["role"]: r["file_id"] for r in con.execute(
            "SELECT role, file_id FROM dataset_files WHERE dataset_id = ?", (d["dataset_id"],))}
        counts = {r["kind"]: r["n"] for r in con.execute(
            "SELECT kind, COUNT(*) AS n FROM result_rows WHERE dataset_id = ? GROUP BY kind", (d["dataset_id"],))}
        counts.update({r["kind"]: r["n"] for r in con.execute(
            "SELECT kind, COUNT(*) AS n FROM result_edges WHERE dataset_id = ? GROUP BY kind", (d["dataset_id"],))})
        datasets.append({"id": d["dataset_id"], "dimensions": dims, "files": files, "counts": counts})
    claims = con.execute(
        "SELECT COUNT(*) AS n FROM result_claims rc JOIN release_mint_contexts rm ON rm.activity_id = rc.activity_id "
        "AND rm.release_id = ?", (rel["release_id"],)).fetchone()["n"]
    minted = con.execute(
        "SELECT COUNT(*) AS n FROM result_claims rc JOIN release_mint_contexts rm ON rm.activity_id = rc.activity_id "
        "AND rm.release_id = ? JOIN dapper_objects o ON o.id = rc.claim_id", (rel["release_id"],)).fetchone()["n"]
    return {
        "id": collection,
        "title": m["title"],
        "description": m.get("description"),
        "owners": m.get("owners", []),
        "methods": m["methods"],
        "dapper_profile": m["dapper_profile"],
        "release": {"id": rel["release_id"], "version": rel["version"], "git_commit": rel["git_commit"]},
        "releases": releases,
        "dataset_dimensions": m["dataset_dimensions"],
        "datasets": datasets,
        "row_kinds": m["row_kinds"],
        "edge_kinds": m.get("edge_kinds", {}),
        "metrics": m["metrics"],
        "tier_rules": m.get("tier_rules", {}),
        "templates": [{"name": t["doc"]["template"], "version": t["doc"]["version"], "for": t["doc"]["for"],
                       "file_id": t["file_id"], "claims": [c["key"] for c in t["doc"]["claims"]]}
                      for t in record["templates"]],
        "queries": sorted(record["queries"]),
        "claims": {"mintable": claims, "minted": minted},
        "dapper_document": f"/v1/collections/{collection}/releases/{rel['version']}/document",
    }


def list_queries(con, collection: str, release: str | None = None) -> list[dict]:
    rel = release_row(con, collection, release)
    out = []
    for name, q in sorted(_json(rel["manifest"])["queries"].items()):
        doc = q["doc"]
        out.append({
            "name": name,
            "description": " ".join(doc["description"].split()),
            "params": doc["params"],
            "plan": doc["plan"],
            "example": {"variables": doc["example"],
                        "rest": f"POST /v1/collections/{collection}/queries/{name}:run",
                        "body": {"variables": doc["example"]},
                        "graphql": (f'{{ runQuery(collection: "{collection}", query: "{name}", '
                                    f'variables: {_gql_literal(doc["example"])}) }}')},
            "file_id": q["file_id"],
        })
    return out


def _gql_literal(value) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_gql_literal(v)}" for k, v in value.items()) + "}"
    return json.dumps(value)


def release_document(con, collection: str, version: str) -> dict:
    return _json(release_row(con, collection, version)["document"])


# --- the executor -----------------------------------------------------------

def _resolve_value(con, manifest: dict, dimension: str, raw: str) -> str:
    spec = next((d for d in manifest["dataset_dimensions"] if d["name"] == dimension), {})
    if spec.get("resolve") == "kpn-traits":
        for col in ("trait_id", "pigean_id", "legacy_id"):
            r = con.execute(f"SELECT trait_id FROM kpn_traits WHERE {col} = ? LIMIT 1", (raw,)).fetchone()
            if r is not None:
                return r["trait_id"]
    return raw


def _bind(value, variables):
    if isinstance(value, str) and value.startswith("$"):
        return variables[value[1:]]
    return value


def _metric_filters(alias, where, variables):
    """JOIN clauses + params for metric conditions; a condition whose
    variable is unset (null) is dropped."""
    joins, params = [], []
    for i, (metric, conds) in enumerate(sorted((where or {}).items())):
        for j, (op, raw) in enumerate(sorted(conds.items())):
            value = _bind(raw, variables)
            if value is None:
                continue
            if op not in FILTER_OPS:
                raise ManifestError(f"unknown filter operator {op!r}")
            a = f"{alias}w{i}_{j}"
            joins.append(f"JOIN result_metrics {a} ON {a}.row_id = {alias}.row_id AND {a}.metric = ? "
                         f"AND {a}.value {FILTER_OPS[op]} ?")
            params.extend([metric, value])
    return joins, params


def _row_view(r, manifest, dims_cache, con):
    kind = manifest["row_kinds"][r["kind"]]
    row = json.loads(r["row_json"])
    if r["dataset_id"] not in dims_cache:
        dims_cache[r["dataset_id"]] = {d["dimension"]: d["value"] for d in con.execute(
            "SELECT dimension, value FROM dataset_dimensions WHERE dataset_id = ?", (r["dataset_id"],))}
    return {
        "kind": r["kind"],
        "dataset": {"id": r["dataset_id"], "dimensions": dims_cache[r["dataset_id"]]},
        "subject": {"id": r["subject_id"], "value": r["subject_value"], "label": r["subject_label"]},
        "object": {"id": r["object_id"], "label": r["object_label"]},
        "metrics": {m: idn.num(row[m]) for m in kind["metrics"]},
        "columns": {c: row.get(c) for c in kind.get("keep", [])},
        "_row_id": r["row_id"],
    }


def run_query(con, collection: str, name: str, variables: dict | None = None, *, release: str | None = None,
              claims: list[str] | None = None, envelope=None) -> dict:
    """Run a query template. `envelope(con, claim_id, source)` serves (and
    asserts) one claim; `claims` limits which claim keys are served (default
    all, [] for none)."""
    rel = release_row(con, collection, release)
    record = _json(rel["manifest"])
    manifest = record["manifest"]
    if name not in record["queries"]:
        raise CollectionNotFound(f"collection {collection!r} has no query {name!r}; "
                                 f"it has {sorted(record['queries'])}")
    qdoc = record["queries"][name]["doc"]
    bound = bind_params(qdoc, variables)
    warnings: list[str] = []
    dims_cache: dict = {}

    # Anchor: filter, order and limit before any join.
    a = qdoc["plan"][0]["anchor"]
    joins, params = [], [rel["release_id"]]
    for i, (dim, raw) in enumerate(sorted((a.get("dimensions") or {}).items())):
        value = _bind(raw, bound)
        if value is None:
            continue
        joins.append(f"JOIN dataset_dimensions d{i} ON d{i}.dataset_id = r.dataset_id AND d{i}.dimension = ? "
                     f"AND d{i}.value = ?")
        params.extend([dim, _resolve_value(con, manifest, dim, value)])
    where_joins, where_params = _metric_filters("r", a.get("where"), bound)
    direction = "ASC" if a["order"].get("dir") == "asc" else "DESC"
    sql = ("SELECT r.* FROM result_rows r "
           "JOIN release_datasets rd ON rd.dataset_id = r.dataset_id AND rd.release_id = ? "
           + " ".join(joins) + " "
           + "JOIN result_metrics o ON o.row_id = r.row_id AND o.metric = ? "
           + " ".join(where_joins)
           + f" WHERE r.kind = ? ORDER BY o.value {direction}, r.subject_value ASC, r.dataset_id ASC LIMIT ?")
    anchors = con.execute(sql, [*params, a["order"]["metric"], *where_params, a["kind"],
                                _bind(a["limit"], bound)]).fetchall()
    rows = [_row_view(r, manifest, dims_cache, con) for r in anchors]

    # Joins through an edge kind, each anchor keeping its own window.
    for step in qdoc["plan"][1:]:
        j = step["join"]
        for row in rows:
            row[j["as"]] = []
        by_dataset: dict[str, list[dict]] = {}
        for row in rows:
            by_dataset.setdefault(row["dataset"]["id"], []).append(row)
        jdir = "ASC" if j["order"].get("dir") == "asc" else "DESC"
        limit = _bind(j["limit_per_anchor"], bound)
        for dataset_id, members in sorted(by_dataset.items()):
            if not con.execute("SELECT 1 FROM result_edges WHERE dataset_id = ? AND kind = ? LIMIT 1",
                               (dataset_id, j["via"])).fetchone():
                warnings.append(
                    f"No {j['via']} edges are loaded for dataset {dataset_id} "
                    f"({', '.join(f'{k}={v}' for k, v in members[0]['dataset']['dimensions'].items())}), so "
                    f"the {j['as']} join could not run; anchors are returned without {j['as']}. This reflects "
                    "a missing data source, not an empty scientific result.")
                continue
            subjects = sorted({m["subject"]["id"] for m in members})
            wj, wp = _metric_filters("t", j.get("where"), bound)
            marks = ",".join("?" * len(subjects))
            joined = con.execute(
                "SELECT * FROM (SELECT e.from_id AS e_from, e.to_id AS e_to, e.from_value AS e_from_value, "
                "e.to_value AS e_to_value, t.*, ROW_NUMBER() OVER (PARTITION BY e.from_id "
                f"ORDER BY o.value {jdir}, t.subject_value ASC) AS e_rank "
                "FROM result_edges e "
                "JOIN result_rows t ON t.dataset_id = e.dataset_id AND t.kind = ? AND t.subject_id = e.to_id "
                "JOIN result_metrics o ON o.row_id = t.row_id AND o.metric = ? " + " ".join(wj) +
                f" WHERE e.dataset_id = ? AND e.kind = ? AND e.from_id IN ({marks})) x "
                "WHERE e_rank <= ? ORDER BY e_from, e_rank",
                [j["kind"], j["order"]["metric"], *wp, dataset_id, j["via"], *subjects, limit]).fetchall()
            index = {m["subject"]["id"]: m for m in members}
            for r in joined:
                index[r["e_from"]][j["as"]].append({
                    "edge": {"kind": j["via"], "dataset_id": dataset_id,
                             "from": {"id": r["e_from"], "value": r["e_from_value"]},
                             "to": {"id": r["e_to"], "value": r["e_to_value"]}},
                    "row": _row_view(r, manifest, dims_cache, con),
                })

    if envelope is not None and claims != []:
        _attach_claims(con, rel["release_id"], rows, qdoc, claims, envelope)
    for row in _walk_rows(rows, qdoc):
        row.pop("_row_id", None)
    return {"collection": collection, "release": {"id": rel["release_id"], "version": rel["version"]},
            "query": name, "variables": bound, "rows": rows, "warnings": warnings}


def _walk_rows(rows, qdoc):
    for row in rows:
        yield row
        for step in qdoc["plan"][1:]:
            for item in row.get(step["join"]["as"], []):
                yield item["row"]


def _attach_claims(con, release_id, rows, qdoc, keys, envelope):
    """Serve (and assert) each row's claims and each edge group's claims,
    limited to the release's mint contexts and the requested keys."""
    all_rows = list(_walk_rows(rows, qdoc))
    wanted = None if keys is None else set(keys)
    by_row: dict[int, dict] = {}
    ids = sorted({r["_row_id"] for r in all_rows})
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for rc in con.execute(
                "SELECT rc.* FROM result_claims rc JOIN release_mint_contexts rm ON rm.activity_id = rc.activity_id "
                f"AND rm.release_id = ? WHERE rc.row_id IN ({','.join('?' * len(chunk))}) ORDER BY rc.claim_key",
                [release_id, *chunk]).fetchall():
            if wanted is None or rc["claim_key"] in wanted:
                by_row.setdefault(rc["row_id"], {})[rc["claim_key"]] = rc
    for r in all_rows:
        r["claims"] = {k: envelope(con, rc["claim_id"], ("collection", rc))
                       for k, rc in sorted(by_row.get(r["_row_id"], {}).items())}
    groups: dict[tuple, dict] = {}
    for row in rows:
        for step in qdoc["plan"][1:]:
            for item in row.get(step["join"]["as"], []):
                e = item["edge"]
                key = (e["dataset_id"], e["kind"], e["to"]["id"])
                if key not in groups:
                    groups[key] = {}
                    for rc in con.execute(
                            "SELECT rc.* FROM result_claims rc JOIN release_mint_contexts rm ON "
                            "rm.activity_id = rc.activity_id AND rm.release_id = ? WHERE rc.dataset_id = ? AND "
                            "rc.edge_kind = ? AND rc.group_id = ? ORDER BY rc.claim_key",
                            (release_id, *key)).fetchall():
                        if wanted is None or rc["claim_key"] in wanted:
                            groups[key][rc["claim_key"]] = envelope(con, rc["claim_id"], ("collection", rc))
                e["claims"] = groups[key]
