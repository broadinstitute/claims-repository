"""Publishing into a deployed service with a `claims login` token (docs/publishing-to-a-deployment.md).

The acceptance test: the same release published directly into a local deployment (SQLite plus a Parquet
index on disk) and pushed over HTTP (its Parquet index into a bucket, here a file:// one) records the same
control rows, objects, mint contexts and release, stores no row in the service database, and serves the
same claims. The rest are what the API refuses."""
from __future__ import annotations

import os
import gzip
import json
import socket
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from claims_cli import push
from service import db as service_db
from service import device_login, publish_api
from service.app import app
from service.collections import identity as idn
from service.collections import index_bucket
from service.collections import publish as pub
from service.collections.manifest import load_collection, load_release
from service.collections.store import ParquetStore
from service.collections.publish import PublishError, publish

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
COLLECTION = FIX / "collections" / "pigean"
PIGEAN_FIX = FIX / "pigean"
ROLES = {"gene_stats": "combined_gene_stats.json", "gene_set_stats": "combined_gene_set_stats.json",
         "membership": "gene_gene_set_stats.json"}
PUBLISHER = "publisher@example.org"
QUERY = "top-genes-with-sets"


@pytest.fixture(autouse=True)
def _quick_retries(monkeypatch):
    monkeypatch.setattr(push, "RETRIES", 2)
    monkeypatch.setattr(push.time, "sleep", lambda _s: None)


@pytest.fixture(scope="module")
def pushed_db():
    """The deployed service's database for this module (the server thread connects through DB_PATH), its
    index cache, and file:// index locations allowed (a stand-in for the owner's bucket)."""
    saved = service_db.DB_PATH, {k: os.environ.get(k) for k in ("CLAIMS_INDEX_ALLOW_FILE", "CLAIMS_INDEX_CACHE")}
    root = Path(tempfile.mkdtemp())
    service_db.DB_PATH = str(root / "pushed.db")
    os.environ["CLAIMS_INDEX_ALLOW_FILE"], os.environ["CLAIMS_INDEX_CACHE"] = "1", str(root / "cache")
    yield service_db.DB_PATH
    service_db.DB_PATH = saved[0]
    for k, v in saved[1].items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(scope="module")
def bucket(pushed_db):
    return "file://" + str(Path(tempfile.mkdtemp()) / "owner-bucket" / "pigean-index")


@pytest.fixture(scope="module")
def api(pushed_db):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


@pytest.fixture(scope="module")
def token(pushed_db):
    con = service_db.connect()
    try:
        started = device_login.start(con, "claims-cli/1", "test-runner", "34.1.2.3")
        con.commit()
        with con:
            assert device_login.decide(con, device_login.normalize_user_code(started["user_code"]), PUBLISHER, True)
        return device_login.exchange(con, started["device_code"])
    finally:
        con.close()


def _files(tmp_path: Path, phenotype: str) -> Path:
    out = tmp_path / phenotype
    out.mkdir(parents=True, exist_ok=True)
    for name in ROLES.values():
        rows = [json.loads(line) for line in (PIGEAN_FIX / name).read_text().splitlines() if line.strip()]
        for row in rows:
            row["phenotype"] = phenotype
        (out / name).write_text("".join(json.dumps(r) + "\n" for r in rows))
    return out


def _spec(tmp_path: Path, version: str, traits=("FIXPHENO", "FIXPHENO2")) -> dict:
    """Two datasets, the second under bounds (its gene sets keep whole rosters)."""
    datasets = []
    for phenotype, where in ((traits[0], None), (traits[1], {"gene_phenotype": {"combined": {"gt": 2}},
                                                                "gene_set_phenotype": {"beta_uncorrected": {"gt": 1.0}}})):
        folder = _files(tmp_path, phenotype)
        ds = {"dimensions": {"trait": phenotype, "library": "fixture-v1"},
              "files": {role: {"path": str(folder / name)} for role, name in ROLES.items()}}
        if where:
            ds["where"] = where
        datasets.append(ds)
    return {"version": version, "datasets": datasets}


def _target(api, token, col, version, bucket, cls=None):
    return (cls or push.RemoteTarget)(api, token["access_token"], col, version, bucket,
                                      Path(tempfile.mkdtemp()) / "staging")


def _push(api, token, bucket, spec, base, **kw):
    col = load_collection(COLLECTION)
    return publish(None, col, spec, base, target=_target(api, token, col, spec["version"], bucket), **kw)


