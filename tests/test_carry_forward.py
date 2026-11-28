"""Carry-forward object releases: a revision opened with `carry_from` starts with a closed release's members, so
only new or changed objects are deposited, and closes into a ResultCollection that names the release it revises."""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import test_object_collections as base
from service import api_keys
from service import db as service_db
from service.app import app
from service.collections import identity as cidn
from service.collections import objects
from service.collections.manifest import load_collection

KEY = "carry-test"
client = TestClient(app)        # conftest gives this one the session's deposit key
ORG = cidn.mint("Organization", {"name": "Common Fund Data Ecosystem (CFDE)"})
PROGRAM = cidn.mint("Organization", {"name": "Illustrative program", "parent_organization": ORG["id"]})


@pytest.fixture(scope="module", autouse=True)
def isolated_db():
    saved = service_db.DB_PATH
    service_db.DB_PATH = str(Path(tempfile.mkdtemp()) / "carry.db")
    yield service_db.DB_PATH
    service_db.DB_PATH = saved


def _collection(tmp: Path, extra_kinds: str = "", gene_set_fields: str = "") -> Path:
    """The catalog-test collection under its own key, optionally with more object kinds or GeneSet fields."""
    col = tmp / KEY
    if col.exists():
        shutil.rmtree(col)
    shutil.copytree(base.COLLECTION, col)
    text = (col / "collection.yaml").read_text().replace("collection: catalog-test", f"collection: {KEY}")
    if gene_set_fields:
        text = text.replace("      size: {slot: n_members}\n", "      size: {slot: n_members}\n" + gene_set_fields)
    text = text.replace("\n# The collection's own vocabulary", extra_kinds + "\n# The collection's own vocabulary")
    (col / "collection.yaml").write_text(text)
    return col


def _register(col: Path) -> None:
    con = service_db.connect()
    try:
        objects.register(con, load_collection(col))
    finally:
        con.close()


def _rows(sql, params=()):
    con = service_db.connect()
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def _fields(version):
    return {(r["object_id"], r["field"], r["value"]) for r in _rows(
        "SELECT object_id, field, value FROM release_object_fields WHERE collection_key = ? AND version = ?",
        (KEY, version))}


def _open(version, **extra):
    return client.post(f"/v1/collections/{KEY}/releases", json={"version": version, **extra})


