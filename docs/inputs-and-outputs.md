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
> GraphQL does have one *internal* side effect, by design (2026-11-09, §3.5):
> serving a result MINTS its claim — the service synthesizes the claim from
> the result's index row plus the method's claim template, verifies it, and
> deposits it to justify the response. Clients supply nothing; the data
> already implied the claim; content addressing makes the minted document
> identical whenever it is minted.

## 1. The flow, end to end

```
PIGEAN result files (S3)                      other methods (EAGGL, ...)
        │                                              │
        ▼                                              ▼
bulk load (scripts/load_pigean.py)          external depositors (toolkit
  DATA PLANE ONLY: index rows (each          tier claims, other methods)
  naming its claim's precomputed address),         │
  Propositions, ClaimScores, Evidence +            ▼  INPUT (the only external
  ValueLocators, GeneSet entities, Files,      POST /v1/claims    write path)
  Activities — NO claims                           │
        │                                          │
        ▼                                          ▼
STORAGE: dapper_objects (authoritative, verbatim, times_seen-counted)
         pigean_* index rows (each with claim_id + synthesis_inputs), rosters,
         claim_memberships, gene_set_entities, object_refs, claims registry
         (derived index tables — internal, rebuildable, NOT an API)
        │
        ├──▶ OUTPUT 1: GraphQL /v1/graphql — the SCIENTIFIC QUERY surface
        │     typed metrics, filters, ordering, joins, persisted queries;
        │     serving a result MINTS its claim (§3.5)
        │
        └──▶ OUTPUT 2: REST — OBJECTS ONLY
              GET /v1/objects/{id} (resolver), GET /v1/objects (list),
              GET /v1/objects/search; plus vocabulary and docs
```

## 2. Input: deposit is REST-only, on purpose

`POST /v1/claims` takes a bundle of DAPPER objects plus `index_hints` and
stores idempotently by content address (re-deposit of identical content →
`exists`, `times_seen` +1). External deposits are asserted by their
depositor, so deposited claims land immediately.

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

### 3.5 Claims are minted when queried (2026-11-09, Chase)

Bulk loads build the **data plane** only — the index tables plus the dapper
objects that are *observations, not assessments* (Propositions, ClaimScores,
EvidenceItems + ValueLocators, GeneSet entities, Files, Activities). **No
claims are stored at load.** Each index row instead carries the content
address of the claim it implies (computed with the method's claim template),
its `proposition_id`, and its `synthesis_inputs`: the reference IDs the claim
needs that the row's own values cannot recompute — the evidence items (whose
byte offsets exist only at load time), the provenance activity / agent / file,
and the gene-set entity. A row and the claim it names therefore always move
together; no later load, deposit, or lookup table can change what an existing
row synthesizes. Roster claims have their own index table keyed by the
claim's content address, so a roster row can never be overwritten.

When the scientific query surface serves a result, the service
**synthesizes** that row's claim — a pure function of the row + the shared
template (`service/pigean_template.py`, imported by both loader and service)
— **verifies** that its content address equals the row's precomputed one,
and on first serve deposits it: *minting the claim that justifies the query
response*. Every later serve from a live index row re-synthesizes,
re-verifies against the stored document, and counts a re-assertion: for claims, `times_seen` is an
**assertion counter**, a real usage signal. Serving a result row asserts its
claim whether or not the client selected the claim envelope: the row IS the
claim's content.

Rules that keep this sound:

- **The digest check is the guardrail, on every serve from a live index
  row.** Such a claim is served only if its synthesis matches the row's precomputed address and — once
  stored — the stored document exactly. Template drift, a stale vendored copy,
  a tampered index row, or a forged document at the row's address is refused
  loudly (a GraphQL error), never served. Deposit verifies local-dev content
  addresses (the id must be the digest of the content), refuses a different
  document at an existing id, reserves implied addresses, and may never
  overwrite a loaded, template-backed index row; the loader refuses to load
  over a stored document that differs from what it addresses identically.
  `scripts/audit_objects.py` re-verifies every claim the data implies across
  the whole store.
- **Assertions are written after the response, in one sorted batch.** While
  a GraphQL response is resolved, serving only *records* mints and
  re-assertions; once the whole response has succeeded they are written in
  one short batch in claim-id order (retried on lock conflicts). Every
  request takes its row locks in the same order — concurrent requests
  serving overlapping claims cannot deadlock — and a failed response writes
  nothing. Arguments are bounded (rows ≤ 500, topGenes ≤ 200, setsPerGene ≤
  50) and a request may assert at most 2,000 claims (aliases included).
- **A load replaces the (phenotype, collection) slices it covers**, and
  deletes only the loader's own rows (template-backed rows and roster
  edges). The slice delete never removes a REST-indexed row, but a loaded
  row written at the same key (phenotype, collection, and gene, gene set, or
  gene + set) supersedes it. The deposited claim stays stored and resolvable
  by id; it is just no longer indexed under that key. No stale loader-written
  association rows or membership edges survive a reload.
