"""Admin API: collections, claims, tables, API traffic and collection
registration, for the admin console.

JSON at /v1/admin/api/*, consumed by the admin console (/admin in frontend/,
the repository's Next.js app). Every call must carry the console server's
short-lived signed assertion (service/admin_gateway.py); responses are
no-store. The API knows collections only through their registered releases
(service/collections/); no method is special here.

Reads never assert or mint: the claim detail re-renders a collection claim to
show whether it still matches its recorded id, and the example query runs a
query template without serving claims. Tables are read-only and allowlisted;
there is no SQL editor. The object browser (/objects, /deposits, /accounts)
shows every stored object of any class exactly as stored, what it references
and what references it, the deposited edges that touch it and the deposits
that carried it; a ScientificAccount is also served assembled with its
question, claims, propositions, evidence and quoted files.

What writes: the console's sign-in audit (POST /auth-events, purpose
auth_event), and collection registration (POST /registration/jobs, purpose
admin_write), which publishes a release from files on this machine's disk and
audits the job's start and finish in admin_auth_events. Registration routes
answer 403 unless the API process sets CLAIMS_COLLECTION_ROOTS, and every path
they read must resolve inside those roots (service/collections/registration.py).
POST /registration/check also needs admin_write but writes nothing.
Deposit keys (service/api_keys.py) are issued and revoked here too: POST /keys
and POST /keys/{key_id}/revoke, purpose admin_write, audited in
admin_auth_events with the admin's email. A key is returned once, by the
issue response, and is never stored, logged or listed.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import admin_gateway as gateway
from . import api_keys
from . import db
from . import telemetry
from .collections import engine as cengine
from .collections import identity as cidn
from .collections import registration as reg
from .collections import serve as cserve
from .collections.manifest import ManifestError

ROOT = Path(__file__).resolve().parent.parent

router = APIRouter(prefix="/v1/admin/api", include_in_schema=False)

NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _core():
    # service.app imports this module at the end of its own import, so the
    # core is resolved lazily (at request time) rather than at import.
    from . import app as core
    return core


def _default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _json(data, status=200):
    return Response(json.dumps(data, default=_default, allow_nan=False), status_code=status,
                    media_type="application/json", headers=NO_STORE)


def _row(r) -> dict:
    return dict(r) if isinstance(r, dict) else {k: r[k] for k in r.keys()}


def _ts(value):
    """UTC 'YYYY-MM-DDTHH:MM:SSZ' for stored timestamps (TEXT or DATETIME)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    text = str(value)
    return f"{text[:10]}T{text[11:19]}Z" if len(text) >= 19 else text


def _cutoff(hours=0, days=0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours, days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _loads(value):
    return value if isinstance(value, (dict, list)) else json.loads(value)


async def _read(fn, *args):
    """Run a database read off the event loop on its own connection."""
    def run():
        con = db.connect()
        try:
            return fn(con, *args)
        finally:
            con.close()
    return await run_in_threadpool(run)


# --- shared reads -----------------------------------------------------------

# Each mint context (activity) belongs to one collection; several releases of
# that collection may list it, hence DISTINCT.
ACTIVITY_COLLECTION = (
    "SELECT DISTINCT rm.activity_id AS activity_id, r.collection_key AS collection_key "
    "FROM release_mint_contexts rm JOIN collection_releases r ON r.release_id = rm.release_id"
)
UNCOLLECTED = "NOT EXISTS (SELECT 1 FROM result_claims x WHERE x.claim_id = c.claim_id)"


def _release_claims(con, release_id) -> dict:
    r = con.execute(
        "SELECT COUNT(*) AS n, COUNT(o.id) AS minted FROM result_claims rc JOIN release_mint_contexts rm "
        "ON rm.activity_id = rc.activity_id AND rm.release_id = ? LEFT JOIN dapper_objects o ON o.id = rc.claim_id",
        (release_id,)).fetchone()
    return {"mintable": int(r["n"]), "minted": int(r["minted"])}


def _summaries(con, key=None) -> list[dict]:
    """CollectionSummary for every collection (or one), from its current release."""
    out = []
    sql = ("SELECT c.collection_key, c.title, c.description, c.current_release_id, c.updated_at, r.version, "
           "r.published_at, r.git_commit, r.manifest FROM collections c LEFT JOIN collection_releases r "
           "ON r.release_id = c.current_release_id")
    rows = con.execute(sql + (" WHERE c.collection_key = ?" if key else "") + " ORDER BY c.collection_key",
                       (key,) if key else ()).fetchall()
    for c in rows:
        manifest = _loads(c["manifest"])["manifest"] if c["manifest"] is not None else {}
        rid = c["current_release_id"]
        out.append({
            "key": c["collection_key"], "title": c["title"], "description": c["description"],
            "methods": manifest.get("methods", []), "owners": manifest.get("owners", []),
            "currentRelease": None if c["version"] is None else {
                "id": rid, "version": c["version"], "publishedAt": _ts(c["published_at"]), "gitCommit": c["git_commit"]},
            "releases": int(con.execute("SELECT COUNT(*) AS n FROM collection_releases WHERE collection_key = ?",
                                        (c["collection_key"],)).fetchone()["n"]),
            "datasets": int(con.execute("SELECT COUNT(*) AS n FROM release_datasets WHERE release_id = ?",
                                        (rid,)).fetchone()["n"]),
            "claims": _release_claims(con, rid),
            "updatedAt": _ts(c["updated_at"]),
        })
    return out


def _registration_info() -> dict:
    allowed = reg.roots()
    return {"enabled": bool(allowed), "roots": [str(r) for r in allowed]}


def _overview(con) -> dict:
    started = time.perf_counter()
    con.execute("SELECT 1").fetchone()
    db_ms = round((time.perf_counter() - started) * 1000, 1)
    stored = con.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(times_seen), 0) AS s, MAX(COALESCE(last_seen_at, created_at)) AS last_at "
        "FROM dapper_objects WHERE class_name = 'Claim'"
    ).fetchone()
    day = _cutoff(hours=24)
    minted_24h = con.execute(
        "SELECT COUNT(*) AS n FROM dapper_objects WHERE class_name = 'Claim' AND created_at >= ?", (day,)
    ).fetchone()["n"]
    asserted_24h = con.execute(
        "SELECT COUNT(*) AS n FROM dapper_objects WHERE class_name = 'Claim' AND COALESCE(last_seen_at, created_at) >= ?",
        (day,),
    ).fetchone()["n"]
    mintable = con.execute(
        "SELECT COUNT(*) AS n FROM result_claims WHERE activity_id IN (SELECT activity_id FROM release_mint_contexts)"
    ).fetchone()["n"]
    uncollected = con.execute(
        f"SELECT COUNT(*) AS n FROM claims c JOIN dapper_objects o ON o.id = c.claim_id WHERE {UNCOLLECTED}"
    ).fetchone()["n"]
    by_class = [
        {"className": r["class_name"], "count": int(r["n"])}
        for r in con.execute(
            "SELECT class_name, COUNT(*) AS n FROM dapper_objects GROUP BY class_name ORDER BY n DESC, class_name"
        ).fetchall()
    ]
    since = (datetime.now(timezone.utc).date() - timedelta(days=29))
    per_day = {
        str(r["d"])[:10]: int(r["n"])
        for r in con.execute(
            "SELECT DATE(created_at) AS d, COUNT(*) AS n FROM dapper_objects "
            "WHERE class_name = 'Claim' AND created_at >= ? GROUP BY DATE(created_at)",
            (since.strftime("%Y-%m-%d 00:00:00"),),
        ).fetchall()
    }
    timeline = [{"date": (since + timedelta(days=i)).isoformat(),
                 "minted": per_day.get((since + timedelta(days=i)).isoformat(), 0)} for i in range(30)]
    release = con.execute("SELECT * FROM kpn_trait_release WHERE release_key = 'current'").fetchone()
    trait_count = con.execute("SELECT COUNT(*) AS n FROM kpn_traits").fetchone()["n"]
    tele = telemetry.snapshot()
    return {
        "asOf": _ts(datetime.now(timezone.utc)),
        "database": {"backend": "sqlite" if con.dialect.sqlite else "mysql",
                     "name": db.MYSQL_DATABASE if not con.dialect.sqlite else Path(db.DB_PATH).name,
                     "pingMs": db_ms},
        "claims": {
            "mintable": int(mintable), "minted": int(stored["n"]), "uncollected": int(uncollected),
            "assertions": int(stored["s"]), "lastAssertedAt": _ts(stored["last_at"]),
            "minted24h": int(minted_24h), "asserted24h": int(asserted_24h),
        },
        "collections": _summaries(con),
        "objectsByClass": by_class,
        "mintTimeline": timeline,
        "traitRegistry": None if release is None else {
            "version": release["version"], "commit": release["commit_sha"],
            "published": not (release["source_url"] or "").startswith("unpublished:"),
            "traits": int(release["trait_count"]), "mappings": int(release["mapping_count"]),
            "loadedAt": _ts(release["loaded_at"]), "rows": int(trait_count),
        },
        "api": {"startedAt": tele["startedAt"], "uptimeSeconds": tele["uptimeSeconds"],
                "requests": sum(r["count"] for r in tele["routes"]),
                "errors5xx": sum(r["errors5xx"] for r in tele["routes"]),
                "graphql": {k: tele["graphql"][k] for k in ("requests", "ok", "failed", "minted", "reasserted")}},
        "registration": _registration_info(),
    }


