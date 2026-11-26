"""Serving collections: rendering claims on serve, discovery, and the
query plan executor.

A collection claim is rendered from its row (or edge group) and a mint
context of the release every time it is served; nothing about it is computed
ahead. The first serve stores it and records it in result_claims (claim id ->
mint context, row or group), which is how its id resolves afterwards; a later
render from that record must reproduce the id, or the claim is refused. The executor runs a query template's bounded plan: an
anchor step (filter, order and limit rows of one kind before anything else),
then a join through an edge kind with a per-anchor window. Each step filters
and orders only on its own kind's metrics, which the manifest checker enforces
when the collection is loaded.
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict

from . import engine
from . import identity as idn
from .manifest import ManifestError, bind_params
from .publish import (dataset_dimensions, dataset_ns, dataset_role_files, group_info, group_inputs,
                      prefetch_groups, request_memo, row_inputs)
from .store import compile_where, get_store


class CollectionNotFound(LookupError):
    pass


class ClaimRenderError(RuntimeError):
    """The template no longer reproduces the claim a row names."""


FILTER_OPS = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">=", "eq": "=", "ne": "!="}


# Parsed mint contexts by activity id. A context is immutable under its id (the id is minted from it), so the
# id alone keys the cache; a context is read from the database only on a miss. ~2k covers every dataset of a
# PIGEAN-sized release for one template.
_CONTEXTS: OrderedDict[str, engine.MintContext] = OrderedDict()
_CONTEXTS_MAX = 2048
_CONTEXTS_LOCK = threading.Lock()


def _cached_context(activity_id: str) -> engine.MintContext | None:
    with _CONTEXTS_LOCK:
        ctx = _CONTEXTS.get(activity_id)
        if ctx is not None:
            _CONTEXTS.move_to_end(activity_id)
        return ctx


def _cache_context(activity_id: str, raw) -> engine.MintContext:
    ctx = engine.MintContext.from_record(raw if isinstance(raw, dict) else json.loads(raw))
    with _CONTEXTS_LOCK:
        _CONTEXTS[activity_id] = ctx
        while len(_CONTEXTS) > _CONTEXTS_MAX:
            _CONTEXTS.popitem(last=False)
    return ctx


def mint_context(con, activity_id: str) -> engine.MintContext:
    ctx = _cached_context(activity_id)
    if ctx is not None:
        return ctx
    row = con.execute("SELECT context FROM mint_contexts WHERE activity_id = ?", (activity_id,)).fetchone()
    if row is None:
        raise ClaimRenderError(f"no mint context {activity_id}")
    return _cache_context(activity_id, row["context"])


def collection_of(con, rc) -> str:
    return mint_context(con, rc["activity_id"]).minting["collection"]


CLAIM_FIELDS = ("claim_id", "activity_id", "claim_key", "dataset_id", "row_id", "edge_kind", "group_id",
                "proposition_id")


def render_claim(con, rc) -> dict:
    """The claim a claim record names, rendered and checked."""
    return render_claim_full(con, rc)[0]


def render_claim_full(con, rc) -> tuple[dict, dict]:
    """(claim, the data-plane objects it references) for a claim record: one
    implied_*_claims just rendered (returned as it is), or one read back from
    result_claims (rendered again and checked against its id)."""
    if "_rendered" in rc:
        return rc["_rendered"][0][rc["claim_key"]], rc["_rendered"][1]
    store = get_store(con, [rc["dataset_id"]])
    ctx = mint_context(con, rc["activity_id"])
    if rc["row_id"] is not None:
        r = store.row(rc["dataset_id"], rc["row_id"])
        if r is None:
            raise ClaimRenderError(f"row {rc['row_id']} behind {rc['claim_id']} is gone")
        claims, _, objects = _render_row(con, ctx, dataset_ns(con, rc["dataset_id"]), r)
    else:
        claims, _, objects = _render_group(con, ctx, dataset_ns(con, rc["dataset_id"]), rc["dataset_id"],
                                           rc["edge_kind"], rc["group_id"])
    claim = claims.get(rc["claim_key"])
    if claim is None or claim["id"] != rc["claim_id"]:
        raise ClaimRenderError(
            f"template drift: {rc['claim_id']} is recorded for this row, but its template now renders "
            f"{claim['id'] if claim else 'no such claim'}; refusing to serve it")
    return claim, objects


def _objects(con, ctx, dataset_id, out, gene_sets) -> dict:
    """The data-plane objects a render references (stored when one of its claims is first served). A lazy
    store (Parquet) stores no gene sets at ingest, so the ones named here are rebuilt from the edges."""
    objects = {oid: doc for oid, (_, doc) in out.objects.items()}
    if get_store(con, [dataset_id]).lazy_objects:
        for gid in gene_sets:
            doc = rebuild_gene_set(con, ctx.minting, dataset_id, gid)
            if doc is not None:
                objects[doc["id"]] = doc
    return objects


def _render_row(con, ctx, dataset: dict, r) -> tuple[dict, dict, dict]:
    out = engine.render_row(ctx, dataset=dataset, **row_inputs(ctx.minting, r["kind"], r))
    gene_sets = [r["subject_id"]] if r["subject_id"].startswith("dapper:GeneSet.") else []
    return out.claims, out.propositions, _objects(con, ctx, dataset["id"], out, gene_sets)


def _render_group(con, ctx, dataset: dict, dataset_id, edge_kind, group_id) -> tuple[dict, dict, dict]:
    out = engine.render_group(ctx, dataset=dataset, group=group_inputs(con, ctx.minting, dataset_id, edge_kind,
                                                                       group_id))
    return out.claims, out.propositions, _objects(con, ctx, dataset_id, out, [group_id])


def _release_contexts(con, release_id: str, dataset_ids) -> dict[str, list]:
    """dataset id -> (activity id, mint context) for the release's mint contexts over those datasets (each
    dataset read once per request)."""
    memo = request_memo(con)
    key = ("contexts", release_id)
    known = memo.setdefault(key, {})
    ask = sorted(set(dataset_ids) - known.keys())
    if ask:
        known.update(_release_contexts_read(con, release_id, ask))
    return {d: known[d] for d in sorted(set(dataset_ids))}


def _release_contexts_read(con, release_id: str, dataset_ids) -> dict[str, list]:
    ids = sorted(set(dataset_ids))
    pairs = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        pairs += [(r["activity_id"], r["dataset_id"]) for r in con.execute(
            f"SELECT activity_id, dataset_id FROM release_mint_contexts WHERE release_id = ? AND "
            f"dataset_id IN ({','.join('?' * len(chunk))}) ORDER BY activity_id", [release_id, *chunk]).fetchall()]
    contexts = {a: c for a in {a for a, _ in pairs} if (c := _cached_context(a)) is not None}
    missing = sorted({a for a, _ in pairs} - contexts.keys())
    for i in range(0, len(missing), 200):
        chunk = missing[i:i + 200]
        for r in con.execute(f"SELECT activity_id, context FROM mint_contexts WHERE activity_id IN "
                             f"({','.join('?' * len(chunk))})", chunk).fetchall():
            contexts[r["activity_id"]] = _cache_context(r["activity_id"], r["context"])
    out: dict[str, list] = {d: [] for d in ids}
    for activity_id, dataset_id in pairs:
        out[dataset_id].append((activity_id, contexts[activity_id]))
    return out


def _record(ctx_id, key, dataset_id, row_id, edge_kind, group_id, claims, propositions, objects) -> dict:
    return {"claim_id": claims[key]["id"], "activity_id": ctx_id, "claim_key": key, "dataset_id": dataset_id,
            "row_id": row_id, "edge_kind": edge_kind, "group_id": group_id, "proposition_id": propositions[key],
            "_rendered": (claims, objects)}


def implied_row_claims(con, release_id: str, rows, keys=None) -> list[dict]:
    """The claims the release's templates make of these stored result rows, rendered now (each row once per
    mint context): claim records carrying their rendered claim, for envelope() to serve."""
    wanted = None if keys is None else set(keys)
    contexts = _release_contexts(con, release_id, [r["dataset_id"] for r in rows])
    datasets: dict[str, dict] = {}
    out = []
    for r in rows:
        for activity_id, ctx in contexts[r["dataset_id"]]:
            if ctx.template["for"] != r["kind"]:
                continue
            if r["dataset_id"] not in datasets:
                datasets[r["dataset_id"]] = dataset_ns(con, r["dataset_id"])
            claims, props, objects = _render_row(con, ctx, datasets[r["dataset_id"]], r)
            out += [_record(activity_id, key, r["dataset_id"], r["row_id"], None, None, claims, props, objects)
                    for key in sorted(claims) if wanted is None or key in wanted]
    return out


def implied_group_claims(con, release_id: str, dataset_id: str, edge_kind: str, group_id: str,
                         keys=None) -> list[dict]:
    """The claims the release's templates make of one edge group (every edge of a kind ending at group_id)."""
    wanted = None if keys is None else set(keys)
    out = []
    dataset = None
    for activity_id, ctx in _release_contexts(con, release_id, [dataset_id])[dataset_id]:
        if ctx.template["for"] != edge_kind or ctx.template.get("group_by") != "to":
            continue
        dataset = dataset or dataset_ns(con, dataset_id)
        claims, props, objects = _render_group(con, ctx, dataset, dataset_id, edge_kind, group_id)
        out += [_record(activity_id, key, dataset_id, None, edge_kind, group_id, claims, props, objects)
                for key in sorted(claims) if wanted is None or key in wanted]
    return out


