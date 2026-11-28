"""`claims objects`: register an object collection and deposit DAPPER documents into its releases with a
`claims login`, against a real server thread (docs/depositing-objects.md)."""
from __future__ import annotations

import csv
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest
import uvicorn
import yaml

from claims_cli import main as cli
from claims_cli import objects as objects_cli
from service import db as service_db
from service import device_login
from service.app import app
from service.collections import identity as cidn

PUBLISHER = "cli-depositor@example.org"
MANIFEST = """collection: cli-genesets
kind: objects
title: Gene sets deposited from the CLI
methods: [tests]
dapper_profile: 0.3.0-a1
object_kinds:
  GeneSet:
    fields:
      name: {slot: name}
"""


@pytest.fixture(autouse=True)
def _quick_retries(monkeypatch):
    monkeypatch.setattr(objects_cli, "RETRIES", 2)
    monkeypatch.setattr(objects_cli.time, "sleep", lambda _s: None)


@pytest.fixture(scope="module")
def server_db():
    saved = service_db.DB_PATH
    service_db.DB_PATH = str(Path(tempfile.mkdtemp()) / "objects-cli.db")
    yield service_db.DB_PATH
    service_db.DB_PATH = saved


@pytest.fixture(scope="module")
def api(server_db):
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
def token(server_db):
    con = service_db.connect()
    try:
        started = device_login.start(con, "claims-cli/1", "test-runner", "34.1.2.3")
        con.commit()
        with con:
            assert device_login.decide(con, device_login.normalize_user_code(started["user_code"]), PUBLISHER, True)
        return device_login.exchange(con, started["device_code"])
    finally:
        con.close()


