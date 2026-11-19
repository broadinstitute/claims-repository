"""The Parquet result store against the SQL one: the same release published to
each implies the same claim ids, gives the same query results (ties
included), and serves the same claims. With either store a claim, its
propositions, scores and evidence are stored the first time it is served."""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from service import db as service_db
from service.app import app
from service.collections import serve as cserve
from service.collections import store as cstore
from service.collections.manifest import load_collection
from service.collections.publish import publish

from collection_claims import implied_claims

pytest.importorskip("duckdb")

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
COLLECTION = FIX / "collections" / "pigean"
ROLES = {"gene_stats": "combined_gene_stats.json", "gene_set_stats": "combined_gene_set_stats.json",
         "membership": "gene_gene_set_stats.json"}
QUERY = "top-genes-with-sets"
client = TestClient(app)


def _files(tmp: Path, phenotype: str, tie: bool) -> Path:
    """The PIGEAN fixture files as one phenotype's run; `tie` gives two genes
    the same combined score so ordering ties are exercised."""
    out = tmp / phenotype
    out.mkdir(parents=True)
    for name in ROLES.values():
        rows = [json.loads(line) for line in (FIX / "pigean" / name).read_text().splitlines() if line.strip()]
        for row in rows:
            row["phenotype"] = phenotype
            if tie and name == ROLES["gene_stats"] and row["gene"] in ("FIXG1", "FIXG3"):
                row["combined"] = 4.25
        (out / name).write_text("".join(json.dumps(r) + "\n" for r in rows))
    return out


def _spec(folders: dict) -> dict:
    return {"version": "0.9.0-store", "datasets": [
        {"dimensions": {"trait": p, "library": "fixture-v1"},
         "files": {role: {"path": str(folder / name), "location": f"s3://bucket/{p}/{name}"}
                   for role, name in ROLES.items()}}
        for p, folder in folders.items()]}


@contextmanager
def _deployment(db_path: str, index_dir: str | None):
    """Point the service (database and result store) at one deployment."""
    saved_db, saved_index = service_db.DB_PATH, os.environ.get("CLAIMS_INDEX_DIR")
    service_db.DB_PATH = db_path
    if index_dir:
        os.environ["CLAIMS_INDEX_DIR"] = index_dir
    else:
        os.environ.pop("CLAIMS_INDEX_DIR", None)
    try:
        yield
    finally:
        service_db.DB_PATH = saved_db
        if saved_index is None:
            os.environ.pop("CLAIMS_INDEX_DIR", None)
        else:
            os.environ["CLAIMS_INDEX_DIR"] = saved_index


@pytest.fixture(scope="module")
def deployments():
    tmp = Path(tempfile.mkdtemp())
    folders = {"FIXPHENO": _files(tmp / "files", "FIXPHENO", tie=True),
               "OTHERPHENO": _files(tmp / "files", "OTHERPHENO", tie=False)}
    spec = _spec(folders)
    out = {}
    for name, index in (("sql", None), ("parquet", str(tmp / "index"))):
        db_path = str(tmp / f"{name}.db")
        with _deployment(db_path, index):
            con = service_db.connect()
            try:
                result = publish(con, load_collection(COLLECTION), spec, tmp)
            finally:
                con.close()
        out[name] = {"db": db_path, "index": index, "result": result}
    return out


def _claim_ids(dep) -> set[tuple]:
    with _deployment(dep["db"], dep["index"]):
        con = service_db.connect()
        try:
            return {(r["claim_id"], r["claim_key"], r["dataset_id"], r["proposition_id"])
                    for r in implied_claims(con)}
        finally:
            con.close()


def _run(dep, variables):
    with _deployment(dep["db"], dep["index"]):
        con = service_db.connect()
        try:
            return cserve.run_query(con, "pigean", QUERY, variables, claims=[])
        finally:
            con.close()


def test_both_stores_publish_the_same_release_and_claims(deployments):
    sql, pq = deployments["sql"]["result"], deployments["parquet"]["result"]
    assert sql.release_id == pq.release_id
    assert sql.stats["mint_contexts_recorded"] == pq.stats["mint_contexts_recorded"] > 0
    assert (sql.stats["rows"], sql.stats["edges"]) == (pq.stats["rows"], pq.stats["edges"])
    ids = _claim_ids(deployments["sql"])
    assert ids == _claim_ids(deployments["parquet"]) and len(ids) == 40


