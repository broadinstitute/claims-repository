"""Depositing into an object collection with a `claims login` instead of a deposit key (docs/depositing-objects.md).

A login (Authorization: Bearer clp_...) may open, fill and close a release of any object collection, with no key
and no grant. Its deposits and releases are recorded under its token id and email (schema 012), never a key id;
publicly they are attributed to "claims login", and the admin console shows the email. The key path is unchanged.
"""
from __future__ import annotations

import json

import tempfile
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from service import api_keys, device_login
from service import db as service_db
from service.app import app
from service.collections import identity as cidn
from service.collections import objects
from service.collections.manifest import load_collection

ROOT = Path(__file__).resolve().parent.parent
COLLECTION = ROOT / "tests" / "fixtures" / "collections" / "catalog-test"
PUBLISHER = "depositor@example.org"
# Not named `client`: conftest gives every module's `client` the session's deposit key, and a login request
# carrying a key as well is refused (400).
writer = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def isolated_db():
    saved = service_db.DB_PATH
    service_db.DB_PATH = str(Path(tempfile.mkdtemp()) / "login-deposits.db")
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


def login(email=PUBLISHER) -> dict:
    """A publisher token, issued the way `claims login` gets one: {access_token, token_id, email, ...}."""
    con = _con()
    try:
        started = device_login.start(con, "claims-cli/1", "test-runner", "34.1.2.3")
        con.commit()
        with con:
            assert device_login.decide(con, device_login.normalize_user_code(started["user_code"]), email, True)
        return device_login.exchange(con, started["device_code"])
    finally:
        con.close()


def bearer(token: dict) -> dict:
    return {"Authorization": f"Bearer {token['access_token']}"}


def _mint(cls, **content):
    return cidn.mint(cls, content)


@pytest.fixture(scope="module")
def registered(isolated_db):
    con = _con()
    try:
        return objects.register(con, load_collection(COLLECTION))
    finally:
        con.close()


@pytest.fixture(scope="module")
def token(isolated_db):
    return login()


def _admin(monkeypatch):
    import jwt

    from service import admin_gateway

    secret = "s" * 40
    monkeypatch.setattr(admin_gateway, "SECRET", secret)

    def headers(purpose):
        now = int(time.time())
        return {"Authorization": "Bearer " + jwt.encode(
            {"iss": admin_gateway.ISSUER, "aud": admin_gateway.AUDIENCE, "iat": now, "exp": now + 120,
             "jti": str(uuid.uuid4()), "sub": "admin@example.org", "purpose": purpose, "email": "admin@example.org",
             "email_verified": True}, secret, algorithm="HS256")}
    return headers


GAP = _mint("KnowledgeGap", name="Login gap", text="Deposited with a login?", gap_description="d",
            gap_kind="KNOWLEDGE_GAP", about_entities=["http://purl.obolibrary.org/obo/MONDO_0021661"])
MECH = _mint("Mechanism", name="Login mechanism", description="Deposited with a login.")


@pytest.fixture(scope="module")
def released(registered, token):
    """A release opened, filled and closed by a login: no key, no grant."""
    opened = writer.post("/v1/collections/catalog-test/releases", json={"version": "2026.12.0"}, headers=bearer(token))
    again = writer.post("/v1/collections/catalog-test/releases", json={"version": "2026.12.0"}, headers=bearer(token))
    part = writer.post("/v1/deposits", json={"document": {"knowledge_gaps": [GAP], "mechanisms": [MECH]},
                                             "collection": "catalog-test", "release": "2026.12.0"}, headers=bearer(token))
    closed = writer.post("/v1/collections/catalog-test/releases/2026.12.0:close", headers=bearer(token))
    return {"opened": opened, "again": again, "part": part, "closed": closed}