def served_claim(con, claim_id: str) -> dict | None:
    """The record of a collection claim that has been served (result_claims), or None."""
    r = con.execute("SELECT * FROM result_claims WHERE claim_id = ?", (claim_id,)).fetchone()
    return dict(r) if r else None


def served_claims_for_proposition(con, proposition_id: str) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM result_claims WHERE proposition_id = ?",
                                         (proposition_id,)).fetchall()]


def rebuild_gene_set(con, minting: dict, dataset_id: str, gene_set_id: str) -> dict | None:
    """A GeneSet document as the publisher minted it, rebuilt from the
    dataset's edges (members are the genes whose edges end at it). None
    unless the rebuilt document has exactly the id the claim names."""
    store = get_store(con, [dataset_id])
    role_files = dataset_role_files(con, dataset_id)
    for kind, spec in (minting.get("edge_kinds") or {}).items():
        entity = minting["entities"].get(spec["to"]["entity"], {})
        if "mint" not in entity:
            continue
        library = dataset_dimensions(con, dataset_id).get(entity["library_dimension"])
        if library is None:
            continue
        lib_doc = engine.gene_set_collection(library)
        membership_file = next((fid for role, fid in sorted(role_files.items())
                                if _yields(minting, role) == entity.get("members_from")), None)
        found = group_info(con, dataset_id, kind, gene_set_id)
        if found is not None:
            doc = engine.gene_set(found[0], lib_doc, found[2], membership_file)
        else:  # a kept set with no member edges: its name is the row's subject value
            row = store.query(*_subject_lookup(store, dataset_id, gene_set_id))
            if not row:
                continue
            doc = engine.gene_set(row[0]["subject_value"], lib_doc, [], membership_file)
        if doc["id"] == gene_set_id:
            return doc
    return None