@pytest.fixture(autouse=True)
def signed_in(token, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAIMS_TOKEN", token["access_token"])
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def _rows(sql, params=()):
    con = service_db.connect()
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def claims(*argv):
    cli.main([str(a) for a in argv])


def _collection(tmp_path: Path, key="cli-genesets") -> Path:
    root = tmp_path / key
    root.mkdir()
    (root / "collection.yaml").write_text(MANIFEST.replace("cli-genesets", key))
    return root


GSC = cidn.mint("GeneSetCollection", {"name": "CLI sets", "collection_key": "cli", "version": "1"})


def _gene_set(name):
    return cidn.mint("GeneSet", {"name": name, "members": ["NOD2"], "member_type": "gene", "n_members": 1,
                                 "in_gene_set_collection": [GSC["id"]]})


def _documents(root: Path, n: int, c2m2: bool = True) -> list[str]:
    """n producer documents as a gene-set release ships them: a GeneSetCollection and its GeneSets, plus a
    `prefixes` map, an `id`, and C2M2 file records the repository's profile does not take."""
    rels = []
    for i in range(n):
        doc = {"id": f"urn:doc:{i}", "prefixes": {"dapper": "https://w3id.org/dapper/"},
               "gene_set_collections": [GSC], "gene_sets": [_gene_set(f"SET_{i}_{j}") for j in range(3)]}
        if c2m2:
            doc["c2m2_files"] = [{"id": f"c2m2:{i}:{k}"} for k in range(2)]
        rel = f"genesets/lib{i % 2}/set{i:03d}/geneset.provenance.dapper.yaml"
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(yaml.safe_dump(doc))
        rels.append(rel)
    return sorted(rels)


def _deposits(key):
    return _rows("SELECT COUNT(*) AS n FROM deposits WHERE collection_key = ?", (key,))[0]["n"]


def test_register_deposit_resume_and_close(api, tmp_path, capsys):
    col = _collection(tmp_path)
    source = tmp_path / "release"
    rels = _documents(source, 5)
    claims("objects", "register", col, "--api", api)
    assert "registered   cli-genesets at" in capsys.readouterr().out
    claims("objects", "register", col, "--api", api)
    assert "unchanged    cli-genesets" in capsys.readouterr().out

    # A trial: the first two documents. c2m2_files is not a DAPPER group, so it is left out.
    claims("objects", "deposit", col, "2026.11.0", "--api", api, "--from", source, "--skip-group", "c2m2_files",
           "--limit", 2, "--workers", 2)
    out = capsys.readouterr().out
    assert "opened       cli-genesets 2026.11.0" in out
    assert "deposited    2 document(s) in 2 part(s)" in out and "8 objects, 0 edges" in out
    assert "skipped c2m2_files: 4" in out and "stays open" in out
    state = tmp_path / "cache" / "claims" / "objects" / "cli-genesets-2026.11.0.sent"
    lines = state.read_text().splitlines()
    assert lines[0] == f"# claims objects deposit {api} cli-genesets 2026.11.0 from={source} skip=c2m2_files"
    assert sorted(lines[1:]) == rels[:2]
    assert _deposits("cli-genesets") == 2

    # Resumed: the same trial again sends nothing new.
    claims("objects", "deposit", col, "2026.11.0", "--api", api, "--from", source, "--skip-group", "c2m2_files",
           "--limit", 2)
    out = capsys.readouterr().out
    assert "2 already deposited" in out and "deposited    0 document(s)" in out and "open         cli-genesets" in out
    assert _deposits("cli-genesets") == 2

    # The rest, then closed.
    claims("objects", "deposit", col, "2026.11.0", "--api", api, "--from", source, "--skip-group", "c2m2_files",
           "--close")
    out = capsys.readouterr().out
    assert "deposited    3 document(s)" in out
    assert "closed       cli-genesets 2026.11.0 as dapper:ResultCollection." in out
    assert "GeneSet: 15" in out and "GeneSetCollection: 1" in out and "unresolved_references: 0" in out
    assert _deposits("cli-genesets") == 5
    rows = _rows("SELECT key_id, publisher_email FROM deposits WHERE collection_key = 'cli-genesets'")
    assert {(r["key_id"], r["publisher_email"]) for r in rows} == {(None, PUBLISHER)}
    assert _rows("SELECT published_by FROM collection_releases WHERE collection_key = 'cli-genesets'") == [
        {"published_by": PUBLISHER}]

    claims("objects", "status", col, "2026.11.0", "--api", api)
    out = capsys.readouterr().out
    assert "closed" in out and "objects: 16 in 5 deposit(s)" in out
    claims("objects", "close", col, "2026.11.0", "--api", api)  # closing again changes nothing
    assert "not reported again" in capsys.readouterr().out


def test_a_revision_carries_the_last_release_and_deposits_only_what_is_new(api, tmp_path, capsys):
    """`--carry-from` opens the revision with the closed release's members; only the new document is sent."""
    col = _collection(tmp_path)
    source, extra = tmp_path / "release", tmp_path / "revision"
    _documents(source, 3, c2m2=False)
    claims("objects", "register", col, "--api", api)
    claims("objects", "deposit", col, "r1", "--api", api, "--from", source, "--close")
    before = _deposits("cli-genesets")
    (extra / "sources").mkdir(parents=True)
    (extra / "sources" / "sources.dapper.yaml").write_text(yaml.safe_dump(
        {"gene_sets": [_gene_set("SET_new")], "gene_set_collections": [GSC]}))
    capsys.readouterr()
    claims("objects", "deposit", col, "r2", "--api", api, "--from", extra, "--carry-from", "r1", "--close")
    out = capsys.readouterr().out
    assert "opened       cli-genesets r2" in out and "carrying 10 members of r1" in out
    assert "deposited    1 document(s) in 1 part(s)" in out
    assert "GeneSet: 10" in out and "GeneSetCollection: 1" in out
    assert _deposits("cli-genesets") == before + 1


def test_a_group_the_service_cannot_verify_stops_the_run(api, tmp_path, capsys):
    col = _collection(tmp_path, "cli-no-skip")
    source = tmp_path / "release"
    _documents(source, 1)
    with pytest.raises(SystemExit) as stopped:
        claims("objects", "deposit", col, "1", "--api", api, "--from", source)
    assert "was refused (400: 'c2m2:0:0' is not a DAPPER-ID-1 identifier" in str(stopped.value)
    state = tmp_path / "cache" / "claims" / "objects" / "cli-no-skip-1.sent"
    assert state.read_text().splitlines()[1:] == []


def test_a_changed_definition_stops_deposit_until_replaced(api, tmp_path, capsys):
    col = _collection(tmp_path, "cli-changed")
    claims("objects", "register", col, "--api", api)
    (col / "collection.yaml").write_text((col / "collection.yaml").read_text().replace("title: Gene sets",
                                                                                        "title: Other sets"))
    source = tmp_path / "release"
    _documents(source, 1, c2m2=False)
    with pytest.raises(SystemExit) as stopped:
        claims("objects", "deposit", col, "1", "--api", api, "--from", source)
    assert "claims objects register --replace" in str(stopped.value)
    assert _deposits("cli-changed") == 0
    claims("objects", "register", col, "--api", api, "--replace")
    assert "replaced     cli-changed" in capsys.readouterr().out
    claims("objects", "deposit", col, "1", "--api", api, "--from", source)
    assert _deposits("cli-changed") == 1


def test_a_manifest_and_role_select_the_documents(api, tmp_path, capsys):
    col = _collection(tmp_path, "cli-manifest")
    source = tmp_path / "release"
    rels = _documents(source, 4, c2m2=False)
    (source / "notes.dapper.yaml").write_text("not: a document the manifest names\n")
    manifest = source / "run" / "release.cfde_manifest.tsv"
    manifest.parent.mkdir()
    with open(manifest, "w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["relative_path", "role", "library"])
        for i, rel in enumerate(rels):
            w.writerow([rel, "provenance" if i != 1 else "gmt", "lib"])
        w.writerow(["genesets/lib0/genesets.gmt", "gmt", "lib"])
    claims("objects", "deposit", col, "1", "--api", api, "--from", source, "--manifest", "run/release.cfde_manifest.tsv",
           "--role", "provenance", "--close")
    out = capsys.readouterr().out
    assert "selected     3 document(s)" in out and "deposited    3 document(s)" in out
    sent = _rows("SELECT DISTINCT o.object_id FROM deposit_objects o JOIN deposits d ON d.deposit_id = o.deposit_id "
                 "WHERE d.collection_key = 'cli-manifest'")
    skipped_doc = yaml.safe_load((source / rels[1]).read_text())
    assert not {g["id"] for g in skipped_doc["gene_sets"]} & {r["object_id"] for r in sent}


def test_a_big_document_is_sent_in_parts_within_the_limits(api, tmp_path, capsys):
    col = _collection(tmp_path, "cli-big")
    sets = [_gene_set(f"BIG_{i}") for i in range(1700)]
    edges = [{"subject": sets[i % 1700]["id"], "predicate": "prov:wasDerivedFrom",
              "object": sets[(i * 7 + 1) % 1700]["id"]} for i in range(5500)]
    # Edges first: the part holding only edges still carries an object.
    doc = {"was_derived_from_edges": edges, "gene_set_collections": [GSC], "gene_sets": sets}
    split = list(objects_cli.parts(doc, set()))
    assert all(sum(len(v) for g, v in p.items() if not g.endswith("_edges")) <= 1000 for p, _, _ in split)
    assert all(sum(len(v) for g, v in p.items() if g.endswith("_edges")) <= 5000 for p, _, _ in split)
    assert all(any(not g.endswith("_edges") for g in p) for p, _, _ in split)
    assert sum(n for _, n, _ in split) == 1701 and sum(e for _, _, e in split) == 5500
    # 4,000 edges with the anchor; 1,500 edges and 800 objects; 800 objects; 101 objects.
    assert [(n, e) for _, n, e in split] == [(0, 4000), (800, 1500), (800, 0), (101, 0)]
    assert list(split[0][0]) == ["gene_set_collections", "was_derived_from_edges"]

    source = tmp_path / "release"
    (source / "big").mkdir(parents=True)
    (source / "big" / "big.dapper.yaml").write_text(yaml.safe_dump(doc))
    claims("objects", "deposit", col, "1", "--api", api, "--from", source, "--close")
    out = capsys.readouterr().out
    assert "deposited    1 document(s) in 4 part(s)" in out and "1,701 objects, 5,500 edges" in out
    assert "GeneSet: 1,700" in out
    assert _deposits("cli-big") == 4
    assert _rows("SELECT COUNT(*) AS n FROM deposit_edges e JOIN deposits d ON d.deposit_id = e.deposit_id "
                 "WHERE d.collection_key = 'cli-big'")[0]["n"] == 5500


def test_a_size_limit_splits_parts_too(monkeypatch):
    monkeypatch.setattr(objects_cli, "MAX_BYTES", 2000)
    doc = {"gene_sets": [_gene_set(f"S{i}") for i in range(10)]}
    split = list(objects_cli.parts(doc, set()))
    assert len(split) > 1 and sum(n for _, n, _ in split) == 10
    with pytest.raises(ValueError, match="no objects"):
        list(objects_cli.parts({"was_derived_from_edges": [{"subject": "a", "object": "b"}]}, set()))


def test_a_state_file_of_another_deposit_is_refused(api, tmp_path):
    col = _collection(tmp_path, "cli-state")
    source = tmp_path / "release"
    _documents(source, 1, c2m2=False)
    state = tmp_path / "trial.sent"
    state.write_text("# claims objects deposit http://127.0.0.1:1 cli-state 1\n" + "x\n")
    with pytest.raises(SystemExit) as stopped:
        claims("objects", "deposit", col, "1", "--api", api, "--from", source, "--state-file", state)
    assert "is the state of another deposit" in str(stopped.value)


def test_a_state_file_of_other_skipped_groups_is_refused(api, tmp_path, capsys):
    """Documents sent without a group are not done for a run that keeps it (or the reverse)."""
    col = _collection(tmp_path, "cli-skip")
    source = tmp_path / "release"
    _documents(source, 1, c2m2=False)
    state = tmp_path / "skip.sent"
    claims("objects", "deposit", col, "1", "--api", api, "--from", source, "--state-file", state,
           "--skip-group", "c2m2_files")
    with pytest.raises(SystemExit) as stopped:
        claims("objects", "deposit", col, "1", "--api", api, "--from", source, "--state-file", state)
    assert "is the state of another deposit" in str(stopped.value)


def test_an_s3_source_is_mirrored_under_the_workdir(api, tmp_path, monkeypatch, capsys):
    col = _collection(tmp_path, "cli-s3")
    bucket = tmp_path / "bucket"
    _documents(bucket / "collections" / "x" / "2026.11", 2, c2m2=False)
    calls = []

    def sync(prefix, dest, includes):
        calls.append((prefix, dest, includes))
        assert prefix == "s3://kpn-test/collections/x/2026.11/"
        shutil.copytree(bucket / "collections" / "x" / "2026.11", dest, dirs_exist_ok=True)

    monkeypatch.setattr(objects_cli, "s3_sync", sync)
    work = tmp_path / "work"
    claims("objects", "deposit", col, "1", "--api", api, "--from", "s3://kpn-test/collections/x/2026.11",
           "--workdir", work)
    assert calls == [("s3://kpn-test/collections/x/2026.11/", work / "source" / "kpn-test" / "collections/x/2026.11",
                      ["*.dapper.yaml"])]
    assert "deposited    2 document(s)" in capsys.readouterr().out
    assert (work / "cli-s3-1.sent").exists()


def test_deposits_may_be_sent_with_a_key(api, tmp_path, monkeypatch, depositor, capsys):
    from service import api_keys

    col = _collection(tmp_path, "cli-keyed")
    claims("objects", "register", col, "--api", api)
    con = service_db.connect()
    try:
        with con:
            api_keys.grant(con, depositor["key_id"], "cli-keyed", "tests")
    finally:
        con.close()
    source = tmp_path / "release"
    _documents(source, 1, c2m2=False)
    monkeypatch.setenv("CLAIMS_API_KEY", depositor["key"])
    claims("objects", "deposit", col, "1", "--api", api, "--from", source, "--key-env", "CLAIMS_API_KEY", "--close")
    out = capsys.readouterr().out
    assert "depositing with a key" in out and "closed       cli-keyed 1" in out
    assert {r["key_id"] for r in _rows("SELECT key_id FROM deposits WHERE collection_key = 'cli-keyed'")} == {
        depositor["key_id"]}


def test_the_cfde_shape_an_s3_source_with_a_manifest_under_it(api, tmp_path, monkeypatch, capsys):
    """--from s3://... --manifest run/<x>.tsv --role provenance: the manifest is fetched from under the prefix, and
    only the documents it selects are mirrored and sent."""
    col = _collection(tmp_path, "cli-s3-manifest")
    bucket = tmp_path / "bucket" / "collections" / "x" / "2026.11"
    rels = _documents(bucket, 3)
    (bucket / "run").mkdir()
    with open(bucket / "run" / "x.cfde_manifest.tsv", "w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["relative_path", "role", "library"])
        w.writerow([rels[0], "provenance", "lib0"])
        w.writerow([rels[2], "provenance", "lib0"])
        w.writerow(["library_gmts/lib0.gmt", "gmt", "lib0"])
    fetched, synced = [], []

    def fetch(location, dest):
        fetched.append(location)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(bucket / location.split("2026.11/", 1)[1], dest)

    def sync(prefix, dest, includes):
        synced.append(includes)
        shutil.copytree(bucket, dest, dirs_exist_ok=True)

    monkeypatch.setattr(objects_cli, "s3_fetch", fetch)
    monkeypatch.setattr(objects_cli, "s3_sync", sync)
    claims("objects", "deposit", col, "2026.11.0", "--api", api, "--from", "s3://kpn-test/collections/x/2026.11/",
           "--manifest", "run/x.cfde_manifest.tsv", "--role", "provenance", "--skip-group", "c2m2_files",
           "--workdir", tmp_path / "work", "--close")
    out = capsys.readouterr().out
    assert fetched == ["s3://kpn-test/collections/x/2026.11/run/x.cfde_manifest.tsv"]
    assert synced == [["*geneset.provenance.dapper.yaml"]]
    assert "selected     2 document(s)" in out and "deposited    2 document(s)" in out
    assert "skipped c2m2_files: 4" in out and "GeneSet: 6" in out


def test_part_mb_makes_smaller_parts(api, tmp_path, capsys):
    """--part-mb caps each deposit's JSON, so a document of big objects is sent in more, smaller parts."""
    col = _collection(tmp_path, "cli-partmb")
    source = tmp_path / "release"
    _documents(source, 1, c2m2=False)
    rel = sorted(p.relative_to(source).as_posix() for p in source.rglob("*.dapper.yaml"))[0]
    document = yaml.safe_load((source / rel).read_text())
    whole = list(objects_cli.parts(document, set()))
    small = list(objects_cli.parts(document, set(), max_bytes=200))
    assert len(small) > len(whole)
    claims("objects", "deposit", col, "1", "--api", api, "--from", source, "--part-mb", "0.05",
           "--state-file", tmp_path / "p.sent")
    assert "deposited    1 document(s)" in capsys.readouterr().out
