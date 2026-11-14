# Claims repository: graph-query contract design (v0)

Status: draft for Day-2 review · Scope: build-plan Tasks 5 (deposit/get) and 7 (graph-query
contract + fixtures). Consumer: Alex's CFDE validation-challenge UI.

> **Decision update (2025-11-06):** the wire format is **GraphQL**, composed from per-method schema
> modules shipped by claim templates — see [method-modules.md](method-modules.md), which supersedes
> §1's bespoke JSON pattern language (the typed-JSON contract was removed). Everything else here —
> execution semantics (§2), data findings (§3), claim granularity (§4), storage (§5), deployment
> (§6), and open decisions (§7) — remains in force; the GraphQL fields implement exactly the §2
> semantics (anchor limit before join, deterministic ties, empty-vs-missing, no score blending).

## 1. What the query language is

A **typed query graph**: named node variables (`g`, `p`, `s`) with categories, and named edges with a
closed predicate set, per-edge metric filters, sort, and limit. It is Cypher-inspired in how you read
it — `(g)-[associated_with]->(p)` — but it is a data structure, not a query string: everything a
client can say is enumerated by `GET /v1/vocabulary`, and the service rejects anything outside it.
That satisfies the plan's "typed patterns, not arbitrary Cypher execution."

The three question shapes Chase listed map directly:

| Question | Pattern | Backing claims |
|---|---|---|
| gene related to phenotype | `(g)-[associated_with]->(p)` | one claim per `combined_gene_stats.json` row |
| gene set related to phenotype | `(s)-[associated_with]->(p)` | one claim per `combined_gene_set_stats.json` row |
| gene related to gene set | `(g)-[member_of]->(s)` | one roster claim per gene set (`has_members`), joined per pair |

Single-edge queries are valid: Alex's "gene related to IBD?" is a one-node-pinned, one-edge query.
The Day-10 validation challenge is the three-edge join
([api/examples/ibd-validation-challenge.query.json](../api/examples/ibd-validation-challenge.query.json)).

## 2. Execution semantics (deterministic, documented)

1. **Per-edge resolution.** Each edge independently resolves to candidate claim-edges from deposited
   claims matching (predicate, categories, method, pinned ids, collection). Filters apply first.
2. **Per-edge sort + limit before joins.** An edge's `limit` prunes *that edge's* candidates after
   its `sort` — this is how "top 20 genes by `combined` before joining gene sets" is expressed, per
   the build plan's top-20-before-join requirement. Ties break on subject ID then object ID
   ascending, so repeated queries return identical results.
   **Amended 2026-11-09 (Chase):** the challenge's set side is a **per-anchor-gene window**, not a
   global row cap — each anchor gene keeps its own top `setsPerGene` sets (ordered within the gene
   by `setOrder`), so one gene's strong sets can never crowd another gene out. Results group per
   anchor gene in anchor order; a gene whose join finds nothing still appears with `sets: []`.
3. **Join.** Edges natural-join on shared node variables by canonical ID (genes: HGNC symbol as it
   appears in result files; phenotypes: portal trait key; gene sets: full result-file name,
   qualified by collection).
4. **Result ordering.** `options.order_results_by` names one edge's metric (the validation challenge
   orders by `e2.beta_uncorrected` desc); otherwise the first declared edge's sort carries over.
   `options.max_results` caps rows and sets `truncated`.
5. **Empty vs. missing.** Zero matches is a valid 200 with `results: []`. A configured-but-absent
   data source (e.g. membership claims not yet deposited) is reported in `warnings`, never silently
   conflated with "no match."

**No score blending.** Every number returns inside its own typed `ClaimScore` with
`metric` + `interpretation`; the API never computes cross-metric composites, and ranking is always
by one named metric on one named edge.

## 3. What the PIGEAN files actually contain (observed 2025-11-06)

`s3://dig-analysis-cfde/catalog_v2/out/pigean/.../cfde-inc-v2/portal/IBD/`, NDJSON, one object per line
(local copy: `kpn-dapper-toolkit/data/pigean/IBD/`, gitignored):

- **combined_gene_stats.json** — 18,321 rows, one per gene, keys all rows:
  `gene, prior, combined, log_bf, n, trait_group=portal, phenotype=IBD, gene_set_size=cfde-inc-v2`;
  929 rows add `label` + `factor` (PIGEAN factor attribution, e.g. `Factor2` /
  `HALLMARK_TNFA_SIGNALING_VIA_NFKB`).
- **combined_gene_set_stats.json** — 5,000 rows, one per gene set, keys all rows:
  `gene_set, source (35 distinct, e.g. lincs, geobulk___igvf), beta, beta_uncorrected, rs_score, n,
  trait_group, phenotype, gene_set_size`; 1,772 rows add `label` + `factor`.

