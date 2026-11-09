"""Contract smoke tests on a throwaway DB: deposit idempotency, retrieval,
graph query over a deposited claim, and the challenge query's missing-source
warning path."""
import json
import os
import tempfile
from pathlib import Path

os.environ["CLAIMS_DB"] = os.path.join(tempfile.mkdtemp(), "test-claims.db")

from fastapi.testclient import TestClient  # noqa: E402

from service.app import app  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
client = TestClient(app)


def _bundle():
    return json.loads((ROOT / "api" / "examples" / "deposit-bundle.request.json").read_text())


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
    # The dapper-object lookup is part of the published contract.
    assert "/v1/objects/{object_id}" in contract.text
    # This deployment is injected as the FIRST server, so Swagger try-it-out
    # stays same-origin (the localhost vs 127.0.0.1 CORS trap).
    first_server = contract.text.split("servers:\n", 1)[1].splitlines()[0]
    assert "http://testserver" in first_server
    # Sections: objects / claims / propositions / deposit are separate tags.
    for tag in ("[Objects]", "[Propositions]", "[Deposit]", "[Claims]"):
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
    # The mint counter tracks every idempotent re-deposit of the same content.
    times = {d["id"]: d["times_deposited"] for d in deposited}
    assert times["dapper:Claim.SYNTHG01"] == 2

    got = client.get("/v1/claims/dapper:Claim.SYNTHG01")
    assert got.status_code == 200
    body = got.json()
    assert {s["metric"] for s in body["scores"]} == {"combined", "log_bf", "prior"}
    assert body["provenance_refs"]["resolution"] == "unresolved"
    assert body["deposits"]["times"] == 2
    assert body["deposits"]["last_at"] is not None


def test_measurement_claim_carries_evidence():
    """The evidence layer (2026-11-09): has_evidence -> EvidenceItem ->
    ValueLocator, dereferenced on the envelope via GraphQL and REST."""
    client.post("/v1/claims", json=_bundle())
    env = client.post("/v1/graphql", json={"query": """
      { claim(id: "dapper:Claim.SYNTHG01") {
          triple { subject predicate object }
          evidence { item valueLocators } } }
    """}).json()["data"]["claim"]
    assert env["triple"] == {"subject": "NOD2", "predicate": "associated_with", "object": "IBD"}
    assert len(env["evidence"]) == 1
    item = env["evidence"][0]["item"]
    assert item["class_name"] == "EvidenceItem"
    assert item["target_proposition"] == "dapper:Proposition.SYNTHG01"
    locators = env["evidence"][0]["valueLocators"]
    assert [loc["value_text"] for loc in locators] == ["9.8"]
    assert locators[0]["file"] == "dapper:File.SYNTHGENESTATS"
    rest = client.get("/v1/claims/dapper:Claim.SYNTHG01").json()
    assert rest["evidence"][0]["valueLocators"][0]["value_text"] == "9.8"


def test_identifier_resolution_and_resolver_urls():
    """CURIE stays canonical; every response offers the shareable URL form."""
    client.post("/v1/claims", json=_bundle())
    idres = client.get("/v1/vocabulary").json()["identifier_resolution"]
    assert idres["prefixes"]["dapper"].startswith("https://")
    assert idres["resolver_url_template"].endswith("/v1/objects/{id}")
    data = client.post("/v1/graphql", json={"query": """
      { claim(id: "dapper:Claim.SYNTHG01") { resolverUrl } identifierResolution }
    """}).json()["data"]
    assert data["claim"]["resolverUrl"].endswith("/v1/objects/dapper:Claim.SYNTHG01")
    assert "dapper" in data["identifierResolution"]["prefixes"]
    listed = client.get("/v1/claims", params={"limit": 1}).json()
    first = listed["claims"][0]
    assert first["resolver_url"].endswith(f"/v1/objects/{first['claim_id']}")
    rest = client.get("/v1/claims/dapper:Claim.SYNTHG01").json()
    assert rest["resolver_url"].endswith("/v1/objects/dapper:Claim.SYNTHG01")