@pytest.mark.parametrize("variables", [
    {"trait": "FIXPHENO", "library": "fixture-v1", "topGenes": 3, "setsPerGene": 2},
    {"trait": "FIXPHENO", "library": "fixture-v1", "topGenes": 2, "setsPerGene": 1, "priorMax": 1.0},
    {"trait": "OTHERPHENO", "library": "fixture-v1", "topGenes": 5, "setsPerGene": 5},
    {"trait": "NOSUCHPHENO", "library": "fixture-v1"},
])
def test_both_stores_answer_queries_identically(deployments, variables):
    sql = _run(deployments["sql"], variables)
    pq = _run(deployments["parquet"], variables)
    assert json.dumps(sql, sort_keys=True) == json.dumps(pq, sort_keys=True)
    if variables["trait"] == "FIXPHENO" and "priorMax" not in variables:  # FIXG1 = FIXG3 = 4.25: ties break by value
        labels = [r["subject"]["label"] for r in sql["rows"]]
        assert labels[:2] == ["FIXG1", "FIXG3"]


def test_describe_counts_agree(deployments):
    out = {}
    for name, dep in deployments.items():
        with _deployment(dep["db"], dep["index"]):
            con = service_db.connect()
            try:
                d = cserve.describe(con, "pigean")
            finally:
                con.close()
        out[name] = ([ds["counts"] for ds in d["datasets"]], d["claims"])
    assert out["sql"] == out["parquet"]


def test_parquet_store_serves_claims_and_stores_their_objects_on_first_serve(deployments):
    dep = deployments["parquet"]
    with _deployment(dep["db"], dep["index"]):
        con = service_db.connect()
        try:
            assert con.execute("SELECT COUNT(*) AS n FROM dapper_objects WHERE id LIKE 'dapper:Proposition.%'"
                               ).fetchone()["n"] == 0  # nothing of the data plane before a serve
        finally:
            con.close()
        body = {"variables": {"trait": "FIXPHENO", "library": "fixture-v1", "topGenes": 1, "setsPerGene": 1}}
        served = client.post(f"/v1/collections/pigean/queries/{QUERY}:run", json=body)
        assert served.status_code == 200, served.text
        row = served.json()["rows"][0]
        measurement = row["claims"]["measurement"]
        assert measurement["triple"]["subject"] == "HGNC.SYMBOL:FIXG1"
        assert [s["metric"] for s in measurement["scores"]] == ["combined", "log_bf", "prior"]
        assert len(measurement["evidence"]) == 3 and measurement["evidence"][0]["valueLocators"]
        roster = row["sets"][0]["edge"]["claims"]["roster"]
        assert roster["provenanceRefs"]["resolution"] == "RESOLVED"  # its GeneSet was rebuilt and stored
        con = service_db.connect()
        try:
            stored = {r["id"] for r in con.execute("SELECT id FROM dapper_objects").fetchall()}
        finally:
            con.close()
        claim = measurement["claim"]
        assert claim["id"] in stored and claim["proposition"] in stored
        assert set(claim["has_score"]) <= stored and set(claim["has_evidence"]) <= stored
        # Resolvable by id afterwards, and refused-or-identical on re-serve.
        got = client.get(f"/v1/objects/{claim['proposition']}")
        assert got.status_code == 200 and got.json()["id"] == claim["proposition"]
        again = client.post(f"/v1/collections/pigean/queries/{QUERY}:run", json=body).json()
        assert again["rows"][0]["claims"]["measurement"]["claim"] == claim


def test_a_claim_resolves_by_id_once_served(deployments):
    """Nothing records a claim before it is served: its id resolves from its first serve on."""
    dep = deployments["parquet"]
    body = {"variables": {"trait": "OTHERPHENO", "library": "fixture-v1", "topGenes": 1, "setsPerGene": 0}}
    with _deployment(dep["db"], dep["index"]):
        top = client.post(f"/v1/collections/pigean/queries/{QUERY}:run", json={**body, "claims": []}).json()
        subject = top["rows"][0]["subject"]["id"]
        con = service_db.connect()
        try:
            store = cstore.get_store(con)
            unserved = next(c["claim_id"] for c in implied_claims(con) if c["claim_key"] == "measurement"
                            and store.row(c["dataset_id"], c["row_id"])["subject_id"] == subject
                            and c["dataset_id"] == top["rows"][0]["dataset"]["id"])
        finally:
            con.close()
        query = f'{{ claim(id: "{unserved}") {{ id triple {{ subject }} }} }}'
        assert client.post("/v1/graphql", json={"query": query}).json()["data"]["claim"] is None
        client.post(f"/v1/collections/pigean/queries/{QUERY}:run", json=body)
        got = client.post("/v1/graphql", json={"query": query})
        assert got.status_code == 200 and "errors" not in got.json(), got.text
        assert got.json()["data"]["claim"]["id"] == unserved