def test_a_login_opens_fills_and_closes_a_release_without_a_key_or_grant(released, token):
    assert released["opened"].status_code == 201, released["opened"].text
    assert released["again"].status_code == 200
    part = released["part"]
    assert part.status_code == 200, part.text
    receipt = part.json()
    assert receipt["depositor"] == {"kind": "login", "name": "claims login", "email": PUBLISHER,
                                    "token_id": token["token_id"]}
    assert receipt["collection"] == {"key": "catalog-test", "release": "2026.12.0", "state": "open", "objects": 2}
    closed = released["closed"]
    assert closed.status_code == 200, closed.text
    assert closed.json()["state"] == "closed" and closed.json()["objects"] == 2
    assert not _rows("SELECT 1 FROM api_key_grants WHERE collection_key = 'catalog-test'")

    deposit = _rows("SELECT * FROM deposits WHERE deposit_id = ?", (receipt["deposit_id"],))[0]
    assert deposit["key_id"] is None and deposit["publisher_token_id"] == token["token_id"]
    assert deposit["publisher_email"] == PUBLISHER and deposit["collection_key"] == "catalog-test"
    release = _rows("SELECT * FROM object_releases WHERE version = '2026.12.0'")[0]
    assert release["opened_by"] is None and release["closed_by"] is None
    assert (release["opened_by_token_id"], release["opened_by_email"]) == (token["token_id"], PUBLISHER)
    assert (release["closed_by_token_id"], release["closed_by_email"]) == (token["token_id"], PUBLISHER)
    published = _rows("SELECT published_by, publisher_token_id FROM collection_releases WHERE version = '2026.12.0'")
    assert published == [{"published_by": PUBLISHER, "publisher_token_id": token["token_id"]}]
    events = [(r["event"], r["email"]) for r in _rows(
        "SELECT event, email FROM admin_auth_events WHERE event LIKE 'object_release_%' ORDER BY id")]
    assert events == [("object_release_opened", PUBLISHER), ("object_release_closed", PUBLISHER)]


def test_login_deposits_are_public_as_claims_login_and_show_their_email_only_to_admins(released, token, monkeypatch):
    receipt = released["part"].json()
    listed = writer.get("/v1/deposits").json()["deposits"]
    mine = next(d for d in listed if d["deposit_id"] == receipt["deposit_id"])
    assert mine["depositor"] == {"kind": "login", "name": "claims login"}
    one = writer.get(f"/v1/deposits/{receipt['deposit_id']}").json()
    assert one["depositor"] == {"kind": "login", "name": "claims login"}
    assert PUBLISHER not in writer.get("/v1/deposits").text and token["token_id"] not in writer.get("/v1/deposits").text
    by_token = writer.get("/v1/deposits", params={"deposited_by": token["token_id"]}).json()
    assert [d["deposit_id"] for d in by_token["deposits"]] == [receipt["deposit_id"]]
    found = writer.get("/v1/objects/search", params={"deposited_by": token["token_id"]}).json()
    assert {o["id"] for o in found["objects"]} == {GAP["id"], MECH["id"]}
    assert writer.get("/v1/deposits", params={"deposited_by": "tok_nothex"}).status_code == 400

    admin = _admin(monkeypatch)
    rows = writer.get("/v1/admin/api/deposits", headers=admin("admin_read")).json()["rows"]
    row = next(r for r in rows if r["depositId"] == receipt["deposit_id"])
    assert row["depositor"] == {"kind": "login", "keyId": None, "name": PUBLISHER, "email": PUBLISHER,
                                "tokenId": token["token_id"]}
    detail = writer.get(f"/v1/admin/api/objects/{GAP['id']}", headers=admin("admin_read")).json()
    assert detail["deposits"][0]["depositor"]["email"] == PUBLISHER