def test_object_landing_page_negotiation():
    """A shared resolver link opens as a page in a browser and as the
    verbatim JSON document everywhere else."""
    client.post("/v1/claims", json=_bundle())
    page = client.get("/v1/objects/dapper:Claim.SYNTHG01", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "dapper:Claim.SYNTHG01" in page.text
    # Referenced dapper IDs are linked through the same resolver.
    assert "/v1/objects/dapper:Proposition.SYNTHG01" in page.text
    raw = client.get("/v1/objects/dapper:Claim.SYNTHG01")
    assert raw.headers["content-type"].startswith("application/json")
    assert raw.json()["class_name"] == "Claim"


def test_graph_query_over_deposited_claim():
    client.post("/v1/claims", json=_bundle())
    query = """
      { phenotype(id: "IBD") {
          pigeanGeneAssociations(where: {prior: {lt: 0.5}}, first: 5) {
            gene { symbol } combined prior claim { id scores { metric value } }
          } } }
    """
    body = client.post("/v1/graphql", json={"query": query}).json()
    assert "errors" not in body, body.get("errors")
    assocs = body["data"]["phenotype"]["pigeanGeneAssociations"]
    assert [a["gene"]["symbol"] for a in assocs] == ["NOD2"]
    assert assocs[0]["claim"]["id"] == "dapper:Claim.SYNTHG01"


def test_challenge_reports_missing_membership_source():
    client.post("/v1/claims", json=_bundle())
    query = (ROOT / "api" / "graphql" / "queries" / "ibd-validation-challenge.graphql").read_text()
    variables = json.loads((ROOT / "api" / "graphql" / "queries" / "ibd-validation-challenge.variables.json").read_text())
    body = client.post("/v1/graphql", json={"query": query, "variables": variables}).json()
    assert "errors" not in body, body.get("errors")
    challenge = body["data"]["pigeanValidationChallenge"]
    assert challenge["rows"] == []
    assert challenge["truncated"] is False
    assert any("membership" in w for w in challenge["warnings"])


def _membership_bundle():
    """Score-less member_of claim (gene + gene set only) plus the gene-set
    association the challenge join needs on the far side."""
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
    body = client.post("/v1/graphql", json={"query": query, "variables": variables}).json()
    assert "errors" not in body, body.get("errors")
    challenge = body["data"]["pigeanValidationChallenge"]
    assert not any("membership" in w for w in challenge["warnings"])
    assert len(challenge["rows"]) == 1
    row = challenge["rows"][0]
    assert row["geneAssociation"]["gene"]["symbol"] == "NOD2"
    # Every claim comes back with its structured triple.
    assert row["geneAssociation"]["claim"]["triple"] == {
        "subject": "NOD2", "predicate": "associated_with", "object": "IBD"
    }
    assert len(row["sets"]) == 1
    entry = row["sets"][0]
    assert entry["membership"]["claim"]["id"] == "dapper:Claim.SYNTHM01"
    assert entry["membership"]["claim"]["triple"]["predicate"] == "member_of"
    assert entry["geneSetAssociation"]["geneSet"]["name"] == "SYNTH_SET"
    assert entry["geneSetAssociation"]["betaUncorrected"] == 2.5
    # The membership claim is score-less: only the set name and the gene.
    membership = client.get("/v1/claims/dapper:Claim.SYNTHM01").json()
    assert membership["scores"] == []
    assert membership["proposition"]["relation"] == "member_of"


def test_resolver_returns_consistent_documents():
    """Every class of dapper object comes back in the same shape: id and
    class_name first, remaining keys sorted, matching the requested ID.
    (Runs after the missing-membership test: deposits membership data.)"""
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
        # GraphQL resolves the same document the same way.
        gql = client.post("/v1/graphql", json={"query": f'{{ dapperObject(id: "{oid}") }}'}).json()
        assert gql["data"]["dapperObject"]["id"] == oid


def _second_set_bundle():
    """A second, weaker gene set for NOD2 so the per-gene limit is testable."""
    return {
        "objects": [
            {
                "class_name": "Proposition",
                "id": "dapper:Proposition.SYNTHS02",
                "statement": "The gene set SYNTH_SET_B is associated with IBD.",
                "subject_entity": "SYNTH_SET_B",
                "relation": "associated_with",
                "object_entity": "IBD",
                "proposition_kind": "RESULT",
            },
            {
                "class_name": "ClaimScore",
                "id": "dapper:ClaimScore.SYNTHS02beta",
                "score_kind": "EFFECT_ESTIMATE",
                "metric": "beta",
                "value": 0.3,
                "interpretation": "PIGEAN joint gene-set effect. SYNTHETIC.",
            },
            {
                "class_name": "ClaimScore",
                "id": "dapper:ClaimScore.SYNTHS02bu",
                "score_kind": "EFFECT_ESTIMATE",
                "metric": "beta_uncorrected",
                "value": 0.9,
                "interpretation": "PIGEAN marginal gene-set effect. SYNTHETIC.",
            },
            {
                "class_name": "Claim",
                "id": "dapper:Claim.SYNTHS02",
                "proposition": "dapper:Proposition.SYNTHS02",
                "has_score": ["dapper:ClaimScore.SYNTHS02beta", "dapper:ClaimScore.SYNTHS02bu"],
            },
            {
                "class_name": "Proposition",
                "id": "dapper:Proposition.SYNTHM02",
                "statement": "The gene NOD2 is a member of the gene set SYNTH_SET_B.",
                "subject_entity": "NOD2",
                "relation": "member_of",
                "object_entity": "SYNTH_SET_B",
                "proposition_kind": "RESULT",
            },
            {
                "class_name": "Claim",
                "id": "dapper:Claim.SYNTHM02",
                "proposition": "dapper:Proposition.SYNTHM02",
            },
        ],
        "index_hints": [
            {
                "claim_id": "dapper:Claim.SYNTHS02",
                "method": "pigean",
                "predicate": "associated_with",
                "subject": {"category": "gene_set", "id": "SYNTH_SET_B"},
                "object": {"category": "phenotype", "id": "IBD"},
                "collection": "cfde-inc-v2",
                "trait_group": "portal",
                "source": "lincs",
            },
            {
                "claim_id": "dapper:Claim.SYNTHM02",
                "method": "pigean",
                "predicate": "member_of",
                "subject": {"category": "gene", "id": "NOD2"},
                "object": {"category": "gene_set", "id": "SYNTH_SET_B"},
                "collection": "cfde-inc-v2",
            },
        ],
    }


def test_sets_per_gene_limit_and_order():
    # Runs after the join test (shared DB); adds a second, weaker set for NOD2.
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
        body = client.post("/v1/graphql", json={"query": q, "variables": {"n": n}}).json()
        assert "errors" not in body, body.get("errors")
        rows = body["data"]["pigeanValidationChallenge"]["rows"]
        assert [r["geneAssociation"]["gene"]["symbol"] for r in rows] == ["NOD2"]
        return [s["geneSetAssociation"]["geneSet"]["name"] for s in rows[0]["sets"]]

    # Both sets, strongest (betaUncorrected 2.5 > 0.9) first.
    assert set_names(5) == ["SYNTH_SET", "SYNTH_SET_B"]
    # The limit is per gene, applied after the within-gene ordering.
    assert set_names(1) == ["SYNTH_SET"]


def test_gene_set_entity_is_traceable():
    """The traceable-IDs example: claim -> geneSetRefs / dapperGeneSetId ->
    full GeneSet entity record."""
    client.post("/v1/claims", json=_bundle())
    client.post("/v1/claims", json=_membership_bundle())

    # ClaimEnvelope surfaces the structured triple (dapper:GeneSet subject)
    # and marks the ref resolved.
    env = client.post("/v1/graphql", json={"query": """
      { claim(id: "dapper:Claim.SYNTHS01") {
          triple { subject predicate object }
          provenanceRefs { geneSetRefs resolution } } }
    """}).json()["data"]["claim"]
    assert env["triple"] == {
        "subject": "dapper:GeneSet.SYNTHSET01",
        "predicate": "associated_with",
        "object": "IBD",
    }
    assert env["provenanceRefs"]["geneSetRefs"] == ["dapper:GeneSet.SYNTHSET01"]
    assert env["provenanceRefs"]["resolution"] == "RESOLVED"

    # The GeneSet view resolves its dapper ID from the entity index.
    gs = client.post("/v1/graphql", json={"query": """
      { geneSet(name: "SYNTH_SET", collection: "cfde-inc-v2") { name dapperGeneSetId } }
    """}).json()["data"]["geneSet"]
    assert gs["dapperGeneSetId"] == "dapper:GeneSet.SYNTHSET01"

    # The ID dereferences to the verbatim entity record, via GraphQL and REST.
    obj = client.post("/v1/graphql", json={"query": """
      { dapperObject(id: "dapper:GeneSet.SYNTHSET01") }
    """}).json()["data"]["dapperObject"]
    assert obj["class_name"] == "GeneSet"
    assert obj["members"] == ["NOD2"]
    rest = client.get("/v1/objects/dapper:GeneSet.SYNTHSET01")
    assert rest.status_code == 200
    assert rest.json()["name"] == "SYNTH_SET"


def test_rest_list_and_search():
    client.post("/v1/claims", json=_bundle())
    client.post("/v1/claims", json=_membership_bundle())

    listed = client.get("/v1/claims", params={"limit": 10}).json()
    assert listed["total"] >= 3
    by_id = {c["claim_id"]: c for c in listed["claims"]}
    gene_claim = by_id["dapper:Claim.SYNTHG01"]
    assert gene_claim["triple"] == {"subject": "NOD2", "predicate": "associated_with", "object": "IBD"}
    assert gene_claim["times_deposited"] >= 2  # re-deposited by earlier tests
    assert gene_claim["url"] == "/v1/claims/dapper:Claim.SYNTHG01"

    member_only = client.get("/v1/claims", params={"predicate": "member_of"}).json()
    assert member_only["claims"]
    assert all(c["triple"]["predicate"] == "member_of" for c in member_only["claims"])

    # entity matches either side of the triple.
    nod2 = client.get("/v1/claims/search", params={"entity": "NOD2"}).json()
    ids = {c["claim_id"] for c in nod2["claims"]}
    assert {"dapper:Claim.SYNTHG01", "dapper:Claim.SYNTHM01"} <= ids

    # A gene-set NAME finds claims encoded against its dapper:GeneSet ID.
    by_set = client.get("/v1/claims/search", params={"entity": "SYNTH_SET"}).json()
    ids = {c["claim_id"] for c in by_set["claims"]}
    assert {"dapper:Claim.SYNTHS01", "dapper:Claim.SYNTHM01"} <= ids

    # Free-text search over the proposition statement.
    text = client.get("/v1/claims/search", params={"q": "member of the gene set"}).json()
    assert "dapper:Claim.SYNTHM01" in {c["claim_id"] for c in text["claims"]}

    # Pagination is deterministic (claim_id order) and reports the total.
    page = client.get("/v1/claims", params={"limit": 1, "offset": 1}).json()
    assert page["total"] == listed["total"]
    assert len(page["claims"]) == 1
    assert page["claims"][0]["claim_id"] == listed["claims"][1]["claim_id"]


def _second_opinion_bundle():
    """A second claim assessing the SAME proposition (SYNTHS01), deposited
    WITHOUT an index hint — the fan-in the proposition/claim split exists
    for, and the auto-registration path for non-edge claims (e.g. tiers)."""
    return {
        "objects": [
            {
                "class_name": "Claim",
                "id": "dapper:Claim.SYNTHS01B",
                "proposition": "dapper:Proposition.SYNTHS01",
                "statement": "A second synthetic assessment of SYNTH_SET's association with IBD.",
            }
        ]
    }


def test_proposition_search_and_claim_fanin():
    client.post("/v1/claims", json=_bundle())
    client.post("/v1/claims", json=_membership_bundle())
    assert client.post("/v1/claims", json=_second_opinion_bundle()).status_code == 200

    # Same search language as /v1/claims/search, over propositions.
    found = client.get("/v1/propositions/search", params={"entity": "SYNTH_SET"}).json()
    by_id = {p["proposition_id"]: p for p in found["propositions"]}
    assert {"dapper:Proposition.SYNTHS01", "dapper:Proposition.SYNTHM01"} <= set(by_id)
    assert by_id["dapper:Proposition.SYNTHS01"]["n_claims"] == 2
    assert by_id["dapper:Proposition.SYNTHS01"]["triple"]["predicate"] == "associated_with"

    # Detail: every claim assessing the proposition, hinted or not.
    detail = client.get("/v1/propositions/dapper:Proposition.SYNTHS01").json()
    briefs = {c["claim_id"]: c for c in detail["claims"]}
    assert set(briefs) == {"dapper:Claim.SYNTHS01", "dapper:Claim.SYNTHS01B"}
    assert {s["metric"] for s in briefs["dapper:Claim.SYNTHS01"]["scores"]} == {"beta", "beta_uncorrected"}
    assert briefs["dapper:Claim.SYNTHS01B"]["scores"] == []

    # Same fan-in over GraphQL, with full envelopes.
    gql = client.post("/v1/graphql", json={"query": """
      { proposition(id: "dapper:Proposition.SYNTHS01") {
          triple { subject predicate object }
          claims { id scores { metric } } } }
    """}).json()["data"]["proposition"]
    assert gql["triple"]["object"] == "IBD"
    assert {c["id"] for c in gql["claims"]} == {"dapper:Claim.SYNTHS01", "dapper:Claim.SYNTHS01B"}

    # Browse endpoint with a predicate filter.
    listed = client.get("/v1/propositions", params={"predicate": "member_of"}).json()
    assert listed["propositions"]
    assert all(p["triple"]["predicate"] == "member_of" for p in listed["propositions"])

    # Statement text search.
    text = client.get("/v1/propositions/search", params={"q": "member of the gene set"}).json()
    assert "dapper:Proposition.SYNTHM01" in {p["proposition_id"] for p in text["propositions"]}
