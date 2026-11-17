"""Collections (docs/collections.md): manifests and templates checked at load,
DAPPER-ID-1 minting through dapper's own module, publishing a release, claims
minted on serve and checked against their recorded ids, discovery, and the
query executor reproducing the PIGEAN validation challenge."""
from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from service import db as service_db
from service.app import app
from service.collections import identity as idn
from service.collections.manifest import ManifestError, bind_params, load_collection, load_release
from service.collections.publish import PublishError, publish

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
COLLECTION = FIX / "collections" / "pigean"
RELEASE = FIX / "collections" / "pigean-fixture-release.yaml"
TOOLKIT_COLLECTION = ROOT.parent / "kpn-dapper-toolkit" / "collections" / "pigean"
QUERY = "top-genes-with-sets"
client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def isolated_db():
    """This module's own database: the app connects through service.db's
    default path, so point it here for the module and restore it after."""
    saved = service_db.DB_PATH
    service_db.DB_PATH = str(Path(tempfile.mkdtemp()) / "collections.db")
    yield service_db.DB_PATH
    service_db.DB_PATH = saved


def _publish(collection=COLLECTION, release=RELEASE, version=None):
    col = load_collection(collection)
    spec, base = load_release(release)
    if version:
        spec = {**spec, "version": version}
    con = service_db.connect()
    try:
        return publish(con, col, spec, base)
    finally:
        con.close()


@pytest.fixture(scope="module")
def published(isolated_db):
    return _publish()


def _run(variables, **body):
    return client.post(f"/v1/collections/pigean/queries/{QUERY}:run", json={"variables": variables, **body})


def _fetch(object_id):
    con = service_db.connect()
    try:
        row = con.execute("SELECT * FROM dapper_objects WHERE id = ?", (object_id,)).fetchone()
        return (dict(row), json.loads(row["payload"])) if row else (None, None)
    finally:
        con.close()


# --- identity ---------------------------------------------------------------

def test_vendored_identity_reproduces_dappers_frozen_vectors():
    vectors = json.loads((idn.VENDOR / "schema/identity/test_vectors.json").read_text())["vectors"]
    assert vectors
    for v in vectors:
        assert idn.mint(v["class"], v["instance"])["id"] == v["expected_id"], v["name"]
    assert idn.VENDOR_INFO["repository"] == "broadinstitute/dapper"
    assert idn.DAPPER_VERSION == "0.2.1-a1"


def test_mint_refuses_fields_the_class_does_not_have():
    with pytest.raises(idn.MintError, match="no slot"):
        idn.mint("ClaimScore", {"score_kind": "SCORE", "metric": "m", "value": 1.0, "interpretation": "i",
                                "vaule": 2.0})


# --- manifests --------------------------------------------------------------

def test_fixture_collection_matches_the_toolkit_copy():
    if not TOOLKIT_COLLECTION.is_dir():
        pytest.skip("kpn-dapper-toolkit checkout not found beside this repository")
    for path in sorted(COLLECTION.rglob("*.yaml")):
        rel = path.relative_to(COLLECTION)
        assert (TOOLKIT_COLLECTION / rel).read_bytes() == path.read_bytes(), f"{rel} drifted from the toolkit"


def _copy_collection(tmp_path):
    dest = tmp_path / "pigean"
    shutil.copytree(COLLECTION, dest)
    return dest


def _edit(path, change):
    doc = yaml.safe_load(path.read_text())
    change(doc)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def test_query_ordering_by_another_kinds_metric_is_refused(tmp_path):
    root = _copy_collection(tmp_path)
    _edit(root / "queries" / f"{QUERY}.yaml",
          lambda d: d["plan"][1]["join"]["order"].__setitem__("metric", "combined"))
    with pytest.raises(ManifestError, match="no blending"):
        load_collection(root)


def test_template_metric_and_rule_mistakes_are_caught_at_load(tmp_path):
    root = _copy_collection(tmp_path)

    def break_it(d):
        d["claims"][1]["classify"] = "no-such-rule"
        d["claims"][0]["scores"].append("beta")
    _edit(root / "templates" / "gene.yaml", break_it)
    with pytest.raises(ManifestError) as exc:
        load_collection(root)
    assert "unknown tier rule" in str(exc.value) and "'beta' is not a metric" in str(exc.value)


