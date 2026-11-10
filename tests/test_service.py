"""Contract tests on a throwaway DB: deposit idempotency, the objects-only
REST surface (resolver, list, search), GraphQL queries over deposited claims,
and — via the real loader on small fixtures — claims synthesized when served
(2026-11-09: claims are minted when queried)."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["CLAIMS_DB"] = os.path.join(tempfile.mkdtemp(), "test-claims.db")

from fastapi.testclient import TestClient  # noqa: E402

from service import db as service_db  # noqa: E402
from service import pigean_template as tpl  # noqa: E402
from service.app import app  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "pigean"
client = TestClient(app)


def _bundle():
    return json.loads((ROOT / "api" / "examples" / "deposit-bundle.request.json").read_text())


def _gql(query, variables=None):
    return client.post("/v1/graphql", json={"query": query, "variables": variables or {}}).json()


def test_health():
    body = client.get("/v1/health").json()
    assert body["status"] == "ok"
    assert body["dapper_profile"] == "0.2.0-a1"


def test_swagger_docs_and_contract():
    docs = client.get("/v1/docs")
    assert docs.status_code == 200
    assert "SwaggerUIBundle" in docs.text
    contract = client.get("/v1/openapi.yaml")
    assert contract.status_code == 200
    assert contract.text.startswith("openapi:")
    # REST output is objects only: resolver + general list + search.
    for path in ("/v1/objects/{object_id}:", "/v1/objects:", "/v1/objects/search:"):
        assert path in contract.text
    for removed in ("/v1/claims/{claim_id}", "/v1/claims/search", "/v1/propositions"):
        assert removed not in contract.text
    # This deployment is injected as the FIRST server, so Swagger try-it-out
    # stays same-origin (the localhost vs 127.0.0.1 CORS trap).
    first_server = contract.text.split("servers:\n", 1)[1].splitlines()[0]
    assert "http://testserver" in first_server
    # Deployed behind a TLS-terminating load balancer the request reads as
    # http; the configured public base must win so an https docs page never
    # defaults to an http server (mixed content: "Failed to fetch").
    import service.app as service_app
    saved = service_app.PUBLIC_BASE
    service_app.PUBLIC_BASE = "https://api-qa.example.org/api/claims"
    try:
        deployed = client.get("/v1/openapi.yaml").text
    finally:
        service_app.PUBLIC_BASE = saved
    assert deployed.split("servers:\n", 1)[1].splitlines()[0] == "  - url: https://api-qa.example.org/api/claims"
    for tag in ("[Meta]", "[Deposit]", "[Objects]", "[GraphQL]"):
        assert tag in contract.text
    # Cross-origin reads are allowed (browser UIs call this API directly).
    cors = client.get("/v1/health", headers={"Origin": "http://127.0.0.1:9999"})
    assert cors.headers.get("access-control-allow-origin") == "*"


def test_deposit_is_idempotent_and_retrievable():
    first = client.post("/v1/claims", json=_bundle())
    assert first.status_code == 200
    statuses = {d["id"]: d["status"] for d in first.json()["deposited"]}
    assert statuses["dapper:Claim.SYNTHG01"] == "created"

    second = client.post("/v1/claims", json=_bundle())
    deposited = second.json()["deposited"]
    assert {d["status"] for d in deposited} == {"exists"}
    # The counter tracks every idempotent re-deposit of the same content.
    times = {d["id"]: d["times_deposited"] for d in deposited}
    assert times["dapper:Claim.SYNTHG01"] == 2

    got = client.get("/v1/objects/dapper:Claim.SYNTHG01")
    assert got.status_code == 200
    assert got.json()["class_name"] == "Claim"
    env = _gql("""{ claim(id: "dapper:Claim.SYNTHG01") {
        scores { metric } provenanceRefs { resolution } timesDeposited } }""")["data"]["claim"]
    assert {s["metric"] for s in env["scores"]} == {"combined", "log_bf", "prior"}
    assert env["provenanceRefs"]["resolution"] == "UNRESOLVED"  # File not deposited
    assert env["timesDeposited"] >= 2


def test_measurement_claim_carries_evidence():
    """has_evidence -> EvidenceItem -> ValueLocator, dereferenced on the
    GraphQL envelope."""
    client.post("/v1/claims", json=_bundle())
    env = _gql("""{ claim(id: "dapper:Claim.SYNTHG01") {
        triple { subject predicate object } evidence { item valueLocators } } }""")["data"]["claim"]
    assert env["triple"] == {"subject": "NOD2", "predicate": "associated_with", "object": "IBD"}
    assert len(env["evidence"]) == 1
    item = env["evidence"][0]["item"]
    assert item["class_name"] == "EvidenceItem"
    assert item["target_proposition"] == "dapper:Proposition.SYNTHG01"
    locators = env["evidence"][0]["valueLocators"]
    assert [loc["value_text"] for loc in locators] == ["9.8"]
    assert locators[0]["file"] == "dapper:File.SYNTHGENESTATS"


def test_identifier_resolution_and_resolver_urls():
    """CURIE stays canonical; every response offers the shareable URL form."""
    client.post("/v1/claims", json=_bundle())
    idres = client.get("/v1/vocabulary").json()["identifier_resolution"]
    assert idres["prefixes"]["dapper"].startswith("https://")
    assert idres["resolver_url_template"].endswith("/v1/objects/{id}")
    data = _gql("""{ claim(id: "dapper:Claim.SYNTHG01") { resolverUrl } identifierResolution }""")["data"]
    assert data["claim"]["resolverUrl"].endswith("/v1/objects/dapper:Claim.SYNTHG01")
    assert "dapper" in data["identifierResolution"]["prefixes"]
    listed = client.get("/v1/objects", params={"limit": 1}).json()
    first = listed["objects"][0]
    assert first["resolver_url"].endswith(f"/v1/objects/{first['id']}")


def test_object_landing_page_negotiation():
    """A shared resolver link opens as a page in a browser and as the JSON
    document everywhere else."""
    client.post("/v1/claims", json=_bundle())
    page = client.get("/v1/objects/dapper:Claim.SYNTHG01", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "dapper:Claim.SYNTHG01" in page.text
    # Referenced dapper IDs are linked through the same resolver, and the
    # reverse direction through the references search.
    assert "/v1/objects/dapper:Proposition.SYNTHG01" in page.text
    assert "/v1/objects/search?references=dapper:Claim.SYNTHG01" in page.text
    raw = client.get("/v1/objects/dapper:Claim.SYNTHG01")
    assert raw.headers["content-type"].startswith("application/json")
    assert raw.json()["class_name"] == "Claim"


def test_graph_query_over_deposited_claim():
    client.post("/v1/claims", json=_bundle())
    body = _gql("""{ phenotype(id: "IBD") {
        pigeanGeneAssociations(where: {prior: {lt: 0.5}}, first: 5) {
          gene { symbol } combined prior claim { id scores { metric value } } } } }""")
    assert "errors" not in body, body.get("errors")
    assocs = body["data"]["phenotype"]["pigeanGeneAssociations"]
    assert [a["gene"]["symbol"] for a in assocs] == ["NOD2"]
    assert assocs[0]["claim"]["id"] == "dapper:Claim.SYNTHG01"


def test_challenge_reports_missing_membership_source():
    client.post("/v1/claims", json=_bundle())
    query = (ROOT / "api" / "graphql" / "queries" / "ibd-validation-challenge.graphql").read_text()
    variables = json.loads((ROOT / "api" / "graphql" / "queries" / "ibd-validation-challenge.variables.json").read_text())
    body = _gql(query, variables)
    assert "errors" not in body, body.get("errors")
    challenge = body["data"]["pigeanValidationChallenge"]
    # A missing data source is not an empty result: anchors come back with no
    # sets, and the gap is stated.
    assert [r["geneAssociation"]["gene"]["symbol"] for r in challenge["rows"]] == ["NOD2"]
    assert challenge["rows"][0]["sets"] == []
    assert challenge["truncated"] is False
    assert any("membership" in w for w in challenge["warnings"])


def _membership_bundle():
    """An externally deposited membership claim (REST deposit path) plus the
    gene-set association the challenge join needs on the far side."""
    return {
        "objects": [
            {
                "class_name": "GeneSet",
                "id": "dapper:GeneSet.SYNTHSET01",
                "name": "SYNTH_SET",
                "member_type": "gene",
                "members": ["NOD2"],
                "n_genes": 1,
            },
            {
                "class_name": "Proposition",
                "id": "dapper:Proposition.SYNTHS01",
                "statement": "The gene set SYNTH_SET is associated with IBD.",
                "subject_entity": "dapper:GeneSet.SYNTHSET01",
                "relation": "associated_with",
                "object_entity": "IBD",
                "proposition_kind": "RESULT",
            },
            {
                "class_name": "ClaimScore",
                "id": "dapper:ClaimScore.SYNTHS01beta",
                "score_kind": "EFFECT_ESTIMATE",
                "metric": "beta",
                "value": 1.2,
                "interpretation": "PIGEAN joint gene-set effect. SYNTHETIC.",
            },
            {
                "class_name": "ClaimScore",
                "id": "dapper:ClaimScore.SYNTHS01bu",
                "score_kind": "EFFECT_ESTIMATE",
                "metric": "beta_uncorrected",
                "value": 2.5,
                "interpretation": "PIGEAN marginal gene-set effect. SYNTHETIC.",
            },
            {
                "class_name": "Claim",
                "id": "dapper:Claim.SYNTHS01",
                "proposition": "dapper:Proposition.SYNTHS01",
                "statement": "PIGEAN assessed the association of gene set SYNTH_SET with IBD.",
                "has_score": ["dapper:ClaimScore.SYNTHS01beta", "dapper:ClaimScore.SYNTHS01bu"],
            },
            {
                "class_name": "Proposition",
                "id": "dapper:Proposition.SYNTHM01",
                "statement": "The gene NOD2 is a member of the gene set SYNTH_SET.",
                "subject_entity": "NOD2",
                "relation": "member_of",
                "object_entity": "dapper:GeneSet.SYNTHSET01",
                "proposition_kind": "RESULT",
            },
            {
                "class_name": "Claim",
                "id": "dapper:Claim.SYNTHM01",
                "proposition": "dapper:Proposition.SYNTHM01",
                "statement": "The cfde-inc-v2 gene-set collection includes NOD2 as a member of SYNTH_SET.",
            },
        ],
        "index_hints": [
            {
                "claim_id": "dapper:Claim.SYNTHS01",
                "method": "pigean",
                "predicate": "associated_with",
                "subject": {"category": "gene_set", "id": "SYNTH_SET", "dapper_id": "dapper:GeneSet.SYNTHSET01"},
                "object": {"category": "phenotype", "id": "IBD"},
                "collection": "cfde-inc-v2",
                "trait_group": "portal",
                "source": "lincs",
            },
            {
                "claim_id": "dapper:Claim.SYNTHM01",
                "method": "pigean",
                "predicate": "member_of",
                "subject": {"category": "gene", "id": "NOD2"},
                "object": {"category": "gene_set", "id": "SYNTH_SET", "dapper_id": "dapper:GeneSet.SYNTHSET01"},
                "collection": "cfde-inc-v2",
            },
        ],
    }


def test_challenge_joins_after_membership_deposit():
    # Runs after test_challenge_reports_missing_membership_source by design:
    # that test needs the shared tmp DB's membership table still empty.
    client.post("/v1/claims", json=_bundle())
    assert client.post("/v1/claims", json=_membership_bundle()).status_code == 200
    query = (ROOT / "api" / "graphql" / "queries" / "ibd-validation-challenge.graphql").read_text()
    variables = json.loads((ROOT / "api" / "graphql" / "queries" / "ibd-validation-challenge.variables.json").read_text())
    body = _gql(query, variables)
    assert "errors" not in body, body.get("errors")
    challenge = body["data"]["pigeanValidationChallenge"]
    assert not any("membership" in w for w in challenge["warnings"])
    assert len(challenge["rows"]) == 1
    row = challenge["rows"][0]
    assert row["geneAssociation"]["gene"]["symbol"] == "NOD2"
    assert row["geneAssociation"]["claim"]["triple"] == {
        "subject": "NOD2", "predicate": "associated_with", "object": "IBD"
    }
    assert len(row["sets"]) == 1
    entry = row["sets"][0]
    assert entry["membership"]["claim"]["id"] == "dapper:Claim.SYNTHM01"
    assert entry["membership"]["claim"]["triple"]["predicate"] == "member_of"
    assert entry["geneSetAssociation"]["geneSet"]["name"] == "SYNTH_SET"
    assert entry["geneSetAssociation"]["betaUncorrected"] == 2.5
    # The membership claim is score-less.
    membership = _gql("""{ claim(id: "dapper:Claim.SYNTHM01") { scores { metric } } }""")["data"]["claim"]
    assert membership["scores"] == []


def test_resolver_returns_consistent_documents():
    """Every class of dapper object comes back in the same shape: id and
    class_name first, remaining keys sorted, matching the requested ID."""
    client.post("/v1/claims", json=_bundle())
    client.post("/v1/claims", json=_membership_bundle())
    cases = [
        ("dapper:Claim.SYNTHG01", "Claim"),
        ("dapper:Proposition.SYNTHG01", "Proposition"),
        ("dapper:ClaimScore.SYNTHG01combined", "ClaimScore"),
        ("dapper:EvidenceItem.SYNTHG01combined", "EvidenceItem"),
        ("dapper:ValueLocator.SYNTHG01combined", "ValueLocator"),
        ("dapper:GeneSet.SYNTHSET01", "GeneSet"),
    ]
    for oid, cls in cases:
        body = client.get(f"/v1/objects/{oid}").json()
        keys = list(body)
        assert keys[0] == "id" and keys[1] == "class_name", (oid, keys[:2])
        assert body["id"] == oid
        assert body["class_name"] == cls
        assert keys[2:] == sorted(keys[2:]), oid
        gql = _gql(f'{{ dapperObject(id: "{oid}") }}')
        assert gql["data"]["dapperObject"]["id"] == oid


def _second_set_bundle():
    """A second, weaker gene set for NOD2 so the per-gene limit is testable."""
    return {
        "objects": [
            {"class_name": "Proposition", "id": "dapper:Proposition.SYNTHS02",
             "statement": "The gene set SYNTH_SET_B is associated with IBD.",
             "subject_entity": "SYNTH_SET_B", "relation": "associated_with",
             "object_entity": "IBD", "proposition_kind": "RESULT"},
            {"class_name": "ClaimScore", "id": "dapper:ClaimScore.SYNTHS02beta",
             "score_kind": "EFFECT_ESTIMATE", "metric": "beta", "value": 0.3,
             "interpretation": "PIGEAN joint gene-set effect. SYNTHETIC."},
            {"class_name": "ClaimScore", "id": "dapper:ClaimScore.SYNTHS02bu",
             "score_kind": "EFFECT_ESTIMATE", "metric": "beta_uncorrected", "value": 0.9,
             "interpretation": "PIGEAN marginal gene-set effect. SYNTHETIC."},
            {"class_name": "Claim", "id": "dapper:Claim.SYNTHS02",
             "proposition": "dapper:Proposition.SYNTHS02",
             "has_score": ["dapper:ClaimScore.SYNTHS02beta", "dapper:ClaimScore.SYNTHS02bu"]},
            {"class_name": "Proposition", "id": "dapper:Proposition.SYNTHM02",
             "statement": "The gene NOD2 is a member of the gene set SYNTH_SET_B.",
             "subject_entity": "NOD2", "relation": "member_of",
             "object_entity": "SYNTH_SET_B", "proposition_kind": "RESULT"},
            {"class_name": "Claim", "id": "dapper:Claim.SYNTHM02",
             "proposition": "dapper:Proposition.SYNTHM02"},
        ],
        "index_hints": [
            {"claim_id": "dapper:Claim.SYNTHS02", "method": "pigean", "predicate": "associated_with",
             "subject": {"category": "gene_set", "id": "SYNTH_SET_B"},
             "object": {"category": "phenotype", "id": "IBD"},
             "collection": "cfde-inc-v2", "trait_group": "portal", "source": "lincs"},
            {"claim_id": "dapper:Claim.SYNTHM02", "method": "pigean", "predicate": "member_of",
             "subject": {"category": "gene", "id": "NOD2"},
             "object": {"category": "gene_set", "id": "SYNTH_SET_B"},
             "collection": "cfde-inc-v2"},
        ],
    }


def test_sets_per_gene_limit_and_order():
    client.post("/v1/claims", json=_bundle())
    client.post("/v1/claims", json=_membership_bundle())
    assert client.post("/v1/claims", json=_second_set_bundle()).status_code == 200
    q = """
      query($n: Int!) {
        pigeanValidationChallenge(
          phenotype: "IBD", where: {prior: {lt: 0.5}}, topGenes: 5, setsPerGene: $n
        ) {
          rows {
            geneAssociation { gene { symbol } }
            sets { geneSetAssociation { geneSet { name } betaUncorrected } }
          }
        }
      }
    """

    def set_names(n):
        body = _gql(q, {"n": n})
        assert "errors" not in body, body.get("errors")
        rows = body["data"]["pigeanValidationChallenge"]["rows"]
        assert [r["geneAssociation"]["gene"]["symbol"] for r in rows] == ["NOD2"]
        return [s["geneSetAssociation"]["geneSet"]["name"] for s in rows[0]["sets"]]

    assert set_names(5) == ["SYNTH_SET", "SYNTH_SET_B"]
    assert set_names(1) == ["SYNTH_SET"]


def test_gene_set_entity_is_traceable():
    """claim -> geneSetRefs / dapperGeneSetId -> full GeneSet entity record."""
    client.post("/v1/claims", json=_bundle())
    client.post("/v1/claims", json=_membership_bundle())
    env = _gql("""{ claim(id: "dapper:Claim.SYNTHS01") {
        triple { subject predicate object } provenanceRefs { geneSetRefs resolution } } }""")["data"]["claim"]
    assert env["triple"] == {
        "subject": "dapper:GeneSet.SYNTHSET01", "predicate": "associated_with", "object": "IBD",
    }
    assert env["provenanceRefs"]["geneSetRefs"] == ["dapper:GeneSet.SYNTHSET01"]
    assert env["provenanceRefs"]["resolution"] == "RESOLVED"
    gs = _gql("""{ geneSet(name: "SYNTH_SET", collection: "cfde-inc-v2") { name dapperGeneSetId } }""")["data"]["geneSet"]
    assert gs["dapperGeneSetId"] == "dapper:GeneSet.SYNTHSET01"
    obj = _gql("""{ dapperObject(id: "dapper:GeneSet.SYNTHSET01") }""")["data"]["dapperObject"]
    assert obj["class_name"] == "GeneSet"
    assert obj["members"] == ["NOD2"]
    rest = client.get("/v1/objects/dapper:GeneSet.SYNTHSET01")
    assert rest.status_code == 200
    assert rest.json()["name"] == "SYNTH_SET"


def _second_opinion_bundle():
    """A second claim assessing the SAME proposition (SYNTHS01), deposited
    WITHOUT an index hint (e.g. an interpretive tier claim)."""
    return {"objects": [{
        "class_name": "Claim", "id": "dapper:Claim.SYNTHS01B",
        "proposition": "dapper:Proposition.SYNTHS01",
        "statement": "A second synthetic assessment of SYNTH_SET's association with IBD.",
    }]}


def test_object_list_and_search():
    """REST output is objects only: general list and search, not tied to a
    class — `references` gives the claim fan-in of a proposition (or any
    object's in-links), `q` matches text, `match` matches string slots."""
    client.post("/v1/claims", json=_bundle())
    client.post("/v1/claims", json=_membership_bundle())
    client.post("/v1/claims", json=_second_set_bundle())
    assert client.post("/v1/claims", json=_second_opinion_bundle()).status_code == 200

    claims = client.get("/v1/objects", params={"class_name": "Claim", "limit": 500}).json()
    assert claims["total"] >= 4
    assert all(o["class_name"] == "Claim" for o in claims["objects"])
    assert "dapper:Claim.SYNTHG01" in {o["id"] for o in claims["objects"]}
    every = client.get("/v1/objects", params={"limit": 3}).json()
    assert every["total"] > claims["total"]  # all classes

    # Fan-in: every claim assessing a proposition, hinted or not.
    fanin = client.get("/v1/objects/search",
                       params={"class_name": "Claim", "references": "dapper:Proposition.SYNTHS01"}).json()
    assert {o["id"] for o in fanin["objects"]} == {"dapper:Claim.SYNTHS01", "dapper:Claim.SYNTHS01B"}
    # In-links of any object: what references the GeneSet entity.
    inlinks = client.get("/v1/objects/search", params={"references": "dapper:GeneSet.SYNTHSET01"}).json()
    assert {"dapper:Proposition.SYNTHS01", "dapper:Proposition.SYNTHM01"} <= {o["id"] for o in inlinks["objects"]}

    # Text search over statement / name / description.
    text = client.get("/v1/objects/search", params={"q": "member of the gene set"}).json()
    assert "dapper:Proposition.SYNTHM01" in {o["id"] for o in text["objects"]}
    # Exact slot match (repeatable, ANDed).
    nod2 = client.get("/v1/objects/search", params=[
        ("class_name", "Proposition"), ("match", "subject_entity:NOD2"), ("match", "relation:member_of"),
    ]).json()
    assert {o["id"] for o in nod2["objects"]} == {"dapper:Proposition.SYNTHM01", "dapper:Proposition.SYNTHM02"}
    assert client.get("/v1/objects/search", params={"match": "Bad Slot:x"}).status_code == 400

    # Same fan-in over GraphQL, with full envelopes.
    gql = _gql("""{ proposition(id: "dapper:Proposition.SYNTHS01") {
        triple { subject predicate object } claims { id } } }""")["data"]["proposition"]
    assert gql["triple"]["object"] == "IBD"
    assert {c["id"] for c in gql["claims"]} == {"dapper:Claim.SYNTHS01", "dapper:Claim.SYNTHS01B"}

    # Deterministic pagination with a stable total.
    page = client.get("/v1/objects", params={"class_name": "Claim", "limit": 1, "offset": 1}).json()
    assert page["total"] == claims["total"]
    assert page["objects"][0]["id"] == claims["objects"][1]["id"]


# ---------------------------------------------------------------------------
# Claims minted when queried — the real loader on fixtures, then GraphQL.

_LOADER = {}


def _loader():
    if "mod" not in _LOADER:
        spec = importlib.util.spec_from_file_location("load_pigean", ROOT / "scripts" / "load_pigean.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _LOADER["mod"] = mod
    return _LOADER["mod"]


def _load_dir(directory, path=None):
    con = service_db.connect(path)
    try:
        return _loader().load(
            con, Path(directory) / "combined_gene_stats.json",
            Path(directory) / "combined_gene_set_stats.json", Path(directory) / "gene_gene_set_stats.json",
        )
    finally:
        con.close()


def _variant(phenotype, collection, membership=None):
    """A copy of the fixtures under another (phenotype, collection) slice;
    `membership` optionally replaces the (gene, gene_set) pairs."""
    out = Path(tempfile.mkdtemp())
    for name in ("combined_gene_stats.json", "combined_gene_set_stats.json", "gene_gene_set_stats.json"):
        rows = [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line.strip()]
        if name == "gene_gene_set_stats.json" and membership is not None:
            rows = [{"gene": g, "gene_set": s, "source": "lincs", "trait_group": "portal"} for g, s in membership]
        for row in rows:
            row["phenotype"], row["gene_set_size"] = phenotype, collection
        (out / name).write_text("".join(json.dumps(r) + "\n" for r in rows))
    return out


_loaded = {}


def _load_fixtures():
    """The base fixture slice (FIXPHENO / fixture-v1), loaded once."""
    if not _loaded:
        _loaded["stats"] = _load_dir(FIXTURES)
    return _loaded["stats"]


def _index_claim_id(table, key_col, key, collection="fixture-v1"):
    con = service_db.connect()
    try:
        return con.execute(f"SELECT claim_id FROM {table} WHERE {key_col} = ? AND collection = ?",
                           (key, collection)).fetchone()["claim_id"]
    finally:
        con.close()


def _times_seen(object_id):
    """times_seen without asserting anything (REST reads never assert)."""
    resp = client.get(f"/v1/objects/{object_id}")
    if resp.status_code == 404:
        return 0
    con = service_db.connect()
    try:
        return con.execute("SELECT times_seen FROM dapper_objects WHERE id = ?", (object_id,)).fetchone()["times_seen"]
    finally:
        con.close()


def test_loader_stores_no_claims():
    # A fresh database: order-independent proof that loading stores no claims.
    path = os.path.join(tempfile.mkdtemp(), "loader-only.db")
    stats = _load_dir(FIXTURES, path)
    assert (stats["gene_claims"], stats["gene_set_claims"], stats["roster_claims"]) == (3, 2, 2)
    assert stats["membership_pairs"] == 3
    con = service_db.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) AS c FROM dapper_objects WHERE class_name = 'Claim'").fetchone()["c"] == 0
        assert con.execute("SELECT COUNT(*) AS c FROM pigean_gene_associations WHERE synthesis_inputs IS NULL").fetchone()["c"] == 0
    finally:
        con.close()
    # Through the service, on a slice no other test serves: the data implies
    # FIXG3's claim, nothing asserted it.
    _load_dir(_variant("NOCLAIM", "noclaim-v1"))
    fixg3 = _index_claim_id("pigean_gene_associations", "gene", "FIXG3", "noclaim-v1")
    missing = client.get(f"/v1/objects/{fixg3}")
    assert missing.status_code == 404
    assert "not yet asserted" in missing.json()["detail"]
    props = client.get("/v1/objects/search", params=[
        ("class_name", "Proposition"), ("match", "subject_entity:FIXG3"), ("q", "NOCLAIM")]).json()
    assert props["total"] == 1  # the data plane is there


def test_claims_are_synthesized_when_served():
    _load_fixtures()
    fixg1 = _index_claim_id("pigean_gene_associations", "gene", "FIXG1")
    fixg2 = _index_claim_id("pigean_gene_associations", "gene", "FIXG2")
    q = """{ phenotype(id: "FIXPHENO") {
        pigeanGeneAssociations(collection: "fixture-v1", where: {prior: {lt: 0.5}}) {
          gene { symbol } prior
          claim { id timesDeposited scores { metric value } evidence { valueLocators } } } } }"""
    before = {fixg1: _times_seen(fixg1), fixg2: _times_seen(fixg2)}
    body = _gql(q)
    assert "errors" not in body, body.get("errors")
    assocs = {a["gene"]["symbol"]: a for a in body["data"]["phenotype"]["pigeanGeneAssociations"]}
    assert set(assocs) == {"FIXG1", "FIXG2"}
    assert assocs["FIXG1"]["claim"]["id"] == fixg1
    assert assocs["FIXG2"]["claim"]["id"] == fixg2
    assert assocs["FIXG1"]["claim"]["timesDeposited"] == before[fixg1] + 1
    # FIXG2's file values are JSON ints ("prior": 0, "combined": 2): the score
    # is normalized to float, the locator keeps the file's exact bytes.
    g2 = assocs["FIXG2"]["claim"]
    assert {s["metric"]: s["value"] for s in g2["scores"]}["prior"] == 0.0
    locs = [loc for e in g2["evidence"] for loc in e["valueLocators"]]
    prior_loc = next(loc for loc in locs if loc["selector"].endswith("key=prior"))
    assert prior_loc["value_text"] == "0"
    data = (FIXTURES / "combined_gene_stats.json").read_bytes()
    for loc in locs:
        assert data[loc["byte_start"]:loc["byte_start"] + loc["byte_length"]].decode() == loc["value_text"]
    # Every further serve is an assertion; stored claims are found by reference.
    again = _gql(q)["data"]["phenotype"]["pigeanGeneAssociations"]
    assert {a["gene"]["symbol"]: a["claim"]["timesDeposited"] for a in again}["FIXG1"] == before[fixg1] + 2
    prop = client.get(f"/v1/objects/{fixg2}").json()["proposition"]
    fanin = client.get("/v1/objects/search", params={"class_name": "Claim", "references": prop}).json()
    assert [o["id"] for o in fanin["objects"]] == [fixg2]


def test_gene_set_associations_synthesize_with_int_beta():
    _load_fixtures()
    body = _gql("""{ phenotype(id: "FIXPHENO") { pigeanGeneSetAssociations(collection: "fixture-v1") {
        geneSet { name dapperGeneSetId } beta claim { id scores { metric value } } } } }""")
    assert "errors" not in body, body.get("errors")
    sets = {s["geneSet"]["name"]: s for s in body["data"]["phenotype"]["pigeanGeneSetAssociations"]}
    assert sets["FIXSET_A"]["beta"] == 0.0  # "beta": 0 in the file
    assert sets["FIXSET_A"]["claim"]["id"] == _index_claim_id("pigean_gene_set_associations", "gene_set", "FIXSET_A")
    assert sets["FIXSET_A"]["geneSet"]["dapperGeneSetId"].startswith("dapper:GeneSet.")


def test_roster_claims_members_and_lookup_by_id():
    _load_fixtures()
    # One roster claim per gene set backs every (gene, set) pair in it.
    g1 = _gql("""{ gene(symbol: "FIXG1") { memberships(collection: "fixture-v1") {
        geneSet { name } claim { id triple { predicate } } } } }""")
    assert "errors" not in g1, g1.get("errors")
    g1_sets = {m["geneSet"]["name"]: m["claim"] for m in g1["data"]["gene"]["memberships"]}
    assert set(g1_sets) == {"FIXSET_A", "FIXSET_B"}
    assert {c["triple"]["predicate"] for c in g1_sets.values()} == {"has_members"}
    members = _gql("""{ geneSet(name: "FIXSET_A", collection: "fixture-v1") {
        members { gene { symbol } claim { id } } } }""")
    assert "errors" not in members, members.get("errors")
    rows = members["data"]["geneSet"]["members"]
    assert sorted(r["gene"]["symbol"] for r in rows) == ["FIXG1", "FIXG2"]
    assert {r["claim"]["id"] for r in rows} == {g1_sets["FIXSET_A"]["id"]}

    # claim(id) on never-served claims finds the index row and synthesizes:
    # a gene-set association, and a roster (looked up in the rosters table).
    _load_dir(_variant("BYID", "byid-v1"))
    set_b = _index_claim_id("pigean_gene_set_associations", "gene_set", "FIXSET_B", "byid-v1")
    assert _times_seen(set_b) == 0  # never served in this slice
    env = _gql(f'{{ claim(id: "{set_b}") {{ id timesDeposited }} }}')
    assert "errors" not in env, env.get("errors")
    assert env["data"]["claim"] == {"id": set_b, "timesDeposited": 1}
    roster_b = g1_sets["FIXSET_B"]["id"]
    env = _gql(f'{{ claim(id: "{roster_b}") {{ id triple {{ predicate }} }} }}')
    assert env["data"]["claim"]["triple"]["predicate"] == "has_members"


def test_challenge_dedupes_assertions_within_a_request():
    """A roster / association claim served by several rows of ONE response is
    asserted once."""
    _load_fixtures()
    roster_a = _index_claim_id("claim_memberships", "gene", "FIXG2")  # FIXG2 is only in FIXSET_A
    set_a = _index_claim_id("pigean_gene_set_associations", "gene_set", "FIXSET_A")
    before = (_times_seen(roster_a), _times_seen(set_a))
    body = _gql("""{ pigeanValidationChallenge(phenotype: "FIXPHENO", collection: "fixture-v1",
        where: {prior: {lt: 10}}, topGenes: 5, setsPerGene: 5) {
          rows { geneAssociation { gene { symbol } } sets { membership { claim { id } } geneSetAssociation { geneSet { name } } } } } }""")
    assert "errors" not in body, body.get("errors")
    served = [s["geneSetAssociation"]["geneSet"]["name"]
              for r in body["data"]["pigeanValidationChallenge"]["rows"] for s in r["sets"]]
    assert served.count("FIXSET_A") == 2  # FIXG1 and FIXG2 both link to it
    assert (_times_seen(roster_a), _times_seen(set_a)) == (before[0] + 1, before[1] + 1)


def test_second_load_keeps_earlier_claims_servable():
    """Loads of two phenotypes into one collection, with different observed
    rosters: the first phenotype's claims still synthesize exactly."""
    _load_dir(_variant("TWOA", "two-v1"))
    _load_dir(_variant("TWOB", "two-v1", membership=[("FIXG1", "FIXSET_A"), ("FIXG3", "FIXSET_A"),
                                                     ("FIXG1", "FIXSET_B")]))
    body = _gql("""{ phenotype(id: "TWOA") { pigeanGeneSetAssociations(collection: "two-v1") {
        geneSet { name } claim { id } } } }""")
    assert "errors" not in body, body.get("errors")
    served = {s["geneSet"]["name"]: s["claim"]["id"] for s in body["data"]["phenotype"]["pigeanGeneSetAssociations"]}
    con = service_db.connect()
    try:
        expected = {r["gene_set"]: r["claim_id"] for r in con.execute(
            "SELECT gene_set, claim_id FROM pigean_gene_set_associations WHERE phenotype = 'TWOA' AND collection = 'two-v1'")}
    finally:
        con.close()
    assert served == expected
    # TWOA's observed membership (FIXG2 in FIXSET_A) still resolves for TWOA.
    challenge = _gql("""{ pigeanValidationChallenge(phenotype: "TWOA", collection: "two-v1",
        where: {prior: {lt: 10}}, topGenes: 5, setsPerGene: 5) {
          rows { geneAssociation { gene { symbol } } sets { membership { claim { id } } } } } }""")
    assert "errors" not in challenge, challenge.get("errors")
    rows = {r["geneAssociation"]["gene"]["symbol"]: r for r in challenge["data"]["pigeanValidationChallenge"]["rows"]}
    assert len(rows["FIXG2"]["sets"]) == 1 and len(rows["FIXG3"]["sets"]) == 0


def test_tampered_row_is_refused_even_after_mint():
    _load_dir(_variant("TAMPER", "tamper-v1"))
    q = """{ gene(symbol: "FIXG1") { pigeanAssociation(phenotype: "TAMPER", collection: "tamper-v1") {
        combined claim { id timesDeposited } } } }"""
    first = _gql(q)
    assert "errors" not in first, first.get("errors")
    claim_id = first["data"]["gene"]["pigeanAssociation"]["claim"]["id"]
    count = _times_seen(claim_id)
    con = service_db.connect()
    with con:
        con.execute("UPDATE pigean_gene_associations SET combined = 99.0 WHERE phenotype = 'TAMPER' AND gene = 'FIXG1'")
    con.close()
    body = _gql(q)
    assert body.get("errors") and "template drift" in body["errors"][0]["message"]
    assert _times_seen(claim_id) == count  # the failed request counted nothing


def test_failed_response_rolls_back_every_mint():
    _load_dir(_variant("ROLLBACK", "rb-v1"))
    con = service_db.connect()
    with con:
        ids = {r["gene"]: r["claim_id"] for r in con.execute(
            "SELECT gene, claim_id FROM pigean_gene_associations WHERE phenotype = 'ROLLBACK'")}
        con.execute("UPDATE pigean_gene_associations SET claim_id = 'dapper:Claim.localdeadbeef' "
                    "WHERE phenotype = 'ROLLBACK' AND gene = 'FIXG2'")
    con.close()
    # Ordered by combined: FIXG3 (4.75), FIXG1 (3.5) synthesize, then FIXG2 fails.
    body = _gql("""{ phenotype(id: "ROLLBACK") { pigeanGeneAssociations(collection: "rb-v1",
        orderBy: {field: COMBINED, dir: DESC}) { claim { id } } } }""")
    assert body.get("errors")
    for gene in ("FIXG3", "FIXG1"):
        assert client.get(f"/v1/objects/{ids[gene]}").status_code == 404, gene


def test_forged_deposit_at_a_reserved_address_is_refused():
    _load_dir(_variant("FORGE", "forge-v1"))
    con = service_db.connect()
    row = con.execute("SELECT * FROM pigean_gene_associations WHERE phenotype = 'FORGE' AND gene = 'FIXG1'").fetchone()
    con.close()
    from service.app import synthesize_claim
    genuine = synthesize_claim("gene", row)
    forged = {**genuine, "statement": "FORGED"}
    resp = client.post("/v1/claims", json={"objects": [forged]})
    # Refused by local-dev address verification (the forged content cannot
    # hash to the genuine address); the reserved-address check backs this up
    # for identity profiles the service cannot verify.
    assert resp.status_code == 400 and "does not hash" in resp.json()["detail"]
    # The genuine document is accepted (asserted by its depositor).
    assert client.post("/v1/claims", json={"objects": [genuine]}).status_code == 200


def test_proposition_fanin_includes_implied_claims():
    _load_dir(_variant("FANIN", "fanin-v1"))
    con = service_db.connect()
    row = con.execute("SELECT claim_id, proposition_id FROM pigean_gene_associations WHERE phenotype = 'FANIN' AND gene = 'FIXG1'").fetchone()
    con.close()
    assert _times_seen(row["claim_id"]) == 0
    body = _gql(f'{{ proposition(id: "{row["proposition_id"]}") {{ claims {{ id timesDeposited }} }} }}')
    assert "errors" not in body, body.get("errors")
    assert body["data"]["proposition"]["claims"] == [{"id": row["claim_id"], "timesDeposited": 1}]


def test_claim_query_on_a_non_claim_returns_null_and_counts_nothing():
    client.post("/v1/claims", json=_bundle())
    before = _times_seen("dapper:Proposition.SYNTHG01")
    body = _gql("""{ claim(id: "dapper:Proposition.SYNTHG01") { id } }""")
    assert body["data"]["claim"] is None
    assert _times_seen("dapper:Proposition.SYNTHG01") == before


def test_graphql_argument_bounds():
    _load_fixtures()  # the phenotype must exist for its child resolvers to run
    for q in ('{ phenotype(id: "FIXPHENO") { pigeanGeneAssociations(collection: "fixture-v1", first: -1) { prior } } }',
              '{ phenotype(id: "FIXPHENO") { pigeanGeneAssociations(collection: "fixture-v1", first: 501) { prior } } }',
              '{ pigeanValidationChallenge(phenotype: "FIXPHENO", collection: "fixture-v1", topGenes: 201) { truncated } }',
              '{ pigeanValidationChallenge(phenotype: "FIXPHENO", collection: "fixture-v1", setsPerGene: 51) { truncated } }'):
        body = _gql(q)
        assert body.get("errors") and "must be between" in body["errors"][0]["message"], q


def test_object_search_is_literal_case_insensitive_and_validated():
    _load_fixtures()
    lower = client.get("/v1/objects/search", params={
        "class_name": "Proposition", "q": "the gene fixg1 is associated with fixpheno."}).json()
    assert lower["total"] == 1  # case-insensitive on every backend
    assert client.get("/v1/objects/search", params={"q": "F_XG1"}).json()["total"] == 0  # '_' is literal
    assert client.get("/v1/objects/search", params={"q": "%"}).json()["total"] == 0      # '%' is literal
    assert client.get("/v1/objects/search", params={"references": "not-an-id"}).status_code == 400
    assert client.get("/v1/objects/search", params={"class_name": "Claim;--"}).status_code == 400
    assert client.get("/v1/objects", params={"offset": 10**12}).status_code == 422
    assert client.get("/v1/objects", params={"limit": 0}).status_code == 422


def test_deposit_rejects_malformed_input():
    assert client.post("/v1/claims", content=b"{not json", headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post("/v1/claims", json=[1, 2]).status_code == 400
    assert client.post("/v1/claims", json={"objects": "x"}).status_code == 400
    assert client.post("/v1/claims", json={"objects": [1]}).status_code == 400
    for bad_id in ('dapper:Claim.x"><script>alert(1)</script>', "dapper:Proposition.WRONGCLASS", "dapper:Claim." + "a" * 200):
        resp = client.post("/v1/claims", json={"objects": [{"class_name": "Claim", "id": bad_id}]})
        assert resp.status_code == 400, bad_id
    long_ref = {"class_name": "Claim", "id": "dapper:Claim.LONGREF", "proposition": "dapper:Proposition." + "b" * 200}
    assert client.post("/v1/claims", json={"objects": [long_ref]}).status_code == 400


def test_deposit_hint_does_not_break_loaded_rosters():
    _load_fixtures()
    con = service_db.connect()
    entity_before = con.execute("SELECT dapper_id, n_genes FROM gene_set_entities WHERE collection = 'fixture-v1' AND name = 'FIXSET_B'").fetchone()
    con.close()
    bundle = {
        "objects": [
            {"class_name": "Proposition", "id": "dapper:Proposition.HINTP01", "statement": "FIXG9 is in FIXSET_B.",
             "subject_entity": "FIXG9", "relation": "member_of", "object_entity": "FIXSET_B", "proposition_kind": "RESULT"},
            {"class_name": "Claim", "id": "dapper:Claim.HINTM01", "proposition": "dapper:Proposition.HINTP01"},
        ],
        "index_hints": [{"claim_id": "dapper:Claim.HINTM01", "method": "pigean", "predicate": "member_of",
                         "subject": {"category": "gene", "id": "FIXG9"},
                         "object": {"category": "gene_set", "id": "FIXSET_B", "dapper_id": "dapper:GeneSet.HINTSET"},
                         "collection": "fixture-v1"}],
    }
    assert client.post("/v1/claims", json=bundle).status_code == 200
    con = service_db.connect()
    entity_after = con.execute("SELECT dapper_id, n_genes FROM gene_set_entities WHERE collection = 'fixture-v1' AND name = 'FIXSET_B'").fetchone()
    con.close()
    assert tuple(entity_after) == tuple(entity_before)  # the hint did not clobber the loaded entity
    body = _gql("""{ geneSet(name: "FIXSET_B", collection: "fixture-v1") { members { gene { symbol } claim { id } } } }""")
    assert "errors" not in body, body.get("errors")
    assert sorted(m["gene"]["symbol"] for m in body["data"]["geneSet"]["members"]) == ["FIXG1", "FIXG9"]


def test_template_number_canonicalization():
    assert tpl.num(-0.0) == 0.0 and str(tpl.num(-0.0)) == "0.0"
    assert tpl.score("beta", -0.0)["id"] == tpl.score("beta", 0)["id"] == tpl.score("beta", 0.0)["id"]
    for bad in (float("nan"), float("inf")):
        try:
            tpl.num(bad)
        except ValueError:
            continue
        raise AssertionError(f"num({bad}) should be refused")


def test_audit_passes_on_a_clean_store_and_fails_on_damage():
    path = os.path.join(tempfile.mkdtemp(), "audit.db")
    _load_dir(FIXTURES, path)
    env = {**os.environ, "CLAIMS_DB": path}
    audit = [sys.executable, str(ROOT / "scripts" / "audit_objects.py")]
    ok = subprocess.run(audit, env=env, capture_output=True, text=True, cwd=ROOT)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    con = service_db.connect(path)
    with con:
        con.execute("DELETE FROM object_refs WHERE rowid IN (SELECT rowid FROM object_refs LIMIT 1)")
    con.close()
    damaged = subprocess.run(audit, env=env, capture_output=True, text=True, cwd=ROOT)
    assert damaged.returncode == 1 and "object_refs missing" in damaged.stdout


def test_template_drift_is_refused():
    """A claim whose synthesis no longer matches its index row's address is
    never served or stored."""
    _load_dir(_variant("DRIFT", "drift-v1"))
    fixg3 = _index_claim_id("pigean_gene_associations", "gene", "FIXG3", "drift-v1")
    con = service_db.connect()
    with con:
        con.execute("UPDATE pigean_gene_associations SET combined = 99.0 WHERE gene = 'FIXG3' AND collection = 'drift-v1'")
    con.close()
    body = _gql("""{ gene(symbol: "FIXG3") {
        pigeanAssociation(phenotype: "DRIFT", collection: "drift-v1") { claim { id } } } }""")
    assert body.get("errors"), body
    assert body["data"] is None  # a failed response writes nothing and reports no data
    assert "template drift" in body["errors"][0]["message"]
    resp = client.get(f"/v1/objects/{fixg3}")
    assert resp.status_code == 404 and "not yet asserted" in resp.json()["detail"]


def test_set_pigean_association_and_never_served_gene_claim_by_id():
    _load_dir(_variant("SETASSOC", "setassoc-v1"))
    body = _gql("""{ geneSet(name: "FIXSET_A", collection: "setassoc-v1") {
        pigeanAssociation(phenotype: "SETASSOC", collection: "setassoc-v1") { beta claim { id } } } }""")
    assert "errors" not in body, body.get("errors")
    assoc = body["data"]["geneSet"]["pigeanAssociation"]
    assert assoc["beta"] == 0.0
    assert assoc["claim"]["id"] == _index_claim_id("pigean_gene_set_associations", "gene_set", "FIXSET_A", "setassoc-v1")
    gene = _index_claim_id("pigean_gene_associations", "gene", "FIXG1", "setassoc-v1")
    assert _times_seen(gene) == 0
    env = _gql(f'{{ claim(id: "{gene}") {{ id timesDeposited triple {{ predicate }} }} }}')
    assert env["data"]["claim"] == {"id": gene, "timesDeposited": 1, "triple": {"predicate": "associated_with"}}
    # A never-served ROSTER claim by id (rosters looked up in their own table).
    con = service_db.connect()
    roster = con.execute("SELECT claim_id FROM pigean_gene_set_rosters WHERE collection = 'setassoc-v1' "
                         "AND gene_set = 'FIXSET_B'").fetchone()["claim_id"]
    con.close()
    assert _times_seen(roster) == 0
    env = _gql(f'{{ claim(id: "{roster}") {{ id triple {{ predicate }} }} }}')
    assert env["data"]["claim"]["triple"]["predicate"] == "has_members"


def _load_traits():
    spec = importlib.util.spec_from_file_location("load_kpn_traits", ROOT / "scripts" / "load_kpn_traits.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    path = mod.release_path(ROOT / "tests" / "fixtures" / "kpn-data-models", "v0.0.2")
    con = service_db.connect()
    try:
        return mod.load(con, path, "v0.0.2", None)
    finally:
        con.close()


def test_kpn_trait_ids_name_the_phenotype():
    """Legacy / PIGEAN phenotype keys resolve to canonical KPN.TRAIT IDs: claims
    name the trait ID, and every key finds the phenotype."""
    stats = _load_traits()
    assert (stats["traits"], stats["mappings"]) == (2, 3)
    _load_dir(_variant("REGPHENO", "reg-v1"))
    con = service_db.connect()
    stored = {r["phenotype"] for r in con.execute(
        "SELECT DISTINCT phenotype FROM pigean_gene_associations WHERE collection = 'reg-v1'")}
    con.close()
    assert stored == {"KPN.TRAIT:9000001"}  # index rows carry the canonical id
    q = """query($id: ID!) { phenotype(id: $id) {
        id legacyId name category traitType conceptId conceptLabel mappings { targetId ontology predicate }
        efo: mappings(ontology: "EFO") { targetId }
        pigeanGeneAssociations(collection: "reg-v1", first: 1, orderBy: {field: COMBINED, dir: DESC}) {
          gene { symbol } claim { triple { object } proposition } } } }"""
    for key in ("REGPHENO", "KPN.TRAIT:9000001"):
        body = _gql(q, {"id": key})
        assert "errors" not in body, body.get("errors")
        ph = body["data"]["phenotype"]
        assert ph["id"] == "KPN.TRAIT:9000001" and ph["legacyId"] == "REGPHENO"
        assert ph["conceptId"] == "EFO:9999991" and ph["category"] == "digestive"
        assert {m["targetId"] for m in ph["mappings"]} == {"EFO:9999991", "MONDO:9999992"}
        assert [m["targetId"] for m in ph["efo"]] == ["EFO:9999991"]
        assoc = ph["pigeanGeneAssociations"][0]
        assert assoc["claim"]["triple"]["object"] == "KPN.TRAIT:9000001"
        assert "Registered fixture phenotype" in assoc["claim"]["proposition"]["statement"]
    # The challenge accepts the legacy key too.
    ch = _gql("""{ pigeanValidationChallenge(phenotype: "REGPHENO", collection: "reg-v1",
        where: {prior: {lt: 10}}, topGenes: 5) { rows { sets { membership { claim { id } } } } } }""")
    assert "errors" not in ch, ch.get("errors")
    assert sum(len(r["sets"]) for r in ch["data"]["pigeanValidationChallenge"]["rows"]) == 3
    # A PIGEAN key resolves via pigean_id; the vocabulary lists the release.
    _load_dir(_variant("PIGKEY", "pig-v1"))
    ph = _gql('{ phenotype(id: "PIGKEY") { id legacyId conceptId } }')["data"]["phenotype"]
    assert ph == {"id": "KPN.TRAIT:9000002", "legacyId": "OLDKEY", "conceptId": "ORPHANET:9999993"}
    vocab = client.get("/v1/vocabulary").json()
    assert vocab["trait_registry"]["version"] == "v0.0.2"
    assert vocab["identifier_resolution"]["prefixes"]["KPN.TRAIT"].startswith("https://")


def test_local_dev_addresses_are_verified_on_deposit():
    genuine = tpl.score("combined", 3.5)
    forged = {**genuine, "value": 99.0}  # same id, different content
    resp = client.post("/v1/claims", json={"objects": [forged]})
    assert resp.status_code == 400 and "does not hash" in resp.json()["detail"]
    assert client.post("/v1/claims", json={"objects": [genuine]}).status_code == 200
    # A re-deposit at an existing (non-verifiable) id must carry the same document.
    client.post("/v1/claims", json=_bundle())
    altered = [dict(o) for o in _bundle()["objects"]]
    altered[0]["statement"] = "ALTERED"
    resp = client.post("/v1/claims", json={"objects": altered[:1]})
    assert resp.status_code == 400 and "different document" in resp.json()["detail"]


def test_deposit_hints_cannot_overwrite_loaded_rows():
    _load_dir(_variant("HINT", "hint-v1"))
    claim = {"class_name": "Claim", "id": "dapper:Claim.HINTOVERWRITE", "proposition": "dapper:Proposition.SYNTHG01",
             "has_score": ["dapper:ClaimScore.SYNTHG01combined", "dapper:ClaimScore.SYNTHG01logbf",
                           "dapper:ClaimScore.SYNTHG01prior"]}
    client.post("/v1/claims", json=_bundle())
    hint = {"claim_id": claim["id"], "method": "pigean", "predicate": "associated_with",
            "subject": {"category": "gene", "id": "FIXG1"}, "object": {"category": "phenotype", "id": "HINT"},
            "collection": "hint-v1"}
    resp = client.post("/v1/claims", json={"objects": [claim], "index_hints": [hint]})
    assert resp.status_code == 400 and "template-backed" in resp.json()["detail"]
    # A hint naming a non-Claim is refused (it would break every serve).
    bad = {**hint, "claim_id": "dapper:Proposition.SYNTHG01", "subject": {"category": "gene", "id": "FIXG9"}}
    resp = client.post("/v1/claims", json={"objects": [claim], "index_hints": [bad]})
    assert resp.status_code == 400 and "not a Claim" in resp.json()["detail"]
    for malformed in ({**hint, "claim_id": ["x"]}, {**hint, "object": {}}, {**hint, "collection": 5}):
        assert client.post("/v1/claims", json={"objects": [claim], "index_hints": [malformed]}).status_code == 400


def test_dapper_id_1_identifiers_are_accepted():
    pid = "dapper:Proposition.2Rz_JqeQJ1Rsr1N7khXSppi7fEtf9lda"  # base64url digest with '_'
    cid = "dapper:Claim.x-Y_z0Abc123"
    bundle = {"objects": [
        {"class_name": "Proposition", "id": pid, "statement": "s", "subject_entity": "A", "relation": "r",
         "object_entity": "B"},
        {"class_name": "Claim", "id": cid, "proposition": pid},
    ]}
    assert client.post("/v1/claims", json=bundle).status_code == 200
    found = client.get("/v1/objects/search", params={"references": pid}).json()
    assert [o["id"] for o in found["objects"]] == [cid]
    page = client.get(f"/v1/objects/{cid}", headers={"Accept": "text/html"}).text
    assert f'href="/v1/objects/{pid}"' in page


def test_search_matches_string_slots_only():
    bundle = {"objects": [{"class_name": "Agent", "id": "dapper:Agent.NULLDESC", "description": None},
                          {"class_name": "Agent", "id": "dapper:Agent.NUMDESC", "description": 7}]}
    assert client.post("/v1/claims", json=bundle).status_code == 200
    assert client.get("/v1/objects/search", params={"q": "null", "class_name": "Agent"}).json()["total"] == 0
    assert client.get("/v1/objects/search", params={"match": "description:7"}).json()["total"] == 0


def test_assertion_budget_bounds_a_request():
    import service.app as svc
    _load_dir(_variant("BUDGET", "budget-v1"))
    original = svc.MAX_ASSERTIONS_PER_REQUEST
    svc.MAX_ASSERTIONS_PER_REQUEST = 2
    try:
        body = _gql("""{ phenotype(id: "BUDGET") { pigeanGeneAssociations(collection: "budget-v1") { claim { id } } } }""")
    finally:
        svc.MAX_ASSERTIONS_PER_REQUEST = original
    assert body.get("errors") and "more than 2 claims" in body["errors"][0]["message"]
    con = service_db.connect()
    ids = [r["claim_id"] for r in con.execute("SELECT claim_id FROM pigean_gene_associations WHERE collection = 'budget-v1'")]
    con.close()
    assert all(_times_seen(i) == 0 for i in ids)  # nothing written


import contextlib  # noqa: E402


@contextlib.contextmanager
def _isolated_db():
    """Point the service at a fresh database for one scenario."""
    original = service_db.DB_PATH
    service_db.DB_PATH = os.path.join(tempfile.mkdtemp(), "isolated.db")
    try:
        yield service_db.DB_PATH
    finally:
        service_db.DB_PATH = original


def _rows(sql, params=()):
    con = service_db.connect()
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def test_results_loaded_before_the_registry_never_duplicate():
    """Rows loaded under the legacy key, then the registry, then a reload:
    one canonical copy, a working challenge, and no deposit can shadow the
    legacy-keyed result in between."""
    with _isolated_db():
        _load_dir(_variant("REGPHENO", "late-v1"))
        _load_traits()
        assert {r["phenotype"] for r in _rows(
            "SELECT DISTINCT phenotype FROM pigean_gene_associations WHERE collection = 'late-v1'")} == {"REGPHENO"}
        client.post("/v1/claims", json=_bundle())
        hint = {"claim_id": "dapper:Claim.SYNTHG01", "method": "pigean", "predicate": "associated_with",
                "subject": {"category": "gene", "id": "FIXG1"},
                "object": {"category": "phenotype", "id": "KPN.TRAIT:9000001"}, "collection": "late-v1"}
        resp = client.post("/v1/claims", json={"objects": _bundle()["objects"], "index_hints": [hint]})
        assert resp.status_code == 400 and "template-backed" in resp.json()["detail"]
        _load_dir(_variant("REGPHENO", "late-v1"))
        for table in ("pigean_gene_associations", "pigean_gene_set_associations", "claim_memberships"):
            keys = {r["phenotype"] for r in _rows(f"SELECT DISTINCT phenotype FROM {table} WHERE collection = 'late-v1'")}
            assert keys == {"KPN.TRAIT:9000001"}, (table, keys)
        body = _gql("""{ pigeanValidationChallenge(phenotype: "REGPHENO", collection: "late-v1",
            where: {prior: {lt: 10}}, topGenes: 5) { rows { geneAssociation { gene { symbol } } } } }""")
        assert "errors" not in body, body.get("errors")
        genes = [r["geneAssociation"]["gene"]["symbol"] for r in body["data"]["pigeanValidationChallenge"]["rows"]]
        assert sorted(genes) == ["FIXG1", "FIXG2", "FIXG3"]


def test_superseded_rosters_are_no_longer_implied():
    with _isolated_db():
        _load_dir(_variant("RR", "rr-v1"))
        old = {r["gene_set"]: r["claim_id"] for r in _rows("SELECT gene_set, claim_id FROM pigean_gene_set_rosters")}
        _load_dir(_variant("RR", "rr-v1", membership=[("FIXG1", "FIXSET_A"), ("FIXG3", "FIXSET_A")]))
        env = _gql(f'{{ claim(id: "{old["FIXSET_A"]}") {{ id }} }}')
        assert env["data"]["claim"] is None  # superseded: no edge cites it any more
        resp = client.get(f"/v1/objects/{old['FIXSET_A']}")
        assert resp.status_code == 404 and "not yet asserted" not in resp.json()["detail"]


def test_gene_set_root_pins_one_phenotypes_observation():
    with _isolated_db():
        _load_dir(_variant("P1", "pin-v1"))
        _load_dir(_variant("P2", "pin-v1", membership=[("FIXG1", "FIXSET_A"), ("FIXG3", "FIXSET_A"), ("FIXG1", "FIXSET_B")]))
        for phenotype, genes in (("P1", ["FIXG1", "FIXG2"]), ("P2", ["FIXG1", "FIXG3"])):
            body = _gql(f"""{{ geneSet(name: "FIXSET_A", collection: "pin-v1", phenotype: "{phenotype}") {{
                dapperGeneSetId members {{ gene {{ symbol }} geneSet {{ dapperGeneSetId }} claim {{ triple {{ subject }} }} }} }} }}""")
            assert "errors" not in body, body.get("errors")
            gs = body["data"]["geneSet"]
            assert sorted(m["gene"]["symbol"] for m in gs["members"]) == genes
            for m in gs["members"]:  # parent, member, and roster claim name one GeneSet
                assert m["geneSet"]["dapperGeneSetId"] == gs["dapperGeneSetId"] == m["claim"]["triple"]["subject"]


def test_trait_release_guards():
    spec = importlib.util.spec_from_file_location("load_kpn_traits", ROOT / "scripts" / "load_kpn_traits.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    release = (ROOT / "tests" / "fixtures" / "kpn-data-models" / "versions" / "trait" / "v0.0.2" / "kpn_trait_collection.yaml").read_text()
    ambiguous = Path(tempfile.mkdtemp()) / "ambiguous.yaml"
    ambiguous.write_text(release.replace("pigean_id: PIGKEY", "pigean_id: REGPHENO"))
    try:
        mod.parse_release(ambiguous)
    except ValueError as exc:
        assert "ambiguous" in str(exc)
    else:
        raise AssertionError("a key naming two traits must be refused")
    with _isolated_db():
        _load_traits()
        _load_dir(_variant("REGPHENO", "orph-v1"))
        lacking = Path(tempfile.mkdtemp()) / "lacking.yaml"
        lacking.write_text(release.replace("KPN.TRAIT:9000001", "KPN.TRAIT:9000009"))
        con = service_db.connect()
        try:
            try:
                mod.load(con, lacking, "v9", None)
            except SystemExit as exc:
                assert "orphan" in str(exc)
            else:
                raise AssertionError("replacing the registry must not orphan loaded results")
        finally:
            con.close()


def test_deposit_rejects_non_finite_ambiguous_and_malformed_input():
    raw = b'{"objects":[{"class_name":"Agent","id":"dapper:Agent.NAN1","value":NaN}]}'
    assert client.post("/v1/claims", content=raw, headers={"Content-Type": "application/json"}).status_code == 400
    raw = b'{"objects":[{"class_name":"Agent","id":"dapper:Agent.BIG1","value":1e400}]}'
    assert client.post("/v1/claims", content=raw, headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post("/v1/claims", json={"objects": [{"class_name": "Agent", "id": "dapper:Agent.NL\n"}]}).status_code == 400
    assert client.post("/v1/claims", json={"objects": [{"class_name": "Agent", "id": "dapper:Agent.FLAG", "flag": 1}]}).status_code == 200
    again = client.post("/v1/claims", json={"objects": [{"class_name": "Agent", "id": "dapper:Agent.FLAG", "flag": True}]})
    assert again.status_code == 400 and "different document" in again.json()["detail"]
    # A hinted claim citing a malformed score is refused (it would break every serve).
    bundle = {"objects": [{"class_name": "ClaimScore", "id": "dapper:ClaimScore.EMPTY"},
                          {"class_name": "Claim", "id": "dapper:Claim.BADSCORE", "proposition": "dapper:Proposition.SYNTHG01",
                           "has_score": ["dapper:ClaimScore.EMPTY"]}],
              "index_hints": [{"claim_id": "dapper:Claim.BADSCORE", "method": "pigean", "predicate": "member_of",
                               "subject": {"category": "gene", "id": "FIXG9"}, "object": {"category": "gene_set", "id": "FIXSET_Z"},
                               "collection": "malformed-v1"}]}
    resp = client.post("/v1/claims", json=bundle)
    assert resp.status_code == 400 and "well-formed ClaimScore" in resp.json()["detail"]
    bad_entity = {"claim_id": "dapper:Claim.SYNTHM01", "method": "pigean", "predicate": "member_of",
                  "subject": {"category": "gene", "id": "FIXG9"},
                  "object": {"category": "gene_set", "id": "FIXSET_Z", "dapper_id": "not-a-dapper-id"}, "collection": "x-v1"}
    client.post("/v1/claims", json=_membership_bundle())
    assert client.post("/v1/claims", json={"objects": _bundle()["objects"], "index_hints": [bad_entity]}).status_code == 400


def test_deposit_cannot_cite_a_loaded_roster_claim():
    with _isolated_db():
        _load_dir(_variant("ROSTERHINT", "rh-v1"))
        roster_b = _rows("SELECT claim_id FROM pigean_gene_set_rosters WHERE gene_set = 'FIXSET_B'")[0]["claim_id"]
        _gql(f'{{ claim(id: "{roster_b}") {{ id }} }}')  # mint it
        hint = {"claim_id": roster_b, "method": "pigean", "predicate": "member_of", "phenotype": "ROSTERHINT",
                "subject": {"category": "gene", "id": "FIXG3"}, "object": {"category": "gene_set", "id": "FIXSET_B"},
                "collection": "rh-v1"}
        resp = client.post("/v1/claims", json={"objects": [{"class_name": "Agent", "id": "dapper:Agent.RH"}], "index_hints": [hint]})
        assert resp.status_code == 400 and "roster" in resp.json()["detail"]


def test_query_budget_bounds_a_request():
    import service.app as svc
    _load_fixtures()
    original = svc.MAX_QUERIES_PER_REQUEST
    svc.MAX_QUERIES_PER_REQUEST = 5
    try:
        body = _gql("""{ phenotype(id: "FIXPHENO") { pigeanGeneAssociations(collection: "fixture-v1") { claim { id } } } }""")
    finally:
        svc.MAX_QUERIES_PER_REQUEST = original
    assert body["data"] is None and "database queries" in body["errors"][0]["message"]



def _gene_set_subject(collection, gene_set, phenotype=None):
    sql = "SELECT synthesis_inputs FROM pigean_gene_set_associations WHERE collection = ? AND gene_set = ?"
    params = (collection, gene_set)
    if phenotype is not None:
        sql += " AND phenotype = ?"
        params += (phenotype,)
    inputs = _rows(sql, params)[0]["synthesis_inputs"]
    return (inputs if isinstance(inputs, dict) else json.loads(inputs))["subject"]


def _set_claim_id(collection, gene_set):
    return _rows("SELECT claim_id FROM pigean_gene_set_associations WHERE collection = ? AND gene_set = ?",
                 (collection, gene_set))[0]["claim_id"]


def _load_without_membership(directory):
    con = service_db.connect()
    try:
        return _loader().load(con, Path(directory) / "combined_gene_stats.json",
                              Path(directory) / "combined_gene_set_stats.json", None)
    finally:
        con.close()


def _members_of(object_id):
    return client.get(f"/v1/objects/{object_id}").json().get("members")


def test_gene_set_subject_is_a_function_of_the_load_inputs():
    with _isolated_db():
        _load_dir(_variant("ML", "ml-v1"))
        member_ful = _gene_set_subject("ml-v1", "FIXSET_B")
        assert _members_of(member_ful) == ["FIXG1"]
        # A reload WITHOUT membership keeps the entity the live roster names,
        # and stores no unused member-less GeneSet.
        _load_without_membership(_variant("ML", "ml-v1"))
        assert _gene_set_subject("ml-v1", "FIXSET_B") == member_ful
        assert not [r for r in _rows("SELECT payload FROM dapper_objects WHERE class_name = 'GeneSet'")
                    if '"FIXSET_B"' in r["payload"] and "none observed" in r["payload"]]
        # A membership file is authoritative for its slice: a set it omits
        # names the member-less entity — and an identical reload is identical.
        omitting = _variant("ML", "ml-v1", membership=[("FIXG1", "FIXSET_A"), ("FIXG2", "FIXSET_A")])
        _load_dir(omitting)
        first = (_gene_set_subject("ml-v1", "FIXSET_B"), _set_claim_id("ml-v1", "FIXSET_B"))
        assert first[0] != member_ful and _members_of(first[0]) is None
        _load_dir(omitting)
        assert (_gene_set_subject("ml-v1", "FIXSET_B"), _set_claim_id("ml-v1", "FIXSET_B")) == first
        body = _gql("""{ claim(id: "%s") { triple { subject } } }""" % first[1])
        assert body["data"]["claim"]["triple"]["subject"] == first[0]


def test_another_phenotypes_membership_never_names_the_subject():
    with _isolated_db():
        _load_dir(_variant("P2", "leak-v1", membership=[("FIXG1", "FIXSET_A"), ("FIXG3", "FIXSET_A")]))
        own = _gene_set_subject("leak-v1", "FIXSET_A", "P2")
        # One gene-set file for P1 and P2, with only P1's membership.
        both = _variant("P1", "leak-v1", membership=[("FIXG1", "FIXSET_A"), ("FIXG2", "FIXSET_A")])
        p2 = _variant("P2", "leak-v1")
        with open(both / "combined_gene_set_stats.json", "a") as out:
            out.write((p2 / "combined_gene_set_stats.json").read_text())
        _load_dir(both)
        assert _gene_set_subject("leak-v1", "FIXSET_A", "P2") == own
        assert _gene_set_subject("leak-v1", "FIXSET_A", "P1") != own
        body = _gql("""{ geneSet(name: "FIXSET_A", collection: "leak-v1", phenotype: "P2") {
            dapperGeneSetId members { claim { triple { subject } } } } }""")
        gs = body["data"]["geneSet"]
        assert gs["dapperGeneSetId"] == own
        assert {m["claim"]["triple"]["subject"] for m in gs["members"]} == {own}
        # A membership file for a phenotype the result files lack is refused.
        mismatched = _variant("P3", "leak-v1")
        (mismatched / "gene_gene_set_stats.json").write_text(
            (_variant("P1", "leak-v1") / "gene_gene_set_stats.json").read_text())
        try:
            _load_dir(mismatched)
        except ValueError as exc:
            assert "own gene_gene_set_stats.json" in str(exc)
        else:
            raise AssertionError("a membership file for another phenotype must be refused")


def test_live_rosters_are_found_through_their_edges_phenotype():
    """A roster row keeps its first writer's phenotype key; the loader finds
    it through the phenotype of the edges that keep it live."""
    with _isolated_db():
        _load_dir(_variant("OLDKEY", "edge-v1"))  # before the registry: rows under OLDKEY
        _load_traits()                           # OLDKEY -> KPN.TRAIT:9000002 (pigean PIGKEY)
        _load_dir(_variant("OLDKEY", "edge-v1"))  # edges move to the trait id
        member_ful = _gene_set_subject("edge-v1", "FIXSET_B")
        con = service_db.connect()
        try:  # a later registry renames the legacy key
            with con:
                con.execute("UPDATE kpn_traits SET legacy_id = 'NEWKEY' WHERE trait_id = 'KPN.TRAIT:9000002'")
        finally:
            con.close()
        _load_without_membership(_variant("NEWKEY", "edge-v1"))
        assert _gene_set_subject("edge-v1", "FIXSET_B") == member_ful


def test_reload_removes_rows_under_long_or_non_ascii_legacy_keys_on_sqlite():
    for legacy in ("Crohn\u2019s disease", "gcat_trait_" + "x" * 70):
        with _isolated_db():
            _load_dir(_variant(legacy, "long-v1"))
            con = service_db.connect()
            try:
                with con:
                    con.execute("INSERT INTO kpn_traits (trait_id, legacy_id, name) VALUES (?, ?, ?)",
                                ("KPN.TRAIT:9100001", legacy, "Long key trait"))
            finally:
                con.close()
            _load_dir(_variant(legacy, "long-v1"))
            for table in ("pigean_gene_associations", "pigean_gene_set_associations", "claim_memberships"):
                keys = {r["phenotype"] for r in _rows(f"SELECT DISTINCT phenotype FROM {table}")}
                assert keys == {"KPN.TRAIT:9100001"}, (legacy, table, keys)


def test_numbers_strings_and_nesting_must_be_interoperable():
    def post(raw):
        return client.post("/v1/claims", content=raw.encode(), headers={"Content-Type": "application/json"})
    for value in (10 ** 30, 2 ** 53, -(2 ** 53), 10 ** 400):
        resp = post('{"objects":[{"class_name":"Agent","id":"dapper:Agent.HUGE","value":%d}]}' % value)
        assert resp.status_code == 400 and "2^53" in resp.json()["detail"], value
    ok = {"objects": [{"class_name": "Agent", "id": "dapper:Agent.EXACT", "value": 2 ** 53 - 1}]}
    assert client.post("/v1/claims", json=ok).status_code == 200
    # A float literal is an IEEE-754 double: accepted, rounded on parse (documented).
    assert post('{"objects":[{"class_name":"Agent","id":"dapper:Agent.DBL","value":9007199254740993.0}]}').status_code == 200
    assert client.get("/v1/objects/dapper:Agent.DBL").json()["value"] == 9007199254740992.0
    for raw in ('{"objects":[{"class_name":"Agent","id":"dapper:Agent.SURR","name":"\\ud800"}]}',
                '{"objects":[{"class_name":"Agent","id":"dapper:Agent.SURRK","\\udc00":1}]}'):
        resp = post(raw)
        assert resp.status_code == 400 and "Unicode" in resp.json()["detail"], raw
    for depth in (100, 600, 5000):
        resp = post('{"objects":[{"class_name":"Agent","id":"dapper:Agent.DEEP%d","x":%s1%s}]}'
                    % (depth, "[" * depth, "]" * depth))
        assert resp.status_code == 400 and "nested" in resp.json()["detail"], depth
    for missing in ("dapper:Agent.SURR", "dapper:Agent.DEEP600"):
        assert client.get(f"/v1/objects/{missing}").status_code == 404


def test_response_render_check_matches_the_real_render():
    import service.app as svc
    try:
        svc._render({"data": {"x": "\ud800"}})  # lone surrogate: not encodable as UTF-8
    except ValueError:
        pass
    else:
        raise AssertionError("a lone surrogate must fail the render check")
    safe = svc._renderable_errors({"data": None, "errors": [{"message": "bad \ud800"}]})
    assert json.loads(svc._render(safe))["errors"]


def test_score_kind_must_be_a_score_kind():
    _load_fixtures()
    scores = [{"class_name": "ClaimScore", "id": f"dapper:ClaimScore.SKX{m}", "score_kind": kind, "metric": m,
               "value": v, "interpretation": "x"}
              for m, kind, v in (("combined", "NOT_A_KIND", 99999.0), ("log_bf", "SCORE", 1.0), ("prior", "SCORE", 1.0))]
    claim = {"class_name": "Claim", "id": "dapper:Claim.SKX", "proposition": "dapper:Proposition.SYNTHG01",
             "has_score": [s["id"] for s in scores]}
    hint = {"claim_id": claim["id"], "method": "pigean", "predicate": "associated_with",
            "subject": {"category": "gene", "id": "ZZKIND"}, "object": {"category": "phenotype", "id": "FIXPHENO"},
            "collection": "fixture-v1"}
    client.post("/v1/claims", json=_bundle())
    resp = client.post("/v1/claims", json={"objects": [*scores, claim], "index_hints": [hint]})
    assert resp.status_code == 400 and "ScoreKind" in resp.json()["detail"]
    # Stored without a hint, the claim still serves — minus the bad score.
    assert client.post("/v1/claims", json={"objects": [*scores, claim]}).status_code == 200
    body = _gql('{ claim(id: "dapper:Claim.SKX") { scores { metric scoreKind } } }')
    assert "errors" not in body, body.get("errors")
    assert sorted(s["metric"] for s in body["data"]["claim"]["scores"]) == ["log_bf", "prior"]


def _rev(repo, ref):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", ref],
                          capture_output=True, text=True).returncode == 0


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})


def test_trait_release_publication_and_key_changes():
    spec = importlib.util.spec_from_file_location("load_kpn_traits", ROOT / "scripts" / "load_kpn_traits.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fixture = (ROOT / "tests" / "fixtures" / "kpn-data-models" / "versions" / "trait" / "v0.0.2"
               / "kpn_trait_collection.yaml").read_text()
    not_git = Path(tempfile.mkdtemp())
    release = mod.release_path(not_git, "v0.0.2")
    release.parent.mkdir(parents=True)
    release.write_text(fixture)
    try:
        mod.git_commit(not_git, release)
    except SystemExit as exc:
        assert "not a git checkout" in str(exc)
    else:
        raise AssertionError("an unverifiable release must be refused")
    assert mod.git_commit(not_git, release, allow_unpublished=True) == (None, False)
    # A local-only commit is refused; once tagged (a tag-pinned clone) it is published.
    _git(not_git, "init", "-q")
    _git(not_git, "add", ".")
    _git(not_git, "commit", "-q", "-m", "release")
    try:
        mod.git_commit(not_git, release)
    except SystemExit as exc:
        assert "unpublished" in str(exc)
    else:
        raise AssertionError("a local-only commit must be refused")
    commit, published = mod.git_commit(not_git, release, allow_unpublished=True)
    assert commit and published is False
    _git(not_git, "tag", "v0.0.2")  # a local-only tag publishes nothing
    try:
        mod.git_commit(not_git, release)
    except SystemExit as exc:
        assert "unpublished" in str(exc)
    else:
        raise AssertionError("a local-only tag must not count as publication")
    upstream = Path(tempfile.mkdtemp()) / "upstream.git"
    _git(upstream.parent, "init", "-q", "--bare", str(upstream))
    _git(not_git, "push", "-q", str(upstream), "v0.0.2")  # published as a tag only (no branch)
    _git(not_git, "remote", "add", "localclone", str(upstream))  # a LOCAL remote proves nothing
    try:
        mod.git_commit(not_git, release)
    except SystemExit as exc:
        assert "not shown published" in str(exc)
    else:
        raise AssertionError("a local-path remote must not count as publication")
    # A non-local remote (offline: insteadOf maps its URL to the local bare repo).
    url = "https://example.invalid/kpn-data-models.git"
    _git(not_git, "config", f"url.{upstream}.insteadOf", url)
    _git(not_git, "remote", "add", "origin", url)
    assert mod.git_commit(not_git, release) == (commit, True)  # it advertises the tag
    fetched = Path(tempfile.mkdtemp())  # `git fetch <url> <tag>` + checkout FETCH_HEAD: not evidence
    _git(fetched, "init", "-q")
    _git(fetched, "fetch", "-q", str(upstream), "v0.0.2")
    _git(fetched, "checkout", "-q", "FETCH_HEAD")
    try:
        mod.git_commit(fetched, mod.release_path(fetched, "v0.0.2"))
    except SystemExit as exc:
        assert "add it as a remote" in str(exc)
    else:
        raise AssertionError("a FETCH_HEAD-only checkout must be refused")
    pulled = Path(tempfile.mkdtemp()) / "pulled"  # a local-only commit pulled from '.' (FETCH_HEAD "of .")
    _git(pulled.parent, "clone", "-q", str(not_git), str(pulled))
    _git(pulled, "remote", "set-url", "origin", url)
    _git(pulled, "config", f"url.{upstream}.insteadOf", url)
    _git(pulled, "checkout", "-q", "-b", "feature", "--track", "master" if _rev(pulled, "master") else "main")
    _git(pulled, "checkout", "-q", "-")
    pulled_release = mod.release_path(pulled, "v0.0.2")
    pulled_release.write_text(fixture + "\n# local edit\n")
    _git(pulled, "commit", "-qam", "local edit")
    _git(pulled, "checkout", "-q", "feature")
    _git(pulled, "pull", "-q", "--ff-only")
    try:
        mod.git_commit(pulled, pulled_release)
    except SystemExit as exc:
        assert "not shown published" in str(exc)
    else:
        raise AssertionError("a local-only commit must stay unpublished whatever FETCH_HEAD says")
    release.write_text(fixture + "\n# local edit\n")  # not the committed blob
    assert mod.git_commit(not_git, release, allow_unpublished=True) == (None, False)
    release.write_text(fixture)
    with _isolated_db():
        _load_dir(_variant("REGPHENO", "legacy-v1"))  # before the registry: stored under the legacy key
        con = service_db.connect()
        try:
            stats = mod.load(con, release, "v0.0.2", commit, published=False)
        finally:
            con.close()
        assert stats["source_url"].startswith("unpublished:")
        assert client.get("/v1/vocabulary").json()["trait_registry"]["published"] is False
        for changed in (fixture.replace("KPN.TRAIT:9000001", "KPN.TRAIT:9000009"),  # trait dropped
                        fixture.replace("legacy_id: REGPHENO", "legacy_id: RENAMED")):  # key unmapped
            path = Path(tempfile.mkdtemp()) / "changed.yaml"
            path.write_text(changed)
            con = service_db.connect()
            try:
                try:
                    mod.load(con, path, "v9", None)
                except SystemExit as exc:
                    assert "KPN.TRAIT:9000001" in str(exc)
                else:
                    raise AssertionError("a release that changes a stored key's trait must be refused")
            finally:
                con.close()


def test_audit_reports_deposited_roster_edges_for_non_members():
    with _isolated_db():
        _load_dir(_variant("AUD", "aud-v1"))
        entity = _rows("SELECT dapper_id FROM gene_set_entities WHERE collection = 'aud-v1' AND name = 'FIXSET_A'")[0]["dapper_id"]
        prop = {"class_name": "Proposition", "id": "dapper:Proposition.ROSTERX", "statement": "roster",
                "subject_entity": entity, "relation": "has_members", "object_entity": "dapper:GeneSetCollection.X"}
        claim = {"class_name": "Claim", "id": "dapper:Claim.ROSTERX", "proposition": prop["id"]}
        hint = {"claim_id": claim["id"], "method": "pigean", "predicate": "member_of",
                "subject": {"category": "gene", "id": "NOTAMEMBER"}, "object": {"category": "gene_set", "id": "FIXSET_A"},
                "collection": "aud-v1"}
        assert client.post("/v1/claims", json={"objects": [prop, claim], "index_hints": [hint]}).status_code == 200
        audit = subprocess.run([sys.executable, str(ROOT / "scripts" / "audit_objects.py")],
                               capture_output=True, text=True, env={**os.environ, "CLAIMS_DB": service_db.DB_PATH})
        assert audit.returncode != 0 and "deposited roster claim dapper:Claim.ROSTERX" in audit.stdout + audit.stderr


def test_mysql_needs_an_explicit_database_name():
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAIMS_MYSQL")}
    env.update(CLAIMS_MYSQL_HOST="127.0.0.1", CLAIMS_MYSQL_PASSWORD="x")
    probe = subprocess.run([sys.executable, "-c", "from service import db; db.connect()"], cwd=ROOT,
                           capture_output=True, text=True, env=env)
    assert probe.returncode != 0 and "CLAIMS_MYSQL_DATABASE must be set" in probe.stderr
    migrate = subprocess.run([sys.executable, str(ROOT / "scripts" / "migrate.py")], capture_output=True,
                             text=True, env=env)
    assert migrate.returncode == 1 and "set CLAIMS_MYSQL_DATABASE" in migrate.stderr


def test_other_phenotypes_member_less_subject_ignores_this_loads_membership():
    """P2's subject in a P1+P2 gene-set file loaded with P1's membership is
    exactly what a P2-only membership-less load names — whatever P1's file says."""
    subjects = []
    for p1_members in ([("FIXG1", "FIXSET_A")], [("FIXG1", "FIXSET_A"), ("FIXG2", "FIXSET_A")], None):
        with _isolated_db():
            both = _variant("P1", "neutral-v1", membership=p1_members)
            with open(both / "combined_gene_set_stats.json", "a") as out:
                out.write((_variant("P2", "neutral-v1") / "combined_gene_set_stats.json").read_text())
            (_load_dir if p1_members else _load_without_membership)(both)
            subjects.append(_gene_set_subject("neutral-v1", "FIXSET_A", "P2"))
            assert "was_derived_from" not in client.get(f"/v1/objects/{subjects[-1]}").json()
    with _isolated_db():
        _load_without_membership(_variant("P2", "neutral-v1"))
        subjects.append(_gene_set_subject("neutral-v1", "FIXSET_A", "P2"))
    assert len(set(subjects)) == 1, subjects


def test_service_finds_memberships_under_non_ascii_legacy_keys_on_sqlite():
    with _isolated_db():
        legacy = "Crohn’s disease"
        _load_dir(_variant(legacy, "svc-v1"))
        con = service_db.connect()
        try:
            with con:
                con.execute("INSERT INTO kpn_traits (trait_id, legacy_id, name) VALUES (?, ?, ?)",
                            ("KPN.TRAIT:9100002", legacy, "Crohn's"))
        finally:
            con.close()
        _load_without_membership(_variant(legacy, "svc-v1"))  # edges stay under the legacy key
        body = _gql("""{ geneSet(name: "FIXSET_A", collection: "svc-v1", phenotype: "KPN.TRAIT:9100002") {
            dapperGeneSetId members { gene { symbol } claim { triple { subject } } } } }""")
        assert "errors" not in body, body.get("errors")
        gs = body["data"]["geneSet"]
        assert sorted(m["gene"]["symbol"] for m in gs["members"]) == ["FIXG1", "FIXG2"]
        assert {m["claim"]["triple"]["subject"] for m in gs["members"]} == {gs["dapperGeneSetId"]}


def test_typed_slots_are_validated_and_stored_documents_never_fail_a_response():
    for obj in ({"class_name": "Proposition", "id": "dapper:Proposition.BADREL", "relation": {"k": 1}},
                {"class_name": "Proposition", "id": "dapper:Proposition.BADSUBJ", "subject_entity": ["ZZ"]},
                {"class_name": "Claim", "id": "dapper:Claim.BADDERIV", "was_derived_from": [{"k": 1}]},
                {"class_name": "EvidenceItem", "id": "dapper:EvidenceItem.BADLOC", "value_locators": 7}):
        resp = client.post("/v1/claims", json={"objects": [obj]})
        assert resp.status_code == 400, obj
    # A malformed document already in the store (written before validation
    # existed) is rendered defensively, never failing the response.
    con = service_db.connect()
    try:
        with con:
            for oid, cls, doc in (
                ("dapper:Proposition.LEGACYBAD", "Proposition",
                 {"subject_entity": ["ZZ"], "relation": "associated_with", "object_entity": {"k": 1}}),
                ("dapper:EvidenceItem.LEGACYBAD", "EvidenceItem", {"value_locators": 7}),
                ("dapper:Claim.LEGACYBAD", "Claim",
                 {"proposition": "dapper:Proposition.LEGACYBAD", "was_derived_from": [{"k": 1}, "dapper:File.X"],
                  "has_score": "nope", "has_evidence": ["dapper:EvidenceItem.LEGACYBAD", 5]}),
            ):
                con.execute(con.dialect.upsert_object,
                            (oid, cls, "unverified", json.dumps({"id": oid, "class_name": cls, **doc})))
    finally:
        con.close()
    body = _gql("""{ claim(id: "dapper:Claim.LEGACYBAD") { triple { subject } scores { metric }
        evidence { valueLocators } provenanceRefs { derivedFromFiles } } }""")
    assert "errors" not in body, body.get("errors")
    env = body["data"]["claim"]
    assert env["triple"] is None and env["scores"] == []
    prop = _gql('{ proposition(id: "dapper:Proposition.LEGACYBAD") { id triple { subject } } }')
    assert "errors" not in prop, prop.get("errors")
    assert prop["data"]["proposition"]["triple"] is None
    assert env["provenanceRefs"]["derivedFromFiles"] == ["dapper:File.X"]
    assert env["evidence"] == [{"valueLocators": []}]


def test_audit_checks_the_cited_rosters_gene_set_and_tolerates_foreign_rosters():
    with _isolated_db():
        _load_dir(_variant("AUD2", "aud2-v1"))
        roster_a = _rows("SELECT proposition_id FROM pigean_gene_set_rosters WHERE gene_set = 'FIXSET_A'")[0]["proposition_id"]
        # A new claim on FIXSET_A's roster proposition, cited for an edge in FIXSET_B.
        wrong = {"class_name": "Claim", "id": "dapper:Claim.WRONGSET", "proposition": roster_a, "statement": "x"}
        hint = {"claim_id": wrong["id"], "method": "pigean", "predicate": "member_of",
                "subject": {"category": "gene", "id": "FIXG2"}, "object": {"category": "gene_set", "id": "FIXSET_B"},
                "collection": "aud2-v1"}
        assert client.post("/v1/claims", json={"objects": [wrong], "index_hints": [hint]}).status_code == 200
        # A foreign roster (members as objects / CURIEs): not checkable, must not crash or fail.
        foreign = [
            {"class_name": "GeneSet", "id": "dapper:GeneSet.FOREIGN", "name": "FOREIGN", "members": [{"symbol": "NOD2"}]},
            {"class_name": "Proposition", "id": "dapper:Proposition.FOREIGN", "subject_entity": "dapper:GeneSet.FOREIGN",
             "relation": "has_members", "object_entity": "dapper:GeneSetCollection.Y"},
            {"class_name": "Claim", "id": "dapper:Claim.FOREIGN", "proposition": "dapper:Proposition.FOREIGN"},
        ]
        entity_a = _rows("SELECT dapper_id FROM gene_set_entities WHERE collection = 'aud2-v1' AND name = 'FIXSET_A'")[0]["dapper_id"]
        about_a = [{"class_name": "Proposition", "id": "dapper:Proposition.ABOUTA", "subject_entity": entity_a,
                    "relation": "has_members", "object_entity": "dapper:GeneSetCollection.Z"},
                   {"class_name": "Claim", "id": "dapper:Claim.OTHERCOLL", "proposition": "dapper:Proposition.ABOUTA"},
                   {"class_name": "Activity", "id": "dapper:Activity.ODDROSTER", "relation": "has_members",
                    "subject_entity": ["dapper:GeneSet.X"]},
                   {"class_name": "Claim", "id": "dapper:Claim.ODD", "proposition": "dapper:Activity.ODDROSTER"}]
        hints = [{"claim_id": "dapper:Claim.OTHERCOLL", "method": "pigean", "predicate": "member_of",
                  "subject": {"category": "gene", "id": "FIXG2"}, "object": {"category": "gene_set", "id": "FIXSET_A"},
                  "collection": "other-v9"},
                 {"claim_id": "dapper:Claim.ODD", "method": "pigean", "predicate": "member_of",
                  "subject": {"category": "gene", "id": "FIXG2"}, "object": {"category": "gene_set", "id": "ODD"},
                  "collection": "odd-v1"}]
        assert client.post("/v1/claims", json={"objects": about_a, "index_hints": hints}).status_code == 200
        hint2 = {"claim_id": "dapper:Claim.FOREIGN", "method": "pigean", "predicate": "member_of",
                 "subject": {"category": "gene", "id": "NOD2"}, "object": {"category": "gene_set", "id": "FOREIGN"},
                 "collection": "foreign-v1"}
        assert client.post("/v1/claims", json={"objects": foreign, "index_hints": [hint2]}).status_code == 200
        audit = subprocess.run([sys.executable, str(ROOT / "scripts" / "audit_objects.py")],
                               capture_output=True, text=True, env={**os.environ, "CLAIMS_DB": service_db.DB_PATH})
        out = audit.stdout + audit.stderr
        assert "Traceback" not in out, out
        assert audit.returncode != 0 and "dapper:Claim.WRONGSET for a different gene set" in out
        assert "dapper:Claim.OTHERCOLL for a different gene set" in out  # right set, wrong collection
        problems = out.split("PROBLEMS", 1)[1]
        assert "Claim.FOREIGN" not in problems and "Claim.ODD" not in problems  # counted, not judged
        assert "(members not checked): 2" in out


def test_loader_cli_membership_options():
    db_path = os.path.join(tempfile.mkdtemp(), "cli.db")
    variant = _variant("CLIPHENO", "cli-v1")
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAIMS_MYSQL")}
    base = [sys.executable, str(ROOT / "scripts" / "load_pigean.py"), "--db", db_path,
            "--gene-stats", str(variant / "combined_gene_stats.json"),
            "--gene-set-stats", str(variant / "combined_gene_set_stats.json")]
    skipped = subprocess.run(base + ["--no-membership"], capture_output=True, text=True, env=env)
    assert skipped.returncode == 0 and "--no-membership" in skipped.stdout
    mismatched = subprocess.run(base + ["--membership-stats", str(_variant("OTHER", "cli-v1") / "gene_gene_set_stats.json")],
                                capture_output=True, text=True, env=env)
    assert mismatched.returncode != 0 and "--no-membership" in mismatched.stderr
    own = subprocess.run(base + ["--membership-stats", str(variant / "gene_gene_set_stats.json")],
                         capture_output=True, text=True, env=env)
    assert own.returncode == 0 and "membership pairs indexed: 3" in own.stdout


def _silent_server():
    """A TCP endpoint that accepts connections and never replies."""
    import socket
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    held = []

    def accept():
        while True:
            try:
                held.append(srv.accept()[0])
            except OSError:
                return
    threading.Thread(target=accept, daemon=True).start()
    return srv


def test_publication_check_is_bounded_and_trusts_only_fully_non_local_remotes():
    import time
    spec = importlib.util.spec_from_file_location("load_kpn_traits", ROOT / "scripts" / "load_kpn_traits.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fixture = (ROOT / "tests" / "fixtures" / "kpn-data-models" / "versions" / "trait" / "v0.0.2"
               / "kpn_trait_collection.yaml").read_text()
    upstream = Path(tempfile.mkdtemp()) / "upstream.git"
    _git(upstream.parent, "init", "-q", "--bare", str(upstream))
    repo = Path(tempfile.mkdtemp())
    release = mod.release_path(repo, "v0.0.2")
    release.parent.mkdir(parents=True)
    release.write_text(fixture)
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "release")
    url = "https://example.invalid/kpn-data-models.git"
    _git(repo, "config", f"url.{upstream}.insteadOf", url)
    _git(repo, "remote", "add", "upstream", url)
    _git(repo, "push", "-q", "upstream", "HEAD:refs/heads/main")
    _git(repo, "fetch", "-q", "upstream")
    commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    srv = _silent_server()
    try:  # a silent remote that sorts first never delays a release a tracking branch proves
        _git(repo, "remote", "add", "aaa-silent", f"http://127.0.0.1:{srv.getsockname()[1]}/x.git")
        started = time.monotonic()
        assert mod.git_commit(repo, release) == (commit, True)
        assert time.monotonic() - started < 5
        # A local-only commit: ls-remote runs, and the silent remote is bounded.
        release.write_text(fixture + "\n# local edit\n")
        _git(repo, "commit", "-qam", "local edit")
        original, mod.LS_REMOTE_TIMEOUT = mod.LS_REMOTE_TIMEOUT, 2
        try:
            started = time.monotonic()
            try:
                mod.git_commit(repo, release)
            except SystemExit as exc:
                assert "not shown published" in str(exc)
            else:
                raise AssertionError("a local-only commit must be refused")
            assert time.monotonic() - started < 15
        finally:
            mod.LS_REMOTE_TIMEOUT = original
    finally:
        srv.close()
    _git(repo, "remote", "remove", "aaa-silent")
    # A push to a LOCAL push URL updates the tracking branch: not evidence.
    mirror = Path(tempfile.mkdtemp()) / "mirror.git"
    _git(mirror.parent, "init", "-q", "--bare", str(mirror))
    _git(repo, "remote", "add", "pushy", url)
    _git(repo, "remote", "set-url", "--push", "pushy", str(mirror))
    _git(repo, "push", "-q", "pushy", "HEAD:refs/heads/main")
    # A local remote named "<eligible>/x": its tracking refs are not upstream's.
    _git(repo, "remote", "add", "upstream/mirror", str(mirror))
    _git(repo, "fetch", "-q", "upstream/mirror")
    try:
        mod.git_commit(repo, release)
    except SystemExit as exc:
        assert "not shown published" in str(exc)
    else:
        raise AssertionError("local push URLs and nested local remotes must not count as publication")


def test_audit_checks_edges_citing_rosters_about_member_less_loaded_sets():
    with _isolated_db():
        _load_dir(_variant("AUD3", "aud3-v1", membership=[("FIXG1", "FIXSET_A")]))  # FIXSET_B: member-less
        entity_b = _gene_set_subject("aud3-v1", "FIXSET_B")
        assert _members_of(entity_b) is None
        objs = [{"class_name": "Proposition", "id": "dapper:Proposition.ABOUTB", "subject_entity": entity_b,
                 "relation": "has_members", "object_entity": "dapper:GeneSetCollection.Z"},
                {"class_name": "Claim", "id": "dapper:Claim.ABOUTB", "proposition": "dapper:Proposition.ABOUTB"}]
        hint = {"claim_id": "dapper:Claim.ABOUTB", "method": "pigean", "predicate": "member_of",
                "subject": {"category": "gene", "id": "NOD2"}, "object": {"category": "gene_set", "id": "SOME_OTHER_SET"},
                "collection": "other-v9"}
        assert client.post("/v1/claims", json={"objects": objs, "index_hints": [hint]}).status_code == 200
        audit = subprocess.run([sys.executable, str(ROOT / "scripts" / "audit_objects.py")],
                               capture_output=True, text=True, env={**os.environ, "CLAIMS_DB": service_db.DB_PATH})
        out = audit.stdout + audit.stderr
        assert audit.returncode != 0 and "dapper:Claim.ABOUTB for a different gene set (aud3-v1/FIXSET_B)" in out, out


# --- admin API (consumed by the admin console in admin/) ---------------------

import time as _time  # noqa: E402
import uuid as _uuid  # noqa: E402

import jwt as _jwt  # noqa: E402

from service import admin_gateway  # noqa: E402
from service import telemetry  # noqa: E402

GATEWAY_SECRET = "test-gateway-secret-at-least-32-characters"


def _gateway(monkeypatch, admins=("admin@example.org",)):
    monkeypatch.setattr(admin_gateway, "SECRET", GATEWAY_SECRET)
    monkeypatch.setattr(admin_gateway, "ADMIN_EMAILS", set(admins))


def _assertion(purpose="admin_read", secret=GATEWAY_SECRET, lifetime=120, **overrides):
    now = int(_time.time())
    claims = {"iss": "claims-admin", "aud": "claims-api", "iat": now, "exp": now + lifetime,
              "jti": str(_uuid.uuid4()), "sub": "admin@example.org", "purpose": purpose,
              "email": "admin@example.org", "email_verified": True}
    claims.update(overrides)
    return _jwt.encode({k: v for k, v in claims.items() if v is not None}, secret, algorithm="HS256")


def _admin_get(path, token=None, **params):
    return client.get(f"/v1/admin/api/{path}", params=params,
                      headers={"Authorization": f"Bearer {token or _assertion()}"})


def test_admin_api_accepts_only_the_consoles_signed_assertion(monkeypatch):
    monkeypatch.setattr(admin_gateway, "SECRET", "")
    assert _admin_get("me").status_code == 503  # unconfigured: closed
    _gateway(monkeypatch)
    ok = _admin_get("me")
    assert ok.status_code == 200 and ok.json()["email"] == "admin@example.org"
    assert ok.headers["cache-control"] == "no-store"
    assert client.get("/v1/admin/api/me").status_code == 401
    refused = {
        "wrong secret": _assertion(secret="another-secret-that-is-at-least-32-chars"),
        "expired": _assertion(iat=int(_time.time()) - 400, exp=int(_time.time()) - 200),
        "too long-lived": _assertion(lifetime=3600),
        "wrong audience": _assertion(aud="reveal-api"),
        "wrong issuer": _assertion(iss="someone-else"),
        "no jti": _assertion(jti=None),
    }
    for label, token in refused.items():
        assert _admin_get("me", token).status_code == 401, label
    unsigned = _jwt.encode({"iss": "claims-admin", "aud": "claims-api", "iat": 1, "exp": 9999999999, "jti": "x",
                            "sub": "a", "purpose": "admin_read", "email": "admin@example.org", "email_verified": True},
                           None, algorithm="none")
    assert _admin_get("me", unsigned).status_code == 401
    # The API's own allowlist and Google's verification both apply.
    assert _admin_get("me", _assertion(email="intruder@example.org")).status_code == 403
    assert _admin_get("me", _assertion(email_verified=False)).status_code == 403
    # A sign-in audit assertion cannot read, and a read assertion cannot write the audit.
    assert _admin_get("me", _assertion(purpose="auth_event")).status_code == 403
    post = lambda token, body: client.post("/v1/admin/api/auth-events", json=body, headers={"Authorization": f"Bearer {token}"})
    assert post(_assertion(), {"event": "login", "email": "x@example.org"}).status_code == 403
    assert post(_assertion(purpose="auth_event"), {"event": "rm -rf", "email": "x@example.org"}).status_code == 422
    assert post(_assertion(purpose="auth_event"), {"event": "login_denied", "email": "Intruder@Example.org",
                                                    "detail": "not an approved admin"}).status_code == 204
    events = _admin_get("activity").json()["authEvents"]
    assert events[0]["event"] == "login_denied" and events[0]["email"] == "intruder@example.org"
    assert "admin@example.org" in _admin_get("activity").json()["approvedAdmins"]


def test_admin_api_reports_claims_templates_tables_and_activity(monkeypatch):
    _gateway(monkeypatch)
    _load_fixtures()
    gene_claim = _index_claim_id("pigean_gene_associations", "gene", "FIXG1")
    client.get("/v1/health")
    client.get(f"/v1/objects/{gene_claim}")

    ov = _admin_get("overview").json()
    fams = {f["family"]: f for f in ov["families"]}
    assert set(fams) == {"gene", "gene_set", "roster"}
    assert ov["claims"]["implied"] == sum(f["implied"] for f in ov["families"]) > 0
    assert len(ov["mintTimeline"]) == 30 and ov["database"]["backend"] == "sqlite"

    listed = _admin_get("claims", family="gene", page_size=100).json()
    assert listed["total"] == fams["gene"]["implied"]
    assert gene_claim in {r["claimId"] for r in listed["rows"]}
    fixg1 = _admin_get("claims", q="fixg1").json()
    assert {r["subject"] for r in fixg1["rows"]} == {"FIXG1"}
    everything = _admin_get("claims", page_size=1).json()
    assert everything["total"] == ov["claims"]["implied"] + ov["claims"]["deposited"]
    assert _admin_get("claims", family="nope").status_code == 422

    before = _times_seen(gene_claim)
    detail = _admin_get(f"claims/{gene_claim}").json()
    assert detail["verification"]["templateBacked"] and detail["verification"]["synthesizes"]
    assert detail["indexRow"]["family"] == "gene" and detail["triple"]["subject"] == "FIXG1"
    assert _times_seen(gene_claim) == before  # inspecting never asserts
    assert _admin_get("claims/dapper:Claim.nonexistent").status_code == 404

    templates = _admin_get("templates").json()
    method = templates["methods"][0]
    assert method["name"] == "pigean" and len(method["template"]["sha256"]) == 64
    assert {m["metric"] for m in method["metrics"]} == {"combined", "log_bf", "prior", "beta", "beta_uncorrected"}
    assert all(m["kindAgrees"] for m in method["metrics"])
    assert any(q["name"] == "ibd-validation-challenge" for q in templates["queryTemplates"])

    tables = _admin_get("tables", fresh="true").json()
    names = {t["name"]: t for t in tables["tables"]}
    assert names["dapper_objects"]["rows"] > 0 and "admin_auth_events" in names
    rows = _admin_get("tables/pigean_gene_associations/rows", column="gene", op="equals", value="FIXG1").json()
    assert rows["total"] >= 1 and all(r["gene"] == "FIXG1" for r in rows["rows"])
    assert _admin_get("tables/sqlite_master/rows").status_code == 404
    assert _admin_get("tables/dapper_objects/rows", column="id; DROP", value="x").status_code == 400

    activity = _admin_get("activity").json()
    routes = {(r["method"], r["route"]) for r in activity["telemetry"]["routes"]}
    assert ("GET", "/v1/health") in routes and ("GET", "/v1/objects/{object_id}") in routes
    assert not any(r.startswith("/v1/admin") for _, r in routes)


def test_graphql_telemetry_counts_mints_and_root_fields():
    _load_fixtures()
    before = telemetry.snapshot()["graphql"]
    _gql('{ health { status } }')
    _gql('{ nope }')
    after = telemetry.snapshot()["graphql"]
    assert after["requests"] == before["requests"] + 2 and after["failed"] == before["failed"] + 1
    fields = {f["field"]: f["count"] for f in after["rootFields"]}
    assert fields.get("health", 0) >= 1 and fields.get("nope", 0) >= 1