# --- collections ------------------------------------------------------------

def _file_facts(con, document: dict):
    """File id -> {filename, sha256, sizeBytes, location} from the release's
    DAPPER document, falling back to the stored File object."""
    files = {f["id"]: f for f in document.get("files", [])}

    def facts(file_id):
        doc = files.get(file_id)
        if doc is None and file_id:
            row = con.execute("SELECT payload FROM dapper_objects WHERE id = ?", (file_id,)).fetchone()
            doc = json.loads(row["payload"]) if row else {}
        doc = doc or {}
        return {"filename": doc.get("filename"), "sha256": doc.get("sha256"), "sizeBytes": doc.get("size_in_bytes"),
                "location": doc.get("location")}
    return facts


def _dimension_views(con, dataset_ids) -> dict:
    out: dict[str, dict] = {d: {} for d in dataset_ids}
    ids = sorted(out)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in con.execute("SELECT dataset_id, dimension, value, label FROM dataset_dimensions WHERE dataset_id IN "
                             f"({','.join('?' * len(chunk))}) ORDER BY dataset_id, dimension", chunk).fetchall():
            out[r["dataset_id"]][r["dimension"]] = {"value": r["value"], "label": r["label"]}
    return out


def _collection(con, key, release) -> dict:
    rel = cserve.release_row(con, key, release)
    record = _loads(rel["manifest"])
    m = record["manifest"]
    document = _loads(rel["document"])
    facts = _file_facts(con, document)
    rid = rel["release_id"]
    summary = _summaries(con, key)
    if not summary:
        raise cserve.CollectionNotFound(f"no collection {key!r}")
    current = summary[0]["currentRelease"]["id"] if summary[0]["currentRelease"] else None
    history = [{"id": r["release_id"], "version": r["version"], "publishedAt": _ts(r["published_at"]),
                "gitCommit": r["git_commit"], "isCurrent": r["release_id"] == current}
               for r in con.execute("SELECT release_id, version, published_at, git_commit FROM collection_releases "
                                    "WHERE collection_key = ? ORDER BY published_at DESC, version DESC", (key,))]

    # Datasets: dimensions, files, row/edge counts, claims, each in one grouped query.
    ids = [r["dataset_id"] for r in con.execute(
        "SELECT dataset_id FROM release_datasets WHERE release_id = ? ORDER BY dataset_id", (rid,)).fetchall()]
    in_release = "dataset_id IN (SELECT dataset_id FROM release_datasets WHERE release_id = ?)"
    dims = _dimension_views(con, ids)
    files: dict[str, list] = {d: [] for d in ids}
    for r in con.execute(f"SELECT dataset_id, role, file_id FROM dataset_files WHERE {in_release} "
                         "ORDER BY dataset_id, role", (rid,)).fetchall():
        files[r["dataset_id"]].append({"role": r["role"], "fileId": r["file_id"], **facts(r["file_id"])})
    counts: dict[str, dict] = {d: {} for d in ids}
    for table in ("result_rows", "result_edges"):
        for r in con.execute(f"SELECT dataset_id, kind, COUNT(*) AS n FROM {table} WHERE {in_release} "
                             "GROUP BY dataset_id, kind", (rid,)).fetchall():
            counts[r["dataset_id"]][r["kind"]] = int(r["n"])
    ds_claims = {r["dataset_id"]: {"mintable": int(r["n"]), "minted": int(r["minted"])} for r in con.execute(
        "SELECT rc.dataset_id, COUNT(*) AS n, COUNT(o.id) AS minted FROM result_claims rc JOIN release_mint_contexts rm "
        "ON rm.activity_id = rc.activity_id AND rm.release_id = ? LEFT JOIN dapper_objects o ON o.id = rc.claim_id "
        "GROUP BY rc.dataset_id", (rid,)).fetchall()}
    datasets = [{"id": d, "dimensions": dims[d], "files": files[d], "counts": counts[d],
                 "claims": ds_claims.get(d, {"mintable": 0, "minted": 0})} for d in ids]

    # Templates: per claim key, counts over this release's mint contexts.
    activities: dict[str, list[str]] = {}
    for r in con.execute("SELECT rm.activity_id, mc.template_file_id FROM release_mint_contexts rm JOIN mint_contexts "
                         "mc ON mc.activity_id = rm.activity_id WHERE rm.release_id = ?", (rid,)).fetchall():
        activities.setdefault(r["template_file_id"], []).append(r["activity_id"])
    by_key: dict[tuple, dict] = {}
    for r in con.execute(
            "SELECT mc.template_file_id AS tf, rc.claim_key, COUNT(*) AS n, COUNT(o.id) AS minted, "
            "COALESCE(SUM(o.times_seen), 0) AS assertions FROM result_claims rc JOIN release_mint_contexts rm "
            "ON rm.activity_id = rc.activity_id AND rm.release_id = ? JOIN mint_contexts mc "
            "ON mc.activity_id = rc.activity_id LEFT JOIN dapper_objects o ON o.id = rc.claim_id "
            "GROUP BY mc.template_file_id, rc.claim_key", (rid,)).fetchall():
        by_key[(r["tf"], r["claim_key"])] = {"mintable": int(r["n"]), "minted": int(r["minted"]),
                                             "assertions": int(r["assertions"])}
    templates = []
    for t in record["templates"]:
        doc, fid = t["doc"], t["file_id"]
        claims = []
        for c in doc["claims"]:
            n = by_key.get((fid, c["key"]), {"mintable": 0, "minted": 0, "assertions": 0})
            example = None
            if n["minted"] and activities.get(fid):
                acts = activities[fid]
                row = con.execute(
                    "SELECT o.payload FROM result_claims rc JOIN dapper_objects o ON o.id = rc.claim_id "
                    f"WHERE rc.activity_id IN ({','.join('?' * len(acts))}) AND rc.claim_key = ? "
                    "ORDER BY COALESCE(o.last_seen_at, o.created_at) DESC, o.id LIMIT 1", [*acts, c["key"]]).fetchone()
                if row is not None:
                    payload = json.loads(row["payload"])
                    example = {"claimId": payload.get("id"), "statement": payload.get("statement")}
            p = c["proposition"]
            claims.append({"key": c["key"], "statement": c["statement"],
                           "proposition": {"statement": p["statement"], "subject": p["subject"],
                                           "relation": p["relation"], "object": p["object"], "kind": p["kind"]},
                           "classify": c.get("classify"), "scores": c.get("scores", []), **n, "example": example})
        templates.append({"name": doc["template"], "version": doc["version"], "for": doc["for"],
                          "groupBy": doc.get("group_by"), "fileId": fid, "path": t["path"],
                          "sha256": facts(fid)["sha256"], "claims": claims})

    queries = []
    for q in cserve.list_queries(con, key, rid):
        path = record["queries"][q["name"]]["path"]
        queries.append({"name": q["name"], "description": q["description"], "params": q["params"],
                        "plan": q["plan"], "example": q["example"], "fileId": q["file_id"], "path": path,
                        "sha256": facts(q["file_id"])["sha256"]})
    manifest_id = (document.get("result_collections") or [{}])[0].get("has_manifest_file")
    sources = [("manifest", key, "collection.yaml", manifest_id)] \
        + [("template", t["doc"]["template"], t["path"], t["file_id"]) for t in record["templates"]] \
        + [("query", name, q["path"], q["file_id"]) for name, q in sorted(record["queries"].items())]
    source_files = []
    for kind, name, path, fid in sources:
        f = facts(fid)
        source_files.append({"kind": kind, "name": name, "path": path, "fileId": fid, "sha256": f["sha256"],
                             "sizeBytes": f["sizeBytes"]})
    return {
        **summary[0],
        "dapperProfile": m["dapper_profile"],
        "release": {"id": rid, "version": rel["version"], "publishedAt": _ts(rel["published_at"]),
                    "gitCommit": rel["git_commit"], "isCurrent": rid == current},
        "releaseHistory": history,
        **reg.manifest_facets(m),
        "datasets": datasets,
        "templates": templates,
        "queries": queries,
        "sourceFiles": source_files,
        "dapperDocumentUrl": f"{_core().PUBLIC_BASE}/v1/collections/{key}/releases/{rel['version']}/document",
        "manifest": m,
    }


