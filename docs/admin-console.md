# Admin console

The admin console is the `/admin` section of the claims repository frontend, a Next.js app in [`frontend/`](../frontend) deployed on Vercel. Only the admin routes are built so far. It reads this service's admin API (`/v1/admin/api/*`). Its layout and components follow the akleao Collab Admin V2 panel: shadcn/ui, TanStack Query and Table, and the akleao theme tokens. Dark mode follows the OS.

| Page | What it shows |
| --- | --- |
| Overview | Implied, minted and deposited claims; assertion totals and 24h activity; per-family coverage; claims minted per day (30 UTC days); objects by class; the KPN trait registry release; phenotypes with results |
| Claims | Every implied and deposited claim, with search, filters (family, minted/implied, phenotype by any key), sorting and pagination. Opening a claim re-synthesizes it from its index row and reports whether it still matches its content address and the stored copy. Opening never asserts. |
| Templates | Each claim template with its source sha256, GraphQL module, claim families and counts, metric semantics (provisional or confirmed), and the persisted query templates with their default variables |
| Tables | Exact row counts, sizes (MySQL) and applied/pending migrations; a read-only row browser in primary-key order with a contains/equals filter on one column |
| Activity | Per-route request counts, 4xx/5xx and p50/p95 latency; GraphQL outcomes, root fields, mints and re-assertions; recent assertions; console sign-ins |

Filters, page, the open claim and the browsed table live in the URL. Overview and Activity refresh every 30 seconds.

## How access works

```
browser ──session cookie──▶ Next.js on Vercel ──signed assertion──▶ claims API (/v1/admin/api/*)
         (NextAuth, Google)    /api/admin/*     (HS256, 2 min)
```

1. **Sign-in.** NextAuth uses Google only, with the claims repository's own OAuth client. Sessions are JWTs with no database and last 12 hours. Sign-in requires a Google-verified email on the console's `ADMIN_EMAILS` list. The list is checked again on every page and every API call, so removing an address ends that admin's access at once.
2. **Proxy.** The browser only calls `/api/admin/*` on the console's own origin. That route checks the session, accepts only the read paths, and calls the API with a fresh assertion. The assertion is HS256, signed with `CLAIMS_ADMIN_GATEWAY_SECRET`, and carries `iss` `claims-admin`, `aud` `claims-api`, a two-minute `exp`, `jti`, `purpose` `admin_read` and the verified email. Browsers never see the secret or an assertion.
3. **API.** [`service/admin_gateway.py`](../service/admin_gateway.py) verifies the signature, issuer, audience, expiry, lifetime (at most 10 minutes) and purpose. It also checks the email against its own `CLAIMS_ADMIN_EMAILS`. Without a configured secret the admin API is closed (503).
4. **Audit.** Sign-ins, denials and sign-outs are posted by the console server with a separate `auth_event` assertion to `POST /v1/admin/api/auth-events`. They are stored in `admin_auth_events` (migration `005_admin_console.sql`). That assertion cannot read, and a read assertion cannot write.

## Configuration

| Where | Setting | Value |
| --- | --- | --- |
| Console (Vercel) | `AUTH_SECRET` | Random, at least 32 bytes |
| | `NEXTAUTH_URL` | The console's URL |
| | `AUTH_GOOGLE_ID`, `AUTH_GOOGLE_SECRET` | The claims repository Google OAuth client |
| | `ADMIN_EMAILS` | Approved admins |
| | `CLAIMS_API_URL` | `https://api-qa.hugeampkpnbi.org/api/claims` (or prod) |
| | `CLAIMS_ADMIN_GATEWAY_SECRET` | Shared with the API |
| API (ECS) | `CLAIMS_ADMIN_GATEWAY_SECRET` | Same value, from the claims secret |
| | `CLAIMS_ADMIN_EMAILS` | Approved admins, in `service.yaml` |
| Google client | Authorized redirect URI | `<NEXTAUTH_URL>/api/auth/callback/google` |

Register `http://localhost:3000/api/auth/callback/google` on the client as well, so local sign-in works on port 3000. For local work without Google, `ADMIN_DEV_BYPASS=true` skips sign-in. It is honored only under `next dev` and never on Vercel; the API still requires the signed assertion. See [`frontend/README.md`](../frontend/README.md).

## Limits

- Read-only: no SQL editor and no write actions beyond the sign-in audit. Table and column names are allowlisted, and values are parameterized.
- API counters are process-local. They reset on deploy and cover one task, which is all QA and prod run.
- Assertion history is a counter per claim plus first-mint and last-assertion times. The per-day chart therefore shows first mints, not re-assertions.
- NextAuth sessions are stateless JWTs. Signing out clears the browser's session, and removing an email from either allowlist revokes access on the next request.
