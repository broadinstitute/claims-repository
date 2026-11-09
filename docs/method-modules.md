# Method modules: claim templates ship their own query surface

Status: design decision, 2025-11-06 · Supersedes the bespoke JSON query-graph wire format
(`docs/query-graph-design.md` §2–§5 — execution semantics, storage, and open decisions — remain in force).

## 1. The decision

Each KPN method's claim template (in broadinstitute/kpn-dapper-toolkit) ships **three generated
artifacts from one source of truth** — the template's metric table:

1. **DAPPER claims** — the row-to-claim generation itself (Task 1, unchanged).
2. **A GraphQL SDL module** — the method's query surface: typed metric fields, filter inputs,
   order enums, and docstrings carrying the metric interpretations.
3. **A resolver binding + index DDL** — how the generic service executes those fields against the
   storage substrate.

The claims repository is a **host**: it owns the core schema (nodes, claim envelopes, provenance
refs, interfaces), composes `core.graphql + methods/*.graphql` into one executable schema at
startup, and runs generic binding-driven resolvers. It contains no PIGEAN-specific code. Adding
EAGGL or FALCON is depositing claims plus registering a module — no service release that touches
query logic.

Why we didn't build our own query language: GraphQL's parser, validator, executor, introspection,
playground, and client codegen are the vetted machinery we'd otherwise maintain. The schema *is*
the constraint ("typed patterns, not arbitrary Cypher"); depth/complexity limits are standard
practice. Alternatives considered and declined for the two-week scope: TRAPI (no sort, no
top-k-before-join, Biolink baggage — viable later as a translation layer), raw Cypher engines
(constraining Cypher means parsing Cypher), SPARQL (right long-term instinct for the RDF/nanopub
world, wrong tool for a UI consumer now), Hasura/PostgREST (welds contract to storage, weak on
MySQL).

## 2. Can GraphQL support per-method query semantics? Yes — three mechanisms

**`extend type` composition.** A module only extends core types
(`extend type Phenotype { pigeanGeneAssociations(...) }`) and adds its own concrete types. Build-time
concatenation + `graphql.build_schema` is all the "engine" the composition needs — this is standard
graphql-core behavior, the same mechanics schema-stitching and Apollo Federation are built on,
without their distributed runtime.

**Interfaces for the shared shape, concrete types for method semantics.**
`PigeanGeneAssociation implements GenePhenotypeAssociation`: cross-method consumers program against
the interface; method-aware queries get *schema-typed metrics* (`prior: Float!`), so an invalid
metric is a query-validation error, not a runtime 400, and every metric field's docstring carries
its interpretation into introspection, GraphiQL hovers, and generated clients.

**No-blending as a type-system property.** Order-by enums are module-private
(`PigeanGeneAssociationOrderField`), and no root field accepts two methods' metrics. Cross-method
score blending isn't rejected by a validator — it is *inexpressible*.

## 3. The module contract

A method module in kpn-dapper-toolkit ships:

```
methods/pigean/
  template.yaml          # metric table: name, score_kind, scale, better, interpretation, confirmed
  schema.graphql         # generated SDL (vendored into claims-repository api/graphql/methods/, pinned like the DAPPER profile)
  binding.yaml           # field → substrate mapping (below)
  index.sql              # DDL + loader spec for the tabular substrate
  queries/*.graphql      # persisted query patterns + default variables + fixtures
  claims.py              # row-to-claim generation (Task 1)
```

Generation from `template.yaml` is what keeps claim interpretations, SDL docstrings,
`MethodInfo.metrics`, filter/order enums, and index columns from drifting: edit the metric table,
regenerate, and every artifact agrees. Excluded metrics (PIGEAN's `rs_score`, `n`) get no typed
field or filter, but anything deposited still rides along losslessly in `ClaimEnvelope.scores`.

## 4. Two substrates, one binding layer

The substrate behind a module is one of:

- **Tabular** (PIGEAN now): the template generates a per-method index table whose columns are its
  metrics — `pigean_gene_associations(phenotype, collection, gene, prior, combined, log_bf,
  claim_id)` — loaded at deposit time from `index_hints` + scores. Filters/order/limit compile to
  indexed SQL on real columns.
- **Object** (methods whose results stay document-shaped): bindings address JSON paths into
  `dapper_objects.payload` (MySQL JSON columns, optionally with generated-column indexes).

Either way the terminal join is the same: `claim_id → ClaimEnvelope` from `dapper_objects`.
`binding.yaml` sketch:

```yaml
method: pigean
substrate: tabular
tables:
  pigean_gene_associations:
    key: [phenotype, collection, gene]
    metrics: { prior: prior, combined: combined, log_bf: logBf }
fields:
  Phenotype.pigeanGeneAssociations:
    table: pigean_gene_associations
    anchor: { filter: where, order: orderBy, limit: first }   # filter → order → limit, pre-join
  Query.pigeanValidationChallenge:
    plan: [anchor: Phenotype.pigeanGeneAssociations, join: memberships, join: geneSet.pigeanAssociation, order: rowOrder]
```

The generic resolver interprets bindings; it never contains method names. Execution semantics
(anchor limits before joins, deterministic ties, empty-vs-missing distinction) are inherited
unchanged from `query-graph-design.md` §2.

## 5. Responsibility boundary

| Concern | Core (claims-repository) | Method module (kpn-dapper-toolkit) |
|---|---|---|
| Node types, claim envelope, provenance refs | owns | — |
| Interfaces, filter primitives, SortDir | owns | implements/uses |
| Metric fields, semantics, filters, order enums | — | owns (generated) |
| Root convenience fields (e.g. `pigeanValidationChallenge`) | — | owns |
| Resolver execution, SQL, storage | owns (binding-driven) | declares bindings |
| Persisted demo queries | hosts | authors |
| Composition, CI schema check, depth limits | owns | — |

Module rules (CI-enforced by composing against core on every change): extend-only, implement the
interfaces, typed fields for vetted metrics only, module-private order enums.

## 6. The constrained-query guarantee, operationally

Alex's UI uses the **persisted query + variables** pattern
(`api/graphql/queries/ibd-validation-challenge.graphql`): the researcher-editable knobs from Task 8
— concept, prior threshold, metric, direction, top N — are the variables; the document doesn't
change. GraphiQL against the composed schema is the power-user escape hatch, bounded by the schema
plus standard depth/complexity limits. Introspection + `Query.methods` replace the bespoke
vocabulary endpoint for GraphQL clients (the REST `/v1/vocabulary` stays for the autocomplete
list and non-GraphQL consumers).

## 7. Adding a method (EAGGL checklist)

1. Write `template.yaml` with the metric table; get interpretations signed off (or marked
   provisional).
2. Generate `schema.graphql`, `binding.yaml`, `index.sql`; CI composes against core.
3. Implement row-to-claim generation; deposit claims with `index_hints`.
4. Vendor the SDL into `api/graphql/methods/`, register the module, redeploy.
5. Ship persisted query patterns + fixtures for its natural questions.

No core type changes, no resolver code, no new endpoints.

## 8. Deferred

Generator tooling itself (v0 modules are authored by hand to the same shape); Relay-style cursor
pagination; federation across separately deployed method services (the composition seam is already
federation-shaped if that day comes); TRAPI translation layer; mutations (deposit stays REST).
