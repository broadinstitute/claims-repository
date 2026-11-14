"""The admin API over collections (service/admin.py): overview, collection
detail, the example query, the claims list and claim detail (which never
mint or assert), and collection registration (check, background publish
jobs, path safety and audit) against the fixture PIGEAN collection."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import jwt
import pytest
import yaml
from fastapi.testclient import TestClient

from service import admin_gateway
from service import db as service_db
from service.app import app
from service.collections import publish as pub
from service.collections import registration as reg
from service.collections.manifest import load_collection, load_release

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
COLLECTION = FIX / "collections" / "pigean"
RELEASE = FIX / "collections" / "pigean-fixture-release.yaml"
QUERY = "top-genes-with-sets"
SECRET = "test-gateway-secret-at-least-32-characters"
ADMIN = "admin@example.org"
client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def isolated_db():
    saved = service_db.DB_PATH
    service_db.DB_PATH = str(Path(tempfile.mkdtemp()) / "admin-collections.db")
    yield service_db.DB_PATH
    service_db.DB_PATH = saved


@pytest.fixture(autouse=True)
def gateway(monkeypatch):
    monkeypatch.setattr(admin_gateway, "SECRET", SECRET)
    monkeypatch.setattr(admin_gateway, "ADMIN_EMAILS", {ADMIN})
    monkeypatch.delenv(reg.ENV, raising=False)


def _token(purpose):
    now = int(time.time())
    return jwt.encode({"iss": "claims-admin", "aud": "claims-api", "iat": now, "exp": now + 120,
                       "jti": str(uuid.uuid4()), "sub": ADMIN, "purpose": purpose, "email": ADMIN,
                       "email_verified": True}, SECRET, algorithm="HS256")


def _get(path, **params):
    return client.get(f"/v1/admin/api/{path}", params=params,
                      headers={"Authorization": f"Bearer {_token('admin_read')}"})


def _post(path, body):
    return client.post(f"/v1/admin/api/{path}", json=body,
                       headers={"Authorization": f"Bearer {_token('admin_write')}"})


def _sql(sql, params=()):
    con = service_db.connect()
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def _claim_counts():
    return _sql("SELECT COUNT(*) AS n, COALESCE(SUM(times_seen), 0) AS s FROM dapper_objects "
                "WHERE class_name = 'Claim'")[0]


# Every table a publish writes, and the audit log: a check must leave them all as they were.
WRITTEN_TABLES = ("collections", "collection_releases", "release_datasets", "dataset_dimensions", "dataset_files",
                  "mint_contexts", "release_mint_contexts", "result_rows", "result_metrics", "result_edges",
                  "result_claims", "dapper_objects", "admin_auth_events")


def _table_counts():
    return {t: _sql(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in WRITTEN_TABLES}


def _fixture_state_when_unpublishable():
    """release.state for the fixture collection's version when its datasets
    cannot be read: new until the fixture release is published, then unknown."""
    if not _sql("SELECT 1 FROM collections WHERE collection_key = 'pigean'"):
        return "new-collection"
    stored = _sql("SELECT 1 FROM collection_releases WHERE collection_key = 'pigean' AND version = '0.0.1-test'")
    return "unknown" if stored else "new-version"


def _wait(job_id, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = _get(f"registration/jobs/{job_id}").json()
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} still running after {timeout}s")


def _fixture_tree(tmp_path) -> tuple[Path, Path]:
    """A copy of the fixture collection, release and result files, in the
    same relative layout, to break or move without touching the originals."""
    shutil.copytree(COLLECTION, tmp_path / "collections" / "pigean")
    shutil.copy(RELEASE, tmp_path / "collections" / RELEASE.name)
    shutil.copytree(FIX / "pigean", tmp_path / "pigean")
    return tmp_path / "collections" / "pigean", tmp_path / "collections" / RELEASE.name


def _edit(path, change):
    doc = yaml.safe_load(path.read_text())
    change(doc)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def _publish():
    con = service_db.connect()
    try:
        spec, base = load_release(RELEASE)
        return pub.publish(con, load_collection(COLLECTION), spec, base)
    finally:
        con.close()


@pytest.fixture(scope="module")
def published(isolated_db):
    return _publish()  # a no-op when the registration job already published it


# --- registration: switch and path safety -------------------------------------

def test_registration_is_off_without_roots():
    sources = _get("registration/sources")
    assert sources.status_code == 200
    assert sources.json() == {"enabled": False, "roots": [], "collections": []}
    body = {"collectionDir": str(COLLECTION), "releaseFile": str(RELEASE)}
    for response in (_post("registration/check", body), _post("registration/jobs", body),
                     _get("registration/jobs"), _get("registration/jobs/abc")):
        assert response.status_code == 403
        assert response.json()["detail"] == reg.DISABLED
    assert _get("overview").json()["registration"] == {"enabled": False, "roots": []}


def test_paths_outside_the_roots_are_refused(monkeypatch, tmp_path):
    inside, release = _fixture_tree(tmp_path / "root")
    monkeypatch.setenv(reg.ENV, str(tmp_path / "root"))
    (tmp_path / "root" / "escape").symlink_to(COLLECTION)
    cases = [
        {"collectionDir": str(COLLECTION), "releaseFile": str(release)},              # outside
        {"collectionDir": str(inside), "releaseFile": str(RELEASE)},                  # outside
        {"collectionDir": str(tmp_path / "root" / "escape"), "releaseFile": str(release)},  # symlink out
        {"collectionDir": str(inside / ".." / ".." / ".." / "x"), "releaseFile": str(release)},  # ..
        {"collectionDir": "collections/pigean", "releaseFile": str(release)},         # relative
    ]
    for body in cases:
        for path in ("registration/check", "registration/jobs"):
            refused = _post(path, body)
            assert refused.status_code == 400, (path, body)
            assert body["collectionDir"] in refused.json()["detail"] or body["releaseFile"] in refused.json()["detail"]
    # A release naming a result file outside the roots: the check reports it, a job refuses it.
    _edit(release, lambda d: d["datasets"][0]["files"]["gene_stats"].__setitem__(
        "path", str(FIX / "pigean" / "combined_gene_stats.json")))
    body = {"collectionDir": str(inside), "releaseFile": str(release)}
    checked = _post("registration/check", body).json()
    assert not checked["ok"]
    steps = {s["id"]: s for s in checked["steps"]}
    assert steps["release"]["status"] == "warning" and checked["release"]["releaseId"] is None
    assert checked["release"]["state"] == _fixture_state_when_unpublishable()
    assert steps["datasets"]["status"] == "error" and steps["samples"]["status"] == "skipped"
    assert any("outside the collection roots" in m for m in steps["datasets"]["messages"])
    gene = next(f for f in checked["datasets"][0]["files"] if f["role"] == "gene_stats")
    assert gene["insideRoots"] is False and gene["exists"] is False and gene["sha256"] is None
    refused = _post("registration/jobs", body)
    assert refused.status_code == 400 and "combined_gene_stats.json" in refused.json()["detail"]
    assert reg.jobs() == [] or all(j["releaseFile"] != str(release) for j in reg.jobs())


def test_sources_lists_collections_under_the_roots(monkeypatch, tmp_path):
    collection, release = _fixture_tree(tmp_path)
    (collection / "releases").mkdir()
    shutil.copy(release, collection / "releases" / "0.0.1.yaml")
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    shutil.copy(collection / "collection.yaml", tmp_path / "node_modules" / "x" / "collection.yaml")
    monkeypatch.setenv(reg.ENV, str(tmp_path))
    body = _get("registration/sources").json()
    assert body["enabled"] and body["roots"] == [str(tmp_path.resolve())]
    assert body["collections"] == [{
        "path": str(collection.resolve()), "key": "pigean", "title": "PIGEAN gene and gene-set associations",
        "releases": [{"path": str((collection / "releases" / "0.0.1.yaml").resolve()), "version": "0.0.1-test"}]}]


# --- registration: the check ---------------------------------------------------

def test_a_broken_manifest_fails_the_first_step_and_skips_the_rest(monkeypatch, tmp_path):
    collection, release = _fixture_tree(tmp_path)
    _edit(collection / "templates" / "gene.yaml",
          lambda d: d["claims"][1].__setitem__("classify", "no-such-rule"))
    monkeypatch.setenv(reg.ENV, str(tmp_path))
    checked = _post("registration/check", {"collectionDir": str(collection), "releaseFile": str(release)})
    assert checked.status_code == 200
    body = checked.json()
    assert body["ok"] is False and body["collection"] is None and body["samples"] == []
    assert [(s["id"], s["status"]) for s in body["steps"]] == [
        ("manifest", "error"), ("git", "skipped"), ("release", "skipped"), ("datasets", "skipped"),
        ("samples", "skipped")]
    assert "unknown tier rule" in body["steps"][0]["messages"][0]


def test_a_missing_result_file_is_a_dataset_error(monkeypatch, tmp_path):
    collection, release = _fixture_tree(tmp_path)
    (tmp_path / "pigean" / "gene_gene_set_stats.json").unlink()
    monkeypatch.setenv(reg.ENV, str(tmp_path))
    body = _post("registration/check", {"collectionDir": str(collection), "releaseFile": str(release)}).json()
    steps = {s["id"]: s["status"] for s in body["steps"]}
    assert steps == {"manifest": "ok", "git": "ok", "release": "warning", "datasets": "error", "samples": "skipped"}
    assert body["release"]["releaseId"] is None and body["git"]["commit"] is None
    assert body["release"]["state"] == _fixture_state_when_unpublishable() and body["ok"] is False
    membership = next(f for f in body["datasets"][0]["files"] if f["role"] == "membership")
    assert membership["exists"] is False and membership["insideRoots"] is True


def test_dataset_problems_under_a_published_version_are_reported(monkeypatch, tmp_path, published):
    """The version is stored, but its content cannot be compared: state
    unknown (not conflict), and the dataset problems are all shown."""
    collection, release = _fixture_tree(tmp_path / "root")
    (tmp_path / "root" / "pigean" / "gene_gene_set_stats.json").unlink()
    _edit(release, lambda d: d["datasets"][0]["files"]["gene_stats"].__setitem__(
        "path", str(FIX / "pigean" / "combined_gene_stats.json")))
    monkeypatch.setenv(reg.ENV, str(tmp_path / "root"))
    body = _post("registration/check", {"collectionDir": str(collection), "releaseFile": str(release)}).json()
    assert body["ok"] is False
    assert [(s["id"], s["status"]) for s in body["steps"]] == [
        ("manifest", "ok"), ("git", "ok"), ("release", "warning"), ("datasets", "error"), ("samples", "skipped")]
    assert body["release"]["state"] == "unknown" and body["release"]["releaseId"] is None
    assert body["release"]["currentVersion"] == "0.0.1-test"
    steps = {s["id"]: s for s in body["steps"]}
    assert published.release_id in steps["release"]["messages"][0]
    assert "cannot be compared" in steps["release"]["messages"][1]
    messages = steps["datasets"]["messages"]
    assert any("gene_stats" in m and "outside the collection roots" in m for m in messages)
    assert any("membership" in m and "does not exist" in m for m in messages)
    files = {f["role"]: f for f in body["datasets"][0]["files"]}
    assert files["gene_stats"]["insideRoots"] is False and files["gene_stats"]["exists"] is False
    assert files["membership"]["insideRoots"] is True and files["membership"]["exists"] is False
    assert files["gene_set_stats"]["insideRoots"] is True and files["gene_set_stats"]["exists"] is True


def test_a_non_object_ndjson_line_is_a_dataset_error(monkeypatch, tmp_path):
    collection, release = _fixture_tree(tmp_path)
    stats = tmp_path / "pigean" / "combined_gene_stats.json"
    lines = stats.read_text().splitlines()
    stats.write_text("\n".join([lines[0], "[1, 2]", *lines[2:]]) + "\n")
    monkeypatch.setenv(reg.ENV, str(tmp_path))
    # A database of its own: under the published fixture version this changed
    # content is a conflict, which stops the check before the rows are read.
    monkeypatch.setattr(service_db, "DB_PATH", str(tmp_path / "ndjson.db"))
    checked = _post("registration/check", {"collectionDir": str(collection), "releaseFile": str(release)})
    assert checked.status_code == 200
    steps = {s["id"]: s for s in checked.json()["steps"]}
    assert steps["datasets"]["status"] == "error" and steps["samples"]["status"] == "skipped"
    assert any("combined_gene_stats.json line 2: expected a JSON object, got list" in m
               for m in steps["datasets"]["messages"]), steps["datasets"]["messages"]


def test_a_template_outside_the_collection_directory_is_a_manifest_error(monkeypatch, tmp_path):
    collection, release = _fixture_tree(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.move(collection / "templates" / "gene.yaml", elsewhere / "gene.yaml")
    _edit(collection / "collection.yaml", lambda d: d["templates"].__setitem__(
        d["templates"].index("templates/gene.yaml"), str(elsewhere / "gene.yaml")))
    monkeypatch.setenv(reg.ENV, str(tmp_path))
    checked = _post("registration/check", {"collectionDir": str(collection), "releaseFile": str(release)})
    assert checked.status_code == 200
    manifest = checked.json()["steps"][0]
    assert manifest["status"] == "error"
    assert "template and query paths must be inside it" in manifest["messages"][0]


def test_an_unusable_path_is_a_400(monkeypatch, tmp_path):
    collection, release = _fixture_tree(tmp_path)
    monkeypatch.setenv(reg.ENV, str(tmp_path))
    for body in ({"collectionDir": str(collection) + "\x00x", "releaseFile": str(release)},
                 {"collectionDir": str(collection), "releaseFile": str(release) + "\x00"}):
        for path in ("registration/check", "registration/jobs"):
            refused = _post(path, body)
            assert refused.status_code == 400, (path, refused.text)
            assert "not a usable path" in refused.json()["detail"]


def _git_cmd(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org"})


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_the_git_check_runs_no_repository_configured_command(monkeypatch, tmp_path):
    collection, release = _fixture_tree(tmp_path / "repo")
    repo = tmp_path / "repo"
    _git_cmd(repo, "init", "-q")
    _git_cmd(repo, "add", "-A")
    _git_cmd(repo, "commit", "-q", "-m", "fixture")
    marker = tmp_path / "fsmonitor-ran"
    _git_cmd(repo, "config", "core.fsmonitor", f"touch {marker}; false")
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()
    assert pub._git_info(collection / "collection.yaml") == (head, False, "collections/pigean/collection.yaml")
    _edit(collection / "collection.yaml", lambda d: d.__setitem__("description", "edited"))  # now dirty
    monkeypatch.setenv(reg.ENV, str(tmp_path))
    body = _post("registration/check", {"collectionDir": str(collection), "releaseFile": str(release)}).json()
    assert body["git"] == {"commit": head, "dirty": True, "codeVersion": f"{head}-dirty"}
    assert not marker.exists()
    # The dirty tree is reported once, by the git step (not again by the dry run).
    assert sum("uncommitted changes" in w for w in body["warnings"]) == 1, body["warnings"]


def test_check_then_publish_job_records_exactly_the_sampled_claims(monkeypatch):
    monkeypatch.setenv(reg.ENV, str(FIX))
    before = _sql("SELECT COUNT(*) AS n FROM result_claims")[0]["n"]
    tables = _table_counts()
    body = {"collectionDir": str(COLLECTION), "releaseFile": str(RELEASE), "sample": 10}
    checked = _post("registration/check", body)
    assert checked.status_code == 200
    report = checked.json()
    assert report["ok"] is True, report["steps"]
    statuses = {s["id"]: s["status"] for s in report["steps"]}
    assert statuses["manifest"] == statuses["release"] == statuses["samples"] == "ok"
    assert statuses["git"] in ("ok", "warning")  # the fixture may be uncommitted
    # FIXPHENO is not in this database's (empty) trait registry: kept as given, with a warning.
    assert statuses["datasets"] == "warning" and any("FIXPHENO" in w for w in report["warnings"])
    assert report["datasets"][0]["dimensions"]["trait"]["resolved"] is False
    expected = "already-published" if before else "new-collection"
    assert report["release"]["state"] == expected
    assert report["release"]["releaseId"].startswith("dapper:ResultCollection.")
    assert report["collection"]["key"] == "pigean" and len(report["collection"]["sourceFiles"]) == 5
    assert {t["name"] for t in report["collection"]["templates"]} == {"gene", "gene_set", "roster"}
    ds = report["datasets"][0]
    assert ds["dimensions"]["trait"]["raw"] == "FIXPHENO" and ds["datasetId"].startswith("dapper:Dataset.")
    assert all(f["exists"] and f["insideRoots"] and len(f["sha256"]) == 64 for f in ds["files"])
    sampled = {c["claimId"] for s in report["samples"] for c in s["claims"]}
    assert len(sampled) == 20  # every row and group of the fixture: 3 x 4 + 2 x 3 + 2 x 1
    assert _table_counts() == tables  # the check wrote nothing

    started = _post("registration/jobs", {"collectionDir": str(COLLECTION), "releaseFile": str(RELEASE)})
    assert started.status_code == 202
    job = _wait(started.json()["id"])
    assert job["state"] == "succeeded", job["error"]
    assert job["releaseId"] == report["release"]["releaseId"] and job["version"] == "0.0.1-test"
    assert job["collection"] == "pigean" and job["startedBy"] == ADMIN and job["finishedAt"]
    assert job["phase"] == "Done" and job["error"] is None and isinstance(job["stats"], dict)
    recorded = {r["claim_id"] for r in _sql("SELECT claim_id FROM result_claims")}
    assert sampled <= recorded and len(recorded) == 20
    assert _get("registration/jobs").json()["jobs"][0]["id"] == job["id"]

    events = [(e["event"], e["email"], e["detail"]) for e in _get("activity").json()["authEvents"]]
    assert ("collection_publish_started", ADMIN, "pigean 0.0.1-test") in events
    assert ("collection_publish_succeeded", ADMIN, "pigean 0.0.1-test") in events

    again = _post("registration/check", body).json()
    assert again["release"]["state"] == "already-published" and again["datasets"][0]["alreadyIngested"]


def test_a_changed_template_under_a_published_version_is_a_conflict(monkeypatch, tmp_path, published):
    collection, release = _fixture_tree(tmp_path)
    _edit(collection / "templates" / "roster.yaml",
          lambda d: d["claims"][0].__setitem__("name", "PIGEAN roster (reworded): {group.label}"))
    monkeypatch.setenv(reg.ENV, str(tmp_path))
    body = _post("registration/check", {"collectionDir": str(collection), "releaseFile": str(release)}).json()
    assert body["ok"] is False and body["release"]["state"] == "conflict"
    assert [s["status"] for s in body["steps"]][2:] == ["error", "skipped", "skipped"]


def test_one_job_at_a_time_and_failures_are_reported(monkeypatch, published):
    monkeypatch.setenv(reg.ENV, str(FIX))
    entered, release_it = threading.Event(), threading.Event()

    def blocking_publish(con, col, spec, base, **kwargs):
        kwargs["progress"]("Pretending to work")
        entered.set()
        release_it.wait(30)
        raise pub.PublishError("stopped by the test")
    monkeypatch.setattr(pub, "publish", blocking_publish)
    body = {"collectionDir": str(COLLECTION), "releaseFile": str(RELEASE)}
    first = _post("registration/jobs", body)
    assert first.status_code == 202 and first.json()["state"] == "running"
    try:
        assert entered.wait(30)
        assert _get(f"registration/jobs/{first.json()['id']}").json()["phase"] == "Pretending to work"
        second = _post("registration/jobs", body)
        assert second.status_code == 409
    finally:
        release_it.set()
    job = _wait(first.json()["id"])
    assert job["state"] == "failed" and job["error"] == "stopped by the test"
    events = [(e["event"], e["detail"]) for e in _get("activity").json()["authEvents"]]
    assert ("collection_publish_failed", "pigean 0.0.1-test") in events
    assert _get("registration/jobs/no-such-job").status_code == 404


# --- the read API ---------------------------------------------------------------

def test_overview_and_collection_summary(published):
    ov = _get("overview").json()
    assert ov["claims"]["mintable"] == 20
    [summary] = ov["collections"]
    assert summary["key"] == "pigean" and summary["methods"] == ["PIGEAN"]
    assert summary["owners"] == ["dig-aggregator-pipeline"]
    assert summary["currentRelease"]["id"] == published.release_id
    assert summary["currentRelease"]["version"] == "0.0.1-test"
    assert summary["releases"] == 1 and summary["datasets"] == 1 and summary["claims"]["mintable"] == 20
    assert _get("collections").json() == {"collections": ov["collections"]}


def test_collection_detail(published):
    d = _get("collections/pigean").json()
    assert d["release"]["id"] == published.release_id and d["release"]["isCurrent"]
    assert [r["id"] for r in d["releaseHistory"]] == [published.release_id]
    assert d["dapperProfile"] == "0.2.1-a1"
    assert [x["name"] for x in d["dimensions"]] == ["trait", "library"]
    assert d["dimensions"][0]["resolve"] == "kpn-traits"
    assert {m["name"] for m in d["metrics"]} == {"combined", "log_bf", "prior", "beta", "beta_uncorrected"}
    assert set(d["rowKinds"]) == {"gene_phenotype", "gene_set_phenotype"} and set(d["edgeKinds"]) == {"member_of"}
    [ds] = d["datasets"]
    assert ds["dimensions"]["trait"] == {"value": "FIXPHENO", "label": "FIXPHENO"}
    assert ds["counts"] == {"gene_phenotype": 3, "gene_set_phenotype": 2, "member_of": 3}
    assert ds["claims"]["mintable"] == 20
    files = {f["role"]: f for f in ds["files"]}
    assert set(files) == {"gene_stats", "gene_set_stats", "membership"}
    gene = files["gene_stats"]
    data = (FIX / "pigean" / "combined_gene_stats.json").read_bytes()
    assert gene["filename"] == "combined_gene_stats.json" and gene["sizeBytes"] == len(data)
    assert len(gene["sha256"]) == 64 and gene["fileId"].startswith("dapper:File.") and gene["location"]
    templates = {t["name"]: t for t in d["templates"]}
    assert {c["key"]: c["mintable"] for c in templates["gene"]["claims"]} == {
        "measurement": 3, "tier-combined": 3, "tier-direct": 3, "tier-indirect": 3}
    assert [c["mintable"] for c in templates["gene_set"]["claims"]] == [2, 2, 2]
    assert templates["roster"]["groupBy"] == "to" and templates["roster"]["claims"][0]["mintable"] == 2
    measurement = templates["gene"]["claims"][0]
    assert "{" in measurement["statement"] and measurement["proposition"]["relation"]
    [q] = d["queries"]
    assert q["name"] == QUERY and q["path"] == f"queries/{QUERY}.yaml" and len(q["sha256"]) == 64
    assert q["example"]["variables"]["trait"] == "IBD"
    assert [(f["kind"], f["name"]) for f in d["sourceFiles"]] == [
        ("manifest", "pigean"), ("template", "gene"), ("template", "gene_set"), ("template", "roster"),
        ("query", QUERY)]
    assert all(len(f["sha256"]) == 64 and f["sizeBytes"] > 0 for f in d["sourceFiles"])
    assert d["dapperDocumentUrl"].endswith("/v1/collections/pigean/releases/0.0.1-test/document")
    assert d["manifest"]["collection"] == "pigean"
    assert _get("collections/pigean", release="0.0.1-test").json()["release"]["id"] == published.release_id
    assert _get("collections/nope").status_code == 404
    assert _get("collections/pigean", release="9.9.9").status_code == 404


def test_example_query_asserts_nothing(monkeypatch, tmp_path, published):
    # The fixture query's example asks for IBD, which the fixture data does not
    # have: publish a copy whose example names the fixture's trait and library,
    # into a database of its own, so the example run returns rows.
    collection, release = _fixture_tree(tmp_path)
    _edit(collection / "queries" / f"{QUERY}.yaml",
          lambda d: d["example"].update(trait="FIXPHENO", library="fixture-v1"))
    monkeypatch.setattr(service_db, "DB_PATH", str(tmp_path / "example.db"))
    con = service_db.connect()
    try:
        spec, base = load_release(release)
        pub.publish(con, load_collection(collection), spec, base)
    finally:
        con.close()
    _mint_one()  # one claim minted, so a served example would move times_seen as well as the count
    before = _claim_counts()
    assert before["n"] >= 1
    result = _get(f"collections/pigean/queries/{QUERY}/example")
    assert result.status_code == 200
    body = result.json()
    assert body["query"] == QUERY and body["variables"]["trait"] == "FIXPHENO"
    assert body["rows"], body
    assert all("claims" not in r for r in body["rows"])
    assert _claim_counts() == before
    assert _get("collections/pigean/queries/nope/example").status_code == 404
    assert _get(f"collections/nope/queries/{QUERY}/example").status_code == 404


def _mint_one():
    """Serve (and so mint) FIXG1's claims through the public query surface."""
    body = client.post(f"/v1/collections/pigean/queries/{QUERY}:run", json={"variables": {
        "trait": "FIXPHENO", "library": "fixture-v1", "priorMax": 10.0, "topGenes": 1, "setsPerGene": 0},
        "claims": ["measurement"]}).json()
    return body["rows"][0]["claims"]["measurement"]["id"]