def _rows(con, sql, params=()):
    return sorted(tuple(r) for r in con.execute(sql, params).fetchall())


def _snapshot(con) -> dict:
    """Everything a release stores, without autoincrement ids or counters."""
    return {
        "releases": _rows(con, "SELECT release_id, collection_key, version, manifest, document, git_commit "
                               "FROM collection_releases"),
        "collections": _rows(con, "SELECT collection_key, title, description, current_release_id FROM collections"),
        "release_datasets": _rows(con, "SELECT release_id, dataset_id FROM release_datasets"),
        "release_mint_contexts": _rows(con, "SELECT release_id, activity_id, dataset_id FROM release_mint_contexts"),
        "mint_contexts": _rows(con, "SELECT activity_id, template_file_id, result_file_id, context FROM mint_contexts"),
        "rows": _rows(con, "SELECT dataset_id, kind, subject_id, subject_value, subject_label, object_id, object_label, "
                           "file_id, row_json, spans FROM result_rows"),
        "metrics": _rows(con, "SELECT r.dataset_id, r.kind, r.subject_id, m.metric, m.value FROM result_metrics m "
                              "JOIN result_rows r ON r.row_id = m.row_id"),
        "edges": _rows(con, "SELECT dataset_id, kind, from_id, to_id, from_value, to_value FROM result_edges"),
        "dimensions": _rows(con, "SELECT dataset_id, dimension, value, label FROM dataset_dimensions"),
        "dataset_files": _rows(con, "SELECT dataset_id, role, file_id FROM dataset_files"),
        "objects": _rows(con, "SELECT id, class_name, payload FROM dapper_objects"),
        "refs": _rows(con, "SELECT from_id, to_id FROM object_refs"),
        "text": _rows(con, "SELECT object_id, field, class_name, words FROM object_text"),
    }


def _served(db_path: str, index_dir: str | None = None) -> list:
    saved = service_db.DB_PATH
    service_db.DB_PATH = db_path
    if index_dir:
        os.environ["CLAIMS_INDEX_DIR"] = index_dir
    try:
        r = TestClient(app).post(f"/v1/collections/pigean/queries/{QUERY}:run",
                                 json={"variables": {"trait": "FIXPHENO2", "library": "fixture-v1", "topGenes": 5}})
        assert r.status_code == 200, r.text
        return [(row["subject"]["id"], sorted((k, c["id"]) for k, c in row["claims"].items()),
                 [(i["row"]["subject"]["id"], sorted((k, c["id"]) for k, c in i["edge"]["claims"].items()))
                  for i in row.get("sets", [])]) for row in r.json()["rows"]]
    finally:
        service_db.DB_PATH = saved
        os.environ.pop("CLAIMS_INDEX_DIR", None)


def test_a_pushed_release_is_the_directly_published_release(api, token, pushed_db, bucket, tmp_path, monkeypatch):
    spec = _spec(tmp_path, "0.2.0-push")
    col = load_collection(COLLECTION)
    direct_db, direct_index = str(tmp_path / "direct.db"), str(tmp_path / "direct-index")
    monkeypatch.setenv("CLAIMS_INDEX_DIR", direct_index)   # a local deployment: its index on this disk
    con = service_db.connect(direct_db)
    try:
        direct = publish(con, col, spec, tmp_path)
    finally:
        con.close()
    monkeypatch.delenv("CLAIMS_INDEX_DIR")

    pushed = _push(api, token, bucket, spec, tmp_path)
    assert pushed.release_id == direct.release_id and pushed.complete
    assert {k: v for k, v in pushed.stats.items() if k not in ("index_bytes", "objects")} == \
        {k: v for k, v in direct.stats.items() if k not in ("index_bytes", "objects")}
    assert direct.stats["rows"] > 0 and direct.stats["edges"] > 0

    a, b = service_db.connect(direct_db), service_db.connect(pushed_db)
    try:
        want, got = _snapshot(a), _snapshot(b)
        for table in want:
            assert got[table] == want[table], table
        assert got["rows"] == got["metrics"] == got["edges"] == []   # no row in the service database
        indexed = b.execute("SELECT dataset_id, location, files FROM dataset_indexes").fetchall()
        assert len(indexed) == 2 and all(r["location"].startswith(bucket + "/datasets/") for r in indexed)
        assert tuple(b.execute("SELECT published_by, publisher_token_id FROM collection_releases").fetchone()) == \
            (PUBLISHER, token["token_id"])
        assert a.execute("SELECT published_by FROM collection_releases").fetchone()[0] is None
        events = [r[0] for r in b.execute("SELECT event FROM admin_auth_events WHERE email = ? ORDER BY id",
                                          (PUBLISHER,)).fetchall()]
        assert events[-2:] == ["release_publish_started", "release_published"]
    finally:
        a.close()
        b.close()

    again = _push(api, token, bucket, spec, tmp_path)   # nothing left to do: nothing changes
    assert again.release_id == direct.release_id and again.stats["datasets_ingested"] == 0
    b = service_db.connect(pushed_db)
    try:
        assert _snapshot(b) == got
    finally:
        b.close()
    assert _served(pushed_db) == _served(direct_db, direct_index) != []


