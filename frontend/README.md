# Claims repository frontend

Next.js app for the claims repository, deployed on Vercel. Only the admin console is built so far (`/admin/*`); the public frontend will live at the root.

```
src/app/
  page.tsx                  /  (redirects to /admin until the public frontend exists)
  admin/signin/             /admin/signin  Google sign-in for approved admins
  admin/(console)/          /admin, /admin/collections (+ /[ref], a collection's UUID or handle), /admin/register (add a collection), /admin/claims, /admin/keys (deposit keys), /admin/tables, /admin/activity
  api/auth/[...nextauth]/   NextAuth (Google)
  api/admin/[...path]/      same-origin proxy to the claims admin API, with a signed assertion
                            (GET reads; POST only for collection registration (registration/check,
                            registration/jobs) and deposit keys (keys, keys/<key id>/revoke): JSON,
                            same origin, 16 KB)
src/components/ui/          shadcn/ui components (shared)
src/components/admin/       admin console building blocks
src/lib/auth.ts             NextAuth options and adminAccess()
src/lib/admin/              admin API client (browser), gateway signing (server), access policy
```

See [docs/admin-console.md](../docs/admin-console.md) for the console's pages and access model.

## Run locally

```bash
npm ci
cp .env.example .env.local   # fill in AUTH_SECRET and CLAIMS_ADMIN_GATEWAY_SECRET (openssl rand -hex 32)
npm run dev                  # http://localhost:3000/admin
```

The API must run with the same gateway secret and list your email:

```bash
CLAIMS_ADMIN_GATEWAY_SECRET=<same value> CLAIMS_ADMIN_EMAILS=<you> \
  ../.venv/bin/python -m uvicorn service.app:app --port 8100 --app-dir ..
```

Google sign-in uses the claims repository's own OAuth client. Put `AUTH_GOOGLE_ID` and `AUTH_GOOGLE_SECRET` in `.env.local`; the client must allow `http://localhost:3000/api/auth/callback/google`. Or set `ADMIN_DEV_BYPASS=true` to skip sign-in under `next dev`.

The Keys page (`/admin/keys`) issues, lists and revokes deposit keys, which depositors send as `X-API-Key` on `POST /v1/deposits`. A key is shown once, when it is issued; the console keeps it only in that page's memory and the API stores only its SHA-256.

To try Add collection locally, also start the API with `CLAIMS_COLLECTION_ROOTS` set to the directory that holds the collection, for example a local kpn-dapper-toolkit checkout (`CLAIMS_COLLECTION_ROOTS=$HOME/src/kpn-dapper-toolkit`). Separate several directories with `:`. Without it the page explains what to set, and registration stays off.

## Checks

```bash
npm test          # access policy and proxy path/origin rules
npm run typecheck
npm run build
```

## Deploy (Vercel)

Production: **https://claims-repository.vercel.app** (Vercel project `chase-yakaboskis-projects/claims-repository`, against the QA claims API). Deploy from this directory with `vercel deploy --prod` (CLI 47.2.2 or later); `.vercelignore` keeps local env files out of the upload.

For a new deployment: framework Next.js (`vercel.json`). Set the production environment variables listed in [docs/admin-console.md](../docs/admin-console.md#configuration); `NEXTAUTH_URL` is not needed on Vercel, where NextAuth uses the request host. Then add `<production URL>/api/auth/callback/google` to the Google client's authorized redirect URIs.