def test_schema_violations_name_the_field(tmp_path):
    root = _copy_collection(tmp_path)
    _edit(root / "collection.yaml", lambda d: d["metrics"]["combined"].__setitem__("score_kind", "VIBES"))
    with pytest.raises(ManifestError, match="metrics/combined/score_kind"):
        load_collection(root)


def test_query_variables_are_checked():
    q = load_collection(COLLECTION).queries[QUERY].doc
    assert bind_params(q, {"trait": "X"})["topGenes"] == 20
    with pytest.raises(ManifestError, match="required"):
        bind_params(q, {})
    with pytest.raises(ManifestError, match="at most 200"):
        bind_params(q, {"trait": "X", "topGenes": 201})
    with pytest.raises(ManifestError, match="unknown variable"):
        bind_params(q, {"trait": "X", "topgenes": 3})


# --- publishing -------------------------------------------------------------

def test_publish_records_a_dapper_release(published):
    assert idn.class_of(published.release_id) == "ResultCollection"
    assert published.stats["rows"] == 5 and published.stats["edges"] == 3
    assert published.stats["claims"] == 20  # 3 genes x 4 + 2 sets x 3 + 2 rosters
    doc = published.document
    release = doc["result_collections"][0]
    assert release["collection_key"] == "pigean" and release["dataset_dimensions"] == ["trait", "library"]
    assert idn.verify(release)
    dataset = doc["datasets"][0]
    assert dataset["dimension_values"] == [{"dimension": "trait", "dimension_value": "FIXPHENO"},
                                           {"dimension": "library", "dimension_value": "fixture-v1"}]
    assert "trait" not in dataset  # unregistered key: not a CURIE, so the trait slot stays unset
    assert {r["role"] for r in dataset["file_roles"]} == {"gene_stats", "gene_set_stats", "membership"}


def test_every_stored_collection_object_is_a_true_dapper_address(published):
    con = service_db.connect()
    try:
        rows = con.execute("SELECT id, payload FROM dapper_objects WHERE identity_profile = ?",
                           (idn.PROFILE,)).fetchall()
    finally:
        con.close()
    assert len(rows) >= published.stats["objects"]
    bad = [r["id"] for r in rows if not idn.verify(json.loads(r["payload"]))]
    assert bad == []


def test_republishing_a_version_is_a_no_op(published):
    again = _publish()
    assert again.release_id == published.release_id
    assert again.stats["datasets_ingested"] == 0 and again.stats["mint_contexts_rendered"] == 0


def test_a_new_version_chains_and_keeps_claim_ids(published):
    con = service_db.connect()
    before = {r["claim_id"] for r in con.execute("SELECT claim_id FROM result_claims")}
    con.close()
    second = _publish(version="0.0.2-test")
    assert second.release_id != published.release_id
    assert second.document["result_collections"][0]["was_revision_of"] == published.release_id
    assert second.stats["claims"] == 0  # same templates, same files: nothing to render
    con = service_db.connect()
    after = {r["claim_id"] for r in con.execute("SELECT claim_id FROM result_claims")}
    con.close()
    assert after == before


def test_a_file_in_the_wrong_dataset_is_refused(tmp_path, published):
    spec, base = load_release(RELEASE)
    spec = copy.deepcopy(spec)
    spec["version"] = "0.0.9-wrong"
    spec["datasets"][0]["dimensions"]["library"] = "some-other-library"
    con = service_db.connect()
    try:
        with pytest.raises(PublishError, match="right dataset"):
            publish(con, load_collection(COLLECTION), spec, base)
    finally:
        con.close()


# --- serving and the executor ----------------------------------------------

