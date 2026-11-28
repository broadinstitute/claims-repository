"""Collections (docs/collections.md): manifests and templates checked at load,
DAPPER-ID-1 minting through dapper's own module, publishing a release, claims
minted on serve and checked against their recorded ids, discovery (the query
catalog), and the query executor."""
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

from collection_claims import implied_claims, implied_ids

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
    vectors = json.loads((idn.VENDOR / "identity/test_vectors.json").read_text())["vectors"]
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
    assert published.stats["mint_contexts_recorded"] == 3 and "claims" not in published.stats  # nothing rendered
    con = service_db.connect()
    try:
        assert len(implied_ids(con)) == 20  # 3 genes x 4 + 2 sets x 3 + 2 rosters, rendered when served
        assert con.execute("SELECT COUNT(*) AS n FROM result_claims").fetchone()["n"] == 0
    finally:
        con.close()
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
    assert again.stats["datasets_ingested"] == 0 and again.stats["mint_contexts_recorded"] == 0


def test_a_new_version_chains_and_keeps_claim_ids(published):
    con = service_db.connect()
    before = implied_ids(con, published.release_id)
    con.close()
    second = _publish(version="0.0.2-test")
    assert second.release_id != published.release_id
    assert second.document["result_collections"][0]["was_revision_of"] == published.release_id
    assert second.stats["mint_contexts_recorded"] == 0  # same templates, same files: the same mint contexts
    con = service_db.connect()
    after = implied_ids(con, second.release_id)
    con.close()
    assert after == before and len(after) == 20


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

REVEAL_QUERY = ("query Candidates($variables: JSON) { runQuery(collection: \"pigean\", query: \"top-genes-with-sets\", "
                "variables: $variables, claims: [\"measurement\"]) }")


def test_reveal_candidates_come_from_run_query(published):
    """What reveal-mechanisms' scientific_account_job.sh needs from each
    candidate (gene, combined, direct = log BF, prior, claim id, trait id), read
    off runQuery rows; the PIGEAN-specific GraphQL fields it used are gone."""
    body = client.post("/v1/graphql", json={"query": REVEAL_QUERY, "variables": {"variables": {
        "trait": "FIXPHENO", "library": "fixture-v1", "priorMax": 1.0, "topGenes": 5, "setsPerGene": 1}}}).json()
    assert "errors" not in body, body.get("errors")
    rows = body["data"]["runQuery"]["rows"]
    candidates = [{"gene": r["subject"]["value"], "combined": r["metrics"]["combined"],
                   "direct": r["metrics"]["log_bf"], "prior": r["metrics"]["prior"],
                   "claim_id": r["claims"]["measurement"]["id"],
                   "trait": r["claims"]["measurement"]["triple"]["object"]} for r in rows]
    assert [c["gene"] for c in candidates] == ["FIXG1", "FIXG2"]
    assert all(c["claim_id"].startswith("dapper:Claim.") and c["trait"] == "FIXPHENO" for c in candidates)
    assert all(c["prior"] < 1.0 for c in candidates)
    assert [r["object"]["id"] for r in rows] == [c["trait"] for c in candidates]
    for field in ("pigeanValidationChallenge(phenotype: \"FIXPHENO\") { warnings }", "methods { name }",
                  "gene(symbol: \"FIXG1\") { symbol }", "phenotypes { id }"):
        gone = client.post("/v1/graphql", json={"query": "{ %s }" % field}).json()
        assert gone.get("errors") and "Cannot query field" in gone["errors"][0]["message"], field


