# Depositing objects into a collection

An object collection (`kind: objects`, [Collections](collections.md) §13)
holds DAPPER objects someone else minted: gene sets, knowledge gaps, mechanisms. Each release is opened, filled
by deposits naming the collection and the release, and closed. This page is how to do that from the `claims` CLI
with a `claims login` ([CLI login](cli-login.md)): no database password and no deposit key.

```text
server$ claims login --api qa                                         # once per 30 days
server$ claims objects register  $C --api qa                          # the definition; repeat after editing it
server$ claims objects deposit   $C 2026.11.0 --api qa --from s3://bucket/prefix/ --limit 20    # a trial
server$ claims objects deposit   $C 2026.11.0 --api qa --from s3://bucket/prefix/ --close       # the rest, closed
server$ claims objects status    $C 2026.11.0 --api qa
```

`$C` is the collection's directory (its `collection.yaml`, query templates and vocabulary files), for example
`kpn-dapper-toolkit/collections/cfde-genesets`. `--api` is `qa` or a service's base URL. `bin/claims` runs from
the repository, so pass absolute paths.

## Registering the definition

`claims objects register $C --api qa` checks the directory as `claims collection validate` does, then sends its
files (only the ones the manifest names), the directory's path in its git repository and its commit, with
`-dirty` when it has uncommitted changes (POST `/v1/publish/objects/{collection}:register`).

| Answer | Meaning |
| --- | --- |
| `registered` | The collection is new to the service. |
| `unchanged` | This definition is already registered. Nothing is written (not even the commit). |
| `refused` (409) | Another definition is registered. Releases already opened keep theirs; to use this one for new releases, run again with `--replace` (answer `replaced`). |
| `refused` (409) | The directory is a file collection, or its key is held by a file collection: publish those with `claims release publish --api`. |

Each file is stored as a DAPPER File whose location is its path in the repository. A File's id does not depend
on its location, so a definition registered earlier with `scripts/publish_collection.py` compares as unchanged.

`object_kinds` and `queries` are optional. A plain list (neither) takes any DAPPER objects: they are members of
each release (listed per class at `/v1/collections/{c}/releases/{v}/members?class=GeneSet`, each served at
`/v1/objects/{id}`), with no indexed fields and no named queries.

## Depositing

`claims objects deposit $C <version> --api qa --from <dir | s3://prefix/>`:

1. registers the definition when it is not registered yet, and stops when another one is (run
   `claims objects register --replace` first);
2. opens the release (opening an open release changes nothing; a closed one is refused);
3. selects the documents: every `*.dapper.yaml` under `--from`, or, with `--manifest <tsv> [--role <role>]`, the
   `relative_path` column of the manifest's rows whose `role` column is `<role>`. The manifest is a local file, an
   s3:// URI, or a path under `--from`. `--limit N` keeps the first N in path order (a trial);
4. sends each document's node and edge groups as deposits, unchanged: the producer's ids are kept. `prefixes`,
   `id` and any group that is not a list are left out, and so is each `--skip-group NAME` (repeatable);
5. prints a summary: documents, parts, objects, edges, items left out per group, and the number of unresolved
   references the receipts reported (summed over parts, so one id missing from several parts counts each time);
6. with `--close`, closes the release and prints what the service answers: its ResultCollection id, the objects
   per class and the ids the members reference that the repository does not hold (the first 100).

An s3:// source is mirrored with `aws s3 sync` under `<workdir>/source/<bucket>/<prefix>` (only the selected file
names, or `*.dapper.yaml`); files already there are not downloaded again. The workdir is `--workdir`, by
default `~/.cache/claims/objects` (or `$XDG_CACHE_HOME/claims/objects`), never the system temp directory.

The CFDE gene sets, for example:

```text
claims objects deposit ~/src/research/kpn-dapper-toolkit/collections/cfde-genesets 2026.11.0 --api qa \
  --from s3://kpn-opendore-releases/collections/cfde-genesets/2026.11/ \
  --manifest run/pigean_portal__cfde_2026_11_24.cfde_manifest.tsv --role provenance \
  --skip-group c2m2_files --workers 4 --close
```

(`c2m2_files` is left out because the repository's DAPPER profile cannot verify C2M2 file ids.)

### Limits and parts

A deposit holds at most 1,000 objects, 5,000 edges and 16 MB. The CLI sends a document in parts of at most 800
objects, 4,000 edges and 10 MB (its own size estimate). A deposit must hold an object, so a part that would hold
edges only repeats the document's first object; depositing an object again changes nothing. Documents are sent
`--workers` at a time (default 4). A 5xx or a dropped connection is tried again (honouring Retry-After); any
other refusal stops the run after the documents in flight.

### Resuming

Once every part of a document is in, its path is appended to the state file: `--state-file`, by default
`<workdir>/<collection>-<version>.sent`. Its first line names the service, collection and release, and a state
file of another deposit is refused. Running the same command again skips the documents listed and continues;
a document that stopped halfway is sent again whole, and its parts already in change nothing.

## Who is recorded, and what is public

| Recorded | Where |
| --- | --- |
| The login's token id and email on each deposit | `deposits.publisher_token_id`, `publisher_email` (`key_id` is null) |
| Who opened and who closed the release | `object_releases.opened_by_token_id` / `opened_by_email`, `closed_by_token_id` / `closed_by_email` |
| The release's publisher | `collection_releases.published_by` (the email) and `publisher_token_id` |
| Registration, open and close | audit events `object_collection_registered`, `object_release_opened`, `object_release_closed` |

A deposit made with a key keeps being recorded as before (`deposits.key_id`, `object_releases.opened_by` and
`closed_by`). Every row names exactly one writer.

Public reads (`GET /v1/deposits`, `/v1/deposits/{id}`) show a login's deposits as
`{"kind": "login", "name": "claims login"}`: no email and no token id. The admin console shows the email and
token id. `deposited_by=` filters by a key id or a token id. The release, its members and the objects are public
like every read.

Any approved login may register and deposit into any object collection, as it may publish any file collection;
there are no per-collection grants for logins. A deposit key still needs the collection's grant.

## With a deposit key instead

`--key-env CLAIMS_API_KEY` sends deposits, the open and the close with the deposit key in that environment variable
(the key needs a grant for the collection, from the admin console). The definition is then not checked:
register it with a login first.

## Schema

Login deposits need `schema/migrations/012_login_deposits.sql` (deposits' `key_id` and object releases'
`opened_by` become nullable; the token id, email and closer columns are added). It is additive: the previous
build keeps working on the migrated database. Apply it before deploying the build that accepts login deposits.
A local SQLite file is upgraded when the service connects.

A plain list (no `object_kinds`) needs this build on every service reading the shared database: an older build
expects `object_kinds` in a closed release's manifest, and its collection catalog (`/v1/queries`) and collection
page would fail once such a release is current. Close a plain list's first release only after the deploy.
