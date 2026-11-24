"""Provenance for a release: a DAPPER provenance document kept with the
collection (stored, resolves here) or referenced by URL (not stored, resolves
where it is hosted). Datasets point at the run Activity the document names;
claim ids do not change."""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from service import db as service_db
from service.app import app
from service.collections import identity as idn
from service.collections.manifest import load_collection
from service.collections.publish import PublishError, publish

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
COLLECTION = FIX / "collections" / "pigean"
ROLES = {"gene_stats": "combined_gene_stats.json", "gene_set_stats": "combined_gene_set_stats.json",
         "membership": "gene_gene_set_stats.json"}
RESOLVER = "https://provenance.example.org/api/provenance-indexer"
client = TestClient(app)


def _setup(tmp: Path):
    folder = tmp / "files"
    folder.mkdir()
    for name in ROLES.values():
        (folder / name).write_bytes((FIX / "pigean" / name).read_bytes())
    run = idn.mint("Activity", {"name": "PIGEAN run over fixture-v1", "description": "The run that wrote the fixtures.",
                                "software_name": "PIGEAN", "software_version": "1.2.3",
                                "generated_at_time": "2026-10-14T00:00:00Z"})
    files, edges = [], []
    for name in ROLES.values():
        data = (folder / name).read_bytes()
        f = idn.mint("File", {"filename": name, "sha256": hashlib.sha256(data).hexdigest(),
                              "size_in_bytes": len(data), "location": f"s3://bucket/run/{name}",
                              "mime_type": "application/x-ndjson"})
        files.append(f)
        edges.append({"subject": f["id"], "object": run["id"]})
    document = {"activities": [run], "files": files, "was_generated_by_edges": edges}
    doc_bytes = yaml.safe_dump(document, sort_keys=False).encode()
    (tmp / "provenance").mkdir()
    (tmp / "provenance" / "run.dapper.yaml").write_bytes(doc_bytes)
    spec = {"version": "0.4.0-prov", "datasets": [{
        "dimensions": {"trait": "FIXPHENO", "library": "fixture-v1"},
        "files": {role: {"path": str(folder / name), "location": f"s3://bucket/run/{name}"}
                  for role, name in ROLES.items()}}]}
    return spec, run, doc_bytes


def _publish(tmp: Path, spec: dict, **kwargs):
    db_path = str(tmp / f"{tempfile.mktemp(dir='').strip('/')}.db")
    saved = service_db.DB_PATH
    service_db.DB_PATH = db_path
    con = service_db.connect()
    try:
        return publish(con, load_collection(COLLECTION), spec, tmp, **kwargs), db_path
    finally:
        con.close()
        service_db.DB_PATH = saved


def _claims(db_path):
    con = service_db.connect(db_path)
    try:
        return {r["claim_id"] for r in con.execute("SELECT claim_id FROM result_claims")}
    finally:
        con.close()


def _dataset(result):
    return result.document["datasets"][0]


def test_stored_provenance_names_the_run_and_resolves_here(tmp_path):
    spec, run, _ = _setup(tmp_path)
    plain, plain_db = _publish(tmp_path, spec)
    stored, stored_db = _publish(tmp_path, {**spec, "provenance": [{"path": "provenance/run.dapper.yaml"}]})
    # Provenance is linked by edges, never by content: dataset ids and claim ids are unchanged.
    assert _dataset(stored)["id"] == _dataset(plain)["id"]
    assert _claims(stored_db) == _claims(plain_db)
    edges = {(e["subject"], e["object"]) for e in stored.document["was_generated_by_edges"]}
    assert (_dataset(stored)["id"], run["id"]) in edges
    assert all((f["id"], run["id"]) in edges for f in stored.document["files"] if f["id"] in _dataset(stored)["has_file"])
    assert run["id"] in {a["id"] for a in stored.document["activities"]}
    assert any(e["object"] == run["id"] for e in stored.document["was_generated_by_edges"])
    saved = service_db.DB_PATH
    service_db.DB_PATH = stored_db
    try:
        got = client.get(f"/v1/objects/{run['id']}")
        assert got.status_code == 200 and got.json()["software_version"] == "1.2.3"
        described = client.get("/v1/collections/pigean").json()
        prov = described["provenance"]
        assert prov == [{"path": "provenance/run.dapper.yaml", "sha256": prov[0]["sha256"], "stored": True,
                         "resolver": None, "objects": 4}]
        assert described["datasets"][0]["provenance"] == {"run": run["id"], "stored": True,
                                                          "resolve": f"/v1/objects/{run['id']}"}
    finally:
        service_db.DB_PATH = saved