def test_revoked_or_expired_login_tokens_are_refused_on_every_write(registered):
    revoked, expired = login("revoked@example.org"), login("expired@example.org")
    con = _con()
    try:
        with con:
            device_login.revoke(con, revoked["token_id"], "admin@example.org")
            con.execute("UPDATE publisher_tokens SET expires_at = '2020-01-01 00:00:00' WHERE token_id = ?",
                        (expired["token_id"],))
    finally:
        con.close()
    malformed = {"access_token": "clp_" + "A" * 43}
    for token in (revoked, expired, malformed):
        for response in (
            writer.post("/v1/collections/catalog-test/releases", json={"version": "2026.12.9"}, headers=bearer(token)),
            writer.post("/v1/deposits", json={"objects": [MECH]}, headers=bearer(token)),
            writer.post("/v1/collections/catalog-test/releases/2026.12.0:close", headers=bearer(token)),
        ):
            assert response.status_code == 401, response.text
            assert response.headers["www-authenticate"] == "Bearer"
            assert response.json() == {"detail": device_login.UNAUTHORIZED}
    assert not _rows("SELECT 1 FROM object_releases WHERE version = '2026.12.9'")


def test_a_key_and_a_login_together_are_refused(token, depositor):
    response = writer.post("/v1/deposits", json={"objects": [MECH]},
                           headers={**bearer(token), "X-API-Key": depositor["key"]})
    assert response.status_code == 400 and response.json() == {"detail": api_keys.BOTH_CREDENTIALS}


def test_a_key_still_needs_its_grant(registered, depositor):
    keyed = TestClient(app, headers={"X-API-Key": depositor["key"]})
    refused = keyed.post("/v1/collections/catalog-test/releases", json={"version": "2026.12.1"})
    assert refused.status_code == 403 and "grant it in the admin console" in refused.json()["detail"]
    plain = keyed.post("/v1/deposits", json={"objects": [MECH]})
    assert plain.status_code == 200 and plain.json()["depositor"] == {"key_id": depositor["key_id"],
                                                                      "name": depositor["name"]}
    row = _rows("SELECT * FROM deposits WHERE deposit_id = ?", (plain.json()["deposit_id"],))[0]
    assert row["key_id"] == depositor["key_id"] and row["publisher_token_id"] is None and row["publisher_email"] is None


def test_a_key_opened_release_records_the_key(registered, depositor):
    con = _con()
    try:
        with con:
            api_keys.grant(con, depositor["key_id"], "catalog-test", "tests")
    finally:
        con.close()
    keyed = TestClient(app, headers={"X-API-Key": depositor["key"]})
    assert keyed.post("/v1/collections/catalog-test/releases", json={"version": "2026.12.2"}).status_code == 201
    assert keyed.post("/v1/deposits", json={"objects": [MECH], "collection": "catalog-test",
                                            "release": "2026.12.2"}).status_code == 200
    assert keyed.post("/v1/collections/catalog-test/releases/2026.12.2:close").status_code == 200
    release = _rows("SELECT * FROM object_releases WHERE version = '2026.12.2'")[0]
    assert release["opened_by"] == release["closed_by"] == depositor["key_id"]
    assert release["opened_by_token_id"] is None and release["closed_by_email"] is None
    published = _rows("SELECT published_by, publisher_token_id FROM collection_releases WHERE version = '2026.12.2'")
    assert published == [{"published_by": None, "publisher_token_id": None}]


def test_every_deposit_row_names_exactly_one_writer(released, depositor):
    """The migrated schema takes a key deposit and a login deposit; the app never writes a row with neither (or
    both)."""
    keyed = TestClient(app, headers={"X-API-Key": depositor["key"]})
    assert keyed.post("/v1/deposits", json={"objects": [MECH]}).status_code == 200
    assert released["part"].status_code == 200  # the login's
    for actor in ({"kind": "login", "name": "claims login", "email": None, "token_id": None},
                  {"kind": "key", "key_id": None, "name": "x"},
                  {"kind": "key", "key_id": "key_0123456789abcdef", "token_id": "tok_0123456789abcdef"},
                  {"kind": "login", "email": "a@b.c", "token_id": "tok_0123456789abcdef", "key_id": "key_1"},
                  {"kind": "other", "key_id": "key_0123456789abcdef"}):
        con = _con()
        try:
            with pytest.raises(ValueError):
                with con:
                    api_keys.record_deposit(con, actor, [{"id": MECH["id"], "status": "exists"}])
        finally:
            con.close()
    rows = _rows("SELECT key_id, publisher_token_id, publisher_email FROM deposits")
    assert rows and all((r["key_id"] is None) != (r["publisher_token_id"] is None) for r in rows)
    assert all((r["publisher_token_id"] is None) == (r["publisher_email"] is None) for r in rows)
    kinds = {"key" if r["key_id"] else "login" for r in rows}
    assert kinds == {"key", "login"}
    assert not _rows("SELECT 1 FROM object_releases WHERE opened_by IS NULL AND opened_by_token_id IS NULL")