def test_a_push_resumes_and_is_recorded_only_when_whole(api, token, pushed_db, bucket, tmp_path):
    spec = _spec(tmp_path, "0.3.0-resume", ("FIXRESUME", "FIXRESUME2"))
    first = _push(api, token, bucket, spec, tmp_path, max_ingest=1)
    assert not first.complete and first.stats["datasets_ingested"] == 1
    con = service_db.connect(pushed_db)
    try:
        assert con.execute("SELECT 1 FROM collection_releases WHERE version = '0.3.0-resume'").fetchone() is None
    finally:
        con.close()
    rest = _push(api, token, bucket, spec, tmp_path)
    assert rest.complete and rest.stats["datasets_ingested"] == 1
    con = service_db.connect(pushed_db)
    try:
        row = con.execute("SELECT release_id, published_by FROM collection_releases WHERE version = '0.3.0-resume'"
                          ).fetchone()
        assert tuple(row) == (rest.release_id, PUBLISHER)
        assert con.execute("SELECT current_release_id FROM collections WHERE collection_key = 'pigean'"
                           ).fetchone()[0] == rest.release_id
    finally:
        con.close()


# --- what the API refuses -------------------------------------------------------------

def _send(api, token, method, path, body, gz=True):
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token["access_token"]
    if gz:
        data, headers["Content-Encoding"] = gzip.compress(data), "gzip"
    return httpx.request(method, f"{api}/v1/publish/pigean/{path}", content=data, headers=headers, timeout=30)


def _begin(api, token, bucket, version="9.9.9-refused"):
    col = load_collection(COLLECTION)
    r = _send(api, token, "POST", f"{version}:begin", {"files": push.collection_files(col), "index_location": bucket,
                                                       "prefix": "tests/fixtures/collections/pigean"})
    assert r.status_code == 200, r.text
    return r.json()["bundle"]


def _one_dataset(tmp_path, phenotype="FIXREF"):
    """(dataset id, the dataset as RemoteTarget describes it, the plan's dataset, vocab, extract)."""
    col = load_collection(COLLECTION)
    folder = _files(tmp_path, phenotype)
    spec = {"version": "x", "datasets": [{"dimensions": {"trait": phenotype, "library": "fixture-v1"},
                                         "files": {role: {"path": str(folder / n)} for role, n in ROLES.items()}}]}
    con = service_db.connect(str(tmp_path / "plan.db"))
    try:
        plan = publish(con, col, spec, tmp_path, dry_run=True).plan
    finally:
        con.close()
    ds = plan["datasets"][0]
    for f in ds["files"].values():
        f["path"] = Path(f["path"])
    extract = pub.read_dataset(col, ds, plan["vocab"])
    dataset = {"raw_dimensions": {n: d["raw"] for n, d in ds["dims"].items()},
               "files": {r: f["doc"] for r, f in ds["files"].items()}, "where": {},
               "was_generated_by": ds["doc"]["was_generated_by"]}
    return ds["doc"]["id"], dataset, ds, plan["vocab"], extract


def _index(tmp_path, bucket, ds, vocab, extract, tamper=None):
    """Build a dataset's index as the CLI does, optionally tampered with, and put it in the bucket."""
    col = load_collection(COLLECTION)
    rows, metrics, edges = pub.parquet_dataset(col, ds, extract, {}, vocab)
    if tamper:
        tamper(rows, metrics, edges)
    staging = ParquetStore(tmp_path / f"staging-{len(list(tmp_path.iterdir()))}")
    staging.write_dataset(ds["doc"]["id"], rows, metrics, edges)
    local = staging.dataset_dir(ds["doc"]["id"])
    where = index_bucket.dataset_location(bucket, ds["doc"]["id"])
    files = {n: {"sha256": index_bucket.sha256_file(local / n), "size": (local / n).stat().st_size,
                 "version_id": index_bucket.upload(local / n, where, n)} for n in index_bucket.FILES}
    return {"location": bucket, "files": files}