Notes that shape the contract:

- `gene_set_size` is a **collection tag**, not a size — it names the gene-set collection/run
  (`cfde-inc-v2`). The contract calls it `collection`.
- **Membership edges are not in these two files** — they come from PIGEAN's own
  `gene_gene_set_stats/{collection}/portal/{phenotype}/gene_gene_set_stats.json` (for IBD:
  143,508 (gene, gene_set) pairs covering 12,519 genes × 3,851 sets). RESOLVED 2026-11-06, per
  Chase: ingest **only the set name and its member genes**; the file's per-pair
  `beta`/`beta_uncorrected`/`rs_score` are NOT ingested for now. Amended 2026-11-09 (Chase):
  membership is **one roster claim per gene set**, not one claim per (gene, set) pair — the
  proposition `(dapper:GeneSet.X, has_members, dapper:GeneSetCollection.Y)` pins the exact
  roster through the GeneSet entity's content-addressed `members` list (143,508 pairs → 3,851
  claims). The per-pair `claim_memberships` index still drives joins. A collection with no
  membership still answers three-edge queries with a missing-data-source warning on `e3`,
  never an empty "result".
- **Gene sets are first-class dapper entities** (added 2026-11-06, traceable-IDs example): the
  loader mints one `GeneSetCollection` (`cfde-inc-v2`), a placeholder creation `Activity`, and one
  `GeneSet` per distinct set (members = genes observed in `gene_gene_set_stats`, backlink
  `in_gene_set_collection`, `was_generated_by` the creation activity). Propositions reference the
  `dapper:GeneSet` IDs, so `ClaimEnvelope.provenanceRefs.geneSetRefs` now resolve (`RESOLVED` when
  the entity is deposited); `GeneSet.dapperGeneSetId` comes from the `gene_set_entities` index
  table, and any object dereferences via `Query.dapperObject(id)` / `GET /v1/objects/{id}`.
  Added 2026-11-09: `ClaimEnvelope.triple` lifts the proposition's (subject, predicate, object)
  out of the verbatim document, and `resolution` also checks `was_derived_from` file refs against
  the deposited objects, so a measurement claim whose result `File` is deposited reads `RESOLVED`.
- `prior` in this file ranges well above 0.5 (log scale, max ≈ 5.5, negatives exist), so the
  challenge filter `prior < 0.5` selects genes whose direct GWAS evidence is not driven by gene-set
  support — consistent with tiering by *incremental* gene-set contribution.
- `factor`/`label` suggest a future `factor` node category with `LOADING` scores; out of v0.

### Provisional metric semantics (confirm with method leads, Day 2)

| metric | DAPPER score_kind | provisional reading |
|---|---|---|
| `log_bf` | SCORE | log Bayes factor, direct GWAS support for gene–phenotype |
| `prior` | SCORE | log prior odds contributed by gene-set membership |
| `combined` | SCORE | combined log posterior odds (direct + indirect) |
| `beta` | EFFECT_ESTIMATE | gene-set effect, joint/corrected for other sets |
| `beta_uncorrected` | EFFECT_ESTIMATE | gene-set effect, marginal |
| `rs_score` | SCORE | **unknown — do not expose as filterable until confirmed** |
| `n` | SCORE (count) | gene rows: memberships?; set rows: set size? — confirm |

None is a probability; the vocabulary marks each `confirmed: false` until sign-off, and the UI is
expected to render unconfirmed semantics as provisional.

## 4. Claim granularity

Default per the plan: **one Claim per result-file row** — a method-specific assessment of one
Proposition, carrying all of that row's scores as typed `ClaimScore`s (one claim with three scores
for a gene row, not three claims). `source_locator` pins the row
(`combined_gene_stats.json#gene=NOD2`); `was_derived_from` points at the result file's
`dapper:File`; the companion provenance sidecar (Task 2) explains how that file was made.
Deposit is idempotent by construction: same content, same ID, `exists` status.

**Claims are minted when queried** (2026-11-09, Chase — supersedes "the repository verifies,
it never mints"): the loader stores no claims; each index row carries its claim's precomputed
content address. The first time the scientific query surface serves a result, the repository
**synthesizes** the claim from the row + the shared template (`service/pigean_template.py`),
refuses it unless its address equals the precomputed one, and deposits it — minting the claim
that justifies the query response — then counts every serve as an assertion (`times_seen`).
REST dereference of an implied claim that nothing has served or deposited returns 404 "not yet asserted"; external REST
deposits are asserted by their depositor and land immediately. Full rationale:
[inputs-and-outputs.md](inputs-and-outputs.md) §3.5.

`index_hints` in the deposit bundle declare each claim's (subject, predicate, object, method,
collection) edge explicitly, so graph indexing never parses proposition text.

