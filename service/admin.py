"""Admin API: telemetry on claims, templates, tables and API traffic.

JSON at /v1/admin/api/*, consumed by the admin console (/admin in frontend/,
the repository's Next.js app). Every call must carry the console server's short-lived signed
assertion (service/admin_gateway.py); responses are no-store. Admin reads
never assert claims: the claim detail re-synthesizes a template-backed claim
to show whether it still matches its content address, but writes nothing.
Tables are read-only and allowlisted; there is no SQL editor and no write
action beyond the console's sign-in audit events.
"""
from __future__ import annotations

import hashlib
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
from . import db
from . import pigean_template as tpl
from . import telemetry

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

def _live(core, family):
    return core._live_roster(family)


def _family_stats(con) -> list[dict]:
    core = _core()
    out = []
    for family, table in core.CLAIM_SOURCES:
        r = con.execute(
            "SELECT COUNT(*) AS implied, COUNT(o.id) AS minted, COALESCE(SUM(o.times_seen), 0) AS assertions, "
            "MAX(COALESCE(o.last_seen_at, o.created_at)) AS last_at "
            f"FROM {table} t LEFT JOIN dapper_objects o ON o.id = t.claim_id "
            f"WHERE t.synthesis_inputs IS NOT NULL{_live(core, family)}"
        ).fetchone()
        out.append({"family": family, "table": table, "implied": int(r["implied"]), "minted": int(r["minted"]),
                    "assertions": int(r["assertions"]), "lastAssertedAt": _ts(r["last_at"])})
    return out


_NOT_TEMPLATE = (
    " AND NOT EXISTS (SELECT 1 FROM pigean_gene_associations x WHERE x.claim_id = c.claim_id AND x.synthesis_inputs IS NOT NULL)"
    " AND NOT EXISTS (SELECT 1 FROM pigean_gene_set_associations x WHERE x.claim_id = c.claim_id AND x.synthesis_inputs IS NOT NULL)"
    " AND NOT EXISTS (SELECT 1 FROM pigean_gene_set_rosters x WHERE x.claim_id = c.claim_id AND x.synthesis_inputs IS NOT NULL)"
)