def test_still_valid_rechecks_a_login_for_revocation_and_expiry(token):
    actor = {"kind": "login", "name": "claims login", "email": PUBLISHER, "token_id": token["token_id"]}
    con = _con()
    try:
        assert api_keys.still_valid(con, actor)
        con.execute("UPDATE publisher_tokens SET expires_at = '2020-01-01 00:00:00' WHERE token_id = ?",
                    (token["token_id"],))
        assert not api_keys.still_valid(con, actor)
        con.rollback()
        assert api_keys.still_valid(con, {**actor, "token_id": token["token_id"]})
        assert not api_keys.still_valid(con, {**actor, "token_id": "tok_0000000000000000"})
    finally:
        con.close()


# --- a plain list collection: no object_kinds, no queries ----------------------------------------------------

PLAIN_MANIFEST = """collection: plain-list
kind: objects
title: A plain list of gene sets
methods: [tests]
dapper_profile: 0.3.0-a1
"""
GSC = _mint("GeneSetCollection", name="Plain sets", collection_key="plain", version="1")
SETS = [_mint("GeneSet", name=f"PLAIN_{i}", members=["NOD2"], member_type="gene", n_members=1,
              in_gene_set_collection=[GSC["id"]]) for i in range(3)]


def test_a_plain_list_collection_holds_members_without_kinds_or_queries(isolated_db, token, tmp_path, monkeypatch):
    (tmp_path / "collection.yaml").write_text(PLAIN_MANIFEST)
    col = load_collection(tmp_path)
    assert col.queries == {} and "object_kinds" not in col.doc
    h = bearer(token)
    registered = writer.post("/v1/publish/objects/plain-list:register", json={"files": {
        "collection.yaml": PLAIN_MANIFEST}}, headers=h)
    assert registered.status_code == 201 and registered.json()["queries"] == [], registered.text
    assert writer.post("/v1/collections/plain-list/releases", json={"version": "1"}, headers=h).status_code == 201
    part = writer.post("/v1/deposits", json={"document": {"gene_set_collections": [GSC], "gene_sets": SETS},
                                             "collection": "plain-list", "release": "1"}, headers=h)
    assert part.status_code == 200 and part.json()["collection"]["objects"] == 4, part.text
    closed = writer.post("/v1/collections/plain-list/releases/1:close", headers=h)
    assert closed.status_code == 200, closed.text
    assert closed.json()["classes"] == {"GeneSet": 3, "GeneSetCollection": 1}
    assert not _rows("SELECT 1 FROM release_object_fields WHERE collection_key = 'plain-list'")
    # an older build sharing the database reads manifest["object_kinds"] / ["queries"] of every current release
    stored = json.loads(_rows("SELECT manifest FROM collection_releases WHERE release_id = ?",
                              (closed.json()["release_id"],))[0]["manifest"])
    assert stored["manifest"]["object_kinds"] == {} and stored["manifest"]["queries"] == []

    members = writer.get("/v1/collections/plain-list/releases/1/members", params={"class": "GeneSet"})
    assert members.status_code == 200 and members.text.split() == sorted(s["id"] for s in SETS)
    assert writer.get(f"/v1/objects/{SETS[0]['id']}").json()["name"] == "PLAIN_0"
    described = writer.get("/v1/collections/plain-list")
    assert described.status_code == 200, described.text
    assert described.json()["queries"] == [] and described.json()["object_kinds"] == {}
    assert described.json()["objects"]["total"] == 4
    document = writer.get("/v1/collections/plain-list/releases/1/document").json()
    rc = document["result_collections"][0]
    assert "has_query_template" not in rc and rc["id"] == closed.json()["release_id"]
    assert writer.get("/v1/collections/plain-list/queries").json()["queries"] == []
    assert any(c["id"] == "plain-list" for c in writer.get("/v1/collections").json()["collections"])
    assert writer.get("/v1/queries").status_code == 200
    admin = _admin(monkeypatch)
    for path in ("collections", "collections/plain-list", "overview"):
        assert writer.get(f"/v1/admin/api/{path}", headers=admin("admin_read")).status_code == 200, path