- **Superseded rosters stop being implied.** Superseded roster rows stay
  (keyed by their immutable claim address), but a roster is implied only
  while an edge cites it. One that was never served is never minted. One
  already minted stays a stored claim: `claim(id)` and `proposition(id)`
  serve it from its stored document (not re-synthesized) and count each
  serve. The same holds for claims whose association rows a reload replaced.
  Membership edges are scoped to the phenotype whose PIGEAN run observed them
  ('' for collection-wide deposits).
- **The GeneSet an association names depends on the load's inputs and,
  only for a (phenotype, collection) the load brings no membership for, on
  that slice's live roster.**
  - When a load brings that phenotype's membership file, the file is
    authoritative: a set it lists gets the member-ful entity, and a set it
    omits gets the member-less one. An identical reload names the identical
    subject.
  - When a load brings no membership for that (phenotype, collection), a
    set keeps the member-ful entity its live roster names (those edges
    stay). Otherwise it gets the member-less entity.
  - Another phenotype's membership never decides the subject: its
    member-less entity carries no membership provenance (exactly what a
    membership-less load mints). A membership file whose phenotype the
    gene-set file lacks is refused (`--membership-stats` / `--no-membership`).
- **Phenotypes are KPN traits.** Result files key phenotypes by legacy
  portal / PIGEAN keys (`IBD`); the loader resolves them through the KPN
  trait registry (pinned release of broadinstitute/kpn-data-models, loaded by
  `scripts/load_kpn_traits.py`), so propositions name the canonical trait
  (`KPN.TRAIT:0000505`) and statements its name. Every query accepts any of a
  trait's keys, and GraphQL `Phenotype` exposes the trait's ontology mappings
  (EFO:0003767, MONDO:0005265, MESH, DOID, OMIM, ...).
- **Numbers are normalized before hashing.** Result files carry some
  integer-typed values (1,149 `"beta": 0` in IBD); the index returns floats.
  The template hashes floats on both sides; ValueLocators keep the file's
  exact bytes ("0").
- The query event never enters the claim document (that would change its
  content address per query); assertion history lives beside it
  (`times_seen`, `first/last_seen_at`).
- GET never mints. Dereferencing an implied claim that nothing has served
  or deposited returns 404 "not yet asserted". A claim exists in the
  repository only because a GraphQL response served it or a depositor
  asserted it via `POST /v1/claims`. At an implied address, a deposit is
  accepted only if it equals the template's document.
- **Membership claims are one roster claim per gene set** (not one per
  (gene, set) pair): proposition `(dapper:GeneSet, has_members,
  dapper:GeneSetCollection)`, the exact roster pinned by the entity's
  content-addressed `members` list — the GMT model, where the set entry IS
  the result row. The per-pair `claim_memberships` index still drives joins;
  all of a set's pairs point at its one roster claim (143,508 pairs → 3,851
  claims).

## 4. Output 2: REST is objects only (2026-11-09, Chase)

REST answers *identity-shaped questions* — "give me this thing", "what
points at this thing", "which things say X" — over **every** dapper object,
with no endpoint tied to claims or propositions:

| Endpoint | Question it answers |
|---|---|
| `GET /v1/objects/{id}` | Dereference ANY dapper ID (the resolver): consistent presentation for every class; JSON, or a browser landing page whose references are resolver links |
| `GET /v1/objects` | Browse stored objects, optionally one `class_name` |
| `GET /v1/objects/search` | `references=<id>` (objects pointing at it: the claims assessing a proposition, everything about a GeneSet, ...), `q` (statement/name/description text), `match=slot:value` (exact string slot) — **no metric filters** |
| `GET /v1/vocabulary` | What can be asked: categories, predicates, methods, metric semantics, identifier resolution (prefixes + resolver template) |
| `GET /v1/docs`, `/v1/openapi.yaml` | The browsable REST contract |

Why one general surface instead of per-class endpoints: the store is
uniformly *dapper objects addressed by content*, so the questions are too —
resolve an ID, list a class, follow references in either direction. A
reference index (`object_refs`) makes "what points at X" a single generic
filter, which subsumes per-class fan-in endpoints: the claims assessing
proposition P is `search?class_name=Claim&references=P`. New classes (tier
claims, EAGGL factors, mechanisms) need no new endpoints.

These stay REST because their outputs are *shareable* (a resolver URL in
Slack), *tool-friendly* (curl, scripts, notebooks without a GraphQL client),
and metric-free — no score filters or sorts, so they cannot undermine the
constrained-query guarantee (§3). The **dereferenced claim envelope**
(scores, evidence + locators, provenance) and the **proposition fan-in with
full envelopes** live in GraphQL (`claim(id)`, `proposition(id)`), next to
the queries that produce them; GraphQL also mirrors the resolver
(`dapperObject(id)`).

## 5. The rule of thumb