def _overview(con) -> dict:
    core = _core()
    started = time.perf_counter()
    con.execute("SELECT 1").fetchone()
    db_ms = round((time.perf_counter() - started) * 1000, 1)
    families = _family_stats(con)
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
    deposited = con.execute(
        "SELECT COUNT(*) AS n FROM claims c JOIN dapper_objects o ON o.id = c.claim_id WHERE 1 = 1" + _NOT_TEMPLATE
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
    phenotypes = []
    for r in con.execute(
        "SELECT t.phenotype, MIN(t.trait_group) AS trait_group, COUNT(*) AS genes, COUNT(o.id) AS minted "
        "FROM pigean_gene_associations t LEFT JOIN dapper_objects o ON o.id = t.claim_id "
        "WHERE t.synthesis_inputs IS NOT NULL GROUP BY t.phenotype ORDER BY genes DESC LIMIT 50"
    ).fetchall():
        view = core._phenotype_view(con, r["phenotype"], r["trait_group"])
        sets = con.execute(
            "SELECT COUNT(*) AS n FROM pigean_gene_set_associations WHERE phenotype = ? AND synthesis_inputs IS NOT NULL",
            (r["phenotype"],),
        ).fetchone()["n"]
        phenotypes.append({"id": view["id"], "legacyId": view["legacyId"], "name": view["name"],
                           "geneRows": int(r["genes"]), "setRows": int(sets), "mintedGeneClaims": int(r["minted"])})
    release = con.execute("SELECT * FROM kpn_trait_release WHERE release_key = 'current'").fetchone()
    trait_count = con.execute("SELECT COUNT(*) AS n FROM kpn_traits").fetchone()["n"]
    tele = telemetry.snapshot()
    return {
        "asOf": _ts(datetime.now(timezone.utc)),
        "database": {"backend": "sqlite" if con.dialect.sqlite else "mysql",
                     "name": db.MYSQL_DATABASE if not con.dialect.sqlite else Path(db.DB_PATH).name,
                     "pingMs": db_ms},
        "claims": {
            "implied": sum(f["implied"] for f in families),
            "minted": int(stored["n"]), "deposited": int(deposited),
            "assertions": int(stored["s"]), "lastAssertedAt": _ts(stored["last_at"]),
            "minted24h": int(minted_24h), "asserted24h": int(asserted_24h),
        },
        "families": families,
        "objectsByClass": by_class,
        "mintTimeline": timeline,
        "phenotypes": phenotypes,
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
    }


# --- claims -----------------------------------------------------------------

CLAIM_SORTS = {
    "recent": "u.last_asserted_at DESC, u.claim_id",
    "asserted": "u.times_seen DESC, u.claim_id",
    "minted": "u.minted_at DESC, u.claim_id",
    "subject": "COALESCE(u.subject, '~') ASC, u.claim_id",
}
FAMILY_SUBJECT = {"gene": "t.gene", "gene_set": "t.gene_set", "roster": "t.gene_set"}


def _claims(con, q, family, status, phenotype, sort, page, page_size) -> dict:
    core = _core()
    parts, params = [], []
    keys = core._phenotype_keys(con, phenotype) if phenotype else None
    for fam, table in core.CLAIM_SOURCES:
        if family and family != fam:
            continue
        where = ["t.synthesis_inputs IS NOT NULL"]
        sub_params = []
        if status == "minted":
            where.append("o.id IS NOT NULL")
        elif status == "unminted":
            where.append("o.id IS NULL")
        if keys:
            where.append(f"t.phenotype {core._in(keys)}")
            sub_params += keys
        if q:
            pattern = f"%{db.like_escape(q)}%"
            where.append(f"({con.dialect.like_ci(FAMILY_SUBJECT[fam])} OR {con.dialect.like_ci('t.claim_id')})")
            sub_params += [pattern, pattern]
        parts.append(
            f"SELECT '{fam}' AS family, t.claim_id AS claim_id, {FAMILY_SUBJECT[fam]} AS subject, "
            "t.phenotype AS phenotype, t.collection AS collection, o.times_seen AS times_seen, "
            "o.created_at AS minted_at, COALESCE(o.last_seen_at, o.created_at) AS last_asserted_at "
            f"FROM {table} t LEFT JOIN dapper_objects o ON o.id = t.claim_id "
            f"WHERE {' AND '.join(where)}{core._live_roster(fam)}"
        )
        params += sub_params
    if (not family or family == "deposited") and status != "unminted" and not keys:
        where = "WHERE 1 = 1" + _NOT_TEMPLATE
        sub_params = []
        if q:
            where += f" AND {con.dialect.like_ci('c.claim_id')}"
            sub_params.append(f"%{db.like_escape(q)}%")
        parts.append(
            "SELECT 'deposited' AS family, c.claim_id AS claim_id, NULL AS subject, NULL AS phenotype, "
            "NULL AS collection, o.times_seen AS times_seen, o.created_at AS minted_at, "
            "COALESCE(o.last_seen_at, o.created_at) AS last_asserted_at "
            f"FROM claims c JOIN dapper_objects o ON o.id = c.claim_id {where}"
        )
        params += sub_params
    if not parts:
        return {"total": 0, "page": page, "pageSize": page_size, "rows": []}
    union = " UNION ALL ".join(parts)
    total = con.execute(f"SELECT COUNT(*) AS n FROM ({union}) u", params).fetchone()["n"]
    rows = con.execute(
        f"SELECT * FROM ({union}) u ORDER BY {CLAIM_SORTS[sort]} LIMIT ? OFFSET ?",
        params + [page_size, (page - 1) * page_size],
    ).fetchall()
    out = []
    for r in rows:
        r = _row(r)
        out.append({
            "claimId": r["claim_id"], "family": r["family"], "subject": r["subject"],
            "phenotype": r["phenotype"], "collection": r["collection"],
            "minted": r["times_seen"] is not None, "timesAsserted": int(r["times_seen"] or 0),
            "mintedAt": _ts(r["minted_at"]), "lastAssertedAt": _ts(r["last_asserted_at"]),
        })
    return {"total": int(total), "page": page, "pageSize": page_size, "rows": out}


def _claim_detail(con, claim_id) -> dict | None:
    core = _core()
    stored = con.execute(
        "SELECT payload, class_name, identity_profile, times_seen, created_at, last_seen_at FROM dapper_objects WHERE id = ?",
        (claim_id,),
    ).fetchone()
    if stored is not None and stored["class_name"] != "Claim":
        return None
    source = core._index_source(con, claim_id)
    verification = {"templateBacked": source is not None, "synthesizes": None, "storedMatches": None, "error": None}
    synthesized = None
    if source is not None:
        try:
            synthesized = core.synthesize_claim(*source)
            verification["synthesizes"] = True
        except core.ClaimSynthesisError as exc:
            verification.update(synthesizes=False, error=str(exc))
    stored_doc = json.loads(stored["payload"]) if stored is not None else None
    if stored_doc is not None and synthesized is not None:
        verification["storedMatches"] = stored_doc == synthesized
        if not verification["storedMatches"]:
            verification["error"] = "the stored document differs from the claim its index row implies"
    claim = stored_doc or synthesized
    if claim is None:
        return None
    prop_id = claim.get("proposition") if isinstance(claim.get("proposition"), str) else None
    proposition = core._fetch_payload(con, prop_id) if prop_id else None
    score_ids = core._id_list(claim.get("has_score"))
    score_docs = core._fetch_payloads(con, score_ids)
    evidence_ids = core._id_list(claim.get("has_evidence"))
    evidence_docs = core._fetch_payloads(con, evidence_ids)
    index_row = None
    if source is not None:
        row = _row(source[1])
        inputs = row.pop("synthesis_inputs", None)
        index_row = {"family": source[0], "columns": row,
                     "synthesisInputs": inputs if isinstance(inputs, dict) else json.loads(inputs) if inputs else None}
    registered = con.execute("SELECT method, deposited_at FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
    assessing = 0
    if prop_id:
        assessing = con.execute("SELECT COUNT(*) AS n FROM claims WHERE proposition_id = ?", (prop_id,)).fetchone()["n"]
    return {
        "claimId": claim_id,
        "minted": stored is not None,
        "identityProfile": stored["identity_profile"] if stored is not None else tpl.IDENTITY_PROFILE,
        "timesAsserted": int(stored["times_seen"]) if stored is not None else 0,
        "mintedAt": _ts(stored["created_at"]) if stored is not None else None,
        "lastAssertedAt": _ts(stored["last_seen_at"] or stored["created_at"]) if stored is not None else None,
        "method": registered["method"] if registered is not None else (tpl.METHOD if source else None),
        "triple": core._triple_of(proposition) if proposition else None,
        "statement": claim.get("statement"),
        "verification": verification,
        "claim": claim,
        "proposition": proposition,
        "scores": [score_docs.get(s) or {"id": s, "missing": True} for s in score_ids],
        "evidence": [evidence_docs.get(e) or {"id": e, "missing": True} for e in evidence_ids],
        "indexRow": index_row,
        "claimsAssessingProposition": int(assessing),
        "resolverUrl": core._resolver_url(claim_id),
    }


# --- templates ----------------------------------------------------------------

def _file_info(path: Path) -> dict:
    data = path.read_bytes()
    return {"path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(data).hexdigest(),
            "lines": data.count(b"\n"), "bytes": len(data)}


FAMILY_METRICS = {"gene": list(tpl.GENE_METRICS), "gene_set": list(tpl.SET_METRICS), "roster": []}
FAMILY_RELATION = {"gene": "associated_with", "gene_set": "associated_with", "roster": "has_members"}


def _templates(con) -> dict:
    core = _core()
    stats = {f["family"]: f for f in _family_stats(con)}
    families = []
    for family, table in core.CLAIM_SOURCES:
        example = con.execute(
            f"SELECT o.payload FROM {table} t JOIN dapper_objects o ON o.id = t.claim_id "
            f"WHERE t.synthesis_inputs IS NOT NULL{core._live_roster(family)} "
            "ORDER BY COALESCE(o.last_seen_at, o.created_at) DESC LIMIT 1"
        ).fetchone()
        doc = json.loads(example["payload"]) if example else None
        families.append({**stats[family], "relation": FAMILY_RELATION[family], "metrics": FAMILY_METRICS[family],
                         "example": {"claimId": doc["id"], "statement": doc.get("statement")} if doc else None})
    metrics = []
    for name, sem in core.PIGEAN_METRICS.items():
        kind, interpretation = tpl.INTERP.get(name, (None, None))
        metrics.append({"metric": name, "scoreKind": sem["score_kind"], "scale": sem["scale"], "better": sem["better"],
                        "confirmed": sem["confirmed"], "description": sem["description"],
                        "claimInterpretation": interpretation,
                        "families": [f for f, ms in FAMILY_METRICS.items() if name in ms],
                        "kindAgrees": kind == sem["score_kind"]})
    queries = []
    qdir = ROOT / "api" / "graphql" / "queries"
    for path in sorted(qdir.glob("*.graphql")):
        variables = path.with_suffix(".variables.json")
        fixture = path.with_name(path.stem + ".fixture-response.json")
        text = path.read_text()
        queries.append({**_file_info(path), "name": path.stem, "document": text,
                        "variables": json.loads(variables.read_text()) if variables.exists() else None,
                        "hasFixture": fixture.exists()})
    return {
        "methods": [{
            "name": tpl.METHOD, "identityProfile": tpl.IDENTITY_PROFILE, "dapperProfile": core.DAPPER_PROFILE,
            "contractVersion": core.CONTRACT_VERSION,
            "template": _file_info(Path(tpl.__file__)),
            "graphqlModule": _file_info(ROOT / "api" / "graphql" / "methods" / f"{tpl.METHOD}.graphql"),
            "families": families, "metrics": metrics,
        }],
        "coreSchema": _file_info(ROOT / "api" / "graphql" / "core.graphql"),
        "queryTemplates": queries,
    }


# --- tables -------------------------------------------------------------------

def _tables_known() -> list[str]:
    import re
    return re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", db.SQLITE_DDL)


HIDDEN_COLUMNS: dict[str, set[str]] = {}
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


# --- API routes -----------------------------------------------------------------

@router.get("/me")
def me(admin: dict = Depends(gateway.require_admin)):
    return _json(admin)


@router.get("/overview")
async def overview(_: dict = Depends(gateway.require_admin)):
    return _json(await _read(_overview))


@router.get("/claims")
async def claims(
    _: dict = Depends(gateway.require_admin),
    q: str | None = Query(None, max_length=200),
    family: str | None = Query(None, pattern="^(gene|gene_set|roster|deposited)$"),
    status: str | None = Query(None, pattern="^(minted|unminted)$"),
    phenotype: str | None = Query(None, max_length=128),
    sort: str = Query("recent", pattern="^(recent|asserted|minted|subject)$"),
    page: int = Query(1, ge=1, le=100_000),
    page_size: int = Query(25, ge=1, le=100),
):
    return _json(await _read(_claims, (q or "").strip() or None, family, status, (phenotype or "").strip() or None,
                             sort, page, page_size))


@router.get("/claims/{claim_id:path}")
async def claim_detail(claim_id: str, _: dict = Depends(gateway.require_admin)):
    detail = await _read(_claim_detail, claim_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="No claim with this ID is stored or implied.")
    return _json(detail)


@router.get("/templates")
async def templates(_: dict = Depends(gateway.require_admin)):
    return _json(await _read(_templates))


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
