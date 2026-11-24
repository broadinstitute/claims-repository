"""Reading stored DAPPER objects in bulk: many by id in one call, and one
object's lineage (what it references, to a bounded depth, and what references
it). Plain reads: nothing here asserts or mints a claim.

Lineage edges come from two places, both written by every deposit and load:
- the reference index (object_refs, service/refs.py): a document naming
  another object's id in one of its slots (was_derived_from,
  was_generated_by, has_evidence, ...). Each edge is labelled with the slot
  the id sits in, read from the document.
- deposited edge groups (deposit_edges): DAPPER edges such as `used_edges`
  (an Activity used a File), which are not slots of either node.
"""
from __future__ import annotations

import json

from .refs import DAPPER_ID_RE

MAX_BATCH = 500
MAX_DEPTH = 3
MAX_NODES = 500


def _class_of(object_id: str) -> str | None:
    return object_id.split(":", 1)[1].split(".", 1)[0] if DAPPER_ID_RE.match(object_id or "") else None


def _payloads(con, ids) -> dict[str, dict]:
    ids = sorted(set(ids))
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in con.execute(f"SELECT id, payload FROM dapper_objects WHERE id IN ({','.join('?' * len(chunk))})",
                             chunk).fetchall():
            out[r["id"]] = json.loads(r["payload"])
    return out


def present(payload: dict) -> dict:
    """The same presentation GET /v1/objects/{id} uses: id and class_name first, the rest sorted."""
    rest = {k: payload[k] for k in sorted(payload) if k not in ("id", "class_name")}
    return {"id": payload.get("id"), "class_name": payload.get("class_name") or _class_of(payload.get("id") or ""),
            **rest}


def batch_get(con, ids: list) -> dict:
    """{objects: {id: document}, missing: [id]} for up to MAX_BATCH distinct ids, in request order.
    A claim the loaded data implies but nothing has asserted yet is missing: reading it here does not mint it."""
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ValueError("ids must be a list of strings")
    unique = list(dict.fromkeys(ids))
    if not unique:
        raise ValueError("ids must name at least one object")
    if len(unique) > MAX_BATCH:
        raise ValueError(f"at most {MAX_BATCH} distinct ids per call, got {len(unique)}")
    found = _payloads(con, unique)
    return {"objects": {i: present(found[i]) for i in unique if i in found},
            "missing": [i for i in unique if i not in found]}


def _slot_refs(payload: dict) -> list[tuple[str, str]]:
    """(slot, referenced id) for every dapper id in the document, the slot being the top-level key it sits under."""
    out = []

    def walk(slot, value):
        if isinstance(value, str):
            if DAPPER_ID_RE.match(value):
                out.append((slot, value))
        elif isinstance(value, list):
            for v in value:
                walk(slot, v)
        elif isinstance(value, dict):
            for v in value.values():
                walk(slot, v)

    for k, v in payload.items():
        if k != "id":
            walk(k, v)
    return out


def _edge_rows(con, column: str, ids: list[str]) -> list:
    rows = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        rows += con.execute(f"SELECT DISTINCT class_name, subject_id, object_id FROM deposit_edges WHERE {column} IN "
                            f"({','.join('?' * len(chunk))})", chunk).fetchall()
    return rows


def lineage(con, object_id: str, depth: int = MAX_DEPTH, max_nodes: int = MAX_NODES) -> dict | None:
    """The object, what it references (to `depth`, at most MAX_DEPTH) and what references it (depth 1), as
    nodes and edges. None when the object is not stored. `truncated` is true when `max_nodes` cut the graph."""
    if not 1 <= depth <= MAX_DEPTH:
        raise ValueError(f"depth must be between 1 and {MAX_DEPTH}, got {depth}")
    payloads = _payloads(con, [object_id])
    if object_id not in payloads:
        return None
    nodes: dict[str, dict] = {object_id: {"depth": 0}}
    edges: dict[tuple, dict] = {}
    truncated = False

    def add_node(i, d):
        nonlocal truncated
        if i in nodes:
            return True
        if len(nodes) >= max_nodes:
            truncated = True
            return False
        nodes[i] = {"depth": d}
        return True

    def add_edge(src, dst, label, kind):
        edges.setdefault((src, dst, label), {"from": src, "to": dst, "label": label, "kind": kind})

    # Outgoing, breadth first.
    frontier = [object_id]
    for d in range(1, depth + 1):
        if not frontier:
            break
        docs = _payloads(con, [i for i in frontier if i not in payloads])
        payloads.update(docs)
        nxt = []
        for src in frontier:
            for slot, ref in _slot_refs(payloads.get(src) or {}):
                if ref == src:
                    continue
                new = ref not in nodes
                if add_node(ref, d):
                    add_edge(src, ref, slot, "reference")
                    if new:
                        nxt.append(ref)
        for r in _edge_rows(con, "subject_id", frontier):
            new = r["object_id"] not in nodes
            if add_node(r["object_id"], d):
                add_edge(r["subject_id"], r["object_id"], r["class_name"], "edge")
                if new:
                    nxt.append(r["object_id"])
        frontier = nxt

    # Incoming, depth 1.
    referrers = [r["from_id"] for r in con.execute(
        "SELECT from_id FROM object_refs WHERE to_id = ? ORDER BY from_id LIMIT ?", (object_id, max_nodes + 1))]
    payloads.update(_payloads(con, [i for i in referrers if i not in payloads]))
    for src in referrers:
        if not add_node(src, -1) and src not in nodes:
            break
        for slot, ref in _slot_refs(payloads.get(src) or {}):
            if ref == object_id:
                add_edge(src, object_id, slot, "reference")
    for r in _edge_rows(con, "object_id", [object_id]):
        if add_node(r["subject_id"], -1):
            add_edge(r["subject_id"], r["object_id"], r["class_name"], "edge")

    payloads.update(_payloads(con, [i for i in nodes if i not in payloads]))
    out_nodes = []
    for i, n in nodes.items():
        doc = payloads.get(i)
        out_nodes.append({"id": i, "class_name": (doc or {}).get("class_name") or _class_of(i),
                          "name": (doc or {}).get("name"), "stored": doc is not None,
                          "direction": "self" if n["depth"] == 0 else ("incoming" if n["depth"] < 0 else "outgoing"),
                          "depth": abs(n["depth"])})
    out_nodes.sort(key=lambda n: (n["direction"] != "self", n["direction"], n["depth"], n["id"]))
    return {"id": object_id, "depth": depth, "nodes": out_nodes,
            "edges": sorted(edges.values(), key=lambda e: (e["from"], e["label"], e["to"])), "truncated": truncated}