def _old_loader():
    spec = importlib.util.spec_from_file_location("load_pigean", ROOT / "scripts" / "load_pigean.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_challenge_query_matches_the_pigean_resolver(published):
    con = service_db.connect()
    try:
        _old_loader().load(con, FIX / "pigean" / "combined_gene_stats.json",
                           FIX / "pigean" / "combined_gene_set_stats.json", FIX / "pigean" / "gene_gene_set_stats.json")
    finally:
        con.close()
    query = (ROOT / "api" / "graphql" / "queries" / "ibd-validation-challenge.graphql").read_text()
    for prior_max, top, per in [(10.0, 20, 5), (1.0, 2, 1), (0.1, 3, 3)]:
        variables = json.loads((ROOT / "api/graphql/queries/ibd-validation-challenge.variables.json").read_text())
        variables.update(phenotype="FIXPHENO", priorMax=prior_max, topGenes=top, setsPerGene=per)
        old = client.post("/v1/graphql", json={
            "query": query.replace("phenotype: $phenotype", 'phenotype: $phenotype, collection: "fixture-v1"'),
            "variables": variables}).json()["data"]["pigeanValidationChallenge"]
        new = _run({"trait": "FIXPHENO", "library": "fixture-v1", "priorMax": prior_max, "topGenes": top,
                    "setsPerGene": per}, claims=[]).json()
        old_rows = [(r["geneAssociation"]["gene"]["symbol"], r["geneAssociation"]["combined"],
                     [s["geneSetAssociation"]["geneSet"]["name"] for s in r["sets"]]) for r in old["rows"]]
        new_rows = [(r["subject"]["label"], r["metrics"]["combined"],
                     [s["row"]["subject"]["label"] for s in r["sets"]]) for r in new["rows"]]
        assert new_rows == old_rows, (prior_max, top, per)


def test_serving_mints_claims_once_and_counts_reassertions(published):
    variables = {"trait": "FIXPHENO", "library": "fixture-v1", "priorMax": 10.0, "topGenes": 1, "setsPerGene": 1}
    first = _run(variables).json()
    row = first["rows"][0]
    assert sorted(row["claims"]) == ["measurement", "tier-combined", "tier-direct", "tier-indirect"]
    measurement = row["claims"]["measurement"]
    stored, doc = _fetch(measurement["id"])
    assert stored["identity_profile"] == idn.PROFILE and idn.verify(doc)
    assert doc == measurement["claim"]
    assert measurement["triple"]["subject"].startswith("HGNC.SYMBOL:")
    assert measurement["evidence"][0]["valueLocators"][0]["value_text"]
    tier = row["claims"]["tier-combined"]["claim"]
    assert tier["direction"] in ("SUPPORTS", "NEUTRAL")
    with_sets = next(r for r in _run({**variables, "topGenes": 3}).json()["rows"] if r["sets"])
    roster = with_sets["sets"][0]["edge"]["claims"]["roster"]
    assert roster["proposition"]["relation"] == "urn:kpn:relation:has_members"
    before, _ = _fetch(measurement["id"])
    _run(variables)
    again, _ = _fetch(measurement["id"])
    assert again["times_seen"] == before["times_seen"] + 1


def test_served_claims_are_refused_when_the_row_drifts(published):
    con = service_db.connect()
    try:
        r = con.execute("SELECT row_id, row_json FROM result_rows WHERE kind = 'gene_phenotype' "
                        "ORDER BY row_id LIMIT 1").fetchone()
        row = json.loads(r["row_json"])
        tampered = {**row, "trait_group": "tampered"}
        with con:
            con.execute("UPDATE result_rows SET row_json = ? WHERE row_id = ?", (idn.canonical(tampered), r["row_id"]))
        response = _run({"trait": "FIXPHENO", "library": "fixture-v1", "priorMax": 100.0, "topGenes": 20,
                         "setsPerGene": 0})
        assert response.status_code == 400 and "template drift" in response.json()["detail"]
    finally:
        with con:
            con.execute("UPDATE result_rows SET row_json = ? WHERE row_id = ?", (r["row_json"], r["row_id"]))
        con.close()


def test_unknown_traits_and_missing_edges_are_stated(published):
    body = _run({"trait": "NOT-A-TRAIT", "library": "fixture-v1"}).json()
    assert body["rows"] == []
    assert _run({"library": "fixture-v1"}).status_code == 400  # trait is required
    assert _run({"trait": "FIXPHENO", "topGenes": 999}).status_code == 400


def test_discovery_describes_the_collection(published):
    listing = client.get("/v1/collections").json()["collections"]
    assert [c["id"] for c in listing] == ["pigean"]
    d = client.get("/v1/collections/pigean").json()
    assert d["methods"] == ["PIGEAN"]
    assert d["datasets"][0]["counts"] == {"gene_phenotype": 3, "gene_set_phenotype": 2, "member_of": 3}
    assert d["metrics"]["combined"]["score_kind"] == "SCORE"
    assert {t["name"] for t in d["templates"]} == {"gene", "gene_set", "roster"}
    assert d["claims"]["mintable"] == 20
    queries = client.get("/v1/collections/pigean/queries").json()["queries"]
    assert queries[0]["name"] == QUERY and queries[0]["example"]["variables"]["trait"] == "IBD"
    document = client.get(f"/v1/collections/pigean/releases/{d['release']['version']}/document").json()
    assert document["result_collections"][0]["id"] == d["release"]["id"]
    assert client.get("/v1/collections/nope").status_code == 404


def test_graphql_run_query_matches_rest(published):
    variables = {"trait": "FIXPHENO", "library": "fixture-v1", "priorMax": 10.0, "topGenes": 2, "setsPerGene": 2}
    gql = client.post("/v1/graphql", json={
        "query": "query($v: JSON) { runQuery(collection: \"pigean\", query: \"%s\", variables: $v, claims: []) "
                 "collections { id version datasets } }" % QUERY,
        "variables": {"v": variables}}).json()["data"]
    rest = _run(variables, claims=[]).json()
    assert gql["runQuery"]["rows"] == rest["rows"]
    assert gql["collections"][0]["id"] == "pigean"


def test_objects_resolver_and_proposition_fan_in_see_collection_claims(published):
    body = _run({"trait": "FIXPHENO", "library": "fixture-v1", "priorMax": 10.0, "topGenes": 1,
                 "setsPerGene": 0}, claims=["measurement"]).json()
    env = body["rows"][0]["claims"]["measurement"]
    obj = client.get(f"/v1/objects/{env['id']}", headers={"accept": "application/json"}).json()
    assert obj["class_name"] == "Claim"
    prop = client.post("/v1/graphql", json={
        "query": "query($id: ID!) { proposition(id: $id) { claims { id } } }",
        "variables": {"id": env["claim"]["proposition"]}}).json()["data"]["proposition"]
    assert env["id"] in {c["id"] for c in prop["claims"]}


def test_the_publish_script_publishes_into_the_given_database(tmp_path):
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if k != "CLAIMS_MYSQL_HOST"}
    env["CLAIMS_DB"] = str(tmp_path / "script.db")
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "publish_collection.py"), str(COLLECTION),
                           "--release", str(RELEASE), "--db", str(tmp_path / "script.db")],
                          capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120)
    assert done.returncode == 0, done.stderr
    assert "published pigean 0.0.1-test as dapper:ResultCollection." in done.stdout


