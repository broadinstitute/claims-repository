"""scripts/rescan_identity.py: recompute stored ids under the current DAPPER
version and say, per object, which version minted it and which hashed slots
changed. Runs on an in-memory SQLite database shaped like dapper_objects."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from service.collections.identity_versions import archived, current

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("rescan_identity", ROOT / "scripts" / "rescan_identity.py")
rescan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rescan)


@pytest.fixture(scope="module")
def versions():
    old = [v for v in archived() if v.version == "0.2.1-a1"]
    if not old:
        pytest.skip("vendor/dapper-archive/0.2.1-a1 is not present")
    return current(), old


def stored(version, cls, payload):
    return {"id": version.mint_id(cls, payload), **payload}


def db(objects: list[tuple[str, dict]]) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("create table dapper_objects (id text primary key, class_name text, identity_profile text, payload text)")
    for cls, doc in objects:
        con.execute("insert into dapper_objects values (?, ?, ?, ?)", (doc["id"], cls, "DAPPER-ID-1", json.dumps(doc)))
    return con


def scan(con, versions):
    now, older = versions
    return {row[0]: rescan.examine(row[0], row[1], row[2], json.loads(row[3]), now, older)
            for row in rescan.rows(con, None, None, None)}


def test_an_unchanged_object_is_the_same_under_both_versions(versions):
    now, (old,) = versions
    prop = stored(old, "Proposition", {"statement": "Gene A is associated with trait T.", "proposition_kind": "RESULT"})
    row = scan(db([("Proposition", prop)]), versions)[prop["id"]]
    assert row["status"] == "same"
    assert row["minted_under"] == "0.2.1-a1 0.3.0-a1"
    assert row["changed_slots"] == ""


def test_a_reshaped_slot_is_reported_as_invalid_with_the_slot(versions):
    """0.2.1 hashed Dataset.trait; 0.3 moved it into contexts."""
    now, (old,) = versions
    dataset = stored(old, "Dataset", {"name": "GWAS summary statistics", "trait": "KPN.TRAIT:0000505"})
    row = scan(db([("Dataset", dataset)]), versions)[dataset["id"]]
    assert row["status"] == "invalid_current"
    assert row["minted_under"] == "0.2.1-a1"
    assert set(row["changed_slots"].split()) == {"-trait", "?trait"}


class Stub:
    """A version that hashes a fixed set of slots: enough to exercise the diff."""

    def __init__(self, version, hashable):
        self.version, self._hashable = version, set(hashable)

    def has_class(self, cls):
        return True

    def slots(self, cls):
        return {"statement", "scope", "note"}

    def hashable(self, cls):
        return self._hashable

    def unknown_slots(self, cls, payload):
        return sorted(k for k in payload if k != "id" and k not in self.slots(cls))

    def hashed(self, cls, payload):
        return {k: v for k, v in payload.items() if k in self._hashable}

    def mint_id(self, cls, payload):
        return "dapper:%s.%s" % (cls, json.dumps(self.hashed(cls, payload), sort_keys=True))


def test_a_slot_that_became_hashed_changes_the_id():
    """No slot changed hashability between 0.2.1 and 0.3; a later release may."""
    old, now = Stub("1.0", {"statement"}), Stub("2.0", {"statement", "scope"})
    payload = {"statement": "s", "scope": "colon"}
    oid = old.mint_id("Proposition", payload)
    row = rescan.examine(oid, "Proposition", "DAPPER-ID-1", {"id": oid, **payload}, now, [old])
    assert row["status"] == "different"
    assert row["minted_under"] == "1.0"
    assert row["changed_slots"] == "+scope"
    assert row["current_id"] == now.mint_id("Proposition", payload)


def test_other_identity_profiles_are_not_recomputed(versions):
    con = db([])
    con.execute("insert into dapper_objects values ('x:1', 'Agent', 'local-dev-sha256-1', '{}')")
    assert scan(con, versions)["x:1"]["status"] == "other_profile"


def test_a_foreign_id_is_unknown(versions):
    now, _ = versions
    prop = {"id": "dapper:Proposition." + "A" * 32, "statement": "s"}
    row = scan(db([("Proposition", prop)]), versions)[prop["id"]]
    assert row["minted_under"] == "unknown"
    assert row["status"] == "different"