@pytest.fixture(scope="module")
def first(depositor, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("carry")
    _register(_collection(tmp))
    con = service_db.connect()
    try:
        with con:
            api_keys.grant(con, depositor["key_id"], KEY, "tests")
    finally:
        con.close()
    assert _open("1.0").status_code == 201
    for part in (base.PART_1, base.PART_2):
        assert client.post("/v1/deposits", json={"document": part, "collection": KEY, "release": "1.0"}).status_code == 200
    closed = client.post(f"/v1/collections/{KEY}/releases/1.0:close").json()
    assert closed["state"] == "closed"
    return {"tmp": tmp, "closed": closed}


def test_carrying_refuses_what_it_cannot_carry(first):
    assert _open("0.9", carry_from="9.9").status_code == 400          # no such release
    assert _open("0.9", carry_from="0.9").status_code == 400          # itself
    assert _open("0.9", carry_from=1).status_code == 400              # not a version
    assert _open("0.8").status_code == 201                            # open, not closed
    refused = _open("0.9", carry_from="0.8")
    assert refused.status_code == 400 and "still open" in refused.json()["detail"], refused.text


def test_a_revision_starts_with_the_members_and_fields_of_the_release_it_carries(first):
    opened = _open("1.1", carry_from="1.0")
    assert opened.status_code == 201, opened.text
    carried = first["closed"]["objects"]
    assert opened.json()["carried_from"] == {"version": "1.0", "objects": carried, "fields_complete": True}
    status = client.get(f"/v1/collections/{KEY}/releases/1.1").json()
    assert status["objects"] == carried and status["classes"] == first["closed"]["classes"]
    assert status["deposits"] == 0                                    # nothing was sent again
    assert _fields("1.1") == _fields("1.0") and _fields("1.0")
    deposits = {r["object_id"]: r["deposit_id"] for r in _rows(
        "SELECT object_id, deposit_id FROM release_objects WHERE collection_key = ? AND version = ?", (KEY, "1.0"))}
    assert deposits == {r["object_id"]: r["deposit_id"] for r in _rows(
        "SELECT object_id, deposit_id FROM release_objects WHERE collection_key = ? AND version = ?", (KEY, "1.1"))}
    # Opening it again is a no-op; opening it again with another release to carry is a conflict.
    assert _open("1.1", carry_from="1.0").status_code == 200
    assert _open("1.1").status_code == 200
    assert _open("1.1", carry_from="0.8").status_code == 409


def test_closing_a_revision_lists_carried_and_new_members_and_names_the_release_it_revises(first):
    added = client.post("/v1/deposits", json={"document": {"organizations": [ORG, PROGRAM]}, "collection": KEY,
                                              "release": "1.1"})
    assert added.status_code == 200, added.text
    closed = client.post(f"/v1/collections/{KEY}/releases/1.1:close").json()
    assert closed["objects"] == first["closed"]["objects"] + 2
    assert closed["classes"]["Organization"] == 2 and closed["unresolved_references"] == []
    document = client.get(f"/v1/collections/{KEY}/releases/1.1/document").json()
    assert document["result_collections"][0]["was_revision_of"] == first["closed"]["release_id"]
    members = client.get(f"/v1/collections/{KEY}/releases/1.1/members", params={"class": "GeneSet"}).text.split()
    assert sorted(members) == sorted([base.SET_SMALL["id"], base.SET_BIG["id"]])
    def run(release):
        return client.post(f"/v1/collections/{KEY}/queries/gene-sets-in-collection:run",
                           json={"variables": {"name": "cfde"}, "release": release}).json()
    out, before = run("1.1"), run("1.0")
    assert out["release"]["version"] == "1.1" and out["rows"]          # carried members are queryable
    assert out["rows"] == before["rows"]                               # exactly as in the release they came from


def test_a_new_definition_indexes_carried_members_under_its_own_fields(first):
    """The revision's definition adds an object kind and a GeneSet field: carried gene sets are re-indexed from
    their stored documents, unchanged kinds are copied, and a new kind indexes new deposits."""
    org_kind = "  Organization:\n    fields:\n      name: {slot: name}\n    links:\n      parent: {slot: parent_organization}\n"
    _register(_collection(first["tmp"], extra_kinds=org_kind, gene_set_fields="      genes: {slot: members}\n"))
    assert _open("2.0", carry_from="1.1").status_code == 201
    fields = _fields("2.0")
    genes = {v for o, f, v in fields if f == "genes"}
    assert genes == {"NOD2", "IL23R", "ATG16L1"}                      # recomputed for carried gene sets
    assert {(o, f, v) for o, f, v in fields if f != "genes" and not o.startswith("dapper:Organization.")} == \
        {(o, f, v) for o, f, v in _fields("1.1") if not o.startswith("dapper:Organization.")}
    assert {v for o, f, v in fields if f == "name" and o == ORG["id"]} == {ORG["name"]}


def test_carrying_a_hundred_thousand_members_takes_seconds(first):
    """Timing (printed): carry ~100k members on SQLite."""
    con = service_db.connect()
    try:
        with con:
            con.execute("INSERT INTO object_releases (collection_key, version, state, definition, opened_at, "
                        "opened_by) SELECT collection_key, '9.0', 'closed', definition, opened_at, opened_by "
                        "FROM object_releases WHERE collection_key = ? AND version = '1.0'", (KEY,))
            con.executemany("INSERT INTO release_objects (collection_key, version, object_id, class_name, deposit_id) "
                            "VALUES (?, '9.0', ?, 'GeneSet', 'dep_synthetic')",
                            [(KEY, f"dapper:GeneSet.synthetic{i:026d}") for i in range(100_000)])
            con.executemany("INSERT INTO release_object_fields (collection_key, version, object_id, field, value, num) "
                            "VALUES (?, '9.0', ?, 'name', ?, NULL)",
                            [(KEY, f"dapper:GeneSet.synthetic{i:026d}", f"set {i}") for i in range(100_000)])
    finally:
        con.close()
    _register(_collection(first["tmp"]))                              # same GeneSet fields as 9.0: copied
    started = time.perf_counter()
    assert _open("9.1", carry_from="9.0").status_code == 201
    seconds = time.perf_counter() - started
    print(f"\ncarried 100,000 members with their fields in {seconds:.2f} s")
    assert client.get(f"/v1/collections/{KEY}/releases/9.1").json()["objects"] == 100_000
    assert seconds < 60


def test_carried_fields_are_copied_in_batches_that_resume_and_closing_waits_for_them(first, monkeypatch):
    """Millions of field rows are not copied in one transaction: each call copies batches until its time is up,
    opening again continues, and the release cannot close before the copy is complete."""
    from service.collections import objects as cobjects

    monkeypatch.setattr(cobjects, "CARRY_FIELDS_BATCH", 2)
    monkeypatch.setattr(cobjects, "CARRY_SECONDS", 0.0)       # one batch per call
    opened = _open("1.3", carry_from="1.0")
    assert opened.status_code == 201, opened.text
    assert opened.json()["carried_from"]["fields_complete"] is False
    refused = client.post(f"/v1/collections/{KEY}/releases/1.3:close")
    assert refused.status_code == 409 and "still copying" in refused.text
    assert client.get(f"/v1/collections/{KEY}/releases/1.3").json()["state"] == "open"
    calls = 1
    while not _open("1.3", carry_from="1.0").json()["carried_from"]["fields_complete"]:
        calls += 1
        assert calls < 50
    assert calls >= first["closed"]["objects"] // 2
    assert _fields("1.3") == _fields("1.0")
    assert client.post(f"/v1/collections/{KEY}/releases/1.3:close").status_code == 200