def test_dimension_values_list_what_a_param_takes(published):
    traits = client.get("/v1/collections/pigean/dimensions/trait").json()
    assert [v["value"] for v in traits["values"]] == ["FIXPHENO"] and traits["count"] == 1
    assert traits["values"][0]["datasets"] == 1
    assert client.get("/v1/collections/pigean/dimensions/trait", params={"search": "fixph"}).json()["count"] == 1
    assert client.get("/v1/collections/pigean/dimensions/trait", params={"search": "zzz"}).json()["values"] == []
    libraries = client.get("/v1/collections/pigean/dimensions/library", params={"first": 0}).json()
    assert libraries["count"] == 1 and libraries["values"] == [] and libraries["truncated"] is True
    assert client.get("/v1/collections/pigean/dimensions/nope").status_code == 404
    gql = client.post("/v1/graphql", json={"query": '{ dimensionValues(collection: "pigean", dimension: "library") }'})
    assert gql.json()["data"]["dimensionValues"]["values"][0]["value"] == "fixture-v1"


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
    """A claim served once resolves by id afterwards, rendered again from its row: if the row no longer renders
    that claim, it is refused rather than served."""
    body = _run({"trait": "FIXPHENO", "library": "fixture-v1", "priorMax": 100.0, "topGenes": 20, "setsPerGene": 0},
                claims=["measurement"]).json()
    claim_id = body["rows"][0]["claims"]["measurement"]["id"]
    con = service_db.connect()
    try:
        r = con.execute("SELECT rr.row_id, rr.row_json FROM result_claims rc JOIN result_rows rr ON rr.row_id = rc.row_id "
                        "WHERE rc.claim_id = ?", (claim_id,)).fetchone()
        tampered = {**json.loads(r["row_json"]), "trait_group": "tampered"}
        with con:
            con.execute("UPDATE result_rows SET row_json = ? WHERE row_id = ?", (idn.canonical(tampered), r["row_id"]))
        got = client.post("/v1/graphql", json={"query": "query($id: ID!) { claim(id: $id) { id } }",
                                               "variables": {"id": claim_id}}).json()
        assert "template drift" in json.dumps(got.get("errors")), got
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
    assert set(d["claims"]) == {"minted"}
    queries = {q["name"]: q for q in client.get("/v1/collections/pigean/queries").json()["queries"]}
    assert queries[QUERY]["run"]["rest"]["body"]["variables"]["trait"] == "IBD"
    assert d["datasets_page"] == {"total": 1, "offset": 0, "first": 50, "returned": 1, "next_offset": None}
    empty = client.get("/v1/collections/pigean", params={"first": 0}).json()
    assert empty["datasets"] == [] and empty["datasets_page"]["next_offset"] == 0
    assert client.get("/v1/collections/pigean", params={"first": 501}).status_code == 422
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

    # Served once (its address is recorded), then removed from the store as if never minted.
    body = _run({"trait": "FIXPHENO", "library": "fixture-v1", "priorMax": 10.0, "topGenes": 1, "setsPerGene": 0},
                claims=["tier-direct"]).json()
    claim_id = body["rows"][0]["claims"]["tier-direct"]["id"]
    con = service_db.connect()
    try:
        with con:
            con.execute("DELETE FROM claims WHERE claim_id = ?", (claim_id,))
            con.execute("DELETE FROM object_refs WHERE from_id = ?", (claim_id,))
            con.execute("DELETE FROM dapper_objects WHERE id = ?", (claim_id,))
        genuine = cserve.render_claim(con, cserve.served_claim(con, claim_id))
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
        bounded = implied_ids(con, out.release_id)
        other = _fresh_db()
        try:
            whole = publish(other, col, _spec(folder), tmp_path)
            assert bounded < implied_ids(other, whole.release_id)
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


def test_files_without_a_sha256_are_hashed_when_fetched(tmp_path):
    """A release may give files by location only: each dataset's files are
    hashed when it is fetched, the caller is told each hash, and the release
    is recorded only when every file is known. Same release as with hashes."""
    import hashlib

    col = load_collection(COLLECTION)
    folders = {p: _variant_files(tmp_path / "upstream", p) for p in ("FIXPHENO", "OTHERPHENO")}

    def spec(with_sha):
        out = {"version": "0.3.0-unhashed", "datasets": []}
        for phenotype, folder in folders.items():
            files = {}
            for role, name in ROLES.items():
                data = (folder / name).read_bytes()
                files[role] = {"location": f"s3://bucket/{phenotype}/{name}", "size": len(data)}
                if with_sha:
                    files[role]["sha256"] = hashlib.sha256(data).hexdigest()
            out["datasets"].append({"dimensions": {"trait": phenotype, "library": "fixture-v1"}, "files": files})
        return out

    def fetch(location, dest):
        dest.write_bytes((folders[location.split("/")[3]] / location.rsplit("/", 1)[1]).read_bytes())

    hashed = {}
    con = _fresh_db()
    try:
        first = publish(con, col, spec(False), tmp_path, workdir=tmp_path / "w", fetch=fetch, max_ingest=1,
                        on_hash=lambda loc, sha, size: hashed.__setitem__(loc, sha))
        assert not first.complete and first.release_id is None and len(hashed) == 3
        assert con.execute("SELECT COUNT(*) AS n FROM collection_releases").fetchone()["n"] == 0
        # The next call knows the first dataset's hashes, so it skips that dataset and hashes the other.
        resumed = spec(False)
        for ds in resumed["datasets"]:
            for f in ds["files"].values():
                if f["location"] in hashed:
                    f["sha256"] = hashed[f["location"]]
        done = publish(con, col, resumed, tmp_path, workdir=tmp_path / "w", fetch=fetch,
                       on_hash=lambda loc, sha, size: hashed.__setitem__(loc, sha))
        assert done.complete and done.stats["datasets_ingested"] == 1 and len(hashed) == 6
    finally:
        con.close()
    known = _fresh_db()
    try:
        assert publish(known, col, spec(True), tmp_path, workdir=tmp_path / "w2", fetch=fetch).release_id == done.release_id
    finally:
        known.close()


