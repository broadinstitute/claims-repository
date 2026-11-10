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

/** Admin API paths the browser may reach through the proxy (GET only). */
const READ_PATHS = [
  /^me$/, /^overview$/, /^claims$/, /^claims\/dapper:[A-Za-z]+\.[A-Za-z0-9_-]{1,200}$/, /^templates$/,
  /^tables$/, /^tables\/[a-z_]{1,64}\/rows$/, /^activity$/,
];
export function readablePath(path: string): boolean {
  return READ_PATHS.some(re => re.test(path));
}