def test_a_collection_claims_address_is_reserved_on_deposit(published):
    """The address of a claim a published collection implies is reserved: a deposit there must be exactly the
    claim the collection renders. (Deposit validates and verifies first, so the only document that can reach this
    rule and differ is one that changes a slot identity ignores.)"""
    from service.collections import serve as cserve

    con = service_db.connect()
    try:
        rc = con.execute(
            "SELECT * FROM result_claims rc WHERE NOT EXISTS (SELECT 1 FROM dapper_objects o WHERE o.id = rc.claim_id) LIMIT 1"
        ).fetchone()
        genuine = cserve.render_claim(con, rc)
    finally:
        con.close()
    assert _fetch(genuine["id"]) == (None, None)
    altered = {**genuine, "asserted_in": ["https://example.org/somewhere-else"]}
    assert altered["id"] == genuine["id"] and idn.verify(altered)
    refused = client.post("/v1/deposits", json={"objects": [altered]})
    assert refused.status_code == 400 and "is reserved" in refused.json()["detail"], refused.text
    assert _fetch(genuine["id"]) == (None, None)
    accepted = client.post("/v1/deposits", json={"objects": [genuine]})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["deposited"][0]["status"] == "created"
    row, stored = _fetch(genuine["id"])
    assert stored == genuine and row["identity_profile"] == idn.PROFILE
    # Served afterwards, it is the same claim: the stored document equals what the collection renders.
    served = client.post("/v1/graphql", json={"query": f'{{ claim(id: "{genuine["id"]}") {{ id }} }}'}).json()
    assert "errors" not in served and served["data"]["claim"]["id"] == genuine["id"]



# --- partial releases, files by location, resume ---------------------------

