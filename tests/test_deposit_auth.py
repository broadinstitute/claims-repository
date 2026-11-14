"""Deposit keys: POST /v1/deposits needs a depositor's X-API-Key, every accepted
bundle is recorded as that depositor's deposit, and reads stay open."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import jwt
from fastapi.testclient import TestClient

from service import admin_gateway, api_keys
from service import db as service_db
from service.app import app
from service.collections import identity as cidn

ROOT = Path(__file__).resolve().parent.parent
client = TestClient(app)        # conftest gives this one the session's deposit key
anonymous = TestClient(app)     # and this one stays without


ACTIVITY = cidn.mint("Activity", {"name": "deposit-auth test fixture"})


def _objects(tag):
    """A small self-contained bundle of real DAPPER nodes; `tag` makes its content, and so its ids, unique to one
    test. Order: [proposition, claim, activity]."""
    proposition = cidn.mint("Proposition", {"statement": f"auth fixture {tag}"})
    claim = cidn.mint("Claim", {"proposition": proposition["id"], "statement": f"auth claim {tag}",
                                "was_generated_by": ACTIVITY["id"], "was_attributed_to": ["urn:claims-repository:test:depositor"]})
    return [proposition, claim, dict(ACTIVITY)]


def _claim(tag):
    return _objects(tag)[1]["id"]


def _rows(sql, params=()):
    con = service_db.connect()
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def _issue(name):
    con = service_db.connect()
    try:
        with con:
            return api_keys.issue(con, name, "tests")
    finally:
        con.close()


def test_deposit_without_a_valid_key_is_refused_before_anything_is_read_or_stored():
    bundle = {"objects": _objects("NOKEY")}
    before = _rows("SELECT COUNT(*) AS n FROM deposits")[0]["n"]
    attempts = [
        anonymous.post("/v1/deposits", json=bundle),
        anonymous.post("/v1/deposits", json=bundle, headers={"X-API-Key": ""}),
        anonymous.post("/v1/deposits", json=bundle, headers={"X-API-Key": "clm_" + "A" * 43}),
        anonymous.post("/v1/deposits", json=bundle, headers={"X-API-Key": "not a key"}),
        # The key belongs in X-API-Key; a bearer token or a query parameter is not one.
        anonymous.post("/v1/deposits", json=bundle, headers={"Authorization": "Bearer " + client.headers["X-API-Key"]}),
        anonymous.post("/v1/deposits?X-API-Key=" + client.headers["X-API-Key"], json=bundle),
        # Authentication comes first: a body that would be a 400 is still a 401 without a key.
        anonymous.post("/v1/deposits", content=b"{not json", headers={"Content-Type": "application/json"}),
        anonymous.post("/v1/deposits", json={"objects": []}),
    ]
    for response in attempts:
        assert response.status_code == 401, response.text
        assert response.json() == {"detail": api_keys.UNAUTHORIZED}
        assert response.headers["www-authenticate"] == "ApiKey"
    assert anonymous.get("/v1/objects/" + _claim("NOKEY")).status_code == 404
    assert _rows("SELECT COUNT(*) AS n FROM deposits")[0]["n"] == before
    # With the key the same malformed body is the ordinary 400.
    assert client.post("/v1/deposits", content=b"{not json", headers={"Content-Type": "application/json"}).status_code == 400


def test_a_deposit_is_recorded_with_its_depositor_and_every_object(depositor):
    first = client.post("/v1/deposits", json={"objects": _objects("REC")})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["depositor"] == {"key_id": depositor["key_id"], "name": depositor["name"]}
    assert api_keys.DEPOSIT_ID_RE.match(body["deposit_id"])
    proposition, claim, activity = _objects("REC")
    assert [(o["id"], o["status"]) for o in body["deposited"]][:2] == [(proposition["id"], "created"), (claim["id"], "created")]
    # The key and its hash appear nowhere in what the service returns.
    assert depositor["key"] not in first.text and "sha256" not in first.text

    # A second depositor re-deposits the same content: it exists, and both deposits are on record.
    other = _issue("second depositor")
    again = anonymous.post("/v1/deposits", json={"objects": _objects("REC")}, headers={"X-API-Key": other["key"]})
    assert again.status_code == 200 and {o["status"] for o in again.json()["deposited"]} == {"exists"}

    listed = anonymous.get("/v1/deposits", params={"object": claim["id"]}).json()
    assert listed["total"] == 2
    assert [d["depositor"]["name"] for d in listed["deposits"]] == ["second depositor", "test-suite depositor"]   # newest first
    assert all(d["object_count"] == 3 and set(d) == {"deposit_id", "received_at", "object_count", "depositor"} for d in listed["deposits"])
    mine = anonymous.get("/v1/deposits", params={"deposited_by": other["key_id"]}).json()
    assert mine["total"] == 1 and mine["deposits"][0]["deposit_id"] == again.json()["deposit_id"]

    one = anonymous.get("/v1/deposits/" + body["deposit_id"])
    assert one.status_code == 200
    recorded = {o["id"]: (o["class_name"], o["status"]) for o in one.json()["objects"]}
    assert recorded[claim["id"]] == ("Claim", "created") and recorded[proposition["id"]] == ("Proposition", "created")
    assert [o["id"] for o in one.json()["objects"]] == sorted(recorded)
    assert one.json()["edges"] == []
    assert {o["status"] for o in anonymous.get("/v1/deposits/" + again.json()["deposit_id"]).json()["objects"]} == {"exists"}
    assert anonymous.get("/v1/deposits/dep_" + "0" * 32).status_code == 404
    assert anonymous.get("/v1/deposits/nonsense").status_code == 404
    assert anonymous.get("/v1/deposits", params={"object": "not-an-id"}).status_code == 400
    assert anonymous.get("/v1/deposits", params={"deposited_by": "someone"}).status_code == 400


def test_object_search_filters_by_depositor_and_by_deposit(depositor):
    solo = _issue("search depositor")
    sent = anonymous.post("/v1/deposits", json={"objects": _objects("SRCH")}, headers={"X-API-Key": solo["key"]}).json()
    client.post("/v1/deposits", json={"objects": _objects("OTHER")})
    by_key = anonymous.get("/v1/objects/search", params={"deposited_by": solo["key_id"]}).json()
    assert {o["id"] for o in by_key["objects"]} == {o["id"] for o in _objects("SRCH")} and by_key["total"] == 3
    claims = anonymous.get("/v1/objects/search", params={"deposited_by": solo["key_id"], "class_name": "Claim"}).json()
    assert [o["id"] for o in claims["objects"]] == [_claim("SRCH")]
    by_deposit = anonymous.get("/v1/objects/search", params={"deposit": sent["deposit_id"]}).json()
    assert by_deposit["total"] == 3
    # Depositing twice does not list an object twice.
    anonymous.post("/v1/deposits", json={"objects": _objects("SRCH")}, headers={"X-API-Key": solo["key"]})
    assert anonymous.get("/v1/objects/search", params={"deposited_by": solo["key_id"]}).json()["total"] == 3
    assert anonymous.get("/v1/objects/search", params={"deposited_by": "key_" + "0" * 16}).json()["total"] == 0
    assert anonymous.get("/v1/objects/search", params={"deposited_by": "x' OR 1=1 --"}).status_code == 400
    assert anonymous.get("/v1/objects/search", params={"deposit": "dep_1"}).status_code == 400


def test_a_rejected_bundle_records_no_deposit():
    before = _rows("SELECT COUNT(*) AS n FROM deposits")[0]["n"]
    bad = client.post("/v1/deposits", json={"objects": [*_objects("ROLLBACK"), {"class_name": "Claim", "id": "not-a-dapper-id"}]})
    assert bad.status_code == 400
    assert _rows("SELECT COUNT(*) AS n FROM deposits")[0]["n"] == before
    assert _rows("SELECT COUNT(*) AS n FROM deposit_objects WHERE object_id = ?", (_claim("ROLLBACK"),))[0]["n"] == 0
    assert anonymous.get("/v1/objects/" + _claim("ROLLBACK")).status_code == 404


def test_an_id_repeated_in_one_bundle_is_one_object_of_the_deposit():
    proposition, claim, _ = _objects("TWICE")
    response = client.post("/v1/deposits", json={"objects": [proposition, claim, proposition]})
    assert response.status_code == 200, response.text
    assert [o["status"] for o in response.json()["deposited"]] == ["created", "created", "exists"]
    record = anonymous.get("/v1/deposits/" + response.json()["deposit_id"]).json()
    assert record["object_count"] == 2 and {o["status"] for o in record["objects"]} == {"created"}
    assert response.json()["unresolved_references"] == [ACTIVITY["id"]] or ACTIVITY["id"] not in response.json()["unresolved_references"]


def test_a_revoked_key_stops_depositing_and_keeps_its_history():
    issued = _issue("short-lived depositor")
    headers = {"X-API-Key": issued["key"]}
    kept = anonymous.post("/v1/deposits", json={"objects": _objects("REVOKE")}, headers=headers)
    assert kept.status_code == 200
    con = service_db.connect()
    try:
        with con:
            assert api_keys.revoke(con, issued["key_id"], "tests") is True
            assert api_keys.revoke(con, issued["key_id"], "tests") is False   # already revoked
        assert api_keys.authenticate(con, issued["key"]) is None
        assert api_keys.still_valid(con, {"key_id": issued["key_id"]}) is False
    finally:
        con.close()
    refused = anonymous.post("/v1/deposits", json={"objects": _objects("AFTER")}, headers=headers)
    assert refused.status_code == 401 and refused.json() == {"detail": api_keys.UNAUTHORIZED}
    assert anonymous.get("/v1/objects/" + _claim("AFTER")).status_code == 404
    history = anonymous.get("/v1/deposits", params={"deposited_by": issued["key_id"]}).json()
    assert history["total"] == 1 and history["deposits"][0]["depositor"]["name"] == "short-lived depositor"


def test_reads_need_no_key():
    client.post("/v1/deposits", json={"objects": _objects("OPEN")})
    before = _rows("SELECT COUNT(*) AS n FROM deposits")[0]["n"]
    opened = _claim("OPEN")
    for path in ("/v1/health", "/v1/objects", "/v1/objects/search?q=auth", "/v1/objects/" + opened, "/v1/deposits"):
        assert anonymous.get(path).status_code == 200, path
    served = anonymous.post("/v1/graphql", json={"query": f'{{ claim(id: "{opened}") {{ id timesDeposited }} }}'})
    assert served.status_code == 200 and served.json()["data"]["claim"]["id"] == opened
    assert _rows("SELECT COUNT(*) AS n FROM deposits")[0]["n"] == before


def test_only_the_hash_is_stored_and_the_admin_table_browser_never_serves_it(monkeypatch, depositor):
    stored = _rows("SELECT * FROM api_keys WHERE key_id = ?", (depositor["key_id"],))[0]
    assert depositor["key"] not in json.dumps(stored) and len(stored["key_sha256"]) == 64
    monkeypatch.setattr(admin_gateway, "SECRET", "s" * 40)
    monkeypatch.setattr(admin_gateway, "ADMIN_EMAILS", {"admin@example.org"})
    now = int(time.time())
    token = jwt.encode({"iss": admin_gateway.ISSUER, "aud": admin_gateway.AUDIENCE, "sub": "admin", "email": "admin@example.org",
                        "email_verified": True, "purpose": "admin_read", "iat": now, "exp": now + 120, "jti": "t"},
                       "s" * 40, algorithm="HS256")
    page = anonymous.get("/v1/admin/api/tables/api_keys/rows", headers={"Authorization": "Bearer " + token})
    assert page.status_code == 200, page.text
    assert stored["key_sha256"] not in page.text and "key_sha256" not in page.text
    assert depositor["key_id"] in page.text and depositor["name"] in page.text


def test_issue_validates_names_and_key_ids():
    con = service_db.connect()
    try:
        for name in ("", "   ", "x" * 121, "line\nbreak", None, 7):
            try:
                api_keys.issue(con, name)
            except ValueError:
                continue
            raise AssertionError(f"accepted name {name!r}")
        try:
            api_keys.revoke(con, "key_1' OR 1=1")
            raise AssertionError("accepted a malformed key id")
        except ValueError:
            pass
        con.rollback()
        for value in (None, "", "clm_short", "CLM_" + "a" * 43, "clm_" + "a" * 44, 5, b"clm_" + b"a" * 43):
            assert api_keys.authenticate(con, value) is None
    finally:
        con.close()


def test_cli_issues_lists_and_revokes_against_its_own_database(tmp_path):
    env = {**os.environ, "CLAIMS_DB": str(tmp_path / "cli.db")}
    env.pop("CLAIMS_MYSQL_HOST", None)
    run = lambda *args: subprocess.run([sys.executable, str(ROOT / "scripts" / "api_key.py"), *args],
                                       capture_output=True, text=True, env=env)
    assert "No keys" in run("list").stdout
    issued = run("issue", "CLI depositor", "--by", "ops@example.org")
    assert issued.returncode == 0, issued.stderr
    fields = dict(line.split(":", 1) for line in issued.stdout.strip().splitlines())
    key_id, key = fields["key_id"].strip(), fields["key"].strip()
    assert api_keys.KEY_ID_RE.match(key_id) and api_keys.KEY_RE.match(key)
    listing = run("list").stdout
    assert key_id in listing and "CLI depositor" in listing and "active" in listing and key not in listing
    assert run("issue", "  ").returncode == 2
    assert run("revoke", key_id, "--by", "ops@example.org").stdout.strip() == f"revoked {key_id}"
    assert run("revoke", key_id).returncode == 1
    assert "revoked" in run("list").stdout
    # The session database never saw the CLI's key.
    assert _rows("SELECT COUNT(*) AS n FROM api_keys WHERE key_id = ?", (key_id,))[0]["n"] == 0
    # Aimed at MySQL by environment alone, a write is refused before any connection is attempted.
    aimed = subprocess.run([sys.executable, str(ROOT / "scripts" / "api_key.py"), "issue", "x"], capture_output=True, text=True,
                           env={**env, "CLAIMS_MYSQL_HOST": "db.invalid", "CLAIMS_MYSQL_DATABASE": "cyaka_claims_repository_v2"})
    assert aimed.returncode == 2 and "--database cyaka_claims_repository_v2" in aimed.stderr
