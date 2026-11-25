# Query performance

How a collection query is served, what makes it fast or slow, the decisions taken, and the query-syntax
changes still open. Everything here holds for any file-based collection: nothing depends on PIGEAN's row
kinds, edge kinds or entity names. `tests/test_query_batching.py` checks that on a copy of the fixture
collection with every name changed.

## How a query is served

`POST /v1/collections/{collection}/queries/{name}:run` runs the query template's plan against the release:

1. **Anchor.** Filter, order and limit rows of one row kind across the release's datasets that match the
   anchor's dimensions (`store.anchor`).
2. **Joins.** For each anchor row, follow an edge kind to rows of another kind, each anchor keeping its own
   window (`store.join`, one query per dataset).
3. **Claims.** Render every claim the rows imply (one per row and template, one per joined edge group and
   roster template), check each against its id, and serve it with its proposition, scores, evidence and
   value locators (`envelopes`). Claims served for the first time are stored at the end of the request.

Steps 1 and 2 read the store: the service database (`result_rows`, for releases published with the
database password), a local deployment's Parquet index, or a bucket index read into a local cache
([publishing-to-a-deployment.md](publishing-to-a-deployment.md)). Step 3 reads the service database.

On QA every database call is a network round trip, and every index query has DuckDB open its Parquet
files. So the cost of a query is mostly how many calls it makes, not how much it reads.

## Decisions

| Date | Decision | Status |
| --- | --- | --- |
| 2026-11-25 | **GraphQL answers each list at once.** GraphQL applies a field's selection to every item of a list, so the first item of a list to ask for `joined(...)` or `claim(key)` answers it for the whole list. A join runs one store query per dataset for every row of the list, and all the joined rows share one batch. A claim key is served for every row or edge of the list in one `envelopes()` call. The `proposition` field serves its claims in one batch too. | Built (#15) |
| 2026-11-25 | **Claims are served in one batch per response.** `serve._attach_claims` gathers every row and edge-group claim the response serves and calls `envelopes(con, items)` once. Claim rows, the stored-object check, propositions, scores, evidence, value locators and reference checks take about five queries for the whole response. `envelope()` is the one-claim case of the same code. | Built (#14) |
| 2026-11-25 | **Edge groups are read up front.** Every edge group the response renders is read in two queries per dataset and edge kind (`store.groups`). That covers joined groups and rows whose subject is a minted entity such as a gene set, and is found from the release's manifest, not from names. The per-group path reads the same prefetch. | Built (#14) |
| 2026-11-25 | **Stored records are read once per request.** A dataset's dimensions, files and mint contexts, and edge groups, are kept on the request's connection (`publish.request_memo`). They are immutable once stored, and nothing is kept between requests. | Built (#14) |
| 2026-11-25 | **A release's store is chosen per dataset.** Releases in MySQL keep being served from MySQL; releases published with a bucket index are served from a sha256-checked local cache of it. | Built (#12) |
| 2026-11-24 | **Bucket indexes are read through a local cache, not per query.** Reading the Parquet straight from S3 took 8 to 94 s a query. | Built (#12) |

Effect on `top-genes-with-sets` (IBD, 10 genes, 3 sets each): 1,114 database calls and 93 index queries
became 13 and 5, with identical responses and stored claims. `gene-across-traits` went from 258 and 35 to
14 and 9.

GraphQL, `rows` for the top 10 IBD genes, each with its measurement claim and 3 joined gene sets with roster
and measurement claims (70 claims): 325 database calls and 57 index queries became 19 and 5, with
identical responses, assertion counters and stored claims.

### Rules that keep serving fast for any collection

- No serving code issues a query per row, per claim or per edge group. Gather the ids, then read them with
  `IN (...)` in chunks of 500: `envelopes`, `app._fetch_payloads`, `app._stored_ids`, `store.groups`,
  `publish.prefetch_groups`.
- Lookups of stored, immutable records go through `publish.request_memo` (`dataset_dimensions`,
  `dataset_role_files`, `group_info`, `serve._release_contexts`).
- Which kinds and entities need what is read from the release's manifest, never from names or id prefixes.
- GraphQL list fields answer the whole list the first time an item asks, keeping the answer on the list's
  shared batch (`rows.row_envelope`, `rows.edge_envelope`, `rows.joined`); never per item.
- A batching change must leave responses byte-identical. Compare the query responses of `main` and the
  branch over copies of one database, and extend `tests/test_query_batching.py` when a new path is batched.

## Open: query-syntax changes that would make queries faster

These change what a query asks for or returns, so they are decisions for the people who write and use
queries. None is built.

| Proposal | What it saves | Cost |
| --- | --- | --- |
| **Claim detail level.** A request option `detail: ids \| claims \| full` (default `full`, today's behavior). `ids` returns each claim's id and resolver URL; `claims` adds the claim without evidence and value locators. | Most of the payload and the evidence reads. IBD with 10 genes and 3 sets each is 571 KB in full; with `claims: []` (rows only) it is 28 KB. The claims are still rendered and asserted. | Clients that need evidence ask for `full`, or resolve ids afterwards (`GET /v1/objects/{id}`). |
| **Rosters opt-in.** Roster claims (one per joined gene set) are the most expensive to render: each needs its group's member count and, the first time, its members. Leave them out unless the request lists them in `claims`, for example `claims: ["gene", "gene_set", "roster"]`. | Every group render and its reads, in queries that join. | Changes the default response of joined queries: a decision per collection or per query. |
| **Smaller default windows.** `topGenes: 20` and `setsPerGene: 10` serve up to 220 rows and 200 groups by default. A query template could default to 10 and 3 and keep the higher maximums. | Work grows with rows times claims, so this is linear. | A collection's query authors choose; it is a template default, not engine code. |
| **Cross-dataset queries name a scope.** A query whose anchor fixes no dimension (`gene-across-traits`) reads every dataset of the release, 1,444 for PIGEAN. A `datasets` limit, or a required dimension, bounds it. | Queries that read every trait. | Restricts what a query can ask. The alternative is a store change with no syntax change (next section). |
| **Paging.** `first` and `offset` on the anchor, as `GET /v1/collections/{c}` already has for its datasets. | Large result sets in pieces. | Clients page. |

The `claims` option already exists: a request may name the claim keys it wants (`claims: ["gene"]`), or
`claims: []` for rows only. It is the fastest path today.

## Next, without syntax changes

- **Keep cached indexes in memory.** Load each dataset's Parquet into DuckDB once rather than reopening the
  files on every index query: QA's half CPU pays for each open.
- **Fill the cache on startup.** QA's task runs on Spot and can be replaced at any time, which empties the
  cache; the first query after that pays for the downloads (7.9 s against 4 s warm).
- **One index file set per release.** For cross-dataset queries over many datasets: rows, metrics and edges
  of the whole release in a few files sorted by dataset and subject, instead of four files per dataset.