def test_claims_list_filters(published):
    uncollected = _get("overview").json()["claims"]["uncollected"]
    everything = _get("claims", page_size=100).json()
    assert everything["total"] == 20 + uncollected
    collected = _get("claims", collection="pigean", page_size=100).json()
    assert collected["total"] == 20 == _get("claims", origin="collection").json()["total"]
    assert all(r["collection"] == "pigean" and r["datasetId"] for r in collected["rows"])
    assert _get("claims", origin="uncollected").json()["total"] == uncollected
    assert _get("claims", collection="nope").json()["total"] == 0
    measurement = _get("claims", claim_key="measurement", sort="subject").json()
    # Both row templates name a claim "measurement": 3 genes + 2 gene sets.
    assert measurement["total"] == 5 and [r["subject"] for r in measurement["rows"]] == sorted(
        r["subject"] for r in measurement["rows"])
    assert {r["kind"] for r in measurement["rows"]} == {"gene_phenotype", "gene_set_phenotype"}
    assert all(r["dimensions"]["library"]["value"] == "fixture-v1" for r in measurement["rows"])
    rosters = _get("claims", claim_key="roster").json()["rows"]
    assert len(rosters) == 2 and all(r["kind"] == "member_of" and r["subject"] for r in rosters)
    fixg1 = _get("claims", q="fixg1").json()
    assert fixg1["total"] == 4 and {r["subject"] for r in fixg1["rows"]} == {"FIXG1"}
    by_id = _get("claims", q=rosters[0]["claimId"][-12:].lower()).json()
    assert rosters[0]["claimId"] in {r["claimId"] for r in by_id["rows"]}
    by_label = _get("claims", q=rosters[0]["subject"]).json()
    assert rosters[0]["claimId"] in {r["claimId"] for r in by_label["rows"]}
    dataset = collected["rows"][0]["datasetId"]
    assert _get("claims", dataset=dataset).json()["total"] == 20

    minted_id = _mint_one()
    assert _get("claims", status="minted", collection="pigean").json()["rows"][0]["claimId"] == minted_id
    assert _get("claims", status="unminted").json()["total"] == 19
    first = _get("claims", sort="recent", page_size=1).json()["rows"][0]
    assert first["minted"] and first["timesAsserted"] >= 1 and first["lastAssertedAt"].endswith("Z")
    assert _get("claims", page=2, page_size=15, origin="collection").json()["rows"].__len__() == 5