- **Writing?** REST `POST /v1/claims`, always.
- **Asking with a metric?** (filter, sort, top-N, join) GraphQL, always.
- **Asking with an ID, a reference, or text?** REST objects (`/v1/objects`,
  `/search`, `/{id}`) — general across every class, shareable URLs; GraphQL
  mirrors the lookups when you are already there.
- **Result tables?** Internal. No API, ever — they are how queries are fast,
  not what queries are made of.

## 6. Known deviations and futures

- The loader writes the data plane straight to storage, bypassing
  `POST /v1/claims` — it deposits no claims (they are minted on serve), so
  the deposit door stays the only path for *claims* entering from outside.
  Once toolkit minting moves to DAPPER-ID-1 content addresses, the template
  should vendor from the toolkit (pinned), with the digest check as the
  contract between them.
- Deposit auth (`X-API-Key`) is declared in the contract but not yet
  enforced — acceptable for QA, required before prod.
- If a UI someday genuinely needs REST-shaped scientific results (e.g. CSV
  export of a challenge run), the shape that preserves the guarantee is a
  **persisted-query execution endpoint** (`GET /v1/queries/{name}?var=…`,
  running a vetted GraphQL document server-side) — not table access.

## 7. Known v0 limitations (accepted for QA)

Review rounds on 2026-11-09 left these open on purpose: each needs a design
decision or belongs to later hardening, and none can corrupt a stored
document. Every claim served from a live index row is re-synthesized and
checked against its content address. External deposits, and minted claims
whose rows a reload replaced or superseded, are served from the stored
document as stored (a local-dev address was verified when it was deposited
or minted).

- **`geneSet` without `phenotype` mixes observations.** A gene set observed
  under several phenotypes resolves to whichever observation the lookup
  finds first. Pass `phenotype` for a definite answer. A member_of edge
  added by a REST index hint renders the loaded GeneSet entity, not the
  `dapper_id` the hint named.
- **A raw key used before the registry knows it.** A REST deposit that
  indexes a phenotype by a key the KPN trait registry does not yet resolve
  stores rows under that raw key. Once the registry learns the key, queries
  find those rows under every key of the trait, but the rows themselves are
  not rewritten to the canonical id.
- **The trait-release guard proves publication, not provenance.** The
  loader refuses a release file that is modified, untracked, or unpublished.
  Published means a remote whose every fetch and push URL is non-local
  contains the commit in a remote-tracking branch, or advertises a branch
  or tag at it (`git ls-remote`, asked only when no tracking branch
  matches; bounded by a timeout and failing closed). A local-only commit or
  tag, a remote with any local URL, and a bare `FETCH_HEAD` checkout do not
  count: clone the release (a tag-pinned or shallow clone works) or add it
  as a remote. Remote-tracking branches are trusted as last fetched, so a
  branch fetched from elsewhere before `git remote set-url` still counts.
  That proves the commit was published somewhere, not which repository, so
  `source_url` names the upstream repository by convention. A load forced with `--allow-unpublished` is recorded with an
  `unpublished:` source URL (and no commit when the file is not the
  committed blob), and `/v1/vocabulary` reports `published: false`.
- **Registry replacement checks result keys, not every use.** The guard
  refuses a release under which a stored result key (in the index tables or
  membership edges) would resolve differently: its trait is dropped, or the
  key is unmapped or moved to another trait. It does not inspect phenotype
  ids embedded in stored documents.
- **Membership edges are trusted to cite rosters.** A deposited membership
  edge that cites a roster claim is accepted without checking that the
  roster lists the gene. `scripts/audit_objects.py` reports an edge citing a
  loader-written roster — a loaded roster claim, a deposited claim on a
  loaded roster's proposition, or a has_members claim about a loaded
  GeneSet — when that roster is for another gene set or collection, or does
  not list the gene. Other deposited rosters' members are not checked (no
  fixed identifier form), only counted.
- **Deposit is check-then-write.** Two concurrent deposits at one address
  can both pass the existence check.
  - If the documents differ, the re-read after the upsert catches the
    loser: it gets a 400, and its transaction, including the upsert's
    count, is rolled back.
  - If the documents are the same, both are counted, but both report
    status "created".
- **Text matching differs by backend.**
  - `q` uses LIKE, where trailing spaces are significant on both backends.
    SQLite `lower()` folds ASCII only, while MySQL `LOWER()` folds Unicode.
  - `match=slot:value` compares with `=`. MySQL ignores trailing spaces
    there (PAD SPACE), so `match=subject_entity:NOD2%20` matches `NOD2`.
    SQLite does not.
- **Local-dev addresses are byte-exact over canonical JSON.** A client that
  re-serializes numbers (e.g. JavaScript turning `1.0` into `1`) changes the
  digest, so a re-deposit of the same claim is refused. DAPPER-ID-1 number
  canonicalization (Task 5) resolves this.
- **Envelope reads are not batched.** Each served row costs about six
  SELECTs. A request is bounded at 25,000 queries and 2,000 assertions;
  batching is a performance follow-up, not a correctness gap.