# --- registering a definition with a login -------------------------------------------------------------------

def _files(root: Path) -> dict:
    from claims_cli.push import collection_files

    return collection_files(load_collection(root))


def test_a_login_registers_an_object_collection(isolated_db, token, tmp_path):
    import shutil

    col = tmp_path / "registered-by-login"
    shutil.copytree(COLLECTION, col)
    (col / "collection.yaml").write_text((col / "collection.yaml").read_text().replace(
        "collection: catalog-test", "collection: login-catalog"))
    files = _files(col)
    assert sorted(files) == ["collection.yaml", "mappings/disease.tsv", "queries/gap-with-anchors.yaml",
                             "queries/gaps-by-disease.yaml", "queries/gene-sets-in-collection.yaml"]
    url = "/v1/publish/objects/login-catalog:register"
    body = {"files": files, "prefix": "collections/login-catalog", "git_commit": "abc1234"}
    first = writer.post(url, json=body, headers=bearer(token))
    assert first.status_code == 201, first.text
    assert first.json() == {"collection": "login-catalog", "status": "registered", "files": 5, "git_commit": "abc1234",
                            "queries": ["gap-with-anchors", "gaps-by-disease", "gene-sets-in-collection"]}
    stored = _rows("SELECT definition, git_commit FROM object_collections WHERE collection_key = 'login-catalog'")[0]
    assert stored["git_commit"] == "abc1234"
    manifest_file = writer.get(f"/v1/objects/{__import__('json').loads(stored['definition'])['manifest_file_id']}")
    assert manifest_file.json()["location"] == "collections/login-catalog/collection.yaml"

    # The same definition again (another commit, another path) changes nothing; File ids ignore the location.
    again = writer.post(url, json={**body, "prefix": "", "git_commit": "def5678"}, headers=bearer(token))
    assert again.status_code == 200 and again.json()["status"] == "unchanged"
    # Registered on the database by scripts/publish_collection.py: the same definition.
    con = _con()
    try:
        objects.register(con, load_collection(col), "fromdisk")
    finally:
        con.close()
    assert writer.post(url, json=body, headers=bearer(token)).json()["status"] == "unchanged"

    changed = {**files, "collection.yaml": files["collection.yaml"].replace("title: Test catalog", "title: Renamed catalog")}
    refused = writer.post(url, json={**body, "files": changed}, headers=bearer(token))
    assert refused.status_code == 409 and "claims objects register --replace" in refused.json()["detail"]
    replaced = writer.post(url, json={**body, "files": changed, "replace": True}, headers=bearer(token))
    assert replaced.status_code == 200 and replaced.json()["status"] == "replaced"
    assert objects.definition(_con(), "login-catalog")["manifest"]["title"].startswith("Renamed catalog")
    events = [r["detail"].split()[1] for r in _rows(
        "SELECT detail FROM admin_auth_events WHERE event = 'object_collection_registered' AND detail LIKE "
        "'login-catalog %' ORDER BY id")]
    assert events == ["registered", "replaced"]

    # Then the login opens a release of it, with no key and no grant.
    opened = writer.post("/v1/collections/login-catalog/releases", json={"version": "1"}, headers=bearer(token))
    assert opened.status_code == 201, opened.text


