"""?async=true: a GraphQL request or a query template run answers 202 with a job at once, and the job's result is
the synchronous response, byte for byte."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from service import jobs
from service.app import app
from tests.test_contained_in import GENES, _published

COUNTS = ("timesDeposited", "timesAsserted", "firstDepositedAt", "lastDepositedAt", "firstAssertedAt", "lastAssertedAt")


def _same(a, b):
    """Two responses alike but for the counts of how often a claim was asserted (each serve asserts it)."""
    def scrub(o):
        if isinstance(o, dict):
            return {k: scrub(v) for k, v in o.items() if k not in COUNTS}
        if isinstance(o, list):
            return [scrub(v) for v in o]
        return o
    if scrub(a.json()) != scrub(b.json()):
        import difflib, json
        raise AssertionError("\n".join(difflib.unified_diff(json.dumps(a.json(), indent=1).splitlines(),
                                                            json.dumps(b.json(), indent=1).splitlines(), lineterm="")))
    return True


def _wait(client, job):
    for _ in range(200):
        r = client.get(f"/v1/jobs/{job['id']}/result")
        if r.status_code != 202:
            return r
        time.sleep(0.05)
    raise AssertionError("the job did not finish")


def test_async_requests_answer_what_sync_requests_answer(tmp_path, monkeypatch):
    _published(tmp_path, monkeypatch, "sql")
    client = TestClient(app)
    query = {"query": "{ " + GENES + " { rows { subject { id } metric(name: \"combined\") } } }"}
    sync = client.post("/v1/graphql", json=query)
    submitted = client.post("/v1/graphql?async=true", json=query)
    assert submitted.status_code == 202 and submitted.headers["location"].endswith(f"/v1/jobs/{submitted.json()['id']}")
    result = _wait(client, submitted.json())
    assert result.status_code == 200 and result.content == sync.content
    state = client.get(f"/v1/jobs/{submitted.json()['id']}").json()
    assert state["status"] == "done" and state["http_status"] == 200 and state["result_url"].endswith("/result")

    run = {"variables": {"trait": "FIXPHENO", "library": "fixture-v1", "topGenes": 3, "setsPerGene": 2}}
    sync = client.post("/v1/collections/pigean/queries/top-genes-with-sets:run", json=run)
    submitted = client.post("/v1/collections/pigean/queries/top-genes-with-sets:run?async=true", json=run)
    assert submitted.status_code == 202 and submitted.json()["request"]["query"] == "top-genes-with-sets"
    assert _same(_wait(client, submitted.json()), sync)

    # A failing request fails the same way in the background.
    bad = {"query": "{ rows(collection: \"pigean\", kind: \"nope\", orderBy: {metric: \"x\"}) { rows { kind } } }"}
    failed = _wait(client, client.post("/v1/graphql?async=true", json=bad).json())
    assert failed.status_code == 400 and failed.content == client.post("/v1/graphql", json=bad).content
    assert client.get("/v1/jobs/unknown").status_code == 404


def test_waiting_jobs_are_bounded(monkeypatch):
    monkeypatch.setattr(jobs, "MAX_PENDING", 0)
    r = TestClient(app).post("/v1/graphql?async=true", json={"query": "{ health { status } }"})
    assert r.status_code == 429 and r.headers["retry-after"]
