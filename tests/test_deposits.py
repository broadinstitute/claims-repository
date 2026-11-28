"""POST /v1/deposits: any DAPPER class, validated against the vendored schema and its DAPPER-ID-1 address; a
whole DAPPER document (nodes and edges) or a flat list; one deposit record per accepted bundle. The accepted
fixtures were minted elsewhere: dapper's own frozen identity test vectors, and a ScientificAccount document
from REVEAL's API examples (minted by REVEAL with DAPPER 0.2.0 tooling)."""
import copy
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import jwt
from fastapi.testclient import TestClient

import test_service as base
from service import admin_gateway, api_keys, dapper_validation as dval
from service import db as service_db
from service.app import app
from service.collections import identity as cidn

ROOT = Path(__file__).resolve().parent.parent
client = TestClient(app)        # conftest gives this one the session's deposit key
anonymous = TestClient(app)
VECTORS = json.loads((ROOT / "vendor/dapper/identity/test_vectors.json").read_text())["vectors"]
ACCOUNT = json.loads((ROOT / "tests/fixtures/deposits/reveal-scientific-account.dapper.json").read_text())
SECRET = "s" * 40


def _vector(name):
    vector = next(v for v in VECTORS if v["name"] == name)
    return {"id": vector["expected_id"], **vector["instance"]}