def test_explorer_examples_validate_and_run(published):
    """Every example the GraphiQL page opens on is valid against the schema and
    runs without errors (here on the fixture collection: FIXPHENO, fixture-v1)."""
    from graphql import parse, validate

    from service import explorer
    from service.app import schema

    examples = explorer.examples()
    assert [e["name"][:2] for e in examples] == [f"{i:02d}" for i in range(1, len(examples) + 1)]
    assert any("runQuery" in e["query"] for e in examples) and any("queries" in e["query"] for e in examples)
    fixture = {"phenotype": "FIXPHENO", "trait": "FIXPHENO", "library": "fixture-v1", "search": "fix", "gene": "FIXG1"}
    for e in examples:
        assert e["query"].lstrip().startswith("#"), f"{e['name']} should open with a comment saying what it does"
        assert validate(schema, parse(e["query"])) == [], e["name"]
        variables = json.loads(e["variables"] or "{}")
        variables = {k: fixture.get(k, v) for k, v in variables.items()}
        if isinstance(variables.get("variables"), dict):
            variables["variables"] = {k: fixture.get(k, v) for k, v in variables["variables"].items()}
        body = client.post("/v1/graphql", json={"query": e["query"], "variables": variables}).json()
        assert "errors" not in body, (e["name"], body.get("errors"))
    page = client.get("/v1/graphql")
    assert page.status_code == 200
    assert "ValidationChallenge" in page.text and "claims-graphiql-" in page.text and "</script>" in page.text


# --- the query catalog (last: it serves every claim the fixture implies) ----

def _ids(rows):
    return json.dumps(rows, sort_keys=True, default=str) if not rows else [
        (r["subject"]["id"], sorted((k, c["id"]) for k, c in r["claims"].items()),
         [(i["row"]["subject"]["id"], sorted((k, c["id"]) for k, c in i["edge"]["claims"].items()))
          for i in r.get("sets", [])]) for r in rows]


def _sample_value(param):
    return param["values"]["sample"][0]["value"]


def test_the_catalog_alone_is_enough_to_run_every_query(published):
    """A client that reads only GET /v1/queries can build and run every query
    it lists, and the rows come back as the entry says."""
    catalog = client.get("/v1/queries").json()
    assert "runQuery" in catalog["guide"]["graphql"]["field"]
    assert catalog == client.post("/v1/graphql", json={"query": "{ queries }"}).json()["data"]["queries"]
    assert catalog["queries"], "the fixture collection has a query"
    for entry in catalog["queries"]:
        variables = dict(entry["run"]["rest"]["body"]["variables"])
        for name, param in entry["params"].items():
            if "dimension" in param:
                assert param["values"]["count"] >= 1 and param["values"]["all"].endswith("/" + param["dimension"])
                variables[name] = _sample_value(param)
            if "subject" in param:  # a gene or gene set the fixture holds
                variables[name] = {"Gene": "FIXG1", "GeneSet": "FIXSET_A"}[
                    entry["returns"]["rows"]["subject"]["entity"]]
        rest = client.post(entry["run"]["rest"]["path"], json={"variables": variables})
        assert rest.status_code == 200, rest.text
        gql = client.post("/v1/graphql", json={"query": entry["run"]["graphql"]["query"],
                                               "variables": {"variables": variables}}).json()
        assert "errors" not in gql, gql.get("errors")
        rows = rest.json()["rows"]
        assert rows and _ids(gql["data"]["runQuery"]["rows"]) == _ids(rows)  # counters differ: each serve asserts
        shape = entry["returns"]
        served = set()
        for row in rows:
            assert row["kind"] == shape["rows"]["kind"]
            assert set(row["metrics"]) == set(shape["rows"]["metrics"])
            assert set(row["columns"]) == set(shape["rows"]["columns"])
            assert set(row["claims"]) <= set(shape["rows"]["claims"])
            served |= set(row["claims"])
            for name, join in shape["joins"].items():
                for item in row[name]:
                    assert item["row"]["kind"] == join["row"]["kind"]
                    assert set(item["edge"]["claims"]) <= set(join["edge"]["claims"])
                    served |= set(item["edge"]["claims"]) | set(item["row"]["claims"])
        assert served and served <= set(entry["claims"])
        only = client.post(entry["run"]["rest"]["path"],
                           json={"variables": variables, "claims": [entry["claims"][0]]}).json()["rows"]
        assert all(set(r["claims"]) <= {entry["claims"][0]} for r in only)
        # The fixture holds fixture-v1, not the templates' default mouse_msigdb library.
        assert any("'mouse_msigdb' is not in this release" in n for n in entry["notes"])
        if entry["name"] == QUERY:
            meaning = shape["rows"]["metrics"]["combined"]
            assert meaning["scale"] == "log posterior odds" and meaning["better"] == "higher"
            assert shape["rows"]["claims"]["measurement"]["scores"] == ["combined", "log_bf", "prior"]
            assert any("'IBD' is not in this release" in n for n in entry["notes"])
        else:  # the across-traits templates: one subject, every trait
            assert {r["subject"]["value"] for r in rows} == {variables.get("gene") or variables.get("geneSet")}
    assert client.get("/v1/queries", params={"collection": "nope"}).status_code == 404
    one = {q["name"]: q for q in client.get("/v1/collections/pigean/queries").json()["queries"]}
    assert set(one) == {QUERY, "gene-across-traits", "gene-set-across-traits"}
    assert one[QUERY]["params"]["trait"]["dimension"] == "trait" and "subject" in one["gene-across-traits"]["params"]["gene"]
    [parts] = catalog["collections"]
    assert parts["row_kinds"]["gene_phenotype"]["metrics"] == ["combined", "log_bf", "prior"]
    assert parts["edge_kinds"]["member_of"] == {"from": "Gene", "to": "GeneSet"} and parts["dimensions"] == ["trait", "library"]


