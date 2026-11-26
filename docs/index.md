# Claims Repository Documentation

Please use the links here to navigate to different documentation about the
Knowledge Portal Network Claims Repository service.

- [Introduction](introduction.md): what the service does, what DAPPER is, and how object identifiers are built.
- [Architecture](architecture.md): where the data lives (S3, the index bucket, Aurora MySQL, DuckDB) and how sign-in, publishing and a query move through the system. A two-page [draw.io diagram](architecture.drawio).
- [Inputs and outputs](inputs-and-outputs.md): what goes in and out of the repository, and why.
- [Method modules](method-modules.md): how each method's claim template ships its own GraphQL schema module.
- [Graph-query contract design](query-graph-design.md): execution semantics, storage design, and open decisions.
- [Admin console](admin-console.md): the `/admin` frontend and its read-only admin API.
- [Claim encoding diagram](claim-encoding.drawio): how claims are structured. This is a draw.io file; open it in [diagrams.net](https://app.diagrams.net/).