def test_no_token_no_push(api):
    assert _send(api, None, "POST", "1.0:begin", {"files": {}}).status_code == 401
    bad = {"access_token": "clp_" + "A" * 43}
    assert _send(api, bad, "POST", "1.0:begin", {"files": {}}).status_code == 401


def test_the_collection_files_are_checked(api, token, bucket):
    col = load_collection(COLLECTION)
    files = push.collection_files(col)
    for body, match in (({"files": {**files, "../evil.yaml": "x"}}, "relative path"),
                        ({"files": {k: v for k, v in files.items() if k != "collection.yaml"}}, "no collection.yaml"),
                        ({"files": {**files, "extra.yaml": "a: 1"}}, "does not use"),
                        ({"files": files, "prefix": "../up"}, "prefix")):
        r = _send(api, token, "POST", "1.0:begin", {**body, "index_location": bucket})
        assert r.status_code == 400 and match in r.json()["detail"], r.text
    for location in (None, "s3://not-allowed-bucket/x", "s3://Bad_Bucket/x", "file:///../etc", "https://x.example/y"):
        r = _send(api, token, "POST", "1.0:begin", {"files": files, "index_location": location})
        assert r.status_code == 400 and ("index location" in r.json()["detail"] or "does not read" in r.json()["detail"]
                                         or "absolute" in r.json()["detail"]), (location, r.text)
    r = httpx.post(f"{api}/v1/publish/other/1.0:begin", json={"files": files, "index_location": bucket},
                   headers={"Authorization": "Bearer " + token["access_token"]})
    assert r.status_code == 400 and "not 'other'" in r.json()["detail"]


def test_a_dataset_index_is_checked_against_its_values(api, token, bucket, tmp_path):
    bundle = _begin(api, token, bucket)
    dataset_id, dataset, ds, vocab, extract = _one_dataset(tmp_path)

    def put(index, d=dataset, did=dataset_id, b=bundle):
        return _send(api, token, "PUT", f"9.9.9-refused/datasets/{did}", {"bundle": b, "dataset": d, "index": index})

    good = _index(tmp_path, bucket, ds, vocab, extract)
    other_trait = {**dataset, "raw_dimensions": {**dataset["raw_dimensions"], "trait": "OTHER"}}
    r = put(good, d=other_trait)
    assert r.status_code == 400 and "not the dataset these values make" in r.json()["detail"]
    tampered_file = json.loads(json.dumps(dataset))
    tampered_file["files"]["gene_stats"]["size_in_bytes"] += 1
    assert "content address" in put(good, d=tampered_file).json()["detail"]
    assert "start the push with :begin" in put(good, b="0" * 64).json()["detail"]
    assert "index is {location" in put(None).json()["detail"]
    elsewhere = {**good, "location": "s3://someone-elses-bucket/x"}
    assert "does not read the bucket" in put(elsewhere).json()["detail"]
    wrong_sha = json.loads(json.dumps(good))
    wrong_sha["files"]["rows.parquet"]["sha256"] = "0" * 64
    assert "not the file recorded at publish" in put(wrong_sha).json()["detail"]

    def other_subject(rows, metrics, edges):
        rows[0]["subject_id"] = "HGNC.SYMBOL:NOT-THIS-ONE"
    r = put(_index(tmp_path, bucket, ds, vocab, extract, other_subject))
    assert r.status_code == 400 and "rows are not what their values make" in r.json()["detail"]

    def higher_score(rows, metrics, edges):
        metrics[0] = (metrics[0][0], metrics[0][1], metrics[0][2] + 1)
    assert "metrics are not the rows' values" in put(_index(tmp_path, bucket, ds, vocab, extract, higher_score)).json()["detail"]

    def extra_edge(rows, metrics, edges):
        edges.append((dataset_id, "member_of", "HGNC.SYMBOL:FIXG9", edges[0][3], "FIXG9", edges[0][5]))
    assert "not what their values make" in put(_index(tmp_path, bucket, ds, vocab, extract, extra_edge)).json()["detail"]

    ok = put(_index(tmp_path, bucket, ds, vocab, extract))
    assert ok.status_code == 200, ok.text
    assert ok.json()["rows"] > 0 and not ok.json()["already_stored"]
    again = put(None)
    assert again.status_code == 200 and again.json()["already_stored"] and again.json()["rows"] == 0