PIGEAN_FIX = FIX / "pigean"
ROLES = {"gene_stats": "combined_gene_stats.json", "gene_set_stats": "combined_gene_set_stats.json",
         "membership": "gene_gene_set_stats.json"}


def _fresh_db():
    return service_db.connect(str(Path(tempfile.mkdtemp()) / "partial.db"))


def _variant_files(tmp_path, phenotype="FIXPHENO"):
    """A copy of the PIGEAN fixture files, optionally as another phenotype's run."""
    out = tmp_path / phenotype
    out.mkdir(parents=True, exist_ok=True)
    for name in ROLES.values():
        rows = [json.loads(line) for line in (PIGEAN_FIX / name).read_text().splitlines() if line.strip()]
        for row in rows:
            row["phenotype"] = phenotype
        (out / name).write_text("".join(json.dumps(r) + "\n" for r in rows))
    return out


def _spec(folder, version="0.1.0-partial", phenotype="FIXPHENO", **extra):
    return {"version": version, **extra, "datasets": [{
        "dimensions": {"trait": phenotype, "library": "fixture-v1"},
        "files": {role: {"path": str(folder / name)} for role, name in ROLES.items()}}]}


def test_where_bounds_ingest_kept_rows_and_whole_rosters(tmp_path):
    # Fixture rows: genes FIXG1 3.5, FIXG2 2, FIXG3 4.75; sets FIXSET_A 1.5, FIXSET_B 0.9;
    # pairs (FIXG1, A), (FIXG2, A), (FIXG1, B). Bounds are strict.
    col = load_collection(COLLECTION)
    folder = _variant_files(tmp_path)
    where = {"gene_phenotype": {"combined": {"gt": 2}}, "gene_set_phenotype": {"beta_uncorrected": {"gt": 1.0}}}
    con = _fresh_db()
    try:
        out = publish(con, col, _spec(folder, where=where), tmp_path)
        assert (out.stats["rows"], out.stats["edges"]) == (3, 2)
        subjects = {(r["kind"], r["subject_value"]) for r in con.execute("SELECT kind, subject_value FROM result_rows")}
        assert subjects == {("gene_phenotype", "FIXG1"), ("gene_phenotype", "FIXG3"), ("gene_set_phenotype", "FIXSET_A")}
        # The kept set keeps its whole roster, including a gene whose own row was dropped.
        edges = {(r["from_value"], r["to_value"]) for r in con.execute("SELECT from_value, to_value FROM result_edges")}
        assert edges == {("FIXG1", "FIXSET_A"), ("FIXG2", "FIXSET_A")}
        sets = [json.loads(r["payload"]) for r in con.execute("SELECT payload FROM dapper_objects WHERE id LIKE 'dapper:GeneSet.%'")]
        assert [(s["name"], s["members"]) for s in sets] == [("FIXSET_A", ["HGNC.SYMBOL:FIXG1", "HGNC.SYMBOL:FIXG2"])]
        # Nothing is minted for a dropped row, and the bounds are named on the Dataset.
        statements = " ".join(json.loads(r["payload"]).get("statement", "") for r in
                              con.execute("SELECT payload FROM dapper_objects WHERE id LIKE 'dapper:Proposition.%'"))
        assert "FIXG2" not in statements and "FIXSET_B" not in statements
        dataset = out.document["datasets"][0]
        assert "combined > 2" in dataset["description"] and "beta_uncorrected > 1" in dataset["description"]
        # Claim ids do not depend on the bounds: the same rows give the same claims unbounded.
        bounded = {r["claim_id"] for r in con.execute("SELECT claim_id FROM result_claims")}
        other = _fresh_db()
        try:
            publish(other, col, _spec(folder), tmp_path)
            assert bounded < {r["claim_id"] for r in other.execute("SELECT claim_id FROM result_claims")}
        finally:
            other.close()
        # The same files under other bounds cannot be a second dataset.
        looser = {"gene_phenotype": {"combined": {"gt": 1}}}
        with pytest.raises(PublishError, match="other `where` bounds"):
            publish(con, col, _spec(folder, version="0.1.1-partial", where=looser), tmp_path)
        with pytest.raises(PublishError, match="not a metric of that row kind"):
            publish(con, col, _spec(folder, version="0.1.2-partial", where={"gene_phenotype": {"beta": {"gt": 1}}}), tmp_path)
    finally:
        con.close()


