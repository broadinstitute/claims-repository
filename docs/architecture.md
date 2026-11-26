# Architecture

Where the data lives and how it moves: result files stay in the data owner's S3, each release's query index
(Parquet) sits in a bucket next to them, and Aurora MySQL keeps only bookkeeping and the claims actually
served. The source is [architecture.drawio](architecture.drawio) (two pages); open it in
[diagrams.net](https://app.diagrams.net/) or the draw.io app to edit.

![Architecture: S3, the publishing machine, the admin console, the QA service and Aurora MySQL, with the
sign-in, publish and query flows](assets/architecture.png)

## One query, step by step

Where each step of a GraphQL or REST query runs (MySQL, DuckDB over the cached index, S3 on a cache miss),
and what batching does to the number of calls ([query-performance.md](query-performance.md)).

![The steps of one query: request, release and datasets, store, anchor, joins, gene sets, claims,
envelopes, response](assets/architecture-query-path.png)

See also: [publishing-to-a-deployment.md](publishing-to-a-deployment.md) (the publish path and its
checks) and [cli-login.md](cli-login.md) (sign-in).