def _query_example(con, key, name, release) -> dict:
    rel = cserve.release_row(con, key, release)
    queries = _loads(rel["manifest"])["queries"]
    if name not in queries:
        raise cserve.CollectionNotFound(f"collection {key!r} has no query {name!r}")
    # envelope=None: rows only; nothing is served, minted or asserted.
    return cserve.run_query(con, key, name, queries[name]["doc"]["example"], release=rel["release_id"],
                            envelope=None)


# --- claims -----------------------------------------------------------------

CLAIM_SORTS = {
    "recent": ("last_asserted_at", "u.last_asserted_at DESC, u.claim_id"),
    "asserted": ("times_seen", "u.times_seen DESC, u.claim_id"),
    "minted": ("minted_at", "u.minted_at DESC, u.claim_id"),
    "subject": ("subject", "COALESCE(u.subject, '~') ASC, u.claim_id"),
}
# A group claim's subject is its group's label (the edges' to_value).
GROUP_LABEL = ("(SELECT e.to_value FROM result_edges e WHERE e.dataset_id = rc.dataset_id "
               "AND e.kind = rc.edge_kind AND e.to_id = rc.group_id LIMIT 1)")
COLLECTED_COLUMNS = {
    "claim_id": "rc.claim_id", "collection": "ac.collection_key", "claim_key": "rc.claim_key",
    "kind": "COALESCE(rr.kind, rc.edge_kind)",
    "subject": f"CASE WHEN rc.row_id IS NULL THEN {GROUP_LABEL} ELSE rr.subject_value END",
    "dataset_id": "rc.dataset_id", "times_seen": "o.times_seen", "minted_at": "o.created_at",
    "last_asserted_at": "COALESCE(o.last_seen_at, o.created_at)",
}
UNCOLLECTED_COLUMNS = {**{k: "NULL" for k in COLLECTED_COLUMNS}, "claim_id": "c.claim_id", "times_seen": "o.times_seen",
                       "minted_at": "o.created_at", "last_asserted_at": "COALESCE(o.last_seen_at, o.created_at)"}


def _claim_parts(con, columns, f, *, ids=None) -> tuple[str, list]:
    """The UNION of collection claims and uncollected claims matching the
    filters `f` (or exactly `ids`), selecting only `columns` and joining only
    what those columns and filters need: 92k rows stay cheap to count and
    sort, and the page's details are read for its rows alone."""
    parts, params = [], []
    marks = ",".join("?" * len(ids)) if ids else ""
    if f["origin"] != "uncollected":
        need_row = ids is not None or "subject" in columns or "kind" in columns
        need_obj = ids is not None or f["status"] or {"times_seen", "minted_at", "last_asserted_at"} & set(columns)
        where, sub = [], []
        if ids is not None:
            where.append(f"rc.claim_id IN ({marks})")
            sub += ids
        else:
            for col, value in (("ac.collection_key", f["collection"]), ("rc.claim_key", f["claim_key"]),
                               ("rc.dataset_id", f["dataset"])):
                if value:
                    where.append(f"{col} = ?")
                    sub.append(value)
            if f["status"]:
                where.append("o.id IS NOT NULL" if f["status"] == "minted" else "o.id IS NULL")
            if f["q"]:
                # Rows first, then their claims: cheaper than matching every claim's joined subject.
                where.append(f"(rc.row_id IN (SELECT row_id FROM result_rows WHERE {con.dialect.like_ci('subject_value')}) "
                             "OR (rc.row_id IS NULL AND (rc.dataset_id, rc.edge_kind, rc.group_id) IN (SELECT "
                             f"dataset_id, kind, to_id FROM result_edges WHERE {con.dialect.like_ci('to_value')})) "
                             f"OR {con.dialect.like_ci('rc.claim_id')})")
                sub += [f["pattern"]] * 3
        parts.append(
            f"SELECT {', '.join(f'{COLLECTED_COLUMNS[c]} AS {c}' for c in columns)} "
            f"FROM result_claims rc JOIN ({ACTIVITY_COLLECTION}) ac ON ac.activity_id = rc.activity_id"
            + (" LEFT JOIN result_rows rr ON rr.row_id = rc.row_id" if need_row else "")
            + (" LEFT JOIN dapper_objects o ON o.id = rc.claim_id" if need_obj else "")
            + (f" WHERE {' AND '.join(where)}" if where else ""))
        params += sub
    only_collected = f["collection"] or f["claim_key"] or f["dataset"] or f["origin"] == "collection" \
        or f["status"] == "unminted"
    if not only_collected:
        where, sub = [UNCOLLECTED], []
        if ids is not None:
            where.append(f"c.claim_id IN ({marks})")
            sub += ids
        elif f["q"]:
            where.append(con.dialect.like_ci("c.claim_id"))
            sub.append(f["pattern"])
        parts.append(
            f"SELECT {', '.join(f'{UNCOLLECTED_COLUMNS[c]} AS {c}' for c in columns)} "
            f"FROM claims c JOIN dapper_objects o ON o.id = c.claim_id WHERE {' AND '.join(where)}")
        params += sub
    return " UNION ALL ".join(parts), params


def _claims(con, q, collection, claim_key, dataset, origin, status, sort, page, page_size) -> dict:
    f = {"q": q, "pattern": f"%{db.like_escape(q)}%" if q else None, "collection": collection,
         "claim_key": claim_key, "dataset": dataset, "origin": origin, "status": status}
    union, params = _claim_parts(con, ["claim_id"], f)
    total = con.execute(f"SELECT COUNT(*) AS n FROM ({union}) u", params).fetchone()["n"]
    key, order = CLAIM_SORTS[sort]
    union, params = _claim_parts(con, ["claim_id", key], f)
    ids = [r["claim_id"] for r in con.execute(
        f"SELECT u.claim_id FROM ({union}) u ORDER BY {order} LIMIT ? OFFSET ?",
        params + [page_size, (page - 1) * page_size]).fetchall()]
    found = {}
    if ids:
        union, params = _claim_parts(con, list(COLLECTED_COLUMNS), f, ids=ids)
        found = {r["claim_id"]: _row(r) for r in con.execute(f"SELECT * FROM ({union}) u", params).fetchall()}
    rows = [found[i] for i in ids if i in found]
    dims = _dimension_views(con, {r["dataset_id"] for r in rows if r["dataset_id"]})
    out = [{
        "claimId": r["claim_id"], "collection": r["collection"], "claimKey": r["claim_key"], "kind": r["kind"],
        "subject": r["subject"], "datasetId": r["dataset_id"],
        "dimensions": dims.get(r["dataset_id"]) if r["dataset_id"] else None,
        "minted": r["times_seen"] is not None, "timesAsserted": int(r["times_seen"] or 0),
        "mintedAt": _ts(r["minted_at"]), "lastAssertedAt": _ts(r["last_asserted_at"]),
    } for r in rows]
    return {"total": int(total), "page": page, "pageSize": page_size, "rows": out}