def _yields(minting: dict, role: str) -> str | None:
    return (minting.get("file_roles") or {}).get(role, {}).get("yields")


def _subject_lookup(store, dataset_id, subject_id):
    rows, _, _ = store.tables([dataset_id])
    return (f"SELECT r.subject_value FROM {rows} r WHERE r.dataset_id = ? AND r.subject_id = ? LIMIT 1",
            [dataset_id, subject_id])


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
        if c["current_release_id"] is None:
            continue  # an object collection registered but with no closed release yet
        n = con.execute("SELECT COUNT(*) AS n FROM release_datasets WHERE release_id = ?",
                        (c["current_release_id"],)).fetchone()["n"]
        manifest = _json(release_row(con, c["collection_key"])["manifest"])["manifest"]
        kind = manifest.get("kind", "files")
        objects = con.execute("SELECT COUNT(*) AS n FROM release_objects WHERE collection_key = ? AND version = ?",
                              (c["collection_key"], c["version"])).fetchone()["n"] if kind == "objects" else 0
        out.append({"id": c["collection_key"], "kind": kind, "title": c["title"], "description": c["description"],
                    "methods": manifest["methods"], "current_release": c["current_release_id"],
                    "version": c["version"], "datasets": n, "objects": objects})
    return out


MAX_DATASETS_PAGE = 500


