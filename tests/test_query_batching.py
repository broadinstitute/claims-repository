"""Serving a query's claims in batches (docs/query-performance.md) holds for any file-based collection, not only
PIGEAN's names: a copy of the fixture collection with every row kind, edge kind and the minted entity renamed
serves the same responses batched as claim by claim, in both stores, and reads each dataset's gene sets once."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from service import app as service_app
from service import db as service_db
from service.app import app
from service.collections import store as cstore
from service.collections.manifest import load_collection, load_release
from service.collections.publish import publish

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "collections"
RENAMES = [(r"\bgene_set_phenotype\b", "module_score"), (r"\bgene_phenotype\b", "protein_score"),
           (r"\bmember_of\b", "belongs_to"), (r"\bGeneSet\b", "Module")]
QUERIES = [("top-genes-with-sets", {"trait": "FIXPHENO", "library": "fixture-v1", "topGenes": 5, "setsPerGene": 3}),
           ("gene-across-traits", {"gene": "FIXG1", "library": "fixture-v1"}),
           ("gene-set-across-traits", {"geneSet": "FIXSET_A", "library": "fixture-v1"})]


def _variant(root: Path) -> Path:
    """The fixture collection under other names: another key, row kinds, edge kind and minted entity."""
    out = root / "variant"
    shutil.copytree(FIX / "pigean", out)
    for path in [out / "collection.yaml", *(out / "templates").iterdir(), *(out / "queries").iterdir()]:
        text = path.read_text()
        for pattern, name in RENAMES:
            text = re.sub(pattern, name, text)
        text = text.replace("mint: Module", "mint: GeneSet")   # the DAPPER class minted stays a GeneSet
        if path.name == "collection.yaml":
            text = re.sub(r"(?m)^collection: .*$", "collection: variant", text, count=1)
        path.write_text(text)
    return out


def _serve(db_path: str, batched: bool, monkeypatch) -> tuple[list, int]:
    """Each query's response, and how many gene-set reads (store.groups) they made."""
    reads = {"n": 0}
    original = cstore._SqlShape.groups

    def counted(self, *args, **kwargs):
        reads["n"] += 1
        return original(self, *args, **kwargs)
    monkeypatch.setattr(cstore._SqlShape, "groups", counted)
    monkeypatch.setattr(service_db, "DB_PATH", db_path)
    if batched:
        monkeypatch.setattr(service_app._collection_envelope, "many", lambda con, items: service_app.envelopes(con, items),
                            raising=False)
    else:
        monkeypatch.delattr(service_app._collection_envelope, "many", raising=False)
    client = TestClient(app)
    out = []
    for name, variables in QUERIES:
        r = client.post(f"/v1/collections/variant/queries/{name}:run", json={"variables": variables})
        assert r.status_code == 200, r.text
        out.append(_scrub(r.json()))
    return out, reads["n"]


def _scrub(o):
    if isinstance(o, dict):
        return {k: _scrub(v) for k, v in o.items() if k not in ("firstDepositedAt", "lastDepositedAt")}
    if isinstance(o, list):
        return [_scrub(v) for v in o]
    return o


@pytest.mark.parametrize("store", ["sql", "parquet"])
def test_batched_serving_is_claim_by_claim_serving_for_any_collection(store, monkeypatch, tmp_path):
    collection_dir = _variant(tmp_path)
    col = load_collection(collection_dir)
    assert col.key == "variant" and "module_score" in col.doc["row_kinds"] and "belongs_to" in col.doc["edge_kinds"]
    spec, base = load_release(FIX / "pigean-fixture-release.yaml")
    if store == "parquet":
        monkeypatch.setenv("CLAIMS_INDEX_DIR", str(tmp_path / "index"))
    first = str(tmp_path / "a.db")
    con = service_db.connect(first)
    try:
        publish(con, col, spec, base)
    finally:
        con.close()
    second = str(tmp_path / "b.db")
    shutil.copyfile(first, second)

    one_by_one, _ = _serve(first, batched=False, monkeypatch=monkeypatch)
    batched, reads = _serve(second, batched=True, monkeypatch=monkeypatch)
    assert batched == one_by_one
    rendered = json.dumps(batched)
    assert '"claims": {' in rendered and "dapper:Claim." in rendered and "belongs_to" in rendered
    assert all(r["rows"] for r in batched), [len(r["rows"]) for r in batched]
    # One dataset, one edge kind: each query reads its gene sets at most once, whatever they are called.
    assert 1 <= reads <= len(QUERIES)


GRAPHQL = """{
  rows(collection: "variant", kind: "protein_score", first: 5, orderBy: {metric: "combined"},
       dimensions: [{name: "trait", value: "FIXPHENO"}, {name: "library", value: "fixture-v1"}]) {
    rows {
      subject { id } claimKeys
      claim(key: "measurement") { id timesDeposited evidence { item valueLocators } }
      joined(via: "belongs_to", kind: "module_score", orderBy: {metric: "beta_uncorrected"}, first: 2) {
        edge { to { id } claim(key: "roster") { id provenanceRefs { resolution } } }
        row { subject { id } claim(key: "measurement") { id } }
      }
    }
  }
}"""


@pytest.mark.parametrize("store", ["sql", "parquet"])
def test_graphql_lists_are_served_in_batches_for_any_collection(store, monkeypatch, tmp_path):
    """Row.claim, Row.joined and RowEdge.claim answer a whole list at once: the same response as claim by claim,
    in a handful of database calls."""
    col = load_collection(_variant(tmp_path))
    spec, base = load_release(FIX / "pigean-fixture-release.yaml")
    if store == "parquet":
        monkeypatch.setenv("CLAIMS_INDEX_DIR", str(tmp_path / "index"))
    first = str(tmp_path / "a.db")
    con = service_db.connect(first)
    try:
        publish(con, col, spec, base)
    finally:
        con.close()
    second = str(tmp_path / "b.db")
    shutil.copyfile(first, second)
    batched_envelopes = service_app.envelopes
    calls = {"n": 0}
    execute = service_db.Connection.execute

    def counted(self, *args, **kwargs):
        calls["n"] += 1
        return execute(self, *args, **kwargs)
    monkeypatch.setattr(service_db.Connection, "execute", counted)

    def run(db_path, one_by_one):
        monkeypatch.setattr(service_db, "DB_PATH", db_path)
        monkeypatch.setattr(service_app, "envelopes", (lambda con, items: [batched_envelopes(con, [i])[0] for i in items])
                            if one_by_one else batched_envelopes)
        calls["n"] = 0
        r = TestClient(app).post("/v1/graphql", json={"query": GRAPHQL})
        assert r.status_code == 200 and "errors" not in r.json(), r.text
        return _scrub(r.json()), calls["n"]

    expected, slow = run(first, True)
    got, fast = run(second, False)
    assert got == expected
    rows = got["data"]["rows"]["rows"]
    assert rows and all(r["claim"] for r in rows) and any(j["edge"]["claim"] for r in rows for j in r["joined"])
    assert fast < slow and fast <= 30, (fast, slow)