def _claim_source(con, rc) -> dict:
    """Where a collection claim comes from: template, releases, dataset, row or group."""
    ctx_row = con.execute("SELECT template_file_id, context FROM mint_contexts WHERE activity_id = ?",
                          (rc["activity_id"],)).fetchone()
    template = None
    if ctx_row is not None:
        t = _loads(ctx_row["context"])["template"]
        template = {"name": t["template"], "version": t["version"], "fileId": ctx_row["template_file_id"]}
    releases = con.execute(
        "SELECT r.collection_key, r.version FROM release_mint_contexts rm JOIN collection_releases r "
        "ON r.release_id = rm.release_id WHERE rm.activity_id = ? ORDER BY r.published_at, r.version",
        (rc["activity_id"],)).fetchall()
    row = group = None
    if rc["row_id"] is not None:
        r = con.execute("SELECT * FROM result_rows WHERE row_id = ?", (rc["row_id"],)).fetchone()
        if r is not None:
            row = {"rowId": r["row_id"], "kind": r["kind"],
                   "subject": {"id": r["subject_id"], "value": r["subject_value"], "label": r["subject_label"]},
                   "object": {"id": r["object_id"], "label": r["object_label"]},
                   "columns": json.loads(r["row_json"]), "fileId": r["file_id"]}
    else:
        g = con.execute("SELECT MIN(to_value) AS label, COUNT(*) AS n FROM result_edges WHERE dataset_id = ? "
                        "AND kind = ? AND to_id = ?", (rc["dataset_id"], rc["edge_kind"], rc["group_id"])).fetchone()
        group = {"edgeKind": rc["edge_kind"], "id": rc["group_id"], "label": g["label"], "count": int(g["n"])}
    return {
        "collection": releases[0]["collection_key"] if releases else None,
        "claimKey": rc["claim_key"], "template": template, "releases": [r["version"] for r in releases],
        "dataset": {"id": rc["dataset_id"], "dimensions": _dimension_views(con, [rc["dataset_id"]])[rc["dataset_id"]]},
        "row": row, "group": group,
    }