def describe(con, collection: str, release: str | None = None, *, first: int = 50, offset: int = 0) -> dict:
    """What a collection holds and how to read it, from its release manifest. Datasets come a page at a
    time (`first`, `offset`); `datasets_page` says how many there are and where the next page starts."""
    if not 0 <= first <= MAX_DATASETS_PAGE or offset < 0:
        raise ValueError(f"first must be 0 to {MAX_DATASETS_PAGE} and offset at least 0")
    rel = release_row(con, collection, release)
    record = _json(rel["manifest"])
    if record.get("kind") == "objects":
        from . import objects

        return objects.describe(con, collection, rel, record)
    m = record["manifest"]
    releases = [{"id": r["release_id"], "version": r["version"], "published_at": str(r["published_at"]),
                 "git_commit": r["git_commit"]}
                for r in con.execute("SELECT release_id, version, published_at, git_commit FROM collection_releases "
                                     "WHERE collection_key = ? ORDER BY published_at, version", (collection,))]
    runs = documented_runs(record, _json(rel["document"]))
    datasets = []
    total = con.execute("SELECT COUNT(*) AS n FROM release_datasets WHERE release_id = ?",
                        (rel["release_id"],)).fetchone()["n"]
    page = [d["dataset_id"] for d in con.execute("SELECT dataset_id FROM release_datasets WHERE release_id = ? "
                                                  "ORDER BY dataset_id LIMIT ? OFFSET ?",
                                                  (rel["release_id"], first, offset)).fetchall()]
    store = get_store(con, page)
    for d in ({"dataset_id": x} for x in page):
        dims = {r["dimension"]: {"value": r["value"], "label": r["label"]} for r in con.execute(
            "SELECT dimension, value, label FROM dataset_dimensions WHERE dataset_id = ?", (d["dataset_id"],))}
        files = {r["role"]: r["file_id"] for r in con.execute(
            "SELECT role, file_id FROM dataset_files WHERE dataset_id = ?", (d["dataset_id"],))}
        datasets.append({"id": d["dataset_id"], "dimensions": dims, "files": files,
                         "counts": store.counts(d["dataset_id"]),
                         **({"provenance": runs[d["dataset_id"]]} if d["dataset_id"] in runs else {})})
    minted = con.execute("SELECT COUNT(*) AS n FROM result_claims rc JOIN release_mint_contexts rm "
                         "ON rm.activity_id = rc.activity_id AND rm.release_id = ?", (rel["release_id"],)).fetchone()["n"]
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
        "datasets_page": {"total": total, "offset": offset, "first": first, "returned": len(datasets),
                          "next_offset": offset + len(datasets) if offset + len(datasets) < total else None},
        "row_kinds": m["row_kinds"],
        "edge_kinds": m.get("edge_kinds", {}),
        "metrics": m["metrics"],
        "tier_rules": m.get("tier_rules", {}),
        "templates": [{"name": t["doc"]["template"], "version": t["doc"]["version"], "for": t["doc"]["for"],
                       "file_id": t["file_id"], "claims": [c["key"] for c in t["doc"]["claims"]]}
                      for t in record["templates"]],
        "queries": sorted(record["queries"]),
        "claims": {"minted": minted},
        "provenance": provenance_sources(record),
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