def test_referenced_provenance_is_not_stored_and_redirects_to_its_host(tmp_path):
    spec, run, doc_bytes = _setup(tmp_path)
    url = f"{RESOLVER}/v1/documents/7b0f6c1e"
    hosted = json.dumps({"id": "7b0f6c1e", "profile": "provenance", "payload": yaml.safe_load(doc_bytes)}).encode()
    fetched = []

    def fetch(u):
        fetched.append(u)
        return hosted

    result, db_path = _publish(tmp_path, {**spec, "provenance": [{"url": url}]}, provenance_fetch=fetch)
    assert fetched == [url]
    assert (_dataset(result)["id"], run["id"]) in {(e["subject"], e["object"]) for e in result.document["was_generated_by_edges"]}
    con = service_db.connect(db_path)
    try:
        assert con.execute("SELECT COUNT(*) AS n FROM dapper_objects WHERE id = ?", (run["id"],)).fetchone()["n"] == 0
    finally:
        con.close()
    saved = service_db.DB_PATH
    service_db.DB_PATH = db_path
    try:
        got = client.get(f"/v1/objects/{run['id']}", follow_redirects=False)
        assert got.status_code == 307 and got.headers["location"] == f"{RESOLVER}/v1/entities/{run['id']}"
        assert client.get("/v1/objects/dapper:Activity.nothere", follow_redirects=False).status_code == 404
    finally:
        service_db.DB_PATH = saved
    # A pinned sha256 that no longer matches what the URL serves is refused.
    with pytest.raises(PublishError, match="changed since the release was planned"):
        _publish(tmp_path, {**spec, "version": "0.4.1-prov", "provenance": [{"url": url, "sha256": "0" * 64}]},
                 provenance_fetch=fetch)


def test_provenance_nodes_must_be_true_addresses(tmp_path):
    spec, run, doc_bytes = _setup(tmp_path)
    doc = yaml.safe_load(doc_bytes)
    doc["activities"][0]["software_version"] = "9.9.9"  # content no longer matches its id
    (tmp_path / "provenance" / "run.dapper.yaml").write_text(yaml.safe_dump(doc))
    with pytest.raises(PublishError, match="not a true DAPPER-ID-1 address"):
        _publish(tmp_path, {**spec, "provenance": [{"path": "provenance/run.dapper.yaml"}]})


def test_a_membership_file_of_only_its_ends_and_dimensions_mints_the_same_gene_sets(tmp_path):
    """docs/publishing-a-pigean-run.md recommends cutting the membership file to gene, gene_set and
    the two dimension columns: only the (gene, gene set) pairs are read, so every roster is unchanged.
    (Gene set ids still change, since each set cites the membership file it was read from.)"""
    spec, _, _ = _setup(tmp_path)
    full, full_db = _publish(tmp_path, spec)
    slim = tmp_path / "slim.json"
    keep = ("gene", "gene_set", "phenotype", "gene_set_size")
    lines = (FIX / "pigean" / ROLES["membership"]).read_text().splitlines()
    slim.write_text("".join(json.dumps({k: json.loads(l)[k] for k in keep}) + "\n" for l in lines if l.strip()))
    slim_spec = {**spec, "version": "0.4.0-slim"}
    slim_spec["datasets"] = [{**spec["datasets"][0], "files": {**spec["datasets"][0]["files"],
                                                              "membership": {"path": str(slim),
                                                                             "location": "s3://bucket/run/slim.json"}}}]
    _, slim_db = _publish(tmp_path, slim_spec)

    def gene_sets(db_path):
        con = service_db.connect(db_path)
        try:
            docs = [json.loads(r["payload"]) for r in
                    con.execute("SELECT payload FROM dapper_objects WHERE class_name = 'GeneSet'")]
            return {(d["name"], tuple(d.get("members", ()))) for d in docs}
        finally:
            con.close()

    assert gene_sets(full_db) and gene_sets(full_db) == gene_sets(slim_db)