def test_a_release_is_recorded_only_as_this_push_makes_it(api, token, bucket, tmp_path):
    spec = _spec(tmp_path, "0.4.0-finish", ("FIXFINISH", "FIXFINISH2"))
    col = load_collection(COLLECTION)
    sent = {}

    class Recording(push.RemoteTarget):
        def record_release(self, *args, **kw):
            sent["args"], sent["bundle"] = args, self.bundle
            raise PublishError("stopped before finishing")

    with pytest.raises(PublishError, match="stopped"):
        publish(None, col, spec, tmp_path, target=_target(api, token, col, spec["version"], bucket, Recording))
    _, release, document, objects, *_rest = sent["args"]
    provenance, code_version = sent["args"][6], sent["args"][10]

    def finish(rel=release, doc=document, version="0.4.0-finish"):
        return _send(api, token, "POST", f"{version}:finish", {"bundle": sent["bundle"], "release": rel,
                                                              "document": {**doc, "result_collections": [rel]},
                                                              "objects": [], "provenance": provenance,
                                                              "code_version": code_version})

    assert "not the one this push makes" in finish(version="0.4.0-other").json()["detail"]
    forged = idn.mint("ResultCollection", {**{k: v for k, v in release.items() if k != "id"},
                                           "was_revision_of": "dapper:ResultCollection.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"})
    r = finish(rel=forged)
    assert r.status_code == 400 and "became current meanwhile" in r.json()["detail"]
    unstored = idn.mint("ResultCollection", {**{k: v for k, v in release.items() if k != "id"},
                                             "members": [*release["members"], "dapper:Dataset.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"],
                                             "n_members": release["n_members"] + 1})
    assert "not stored for pigean" in finish(rel=unstored).json()["detail"]
    ok = finish()
    assert ok.status_code == 200 and ok.json()["published_by"] == PUBLISHER
    assert finish().json()["already_recorded"] is True

    changed = idn.mint("ResultCollection", {**{k: v for k, v in release.items() if k != "id"}, "name": "Renamed"})
    r = finish(rel=changed)
    assert r.status_code == 409 and "different content" in r.json()["detail"]


def test_bodies_are_bounded(api, token, monkeypatch):
    monkeypatch.setattr(publish_api, "MAX_INFLATED", 1000)
    r = _send(api, token, "POST", "1.0:check", {"datasets": ["x" * 50] * 100})
    assert r.status_code == 413
    r = httpx.post(f"{api}/v1/publish/pigean/1.0:check", content=b"not json",
                   headers={"Authorization": "Bearer " + token["access_token"]})
    assert r.status_code == 400


def test_the_contract_lists_the_publish_routes():
    contract = TestClient(app).get("/v1/openapi.yaml").text
    for path in ("/v1/publish/{collection}/{version}:begin", "/v1/publish/{collection}/{version}:check",
                 "/v1/publish/{collection}/{version}/datasets/{dataset_id}", "/v1/publish/{collection}/{version}:finish"):
        assert path in contract