def test_rows_asks_questions_in_graphql_with_the_templates_rules(published):
    q = """query($subject: [String!], $where: [MetricCondition!], $order: MetricOrder!, $first: Int) {
      rows(collection: "pigean", kind: "gene_phenotype", dimensions: [{name: "trait", value: "FIXPHENO"}],
           subject: $subject, where: $where, orderBy: $order, first: $first) {
        warnings release { version }
        rows { kind subject { id value } object { id } dataset { dimensions { name value } }
               prior: metric(name: "prior") metrics { name value } claimKeys
               claim(key: "measurement") { id triple { subject object } }
               joined(via: "member_of", kind: "gene_set_phenotype", orderBy: {metric: "beta"}, first: 2) {
                 row { subject { value } beta: metric(name: "beta") claim(key: "measurement") { id } }
                 edge { kind claimKeys claim(key: "roster") { id } } } } } }"""

    def run(**v):
        return client.post("/v1/graphql", json={"query": q, "variables": {"order": {"metric": "prior", "dir": "ASC"},
                                                                          **v}}).json()

    body = run(where=[{"metric": "log_bf", "gte": 0}], first=5)
    assert "errors" not in body, body.get("errors")
    rows = body["data"]["rows"]["rows"]
    priors = [r["prior"] for r in rows]
    assert priors == sorted(priors) and len(rows) == 3 and body["data"]["rows"]["warnings"] == []
    template = _run({"trait": "FIXPHENO", "library": "fixture-v1", "topGenes": 20, "setsPerGene": 2},
                    claims=["measurement"]).json()["rows"]
    by_gene = {r["subject"]["value"]: r for r in template}
    for r in rows:  # the same claims a template serves
        assert r["claim"]["id"] == by_gene[r["subject"]["value"]]["claims"]["measurement"]["id"]
        assert "measurement" in r["claimKeys"] and r["dataset"]["dimensions"][1] == {"name": "trait", "value": "FIXPHENO"}
    with_sets = next(r for r in rows if r["joined"])
    assert with_sets["joined"][0]["edge"]["claim"]["id"].startswith("dapper:Claim.")
    one = run(subject=["FIXG1"])["data"]["rows"]["rows"]
    assert [r["subject"]["value"] for r in one] == ["FIXG1"]
    assert run(subject=["NOPE"])["data"]["rows"]["warnings"]

    def refused(fragment, query=None, **v):
        out = client.post("/v1/graphql", json={"query": query or q, "variables": {
            "order": {"metric": "prior"}, **v}}).json()
        assert out.get("errors") and fragment in out["errors"][0]["message"], out

    refused("not a metric of gene_phenotype", order={"metric": "beta"})
    refused("not a metric of gene_phenotype", where=[{"metric": "beta", "gt": 0}])
    refused("must be between 0 and 500", first=501)
    refused("has no row kind", query='{ rows(collection: "pigean", kind: "nope", orderBy: {metric: "x"}) { warnings } }')
    refused("cannot join", query="""{ rows(collection: "pigean", kind: "gene_phenotype", orderBy: {metric: "prior"}) {
      rows { joined(via: "member_of", kind: "gene_phenotype", orderBy: {metric: "prior"}) { row { kind } } } } }""")
    assert client.post("/v1/collections/pigean/plans:run", json={"plan": []}).status_code in (404, 405)