**Evidence layer** (2026-11-09, Chase): measurement claims also carry `has_evidence` — one
EvidenceItem per metric, grounded by a ValueLocator pinning the exact byte span of the number in
the result file (`file[byte_start : byte_start+byte_length] == value_text` under the File's
sha256). `ClaimEnvelope.evidence` returns each item with its locators dereferenced. The typed
numbers stay in `has_score` (the filter/sort surface); the evidence makes them re-verifiable.

**Mint counter** (2026-11-09, Chase): content addressing makes re-deposits idempotent, so
`dapper_objects.times_seen` records how often the same document keeps being minted — +1 per
re-deposit (the loader counts re-runs the same way, deduping within a run) and, for claims,
+1 per serve: it is the **assertion counter**. Surfaced as `times_deposited` in deposit
responses, `times_seen` in object summaries, and `timesDeposited` / `firstDepositedAt` /
`lastDepositedAt` on the GraphQL envelope.

**REST is objects only** (2026-11-09, per Chase): `GET /v1/objects/{id}` (the resolver),
`GET /v1/objects` (list, optionally one `class_name`), and `GET /v1/objects/search`
(`references=<id>` over the `object_refs` reference index — e.g. the claims assessing a
proposition; `q` over statement/name/description; `match=slot:value` exact string-slot match;
no metric filters). No endpoint is tied to claims or propositions; the dereferenced claim
envelope and the proposition fan-in are GraphQL's `claim(id)` / `proposition(id)`. Contract:
`api/openapi.yaml`, served live at `GET /v1/openapi.yaml` with Swagger UI at `GET /v1/docs`
(the hand-written contract is the authority; FastAPI auto-docs are disabled).

**Identifier resolution** (2026-11-09, per Chase): the content-addressed CURIE
(`dapper:{Class}.{digest}`) stays the canonical, location-independent identifier; URLs are
presentations of it and never enter hashed content. `/v1/objects/{id}` is the resolver: a
consistent document presentation for every class (id + class_name first, sorted keys), with
content negotiation (browsers get a landing page whose dapper references are themselves
resolver links). Entries in `/v1/objects` and `/v1/objects/search` carry `resolver_url`, and
GraphQL claim envelopes carry `resolverUrl` (deposit responses, the resolver's own JSON, and other
GraphQL ID fields do not);
`/v1/vocabulary.identifier_resolution` (and `Query.identifierResolution`) publish the prefix
expansions and the resolver URL template. `scripts/audit_objects.py` sweeps the whole store:
document structure + referential integrity, the reference index against recomputed
references, and every claim the data implies synthesized and digest-verified (IBD: 172,700
objects, 27,172 claims verified, clean). A permanent-IRI layer (w3id.org/dapper) is drafted for
the dapper maintainers, phased with the DAPPER-ID-1 cutover (dapper repo:
schema/identity/w3id-namespace-proposal.md). Deposit registers every `Claim` object with its
proposition, so GraphQL's `proposition(id)` fan-in also sees claims without a graph-edge index
hint (e.g. interpretive tier claims); an index_hint later adds the method.

## 5. Storage (mirrors reveal-mechanisms)

Same shape as reveal's `schema/migrations` MySQL pattern (shared Aurora cluster, project database
`cyaka_claims_repository_v2` for QA, named by `CLAIMS_MYSQL_DATABASE`; plain SQL migrations):

```sql
-- authoritative DAPPER documents, verbatim
CREATE TABLE dapper_objects (
  id VARCHAR(128) ascii PRIMARY KEY, class_name VARCHAR(64), identity_profile VARCHAR(32),
  payload_sha256 CHAR(64), payload JSON, created_at TIMESTAMP
);
-- one row per deposited Claim
CREATE TABLE claims (
  claim_id VARCHAR(128) PRIMARY KEY REFERENCES dapper_objects(id),
  proposition_id VARCHAR(128), method VARCHAR(32), deposited_at TIMESTAMP
);
-- the query graph's edges
CREATE TABLE claim_edges (
  claim_id VARCHAR(128) PRIMARY KEY REFERENCES claims(claim_id),
  predicate VARCHAR(32), subject_category VARCHAR(16), subject_id VARCHAR(512),
  object_category VARCHAR(16), object_id VARCHAR(512),
  collection VARCHAR(64), trait_group VARCHAR(64), source VARCHAR(128),
  INDEX (predicate, object_category, object_id, collection),
  INDEX (predicate, subject_category, subject_id)
);
-- one row per ClaimScore, the filter/sort surface
CREATE TABLE claim_scores (
  claim_id VARCHAR(128), metric VARCHAR(64), score_kind VARCHAR(32), value DOUBLE,
  PRIMARY KEY (claim_id, metric), INDEX (metric, value)
);
```

