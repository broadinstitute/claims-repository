"""The admin API's object browser (service/admin.py): every stored object of any class as stored, with what it
references, what references it, its deposited edges and its deposits; and a ScientificAccount assembled with its
question, claims, propositions, evidence and quoted files. Reads only: nothing is minted or asserted."""
from __future__ import annotations

import json
import tempfile
import time
import uuid
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient

from service import admin_gateway
from service import db as service_db
from service.app import app

ROOT = Path(__file__).resolve().parent.parent
# A literature account as a REVEAL agent job deposits it: statement-only propositions, located quotes, files, edges.
ACCOUNT = json.loads((ROOT / "tests/fixtures/deposits/reveal-literature-account.dapper.json").read_text())
DOCUMENT = ACCOUNT["document"]
# A research account, whose propositions carry subject, relation and object.
RESEARCH = json.loads((ROOT / "tests/fixtures/deposits/reveal-scientific-account.dapper.json").read_text())
SECRET = "test-gateway-secret-at-least-32-characters"
ADMIN = "admin@example.org"
client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def isolated_db():
    saved = service_db.DB_PATH
    service_db.DB_PATH = str(Path(tempfile.mkdtemp()) / "admin-objects.db")
    yield service_db.DB_PATH
    service_db.DB_PATH = saved


@pytest.fixture(autouse=True)
def gateway(monkeypatch):
    monkeypatch.setattr(admin_gateway, "SECRET", SECRET)


@pytest.fixture(scope="module")
def deposited(isolated_db):
    reply = client.post("/v1/deposits", json=ACCOUNT)
    assert reply.status_code == 200, reply.text
    return reply.json()


def _get(path, **params):
    now = int(time.time())
    token = jwt.encode({"iss": "claims-admin", "aud": "claims-api", "iat": now, "exp": now + 120, "jti": str(uuid.uuid4()),
                        "sub": ADMIN, "purpose": "admin_read", "email": ADMIN, "email_verified": True}, SECRET, algorithm="HS256")
    return client.get(f"/v1/admin/api/{path}", params=params, headers={"Authorization": f"Bearer {token}"})


def _state():
    con = service_db.connect()
    try:
        return [tuple(r) for r in con.execute("SELECT id, times_seen, last_seen_at FROM dapper_objects ORDER BY id").fetchall()]
    finally:
        con.close()


def _nodes():
    return [(name, node) for name, items in DOCUMENT.items() if not name.endswith("_edges") for node in items]


def test_every_route_needs_the_console_assertion(deposited):
    account_id = DOCUMENT["scientific_accounts"][0]["id"]
    for path in ("objects/classes", "objects", f"objects/{account_id}", "deposits", "accounts", f"accounts/{account_id}"):
        assert client.get(f"/v1/admin/api/{path}").status_code == 401, path


def test_classes_count_every_stored_object_and_every_deposited_edge(deposited):
    found = _get("objects/classes").json()
    counts = {c["className"]: c["count"] for c in found["classes"]}
    assert found["total"] == sum(counts.values()) == len(_nodes())
    assert counts["ScientificAccount"] == 1 and counts["Proposition"] == len(DOCUMENT["propositions"])
    assert sum(c["count"] for c in found["edgeClasses"]) == sum(len(v) for k, v in DOCUMENT.items() if k.endswith("_edges"))
    assert found["deposits"] == 1


def test_objects_list_filters_by_class_text_deposit_reference_and_pasted_id(deposited):
    everything = _get("objects", page_size=100).json()
    assert everything["total"] == len(_nodes()) and {r["id"] for r in everything["rows"]} == {n["id"] for _, n in _nodes()}
    account = DOCUMENT["scientific_accounts"][0]
    row = _get("objects", class_name="ScientificAccount").json()["rows"][0]
    assert row["id"] == account["id"] and row["className"] == "ScientificAccount" and row["timesSeen"] == 1
    assert row["label"] == (account.get("name") or account["context"]) and "component_claims" in row["slots"] and "id" not in row["slots"]
    proposition = DOCUMENT["propositions"][0]
    assert [r["id"] for r in _get("objects", q=proposition["statement"][:25]).json()["rows"]].count(proposition["id"]) == 1
    assert _get("objects", deposit=deposited["deposit_id"], page_size=100).json()["total"] == len(_nodes())
    referring = {r["id"] for r in _get("objects", references=proposition["id"]).json()["rows"]}
    assert next(c["id"] for c in DOCUMENT["claims"] if c["proposition"] == proposition["id"]) in referring
    assert [r["id"] for r in _get("objects", q=account["id"]).json()["rows"]] == [account["id"]]
    assert _get("objects", q="dapper:Claim.notStoredAnywhere").json() == {"total": 0, "page": 1, "pageSize": 25, "rows": []}
    assert _get("objects", class_name="not a class").status_code == 422
    assert _get("objects", references="not-an-id").status_code == 400


def test_object_detail_is_the_stored_document_with_both_directions_of_reference(deposited):
    claim = DOCUMENT["claims"][0]
    d = _get(f"objects/{claim['id']}").json()
    assert d["stored"] is True and d["className"] == "Claim" and d["document"] == claim and d["timesSeen"] == 1
    slots = {(r["slot"], r["id"]) for r in d["references"]}
    assert ("proposition", claim["proposition"]) in slots and all(("has_evidence", e) in slots for e in claim["has_evidence"])
    assert all(r["stored"] and r["className"] for r in d["references"])
    assert DOCUMENT["scientific_accounts"][0]["id"] in {o["id"] for o in d["referencedBy"]["objects"]}
    assert d["deposits"][0]["depositId"] == deposited["deposit_id"] and d["deposits"][0]["status"] == "created"
    assert d["resolverUrl"].endswith("/v1/objects/" + claim["id"])
    proposition = _get(f"objects/{claim['proposition']}").json()
    assert proposition["triple"] is None and set(proposition["document"]) == {"id", "statement"}