def test_python_none_is_read_in_edge_files_only(tmp_path):
    col = load_collection(COLLECTION)
    folder = _variant_files(tmp_path)
    membership = folder / ROLES["membership"]
    lines = membership.read_text().splitlines()
    lines[0] = lines[0][:-1] + ', "log_bf": None}'
    membership.write_text("\n".join(lines) + "\n")
    con = _fresh_db()
    try:
        assert publish(con, col, _spec(folder), tmp_path).stats["edges"] == 3
    finally:
        con.close()
    genes = folder / ROLES["gene_stats"]
    genes.write_text(genes.read_text().replace('"n": 4', '"n": None'))
    con = _fresh_db()
    try:
        with pytest.raises(PublishError, match="not valid JSON"):
            publish(con, col, _spec(folder), tmp_path)
    finally:
        con.close()


def test_files_by_location_are_fetched_checked_and_resumed(tmp_path):
    import hashlib

    col = load_collection(COLLECTION)
    folders = {p: _variant_files(tmp_path / "upstream", p) for p in ("FIXPHENO", "OTHERPHENO")}
    spec = {"version": "0.2.0-located", "datasets": []}
    for phenotype, folder in folders.items():
        files = {}
        for role, name in ROLES.items():
            data = (folder / name).read_bytes()
            files[role] = {"location": f"s3://bucket/{phenotype}/{name}", "sha256": hashlib.sha256(data).hexdigest(),
                           "size": len(data)}
        spec["datasets"].append({"dimensions": {"trait": phenotype, "library": "fixture-v1"}, "files": files})
    work = tmp_path / "work"
    calls = []

    def fetch(location, dest, fail_on=None):
        calls.append(location)
        if fail_on and fail_on in location:
            raise PublishError(f"cannot fetch {location}")
        dest.write_bytes((folders[location.split("/")[3]] / location.rsplit("/", 1)[1]).read_bytes())

    con = _fresh_db()
    try:
        with pytest.raises(PublishError, match="needs a fetcher"):
            publish(con, col, spec, tmp_path)
        # The second dataset's fetch fails: the first is stored, no release is recorded, nothing is left on disk.
        with pytest.raises(PublishError, match="cannot fetch"):
            publish(con, col, spec, tmp_path, workdir=work, fetch=lambda loc, dest: fetch(loc, dest, "OTHERPHENO"))
        assert con.execute("SELECT COUNT(DISTINCT dataset_id) AS n FROM dataset_files").fetchone()["n"] == 1
        assert con.execute("SELECT COUNT(*) AS n FROM collection_releases").fetchone()["n"] == 0
        assert not [p for p in work.rglob("*") if p.is_file()]
        # A staged run that has nothing left under its limit stores nothing new and records no release.
        staged = publish(con, col, spec, tmp_path, workdir=work, fetch=fetch, max_ingest=0)
        assert not staged.complete and "not recorded" in staged.warnings[-1]
        assert con.execute("SELECT COUNT(*) AS n FROM collection_releases").fetchone()["n"] == 0
        # A shard that owns nothing still missing does the same.
        sharded = publish(con, col, spec, tmp_path, workdir=work, fetch=fetch, shard=(0, 2))
        assert not sharded.complete and sharded.stats["datasets_ingested"] == 0
        # Re-running fetches and ingests only what is missing, then records the release.
        calls.clear()
        out = publish(con, col, spec, tmp_path, workdir=work, fetch=fetch)
        assert out.complete and out.stats["datasets_ingested"] == 1 and all("OTHERPHENO" in c for c in calls)
        assert con.execute("SELECT COUNT(*) AS n FROM release_datasets").fetchone()["n"] == 2
        assert not [p for p in work.rglob("*") if p.is_file()]
        # A file that is not the declared bytes is refused.
        bad = copy.deepcopy(spec)
        bad["version"] = "0.2.1-located"
        bad["datasets"] = [copy.deepcopy(spec["datasets"][0])]
        bad["datasets"][0]["dimensions"]["trait"] = "THIRDPHENO"
        bad["datasets"][0]["files"]["gene_stats"]["sha256"] = "0" * 64
        with pytest.raises(PublishError, match="the release declares"):
            publish(con, col, bad, tmp_path, workdir=work, fetch=fetch)
    finally:
        con.close()