def provenance_resolver(con, object_id: str) -> str | None:
    """Where an id from a release's referenced (not stored) provenance
    document resolves: <resolver>/v1/entities/<id>, or None."""
    from urllib.parse import quote

    # Read only each manifest's provenance list. Sorting whole JSON manifests is the likely cause of QA
    # answering 500, not 404, for every unknown id (MySQL sorts the full rows), and it read them all each time.
    for r in con.execute("SELECT json_extract(manifest, '$.provenance') AS provenance FROM collection_releases "
                         "ORDER BY published_at DESC").fetchall():
        sources = _json(r["provenance"]) if r["provenance"] is not None else None
        for src in sources if isinstance(sources, list) else []:
            if not isinstance(src, dict):
                continue
            if not src.get("stored") and src.get("resolver") and object_id in set(src.get("ids") or []):
                return f"{src['resolver'].rstrip('/')}/v1/entities/{quote(object_id, safe=':')}"
    return None


def documented_runs(record: dict, document: dict) -> dict[str, dict]:
    """dataset id -> the run a provenance document names for it, and where that run resolves."""
    from urllib.parse import quote

    where = {}
    for src in record.get("provenance") or []:
        for oid in src.get("ids") or []:
            where[oid] = src
    out = {}
    datasets = {d["id"] for d in document.get("datasets") or []}
    for e in document.get("was_generated_by_edges") or []:
        if e.get("subject") in datasets and e.get("object") in where:
            src = where[e["object"]]
            out[e["subject"]] = {"run": e["object"], "stored": src.get("stored", False),
                                 "resolve": (f"/v1/objects/{e['object']}" if src.get("stored")
                                             else f"{src['resolver'].rstrip('/')}/v1/entities/{quote(e['object'], safe=':')}"
                                             if src.get("resolver") else None)}
    return out


def provenance_sources(record: dict) -> list[dict]:
    """A release's provenance for discovery: where each document is, without its id list."""
    return [{k: v for k, v in s.items() if k != "ids"} | {"objects": len(s.get("ids") or [])}
            for s in record.get("provenance") or []]


def release_document(con, collection: str, version: str) -> dict:
    return _json(release_row(con, collection, version)["document"])


# --- the executor -----------------------------------------------------------

def _resolve_value(con, manifest: dict, dimension: str, raw: str, mappings: dict | None = None) -> str:
    """A query's value for a dimension: through the collection's own mapping, else as given."""
    mapped = (mappings or {}).get(dimension, {}).get(raw)
    return mapped[0] if mapped else raw


def _bind(value, variables):
    if isinstance(value, str) and value.startswith("$"):
        return variables[value[1:]]
    return value


def release_scope(con, release_id: str) -> tuple[list[str], set[str]]:
    """The release's dataset ids and mint-context activity ids (control plane)."""
    datasets = [r["dataset_id"] for r in con.execute(
        "SELECT dataset_id FROM release_datasets WHERE release_id = ? ORDER BY dataset_id", (release_id,)).fetchall()]
    activities = {r["activity_id"] for r in con.execute(
        "SELECT activity_id FROM release_mint_contexts WHERE release_id = ?", (release_id,)).fetchall()}
    return datasets, activities


def _datasets_matching(con, release_id: str, dims: dict) -> list[str]:
    """The release's datasets whose dimensions equal every given value."""
    sql = "SELECT rd.dataset_id FROM release_datasets rd"
    params: list = []
    for i, (dim, value) in enumerate(sorted(dims.items())):
        sql += (f" JOIN dataset_dimensions d{i} ON d{i}.dataset_id = rd.dataset_id AND d{i}.dimension = ? "
                f"AND d{i}.value = ?")
        params.extend([dim, value])
    sql += " WHERE rd.release_id = ? ORDER BY rd.dataset_id"
    return [r["dataset_id"] for r in con.execute(sql, [*params, release_id]).fetchall()]


def _row_view(r, manifest, dims_cache, con):
    kind = manifest["row_kinds"][r["kind"]]
    row = json.loads(r["row_json"])
    if r["dataset_id"] not in dims_cache:
        dims_cache[r["dataset_id"]] = dict(dataset_dimensions(con, r["dataset_id"]))
    return {
        "kind": r["kind"],
        "dataset": {"id": r["dataset_id"], "dimensions": dims_cache[r["dataset_id"]]},
        "subject": {"id": r["subject_id"], "value": r["subject_value"], "label": r["subject_label"]},
        "object": {"id": r["object_id"], "label": r["object_label"]},
        "metrics": {m: idn.num(row[m]) for m in kind["metrics"]},
        "columns": {c: row.get(c) for c in kind.get("keep", [])},
        "_row_id": r["row_id"],
        "_raw": r,
    }