def _claim_detail(con, claim_id) -> dict | None:
    core = _core()
    stored = con.execute(
        "SELECT payload, class_name, identity_profile, times_seen, created_at, last_seen_at FROM dapper_objects WHERE id = ?",
        (claim_id,),
    ).fetchone()
    if stored is not None and stored["class_name"] != "Claim":
        return None
    rc = con.execute("SELECT * FROM result_claims WHERE claim_id = ?", (claim_id,)).fetchone()
    if stored is None and rc is None:
        return None
    verification = {"collectionBacked": rc is not None, "renders": None, "storedMatches": None, "error": None}
    rendered = None
    if rc is not None:
        # serve.render_claim is pure: inspecting a claim never mints or asserts it.
        try:
            rendered = cserve.render_claim(con, rc)
            verification["renders"] = True
        except (cserve.ClaimRenderError, cengine.TemplateError, cidn.MintError) as exc:
            verification.update(renders=False, error=str(exc))
    stored_doc = json.loads(stored["payload"]) if stored is not None else None
    if stored_doc is not None and rendered is not None:
        verification["storedMatches"] = stored_doc == rendered
        if not verification["storedMatches"]:
            verification["error"] = "the stored document differs from the claim its row re-renders"
    claim = stored_doc or rendered or {}
    source = _claim_source(con, rc) if rc is not None else None
    prop_id = claim.get("proposition") if isinstance(claim.get("proposition"), str) else None
    proposition = core._fetch_payload(con, prop_id) if prop_id else None
    score_ids = core._id_list(claim.get("has_score"))
    score_docs = core._fetch_payloads(con, score_ids)
    evidence_ids = core._id_list(claim.get("has_evidence"))
    evidence_docs = core._fetch_payloads(con, evidence_ids)
    registered = con.execute("SELECT method FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
    assessing = 0
    if prop_id:
        assessing = con.execute("SELECT COUNT(*) AS n FROM claims WHERE proposition_id = ?", (prop_id,)).fetchone()["n"]
    collection = source["collection"] if source else None
    return {
        "claimId": claim_id,
        "minted": stored is not None,
        "identityProfile": stored["identity_profile"] if stored is not None else cidn.PROFILE,
        "timesAsserted": int(stored["times_seen"]) if stored is not None else 0,
        "mintedAt": _ts(stored["created_at"]) if stored is not None else None,
        "lastAssertedAt": _ts(stored["last_seen_at"] or stored["created_at"]) if stored is not None else None,
        # Serving records a collection claim's method as its collection key.
        "method": registered["method"] if registered is not None else collection,
        "collection": collection,
        "claimKey": rc["claim_key"] if rc is not None else None,
        "triple": core._triple_of(proposition) if proposition else None,
        "statement": claim.get("statement"),
        "verification": verification,
        "claim": claim or None,
        "proposition": proposition,
        "scores": [score_docs.get(s) or {"id": s, "missing": True} for s in score_ids],
        "evidence": [evidence_docs.get(e) or {"id": e, "missing": True} for e in evidence_ids],
        "source": source,
        "claimsAssessingProposition": int(assessing),
        "resolverUrl": core._resolver_url(claim_id),
    }


# --- tables -------------------------------------------------------------------

def _tables_known() -> list[str]:
    import re
    return re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", db.SQLITE_DDL)


# Never served by the table browser. A deposit key's hash is not the key, but it is what authenticates one.
HIDDEN_COLUMNS: dict[str, set[str]] = {"api_keys": {"key_sha256"}}
_table_cache: dict = {"at": 0.0, "data": None}
TABLE_CACHE_SECONDS = 30


def _columns(con, table) -> list[dict]:
    if con.dialect.sqlite:
        return [{"name": r["name"], "type": r["type"], "pk": int(r["pk"])}
                for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    rows = con.execute(
        "SELECT COLUMN_NAME AS name, COLUMN_TYPE AS type, COLUMN_KEY AS ckey, ORDINAL_POSITION AS pos "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
        (table,),
    ).fetchall()
    pk = [r["name"] for r in con.execute(
        "SELECT COLUMN_NAME AS name FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ? AND CONSTRAINT_NAME = 'PRIMARY' ORDER BY ORDINAL_POSITION",
        (table,),
    ).fetchall()]
    return [{"name": r["name"], "type": r["type"], "pk": pk.index(r["name"]) + 1 if r["name"] in pk else 0}
            for r in rows]


def _tables(con, fresh=False) -> dict:
    now = time.time()
    if not fresh and _table_cache["data"] is not None and now - _table_cache["at"] < TABLE_CACHE_SECONDS:
        return _table_cache["data"]
    sizes = {}
    if not con.dialect.sqlite:
        sizes = {r["name"]: r for r in con.execute(
            "SELECT TABLE_NAME AS name, DATA_LENGTH AS data_bytes, INDEX_LENGTH AS index_bytes, TABLE_ROWS AS est "
            "FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
        ).fetchall()}
    tables = []
    for name in _tables_known() + ([] if con.dialect.sqlite else ["schema_migrations"]):
        if not con.dialect.sqlite and name not in sizes:
            tables.append({"name": name, "available": False})
            continue
        started = time.perf_counter()
        count = con.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"]
        size = sizes.get(name)
        cols = _columns(con, name)
        tables.append({
            "name": name, "available": True, "rows": int(count),
            "countMs": round((time.perf_counter() - started) * 1000, 1),
            "columns": len(cols), "primaryKey": [c["name"] for c in sorted(cols, key=lambda c: c["pk"]) if c["pk"]],
            "dataBytes": int(size["data_bytes"]) if size else None,
            "indexBytes": int(size["index_bytes"]) if size else None,
        })
    migrations = []
    if not con.dialect.sqlite:
        migrations = [{"version": r["version"], "appliedAt": _ts(r["applied_at"])}
                      for r in con.execute("SELECT version, applied_at FROM schema_migrations ORDER BY version").fetchall()]
    expected = sorted(p.name for p in (ROOT / "schema" / "migrations").glob("*.sql"))
    applied = {m["version"] for m in migrations}
    data = {"asOf": _ts(datetime.now(timezone.utc)), "backend": "sqlite" if con.dialect.sqlite else "mysql",
            "tables": tables, "migrations": migrations,
            "pendingMigrations": [] if con.dialect.sqlite else [v for v in expected if v not in applied]}
    _table_cache.update(at=now, data=data)
    return data


MAX_CELL = 2000
MAX_TABLE_OFFSET = 1_000_000


def _table_rows(con, table, page, page_size, column, op, value) -> dict:
    allowed = set(_tables_known()) | ({"schema_migrations"} if not con.dialect.sqlite else set())
    if table not in allowed:
        raise HTTPException(status_code=404, detail="Unknown table.")
    cols = _columns(con, table)
    if not cols:
        raise HTTPException(status_code=404, detail="Table not available on this database.")
    hidden = HIDDEN_COLUMNS.get(table, set())
    visible = [c for c in cols if c["name"] not in hidden]
    names = {c["name"] for c in visible}
    order = [c["name"] for c in sorted(cols, key=lambda c: c["pk"]) if c["pk"]] or [visible[0]["name"]]
    where, params = "", []
    if column and value not in (None, ""):
        if column not in names:
            raise HTTPException(status_code=400, detail="Unknown column.")
        if op == "equals":
            where, params = f"WHERE {column} = ?", [value]
        else:
            where, params = f"WHERE {con.dialect.like_ci(column)}", [f"%{db.like_escape(value)}%"]
    offset = (page - 1) * page_size
    if offset > MAX_TABLE_OFFSET:
        raise HTTPException(status_code=400, detail="Page too deep; filter instead.")
    select = ", ".join(c["name"] for c in visible)
    total = con.execute(f"SELECT COUNT(*) AS n FROM {table} {where}", params).fetchone()["n"]
    rows = con.execute(
        f"SELECT {select} FROM {table} {where} ORDER BY {', '.join(order)} LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()
    out = []
    for r in rows:
        r = _row(r)
        cells = {}
        for k, v in r.items():
            if isinstance(v, (bytes, bytearray)):
                v = v.decode("utf-8", errors="replace")
            if isinstance(v, str) and len(v) > MAX_CELL:
                cells[k] = {"text": v[:MAX_CELL], "truncated": len(v)}
            else:
                cells[k] = v
        out.append(cells)
    return {"table": table, "columns": visible, "orderBy": order, "total": int(total),
            "page": page, "pageSize": page_size, "rows": out}


# --- activity -------------------------------------------------------------------

def _activity(con) -> dict:
    core = _core()
    recent = [_row(r) for r in con.execute(
        "SELECT id, payload, times_seen, created_at, COALESCE(last_seen_at, created_at) AS last_at "
        "FROM dapper_objects WHERE class_name = 'Claim' ORDER BY last_at DESC, id LIMIT 25"
    ).fetchall()]
    docs = {r["id"]: json.loads(r["payload"]) for r in recent}
    props = core._fetch_payloads(con, sorted({d.get("proposition") for d in docs.values() if isinstance(d.get("proposition"), str)}))
    assertions = []
    for r in recent:
        prop = props.get(docs[r["id"]].get("proposition"))
        assertions.append({"claimId": r["id"], "timesAsserted": int(r["times_seen"]), "mintedAt": _ts(r["created_at"]),
                           "lastAssertedAt": _ts(r["last_at"]), "triple": core._triple_of(prop) if prop else None})
    events = [{"at": _ts(r["at"]), "event": r["event"], "email": r["email"], "detail": r["detail"]}
              for r in con.execute("SELECT at, event, email, detail FROM admin_auth_events ORDER BY id DESC LIMIT 50").fetchall()]
    return {"asOf": _ts(datetime.now(timezone.utc)), "telemetry": telemetry.snapshot(),
            "recentAssertions": assertions, "authEvents": events,
            "approvedAdmins": sorted(gateway.ADMIN_EMAILS)}



# --- objects, deposits and scientific accounts ------------------------------------

# Every stored object is a row of dapper_objects, whatever its class: this is the console's view of what the
# repository holds, as stored. Reads only; nothing here mints, asserts or changes times_seen.
LABEL_SLOTS = ("title", "text", "context", "snippet", "value_text", "filename")
REFERENCE_LIMIT, REFERENCED_BY_LIMIT, EDGE_LIMIT = 300, 50, 200
ACCOUNT_CLASS = "ScientificAccount"


def _object_label(doc) -> str | None:
    """One readable line for any class: the public label, then the slots it does not look at."""
    if not isinstance(doc, dict):
        return None
    label = _core()._label(doc)
    if isinstance(label, str) and label:
        return label
    for slot in LABEL_SLOTS:
        if isinstance(doc.get(slot), str) and doc[slot]:
            return doc[slot]
    person = " ".join(doc[s] for s in ("given_name", "family_name") if isinstance(doc.get(s), str) and doc[s])
    return person or None


def _class_name(object_id, doc=None) -> str | None:
    """A document's class, or the class its id names when the object is not stored."""
    named = (doc or {}).get("class_name") if isinstance(doc, dict) else None
    if named:
        return named
    head = object_id.split(":", 1)[-1] if isinstance(object_id, str) else ""
    return head.split(".", 1)[0] if "." in head else None


def _references_in(value, slot=None, found=None) -> list[tuple[str, str]]:
    """(top-level slot, dapper id) for every dapper id a document mentions, in document order, once each."""
    from .refs import DAPPER_ID_RE
    found = [] if found is None else found
    if isinstance(value, str):
        if slot is not None and slot != "id" and DAPPER_ID_RE.match(value) and (slot, value) not in found:
            found.append((slot, value))
    elif isinstance(value, list):
        for item in value:
            _references_in(item, slot, found)
    elif isinstance(value, dict):
        for key, item in value.items():
            _references_in(item, slot if slot is not None else key, found)
    return found


def _summaries_of(con, ids) -> dict:
    """{id: {id, className, label, stored}} for ids, stored or not, in one read."""
    ids = list(dict.fromkeys(i for i in ids if isinstance(i, str)))
    docs = _core()._fetch_payloads(con, ids)
    return {i: {"id": i, "className": _class_name(i, docs.get(i)), "label": _object_label(docs.get(i)),
                "stored": i in docs} for i in ids}


def _deposits_of(con, object_id) -> list[dict]:
    rows = con.execute(
        "SELECT d.deposit_id, d.received_at, d.object_count, d.key_id, k.name, o.status FROM deposit_objects o "
        "JOIN deposits d ON d.deposit_id = o.deposit_id JOIN api_keys k ON k.key_id = d.key_id "
        "WHERE o.object_id = ? ORDER BY d.received_at DESC, d.deposit_id DESC LIMIT 50", (object_id,)).fetchall()
    return [{"depositId": r["deposit_id"], "receivedAt": _ts(r["received_at"]), "objectCount": int(r["object_count"]),
             "depositor": {"keyId": r["key_id"], "name": r["name"]}, "status": r["status"]} for r in rows]


def _edges_of(con, object_ids) -> list[dict]:
    """Deposited edges (prov:used, prov:wasDerivedFrom, ...) that start or end at any of the ids. An edge is not
    an object, so this is the only place the console shows it. The same edge sent by several deposits is one row."""
    ids = list(dict.fromkeys(object_ids))
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    seen, out = {}, []
    for column in ("subject_id", "object_id"):
        for r in con.execute(
                f"SELECT deposit_id, seq, class_name, subject_id, object_id, payload FROM deposit_edges "
                f"WHERE {column} IN ({marks}) ORDER BY deposit_id, seq LIMIT {EDGE_LIMIT}", ids).fetchall():
            payload = _loads(r["payload"])
            key = (r["class_name"], r["subject_id"], r["object_id"], json.dumps(payload, sort_keys=True))
            if key in seen:
                if r["deposit_id"] not in seen[key]["depositIds"]:
                    seen[key]["depositIds"].append(r["deposit_id"])
                continue
            seen[key] = {"className": r["class_name"], "subject": r["subject_id"], "object": r["object_id"],
                         "predicate": payload.get("predicate") if isinstance(payload, dict) else None,
                         "document": payload, "depositIds": [r["deposit_id"]]}
            out.append(seen[key])
    return out


def _object_classes(con) -> dict:
    classes = [{"className": r["class_name"], "count": int(r["n"])} for r in con.execute(
        "SELECT class_name, COUNT(*) AS n FROM dapper_objects GROUP BY class_name ORDER BY class_name").fetchall()]
    edges = [{"className": r["class_name"], "count": int(r["n"])} for r in con.execute(
        "SELECT class_name, COUNT(*) AS n FROM deposit_edges GROUP BY class_name ORDER BY class_name").fetchall()]
    return {"total": sum(c["count"] for c in classes), "classes": classes, "edgeClasses": edges,
            "deposits": int(con.execute("SELECT COUNT(*) AS n FROM deposits").fetchone()["n"])}


def _objects(con, class_name, q, references, deposit, page, page_size) -> dict:
    from .refs import DAPPER_ID_RE, MAX_ID_LENGTH
    core = _core()
    if q and DAPPER_ID_RE.match(q) and len(q) <= MAX_ID_LENGTH:
        # A pasted id is looked up as an id, not searched for as text.
        row = con.execute("SELECT id FROM dapper_objects WHERE id = ?", (q,)).fetchone()
        found = {"total": 1 if row else 0, "objects": [{"id": q}] if row else []}
    else:
        found = core._object_search(con, class_name=class_name, q=q, references=references, deposit=deposit,
                                    limit=page_size, offset=(page - 1) * page_size)
    ids = [o["id"] for o in found["objects"]]
    rows = {}
    if ids:
        marks = ",".join("?" * len(ids))
        rows = {r["id"]: r for r in con.execute(
            f"SELECT id, class_name, identity_profile, payload, times_seen, created_at, last_seen_at FROM dapper_objects "
            f"WHERE id IN ({marks})", ids).fetchall()}
    out = []
    for i in ids:
        r = rows.get(i)
        if r is None:
            continue
        doc = _loads(r["payload"])
        out.append({"id": i, "className": r["class_name"], "label": _object_label(doc),
                    "identityProfile": r["identity_profile"], "timesSeen": int(r["times_seen"]),
                    "createdAt": _ts(r["created_at"]), "lastSeenAt": _ts(r["last_seen_at"] or r["created_at"]),
                    "slots": sorted(k for k in doc if k not in ("id", "class_name")) if isinstance(doc, dict) else []})
    return {"total": int(found["total"]), "page": page, "pageSize": page_size, "rows": out}


def _object_detail(con, object_id) -> dict | None:
    core = _core()
    row = con.execute(
        "SELECT class_name, identity_profile, payload, times_seen, created_at, last_seen_at FROM dapper_objects WHERE id = ?",
        (object_id,)).fetchone()
    doc = _loads(row["payload"]) if row is not None else None
    referenced_total = int(con.execute("SELECT COUNT(*) AS n FROM object_refs WHERE to_id = ?", (object_id,)).fetchone()["n"])
    edges = _edges_of(con, [object_id])
    if row is None and not referenced_total and not edges:
        return None
    referrers = [r["from_id"] for r in con.execute(
        f"SELECT from_id FROM object_refs WHERE to_id = ? ORDER BY from_id LIMIT {REFERENCED_BY_LIMIT}", (object_id,)).fetchall()]
    references = _references_in(doc)[:REFERENCE_LIMIT] if doc is not None else []
    known = _summaries_of(con, [i for _, i in references] + referrers
                          + [e[k] for e in edges for k in ("subject", "object")])
    proposition = doc if row is not None and row["class_name"] == "Proposition" else None
    return {
        "id": object_id, "stored": row is not None,
        "className": row["class_name"] if row is not None else _class_name(object_id),
        "label": _object_label(doc),
        "identityProfile": row["identity_profile"] if row is not None else None,
        "timesSeen": int(row["times_seen"]) if row is not None else 0,
        "createdAt": _ts(row["created_at"]) if row is not None else None,
        "lastSeenAt": _ts(row["last_seen_at"] or row["created_at"]) if row is not None else None,
        "document": doc,
        "triple": core._triple_of(proposition) if proposition else None,
        "references": [{"slot": slot, **known[i]} for slot, i in references],
        "referencedBy": {"total": referenced_total, "objects": [known[i] for i in referrers]},
        "edges": [{**e, "subjectSummary": known.get(e["subject"]), "objectSummary": known.get(e["object"])} for e in edges],
        "deposits": _deposits_of(con, object_id),
        "resolverUrl": core._resolver_url(object_id) if row is not None else None,
    }


def _deposit_list(con, page, page_size) -> dict:
    found = api_keys.deposits(con, limit=page_size, offset=(page - 1) * page_size)
    return {"total": int(found["total"]), "page": page, "pageSize": page_size,
            "rows": [{"depositId": d["deposit_id"], "receivedAt": _ts(d["received_at"]), "objectCount": int(d["object_count"]),
                      "depositor": {"keyId": d["depositor"]["key_id"], "name": d["depositor"]["name"]}} for d in found["deposits"]]}


def _docs(con, ids) -> dict:
    return _core()._fetch_payloads(con, list(dict.fromkeys(i for i in ids if isinstance(i, str))))


def _accounts(con, page, page_size) -> dict:
    core = _core()
    total = con.execute("SELECT COUNT(*) AS n FROM dapper_objects WHERE class_name = ?", (ACCOUNT_CLASS,)).fetchone()["n"]
    rows = con.execute(
        "SELECT id, payload, times_seen, created_at FROM dapper_objects WHERE class_name = ? "
        "ORDER BY created_at DESC, id LIMIT ? OFFSET ?", (ACCOUNT_CLASS, page_size, (page - 1) * page_size)).fetchall()
    docs = {r["id"]: _loads(r["payload"]) for r in rows}
    linked = _docs(con, [d.get("question") for d in docs.values()]
                   + [a for d in docs.values() for a in core._id_list(d.get("was_attributed_to"))])
    out = []
    for r in rows:
        doc = docs[r["id"]]
        deposits = _deposits_of(con, r["id"])
        out.append({
            "id": r["id"], "question": _object_label(linked.get(doc.get("question"))),
            "claims": len(core._id_list(doc.get("component_claims"))),
            "attributedTo": [_object_label(linked.get(a)) or a for a in core._id_list(doc.get("was_attributed_to"))],
            "storedAt": _ts(r["created_at"]), "timesSeen": int(r["times_seen"]),
            "deposits": len(deposits), "lastDeposit": deposits[0] if deposits else None,
        })
    return {"total": int(total), "page": page, "pageSize": page_size, "rows": out}


def _account(con, account_id) -> dict | None:
    """One ScientificAccount with everything it points at, each object exactly as stored: its question, every
    component claim with its proposition and evidence, each evidence item's publication and located quotes, the
    files those quotes address and what they derive from, and the deposits that carried it."""
    core = _core()
    row = con.execute("SELECT class_name, payload, times_seen, created_at FROM dapper_objects WHERE id = ?",
                      (account_id,)).fetchone()
    if row is None or row["class_name"] != ACCOUNT_CLASS:
        return None
    account = _loads(row["payload"])
    claim_ids = core._id_list(account.get("component_claims"))
    claims = _docs(con, claim_ids)
    evidence_ids = [e for c in claims.values() for e in core._id_list(c.get("has_evidence"))]
    evidence = _docs(con, evidence_ids)
    locator_ids = [v for e in evidence.values() for v in core._id_list(e.get("value_locators"))]
    locators = _docs(con, locator_ids)
    file_ids = [v.get("file") for v in locators.values() if isinstance(v.get("file"), str)]
    edges = _edges_of(con, list(dict.fromkeys(file_ids)))
    derived = {}
    for e in edges:
        if e["predicate"] == "prov:wasDerivedFrom" and e["subject"] in file_ids:
            derived.setdefault(e["subject"], []).append(e["object"])
    head_ids = [account.get("question"), account.get("was_generated_by")] + core._id_list(account.get("was_attributed_to"))
    others = _docs(con, head_ids + [c.get("proposition") for c in claims.values()]
                   + [e.get("reference") for e in evidence.values()] + file_ids + [f for fs in derived.values() for f in fs])
    wanted = [i for i in head_ids + claim_ids + evidence_ids + locator_ids + file_ids if isinstance(i, str)]
    stored = {**claims, **evidence, **locators, **others}

    def node(object_id):
        return None if not isinstance(object_id, str) else {"id": object_id, "document": stored.get(object_id)}

    def claim_view(claim_id):
        claim = claims.get(claim_id) or {}
        proposition = others.get(claim.get("proposition")) if isinstance(claim.get("proposition"), str) else None
        items = []
        for evidence_id in core._id_list(claim.get("has_evidence")):
            item = evidence.get(evidence_id) or {}
            quotes = []
            for locator_id in core._id_list(item.get("value_locators")):
                locator = locators.get(locator_id) or {}
                file_id = locator.get("file") if isinstance(locator.get("file"), str) else None
                quotes.append({"id": locator_id, "document": locators.get(locator_id), "file": node(file_id),
                               "derivedFrom": [node(f) for f in derived.get(file_id, [])]})
            items.append({"id": evidence_id, "document": evidence.get(evidence_id),
                          "publication": node(item.get("reference")), "quotes": quotes})
        return {"id": claim_id, "document": claims.get(claim_id), "proposition": node(claim.get("proposition")),
                "triple": core._triple_of(proposition) if proposition else None, "evidence": items}

    classes: dict = {}
    for object_id in dict.fromkeys([account_id, *stored]):
        name = _class_name(object_id, stored.get(object_id)) or "Unknown"
        classes[name] = classes.get(name, 0) + 1
    return {
        "id": account_id, "document": account, "storedAt": _ts(row["created_at"]), "timesSeen": int(row["times_seen"]),
        "question": node(account.get("question")), "activity": node(account.get("was_generated_by")),
        "attributedTo": [node(a) for a in core._id_list(account.get("was_attributed_to"))],
        "claims": [claim_view(c) for c in claim_ids],
        "deposits": _deposits_of(con, account_id), "edges": edges,
        "objectsByClass": [{"className": k, "count": v} for k, v in sorted(classes.items())],
        "missing": sorted(i for i in dict.fromkeys(wanted) if i not in stored),
        "resolverUrl": core._resolver_url(account_id),
    }



# --- API routes -----------------------------------------------------------------

@router.get("/me")
def me(admin: dict = Depends(gateway.require_admin)):
    return _json(admin)


@router.get("/overview")
async def overview(_: dict = Depends(gateway.require_admin)):
    return _json(await _read(_overview))


@router.get("/collections")
async def collections(_: dict = Depends(gateway.require_admin)):
    return _json({"collections": await _read(_summaries)})


def _not_found(fn, *args):
    def run(con):
        try:
            return fn(con, *args)
        except cserve.CollectionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ManifestError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return run


@router.get("/collections/{key}")
async def collection(key: str, _: dict = Depends(gateway.require_admin),
                     release: str | None = Query(None, max_length=128)):
    return _json(await _read(_not_found(_collection, key, release)))


@router.get("/collections/{key}/queries/{name}/example")
async def query_example(key: str, name: str, _: dict = Depends(gateway.require_admin),
                        release: str | None = Query(None, max_length=128)):
    return _json(await _read(_not_found(_query_example, key, name, release)))


@router.get("/claims")
async def claims(
    _: dict = Depends(gateway.require_admin),
    q: str | None = Query(None, max_length=200),
    collection: str | None = Query(None, max_length=128),
    claim_key: str | None = Query(None, max_length=64),
    dataset: str | None = Query(None, max_length=128),
    origin: str | None = Query(None, pattern="^(collection|uncollected)$"),
    status: str | None = Query(None, pattern="^(minted|unminted)$"),
    sort: str = Query("recent", pattern="^(recent|asserted|minted|subject)$"),
    page: int = Query(1, ge=1, le=100_000),
    page_size: int = Query(25, ge=1, le=100),
):
    def clean(v):
        return (v or "").strip() or None
    return _json(await _read(_claims, clean(q), clean(collection), clean(claim_key), clean(dataset), origin, status,
                             sort, page, page_size))


@router.get("/claims/{claim_id:path}")
async def claim_detail(claim_id: str, _: dict = Depends(gateway.require_admin)):
    detail = await _read(_claim_detail, claim_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="No claim with this ID is stored or mintable.")
    return _json(detail)


@router.get("/tables")
async def tables(_: dict = Depends(gateway.require_admin), fresh: bool = False):
    return _json(await _read(_tables, fresh))


@router.get("/tables/{table}/rows")
async def table_rows(
    table: str,
    _: dict = Depends(gateway.require_admin),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    column: str | None = Query(None, max_length=64),
    op: str = Query("contains", pattern="^(contains|equals)$"),
    value: str | None = Query(None, max_length=500),
):
    return _json(await _read(_table_rows, table, page, page_size, column, op, value))


@router.get("/activity")
async def activity(_: dict = Depends(gateway.require_admin)):
    return _json(await _read(_activity))


def _object_id(value: str) -> str:
    from .refs import DAPPER_ID_RE, MAX_ID_LENGTH
    if not DAPPER_ID_RE.match(value) or len(value) > MAX_ID_LENGTH:
        raise HTTPException(status_code=400, detail="This is not a dapper identifier.")
    return value


@router.get("/objects/classes")
async def object_classes(_: dict = Depends(gateway.require_admin)):
    return _json(await _read(_object_classes))


@router.get("/objects")
async def objects(
    _: dict = Depends(gateway.require_admin),
    class_name: str | None = Query(None, pattern="^[A-Za-z]{1,64}$"),
    q: str | None = Query(None, max_length=200),
    references: str | None = Query(None, max_length=128),
    deposit: str | None = Query(None, max_length=40),
    page: int = Query(1, ge=1, le=100_000),
    page_size: int = Query(25, ge=1, le=100),
):
    def clean(v):
        return (v or "").strip() or None
    return _json(await _read(_objects, class_name, clean(q), clean(references), clean(deposit), page, page_size))


@router.get("/objects/{object_id:path}")
async def object_detail(object_id: str, _: dict = Depends(gateway.require_admin)):
    detail = await _read(_object_detail, _object_id(object_id))
    if detail is None:
        raise HTTPException(status_code=404, detail="No object with this ID is stored or referenced.")
    return _json(detail)


@router.get("/deposits")
async def deposit_list(_: dict = Depends(gateway.require_admin), page: int = Query(1, ge=1, le=100_000),
                       page_size: int = Query(25, ge=1, le=100)):
    return _json(await _read(_deposit_list, page, page_size))


@router.get("/accounts")
async def accounts(_: dict = Depends(gateway.require_admin), page: int = Query(1, ge=1, le=100_000),
                   page_size: int = Query(25, ge=1, le=100)):
    return _json(await _read(_accounts, page, page_size))


@router.get("/accounts/{account_id:path}")
async def account_detail(account_id: str, _: dict = Depends(gateway.require_admin)):
    detail = await _read(_account, _object_id(account_id))
    if detail is None:
        raise HTTPException(status_code=404, detail="No scientific account with this ID is stored.")
    return _json(detail)


class AuthEvent(BaseModel):
    event: str = Field(pattern="^(login|login_denied|logout)$")
    email: str | None = Field(None, max_length=320)
    detail: str | None = Field(None, max_length=255)


def _record_event(event: AuthEvent) -> None:
    con = db.connect()
    try:
        with con:
            con.execute(
                "INSERT INTO admin_auth_events (at, event, email, detail) VALUES (?, ?, ?, ?)",
                (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), event.event,
                 (event.email or "").strip().lower() or None, event.detail or None),
            )
    finally:
        con.close()


@router.post("/auth-events", status_code=204)
async def auth_event(event: AuthEvent, _: dict = Depends(gateway.require_auth_event)):
    """The console's sign-in audit: approved sign-ins, denials, sign-outs."""
    await run_in_threadpool(_record_event, event)
    return Response(status_code=204, headers=NO_STORE)


# --- deposit keys ------------------------------------------------------------------

class KeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=api_keys.MAX_NAME)


def _key_event(con, event: str, email: str, key_id: str, name: str) -> None:
    """Audit row in the same transaction as the key change. The key itself is never written anywhere."""
    con.execute(
        "INSERT INTO admin_auth_events (at, event, email, detail) VALUES (?, ?, ?, ?)",
        (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), event, email, f"{key_id} {name}"[:255]),
    )