def test_registering_refuses_what_is_not_an_object_collection(isolated_db, token, tmp_path):
    pigean = ROOT / "tests" / "fixtures" / "collections" / "pigean"
    files = _files(pigean)
    key = load_collection(pigean).key
    refused = writer.post(f"/v1/publish/objects/{key}:register", json={"files": files}, headers=bearer(token))
    assert refused.status_code == 409 and "claims release publish" in refused.json()["detail"]

    plain = {"collection.yaml": PLAIN_MANIFEST.replace("plain-list", "other-key")}
    wrong = writer.post("/v1/publish/objects/not-this-key:register", json={"files": plain}, headers=bearer(token))
    assert wrong.status_code == 400 and "not 'not-this-key'" in wrong.json()["detail"]
    extra = writer.post("/v1/publish/objects/other-key:register",
                        json={"files": {**plain, "notes.txt": "x"}}, headers=bearer(token))
    assert extra.status_code == 400 and "does not use" in extra.json()["detail"]
    missing = writer.post("/v1/publish/objects/other-key:register", json={"files": {
        "collection.yaml": plain["collection.yaml"] + "queries: [queries/q.yaml]\n"}}, headers=bearer(token))
    assert missing.status_code == 400 and "not sent" in missing.json()["detail"]

    # A key a file collection holds cannot be taken over by an object collection.
    con = _con()
    try:
        with con:
            con.execute("INSERT INTO collections (collection_key, title, description, current_release_id) "
                        "VALUES ('file-held', 'A file collection', NULL, NULL)")
    finally:
        con.close()
    held = writer.post("/v1/publish/objects/file-held:register",
                       json={"files": {"collection.yaml": PLAIN_MANIFEST.replace("plain-list", "file-held")}},
                       headers=bearer(token))
    assert held.status_code == 409 and "is a file collection" in held.json()["detail"]
    assert objects.definition(_con(), "file-held") is None


def test_registering_needs_a_valid_login(isolated_db, depositor):
    revoked, expired = login("r2@example.org"), login("e2@example.org")
    con = _con()
    try:
        with con:
            device_login.revoke(con, revoked["token_id"], "admin@example.org")
            con.execute("UPDATE publisher_tokens SET expires_at = '2020-01-01 00:00:00' WHERE token_id = ?",
                        (expired["token_id"],))
    finally:
        con.close()
    body = {"files": {"collection.yaml": PLAIN_MANIFEST.replace("plain-list", "nobody")}}
    for headers in (bearer(revoked), bearer(expired), {"X-API-Key": depositor["key"]}, {}):
        response = writer.post("/v1/publish/objects/nobody:register", json=body, headers=headers)
        assert response.status_code == 401, response.text
    assert objects.definition(_con(), "nobody") is None


def test_the_contract_lists_the_register_route_and_both_credentials():
    import yaml

    contract = yaml.safe_load(writer.get("/v1/openapi.yaml").text)
    register = contract["paths"]["/v1/publish/objects/{collection}:register"]["post"]
    assert register["security"] == [{"PublisherToken": []}]
    for path, method in (("/v1/deposits", "post"), ("/v1/collections/{collection}/releases", "post"),
                         ("/v1/collections/{collection}/releases/{version}:close", "post")):
        assert contract["paths"][path][method]["security"] == [{"ApiKeyHeader": []}, {"PublisherToken": []}], path


def test_open_and_close_recheck_a_login_inside_their_transaction(registered):
    """A login revoked after the request was authenticated opens and closes nothing (as a deposit re-checks it)."""
    from service.collections import objects as cobjects

    late = login("late@example.org")
    actor = {"kind": "login", "token_id": late["token_id"], "email": "late@example.org", "name": "late@example.org"}
    con = _con()
    try:
        with con:
            device_login.revoke(con, late["token_id"], "admin@example.org")
        with pytest.raises(cobjects.NotGranted):
            with con:
                cobjects.open_release(con, actor, "catalog-test", "2026.12.8")
        with pytest.raises(cobjects.NotGranted):
            with con:
                cobjects.close_release(con, actor, "catalog-test", "2026.12.0")
    finally:
        con.close()
    assert not _rows("SELECT 1 FROM object_releases WHERE version = '2026.12.8'")