def run_query(con, collection: str, name: str, variables: dict | None = None, *, release: str | None = None,
              claims: list[str] | None = None, envelope=None) -> dict:
    """Run a query template. `envelope(con, claim_id, source)` serves (and
    asserts) one claim; `claims` limits which claim keys are served (default
    all, [] for none)."""
    rel = release_row(con, collection, release)
    record = _json(rel["manifest"])
    if record.get("kind") == "objects":
        from . import objects

        return objects.run_query(con, collection, name, variables, release, rel, record)
    if name not in record["queries"]:
        raise CollectionNotFound(f"collection {collection!r} has no query {name!r}; "
                                 f"it has {sorted(record['queries'])}")
    qdoc = record["queries"][name]["doc"]
    return run_plan(con, collection, qdoc, bind_params(qdoc, variables), release=release, name=name,
                    claims=claims, envelope=envelope)


def run_plan(con, collection: str, qdoc: dict, bound: dict, *, release: str | None = None, name: str | None = None,
             claims: list[str] | None = None, envelope=None) -> dict:
    """Run a query plan (a query template's `plan`) against a release.
    `bound` holds the values its `$` references take; literal values in the
    plan are used as they are."""
    rel = release_row(con, collection, release)
    record = _json(rel["manifest"])
    manifest = record["manifest"]
    warnings: list[str] = []
    dims_cache: dict = {}

    # Anchor: filter, order and limit before any join.
    a = qdoc["plan"][0]["anchor"]
    dims = {}
    for dim, raw in sorted((a.get("dimensions") or {}).items()):
        value = _bind(raw, bound)
        if value is not None:
            dims[dim] = _resolve_value(con, manifest, dim, value, record.get("mappings"))
    dataset_ids = _datasets_matching(con, rel["release_id"], dims)
    store = get_store(con, dataset_ids)
    direction = "ASC" if a["order"].get("dir") == "asc" else "DESC"
    subject = _bind(a.get("subject"), bound)
    subjects = None if subject is None else ([subject] if isinstance(subject, str) else list(subject))
    anchors = store.anchor(dataset_ids, a["kind"], _conds(a.get("where"), bound), a["order"]["metric"],
                           direction, _bind(a["limit"], bound), subjects)
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
            if not store.has_edges(dataset_id, j["via"]):
                warnings.append(
                    f"No {j['via']} edges are loaded for dataset {dataset_id} "
                    f"({', '.join(f'{k}={v}' for k, v in members[0]['dataset']['dimensions'].items())}), so "
                    f"the {j['as']} join could not run; anchors are returned without {j['as']}. This reflects "
                    "a missing data source, not an empty scientific result.")
                continue
            subjects = sorted({m["subject"]["id"] for m in members})
            joined = store.join(dataset_id, j["via"], subjects, j["kind"], _conds(j.get("where"), bound),
                                j["order"]["metric"], jdir, limit)
            index = {m["subject"]["id"]: m for m in members}
            for r in joined:
                index[r["e_from"]][j["as"]].append({
                    "edge": {"kind": j["via"], "dataset_id": dataset_id,
                             "from": {"id": r["e_from"], "value": r["e_from_value"]},
                             "to": {"id": r["e_to"], "value": r["e_to_value"]}},
                    "row": _row_view(r, manifest, dims_cache, con),
                })

    if envelope is not None and claims != []:
        _attach_claims(con, rel["release_id"], rows, qdoc, claims, envelope, dataset_ids)
    for row in _walk_rows(rows, qdoc):
        row.pop("_row_id", None)
        row.pop("_raw", None)
    return {"collection": collection, "release": {"id": rel["release_id"], "version": rel["version"]},
            "query": name, "variables": bound, "rows": rows, "warnings": warnings}