def test_edges_are_shown_on_the_objects_they_join(deposited):
    edge = DOCUMENT["was_derived_from_edges"][0]
    for end in ("subject", "object"):
        shown = _get(f"objects/{edge[end]}").json()["edges"]
        match = [e for e in shown if e["subject"] == edge["subject"] and e["object"] == edge["object"]]
        assert len(match) == 1 and match[0]["predicate"] == edge["predicate"] and match[0]["depositIds"] == [deposited["deposit_id"]]
        assert match[0]["subjectSummary"]["stored"] and match[0]["objectSummary"]["className"] == "File"


def test_an_id_that_is_only_referenced_is_reported_as_not_stored(deposited):
    con = service_db.connect()
    try:
        missing = "dapper:Publication.neverDepositedHere"
        con.execute(con.dialect.insert_ref_sql() if hasattr(con.dialect, "insert_ref_sql") else
                    "INSERT OR IGNORE INTO object_refs (from_id, to_id) VALUES (?, ?)", (DOCUMENT["claims"][0]["id"], missing))
        con.commit()
    finally:
        con.close()
    d = _get(f"objects/{missing}").json()
    assert d["stored"] is False and d["document"] is None and d["className"] == "Publication"
    assert d["referencedBy"]["total"] == 1 and d["resolverUrl"] is None
    assert _get("objects/dapper:Claim.nobodyKnowsThis").status_code == 404
    assert _get("objects/not-an-id").status_code == 400


def test_deposits_and_accounts_lists(deposited):
    deposits = _get("deposits").json()
    assert deposits["total"] == 1 and deposits["rows"][0]["depositId"] == deposited["deposit_id"]
    assert deposits["rows"][0]["objectCount"] == len(_nodes()) and deposits["rows"][0]["depositor"]["name"]
    account = DOCUMENT["scientific_accounts"][0]
    listed = _get("accounts").json()
    assert listed["total"] == 1
    row = listed["rows"][0]
    assert row["id"] == account["id"] and row["claims"] == len(account["component_claims"]) and row["deposits"] == 1
    assert row["question"] == DOCUMENT["knowledge_gaps"][0]["text"]
    assert row["lastDeposit"]["depositId"] == deposited["deposit_id"]


def test_account_view_assembles_every_object_as_stored(deposited):
    account = DOCUMENT["scientific_accounts"][0]
    by_id = {n["id"]: n for _, n in _nodes()}
    d = _get(f"accounts/{account['id']}").json()
    assert d["document"] == account and d["question"]["document"] == by_id[account["framing"][0]]
    assert [f["id"] for f in d["framing"]] == account["framing"]
    assert d["activity"]["document"] == by_id[account["was_generated_by"]] and d["missing"] == []
    assert [c["id"] for c in d["claims"]] == account["component_claims"]
    for shown in d["claims"]:
        claim = by_id[shown["id"]]
        assert shown["document"] == claim and shown["proposition"]["document"] == by_id[claim["proposition"]]
        assert shown["triple"] is None      # these propositions carry a statement only
        assert [e["id"] for e in shown["evidence"]] == claim["has_evidence"]
        for item in shown["evidence"]:
            evidence = by_id[item["id"]]
            assert item["document"] == evidence and item["publication"]["document"] == by_id[evidence["reference"]]
            assert [q["id"] for q in item["quotes"]] == evidence["value_locators"]
            for quote in item["quotes"]:
                locator = by_id[quote["id"]]
                assert quote["document"] == locator and quote["file"]["document"] == by_id[locator["file"]]
                derived = [e["object"] for e in DOCUMENT["was_derived_from_edges"] if e["subject"] == locator["file"]]
                assert [f["id"] for f in quote["derivedFrom"]] == derived
    assert {c["className"] for c in d["objectsByClass"]} >= {"ScientificAccount", "Claim", "Proposition", "EvidenceItem", "File"}
    assert d["deposits"][0]["depositId"] == deposited["deposit_id"]
    assert _get(f"accounts/{account['component_claims'][0]}").status_code == 404
    assert _get("accounts/not-an-id").status_code == 400


def test_browsing_never_asserts_or_mints(deposited):
    before = _state()
    account_id = DOCUMENT["scientific_accounts"][0]["id"]
    for path in ("objects/classes", "objects", f"objects/{account_id}", f"objects/{DOCUMENT['claims'][0]['id']}",
                 "deposits", "accounts", f"accounts/{account_id}"):
        assert _get(path).status_code == 200, path
    assert _state() == before


def test_a_proposition_with_subject_relation_and_object_shows_its_triple(deposited):
    body = RESEARCH if "document" in RESEARCH else {"document": RESEARCH}
    assert client.post("/v1/deposits", json=body).status_code == 200
    document = body["document"]
    proposition = next(p for p in document["propositions"] if p.get("relation"))
    expected = {"subject": proposition["subject_entity"], "predicate": proposition["relation"], "object": proposition["object_entity"]}
    assert _get(f"objects/{proposition['id']}").json()["triple"] == expected
    account = _get(f"accounts/{document['scientific_accounts'][0]['id']}").json()
    assert expected in [c["triple"] for c in account["claims"]]
    listed = _get("accounts").json()
    assert listed["total"] == 2 and _get("deposits").json()["total"] == 2
    person = document["persons"][0]
    row = next(r for r in listed["rows"] if r["id"] == document["scientific_accounts"][0]["id"])
    assert row["attributedTo"] == [f"{person['given_name']} {person['family_name']}"]
