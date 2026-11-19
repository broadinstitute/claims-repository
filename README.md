# claims-repository

Independently installable service that owns deposited DAPPER Claims: deposit (structural and
identifier checks in v0; full profile validation is Task 5), retrieval
by `dapper:Claim` ID, and a typed GraphQL query surface over them (SIIPR build-plan Tasks 5–7).
Depends on broadinstitute/dapper (pinned profile `0.2.0-a1`); provenance resolution for referenced
`dapper:File`/`dapper:GeneSet` IDs belongs to broadinstitute/provenance-indexer.

**Contract-first:** the v0 contract is the current deliverable; the service implements it.

**Queries are collection query templates.** Every result query is a registered collection's query
template, run with `runQuery` (GraphQL) or `POST /v1/collections/{c}/queries/{name}:run`. To find out
what you can run, start at `GET /v1/queries` (GraphQL: `queries`): each query with its params and the
values they take, the rows it returns, the claims it serves, and a request that runs as given.
`GET /v1/collections/{c}/dimensions/{d}` (GraphQL: `dimensionValues`) lists every value of a param
such as `trait`. There are no per-method GraphQL fields ([docs/collections.md](docs/collections.md)).

- [api/graphql/core.graphql](api/graphql/core.graphql) — method-agnostic schema: claim and proposition
  envelopes, objects, provenance refs. What a trait means comes from its collection, through
  `dimensionValues`.
- [api/graphql/collections.graphql](api/graphql/collections.graphql) — `queries`, `dimensionValues`,
  `collections`, `collection`, `collectionQueries` and `runQuery`.
- [api/graphql/examples/](api/graphql/examples/) — the example queries the GraphiQL explorer opens
  on, one tab each with its variables (what can I run, find a trait, the validation challenge, top
  genes only, collections, one claim).
- [api/openapi.yaml](api/openapi.yaml) — REST: health, vocabulary, deposit (`POST /v1/deposits`: any DAPPER class, validated; needs a key),
  and objects only — the `/v1/objects/{id}` resolver, `/v1/objects` list, `/v1/objects/search`
  — plus the `/v1/graphql` endpoint shell. Browsable at `/v1/docs`. Deposit example:
  [api/examples/](api/examples/).
- [docs/inputs-and-outputs.md](docs/inputs-and-outputs.md) — what goes in and out and why:
  deposit is the only external write; metric questions are GraphQL; **claims are minted when
  queried** (synthesized from index rows + the method template, verified on every serve from a live index row).
- [docs/query-graph-design.md](docs/query-graph-design.md) — execution semantics, storage design
  (MySQL, reveal-style), QA deployment, and open Day-2 decisions.
- [docs/collections.md](docs/collections.md) — collections as the general unit, so any method
  can register files, claim templates and queries without service code (phases 1–4 built; §12)
- [docs/admin-console.md](docs/admin-console.md) — the admin console (`/admin` in [frontend/](frontend/), the
  repository's Next.js frontend on Vercel): collections, claims, tables and API activity, plus a local-only flow to register a collection, behind Google sign-in for approved admins,
  reading this service's `/v1/admin/api/*` through signed server-to-server assertions.

All fixtures are synthetic: `.SYNTH…` IDs are placeholders for computed DAPPER content addresses,
and no example value is a scientific result.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/python scripts/load_kpn_traits.py
```

```bash
.venv/bin/python scripts/load_pigean.py
```

```bash
.venv/bin/python -m uvicorn service.app:app --port 8100
```

A whole PIGEAN aggregator run (every phenotype of one gene-set library) loads from a run specification in
`loads/`, which pins the upstream location, the run and library descriptions, and the row thresholds
(`min_combined`, `min_beta_uncorrected`: rows at or below a threshold are not ingested). It downloads each
phenotype's files with the aws CLI, keeps a receipt, and resumes where it stopped. Phenotype folders the trait
registry does not know are skipped, or loaded under their folder names when the specification says so. Try it on
a scratch database first:

```bash
.venv/bin/python scripts/load_pigean_run.py loads/pigean-paper-regression-2.mouse_msigdb.json --db /tmp/scratch.db --only IBD,AD
```

Deposits need a key ([docs/inputs-and-outputs.md](docs/inputs-and-outputs.md#deposit-keys)); issue one into the
local database and send it as `X-API-Key`:

```bash
.venv/bin/python scripts/api_key.py issue "local development"
```

Collections (the general path, [docs/collections.md](docs/collections.md)): publish the PIGEAN
collection from the toolkit, then ask it about itself and run its query.

```bash
.venv/bin/python scripts/publish_collection.py ../kpn-dapper-toolkit/collections/pigean \
  --release ../kpn-dapper-toolkit/collections/pigean/releases/2026.11.0.yaml
```

A collection is identified by the UUID in its manifest. The short name in these URLs (`pigean`) is its
handle, and it resolves to the UUID. Responses identify the collection by that UUID (`id`), with the
handle as `key`.

```bash
curl -s localhost:8100/v1/collections/pigean
```

```bash
curl -s -X POST localhost:8100/v1/collections/pigean/queries/top-genes-with-sets:run \
  -H 'content-type: application/json' -d '{"variables": {"trait": "IBD", "priorMax": 0.5, "topGenes": 5}}'
```

`load_kpn_traits.py` loads the KPN trait registry from a pinned release of
[broadinstitute/kpn-data-models](https://github.com/broadinstitute/kpn-data-models) (default: the
local clone at `~/src/research/portal-data-models`, release `v0.0.2`): canonical `KPN.TRAIT` IDs,
the legacy / PIGEAN keys they replace (`IBD` → `KPN.TRAIT:0000505`), and their ontology mappings.
Load it first so claims name the trait. The PIGEAN loader turns the PIGEAN IBD result files (local copies under
`kpn-dapper-toolkit/data/pigean/IBD/`) into the **data plane** — index rows, propositions, scores,
evidence + byte-span locators, gene-set entities (172,700 DAPPER objects) — in a local SQLite DB
(`claims.db`; QA runs the same code on Aurora MySQL). It stores **no claims**: each of the 27,172
claims the data implies (18,321 gene, 5,000 gene-set, 3,851 gene-set rosters) is minted the first
time GraphQL serves it. IDs are deterministic local-dev digests, not DAPPER-ID-1 content
addresses; real minting belongs to the toolkit. Then http://localhost:8100/v1/graphql is the
GraphiQL explorer (opened on the example queries in `api/graphql/examples/`), http://localhost:8100/v1/docs the REST
contract. `scripts/audit_objects.py` verifies the whole store.
Tests: `.venv/bin/python -m pytest tests/`.
