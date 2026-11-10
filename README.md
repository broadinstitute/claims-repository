# claims-repository

Independently installable service that owns deposited DAPPER Claims: deposit (structural and
identifier checks in v0; full profile validation is Task 5), retrieval
by `dapper:Claim` ID, and a typed GraphQL query surface over them (SIIPR build-plan Tasks 5–7).
Depends on broadinstitute/dapper (pinned profile `0.2.0-a1`); provenance resolution for referenced
`dapper:File`/`dapper:GeneSet` IDs belongs to broadinstitute/provenance-indexer.

**Contract-first:** the v0 contract is the current deliverable; the service implements it.

**Design centerpiece — method modules:** each KPN method's claim template (kpn-dapper-toolkit)
ships its own GraphQL schema module alongside its claim generation; this service is a generic host
that composes `core + methods/*` at startup and knows nothing method-specific. See
[docs/method-modules.md](docs/method-modules.md).

- [api/graphql/core.graphql](api/graphql/core.graphql) — method-agnostic schema: nodes, claim
  envelopes, provenance refs, association interfaces.
- [api/graphql/methods/pigean.graphql](api/graphql/methods/pigean.graphql) — the PIGEAN module:
  typed metrics, filters, order enums, and the `pigeanValidationChallenge` root field.
- [api/graphql/queries/](api/graphql/queries/) — the persisted Day-10 IBD validation-challenge
  query, its researcher-editable variables, and a synthetic fixture response for UI mocks.
- [api/openapi.yaml](api/openapi.yaml) — REST: health, vocabulary, deposit (`POST /v1/claims`),
  and objects only — the `/v1/objects/{id}` resolver, `/v1/objects` list, `/v1/objects/search`
  — plus the `/v1/graphql` endpoint shell. Browsable at `/v1/docs`. Deposit example:
  [api/examples/](api/examples/).
- [docs/inputs-and-outputs.md](docs/inputs-and-outputs.md) — what goes in and out and why:
  deposit is the only external write; metric questions are GraphQL; **claims are minted when
  queried** (synthesized from index rows + the method template, verified on every serve from a live index row).
- [docs/query-graph-design.md](docs/query-graph-design.md) — execution semantics, storage design
  (MySQL, reveal-style), QA deployment, and open Day-2 decisions.
- [docs/admin-console.md](docs/admin-console.md) — the admin console (`/admin` in [frontend/](frontend/), the
  repository's Next.js frontend on Vercel): claims, templates, tables and API activity, behind Google sign-in for approved admins,
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
GraphiQL explorer (pre-loaded with an IBD example), http://localhost:8100/v1/docs the REST
contract. `scripts/audit_objects.py` verifies the whole store.
Tests: `.venv/bin/python -m pytest tests/`.
