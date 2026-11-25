"""Publishing into a deployed service with a `claims login` token (docs/publishing-to-a-deployment.md).

The acceptance test: the same release published directly into one database and pushed over HTTP into
another holds the same rows, objects, mint contexts and release record, and serves the same claims. The
rest are what the API refuses."""
from __future__ import annotations

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
from service.collections.manifest import load_collection, load_release
from service.collections.publish import PublishError, publish

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
COLLECTION = FIX / "collections" / "pigean"
PIGEAN_FIX = FIX / "pigean"
ROLES = {"gene_stats": "combined_gene_stats.json", "gene_set_stats": "combined_gene_set_stats.json",
         "membership": "gene_gene_set_stats.json"}
PUBLISHER = "publisher@example.org"
QUERY = "top-genes-with-sets"


@pytest.fixture(scope="module")
def pushed_db():
    """The deployed service's database for this module (the server thread connects through DB_PATH)."""
    saved = service_db.DB_PATH
    service_db.DB_PATH = str(Path(tempfile.mkdtemp()) / "pushed.db")
    yield service_db.DB_PATH
    service_db.DB_PATH = saved


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


def _push(api, token, spec, base, **kw):
    col = load_collection(COLLECTION)
    target = push.RemoteTarget(api, token["access_token"], col, spec["version"])
    return publish(None, col, spec, base, target=target, **kw)


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


def _served(db_path: str) -> list:
    saved = service_db.DB_PATH
    service_db.DB_PATH = db_path
    try:
        r = TestClient(app).post(f"/v1/collections/pigean/queries/{QUERY}:run",
                                 json={"variables": {"trait": "FIXPHENO2", "library": "fixture-v1", "topGenes": 5}})
        assert r.status_code == 200, r.text
        return [(row["subject"]["id"], sorted((k, c["id"]) for k, c in row["claims"].items()),
                 [(i["row"]["subject"]["id"], sorted((k, c["id"]) for k, c in i["edge"]["claims"].items()))
                  for i in row.get("sets", [])]) for row in r.json()["rows"]]
    finally:
        service_db.DB_PATH = saved


def test_a_pushed_release_is_the_directly_published_release(api, token, pushed_db, tmp_path):
    spec = _spec(tmp_path, "0.2.0-push")
    col = load_collection(COLLECTION)
    direct_db = str(tmp_path / "direct.db")
    con = service_db.connect(direct_db)
    try:
        direct = publish(con, col, spec, tmp_path)
    finally:
        con.close()

    pushed = _push(api, token, spec, tmp_path)
    assert pushed.release_id == direct.release_id and pushed.complete
    assert pushed.stats == direct.stats and direct.stats["rows"] > 0 and direct.stats["edges"] > 0

    a, b = service_db.connect(direct_db), service_db.connect(pushed_db)
    try:
        want, got = _snapshot(a), _snapshot(b)
        for table in want:
            assert got[table] == want[table], table
        assert tuple(b.execute("SELECT published_by, publisher_token_id FROM collection_releases").fetchone()) == \
            (PUBLISHER, token["token_id"])
        assert a.execute("SELECT published_by FROM collection_releases").fetchone()[0] is None
        events = [r[0] for r in b.execute("SELECT event FROM admin_auth_events WHERE email = ? ORDER BY id",
                                          (PUBLISHER,)).fetchall()]
        assert events[-2:] == ["release_publish_started", "release_published"]
    finally:
        a.close()
        b.close()

    again = _push(api, token, spec, tmp_path)   # nothing left to do: nothing changes
    assert again.release_id == direct.release_id and again.stats["datasets_ingested"] == 0
    b = service_db.connect(pushed_db)
    try:
        assert _snapshot(b) == got
    finally:
        b.close()
    assert _served(pushed_db) == _served(direct_db) != []


def test_a_push_resumes_and_is_recorded_only_when_whole(api, token, pushed_db, tmp_path):
    spec = _spec(tmp_path, "0.3.0-resume", ("FIXRESUME", "FIXRESUME2"))
    first = _push(api, token, spec, tmp_path, max_ingest=1)
    assert not first.complete and first.stats["datasets_ingested"] == 1
    con = service_db.connect(pushed_db)
    try:
        assert con.execute("SELECT 1 FROM collection_releases WHERE version = '0.3.0-resume'").fetchone() is None
    finally:
        con.close()
    rest = _push(api, token, spec, tmp_path)
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


def _begin(api, token, version="9.9.9-refused"):
    col = load_collection(COLLECTION)
    r = _send(api, token, "POST", f"{version}:begin", {"files": push.collection_files(col), "prefix": "tests/fixtures/collections/pigean"})
    assert r.status_code == 200, r.text
    return r.json()["bundle"]


def _one_dataset(tmp_path, phenotype="FIXREF"):
    """A dataset's request body as RemoteTarget sends it, from a direct publish's plan."""
    from service.collections import publish as pub
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
    return ds["doc"]["id"], dataset, extract


def test_no_token_no_push(api):
    assert _send(api, None, "POST", "1.0:begin", {"files": {}}).status_code == 401
    bad = {"access_token": "clp_" + "A" * 43}
    assert _send(api, bad, "POST", "1.0:begin", {"files": {}}).status_code == 401


