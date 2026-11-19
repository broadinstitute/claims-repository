# Admin console

The admin console is the `/admin` section of the claims repository frontend, a Next.js app in [`frontend/`](../frontend) deployed on Vercel. Only the admin routes are built so far. It reads this service's admin API (`/v1/admin/api/*`) and, through it, issues and revokes deposit keys and, where enabled, registers collections. Its layout and components follow the akleao Collab Admin V2 panel: shadcn/ui, TanStack Query and Table, and the akleao theme tokens. Dark mode follows the OS.

| Page | What it shows |
| --- | --- |
| Overview | Minted (served at least once) and uncollected claims; assertion totals and 24h activity; the registered collections with their current release and claims served; claims minted per day (30 UTC days); objects by class; the KPN trait registry release |
| Collections | Every registered collection with its title, handle and ID, its current release, dataset count and claim counts |
| Collection detail (`/admin/collections/<uuid>`) | One collection at one release (the release picker lists earlier versions): its ID and handle, data model, datasets and their files, claim templates with example claims, metrics and tier rules, queries with a runnable example, source files and the raw manifest. The query example runs the query without serving claims. Collections are addressed by UUID. A handle in the URL still loads the collection, and the page replaces it with the UUID. |
| Add collection (`/admin/register`) | Registers a collection from files on the API machine's disk. The manifest needs an `id`. See "Add a collection". |
| Claims | Every served collection claim and every uncollected claim, with search, filters (collection, claim key, origin) and sorting, and pagination. Opening a claim re-renders it from its template and reports whether it still matches its content address and the stored copy. Opening never asserts or mints. |
| Tables | Exact row counts, sizes (MySQL) and applied/pending migrations; a read-only row browser in primary-key order with a contains/equals filter on one column. The legacy `pigean_*` tables are listed here. |
| Accounts (`/admin/accounts`) | Every deposited `ScientificAccount`, newest first: its question, claim count, who it is attributed to and who deposited it. Opening one shows the whole account as stored: question, context and closing remarks, the activity that built it, and each component claim with its proposition (and its subject, relation and object, or a note that it has only a statement), its evidence items, their publications and located quotes, the files those quotes address and what they derive from. Objects the account points at but the repository does not hold are listed. |
| Objects (`/admin/objects`) | Every stored object of any class, with a count per class and filters by class, text, deposit and what references an id. Opening one (`?object=<id>`, also from Accounts and Claims) shows every slot of the stored document, each referenced id as a link, the objects that reference it, the deposited edges that start or end at it and the deposits that carried it. A slot that is not shown is not in the database. Edges are not objects and appear only on the objects they join. Reads never mint or assert. |
| Keys (`/admin/keys`) | Deposit keys: issue a key for a depositor (shown once), see each key's deposits and last use, and revoke one. See "Deposit keys". |
| Activity | Per-route request counts, 4xx/5xx and p50/p95 latency; GraphQL outcomes, root fields, mints and re-assertions; recent assertions; console sign-ins and collection publishes |

An uncollected claim is a stored claim with no collection index row, such as a REST deposit.

Filters, page, the open claim, the browsed table and the collection release live in the URL. Overview and Activity refresh every 30 seconds.

## How access works

```
browser ──session cookie──▶ Next.js on Vercel ──signed assertion──▶ claims API (/v1/admin/api/*)
         (NextAuth, Google)    /api/admin/*     (HS256, 2 min)
```

1. **Sign-in.** NextAuth uses Google only, with the claims repository's own OAuth client. Sessions are JWTs with no database and last 12 hours. Sign-in requires a Google-verified email on the console's `ADMIN_EMAILS` list. The list is checked again on every page and every API call, so removing an address ends that admin's access at once.
2. **Proxy.** The browser only calls `/api/admin/*` on the console's own origin. That route checks the session, accepts only the paths it lists (GET for reads, POST for the registration and deposit-key paths), and calls the API with a fresh assertion. The assertion is HS256, signed with `CLAIMS_ADMIN_GATEWAY_SECRET`, and carries `iss` `claims-admin`, `aud` `claims-api`, a two-minute `exp`, `jti`, a `purpose` (`admin_read` for GET, `admin_write` for POST) and the verified email. Browsers never see the secret or an assertion.
3. **API.** [`service/admin_gateway.py`](../service/admin_gateway.py) verifies the signature, issuer, audience, expiry, lifetime (at most 10 minutes) and purpose. It requires a verified email but keeps no list of its own: the console's `ADMIN_EMAILS` is the only list of admins, and only the console holds the secret that signs an assertion. Without a configured secret the admin API is closed (503).
4. **Writes.** Four POST paths are writable through the proxy: `registration/check`, `registration/jobs`, `keys` and `keys/<key id>/revoke`. A POST must also have `Content-Type: application/json`, an `Origin` whose host equals the request's `Host` (the session cookie is the only browser credential, so cross-origin writes are refused), and a body of at most 16 KB. Failures return 415, 403 and 413. The API verifies an `admin_write` assertion with the same checks as `admin_read`, and neither purpose is accepted for the other. The registration routes also answer 403 unless the API process sets `CLAIMS_COLLECTION_ROOTS`, a list of directories. Every path they read (collection directory, release file, and each result file the release names) must resolve inside one of those roots after symlinks and `..` are followed, or the call fails with 400.
5. **Audit.** Sign-ins, denials and sign-outs are posted by the console server with a separate `auth_event` assertion to `POST /v1/admin/api/auth-events`. They are stored in `admin_auth_events` (migration `005_admin_console.sql`). That assertion cannot read, and a read assertion cannot write. Starting and finishing a publish job each add an `admin_auth_events` row (`collection_publish_started`, then `collection_publish_succeeded` or `collection_publish_failed`) with the admin's email, the collection directory name and the version. They appear in Activity. Issuing and revoking a deposit key are stored there too, as `api_key_issued` and `api_key_revoked` with the admin's email and the key id and name, never the key.