Each query edge compiles to `claim_edges ⋈ claim_scores` with WHERE/ORDER BY/LIMIT; joins happen in
SQL on the bound node ID columns. No graph database needed at this scale (~23k claims per
phenotype-collection for PIGEAN IBD).

## 6. QA deployment (dig-service-platform) — REGISTERED 2026-11-09

Status: implemented, awaiting merge (nothing committed/pushed yet).

- **Dual-backend storage** (`service/db.py`): SQLite locally/CI; Aurora MySQL 8 when
  `CLAIMS_MYSQL_*` is set. One Connection wrapper (placeholder translation, dict rows,
  transactions) + a Dialect for the genuinely divergent SQL (json_extract, upserts, now()).
  MySQL DDL: `schema/migrations/001_init.sql` … `004_kpn_traits.sql`, applied by
  `scripts/migrate.py` (idempotent: one effect per statement, `requires-empty` guards, schema
  verified against information_schema afterwards, object_refs backfilled; tracks
  `schema_migrations`; refuses databases outside the `cyaka_` grant).
- **Database LIVE**: `cyaka_claims_repository_v2` (built on migrations 001–004; the pre-003
  `cyaka_claims_repository` remains until both QA and prod run the `_v2` deploy) on the shared cluster
  (`aurora-giant-bioindex.cluster-cxrzznxifeib.us-east-1.rds.amazonaws.com`, user `cyaka`,
  verified TLS). Loaded with the KPN trait release (8,402 traits, 24,050 mappings) and the full IBD
  slice: 172,700 data-plane objects; 27,172 claims implied (18,321 gene, 5,000 gene-set, 3,851
  gene-set rosters), none stored until served. Load order: `load_kpn_traits.py`, then
  `load_pigean.py`.
- **Service registered** in dig-service-platform (working copy): `claims/` directory (vendored
  `service/` + `api/` + `schema/` + mount shim under `/api/claims`), `service.yaml` (port 8000,
  arm64, priority 50, CLAIMS_MYSQL_* env, password via Secrets Manager), `services.yaml` line,
  `platform.yml` check/validate/qa/prod jobs, `deploy-service.yml` choice, platform test lists.
  `dig-platform validate` + registry-check pass; 239 platform tests + 3 claims smoke tests pass.
- **Before first deploy**: create the `cyaka/claims/{qa,prod}` secrets (commands in
  `dig-service-platform/claims/README.md`) and fill the ARN suffixes in `service.yaml`; then
  merge to main → QA deploys automatically.
- Health: `/api/claims/v1/health`. Public QA base URL for Alex:
  `https://api-qa.hugeampkpnbi.org/api/claims/v1`.

## 7. Open decisions (feed the Day-2 list)

1. **IBD concept ID** — RESOLVED 2026-11-09 (Chase): phenotypes resolve through the KPN trait
   registry (broadinstitute/kpn-data-models v0.0.2) — `IBD` → `KPN.TRAIT:0000505`, primary
   concept EFO:0003767, plus MONDO:0005265 / MESH:D015212 / DOID:0050589 / OMIM mappings.
2. **Metric semantics sign-off** — table above, esp. `rs_score`, `n`, and whether `prior` filters
   should be on log or probability scale in the UI.
3. **Membership claim source** — RESOLVED 2026-11-06 (Chase): PIGEAN's `gene_gene_set_stats`
   output (set name + genes only), encoded 2026-11-09 as one roster claim per gene set. `e3` is
   live for IBD / cfde-inc-v2. Whether to additionally expose the file's per-membership `beta`/`beta_uncorrected`
   stays open for later.
4. **Gene ID canon** — files carry symbols; do we join on symbol (v0) or re-key to HGNC CURIEs
   before the demo? Affects `claim_edges.subject_id` and UI linking.
5. **Deposit auth** — `X-API-Key` for deposit only, reads open. Enforced since 2026-11-12
   (docs/inputs-and-outputs.md, Deposit keys).
6. **EAGGL** — additive: a new `method` enum value plus its metric vocabulary; no structural change
   expected.

## 8. Explicitly deferred

Cursor pagination on query results; arbitrary Cypher; cross-metric composite ranking;
natural-language query compilation; factor nodes; provenance *resolution* inside this service
(Task 6 adds the provenance-indexer join behind `provenance_refs.resolution`); production auth and
federation.
