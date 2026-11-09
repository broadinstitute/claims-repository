# Claims repository: inputs and outputs

Status: design justification (2026-11-09, per Chase) · Companions:
[query-graph-design.md](query-graph-design.md) (execution semantics, storage),
[method-modules.md](method-modules.md) (per-method GraphQL modules),
[claim-encoding.drawio](claim-encoding.drawio) (how claims are structured).

This document answers one question: **what goes into this service, what comes
out, and why each path is shaped the way it is.** It also corrects the one
easy misreading of the architecture up front:

> **GraphQL never accepts writes.** The GraphQL schema has no `Mutation`
> type (see `api/graphql/core.graphql`): clients cannot put content into the
> store through it — the only external write path is REST `POST /v1/claims`.
> GraphQL does have one *internal* side effect, by design (2026-11-09, lazy
> assertion, §3.5): serving a result MATERIALIZES its precomputed claim —
> the service minting the claim that justifies the response. Clients supply
> nothing; the data already implied the claim; content addressing makes the
> materialized document identical whenever it is minted.

## 1. The flow, end to end

```
PIGEAN result files (S3)                      other methods (EAGGL, ...)
        │                                              │
        ▼                                              ▼
kpn-dapper-toolkit claim templates  ──────  (same template pattern)
  rows → DAPPER documents:
  Proposition + Claim + ClaimScores + EvidenceItems + ValueLocators
  (+ GeneSet/Collection entities, Files, Activities, Agents)
        │
        ▼  INPUT (the only write path)
POST /v1/claims  ─ REST, bundle of objects + index_hints, idempotent
        │
        ▼
STORAGE: dapper_objects (authoritative, verbatim, mint-counted)
         claims / pigean_* / claim_memberships / gene_set_entities
         (derived index tables — internal, rebuildable, NOT an API)
        │
        ├──▶ OUTPUT 1: GraphQL /v1/graphql — the SCIENTIFIC QUERY surface
        │     typed metrics, filters, ordering, joins, persisted queries
        │
        └──▶ OUTPUT 2: REST — the IDENTITY & CONTENT surface
              claim/proposition get+list+search, the /v1/objects resolver,
              vocabulary, docs
```

## 2. Input: deposit is REST-only, on purpose

`POST /v1/claims` takes a bundle of DAPPER objects plus `index_hints` and
stores idempotently by content address (re-deposit of identical content →
`exists`, mint counter +1).

Why the write path is REST and not GraphQL:

- **One auditable door.** Writes are the security- and integrity-sensitive
  path (planned `X-API-Key` guards deposit only; reads stay open on QA). One
  endpoint with one request shape is easy to log, gate, and reason about.
- **Deposit is a transfer, not a query.** A bundle is a document upload with
  all-or-nothing semantics. GraphQL mutations would add nothing but a second
  serialization of the same bundle — and would blur the guarantee that the
  query surface cannot change state.
- **`index_hints` keep indexing explicit.** The depositor declares each
  claim's graph edge (subject/predicate/object/method/collection); the
  service never parses proposition text to guess at structure. That contract
  belongs next to the payload, in the deposit request.

## 3. Output 1: GraphQL is the scientific query surface

Everything that asks a *result-table-shaped question* — filter by a metric,
order by a metric, take top-N, join edges — goes through
`POST /v1/graphql`:

- per-method typed fields (`pigeanGeneAssociations`, `beta_uncorrected`
  ordering, …) generated from the method's claim template, so claim
  generation and query surface share one source of truth;
- the persisted CFDE validation-challenge query, where researchers edit
  **variables, never query text** — the constrained-query guarantee;
- no cross-method order-by exists in the schema, so unlike scores **cannot**
  be blended, by construction.

### Why there is no REST API over PIGEAN results / the index tables

The `pigean_*` tables are an **internal index** — a derived, rebuildable
projection of the authoritative DAPPER documents, shaped for fast
filter/sort/join. Exposing them over REST would mean:

1. **A second query language.** Metric filters, ordering, limits, and joins
   over REST query params is exactly the bespoke pattern language we decided
   not to build and maintain (the 2025-11-06 decision that made the wire
   format GraphQL). Every capability would exist twice and drift.
2. **Losing the constrained-query guarantee.** The GraphQL schema enumerates
   what can be asked: closed predicate set, module-private order enums,
   documented execution semantics (anchor-limit-before-join, deterministic
   ties, empty-vs-missing). Ad-hoc REST table access has none of that.
3. **Coupling consumers to storage.** The index tables' columns are an
   implementation detail of this service (SQLite locally, Aurora in QA). The
   moment a UI reads them directly — or through a thin REST mirror — schema
   evolution breaks consumers instead of resolvers.

So: **if the question mentions a metric, it is a GraphQL question.** This is
the invariant to keep as EAGGL and later methods arrive.