def test_claim_detail_never_mints_or_asserts(published):
    unminted = _get("claims", status="unminted", claim_key="tier-combined", page_size=1).json()["rows"][0]
    before = _claim_counts()
    d = _get(f"claims/{unminted['claimId']}").json()
    assert d["minted"] is False and d["timesAsserted"] == 0 and d["identityProfile"] == "DAPPER-ID-1"
    assert d["verification"] == {"collectionBacked": True, "renders": True, "storedMatches": None, "error": None}
    assert d["claim"]["id"] == unminted["claimId"] and d["statement"] == d["claim"]["statement"]
    assert d["collection"] == "pigean" and d["claimKey"] == "tier-combined" and d["method"] == "pigean"
    assert d["proposition"]["id"] == d["claim"]["proposition"] and d["triple"]["subject"].startswith("hgnc.symbol:")
    src = d["source"]
    assert src["template"]["name"] == "gene" and src["releases"] == ["0.0.1-test"] and src["group"] is None
    assert src["row"]["kind"] == "gene_phenotype" and src["row"]["subject"]["value"] == unminted["subject"]
    assert "combined" in src["row"]["columns"] and src["dataset"]["dimensions"]["trait"]["value"] == "FIXPHENO"
    assert _claim_counts() == before

    minted_id = _mint_one()
    before = _claim_counts()
    m = _get(f"claims/{minted_id}").json()
    assert m["minted"] and m["verification"]["renders"] and m["verification"]["storedMatches"] is True
    assert m["scores"] and all("missing" not in s for s in m["scores"]) and m["evidence"]
    assert _claim_counts() == before

    roster = _get("claims", claim_key="roster", page_size=1).json()["rows"][0]
    g = _get(f"claims/{roster['claimId']}").json()
    assert g["verification"]["renders"] and g["source"]["row"] is None
    assert g["source"]["group"]["edgeKind"] == "member_of" and g["source"]["group"]["label"] == roster["subject"]