def _key_row(row: dict) -> dict:
    return {"keyId": row["key_id"], "name": row["name"], "createdAt": _ts(row["created_at"]), "createdBy": row["created_by"],
            "revokedAt": _ts(row["revoked_at"]), "revokedBy": row["revoked_by"], "deposits": int(row["deposits"]),
            "lastDepositAt": _ts(row["last_deposit_at"]), "collections": row.get("collections", [])}


@router.get("/keys")
async def keys_list(_: dict = Depends(gateway.require_admin)):
    """Deposit keys with how much each has deposited. Never the key or its hash."""
    rows = await _read(api_keys.list_keys)
    return _json({"asOf": _ts(datetime.now(timezone.utc)), "keys": [_key_row(row) for row in rows]})


@router.post("/keys")
async def keys_issue(body: KeyRequest, admin: dict = Depends(gateway.require_admin_write)):
    """Issue a deposit key. This response is the only time the key is shown; the service keeps its SHA-256."""
    def run():
        con = db.connect()
        try:
            with con:
                issued = api_keys.issue(con, body.name, admin["email"])
                _key_event(con, "api_key_issued", admin["email"], issued["key_id"], issued["name"])
            return issued
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        finally:
            con.close()
    issued = await run_in_threadpool(run)
    return _json({"keyId": issued["key_id"], "name": issued["name"], "key": issued["key"]}, status=201)


