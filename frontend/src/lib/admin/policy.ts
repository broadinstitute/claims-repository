// Pure policy, shared by the server code and its tests.

/** Exact, case-insensitive, whitespace-trimmed membership in a comma-separated list. */
export function allowedAdmin(email: unknown, verified: unknown, list: string | undefined): boolean {
  if (typeof email !== "string" || verified !== true) return false;
  const wanted = email.trim().toLowerCase();
  if (!wanted) return false;
  return (list ?? "").split(",").map(e => e.trim().toLowerCase()).filter(Boolean).includes(wanted);
}

type Env = Record<string, string | undefined>;

/** Sign-in is skipped only by an explicit flag in `next dev`, never on Vercel or in a build. */
export function adminBypass(env: Env): boolean {
  return env.ADMIN_DEV_BYPASS === "true" && env.NODE_ENV === "development" && !env.VERCEL && !env.VERCEL_ENV;
}

// Collection keys and query names; job ids; deposit key ids. Kept in step with the admin API contract.
const KEY = "[a-z0-9][a-z0-9._-]{0,127}";
const JOB = "[A-Za-z0-9_-]{1,64}";
const KEY_ID = "key_[0-9a-f]{16}";
const TOKEN_ID = "tok_[0-9a-f]{16}";
// A CLI sign-in's user code as the API stores it: 8 of these 20 consonants, upper case, no dash.
const USER_CODE = "[BCDFGHJKLMNPQRSTVWXZ]{8}";
const DAPPER = "dapper:[A-Za-z]+\\.[A-Za-z0-9_-]{1,200}";
const exact = (pattern: string) => new RegExp(`^${pattern}$`);

/** Admin API paths the browser may read through the proxy (GET, purpose admin_read). */
const READ_PATHS = [
  "me", "overview",
  "collections", `collections/${KEY}`, `collections/${KEY}/queries/${KEY}/example`,
  "claims", `claims/${DAPPER}`,
  "objects", "objects/classes", `objects/${DAPPER}`, "deposits", "accounts", `accounts/${DAPPER}`,
  "tables", "tables/[a-z_]{1,64}/rows", "activity",
  "registration/sources", "registration/jobs", `registration/jobs/${JOB}`,
  "keys", "tokens",
].map(exact);
export function readablePath(path: string): boolean {
  return READ_PATHS.some(re => re.test(path));
}

/** Admin API paths the browser may write through the proxy (POST, purpose admin_write). */
const WRITE_PATHS = ["registration/check", "registration/jobs", "keys", `keys/${KEY_ID}/revoke`,
  `keys/${KEY_ID}/grants`, `keys/${KEY_ID}/grants/${KEY}/withdraw`, `tokens/${TOKEN_ID}/revoke`].map(exact);
export function writablePath(path: string): boolean {
  return WRITE_PATHS.some(re => re.test(path));
}

/**
 * CLI sign-in paths, signed with their own purpose (device_approve), never admin_read or admin_write: GET looks a
 * pending sign-in up, POST approves or denies it. Kept out of READ_PATHS and WRITE_PATHS.
 */
const DEVICE_READ = exact(`device/${USER_CODE}`);
const DEVICE_WRITE = exact(`device/${USER_CODE}/(approve|deny)`);
export function deviceReadPath(path: string): boolean {
  return DEVICE_READ.test(path);
}
export function deviceWritePath(path: string): boolean {
  return DEVICE_WRITE.test(path);
}

/** A user code as typed or linked (any case, a dash or spaces) in the stored form, or null. */
export function normalizeUserCode(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  const code = raw.replace(/[\s-]/g, "").toUpperCase();
  return new RegExp(`^${USER_CODE}$`).test(code) ? code : null;
}

/** Where sign-in may return to: a console path on this site, never another origin. */
export function safeNext(next: unknown): string {
  if (typeof next !== "string" || !next.startsWith("/admin") || next.startsWith("//") || /[\\\s]/.test(next)) return "/admin";
  if (!/^\/admin(\/[A-Za-z0-9/_-]*)?(\?[A-Za-z0-9=&_-]*)?$/.test(next)) return "/admin";
  return next;
}

/** Writes accept JSON only (`application/json`, parameters such as charset allowed). */
export function jsonContentType(value: string | null | undefined): boolean {
  return typeof value === "string" && value.split(";")[0].trim().toLowerCase() === "application/json";
}

/**
 * Same-origin check for writes: the Origin header's host must equal the
 * request's Host exactly. The session cookie is the only browser credential,
 * so a missing or unparseable Origin is refused.
 */
export function sameOrigin(origin: string | null | undefined, host: string | null | undefined): boolean {
  if (!origin || !host) return false;
  let parsed: URL;
  try { parsed = new URL(origin); } catch { return false; }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return false;
  if (parsed.origin !== origin) return false;
  return parsed.host === host;
}