def test_the_collection_files_are_checked(api, token):
    col = load_collection(COLLECTION)
    files = push.collection_files(col)
    for body, match in (({"files": {**files, "../evil.yaml": "x"}}, "relative path"),
                        ({"files": {k: v for k, v in files.items() if k != "collection.yaml"}}, "no collection.yaml"),
                        ({"files": {**files, "extra.yaml": "a: 1"}}, "does not use"),
                        ({"files": files, "prefix": "../up"}, "prefix")):
        r = _send(api, token, "POST", "1.0:begin", body)
        assert r.status_code == 400 and match in r.json()["detail"], r.text
    r = httpx.post(f"{api}/v1/publish/other/1.0:begin", json={"files": files},
                   headers={"Authorization": "Bearer " + token["access_token"]})
    assert r.status_code == 400 and "not 'other'" in r.json()["detail"]


def test_a_dataset_is_what_its_values_make(api, token, tmp_path):
    bundle = _begin(api, token)
    dataset_id, dataset, extract = _one_dataset(tmp_path)

    def put(ds=dataset, ex=extract, did=dataset_id, b=bundle):
        return _send(api, token, "PUT", f"9.9.9-refused/datasets/{did}", {"bundle": b, "dataset": ds, "extract": ex})

    other_trait = {**dataset, "raw_dimensions": {**dataset["raw_dimensions"], "trait": "OTHER"}}
    r = put(ds=other_trait)
    assert r.status_code == 400 and "not the dataset these values make" in r.json()["detail"]
    tampered_file = json.loads(json.dumps(dataset))
    tampered_file["files"]["gene_stats"]["size_in_bytes"] += 1
    assert "content address" in put(ds=tampered_file).json()["detail"]
    below = json.loads(json.dumps(extract))
    below["rows"]["gene_phenotype"][0]["row_json"] = json.dumps({"x": 1})
    assert "do not match its row" in put(ex=below).json()["detail"]
    other_phenotype = json.loads(json.dumps(extract))
    row = json.loads(other_phenotype["rows"]["gene_phenotype"][0]["row_json"])
    row["phenotype"] = "ELSEWHERE"
    other_phenotype["rows"]["gene_phenotype"][0]["row_json"] = idn.canonical(row)
    assert "does not match the dataset's trait" in put(ex=other_phenotype).json()["detail"]
    assert "not stored yet" in put(ex=None).json()["detail"]
    assert "start the push with :begin" in put(b="0" * 64).json()["detail"]

    ok = put()
    assert ok.status_code == 200 and ok.json()["rows"] > 0 and not ok.json()["already_stored"]
    again = put()
    assert again.status_code == 200 and again.json()["already_stored"] and again.json()["rows"] == 0


def test_a_release_is_recorded_only_as_this_push_makes_it(api, token, tmp_path):
    spec = _spec(tmp_path, "0.4.0-finish", ("FIXFINISH", "FIXFINISH2"))
    col = load_collection(COLLECTION)
    sent = {}

    class Recording(push.RemoteTarget):
        def record_release(self, *args, **kw):
            sent["args"], sent["bundle"] = args, self.bundle
            raise PublishError("stopped before finishing")

    with pytest.raises(PublishError, match="stopped"):
        publish(None, col, spec, tmp_path, target=Recording(api, token["access_token"], col, spec["version"]))
    _, release, document, objects, *_rest = sent["args"]
    provenance, code_version = sent["args"][6], sent["args"][10]

    def finish(rel=release, doc=document, version="0.4.0-finish"):
        return _send(api, token, "POST", f"{version}:finish", {"bundle": sent["bundle"], "release": rel,
                                                              "document": {**doc, "result_collections": [rel]},
                                                              "objects": [], "provenance": provenance,
                                                              "code_version": code_version})

    assert "not pigean 0.4.0-other" in finish(version="0.4.0-other").json()["detail"]
    forged = idn.mint("ResultCollection", {**{k: v for k, v in release.items() if k != "id"},
                                           "was_revision_of": "dapper:ResultCollection.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"})
    r = finish(rel=forged)
    assert r.status_code == 400 and "was_revision_of" in r.json()["detail"]
    unstored = idn.mint("ResultCollection", {**{k: v for k, v in release.items() if k != "id"},
                                             "members": [*release["members"], "dapper:Dataset.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"],
                                             "n_members": release["n_members"] + 1})
    assert "no Dataset record" in finish(rel=unstored).json()["detail"]
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


def test_the_cli_publishes_with_api_and_workers(api, token, pushed_db, tmp_path):
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
    out = subprocess.run(base + ["--api", api, "--workers", "2"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr + out.stdout
    assert f"publishing to {api}" in out.stdout and f"at {api}" in out.stdout and "recorded     2 datasets" in out.stdout
    con = service_db.connect(pushed_db)
    try:
        assert con.execute("SELECT published_by FROM collection_releases WHERE version = '0.5.0-cli'").fetchone()[0] \
            == PUBLISHER
    finally:
        con.close()
    assert not (tmp_path / "index" / "datasets").exists()  # nothing went into the local deployment

    env.pop("CLAIMS_TOKEN")
    out = subprocess.run(base + ["--api", api], cwd=ROOT, env=env, capture_output=True, text=True)
    assert out.returncode != 0 and "not signed in" in out.stderr
    out = subprocess.run(base + ["--api", "http://api.example.org"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert out.returncode != 0 and "not an https URL" in out.stderr