class GrantRequest(BaseModel):
    collection: str = Field(min_length=1, max_length=64)


@router.post("/keys/{key_id}/grants")
async def keys_grant(key_id: str, body: GrantRequest, admin: dict = Depends(gateway.require_admin_write)):
    """Let a key deposit into a collection (open, fill and close its releases). Without a grant a key can still
    make deposits that name no collection."""
    if not api_keys.KEY_ID_RE.match(key_id):
        raise HTTPException(status_code=404, detail="Unknown key.")
    def run():
        con = db.connect()
        try:
            with con:
                row = con.execute("SELECT name, revoked_at FROM api_keys WHERE key_id = ?", (key_id,)).fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="Unknown key.")
                if row["revoked_at"] is not None:
                    raise HTTPException(status_code=409, detail="This key is revoked.")
                try:
                    added = api_keys.grant(con, key_id, body.collection, admin["email"])
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc))
                if added:
                    _key_event(con, "api_key_granted", admin["email"], key_id, f"{row['name']} -> {body.collection}")
                return api_keys.grants(con, key_id)
        finally:
            con.close()
    return _json({"keyId": key_id, "collections": await run_in_threadpool(run)})


@router.post("/keys/{key_id}/grants/{collection}/withdraw")
async def keys_ungrant(key_id: str, collection: str, admin: dict = Depends(gateway.require_admin_write)):
    """Withdraw a key's grant for a collection. A deposit into it already in flight finishes first."""
    if not api_keys.KEY_ID_RE.match(key_id):
        raise HTTPException(status_code=404, detail="Unknown key.")
    def run():
        con = db.connect()
        try:
            with con:
                row = con.execute("SELECT name FROM api_keys WHERE key_id = ?", (key_id,)).fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="Unknown key.")
                if api_keys.revoke_grant(con, key_id, collection):
                    _key_event(con, "api_key_grant_withdrawn", admin["email"], key_id, f"{row['name']} -> {collection}")
                return api_keys.grants(con, key_id)
        finally:
            con.close()
    return _json({"keyId": key_id, "collections": await run_in_threadpool(run)})


