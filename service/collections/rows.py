"""Typed GraphQL reads over any collection of result rows: Query.rows and
Row.joined. The questions a collection's owners did not write a template for
are asked in GraphQL itself: pick a row kind, filter it by dimensions, subject
and metric conditions, order it, limit it, and follow edges to joined rows,
selecting only the fields you need.

GraphQL resolves a field item by item, but applies the same selection to every
item of a list. So the first item of a list to ask for a join, or for a claim
under a key, answers it for the whole list at once (one store join per dataset,
one envelopes() call per key) and the rest read the answer: a response costs a
few reads per list, not a few per row (docs/query-performance.md).

The same rules hold as for a template, checked against the collection's
manifest: a condition or order names a metric of the row kind it applies to
(scores of different kinds are never compared), a join follows an edge kind
whose ends match the two row kinds, and every list is bounded. Claims are
served when a row's `claim` field is selected, exactly as a template serves
them.
"""
from __future__ import annotations

import json

from .publish import prefetch_groups
from .serve import (CollectionNotFound, _datasets_matching, _json, _resolve_value, _row_view,
                    implied_group_claims, implied_row_claims, release_row)
from .store import FILTER_OPS, get_store

MAX_ROWS = 500
MAX_JOINED = 50
MAX_SUBJECTS = 100


def _metrics_of(manifest: dict, kind: str) -> list[str]:
    return manifest["row_kinds"][kind]["metrics"]


def _check_kind(collection: str, manifest: dict, kind: str) -> None:
    if kind not in manifest["row_kinds"]:
        raise ValueError(f"{collection} has no row kind {kind!r}; it has {sorted(manifest['row_kinds'])}")


def _conditions(collection: str, manifest: dict, kind: str, where, order) -> tuple[list, str, str]:
    """(store conditions, order metric, direction), refusing a metric that is not the kind's own."""
    own = _metrics_of(manifest, kind)

    def check(metric, what):
        if metric not in own:
            raise ValueError(f"{what}: {metric!r} is not a metric of {kind} rows in {collection}; they have {own}. "
                             "A step filters and orders only on its own kind's metrics.")

    conds = []
    for c in where or []:
        check(c["metric"], "where")
        for op in ("lt", "lte", "gt", "gte", "eq", "ne"):
            if c.get(op) is not None:
                conds.append((c["metric"], FILTER_OPS[op], c[op]))
    check(order["metric"], "orderBy")
    return conds, order["metric"], "ASC" if order.get("dir") == "ASC" else "DESC"


def _bounded(name: str, value: int, upper: int) -> int:
    if value is None or not 0 <= value <= upper:
        raise ValueError(f"{name} must be between 0 and {upper}, got {value}")
    return value


def rows(con, collection: str, kind: str, *, order: dict, first: int = 20, release: str | None = None,
         dimensions=None, subject=None, where=None) -> dict:
    _bounded("first", first, MAX_ROWS)
    rel = release_row(con, collection, release)
    record = _json(rel["manifest"])
    if record.get("kind") == "objects":
        raise ValueError(f"{collection} holds deposited objects, not result rows: use its query templates (runQuery)")
    manifest = record["manifest"]
    _check_kind(collection, manifest, kind)
    conds, metric, direction = _conditions(collection, manifest, kind, where, order)
    names = [d["name"] for d in manifest["dataset_dimensions"]]
    dims = {}
    for d in dimensions or []:
        if d["name"] not in names:
            raise ValueError(f"{collection} has no dimension {d['name']!r}; it has {names}")
        dims[d["name"]] = _resolve_value(con, manifest, d["name"], d["value"], record.get("mappings"))
    subjects = list(subject or []) or None
    if subjects and len(subjects) > MAX_SUBJECTS:
        raise ValueError(f"subject takes at most {MAX_SUBJECTS} values")
    dataset_ids = _datasets_matching(con, rel["release_id"], dims)
    found = get_store(con, dataset_ids).anchor(dataset_ids, kind, conds, metric, direction, first, subjects)
    root = {"release_id": rel["release_id"], "manifest": manifest, "groups": {}}
    out = _batch(con, found, root)
    warnings = [] if out else [
        f"No {kind} rows in {collection} release {rel['version']} match. Check dimension values with "
        f"dimensionValues(collection: \"{collection}\", dimension: ...) and subjects as the result files name them."]
    return {"collection": collection, "release": {"id": rel["release_id"], "version": rel["version"]},
            "rows": out, "warnings": warnings}


def _batch(con, found, root) -> list[dict]:
    """Row views sharing one claim lookup: the first `claim` selected loads the claims of every row in it."""
    batch = {"root": root, "claims": None, "rows": [], "envelopes": {}, "joins": {}}
    cache: dict = {}
    for r in found:
        view = _row_view(r, root["manifest"], cache, con)
        view["_batch"] = batch
        batch["rows"].append(view)
    return batch["rows"]