def test_claim_detail_reports_drift_instead_of_failing(published):
    unminted = _get("claims", status="unminted", claim_key="tier-direct", page_size=1).json()["rows"][0]
    row = _sql("SELECT rc.row_id, rr.row_json FROM result_claims rc JOIN result_rows rr ON rr.row_id = rc.row_id "
               "WHERE rc.claim_id = ?", (unminted["claimId"],))[0]
    con = service_db.connect()
    try:
        with con:
            con.execute("UPDATE result_rows SET row_json = ? WHERE row_id = ?",
                        (json.dumps({**json.loads(row["row_json"]), "trait_group": "tampered"}), row["row_id"]))
        d = _get(f"claims/{unminted['claimId']}")
        assert d.status_code == 200
        v = d.json()["verification"]
        assert v["collectionBacked"] and v["renders"] is False and "template drift" in v["error"]
    finally:
        with con:
            con.execute("UPDATE result_rows SET row_json = ? WHERE row_id = ?", (row["row_json"], row["row_id"]))
        con.close()


def test_uncollected_claims(published):
    # The published deposit example: real DAPPER objects, deposited exactly as the file gives them.
    bundle = json.loads((ROOT / "api" / "examples" / "deposit-bundle.request.json").read_text())
    assert client.post("/v1/deposits", json=bundle).status_code == 200
    claim_id = bundle["index_hints"][0]["claim_id"]
    listed = _get("claims", origin="uncollected", page_size=100).json()
    assert claim_id in {r["claimId"] for r in listed["rows"]}
    assert all(r["collection"] is None and r["dimensions"] is None for r in listed["rows"])
    assert _get("claims", status="unminted", q=claim_id[-14:].lower()).json()["total"] == 0
    d = _get(f"claims/{claim_id}").json()
    assert d["verification"]["collectionBacked"] is False and d["verification"]["renders"] is None
    assert d["source"] is None and d["collection"] is None and d["claimKey"] is None