@router.post("/keys/{key_id}/revoke")
async def keys_revoke(key_id: str, admin: dict = Depends(gateway.require_admin_write)):
    """Stop a key depositing. Its past deposits stay attributed to it."""
    if not api_keys.KEY_ID_RE.match(key_id):
        raise HTTPException(status_code=404, detail="Unknown key.")
    def run():
        con = db.connect()
        try:
            with con:
                row = con.execute("SELECT name, revoked_at FROM api_keys WHERE key_id = ?", (key_id,)).fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="Unknown key.")
                if row["revoked_at"] is not None or not api_keys.revoke(con, key_id, admin["email"]):
                    raise HTTPException(status_code=409, detail="This key is already revoked.")
                _key_event(con, "api_key_revoked", admin["email"], key_id, row["name"])
        finally:
            con.close()
    await run_in_threadpool(run)
    return _json({"keyId": key_id, "revoked": True})


# --- registration (the Add collection flow) --------------------------------------

def _registration_enabled() -> None:
    """Runs after the gateway dependency: off unless CLAIMS_COLLECTION_ROOTS is set."""
    if not reg.roots():
        raise HTTPException(status_code=403, detail=reg.DISABLED)


class CheckRequest(BaseModel):
    collectionDir: str = Field(min_length=1, max_length=4096)
    releaseFile: str = Field(min_length=1, max_length=4096)
    sample: int = Field(3, ge=0, le=10)


class JobRequest(BaseModel):
    collectionDir: str = Field(min_length=1, max_length=4096)
    releaseFile: str = Field(min_length=1, max_length=4096)
    requireClean: bool = False


@router.get("/registration/sources")
async def registration_sources(_: dict = Depends(gateway.require_admin)):
    return _json(await run_in_threadpool(reg.sources))


@router.post("/registration/check")
async def registration_check(body: CheckRequest, _: dict = Depends(gateway.require_admin_write),
                             __: None = Depends(_registration_enabled)):
    def run(con):
        try:
            return reg.check(con, body.collectionDir, body.releaseFile, body.sample)
        except reg.OutsideRoots as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return _json(await _read(run))


@router.post("/registration/jobs")
async def registration_start(body: JobRequest, admin: dict = Depends(gateway.require_admin_write),
                             __: None = Depends(_registration_enabled)):
    def run():
        try:
            return reg.start_job(body.collectionDir, body.releaseFile, body.requireClean, admin["email"])
        except reg.OutsideRoots as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except reg.JobRunning as exc:
            raise HTTPException(status_code=409, detail=str(exc))
    return _json(await run_in_threadpool(run), status=202)


@router.get("/registration/jobs")
async def registration_jobs(_: dict = Depends(gateway.require_admin), __: None = Depends(_registration_enabled)):
    return _json({"jobs": reg.jobs()})


@router.get("/registration/jobs/{job_id}")
async def registration_job(job_id: str, _: dict = Depends(gateway.require_admin),
                           __: None = Depends(_registration_enabled)):
    found = reg.job(job_id)
    if found is None:
        raise HTTPException(status_code=404, detail="No such job (jobs are kept in memory and lost on restart).")
    return _json(found)
