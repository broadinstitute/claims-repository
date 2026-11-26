"""Object collections (kind: objects): releases built from deposits of DAPPER objects, with per-collection key
grants, multi-part releases, named queries over the deposited objects; and the bulk object reads REVEAL's pages
use (batch get, lineage)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from service import api_keys
from service import db as service_db
from service.app import app
from service.collections import identity as cidn
from service.collections import objects
from service.collections.manifest import ManifestError, load_collection

ROOT = Path(__file__).resolve().parent.parent
COLLECTION = ROOT / "tests" / "fixtures" / "collections" / "catalog-test"
CAD = "http://purl.obolibrary.org/obo/MONDO_0021661"
client = TestClient(app)        # conftest gives this one the session's deposit key


@pytest.fixture(scope="module", autouse=True)
def isolated_db():
    saved = service_db.DB_PATH
    service_db.DB_PATH = str(Path(tempfile.mkdtemp()) / "objects.db")
    yield service_db.DB_PATH
    service_db.DB_PATH = saved


def _con():
    return service_db.connect()


def _rows(sql, params=()):
    con = _con()
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def _mint(cls, **content):
    return cidn.mint(cls, content)


# The catalog: two gaps about CAD (one names a mechanism it is about), a gap about another disease, a mechanism,
# a gene-set collection and two of its gene sets.
MECH = _mint("Mechanism", name="Endothelial dysfunction", description="Loss of endothelial barrier function.")
GAP_A = _mint("KnowledgeGap", name="A: CAD contexts", text="Are CAD contexts causes or readouts?",
              gap_description="Some contexts may be downstream readouts.", gap_kind="KNOWLEDGE_GAP",
              scope="Coronary Artery Disease", about_entities=[CAD])
GAP_B = _mint("KnowledgeGap", name="B: CAD endothelium", text="Does endothelial dysfunction precede plaque?",
              gap_description="Ordering is unknown.", gap_kind="KNOWLEDGE_GAP", scope="Coronary Artery Disease",
              about_entities=[CAD, MECH["id"]])
GAP_C = _mint("KnowledgeGap", name="C: other disease", text="q", gap_description="d", gap_kind="KNOWLEDGE_GAP",
              about_entities=["http://purl.obolibrary.org/obo/MONDO_0005148"])
GSC = _mint("GeneSetCollection", name="CFDE test sets", collection_key="cfde-test", version="1")
SET_SMALL = _mint("GeneSet", name="SET_SMALL", members=["NOD2"], member_type="gene", n_members=1,
                  in_gene_set_collection=[GSC["id"]])
SET_BIG = _mint("GeneSet", name="SET_BIG", members=["NOD2", "IL23R", "ATG16L1"], member_type="gene", n_members=3,
                in_gene_set_collection=[GSC["id"]])
PART_1 = {"knowledge_gaps": [GAP_A, GAP_B, GAP_C], "mechanisms": [MECH]}
PART_2 = {"gene_set_collections": [GSC], "gene_sets": [SET_SMALL, SET_BIG]}


def _deposit(document, **extra):
    return client.post("/v1/deposits", json={"document": document, **extra})


def _run(query, variables):
    return client.post(f"/v1/collections/catalog-test/queries/{query}:run", json={"variables": variables})


@pytest.fixture(scope="module")
def registered(isolated_db):
    con = _con()
    try:
        return objects.register(con, load_collection(COLLECTION))
    finally:
        con.close()


def _admin(monkeypatch):
    """A function giving admin-console headers for a purpose (admin_read, admin_write)."""
    import time
    import uuid

    import jwt

    from service import admin_gateway

    secret = "s" * 40
    monkeypatch.setattr(admin_gateway, "SECRET", secret)

    def token(purpose):
        now = int(time.time())
        return {"Authorization": "Bearer " + jwt.encode(
            {"iss": admin_gateway.ISSUER, "aud": admin_gateway.AUDIENCE, "iat": now, "exp": now + 120,
             "jti": str(uuid.uuid4()), "sub": "admin@example.org", "purpose": purpose, "email": "admin@example.org",
             "email_verified": True}, secret, algorithm="HS256")}
    return token


def _grant(depositor, collection="catalog-test"):
    con = _con()
    try:
        with con:
            api_keys.grant(con, depositor["key_id"], collection, "tests")
    finally:
        con.close()


@pytest.fixture(scope="module")
def closed(registered, depositor):
    """The two-part catalog release, opened, filled and closed."""
    _grant(depositor)
    assert client.post("/v1/collections/catalog-test/releases", json={"version": "2026.11.0"}).status_code == 201
    one = _deposit(PART_1, collection="catalog-test", release="2026.11.0")
    two = _deposit(PART_2, collection="catalog-test", release="2026.11.0")
    assert one.status_code == 200 and two.status_code == 200, (one.text, two.text)
    done = client.post("/v1/collections/catalog-test/releases/2026.11.0:close")
    assert done.status_code == 200, done.text
    return {"parts": [one.json(), two.json()], "closed": done.json()}


# --- the manifest ------------------------------------------------------------------------------------------

def test_object_manifests_are_checked_against_dapper(tmp_path):
    import shutil

    col = tmp_path / "c"
    shutil.copytree(COLLECTION, col)
    text = (col / "collection.yaml").read_text()
    (col / "collection.yaml").write_text(text.replace("disease: {slot: about_entities, description",
                                                      "disease: {slot: about_things, description"))
    with pytest.raises(ManifestError, match="KnowledgeGap has no slot 'about_things'"):
        load_collection(col)
    (col / "collection.yaml").write_text(text.replace("  Mechanism:\n", "  NotAClass:\n"))
    with pytest.raises(ManifestError, match="'NotAClass' is not a class"):
        load_collection(col)
    (col / "collection.yaml").write_text(text)
    q = col / "queries" / "gaps-by-disease.yaml"
    q.write_text(q.read_text().replace("order: {field: name", "order: {field: nickname"))
    with pytest.raises(ManifestError, match="'nickname' is not a field of KnowledgeGap"):
        load_collection(col)


# --- grants, open, parts, close ----------------------------------------------------------------------------

def test_a_registered_collection_without_a_closed_release_breaks_nothing(registered, monkeypatch):
    """Between registration and its first close, a collection is listed nowhere and serves no queries, and
    every discovery and console page still answers."""
    token = _admin(monkeypatch)
    anonymous = TestClient(app)
    assert all(c["id"] != "catalog-test" for c in client.get("/v1/collections").json()["collections"])
    assert all(q["collection"] != "catalog-test" for q in client.get("/v1/queries").json()["queries"])
    assert client.get("/v1/collections/catalog-test").status_code == 404
    assert _run("gaps-by-disease", {"disease": "CAD"}).status_code == 404
    for path in ("overview", "collections"):
        assert anonymous.get(f"/v1/admin/api/{path}", headers=token("admin_read")).status_code == 200, path
    assert anonymous.get("/v1/admin/api/collections/catalog-test", headers=token("admin_read")).status_code in (200, 404)


def test_a_key_needs_a_grant_for_a_collection_but_not_for_a_plain_deposit(registered, depositor):
    con = _con()
    try:
        with con:
            api_keys.revoke_grant(con, depositor["key_id"], "catalog-test")
    finally:
        con.close()
    refused = client.post("/v1/collections/catalog-test/releases", json={"version": "2026.10.0"})
    assert refused.status_code == 403 and "grant it in the admin console" in refused.json()["detail"]
    node = _mint("Proposition", statement="a plain deposit needs no grant")
    plain = client.post("/v1/deposits", json={"objects": [node]})
    assert plain.status_code == 200 and plain.json()["collection"] is None
    into = client.post("/v1/deposits", json={"objects": [node], "collection": "catalog-test", "release": "2026.10.0"})
    assert into.status_code == 403


def test_a_two_part_release_is_readable_through_a_named_query(closed):
    one, two = closed["parts"]
    assert one["collection"] == {"key": "catalog-test", "release": "2026.11.0", "state": "open", "objects": 4}
    assert two["collection"]["objects"] == 3
    status = closed["closed"]
    assert status["state"] == "closed" and status["release_id"].startswith("dapper:ResultCollection.")
    assert status["objects"] == 7 and status["deposits"] == 2 and status["unresolved_references"] == []
    assert status["classes"] == {"GeneSet": 2, "GeneSetCollection": 1, "KnowledgeGap": 3, "Mechanism": 1}

    by_key = _run("gaps-by-disease", {"disease": "CAD"}).json()        # the collection's own mapping
    by_iri = _run("gaps-by-disease", {"disease": CAD}).json()
    assert [r["id"] for r in by_key["rows"]] == [GAP_A["id"], GAP_B["id"]] == [r["id"] for r in by_iri["rows"]]
    row = by_key["rows"][0]
    assert row["class"] == "KnowledgeGap" and row["fields"]["name"] == "A: CAD contexts"
    assert row["object"] == client.get(f"/v1/objects/{GAP_A['id']}").json()  # served as stored, ids unchanged
    assert by_key["release"]["id"] == status["release_id"] and by_key["release"]["version"] == "2026.11.0"

    anchors = _run("gap-with-anchors", {"gap": GAP_B["id"]}).json()["rows"]
    assert [a["id"] for a in anchors[0]["anchors"]] == [MECH["id"]]
    sets = _run("gene-sets-in-collection", {"name": "cfde"}).json()["rows"]
    assert [s["fields"]["name"] for s in sets[0]["sets"]] == ["SET_BIG", "SET_SMALL"]  # size, descending
    assert _run("gene-sets-in-collection", {"name": "CFDE", "setsPerCollection": 1}).json()["rows"][0]["sets"][0][
        "id"] == SET_BIG["id"]
    gql = client.post("/v1/graphql", json={"query": '{ runQuery(collection: "catalog-test", query: "gaps-by-disease", '
                                                    'variables: {disease: "CAD"}) }'}).json()
    assert [r["id"] for r in gql["data"]["runQuery"]["rows"]] == [GAP_A["id"], GAP_B["id"]]


def test_resending_a_part_is_safe_and_a_closed_release_takes_no_more(closed):
    again = client.post("/v1/collections/catalog-test/releases/2026.11.0:close")
    assert again.status_code == 200 and again.json()["release_id"] == closed["closed"]["release_id"]
    late = _deposit(PART_2, collection="catalog-test", release="2026.11.0")
    assert late.status_code == 409 and "closed" in late.json()["detail"]
    reopened = client.post("/v1/collections/catalog-test/releases", json={"version": "2026.11.0"})
    assert reopened.status_code == 409
    # A failed part resent into an open release changes nothing but the deposit log.
    assert client.post("/v1/collections/catalog-test/releases", json={"version": "2026.11.1"}).status_code == 201
    assert client.post("/v1/collections/catalog-test/releases", json={"version": "2026.11.1"}).status_code == 200
    first = _deposit(PART_2, collection="catalog-test", release="2026.11.1").json()
    second = _deposit(PART_2, collection="catalog-test", release="2026.11.1").json()
    assert first["collection"]["objects"] == second["collection"]["objects"] == 3
    assert all(d["status"] == "exists" and not d.get("stored_differs") for d in second["deposited"])
    status = client.get("/v1/collections/catalog-test/releases/2026.11.1").json()
    assert status["state"] == "open" and status["objects"] == 3 and status["deposits"] == 2
    # Naming no release works while exactly one is open.
    implicit = _deposit({"mechanisms": [MECH]}, collection="catalog-test").json()
    assert implicit["collection"]["release"] == "2026.11.1"
    # Queries keep reading the closed release until this one closes.
    assert _run("gaps-by-disease", {"disease": "CAD"}).json()["release"]["version"] == "2026.11.0"


def test_a_later_plain_deposit_of_a_catalog_object_is_unchanged(closed):
    receipt = _deposit({"knowledge_gaps": [GAP_A]})
    assert receipt.status_code == 200, receipt.text
    body = receipt.json()
    [outcome] = body["deposited"]
    assert outcome["id"] == GAP_A["id"] and outcome["status"] == "exists"
    assert outcome.get("stored_differs", False) is False and body["unresolved_references"] == []
    assert body["collection"] is None
    assert client.get(f"/v1/deposits/{body['deposit_id']}").json()["collection"] is None


def test_deposits_report_their_collection_and_release(closed):
    part = closed["parts"][0]
    got = client.get(f"/v1/deposits/{part['deposit_id']}").json()
    assert got["collection"] == {"key": "catalog-test", "release": "2026.11.0",
                                 "release_id": closed["closed"]["release_id"], "state": "closed"}
    listed = client.get("/v1/deposits", params={"object": GAP_A["id"]}).json()["deposits"]
    assert {d["collection"]["key"] if d["collection"] else None for d in listed} >= {"catalog-test", None}


# --- discovery ---------------------------------------------------------------------------------------------

def test_the_catalog_describes_object_queries_well_enough_to_run_them(closed):
    catalog = client.get("/v1/queries", params={"collection": "catalog-test"}).json()
    entries = {e["name"]: e for e in catalog["queries"]}
    assert set(entries) == {"gaps-by-disease", "gap-with-anchors", "gene-sets-in-collection"}
    e = entries["gaps-by-disease"]
    assert e["kind"] == "objects" and e["claims"] == [] and e["returns"]["rows"]["class"] == "KnowledgeGap"
    disease = e["params"]["disease"]
    assert disease["field"] == "disease" and disease["values"]["all"] == "/v1/collections/catalog-test/dimensions/disease"
    cad = next(v for v in disease["values"]["sample"] if v["value"] == CAD)
    assert cad["also_accepts"] == ["CAD"] and cad["label"] == "coronary artery disease" and cad["category"] == "cardiovascular"
    assert "notes" in entries["gap-with-anchors"]  # its example names no gap in the release
    for entry in entries.values():
        r = client.post(entry["run"]["rest"]["path"], json=entry["run"]["rest"]["body"])
        assert r.status_code == 200, (entry["name"], r.text)
    assert "object_row" in catalog["guide"]
    values = client.get("/v1/collections/catalog-test/dimensions/disease", params={"search": "coronary"}).json()
    assert [v["value"] for v in values["values"]] == [CAD] and values["values"][0]["objects"] == 2
    summary = next(c for c in client.get("/v1/collections").json()["collections"] if c["id"] == "catalog-test")
    assert summary["kind"] == "objects" and summary["objects"] == 7
    described = client.get("/v1/collections/catalog-test").json()
    assert described["kind"] == "objects" and described["objects"]["total"] == 7
    document = client.get("/v1/collections/catalog-test/releases/2026.11.0/document").json()
    rc = document["result_collections"][0]
    assert rc["id"] == closed["closed"]["release_id"] and cidn.verify(rc) and rc["dataset_dimensions"] == ["class"]
    gaps = next(d for d in document["datasets"] if d["dimension_values"][0]["dimension_value"] == "KnowledgeGap")
    members_file = next(f for f in document["files"] if f["id"] == gaps["has_file"][0])
    served = client.get(members_file["location"])
    assert served.status_code == 200 and served.text.split() == sorted([GAP_A["id"], GAP_B["id"], GAP_C["id"]])
    import hashlib
    assert hashlib.sha256(served.content).hexdigest() == members_file["sha256"]


# --- bulk reads --------------------------------------------------------------------------------------------

def test_batch_get_returns_found_documents_and_missing_ids(closed):
    missing = "dapper:Mechanism." + "Z" * 32
    got = client.post("/v1/objects:batchGet", json={"ids": [GAP_A["id"], MECH["id"], missing, GAP_A["id"]]}).json()
    assert list(got["objects"]) == [GAP_A["id"], MECH["id"]] and got["missing"] == [missing]
    assert got["objects"][GAP_A["id"]] == client.get(f"/v1/objects/{GAP_A['id']}").json()
    too_many = client.post("/v1/objects:batchGet", json={"ids": [f"dapper:Mechanism.{i:032d}" for i in range(501)]})
    assert too_many.status_code == 400 and "at most 500" in too_many.json()["detail"]
    assert client.post("/v1/objects:batchGet", json={"ids": "x"}).status_code == 400


def test_lineage_follows_references_and_deposited_edges(closed):
    lineage = client.get(f"/v1/objects/{GAP_B['id']}/lineage").json()
    nodes = {n["id"]: n for n in lineage["nodes"]}
    assert nodes[GAP_B["id"]]["direction"] == "self"
    assert nodes[MECH["id"]]["direction"] == "outgoing" and nodes[MECH["id"]]["class_name"] == "Mechanism"
    edges = {(e["from"], e["label"], e["to"]) for e in lineage["edges"]}
    assert (GAP_B["id"], "about_entities", MECH["id"]) in edges and lineage["truncated"] is False
    # Something that names the gap appears as an incoming node, labelled with the slot that names it.
    prop = _mint("Proposition", statement="The gap is about endothelial dysfunction.", subject_entity=GAP_B["id"],
                 relation="urn:test:about", object_entity=MECH["id"])
    assert _deposit({"propositions": [prop]}).status_code == 200
    again = client.get(f"/v1/objects/{GAP_B['id']}/lineage").json()
    assert {n["id"]: n["direction"] for n in again["nodes"]}[prop["id"]] == "incoming"
    assert (prop["id"], "subject_entity", GAP_B["id"]) in {(e["from"], e["label"], e["to"]) for e in again["edges"]}
    # An Activity that used a File: a deposited edge, not a slot of either node.
    file = _mint("File", filename="paper.xml", mime_type="application/xml", sha256="a" * 64, size_in_bytes=10)
    activity = _mint("Activity", name="Fetch a paper", activity_type="Fetch")
    receipt = _deposit({"activities": [activity], "files": [file],
                        "used_edges": [{"subject": activity["id"], "object": file["id"]}]})
    assert receipt.status_code == 200, receipt.text
    out = client.get(f"/v1/objects/{activity['id']}/lineage", params={"depth": 1}).json()
    assert {(e["from"], e["label"], e["to"], e["kind"]) for e in out["edges"]} == {
        (activity["id"], "Used", file["id"], "edge")}
    back = client.get(f"/v1/objects/{file['id']}/lineage", params={"depth": 1}).json()
    assert {n["id"]: n["direction"] for n in back["nodes"]}[activity["id"]] == "incoming"
    assert client.get(f"/v1/objects/{GAP_B['id']}/lineage", params={"depth": 4}).status_code == 422
    assert client.get("/v1/objects/dapper:Mechanism.nothere/lineage").status_code == 404


# --- the admin console -------------------------------------------------------------------------------------

def test_admin_grants_and_collection_pages(closed, monkeypatch):
    token = _admin(monkeypatch)
    anonymous = TestClient(app)
    issued = anonymous.post("/v1/admin/api/keys", json={"name": "catalog depositor"}, headers=token("admin_write")).json()
    key = {"X-API-Key": issued["key"]}
    assert anonymous.post("/v1/collections/catalog-test/releases", json={"version": "2026.12.0"},
                          headers=key).status_code == 403
    path = f"/v1/admin/api/keys/{issued['keyId']}/grants"
    assert anonymous.post(path, json={"collection": "catalog-test"}, headers=token("admin_read")).status_code in (401, 403)
    granted = anonymous.post(path, json={"collection": "catalog-test"}, headers=token("admin_write"))
    assert granted.status_code == 200 and granted.json()["collections"] == ["catalog-test"]
    listed = next(k for k in anonymous.get("/v1/admin/api/keys", headers=token("admin_read")).json()["keys"]
                  if k["keyId"] == issued["keyId"])
    assert listed["collections"] == ["catalog-test"]
    assert anonymous.post("/v1/collections/catalog-test/releases", json={"version": "2026.12.0"},
                          headers=key).status_code == 201
    withdrawn = anonymous.post(f"{path}/catalog-test/withdraw", json={}, headers=token("admin_write"))
    assert withdrawn.status_code == 200 and withdrawn.json()["collections"] == []
    assert anonymous.post("/v1/collections/catalog-test/releases/2026.12.0:close", headers=key).status_code == 403
    events = _rows("SELECT event FROM admin_auth_events WHERE detail LIKE ? ORDER BY id", (issued["keyId"] + "%",))
    assert [e["event"] for e in events] == ["api_key_issued", "api_key_granted", "api_key_grant_withdrawn"]
    # The console's collection pages read an object collection without failing.
    collections = anonymous.get("/v1/admin/api/collections", headers=token("admin_read"))
    assert collections.status_code == 200, collections.text
    detail = anonymous.get("/v1/admin/api/collections/catalog-test", headers=token("admin_read"))
    assert detail.status_code == 200, detail.text


TOOLKIT = ROOT.parent / "kpn-dapper-toolkit" / "collections"


@pytest.mark.skipif(not (TOOLKIT / "dismech").is_dir(), reason="kpn-dapper-toolkit checkout not found beside this repository")
def test_the_toolkits_dismech_collection_reads_a_deposited_release(closed, depositor):
    con = _con()
    try:
        objects.register(con, load_collection(TOOLKIT / "dismech"))
    finally:
        con.close()
    _grant(depositor, "dismech")
    assert client.post("/v1/collections/dismech/releases", json={"version": "test.1"}).status_code == 201
    assert _deposit(PART_1, collection="dismech", release="test.1").status_code == 200
    assert client.post("/v1/collections/dismech/releases/test.1:close").status_code == 200
    run = lambda q, v: client.post(f"/v1/collections/dismech/queries/{q}:run", json={"variables": v}).json()  # noqa: E731
    assert [r["id"] for r in run("gaps-by-disease", {"diseaseName": "coronary"})["rows"]] == [GAP_A["id"], GAP_B["id"]]
    assert [r["id"] for r in run("gaps-by-disease", {"disease": CAD})["rows"]] == [GAP_A["id"], GAP_B["id"]]
    assert len(run("gaps-by-disease", {})["rows"]) == 3
    [gap] = run("gap-with-anchors", {"gap": GAP_B["id"]})["rows"]
    assert [a["id"] for a in gap["anchors"]] == [MECH["id"]]


def test_a_second_release_becomes_current_and_names_the_first(closed):
    """Last: closes 2026.11.1 (opened by an earlier test), which moves every catalog-test query to it."""
    first = closed["closed"]["release_id"]
    done = client.post("/v1/collections/catalog-test/releases/2026.11.1:close").json()
    assert done["state"] == "closed" and done["release_id"] != first
    document = client.get("/v1/collections/catalog-test/releases/2026.11.1/document").json()
    assert document["result_collections"][0]["was_revision_of"] == first
    out = _run("gene-sets-in-collection", {"name": "cfde"}).json()
    assert out["release"] == {"id": done["release_id"], "version": "2026.11.1"}
    assert _run("gaps-by-disease", {"disease": "CAD"}).json()["rows"] == []   # no gaps in this release
    old = client.post("/v1/collections/catalog-test/queries/gaps-by-disease:run",
                      json={"variables": {"disease": "CAD"}, "release": "2026.11.0"}).json()
    assert [r["id"] for r in old["rows"]] == [GAP_A["id"], GAP_B["id"]]       # an earlier release stays readable


def test_an_unknown_id_is_404_beside_an_object_release(closed):
    for missing in ("dapper:GeneSet.nothere", "nope", "dapper:Claim.nothere"):
        assert client.get(f"/v1/objects/{missing}").status_code == 404, missing
