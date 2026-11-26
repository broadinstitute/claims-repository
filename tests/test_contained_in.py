"""Row.containedIn: a row's sets when the membership is listed by set objects of an object collection (a CFDE
gene set's members in cfde-genesets), not by edges of the dataset. With the fixture's own memberships written
into an object collection's field index, it answers what Row.joined answers through the edges, in both stores."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from service import db as service_db
from service.app import app
from service.collections import store as cstore
from service.collections.manifest import load_collection, load_release
from service.collections.publish import publish
from tests.test_query_batching import FIX

GENES = """rows(collection: "pigean", kind: "gene_phenotype", first: 8, orderBy: {metric: "combined"},
       dimensions: [{name: "trait", value: "FIXPHENO"}, {name: "library", value: "fixture-v1"}])"""


def _published(tmp_path, monkeypatch, store) -> str:
    if store == "parquet":
        monkeypatch.setenv("CLAIMS_INDEX_DIR", str(tmp_path / "index"))
    path = str(tmp_path / "c.db")
    con = service_db.connect(path)
    try:
        spec, base = load_release(FIX / "pigean-fixture-release.yaml")
        publish(con, load_collection(FIX / "pigean"), spec, base)
        # An object collection whose GeneSets list the fixture's memberships under the field `gene`.
        dataset_ids = [r["dataset_id"] for r in con.execute("SELECT dataset_id FROM release_datasets").fetchall()]
        st = cstore.get_store(con, dataset_ids)
        edges = st.query(f"SELECT e.from_id, e.to_id FROM {st.tables(dataset_ids)[2]} e WHERE e.kind = 'member_of'",
                         [])
        assert edges
        record = {"kind": "objects", "manifest": {"object_kinds": {"GeneSet": {"fields": {"gene": {"slot": "members"}}}}}}
        con.execute("INSERT INTO collections (collection_key, title, current_release_id) VALUES (?, ?, ?)",
                    ("fixture-sets", "Fixture sets", "dapper:ResultCollection.fixturesets"))
        con.execute("INSERT INTO collection_releases (release_id, collection_key, version, manifest, document) "
                    "VALUES (?, ?, ?, ?, ?)", ("dapper:ResultCollection.fixturesets", "fixture-sets", "1",
                                              json.dumps(record), "{}"))
        con.executemany("INSERT INTO release_object_fields (collection_key, version, object_id, field, value, num) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        sorted({("fixture-sets", "1", e["to_id"], "gene", e["from_id"], None) for e in edges}))
        con.commit()
    finally:
        con.close()
    monkeypatch.setattr(service_db, "DB_PATH", path)
    return path


def _ask(query):
    r = TestClient(app).post("/v1/graphql", json={"query": query})
    assert r.status_code == 200, r.text
    body = r.json()
    assert not body.get("errors"), body
    return body["data"]


@pytest.mark.parametrize("store", ["sql", "parquet"])
def test_contained_in_answers_what_the_edges_answer(store, tmp_path, monkeypatch):
    _published(tmp_path, monkeypatch, store)
    for sharing, dims in (('["trait", "library"]', ""), ('["trait"]', ', dimensions: [{name: "library", value: "fixture-v1"}]')):
        data = _ask("{ " + GENES + """ { rows { subject { id }
            joined(via: "member_of", kind: "gene_set_phenotype", orderBy: {metric: "beta_uncorrected"}, first: 3) {
              row { subject { id } betaU: metric(name: "beta_uncorrected") } }
            containedIn(kind: "gene_set_phenotype", setsFrom: "fixture-sets", memberField: "gene",
                        sharing: """ + sharing + dims + """, orderBy: {metric: "beta_uncorrected"}, first: 3) {
              subject { id } betaU: metric(name: "beta_uncorrected") claim(key: "measurement") { id } } } } }""")
        rows = data["rows"]["rows"]
        assert rows and any(r["joined"] for r in rows)
        for r in rows:
            assert [j["row"] for j in r["joined"]] == [{k: v for k, v in c.items() if k != "claim"}
                                                      for c in r["containedIn"]]
            assert all(c["claim"]["id"].startswith("dapper:Claim.") for c in r["containedIn"])


def test_contained_in_refuses_what_it_cannot_answer(tmp_path, monkeypatch):
    _published(tmp_path, monkeypatch, "sql")

    def error(args):
        r = TestClient(app).post("/v1/graphql", json={"query": "{ " + GENES + " { rows { containedIn(" + args +
                                                      ") { subject { id } } } } }"})
        return json.dumps(r.json().get("errors"))

    base = 'kind: "gene_set_phenotype", orderBy: {metric: "beta_uncorrected"}, sharing: ["trait"]'
    assert "not an object collection" in error(base + ', setsFrom: "pigean", memberField: "gene"')
    assert "indexes no field" in error(base + ', setsFrom: "fixture-sets", memberField: "name"')
    assert "no dimension" in error('kind: "gene_set_phenotype", orderBy: {metric: "beta_uncorrected"}, '
                                   'sharing: ["tissue"], setsFrom: "fixture-sets", memberField: "gene"')
    assert "not a metric" in error('kind: "gene_set_phenotype", orderBy: {metric: "combined"}, sharing: ["trait"], '
                                   'setsFrom: "fixture-sets", memberField: "gene"')