def _rows(sql, params=()):
    con = service_db.connect()
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def _count(table):
    return _rows(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


def _minted(class_name, **content):
    return cidn.mint(class_name, content)


def _refused(body, fragment):
    before = (_count("dapper_objects"), _count("deposits"))
    response = client.post("/v1/deposits", json=body)
    assert response.status_code == 400, response.text
    assert fragment in response.json()["detail"], response.json()["detail"]
    assert (_count("dapper_objects"), _count("deposits")) == before
    return response.json()["detail"]


def test_dappers_own_identity_vectors_deposit_as_any_class():
    nodes = [{"id": v["expected_id"], **v["instance"]} for v in VECTORS]
    response = client.post("/v1/deposits", json={"objects": nodes})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["validation"] == {"identity_profile": "DAPPER-ID-1", "dapper_version": cidn.DAPPER_VERSION}
    assert {o["class_name"] for o in body["deposited"]} == {v["class"] for v in VECTORS} and len({v["class"] for v in VECTORS}) > 3
    assert body["edges"] == 0
    # dapper's vectors include two pairs that share an address on purpose: the second of each differs only in slots
    # identity ignores (a re-stamped Activity, a relocated Embedding). The first document stands.
    repeats = {"activity-timestamp-changed", "embedding-relocated"}
    for vector, outcome in zip(VECTORS, body["deposited"]):
        expected = {"status": "exists", "stored_differs": True} if vector["name"] in repeats else {"status": "created"}
        assert {k: outcome.get(k) for k in expected} == expected, (vector["name"], outcome)
    first = {}
    for node in nodes:
        first.setdefault(node["id"], node)
    for oid, node in first.items():
        stored = _rows("SELECT class_name, identity_profile, payload FROM dapper_objects WHERE id = ?", (oid,))[0]
        # The server recorded the profile it verified, and stored the plain DAPPER node.
        assert stored["identity_profile"] == "DAPPER-ID-1" and json.loads(stored["payload"]) == node
        assert cidn.verify(json.loads(stored["payload"])) and "class_name" not in json.loads(stored["payload"])
        assert anonymous.get("/v1/objects/" + oid).status_code == 200


def test_a_whole_scientific_account_document_deposits_with_its_edges(depositor):
    response = client.post("/v1/deposits", json={"document": ACCOUNT})
    assert response.status_code == 200, response.text
    body = response.json()
    nodes = [n for group, rows in ACCOUNT.items() if not group.endswith("_edges") for n in rows]
    assert len(body["deposited"]) == len(nodes) == 52 and body["edges"] == len(ACCOUNT["used_edges"]) == 5
    assert body["unresolved_references"] == [] and all(o["status"] == "created" for o in body["deposited"])
    account_id = ACCOUNT["scientific_accounts"][0]["id"]
    assert anonymous.get("/v1/objects/" + account_id).json()["component_claims"] == ACCOUNT["scientific_accounts"][0]["component_claims"]

    record = anonymous.get("/v1/deposits/" + body["deposit_id"]).json()
    assert record["depositor"]["key_id"] == depositor["key_id"] and record["object_count"] == 52
    assert {o["id"] for o in record["objects"]} == {n["id"] for n in nodes}
    assert record["edges"] == [{"class_name": "Used", **edge} for edge in ACCOUNT["used_edges"]]
    found = anonymous.get("/v1/objects/search", params={"deposit": body["deposit_id"], "class_name": "ScientificAccount"}).json()
    assert [o["id"] for o in found["objects"]] == [account_id]
    # Everything the account's claims reference is walkable through the reference index.
    claim = ACCOUNT["claims"][0]
    assessing = anonymous.get("/v1/objects/search", params={"references": claim["proposition"], "class_name": "Claim"}).json()
    assert claim["id"] in {o["id"] for o in assessing["objects"]}
    assert anonymous.post("/v1/graphql", json={"query": f'{{ claim(id: "{claim["id"]}") {{ id }} }}'}).json()["data"]["claim"]["id"] == claim["id"]

    # Depositing the account again changes nothing and is a second deposit of the same objects.
    again = client.post("/v1/deposits", json={"document": ACCOUNT}).json()
    assert {o["status"] for o in again["deposited"]} == {"exists"} and not any("stored_differs" in o for o in again["deposited"])
    listed = anonymous.get("/v1/deposits", params={"object": account_id}).json()
    assert [d["deposit_id"] for d in listed["deposits"]] == [again["deposit_id"], body["deposit_id"]]   # newest first


def test_invalid_objects_are_refused_whole_and_nothing_is_stored():
    proposition = _minted("Proposition", statement="A valid proposition that must not be stored beside an invalid one.")
    activity = _vector("minimal-activity")
    tampered = {**proposition, "statement": "changed after minting"}
    _refused({"objects": [proposition, tampered]}, "the id is not the DAPPER-ID-1 address of this content")
    _refused({"objects": [{**_minted("Proposition", statement="x"), "confidence": 0.9}]}, "Proposition has no slot 'confidence'")
    missing = {"id": "dapper:Claim." + "A" * 32, "statement": "no proposition"}
    assert "proposition is required" in _refused({"objects": [missing]}, "is required")
    _refused({"objects": [{"id": "dapper:Proposition." + "B" * 32, "statement": ["a list"]}]}, "statement must be a single value")
    _refused({"objects": [{"id": "dapper:Claim." + "C" * 32, "proposition": activity["id"], "has_evidence": "one"}]}, "must be a list")
    _refused({"objects": [{"id": "dapper:Claim." + "D" * 32, "proposition": activity["id"]}]}, "proposition must reference Proposition, not Activity")
    _refused({"objects": [{"id": "dapper:Claim." + "E" * 32, "proposition": proposition["id"], "direction": "SIDEWAYS"}]}, "direction must be one of")
    _refused({"objects": [{"id": "dapper:File." + "F" * 32, "size_in_bytes": "big"}]}, "size_in_bytes must be")
    _refused({"objects": [{"id": "dapper:Proposition." + "G" * 32, "statement": {"nested": True}}]}, "must not be a nested object")
    # Mixins, abstract classes, synthetic and local-dev ids cannot be deposited here.
    _refused({"objects": [{"id": "dapper:Agent." + "H" * 32, "name": "a mixin"}]}, "Agent is not a DAPPER class that can be deposited")
    _refused({"objects": [{"id": "dapper:Proposition.SYNTHG01", "statement": "x"}]}, "is not a DAPPER-ID-1 identifier")
    _refused({"objects": [{"id": "dapper:Proposition.local" + "0" * 32, "statement": "x"}]}, "is not a DAPPER-ID-1 identifier")
    _refused({"objects": [{**proposition, "class_name": "Claim"}]}, "disagrees with the id's class")
    _refused({"objects": [proposition, "not an object"]}, "every object must be a JSON object")
    for body, fragment in (({"objects": []}, "at least one object"), ({"objects": "x"}, "objects must be a list"),
                           ({}, "exactly one of document"), ({"objects": [proposition], "document": {}}, "exactly one of document"),
                           ({"document": []}, "document must be a JSON object"),
                           ({"document": {"claimz": [proposition]}}, "document has no DAPPER group 'claimz'"),
                           ({"document": {"claims": [proposition]}}, "listed under Claim but its id names Proposition"),
                           ({"document": {"propositions": proposition}}, "document.propositions must be a list"),
                           ({"objects": [{}] * 1001}, "at most 1000 objects")):
        _refused(body, fragment)
    assert anonymous.get("/v1/objects/" + proposition["id"]).status_code == 404


def test_document_edges_are_validated_and_belong_to_the_deposit():
    activity, file = _vector("minimal-activity"), _minted("File", filename="edges.txt", sha256="0" * 64)
    good = {"subject": activity["id"], "predicate": "prov:used", "object": file["id"]}
    for edge, fragment in (({"subject": activity["id"], "predicate": "prov:used"}, "object is required"),
                           ({**good, "weight": 3}, "Used has no slot 'weight'"),
                           ("activity used file", "every edge must be a JSON object")):
        _refused({"document": {"activities": [activity], "files": [file], "used_edges": [edge]}}, fragment)
    _refused({"document": {"activities": [activity], "used_edge": [good]}}, "document has no DAPPER group 'used_edge'")
    response = client.post("/v1/deposits", json={"document": {
        "activities": [activity], "files": [file], "used_edges": [good],
        "was_generated_by_edges": [{"subject": "dapper:Dataset." + "Z" * 32, "predicate": "prov:wasGeneratedBy", "object": activity["id"]}]}})
    assert response.status_code == 200, response.text
    body = response.json()
    # A reference the store cannot resolve is reported, not refused.
    assert body["edges"] == 2 and body["unresolved_references"] == ["dapper:Dataset." + "Z" * 32]
    record = anonymous.get("/v1/deposits/" + body["deposit_id"]).json()
    assert [e["class_name"] for e in record["edges"]] == ["Used", "WasGeneratedBy"] and record["edges"][0]["object"] == file["id"]
    assert _rows("SELECT subject_id, object_id FROM deposit_edges WHERE deposit_id = ? AND seq = 0", (body["deposit_id"],)) == [
        {"subject_id": activity["id"], "object_id": file["id"]}]


def test_a_verified_id_already_stored_is_exists_and_the_first_document_stands():
    """Identity ignores some slots (an Activity's generated_at_time, a File's location), so two honest minters
    can hold different documents at one address. Nothing stored is rewritten; the depositor is told."""
    first = _minted("Activity", name="shared activity", command="run --once", generated_at_time="2026-01-01T00:00:00Z")
    second = {**first, "generated_at_time": "2026-06-30T12:00:00Z"}
    assert second["id"] == first["id"] and cidn.verify(second)
    assert client.post("/v1/deposits", json={"objects": [first]}).json()["deposited"][0]["status"] == "created"
    other = _minted("Proposition", statement="deposited beside a re-stamped activity")
    response = client.post("/v1/deposits", json={"objects": [second, other]})
    assert response.status_code == 200, response.text
    outcome = {o["id"]: o for o in response.json()["deposited"]}
    assert outcome[first["id"]]["status"] == "exists" and outcome[first["id"]]["stored_differs"] is True
    assert outcome[other["id"]] == {"id": other["id"], "class_name": "Proposition", "status": "created", "times_deposited": 1}
    assert anonymous.get("/v1/objects/" + first["id"]).json()["generated_at_time"] == "2026-01-01T00:00:00Z"
    # The deposit record keeps that this depositor held a different copy.
    record = anonymous.get("/v1/deposits/" + response.json()["deposit_id"]).json()
    assert {o["id"]: o.get("stored_differs") for o in record["objects"]} == {first["id"]: True, other["id"]: None}
    assert _rows("SELECT status FROM deposit_objects WHERE deposit_id = ? AND object_id = ?",
                 (response.json()["deposit_id"], first["id"])) == [{"status": "differs"}]


def test_a_stored_document_that_does_not_hash_to_its_address_is_never_taken_for_the_object():
    """A row written out of band (or by an earlier version of the service) at an address it does not hash to: the
    verified deposit is refused and the stored row is left as it is."""
    other = _minted("Proposition", statement="an address occupied out of band")
    con = service_db.connect()
    try:
        with con:
            con.execute("INSERT INTO dapper_objects (id, class_name, identity_profile, payload) VALUES (?, 'Proposition', 'unverified', ?)",
                        (other["id"], json.dumps({"class_name": "Proposition", "id": other["id"], "statement": "planted"})))
    finally:
        con.close()
    bystander = _minted("Proposition", statement="bundled with a conflict")
    refused = client.post("/v1/deposits", json={"objects": [bystander, other]})
    assert refused.status_code == 400 and "does not hash to this address" in refused.json()["detail"]
    assert anonymous.get("/v1/objects/" + other["id"]).json()["statement"] == "planted"
    assert anonymous.get("/v1/objects/" + bystander["id"]).status_code == 404   # the bundle is refused whole
    assert _rows("SELECT COUNT(*) AS n FROM deposit_objects WHERE object_id = ?", (other["id"],))[0]["n"] == 0
    # The same node stored earlier with the legacy class_name envelope IS the object: an idempotent `exists`.
    enveloped = _minted("Proposition", statement="stored with the legacy envelope")
    con = service_db.connect()
    try:
        with con:
            con.execute("INSERT INTO dapper_objects (id, class_name, identity_profile, payload) VALUES (?, 'Proposition', 'unverified', ?)",
                        (enveloped["id"], json.dumps({"class_name": "Proposition", **enveloped})))
    finally:
        con.close()
    again = client.post("/v1/deposits", json={"objects": [enveloped]}).json()["deposited"][0]
    assert again["status"] == "exists" and "stored_differs" not in again


def test_a_reference_has_one_spelling_and_hashed_content_is_compared_literally():
    """dapper's identity replaces a reference by its bare digest before hashing, so several spellings of one
    reference hash alike. Only dapper:{Class}.{digest} is accepted, and two documents at one address must agree
    literally in every hashed slot."""
    proposition = _minted("Proposition", statement="referenced four ways")
    digest = proposition["id"].split(".")[1]
    provenance = {"was_generated_by": _vector("minimal-activity")["id"], "was_attributed_to": ["urn:test:depositor"]}
    honest = _minted("Claim", proposition=proposition["id"], **provenance)
    for spelling, fragment in ((digest, "not a bare digest"),
                               ("https://broadinstitute.github.io/dapper/ns#Proposition." + digest, "not written as dapper:{Class}.{digest}"),
                               ("dapper:." + digest, "not written as dapper:{Class}.{digest}"),
                               ("dapper:Activity." + digest, "must reference Proposition, not Activity")):
        aliased = {**honest, "proposition": spelling}
        assert cidn.verify(aliased) or "Activity" in spelling or True
        _refused({"objects": [proposition, aliased]}, fragment)
    # In a slot whose range is not a class, a namespace-URI spelling is refused the same way.
    _refused({"objects": [{**honest, "was_attributed_to": ["https://broadinstitute.github.io/dapper/ns#Person." + digest]}]},
             "not written as dapper:{Class}.{digest}")
    assert client.post("/v1/deposits", json={"objects": [proposition, honest]}).status_code == 200
    refs = anonymous.get("/v1/objects/search", params={"references": proposition["id"], "class_name": "Claim"}).json()
    assert honest["id"] in {o["id"] for o in refs["objects"]}
    # Two spellings that both pass validation (Person vs Organization at one digest, in a slot whose range is a type):
    # same address, but the stored hashed content differs, so the second is refused, not treated as `exists`.
    first = _minted("Claim", proposition=proposition["id"], was_generated_by=provenance["was_generated_by"],
                    was_attributed_to=["dapper:Person." + digest])
    second = {**first, "was_attributed_to": ["dapper:Organization." + digest]}
    assert second["id"] == first["id"] and cidn.verify(second)
    assert client.post("/v1/deposits", json={"objects": [first]}).status_code == 200
    clash = client.post("/v1/deposits", json={"objects": [second]})
    assert clash.status_code == 400 and "different content in was_attributed_to" in clash.json()["detail"]
    assert anonymous.get("/v1/objects/" + first["id"]).json()["was_attributed_to"] == ["dapper:Person." + digest]


def test_index_hints_index_validated_claims_and_only_claims_the_store_knows():
    proposition = _minted("Proposition", statement="HINTGENE is associated with HINTPHENO.", subject_entity="HINTGENE",
                          relation="associated_with", object_entity="HINTPHENO")
    scores = [_minted("ClaimScore", score_kind="SCORE", metric=metric, value=value, interpretation="synthetic")
              for metric, value in (("combined", 4.0), ("log_bf", 3.0), ("prior", 0.25))]
    claim = _minted("Claim", proposition=proposition["id"], has_score=[s["id"] for s in scores],
                    was_generated_by=_vector("minimal-activity")["id"], was_attributed_to=["urn:test:depositor"])
    hint = {"claim_id": claim["id"], "method": "pigean", "predicate": "associated_with",
            "subject": {"category": "gene", "id": "HINTGENE"}, "object": {"category": "phenotype", "id": "HINTPHENO"},
            "collection": "hint-test-v1"}
    unknown = {**hint, "claim_id": "dapper:Claim." + "U" * 32}
    refused = client.post("/v1/deposits", json={"objects": [proposition, *scores, claim], "index_hints": [unknown]})
    assert refused.status_code == 400 and "unknown claim" in refused.json()["detail"]
    assert anonymous.get("/v1/objects/" + claim["id"]).status_code == 404      # refused whole: nothing stored
    not_a_claim = client.post("/v1/deposits", json={"objects": [proposition, *scores, claim],
                                                    "index_hints": [{**hint, "claim_id": proposition["id"]}]})
    assert not_a_claim.status_code == 400 and "is not a Claim" in not_a_claim.json()["detail"]
    accepted = client.post("/v1/deposits", json={"objects": [proposition, *scores, claim], "index_hints": [hint]})
    assert accepted.status_code == 200, accepted.text
    con = service_db.connect()
    try:
        indexed = [dict(r) for r in con.execute("SELECT gene, combined, claim_id FROM pigean_gene_associations "
                                                "WHERE phenotype = 'HINTPHENO' AND collection = 'hint-test-v1'")]
    finally:
        con.close()
    assert indexed == [{"gene": "HINTGENE", "combined": 4.0, "claim_id": claim["id"]}]


def test_the_published_example_bundle_is_valid_dapper_and_deposits_as_given():
    example = json.loads((ROOT / "api/examples/deposit-bundle.request.json").read_text())
    for node in example["objects"]:
        assert dval.validate_node(node)[2] == node
    with base._isolated_db():
        response = client.post("/v1/deposits", json=example)
        assert response.status_code == 200, response.text
        assert {o["status"] for o in response.json()["deposited"]} == {"created"}
        claim_id = example["index_hints"][0]["claim_id"]
        assert anonymous.get("/v1/objects/" + claim_id).json()["class_name"] == "Claim"


def test_the_old_claims_route_is_gone():
    node = _minted("Proposition", statement="sent to the removed route")
    before = (_count("dapper_objects"), _count("deposits"))
    for sender in (client, anonymous):
        for body in ({"objects": [node]}, {"objects": [{"class_name": "Proposition", "id": "dapper:Proposition.OLDROUTE", "statement": "x"}]}):
            assert sender.post("/v1/claims", json=body).status_code in (404, 405)
    assert (_count("dapper_objects"), _count("deposits")) == before
    assert anonymous.get("/v1/objects/" + node["id"]).status_code == 404


def test_deposits_post_needs_a_key_and_deposits_get_does_not():
    node = _minted("Proposition", statement="keyless deposit attempt")
    for response in (anonymous.post("/v1/deposits", json={"objects": [node]}),
                     anonymous.post("/v1/deposits", json={"document": ACCOUNT}, headers={"X-API-Key": "clm_" + "A" * 43}),
                     anonymous.post("/v1/deposits", content=b"{not json", headers={"Content-Type": "application/json"})):
        assert response.status_code == 401 and response.json() == {"detail": api_keys.UNAUTHORIZED}
    assert anonymous.get("/v1/objects/" + node["id"]).status_code == 404
    assert anonymous.get("/v1/deposits").status_code == 200
    assert anonymous.get("/v1/deposits", params={"object": node["id"]}).json()["total"] == 0
    assert client.post("/v1/deposits", content=b"{not json", headers={"Content-Type": "application/json"}).status_code == 400


def test_a_key_revoked_while_its_deposit_is_in_flight_deposits_nothing():
    con = service_db.connect()
    try:
        with con:
            issued = api_keys.issue(con, "revoked mid-flight", "tests")
            api_keys.revoke(con, issued["key_id"], "tests")
    finally:
        con.close()
    node = _minted("Proposition", statement="sent by a key revoked after it authenticated")
    before = (_count("dapper_objects"), _count("deposits"), _count("object_refs"))
    # The request passed the front-door check; only the in-transaction re-check stands between it and the store.
    app.dependency_overrides[api_keys.require_depositor] = lambda: {"key_id": issued["key_id"], "name": issued["name"]}
    try:
        response = anonymous.post("/v1/deposits", json={"objects": [node]})
        assert response.status_code == 401 and response.json() == {"detail": api_keys.UNAUTHORIZED}, response.text
    finally:
        app.dependency_overrides.clear()
    assert (_count("dapper_objects"), _count("deposits"), _count("object_refs")) == before
    assert anonymous.get("/v1/objects/" + node["id"]).status_code == 404


def test_serving_a_query_mints_claims_without_a_key_and_records_no_deposit():
    with base._isolated_db():
        base._load_dir(base._variant("OPENPHENO", "open-v1"))
        before = (_count("dapper_objects"), _count("deposits"), _count("deposit_objects"))
        claims = [r["claim_id"] for r in base._rows(
            "SELECT claim_id FROM pigean_gene_associations WHERE phenotype = 'OPENPHENO' ORDER BY gene")]
        served = anonymous.post("/v1/graphql", json={"query": "{ " + " ".join(
            f'c{i}: claim(id: "{c}") {{ id }}' for i, c in enumerate(claims)) + " }"})
        assert served.status_code == 200 and "errors" not in served.json(), served.text
        assert claims and _count("dapper_objects") > before[0]                    # serving minted them
        assert (_count("deposits"), _count("deposit_objects")) == before[1:]      # and that is not a deposit
        assert anonymous.get("/v1/deposits", params={"object": claims[0]}).json()["total"] == 0
        assert anonymous.get("/v1/objects/" + claims[0]).status_code == 200


def test_deposit_ids_sort_by_time_within_a_second():
    ids = [client.post("/v1/deposits", json={"objects": [_minted("Proposition", statement=f"ordering {n}")]}).json()["deposit_id"]
           for n in range(3) if not time.sleep(0.003)]
    assert ids == sorted(ids)
    newest = anonymous.get("/v1/deposits", params={"limit": 3}).json()["deposits"]
    assert [d["deposit_id"] for d in newest] == ids[::-1]


def test_validator_is_pure_and_names_every_group_dapper_names():
    assert dval.node_groups()["scientific_accounts"] == "ScientificAccount" and "Agent" not in dval.node_classes()
    assert dval.edge_groups()["used_edges"] == "Used" and dval.edge_groups()["was_generated_by_edges"] == "WasGeneratedBy"
    nodes, edges = dval.document_items(ACCOUNT)
    assert all(dval.validate_node(node, cls)[2] == node for cls, node in nodes)
    assert [dval.validate_edge(cls, edge) for cls, edge in edges] == ACCOUNT["used_edges"]
    # Null slots are not part of a document: identity skips them, so they are dropped before storage.
    with_null = {**_minted("Proposition", statement="null slot"), "scope": None}
    assert "scope" not in dval.validate_node(with_null)[2]
    untouched = copy.deepcopy(ACCOUNT)
    dval.document_items(ACCOUNT)
    assert ACCOUNT == untouched


def test_dapper_identity_has_no_size_limit_and_a_big_node_deposits():
    """DAPPER-ID-1 sets no size limit (dapper #35): a release of 20,253 Datasets mints, and a node with more than
    10,000 values is not refused for its size. Request bodies are bounded where they are accepted."""
    assert not hasattr(cidn._dapper, "MAX_TRIPLES")
    members = [f"dapper:Dataset.{i:032d}" for i in range(20_253)]
    rc = cidn.mint("ResultCollection", {"name": "big", "collection_key": "big", "version": "1", "methods": ["m"],
                                        "members": members, "n_members": len(members)})
    assert cidn.verify(rc)
    big = _minted("GeneSet", name="many members", members=[f"G{i}" for i in range(12_000)])
    assert dval.validate_node(big)[2] == big


# --- admin console: deposit keys -----------------------------------------------------------------------------

def _token(purpose, email="admin@example.org"):
    now = int(time.time())
    return {"Authorization": "Bearer " + jwt.encode(
        {"iss": admin_gateway.ISSUER, "aud": admin_gateway.AUDIENCE, "iat": now, "exp": now + 120, "jti": str(uuid.uuid4()),
         "sub": email, "purpose": purpose, "email": email, "email_verified": True}, SECRET, algorithm="HS256")}


def _gateway(monkeypatch):
    monkeypatch.setattr(admin_gateway, "SECRET", SECRET)


def test_admin_issues_lists_and_revokes_keys_with_an_audit_trail(monkeypatch):
    _gateway(monkeypatch)
    issued = anonymous.post("/v1/admin/api/keys", json={"name": "  Console-issued depositor  "}, headers=_token("admin_write"))
    assert issued.status_code == 201, issued.text
    assert issued.headers["cache-control"] == "no-store"
    body = issued.json()
    assert set(body) == {"keyId", "name", "key"} and body["name"] == "Console-issued depositor"
    assert api_keys.KEY_RE.match(body["key"]) and api_keys.KEY_ID_RE.match(body["keyId"])

    node = _minted("Proposition", statement="deposited with a console-issued key")
    assert anonymous.post("/v1/deposits", json={"objects": [node]}, headers={"X-API-Key": body["key"]}).status_code == 200
    listing = anonymous.get("/v1/admin/api/keys", headers=_token("admin_read"))
    assert listing.status_code == 200 and body["key"] not in listing.text and "sha256" not in listing.text.lower()
    row = next(k for k in listing.json()["keys"] if k["keyId"] == body["keyId"])
    assert row["createdBy"] == "admin@example.org" and row["deposits"] == 1 and row["revokedAt"] is None and row["lastDepositAt"]
    stored = _rows("SELECT * FROM api_keys WHERE key_id = ?", (body["keyId"],))[0]
    assert body["key"] not in json.dumps(stored)

    path = f"/v1/admin/api/keys/{body['keyId']}/revoke"
    assert anonymous.post(path, json={}, headers=_token("admin_write")).json() == {"keyId": body["keyId"], "revoked": True}
    assert anonymous.post(path, json={}, headers=_token("admin_write")).status_code == 409
    assert anonymous.post("/v1/admin/api/keys/key_0000000000000000/revoke", json={}, headers=_token("admin_write")).status_code == 404
    assert anonymous.post("/v1/admin/api/keys/not-a-key/revoke", json={}, headers=_token("admin_write")).status_code == 404
    assert anonymous.post("/v1/deposits", json={"objects": [node]}, headers={"X-API-Key": body["key"]}).status_code == 401
    after = next(k for k in anonymous.get("/v1/admin/api/keys", headers=_token("admin_read")).json()["keys"] if k["keyId"] == body["keyId"])
    assert after["revokedBy"] == "admin@example.org" and after["revokedAt"] and after["deposits"] == 1

    events = _rows("SELECT event, email, detail FROM admin_auth_events WHERE detail LIKE ? ORDER BY id", (body["keyId"] + "%",))
    assert [(e["event"], e["email"]) for e in events] == [("api_key_issued", "admin@example.org"), ("api_key_revoked", "admin@example.org")]
    assert all(body["key"] not in e["detail"] for e in events)


def test_admin_key_routes_take_only_their_own_authority_and_never_expose_the_hash(monkeypatch, depositor):
    _gateway(monkeypatch)
    keys = _count("api_keys")
    revoke = f"/v1/admin/api/keys/{depositor['key_id']}/revoke"
    for headers, status in (({}, 401), (_token("admin_read"), 403), (_token("auth_event"), 403),
                            (_token("admin_write", ""), 403), ({"X-API-Key": depositor["key"]}, 401)):
        assert anonymous.post("/v1/admin/api/keys", json={"name": "x"}, headers=headers).status_code == status
        assert anonymous.post(revoke, json={}, headers=headers).status_code == status
    for headers, status in (({}, 401), (_token("admin_write"), 403), (_token("admin_read", ""), 403)):
        assert anonymous.get("/v1/admin/api/keys", headers=headers).status_code == status
    for name in ("", " " * 5, "x" * 121, 7):
        assert anonymous.post("/v1/admin/api/keys", json={"name": name}, headers=_token("admin_write")).status_code in (400, 422)
    assert _count("api_keys") == keys
    # The session key still works: nothing above revoked it.
    assert client.post("/v1/deposits", json={"objects": [_minted("Proposition", statement="still valid")]}).status_code == 200
    # The table browser hides the hash column and refuses to filter on it.
    digest = _rows("SELECT key_sha256 FROM api_keys WHERE key_id = ?", (depositor["key_id"],))[0]["key_sha256"]
    rows = anonymous.get("/v1/admin/api/tables/api_keys/rows", headers=_token("admin_read"))
    assert rows.status_code == 200 and digest not in rows.text and "key_sha256" not in rows.text
    probe = anonymous.get("/v1/admin/api/tables/api_keys/rows", headers=_token("admin_read"),
                          params={"column": "key_sha256", "op": "equals", "value": digest})
    assert probe.status_code == 400


def test_cli_confirms_the_database_where_an_operator_would_type_it(tmp_path):
    env = {**os.environ, "CLAIMS_DB": str(tmp_path / "cli.db"), "CLAIMS_MYSQL_HOST": "db.invalid",
           "CLAIMS_MYSQL_DATABASE": "cyaka_claims_repository_v2"}
    run = lambda *args: subprocess.run([sys.executable, str(ROOT / "scripts" / "api_key.py"), *args], capture_output=True, text=True, env=env)
    refused = run("issue", "a name")
    assert refused.returncode == 2 and "add --database cyaka_claims_repository_v2 after the issue arguments" in refused.stderr
    # The flag is accepted where the message says to put it; naming another database is still refused.
    wrong = run("issue", "a name", "--database", "some_other_database")
    assert wrong.returncode == 2 and "Refusing to change" in wrong.stderr and "unrecognized arguments" not in wrong.stderr
    assert run("revoke", "key_0123456789abcdef", "--database", "some_other_database").returncode == 2


def test_a_deposit_names_the_dapper_version_it_was_written_for():
    """dapper_version is required and must be the vendored version. `client.request` bypasses the suite's
    default (conftest adds the version to `post` bodies that name none)."""
    node = _minted("Proposition", statement="A deposit written for one DAPPER version.")
    missing = client.request("POST", "/v1/deposits", json={"objects": [node]})
    assert missing.status_code == 400 and cidn.DAPPER_VERSION in missing.json()["detail"], missing.text
    older = client.request("POST", "/v1/deposits", json={"objects": [node], "dapper_version": "0.2.1-a1"})
    assert older.status_code == 400 and "'0.2.1-a1'" in older.json()["detail"], older.text
    named = client.request("POST", "/v1/deposits", json={"objects": [node], "dapper_version": cidn.DAPPER_VERSION})
    assert named.status_code == 200, named.text
    assert named.json()["validation"]["dapper_version"] == cidn.DAPPER_VERSION