## Configuration

| Where | Setting | Value |
| --- | --- | --- |
| Console (Vercel) | `AUTH_SECRET` | Random, at least 32 bytes |
| | `NEXTAUTH_URL` | The console's URL |
| | `AUTH_GOOGLE_ID`, `AUTH_GOOGLE_SECRET` | The claims repository Google OAuth client |
| | `ADMIN_EMAILS` | Approved admins: the only list. To add someone, edit it in the Vercel project settings and redeploy (`vercel deploy --prod`) |
| | `CLAIMS_API_URL` | `https://api-qa.hugeampkpnbi.org/api/claims` (or prod) |
| | `CLAIMS_ADMIN_GATEWAY_SECRET` | Shared with the API |
| API (ECS) | `CLAIMS_ADMIN_GATEWAY_SECRET` | Same value, from the claims secret |
| | `CLAIMS_COLLECTION_ROOTS` | Optional. Directories (separated by the system path separator) the Add collection flow may read. Unset by default, which keeps the deployment read-only: registration routes answer 403. Set it only on a machine whose disk holds the collection files, such as a local API. |
| Google client | Authorized redirect URI | `<NEXTAUTH_URL>/api/auth/callback/google` |

Register `http://localhost:3000/api/auth/callback/google` on the client as well, so local sign-in works on port 3000. For local work without Google, `ADMIN_DEV_BYPASS=true` skips sign-in. It is honored only under `next dev` and never on Vercel; the API still requires the signed assertion. See [`frontend/README.md`](../frontend/README.md).

## Deposit keys

`POST /v1/deposits` needs a depositor's key ([inputs-and-outputs.md](inputs-and-outputs.md#deposit-keys)). The **Keys** page manages them.

- **Issue.** Give the key a name that says who or what deposits with it, such as "REVEAL agent service (QA)". The name is public: it is shown to anyone reading who deposited an object. The key is displayed once, in the response to that request. The service keeps only its SHA-256, so a lost key is replaced, not recovered.
- **List.** Each key with its id, state, who created it and when, how many deposits it has made and when it last deposited. The list never contains a key or a hash, and the table browser hides the hash column of `api_keys`.
- **Revoke.** Stops new deposits with that key at once. Its past deposits stay attributed to it; nothing is un-deposited.
- **Authority.** Issuing and revoking need the `admin_write` assertion, like registration; they are not gated by `CLAIMS_COLLECTION_ROOTS`. The admin's email comes from the signed assertion, never from the request body, and is recorded on the key and in the audit trail.
- **One database for QA and prod.** A key issued from either console works in both environments.

`scripts/api_key.py` does the same from a shell, for the first key or when the console is unavailable.

## Add a collection

A collection is a directory with `collection.yaml`, templates, queries and a `releases/` folder, plus a release file that lists each dataset's dimension values and result files. The manifest needs a top-level `id`, a lowercase UUID that identifies the collection, written once and never changed (make one with `uuidgen | tr A-Z a-z`). Its `collection:` field is the handle, a short name such as `pigean` that can be changed later and may not look like a UUID. The Add collection page needs `CLAIMS_COLLECTION_ROOTS` on the API. Without it the page says what to set and shows the CLI command. Four steps:

1. **Choose the source.** Pick a collection directory and release file found under the roots, or type the paths.
2. **Check.** Reads the files and writes nothing. Reports the manifest and its id, git state (uncommitted changes are a warning), release state (new collection, new version, already published, or conflict with a different release of the same version), and each dataset's dimensions and files. Release state is decided by the id. If the id is already registered under another handle or title, the check warns that publishing renames the collection. If another collection's id already owns the handle, the check reports an error and a publish is refused. Problems appear in the report.
3. **Preview sample claims.** Renders a few claims per template, with the same ids a publish records.
4. **Publish.** Runs the real publish as a background job, with an option to require a clean git tree. The page shows the job's phase and result.

The same publish from the command line, run from the repository root on the API machine:

```
.venv/bin/python scripts/publish_collection.py <collection dir> --release <release file> [--require-clean]
```

## Limits

- No SQL editor. Table and column names are allowlisted, and values are parameterized.
- Writes are limited to the sign-in audit, deposit keys and collection registration. Registration is off unless the API sets `CLAIMS_COLLECTION_ROOTS`; when on, it publishes only files inside those roots, one job at a time. The files must be on the API machine's disk, so the console cannot upload them.
- Publish jobs are held in the API process's memory (the latest 20) and are lost on restart. A published release stays in the database; check the collection page to see it.
- On SQLite a publish runs in one write transaction. Other requests to the API can fail with "database is locked" until it commits, which takes about 100 seconds for the PIGEAN IBD release.
- API counters are process-local. They reset on deploy and cover one task, which is all QA and prod run.
- Assertion history is a counter per claim plus first-mint and last-assertion times. The per-day chart therefore shows first mints, not re-assertions.
- NextAuth sessions are stateless JWTs. Signing out clears the browser's session, and removing an email from `ADMIN_EMAILS` (and redeploying) revokes access on the next request.