def joined(con, row: dict, via: str, kind: str, *, order: dict, first: int = 10, where=None) -> list[dict]:
    """Rows reached from `row` through an edge kind, ordered and limited per row. Computed for every row of
    the row's list the first time one asks (one store join per dataset); the joined rows of the whole list
    share one batch, and their edges one edge batch."""
    _bounded("first", first, MAX_JOINED)
    batch = row["_batch"]
    key = json.dumps([via, kind, order, first, where], sort_keys=True, default=str)
    if key not in batch["joins"]:
        batch["joins"][key] = _join_batch(con, batch, via, kind, order, first, where)
    return batch["joins"][key].get((row["dataset"]["id"], row["subject"]["id"]), [])


def _join_batch(con, batch: dict, via: str, kind: str, order: dict, first: int, where) -> dict:
    """{(dataset id, subject id): joined rows} for every row of a batch."""
    root = batch["root"]
    manifest = root["manifest"]
    collection = "this collection"
    _check_kind(collection, manifest, kind)
    edge = (manifest.get("edge_kinds") or {}).get(via)
    if edge is None:
        raise ValueError(f"no edge kind {via!r}; there are {sorted(manifest.get('edge_kinds') or {})}")
    conds, metric, direction = _conditions(collection, manifest, kind, where, order)
    by_dataset: dict[str, list] = {}
    for r in batch["rows"]:
        starts = manifest["row_kinds"][r["kind"]]["subject"].get("entity")
        ends = manifest["row_kinds"][kind]["subject"].get("entity")
        if edge["from"]["entity"] != starts or edge["to"]["entity"] != ends:
            raise ValueError(f"{via} goes from {edge['from']['entity']} to {edge['to']['entity']}; it cannot join "
                             f"{r['kind']} rows (about {starts}) to {kind} rows (about {ends})")
        by_dataset.setdefault(r["dataset"]["id"], []).append(r["subject"]["id"])
    found = []
    for dataset_id, subjects in sorted(by_dataset.items()):
        store = get_store(con, [dataset_id])
        if not store.has_edges(dataset_id, via):
            continue
        for r in store.join(dataset_id, via, sorted(set(subjects)), kind, conds, metric, direction, first):
            found.append((dataset_id, r))
    views = _batch(con, [r for _, r in found], root)
    edges: list[dict] = []
    out: dict[tuple, list] = {}
    for (dataset_id, r), v in zip(found, views):
        e = {"kind": via, "dataset_id": dataset_id, "from": {"id": r["e_from"], "value": r["e_from_value"]},
             "to": {"id": r["e_to"], "value": r["e_to_value"]}, "_root": root, "_edges": edges}
        edges.append(e)
        out.setdefault((dataset_id, r["e_from"]), []).append({"edge": e, "row": v})
    return out


def row_envelope(con, row: dict, key: str, many):
    """The envelope of `row`'s claim under `key`, served for every row of its batch at once (`many` is
    app.envelopes)."""
    batch = row["_batch"]
    if key not in batch["envelopes"]:
        wanted = [(r["_row_id"], rc) for r in batch["rows"] if (rc := row_claims(con, r).get(key))]
        served = many(con, [(rc["claim_id"], ("collection", rc)) for _, rc in wanted])
        batch["envelopes"][key] = {row_id: env for (row_id, _), env in zip(wanted, served)}
    return batch["envelopes"][key].get(row["_row_id"])


def edge_envelope(con, edge: dict, key: str, many):
    """The envelope of `edge`'s group claim under `key`, served for every edge of its batch at once."""
    edges = edge.get("_edges") or [edge]
    cache = edges[0].setdefault("_envelopes", {})
    if key not in cache:
        groups: dict[tuple, set] = {}
        for e in edges:
            groups.setdefault((e["dataset_id"], e["kind"]), set()).add(e["to"]["id"])
        for (dataset_id, kind), ids in sorted(groups.items()):
            prefetch_groups(con, dataset_id, kind, ids)
        wanted = [(id(e), rc) for e in edges if (rc := edge_claims(con, e).get(key))]
        served = many(con, [(rc["claim_id"], ("collection", rc)) for _, rc in wanted])
        cache[key] = {i: env for (i, _), env in zip(wanted, served)}
    return cache[key].get(id(edge))


def row_claims(con, row: dict) -> dict:
    """{claim key: claim record} for one row, rendering its whole batch's claims at once."""
    batch = row["_batch"]
    if batch["claims"] is None:
        unique = {r["_row_id"]: r["_raw"] for r in batch["rows"]}
        batch["claims"] = {}
        for rc in implied_row_claims(con, batch["root"]["release_id"], [unique[i] for i in sorted(unique)]):
            batch["claims"].setdefault(rc["row_id"], {})[rc["claim_key"]] = rc
    return batch["claims"].get(row["_row_id"], {})


def edge_claims(con, edge: dict) -> dict:
    root = edge["_root"]
    key = (edge["dataset_id"], edge["kind"], edge["to"]["id"])
    if key not in root["groups"]:
        root["groups"][key] = {rc["claim_key"]: rc for rc in implied_group_claims(con, root["release_id"], *key)}
    return root["groups"][key]


__all__ = ["rows", "joined", "row_claims", "edge_claims", "row_envelope", "edge_envelope", "CollectionNotFound",
           "MAX_ROWS", "MAX_JOINED"]