### 3.5 Lazy assertion: claims mint when queried (2026-11-09, Chase)

Bulk loads populate the **data plane** eagerly — index tables plus the
dapper objects that are *observations, not assessments* (Propositions,
ClaimScores, EvidenceItems + ValueLocators, GeneSet entities, Files,
Activities). **Claims are not deposited at load.** They are precomputed
(content addressing makes their IDs knowable in advance) and staged in
`pending_claims`; the first time the scientific query surface serves a
result, the service materializes its claim into `dapper_objects` and the
claims registry — *minting the claim to justify the query response*. Every
subsequent serve counts as a re-assertion: `times_seen` is an **assertion
counter**, a real usage signal.

Rules that keep this sound:

- The query event never enters the claim document (that would change its
  content address per query); assertion history lives beside it
  (`times_seen`, `first/last_seen_at`).
- Eager and lazy minting are byte-identical — same row + same template
  version → same document — so materialization timing carries no meaning
  risk. (Guardrail once templates vendor into the service: golden tests
  that toolkit-minted and query-minted claims agree exactly.)
- REST never materializes: dereferencing a precomputed-but-unasserted claim
  returns 404 "not yet asserted", so existence in the repository means *some
  query was actually stood behind*. External deposits via REST are asserted
  by their depositor and land immediately, as before.
- **Membership claims are one roster claim per gene set** (not one per
  (gene, set) pair): the proposition's subject is the content-addressed
  GeneSet entity, whose hashable `members` list pins the exact roster —
  matching the GMT model where the set entry IS the result row. The
  per-pair `claim_memberships` index still drives joins; all of a set's
  pairs point at its one roster claim (143,508 pairs → 3,851 claims).
  Known trade-off: proposition search by gene symbol no longer surfaces
  membership claims (the genes live in the roster, not the triple);
  memberships remain queryable via `Gene.memberships` and the GeneSet
  entity.

## 4. Output 2: REST is the identity and content surface

REST answers *identity-shaped questions* — "give me this thing", "which
things mention X" — where shareable URLs and curl-ability matter more than
typed metrics:

| Endpoint | Question it answers |
|---|---|
| `GET /v1/objects/{id}` | Dereference ANY dapper ID (the resolver; JSON or browser landing page; consistent presentation for every class) |
| `GET /v1/claims/{id}` | One claim's full envelope: triple, scores, evidence + locators, provenance refs, mint count |
| `GET /v1/claims` + `/v1/claims/search` | Browse/search claims by proposition content: entity (either side of the triple, gene-set names expand to dapper IDs), predicate, statement text — **not metrics** |
| `GET /v1/propositions*` | Same search language one level up, plus the fan-in view: every claim assessing one statement |
| `GET /v1/vocabulary` | What can be asked: categories, predicates, methods, metric semantics, identifier resolution (prefixes + resolver template) |
| `GET /v1/docs`, `/v1/openapi.yaml` | The browsable REST contract |

These exist in REST because their outputs are *shareable* (a resolver URL in
Slack), *tool-friendly* (curl, scripts, notebooks without a GraphQL client),
and metric-free — they never filter or sort by a score, so they cannot
undermine the constrained-query guarantee. GraphQL mirrors the same lookups
(`claim(id)`, `proposition(id)`, `dapperObject(id)`) so UI code never has to
mix protocols mid-query.

The one REST search capability that overlaps GraphQL — entity/text search —
is deliberately **content search, not scientific query**: it matches triples
and statements, returns summaries with links, and offers no metric filters.
Adding metric parameters to it is the line not to cross (§3).

## 5. The rule of thumb

- **Writing?** REST `POST /v1/claims`, always.
- **Asking with a metric?** (filter, sort, top-N, join) GraphQL, always.
- **Asking with an ID or a statement?** REST works and gives you shareable
  URLs; GraphQL mirrors it when you are already there.
- **Result tables?** Internal. No API, ever — they are how queries are fast,
  not what queries are made of.

## 6. Known deviations and futures

- The local loader (`scripts/load_pigean.py`) writes straight to storage,
  bypassing `POST /v1/claims` — a bootstrap convenience that mirrors deposit
  semantics (same upsert + mint counting). Once toolkit minting moves to
  DAPPER-ID-1 content addresses, bulk loads should go through deposit so
  validation and provenance land in one place.
- Deposit auth (`X-API-Key`) is declared in the contract but not yet
  enforced — acceptable for QA, required before prod.
- If a UI someday genuinely needs REST-shaped scientific results (e.g. CSV
  export of a challenge run), the shape that preserves the guarantee is a
  **persisted-query execution endpoint** (`GET /v1/queries/{name}?var=…`,
  running a vetted GraphQL document server-side) — not table access.