def _conds(where, bound):
    try:
        return compile_where(where, lambda raw: _bind(raw, bound))
    except ValueError as exc:
        raise ManifestError(str(exc)) from exc


def _walk_rows(rows, qdoc):
    for row in rows:
        yield row
        for step in qdoc["plan"][1:]:
            for item in row.get(step["join"]["as"], []):
                yield item["row"]


def _minted_group_kinds(con, release_id) -> dict[str, list[str]]:
    """{row kind: the edge kinds whose groups are its subjects}, for row kinds about a minted entity (one whose
    documents are built from edge groups, such as a gene set from its member edges). Read from the release's
    manifest, so it holds for any collection."""
    manifest = _json(con.execute("SELECT manifest FROM collection_releases WHERE release_id = ?",
                                 (release_id,)).fetchone()["manifest"])["manifest"]
    entities, edge_kinds = manifest.get("entities") or {}, manifest.get("edge_kinds") or {}
    out = {}
    for kind, rk in (manifest.get("row_kinds") or {}).items():
        entity = rk["subject"].get("entity")
        if "mint" in entities.get(entity, {}):
            out[kind] = [k for k, spec in edge_kinds.items() if spec["to"]["entity"] == entity]
    return out


def _attach_claims(con, release_id, rows, qdoc, keys, envelope, dataset_ids=None):
    """Render, serve (and assert) each row's claims and each edge group's
    claims under the release's mint contexts, limited to the requested keys."""
    all_rows = list(_walk_rows(rows, qdoc))
    unique = {r["_row_id"]: r["_raw"] for r in all_rows}
    # The gene sets this response renders (joined edge groups, and rows about a gene set), read up front: two
    # queries per dataset and edge kind rather than two per gene set.
    wanted_groups: dict[tuple, set] = {}
    for row in rows:
        for step in qdoc["plan"][1:]:
            for item in row.get(step["join"]["as"], []):
                e = item["edge"]
                wanted_groups.setdefault((e["dataset_id"], e["kind"]), set()).add(e["to"]["id"])
    minted = _minted_group_kinds(con, release_id)
    for r in all_rows:
        for kind in minted.get(r["kind"], []):
            wanted_groups.setdefault((r["dataset"]["id"], kind), set()).add(r["subject"]["id"])
    for (dataset_id, kind), ids in sorted(wanted_groups.items()):
        prefetch_groups(con, dataset_id, kind, ids)
    by_row: dict[int, dict] = {}
    for rc in implied_row_claims(con, release_id, [unique[i] for i in sorted(unique)], keys):
        by_row.setdefault(rc["row_id"], {})[rc["claim_key"]] = rc
    groups: dict[tuple, list] = {}
    for row in rows:
        for step in qdoc["plan"][1:]:
            for item in row.get(step["join"]["as"], []):
                e = item["edge"]
                key = (e["dataset_id"], e["kind"], e["to"]["id"])
                if key not in groups:
                    groups[key] = implied_group_claims(con, release_id, *key, keys=keys)
    # Every claim of the response, served in one batch when the caller can (envelope.many).
    wanted = [rc for r in all_rows for rc in by_row.get(r["_row_id"], {}).values()]
    wanted += [rc for found in groups.values() for rc in found]
    many = getattr(envelope, "many", None)
    if many is not None:
        served = dict(zip((rc["claim_id"] for rc in wanted), many(con, [(rc["claim_id"], ("collection", rc))
                                                                         for rc in wanted])))
    else:
        served = {rc["claim_id"]: envelope(con, rc["claim_id"], ("collection", rc)) for rc in wanted}
    for r in all_rows:
        r["claims"] = {k: served[rc["claim_id"]] for k, rc in sorted(by_row.get(r["_row_id"], {}).items())}
    for row in rows:
        for step in qdoc["plan"][1:]:
            for item in row.get(step["join"]["as"], []):
                e = item["edge"]
                e["claims"] = {rc["claim_key"]: served[rc["claim_id"]]
                               for rc in groups[(e["dataset_id"], e["kind"], e["to"]["id"])]}

