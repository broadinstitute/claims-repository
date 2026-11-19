# Collections: a general claims repository

Status: **phases 1–4 built, phase 5 partly** (2026-11-10; see §12). For the step-by-step guide to building one, with PIGEAN as the example, see
[creating-a-collection.md](creating-a-collection.md). Supersedes the host/module split in
[method-modules.md](method-modules.md) and realizes the generic tables sketched
in [query-graph-design.md](query-graph-design.md) §5. Built on DAPPER
**0.2.1-a1** (on top of 0.2.0), which adds `ResultCollection` and `ValueLocator` (dapper
`docs/releases/0.2.1-a1.md`, `schema/docs/result-collections.md`). Local
development only for now: no QA migration or challenge-UI compatibility is
required.

Today the repository serves one method, PIGEAN, through hand-written code. This
design makes the **collection** the unit everything hangs off: a group of
result files from a method, the templates that mint claims from them, and the
queries that read them. A new method onboards by registering a collection.
It changes no service code and adds no tables.

**Acceptance test:** PIGEAN is re-registered through the collection path with
the same claims and the same challenge-query results, and then a second method
(EAGGL) onboards with no change to service code.

## Decisions

| Question | Decision |
| --- | --- |
| Claim templates | `{placeholder}` text plus a fixed function vocabulary. No owner code runs in the service. |
| Result storage | Generic Aurora tables keyed by collection release. Registering never runs DDL. |
| Owner-hosted APIs | Pull and snapshot a release through a small Rows contract; live calls only for data that cannot leave the owner's system. |
| Query surface | A generic plan executor, `runQuery(collection, query, variables)`. Typed GraphQL may be generated later. |
| Organizing object | The **collection**: files, datasets, templates and queries, released together, and self-describing. |
| Collection identity | The **handle** (the manifest's `collection:`, such as `pigean`), decided 2026-11-17 by Chase: it is what a collection is addressed by and what claim ids depend on. A UUID, if one is ever added, is internal to the database and enters no id. The UUID-as-identity design described in §1, §2 and §12 below was never built and is superseded by this row. |
| Collection in DAPPER | Each release is a DAPPER `ResultCollection` record; the release id is its DAPPER id, and its `collection_key` holds the collection's handle. `ResultCollection` and `GeneSetCollection` both inherit from an abstract DAPPER `Collection` (members plus a version chain). |
| Bringing data | Upload, an owner's Rows API, or a pointer to a data repository: a Croissant record, a DOI or DRS objects. All three snapshot bytes by sha256. |
| Ids | DAPPER-ID-1 now, through dapper's own `dapper_identity` at the 0.2.1-a1 tag. No interim local-dev scheme. |
| Methods per collection | One is the norm. More than one is allowed, but DAPPER documents it as usually an anti-pattern and the linter warns. |
| Entity identifiers | Taken from the result files as-is (gene symbols today, HGNC ids if the files change). Nothing is hardcoded. |

## 1. Model

```
Collection  id 7b0f6c1e-2d4a-4e8b-9c35-1a6f0d2e8b47   handle pigean   owners, title, description
 └─ Release  2026.11.0                      a DAPPER ResultCollection record: immutable, content-addressed
     ├─ Templates   claim templates + tier rules (versioned with the release)
     ├─ Queries     query templates (params, plan, example)
     └─ Datasets    one per dimension tuple, e.g. {trait: KPN.TRAIT:0000505, library: cfde-inc-v2}
         └─ Files   by role: gene_stats, gene_set_stats, membership (sha256, size, location)
```

- **Collection.** A named, owned group of results, usually from one method.
  Its identity is the UUID in the manifest's `id`. The manifest's
  `collection:` field is its handle, a short name such as `pigean` that is
  unique across collections and resolves to the id; it may not itself look like
  a UUID. Title, owners, description and handle can change. Nothing that mints
  claims can. A rename changes no claim id, no mint-context Activity id and no
  dataset id, because claim identity uses the UUID. It does change the release
  id, since a release records the exact manifest bytes and its name carries the
  title, so a rename is published as a new version. The same version with a
  renamed manifest is refused as a conflict. Old handles are not kept as
  aliases, and a handle that another collection id owns is refused. In DAPPER,
  the UUID is the `collection_key` that names the collection across releases.
- **Release.** One DAPPER `ResultCollection` record, immutable and
  content-addressed. It pins the claim templates, query templates, manifest
  and every dataset's files. The release id is that record's DAPPER id, and
  `was_revision_of` chains each release to the one before. Several releases
  can be live. Queries default to the collection's current release.
- **Dataset.** One slice of a release: a DAPPER `Dataset` whose
  `dimension_values` place it along the collection's declared **dimensions**.
  For PIGEAN the dimensions are trait and gene-set library, so an IBD run and
  a T2D run are two datasets of one release.
- **File role.** A Dataset's `file_roles` say which file plays which role
  (`gene_stats`, `membership`). The manifest declares each role's format and
  what it yields: a row kind (rows with a subject, object and metrics) or an
  edge kind (such as gene `member_of` gene set).
- **Provenance.** Claims never reference the release. A claim names its
  minting Activity (`was_generated_by`), which `used` the template File and
  the result File, and its result File (`was_derived_from`). Claim ids
  therefore depend only on template and file bytes. A release that adds T2D
  leaves every IBD claim id unchanged, and a template edit re-addresses only
  that template's claims.

**Naming clash to fix first:** "collection" already means the gene-set library
today: the `collection` column and `DEFAULT_COLLECTION = "cfde-inc-v2"`. In the
new model that becomes the `library` dimension, and "collection" means only the
registered object. In DAPPER a library stays a `GeneSetCollection`. It is a
sibling of `ResultCollection` under the abstract `Collection`, so a library can
be versioned the same way (`collection_key: cfde-inc`, `version: v2`).

## 2. The collection manifest

Lives in git, in the toolkit (`collections/pigean/`). PR review is the
governance step, the same rule the KPN trait registry uses. Illustrative:

```yaml
# collections/pigean/collection.yaml
id: 7b0f6c1e-2d4a-4e8b-9c35-1a6f0d2e8b47   # the collection's identity: a lowercase UUID, written once, never changed
collection: pigean                         # its handle: a unique short name that can change and resolves to the id
title: PIGEAN gene and gene-set associations
owners: [dig-aggregator-pipeline]
methods: [PIGEAN]
dapper_profile: 0.2.1-a1
dimensions:
  trait:   { entity: Trait }            # resolved through the KPN trait registry
  library: { type: string }             # gene-set library, e.g. cfde-inc-v2
entities:           # how a file's value becomes a CURIE; values are never normalized
  Gene:    { prefix: HGNC.SYMBOL }      # today's files carry symbols; switch if they move to HGNC ids
  GeneSet: { mint: GeneSet }            # DAPPER GeneSet id from the membership roster
  Trait:   { registry: kpn-traits }     # IBD -> KPN.TRAIT:0000505
prefixes: { HGNC.SYMBOL: "https://identifiers.org/hgnc.symbol:" }
row_kinds:
  gene_phenotype:     { subject: Gene,    object: $trait, metrics: [combined, log_bf, prior] }
  gene_set_phenotype: { subject: GeneSet, object: $trait, metrics: [beta, beta_uncorrected] }
edge_kinds:
  member_of:          { from: Gene, to: GeneSet, scope: [library, trait?] }
file_roles:
  gene_stats:     { format: ndjson, yields: gene_phenotype,     key: { subject: gene } }
  gene_set_stats: { format: ndjson, yields: gene_set_phenotype, key: { subject: gene_set } }
  membership:     { format: ndjson, yields: member_of,          key: { from: gene, to: gene_set } }
metrics:            # one copy of the semantics (today there are three)
  combined: { score_kind: SCORE, scale: log posterior odds, better: higher, interpretation: "..." }
  # ...
templates: [templates/measurement.yaml, templates/tiers.yaml, templates/roster.yaml]
queries:   [queries/top-genes-with-sets.yaml]
```

The release is a DAPPER document generated by the CLI from that manifest plus
the data, linted with DAPPER's `result-collection` profile. Abridged; dapper's
`schema/examples/example_result_collection.yaml` is a full one:

```yaml
result_collections:
  - id: dapper:ResultCollection.<digest>          # the release id
    collection_key: 7b0f6c1e-2d4a-4e8b-9c35-1a6f0d2e8b47   # the collection's UUID, not its handle
    version: 2026.11.0
    methods: [PIGEAN]
    dataset_dimensions: [trait, library]
    members: [dapper:Dataset.<ibd>, ...]          # Collection.members, typed to Dataset
    has_claim_template: [dapper:File.<measurement.yaml>, dapper:File.<tiers.yaml>, ...]
    has_query_template: [dapper:File.<top-genes-with-sets.yaml>]
    has_manifest_file: dapper:File.<collection.yaml>
    was_revision_of: dapper:ResultCollection.<previous>
    was_generated_by: dapper:Activity.<publish>  # used the manifest; code_version = git commit
datasets:
  - id: dapper:Dataset.<ibd>
    trait: KPN.TRAIT:0000505
    dimension_values:
      - { dimension: trait,   dimension_value: KPN.TRAIT:0000505 }
      - { dimension: library, dimension_value: cfde-inc-v2 }
    has_file: [dapper:File.<gene_stats>, dapper:File.<gene_set_stats>, dapper:File.<membership>]
    file_roles:
      - { role: gene_stats,     file: dapper:File.<gene_stats> }   # File: sha256, size, location
      - { role: gene_set_stats, file: dapper:File.<gene_set_stats> }
      - { role: membership,     file: dapper:File.<membership> }
```

A JSON Schema validates the manifest and templates. DAPPER's linter validates
the release. Today nothing validates `template.yaml`, and errors appear only at
render time.

**Entity CURIEs.** DAPPER rejects bare strings in relationship slots.
Today's toolkit output fails that check: `subject_entity: CXCR2`,
`object_entity: IBD`, `relation: associated_with`. The manifest's `entities`
block declares how each value becomes a CURIE: a prefix, a registry or a
minted id. Values are never rewritten, so a gene symbol stays a symbol. If
the files move to HGNC ids, only the prefix changes.

## 3. Claim templates

Templates bind to a row kind or edge kind and say how one row becomes a
proposition, scores, evidence and claims. Today `template.yaml` holds only the
wording and tier rules. Subject and object, scope, names, the file wiring and
the evidence text are Python in two places (`claims.py` and
`service/pigean_template.py`), and the two have drifted. Everything moves into
the template:

```yaml
# templates/measurement.yaml
claim: measurement
for: gene_phenotype
proposition:
  subject: "{subject.id}"
  relation: associated_with
  object: "{object.id}"
  kind: RESULT
  statement: "{subject.label} is associated with {object.label} ({dataset.library})"
scores: [combined, log_bf, prior]
evidence: { per_metric: true, locator: value }   # byte-exact ValueLocator into the source file
```

**One placeholder namespace for every layer:** `row.<column>`,
`subject.{id,label}`, `object.{id,label}`, `dataset.<dimension>`,
`collection.id`, `release.id`, `metric.<name>.{value,text}`,
`tier.{label,rule,rule_version}`. `collection.id` is the collection's UUID. The handle and the title are not
available to templates, because they are not part of claim identity, and a
template that uses `{collection.key}` is refused. Today measurement claims and tier claims
see different names, and numbers render differently between them.

**Fixed function vocabulary.** Everything the Python does today, and nothing
more:

| Function | Does |
| --- | --- |
| `classify(metric, rule)` | Tier label; first match wins. Adds `max`, exclusive bounds and `abs` to today's rules. |
| `direction(rule)` | SUPPORTS / DISPUTES / NEUTRAL from a declared rule. DAPPER forbids thresholding without a declared rule, and this is that rule. |
| `locate(metric)` | Byte span plus exact text in the source file. NDJSON today; TSV and CSV next. |
| `label(entity)` | Entity label from the entity registry (trait names, gene symbols). |
| `fmt.num(x, digits)` | Deterministic number text, the same in every layer. |
| `group(edge_kind)` | Aggregation for roster claims (one claim per set, listing its members). |

**One identity scheme: DAPPER-ID-1.** The toolkit and the service give the
same content different ids today. One hashes the class name and the other
doesn't, and only the service float-normalizes numbers. Neither is DAPPER-ID-1.
The fix: both mint through dapper's own `dapper_identity` (`compute_id`),
pinned to the 0.2.1-a1 tag, with a golden test that the two produce identical
ids. Measured at 0.09 ms per node, so minting on serve stays. The template
enters identity as bytes: the minting Activity `used` the template File.

## 4. Bringing data

- **Upload.** The owner uploads files or names S3 URIs with sha256. We keep the
  original bytes content-addressed in S3, since byte-exact evidence depends on
  them. An ingest job parses rows through the file roles.
- **Registered API (pull and snapshot).** The owner implements two endpoints:
  `GET /releases` and `GET /rows?dataset&kind&cursor`. They send rows, never
  claims. We snapshot a release into S3 and from then on it is identical to an
  upload. We mint with their template, so minted claims and evidence stay
  reproducible even if their service changes.
- **Repository pointer (Croissant first).** The owner points at data already
  published in a repository instead of moving it. The preferred pointer is a
  [Croissant](https://mlcommons.org/working-groups/data/croissant/) record, the
  MLCommons JSON-LD format that Hugging Face, Kaggle and OpenML publish. Its
  `distribution` lists every file with `contentUrl` and a checksum, and its
  record sets name the columns. We fetch the record and keep it as a File,
  which the release's publication Activity `used`. We then download each
  listed file, verify its checksum, and snapshot it by sha256. Two other
  pointers are adapters to the same file list: a DOI, resolved through the
  repository's file API, and GA4GH DRS objects, which carry their own
  checksums. The manifest maps repository files to datasets and roles, and
  record-set fields can prefill row kinds:

  ```yaml
  sources:
    - croissant: https://<repository>/<dataset>/croissant.json
      files:
        - match: "*/combined_gene_stats.json"      # FileObject name or FileSet includes
          role: gene_stats
          dimensions: { trait: "{path.0}", library: cfde-inc-v2 }
  ```

  Metric meaning still comes from the manifest, because Croissant's `dataType`
  does not say what a score means. DAPPER's Croissant guide
  (`schema/docs/croissant.md`) gives the field-by-field mapping in both directions. No DAPPER class is needed: FileObjects become Files,
  the record's DOI becomes `alternate_identifier`, and the landing page becomes
  `location`.

All three paths end the same way. The bytes sit in S3 under their sha256, and
the ingest job, minting and byte-exact evidence never depend on the source
staying online.

## 5. Storage

Generic tables replace the five PIGEAN tables. The ledger (`dapper_objects`,
`claims`, `object_refs`) is unchanged, except that `claims.method` for a collection
claim records the manifest's method (`PIGEAN`; several methods are joined with
`+`), not the handle. A claim served under two releases is
one row, because its id does not depend on the release.

| Table | Holds |
| --- | --- |
| `collections` | `collection_id` (the manifest's UUID, primary key), `collection_key` (the handle, unique), title, description, current release |
| `collection_releases` | release id (the `ResultCollection` DAPPER id), `collection_id`, version (unique per collection), manifest, its DAPPER document, git commit, published_at |
| `datasets` | release, dimension tuple |
| `release_files` | dataset, role, sha256, size, location |
| `ingest_jobs` | state of uploads and snapshots |
| `entities` | type, id, label, xrefs: Gene, GeneSet and future types (generalizes `gene_set_entities`; `kpn_traits` stays the Trait registry) |
| `result_rows` | row_id, dataset, kind, subject_id, object_id, claim_id, proposition_id, synthesis_inputs, file, byte span |
| `result_metrics` | row_id, metric, value; index `(dataset, kind, object, metric, value)` for filter, order and top-k |
| `result_edges` | dataset, kind, from_id, to_id, scope, claim_id (generalizes `claim_memberships`) |

Sizes are small: about 18k gene rows and 5k gene-set rows per trait. The
metrics index serves "top k by one metric for one trait" directly. Filtering on
a second metric adds a join. If one collection outgrows this, it gets a
dedicated table through a normal migration. Parquet with DuckDB stays the
fallback.

## 6. Queries: the plan executor

Query templates are bounded declarative plans:

```yaml
# queries/top-genes-with-sets.yaml
name: top-genes-with-sets
description: Top genes for a trait with low prior, each with its strongest gene sets.
params:
  trait:       { type: Trait }
  priorMax:    { type: number, default: 0.5 }
  topGenes:    { type: int, default: 20, max: 200 }
  setsPerGene: { type: int, default: 5,  max: 50 }
plan:
  - anchor: { kind: gene_phenotype, object: $trait, where: { prior: { lt: $priorMax } }, order: combined desc, limit: $topGenes }
  - join:   { edge: member_of, from: subject }
  - join:   { kind: gene_set_phenotype, object: $trait, order: beta_uncorrected desc, limit_per_group: $setsPerGene }
example: { trait: IBD }
```

- **Call:** `POST /v1/collections/{c}/queries/{name}:run` and GraphQL
  `runQuery(collection, query, variables)`. Rows come back with claim
  envelopes, so serving still mints.
- **No blending** becomes a registration check, not just a convention. Each
  step filters and orders only on its own kind's metrics, so cross-kind or
  cross-collection score arithmetic can't be written. This is also what keeps
  a mixed-method collection safe, if one is ever registered.
- **Kept from today:** filter, order, then limit before any join; ties
  broken by entity ascending; per-anchor windows; scoped edges beating
  library-wide ones; `warnings` and `truncated`; the 25,000-query budget; the
  2,000-assertion cap; row bounds; one transaction per request; render before
  write.
- `pigeanValidationChallenge` became this query template and was removed on
  2026-11-18, with every other per-method GraphQL field (`pigean*Associations`,
  `pigeanAssociation`, `gene`, `geneSet`, `phenotypes`, `methods`). The
  persisted-query fixture is gone with it.

## 7. Discovery: asking a collection about itself

Every collection describes itself from its manifest, so the docs can't drift
from what runs:

- `GET /v1/collections`: `id` (the UUID), `key` (the handle), title, methods,
  current release, dataset count.
- `GET /v1/collections/{c}`: releases; datasets with their dimension values
  and labels (which traits are covered); row and edge kinds; metrics with
  meaning, scale and direction; claim templates; row counts and claims
  minted.
- `GET /v1/collections/{c}/queries`: each query's description, parameters
  and a runnable example call.
- GraphQL `collections` and `collection(id)` return the same data. The
  admin console shows it through its own admin API.

In every path `{c}`, and in every GraphQL argument that names a collection, the
reference is either the UUID or the handle. A handle only resolves to the UUID.
Responses return the UUID as `id` and the handle as `key`, and the URLs and
example calls the API generates use the UUID.

This gives a person or an LLM agent one route: list collections, read a
manifest, run a query. An MCP tool later is a thin wrapper over these three
calls.

## 8. Registration API

```
POST /v1/collections                          create (id, title, owners)
POST /v1/collections/{c}/releases             draft from a manifest at a git commit + files or a source
POST /v1/collections/{c}/releases/{r}:dry-run sample rows → rendered claims + DAPPER validation + query examples run
POST /v1/collections/{c}/releases/{r}:publish immutable; requires a published git commit
POST /v1/collections/{c}/sources              register an owner API (Rows probe) or a repository pointer
                                              (Croissant URL, DOI, DRS ids); returns the resolved file list
GET  /v1/jobs/{id}
```

The toolkit gets a CLI: `kpn-dapper collection publish collections/pigean`.
**Prerequisite:** per-agent API keys. Deposit (`POST /v1/deposits`) has them since 2026-11-12
(`service/api_keys.py`, [inputs-and-outputs.md](inputs-and-outputs.md#deposit-keys)); the registration API
would reuse the same keys and deposit records.

## 9. What changes in today's code

| Today (PIGEAN-specific) | Becomes |
| --- | --- |
| `service/pigean_template.py`, `CLAIM_BUILDERS`, `CLAIM_SOURCES`, `METHOD="pigean"` | Template engine driven by the release's templates |
| `canonical` / `mint` / `num` inside the PIGEAN module | dapper's `dapper_identity` (DAPPER-ID-1), also used by the toolkit |
| Five `pigean_*` / membership / gene-set tables | `result_rows`, `result_metrics`, `result_edges`, `entities` |
| Hand-written resolvers and `pigeanValidationChallenge` | Plan executor plus a query template |
| Core `gene`, `geneSet`, `phenotype(s)` reading PIGEAN tables | Entity registry, with coverage from `datasets` |
| `/v1/vocabulary`, `Query.methods`, admin `_templates` / `_overview`, frontend `Family` | Generated from collection manifests |
| Deposit `index_hints` keyed on gene / gene_set and PIGEAN metric names | Hints naming a collection row kind, checked against its manifest |
| `load_pigean.py` (hardcoded IBD / cfde-inc-v2 S3 paths, fixed Activity) | Generic ingester driven by file roles |
| `audit_objects.py` check 4 (roster-only) and unknown-class failures | Per-release conformance: re-synthesize every row and verify its address |
| `kpn_traits.pigean_id` | Trait xrefs keyed by collection |

## 10. Phases

Each phase ends with a parity check against what runs today.

1. **DAPPER-ID-1 everywhere.** Pin dapper 0.2.1-a1. The toolkit and service
   mint through `dapper_identity`, with a golden test that both produce
   identical ids. Emit CURIEs for genes, traits and relations so toolkit
   output lints clean. Add a template JSON Schema. Fix the collection/library
   naming clash. Every id changes once here, which is fine locally.
2. **PIGEAN as a collection manifest plus the template engine.** The engine
   renders the same claims as the toolkit's `claims.py`, or a listed set of
   intentional differences. Tier claims mint on serve, from the template.
   Retire `pigean_template.py`.
3. **Generic storage and ingester.** Load PIGEAN IBD into the `result_*`
   tables. Every claim id matches phase 2's.
4. **Plan executor and discovery endpoints.** The challenge query as a query
   template, matching the persisted-query fixture. Then remove
   `pigeanValidationChallenge`.
5. **Registration API, auth and CLI.** Publish PIGEAN locally through it as a
   `ResultCollection` release that passes DAPPER's linter.
6. **Second collection (EAGGL)** with no service code change. Then drop the
   `pigean_*` tables.

## 11. Decided and open

Decided (2026-11-10):

1. **Ids:** DAPPER-ID-1 now, on DAPPER 0.2.1-a1, which includes ValueLocator.
2. **Collection scope:** mixing methods is allowed. DAPPER's result-collections
   guide explains why it is usually an anti-pattern, and the linter warns.
3. **Collection as a DAPPER object:** abstract `Collection` (a Set of DAPPER
   records with an optional version chain), with `GeneSetCollection` and the
   new `ResultCollection` as subclasses. Also `Dataset.dimension_values` and
   `Dataset.file_roles`, all in 0.2.1-a1.
4. **Repository data:** Croissant records, DOIs and DRS objects resolve to a
   checksummed file list and are snapshotted like uploads.
5. **Gene identifiers:** not this repository's decision. Ids come from the
   result files, and the manifest declares only their CURIE prefix.
6. **Challenge UI:** out of scope. Development is local only.

Open:

1. **Releasing 0.2.1-a1.** The schema, linter, examples and docs are in the
   dapper working tree. It still needs a commit, a PR to broadinstitute/dapper
   and the tag.
2. **Gene symbol namespace.** Decided 2026-11-17: `HGNC.SYMBOL:` (upper case, as REVEAL deposits
   write it), expanding to identifiers.org `hgnc.symbol`. Earlier text: `hgnc.symbol` (identifiers.org) is the proposed
   prefix for symbol-keyed files. It is a declaration in the manifest, not a
   mapping step.
3. **Claim-only documents.** DAPPER has no linter profile for a document of
   minted claims without a ScientificAccount, so such documents get only the
   generic checks. A `claim-set` profile would need a rule for claims that are
   also account components.

## 12. Status (2026-11-10)

Built, beside the PIGEAN path rather than replacing it:

- **DAPPER-ID-1 minting** (`service/collections/identity.py`) through dapper's
  own `dapper_identity.py`, vendored with its schema at the 0.2.1-a1 commit
  (`vendor/dapper`, refreshed by `scripts/vendor_dapper.py`). It reproduces
  dapper's frozen test vectors, and it refuses fields that are not slots of the
  class, so a typo can't silently drop out of an id.
- **The PIGEAN collection as data** in the toolkit: `collections/pigean/` holds
  the manifest, three claim templates (gene, gene set, roster), the challenge
  query and the 2026.11.0 release spec. JSON Schemas in `api/collections/`
  validate it, and the loader cross-checks metrics, rules, relations and
  entities. It also enforces no-blend: a query step may only filter and order
  on its own kind's metrics.
- **The template engine** (`service/collections/engine.py`): placeholders plus
  `classify`, `direction`, `locate` and grouping. Measurement, tier and roster
  claims all render from templates, so tier claims now mint on serve too.
- **(Not built; superseded 2026-11-17: the handle is the identity.)** Collections are identified by UUID. The manifest's required `id` is the
  identity and `collection:` is a changeable handle, accepted wherever a
  collection is addressed. A local SQLite database made before this change is
  refused on connect: drop its collection tables (or delete the database) and
  republish each collection from a manifest that declares its `id`.
- **Generic storage** (`006_collections.sql`) and the **publisher**
  (`scripts/publish_collection.py`). A release becomes a DAPPER
  `ResultCollection` with its Datasets, Files and Activities, and that document
  passes dapper's linter. Publishing the IBD release takes about 100 s and
  yields 23,321 rows, 143,508 edges and 92,135 mintable claims (counted when publishing still rendered
  every claim; since 2026-11-18 it renders none). Claim ids
  depend only on the template, the file and the manifest's minting semantics,
  so a new release of unchanged data re-addresses nothing.
- **Partial releases and files by location** (2026-11-17). A release specification may carry `where`
  (for the whole release or per dataset): strict lower bounds per row kind and metric, such as
  `gene_phenotype: {combined: {gt: 0.5}}`. A row at or below a bound is not ingested at all. A minted
  entity (a gene set) then exists only for a kept row and keeps its whole roster, because its roster
  claim counts the edges that end at it. The bounds are written into the Dataset description and enter
  no claim id; the same files under other bounds cannot be a second dataset, and adding rows to an
  ingested dataset is not supported yet. A result file may be given by `location` (an `s3://` URI)
  with its `sha256` and `size` instead of a `path`; the CLI downloads it when its dataset is ingested,
  checks the sha256 and removes it. Each dataset is stored in its own transaction and the release is
  recorded last, so an interrupted publish resumes. Edge files are read with a bare Python `None` as
  null (some aggregator runs write their membership file that way); row files stay strict.
- **Claims render on serve, never ahead** (2026-11-18). Publishing records one mint context per
  template and result file and renders no claim. A query renders the claims of the rows it returns
  under the release's mint contexts; the first serve stores the claim (with its proposition, scores
  and evidence) and records it in `result_claims`, which is how its id resolves afterwards. A
  template or wording change therefore costs one mint context per dataset instead of a pass over
  every row (the PIGEAN republish of 2026-11-18 was re-rendering about 1.6 million claims). A claim
  that has never been served has no record, so its id does not resolve and its address cannot be
  reserved against a deposit until it is first served.
- **Serving and discovery.** Claims are rendered on every serve; one already served is checked
  against its record (a drifted row is refused). Endpoints: `GET /v1/collections`,
  `/v1/collections/{c}`, `/v1/collections/{c}/queries`,
  `/v1/collections/{c}/releases/{version}/document` and
  `POST /v1/collections/{c}/queries/{name}:run`, plus the query catalog
  `GET /v1/queries` (every query with its params and their values, the rows it
  returns, the claims it serves and a request that runs as given) and
  `GET /v1/collections/{c}/dimensions/{d}` (a dimension's values). GraphQL
  has `queries`, `dimensionValues`, `collections`, `collection`,
  `collectionQueries` and `runQuery`, and no per-method fields.
- **Parity.** The challenge query template returns the same genes, order,
  values and gene sets as `pigeanValidationChallenge`. That holds on the
  fixtures (tested) and on real IBD at 20, 37, 50 and 60 genes, up to 550 sets.
  dapper's verifier accepts every served claim and the objects it references.

Not built yet:

- **The registration API and per-agent keys** (§8). Publishing is the local CLI. An admin-console registration flow now exists for local use (server-side paths, gated by `CLAIMS_COLLECTION_ROOTS`); the public registration API and per-agent keys are still not built.
- **The Croissant/DOI/DRS source adapter** (§4). Files are given by path.
- **Toolkit side:** `methods/pigean/claims.py` still mints local-dev ids. It
  retires in favor of the engine.
- **Asking your own question (2026-11-19).** Questions no template asks are
  written in GraphQL, not in a plan language: `Query.rows(collection, kind,
  dimensions, subject, where, orderBy, first)` returns typed `Row`s, with
  `Row.joined(via, kind, ...)` to follow edges and `Row.claim(key)` for the
  claim behind a row. The same rules as a template are checked against the
  manifest (a condition or order uses the kind's own metrics; edges must
  match the kinds) and lists are bounded (500 rows, 50 joined per row). A
  template's anchor may name a `subject` too (pigean `gene-across-traits`,
  `gene-set-across-traits`). The catalog lists each
  collection's row kinds, metrics, dimensions and edges under `collections`.
  `GET /v1/collections/{c}` pages its datasets (`first`, `offset`).
  Rows serve their claims by default.
- **Vocabularies live in each collection (2026-11-18).** A collection's
  `mappings/<dimension>.tsv` (key, id, label and attributes such as category
  and primary concept) and `mappings/<dimension>.sssom.tsv` (cross-references)
  are its whole vocabulary: publishing and queries read nothing else, and
  `dimensionValues` serves them. `resolve: kpn-traits` and GraphQL `phenotype`
  are gone; `claims vocab check --write-template` writes the files from a
  kpn-data-models trait release. (The `entities`/`kpn_traits` sketches above
  predate this.)
- **Removing the rest of the PIGEAN path.** The GraphQL fields are gone
  (2026-11-18). Still there: the `pigean_*` tables and `load_pigean.py`, the
  legacy claim sources `claim(id)` and `proposition` still synthesize from,
  deposit `index_hints` (which write those tables), and `/v1/vocabulary`.


## 13. Object collections: releases built from deposits (2026-11-18)

REVEAL will keep no scientific objects of its own. Its job results are
deposited with no collection (§ inputs-and-outputs 4.5), and its catalogs
(DisMech gaps, EAGGL factors, CFDE gene sets, traits, gnomAD constraint) are
deposited once per upstream release into collections, so that named queries
can read them.

**Why not an ordinary (file-based) collection.** A file-based release reads
result files and mints its own claims from their rows with its templates. A
catalog is not rows to mint from: it is DAPPER objects REVEAL has already
minted (KnowledgeGaps, Mechanisms, GeneSets), whose ids job deposits copy
verbatim. Re-minting them from files would give them other ids, or would
need a template per class reproducing REVEAL's builders exactly. So a
catalog goes in as deposits, and a collection may say its releases are built
from deposits:

| | File-based (`kind` absent) | Object collection (`kind: objects`) |
|---|---|---|
| A release is | result files, read and minted by the publisher | DAPPER objects deposited into it |
| Ids | minted by the collection's templates | the deposited ids, unchanged (DAPPER-ID-1, checked on deposit) |
| Built by | `scripts/publish_collection.py --release` | register the definition once; then open, deposit parts, close |
| Queries read | `result_rows` / edges (metrics, dimensions) | the release's objects by class, indexed fields, links |
| Query rows serve | claim envelopes (asserting them) | the stored documents; plain reads, no claims |
| Release document | ResultCollection of Datasets of result files | ResultCollection of one Dataset per class, each with a File listing its member ids |

**The manifest** (`api/collections/object-collection.schema.json`) names the
DAPPER classes the collection holds (`object_kinds`), and for each the
`fields` queries filter and order on (a slot each: string, number, or a list
of them) and the `links` queries follow (a slot holding dapper ids). Classes
and slots are checked against the vendored DAPPER schema. Vocabulary is the
collection's own, as for file collections: `mappings` and `xrefs` keyed by a
field (e.g. disease keys → the IRIs gaps name).

**Query templates** (`object-query.schema.json`): an anchor (a class, `where`
on its fields with eq, ne, lt, lte, gt, gte, contains; `order` by a field;
`limit`), then up to three steps, each either `follow` (the objects an anchor's
link names) or `referenced_by` (objects of a class in the release whose link
names the anchor), with a per-anchor limit. A condition whose variable is
unset is dropped, so one template can take alternative parameters.

**Life of a release.**
1. Register the definition from its directory:
   `scripts/publish_collection.py <dir>` (no `--release`). It stores the
   manifest, query and vocabulary Files.
2. A key granted the collection opens a release:
   `POST /v1/collections/{c}/releases {"version": ...}`.
3. Deposits naming the collection and release add their objects. Membership
   and field values are recorded in the deposit's transaction, after checking
   the grant and that the release is open (locking reads), so a part sent
   after the close is refused whole. Resending a part changes nothing.
4. `POST /v1/collections/{c}/releases/{v}:close` mints the ResultCollection,
   records it in `collection_releases` and makes it current. Queries read
   closed releases only.

Tables: `object_collections`, `object_releases`, `release_objects`,
`release_object_fields`, `api_key_grants`, and `deposits.collection_key` /
`release_version` (SQLite on connect; MySQL `schema/migrations/008_object_collections.sql`,
not yet applied anywhere and untested on MySQL).

**Catalog collections in kpn-dapper-toolkit.**
- `collections/dismech`: KnowledgeGaps and Mechanisms; `gaps-by-disease`
  (MONDO IRI and/or part of the disease name) and `gap-with-anchors`. The
  anchors are the Mechanisms a gap's `about_entities` names, so REVEAL must
  list each anchor's dapper id there (proposed; REVEAL's gaps carry only the
  disease IRI today, and their anchors are REVEAL-side attachments).
- **Waiting on REVEAL:** "factors and gene sets for a trait" (EAGGL) and
  "constraint by gene" (gnomAD). DAPPER has no class for a constraint score,
  and Mechanism and GeneSet have no trait slot; REVEAL's CFDE GeneSets carry
  neither members nor a collection link. These queries can be written once
  REVEAL fixes how a factor names its trait and genes, and how a constraint
  value is expressed (for example as a Proposition about the gene with a
  ClaimScore), since an object query can filter Propositions by
  subject_entity, relation and object_entity.