def test_the_cli_publishes_with_api_and_workers(api, token, pushed_db, bucket, tmp_path):
    """`claims release publish ... --api URL --workers 2`, as a server runs it."""
    import os
    import shutil
    import subprocess
    import sys

    import yaml

    col_dir = tmp_path / "pigean"
    shutil.copytree(COLLECTION, col_dir)
    spec = _spec(tmp_path, "0.5.0-cli", ("FIXCLI", "FIXCLI2"))
    (col_dir / "releases").mkdir()
    (col_dir / "releases" / "0.5.0-cli.yaml").write_text(yaml.safe_dump(spec))
    env = {**{k: v for k, v in os.environ.items() if k not in ("CLAIMS_MYSQL_HOST", "CLAIMS_DB", "CLAIMS_INDEX_DIR")},
           "CLAIMS_TOKEN": token["access_token"], "XDG_CONFIG_HOME": str(tmp_path / "config")}
    base = [sys.executable, "-m", "claims_cli.main", "--db", str(tmp_path / "local.db"), "--index-dir",
            str(tmp_path / "index"), "release", "publish", str(col_dir), "0.5.0-cli"]
    out = subprocess.run(base + ["--api", api, "--index-to", bucket, "--workers", "2"], cwd=ROOT, env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr + out.stdout
    assert f"publishing to {api}" in out.stdout and f"at {api}" in out.stdout and "recorded     2 datasets" in out.stdout
    con = service_db.connect(pushed_db)
    try:
        assert con.execute("SELECT published_by FROM collection_releases WHERE version = '0.5.0-cli'").fetchone()[0] \
            == PUBLISHER
    finally:
        con.close()
    assert not (tmp_path / "index" / "datasets").exists()  # nothing went into the local deployment
    out = subprocess.run(base + ["--api", api], cwd=ROOT, env=env, capture_output=True, text=True)
    assert out.returncode != 0 and "--index-to" in out.stderr

    env.pop("CLAIMS_TOKEN")
    out = subprocess.run(base + ["--api", api, "--index-to", bucket], cwd=ROOT, env=env, capture_output=True, text=True)
    assert out.returncode != 0 and "not signed in" in out.stderr
    out = subprocess.run(base + ["--api", "http://api.example.org", "--index-to", bucket], cwd=ROOT, env=env,
                         capture_output=True, text=True)
    assert out.returncode != 0 and "not an https URL" in out.stderr


# --- review findings (2026-11-24) ---------------------------------------------------------

def test_a_bundle_never_reads_outside_itself(api, token, bucket):
    import yaml

    col = load_collection(COLLECTION)
    files = push.collection_files(col)
    manifest = yaml.safe_load(files["collection.yaml"])
    manifest["templates"] = ["../../../../../../../../etc/hosts"]
    sent = {k: v for k, v in files.items() if not k.startswith("templates/")}
    sent["collection.yaml"] = yaml.safe_dump(manifest)
    r = _send(api, token, "POST", "1.0:begin", {"files": sent, "index_location": bucket})
    assert r.status_code == 400 and "not sent" in r.json()["detail"] and "localhost" not in r.text
    before = set(publish_api._BUNDLE_DIR.iterdir())
    broken = {**files, "templates/gene.yaml": "not: [a template"}
    assert _send(api, token, "POST", "1.0:begin", {"files": broken, "index_location": bucket}).status_code == 400
    assert set(publish_api._BUNDLE_DIR.iterdir()) == before  # nothing left behind


def test_finish_stores_only_what_the_release_carries(api, token, bucket, tmp_path):
    spec = _spec(tmp_path, "0.6.0-objects", ("FIXOBJ", "FIXOBJ2"))
    col = load_collection(COLLECTION)
    sent = {}

    class Recording(push.RemoteTarget):
        def record_release(self, *args, **kw):
            sent["args"], sent["bundle"] = args, self.bundle
            raise PublishError("stopped before finishing")

    with pytest.raises(PublishError, match="stopped"):
        publish(None, col, spec, tmp_path, target=_target(api, token, col, spec["version"], bucket, Recording))
    _, release, document, _objects, *_ = sent["args"]
    provenance, code_version = sent["args"][6], sent["args"][10]

    def finish(doc=document, objects=(), prov=provenance):
        return _send(api, token, "POST", "0.6.0-objects:finish", {
            "bundle": sent["bundle"], "release": release, "document": doc, "objects": list(objects),
            "provenance": prov, "code_version": code_version})

    gene_set = idn.mint("GeneSet", {"name": "forged", "members": ["HGNC.SYMBOL:FIXG1"]})
    claim_like = idn.mint("Activity", {"name": "unrelated activity"})
    r = finish(objects=[gene_set])
    assert r.status_code == 400 and "never a class the service mints" in r.json()["detail"]
    r = finish(objects=[gene_set], prov=[{"ids": [gene_set["id"]]}])
    assert r.status_code == 400 and "never a class the service mints" in r.json()["detail"]
    r = finish(doc={**document, "activities": [*document["activities"], claim_like]})
    assert r.status_code == 400 and "not a record this release carries" in r.json()["detail"]
    r = finish(doc={**document, "used_edges": [{"subject": "dapper:Activity.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                                                "predicate": "prov:used", "object": release["id"]}]})
    assert r.status_code == 400 and "from a record of this release" in r.json()["detail"]
    r = finish(doc={**document, "extra_key": 1})
    assert r.status_code == 400
    tampered = json.loads(json.dumps(document))
    member_files = {e["file"] for d in document["datasets"] for e in d["file_roles"]}
    tampered["files"] = [idn.mint("File", {**{k: v for k, v in f.items() if k != "id"}, "mime_type": "text/plain"})
                         if f["id"] in member_files else f for f in tampered["files"]]
    r = finish(doc=tampered)
    assert r.status_code == 400  # a member's File record is recomputed from the collection's file roles
    assert finish().status_code == 200


def test_spans_must_quote_the_value(api, token, bucket, tmp_path):
    bundle = _begin(api, token, bucket, "9.9.8-spans")
    dataset_id, dataset, ds, vocab, extract = _one_dataset(tmp_path, "FIXSPAN")

    def forged_span(rows, metrics, edges):
        spans = json.loads(rows[0]["spans"])
        metric = next(iter(spans))
        spans[metric] = [spans[metric][0], 6, "1e-300"]
        rows[0]["spans"] = idn.canonical(spans)
    r = _send(api, token, "PUT", f"9.9.8-spans/datasets/{dataset_id}",
              {"bundle": bundle, "dataset": dataset, "index": _index(tmp_path, bucket, ds, vocab, extract, forged_span)})
    assert r.status_code == 400 and "byte span" in r.json()["detail"]


def test_object_collections_and_nan_are_refused(api, token, pushed_db, bucket):
    col = load_collection(COLLECTION)
    con = service_db.connect(pushed_db)
    try:
        with con:
            con.execute("INSERT INTO object_collections (collection_key, definition) VALUES ('pigean', '{}')")
        r = _send(api, token, "POST", "1.0:begin", {"files": push.collection_files(col), "index_location": bucket})
        assert r.status_code == 409 and "object collection" in r.json()["detail"]
    finally:
        with con:
            con.execute("DELETE FROM object_collections WHERE collection_key = 'pigean'")
        con.close()
    r = httpx.post(f"{api}/v1/publish/pigean/1.0:check", content=b'{"datasets": [NaN]}',
                   headers={"Authorization": "Bearer " + token["access_token"]})
    assert r.status_code == 400 and "NaN" in r.json()["detail"]


def test_a_dataset_being_stored_answers_busy_before_its_body(api, token):
    dataset_id = "dapper:Dataset." + "B" * 32
    _, digest = publish_api._lock_name("dataset", dataset_id)
    lock = publish_api._LOCAL_LOCKS[int(digest[:8], 16) % len(publish_api._LOCAL_LOCKS)]
    with lock:
        r = _send(api, token, "PUT", f"1.0/datasets/{dataset_id}", {"bundle": "0" * 64})
    assert r.status_code == 503 and r.headers["retry-after"] == "30"


def test_two_finishes_of_one_version_record_one_release(api, token, pushed_db, bucket, tmp_path):
    spec = _spec(tmp_path, "0.7.0-race", ("FIXRACE", "FIXRACE2"))
    col = load_collection(COLLECTION)
    captured = []

    class Recording(push.RemoteTarget):
        def record_release(self, *args, **kw):
            captured.append((args, self.bundle))
            raise PublishError("stopped")

    with pytest.raises(PublishError):
        publish(None, col, spec, tmp_path, target=_target(api, token, col, spec["version"], bucket, Recording))
    (args, bundle), = captured
    _, release, document, _o, *_ = args
    loaded = publish_api._bundle(service_db.connect(pushed_db), "pigean", bundle)
    bodies = []
    for code_version in ("aaaa", "bbbb"):
        publication = pub.publication_doc(col, "0.7.0-race", code_version)
        rel = pub.release_doc(col, "0.7.0-race", release["members"], publication["id"], release["was_revision_of"],
                              loaded.manifest_file, loaded.template_files, loaded.query_files)
        doc = json.loads(json.dumps(document).replace(release["was_generated_by"], publication["id"]))
        doc["result_collections"] = [rel]
        doc["activities"] = [publication if a["id"] == publication["id"] else a for a in doc["activities"]]
        bodies.append({"bundle": bundle, "release": rel, "document": doc, "objects": [], "provenance": args[6],
                       "code_version": code_version})
    results = []
    threads = [threading.Thread(target=lambda b=b: results.append(_send(api, token, "POST", "0.7.0-race:finish", b)))
               for b in bodies]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(r.status_code for r in results) == [200, 409], [r.text for r in results]
    con = service_db.connect(pushed_db)
    try:
        current = con.execute("SELECT current_release_id FROM collections WHERE collection_key = 'pigean'").fetchone()[0]
        assert con.execute("SELECT 1 FROM collection_releases WHERE release_id = ?", (current,)).fetchone()
    finally:
        con.close()


def test_an_index_changed_in_the_bucket_is_not_served(api, token, pushed_db, bucket, tmp_path):
    """The service serves only the bytes recorded at publish: a file the owner rewrote afterwards is refused."""
    import shutil

    from service.collections import store as cstore

    spec = _spec(tmp_path, "0.8.0-changed", ("FIXCHANGED", "FIXCHANGED2"))
    _push(api, token, bucket, spec, tmp_path)
    con = service_db.connect(pushed_db)
    try:
        locations = [r[0] for r in con.execute(
            "SELECT location FROM dataset_indexes WHERE dataset_id IN (SELECT dataset_id FROM release_datasets d JOIN "
            "collection_releases r ON r.release_id = d.release_id WHERE r.version = '0.8.0-changed')").fetchall()]
    finally:
        con.close()
    cache = cstore.bucket_store().cache
    shutil.rmtree(cache.root / "datasets", ignore_errors=True)
    (cache.root / "datasets").mkdir()
    assert len(locations) == 2
    for location in locations:
        rows = Path(location[len("file://"):]) / "rows.parquet"
        rows.write_bytes(rows.read_bytes() + b"tampered")
    r = TestClient(app).post(f"/v1/collections/pigean/queries/{QUERY}:run",
                             json={"variables": {"trait": "FIXCHANGED", "library": "fixture-v1", "topGenes": 5}})
    assert r.status_code >= 400 or "not the file recorded at publish" in r.text


def test_another_collection_may_hold_the_same_files(api, token, pushed_db, bucket, tmp_path):
    """One result file is one dataset within a collection; another collection over the same files (its claim
    ids differ) is no conflict."""
    import re
    import shutil

    spec = _spec(tmp_path, "0.9.0-shared", ("FIXSHARED", "FIXSHARED2"))
    _push(api, token, bucket, spec, tmp_path)
    looser = {**spec, "version": "0.9.1-shared",
              "datasets": [{**d, "where": {"gene_phenotype": {"combined": {"gt": 0}}}} for d in spec["datasets"]]}
    with pytest.raises(PublishError, match="other `where` bounds"):
        _push(api, token, bucket, looser, tmp_path)

    other = tmp_path / "other-collection"
    shutil.copytree(COLLECTION, other)
    manifest = (other / "collection.yaml").read_text()
    manifest = re.sub(r"(?m)^collection: .*$", "collection: pigean-other", manifest, count=1)
    (other / "collection.yaml").write_text(re.sub(r"(?m)^title: .*$", "title: PIGEAN (other)", manifest, count=1))
    col = load_collection(other)
    target = _target(api, token, col, "0.1.0", bucket)
    out = publish(None, col, {**looser, "version": "0.1.0"}, tmp_path, target=target)
    assert out.complete and out.stats["datasets_ingested"] == 2


def test_prewarm_reads_every_current_release_into_a_fresh_cache(api, token, pushed_db, bucket, tmp_path, monkeypatch):
    """store.prewarm (CLAIMS_INDEX_PREWARM=1 at startup) reads every dataset of every current release that has
    a bucket index into the cache, before any query asks."""
    from service import app as service_app
    from service.collections import store as cstore

    spec = _spec(tmp_path, "0.9.0-prewarm", ("FIXWARM", "FIXWARM2"))
    assert _push(api, token, bucket, spec, tmp_path).complete
    fresh = tmp_path / "fresh-cache"
    monkeypatch.setenv("CLAIMS_INDEX_CACHE", str(fresh))
    monkeypatch.setattr(cstore, "_BUCKET", {})
    con = service_db.connect(pushed_db)
    try:
        current = {r[0] for r in con.execute("SELECT di.dataset_id FROM dataset_indexes di JOIN release_datasets rd "
                                             "ON rd.dataset_id = di.dataset_id JOIN collections c ON "
                                             "c.current_release_id = rd.release_id").fetchall()}
    finally:
        con.close()
    assert current
    monkeypatch.setenv("CLAIMS_INDEX_PREWARM", "1")
    thread = service_app.start_index_prewarm()
    thread.join(60)
    store = cstore.bucket_store()
    assert set(store.known) >= current
    assert all((store.cache.dir(d) / ".checked").exists() for d in current)
    monkeypatch.delenv("CLAIMS_INDEX_PREWARM")
    assert service_app.start_index_prewarm() is None
