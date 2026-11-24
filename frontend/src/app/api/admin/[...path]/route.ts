import { adminAccess } from "@/lib/auth";
import { deviceReadPath, deviceWritePath, jsonContentType, readablePath, sameOrigin, writablePath } from "@/lib/admin/policy";
import { apiBase, assertion, type Purpose } from "@/lib/admin/gateway";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const HEADERS = { "Cache-Control": "private, no-store", Vary: "Cookie" };
const MAX_BODY = 16 * 1024;

const fail = (error: string, status: number) => Response.json({ error }, { status, headers: HEADERS });

/** Forwards one call with a fresh assertion and maps the API's `{detail}` errors to `{error}`. */
async function forward(target: string, init: { method: "GET" | "POST"; search?: string; body?: string; purpose: Purpose; email: string; timeoutMs: number }) {
  try {
    const token = await assertion(init.email, init.purpose, { email: init.email, email_verified: true });
    const encoded = target.split("/").map(encodeURIComponent).join("/");
    const upstream = await fetch(`${apiBase()}/v1/admin/api/${encoded}${init.search ?? ""}`, {
      method: init.method,
      headers: { Authorization: `Bearer ${token}`, Accept: "application/json", ...(init.body !== undefined ? { "Content-Type": "application/json" } : {}) },
      body: init.body, cache: "no-store", signal: AbortSignal.timeout(init.timeoutMs),
    });
    const body = await upstream.json().catch(() => ({}));
    if (upstream.status === 401) {
      // The API rejected our signed assertion: a console/API configuration problem, not a missing console session.
      return fail("The claims API rejected the console's credentials. Check that both use the same CLAIMS_ADMIN_GATEWAY_SECRET, issuer and audience.", 502);
    }
    if (!upstream.ok) {
      const detail = typeof body.detail === "string" ? body.detail
        : Array.isArray(body.detail) ? "The request was not valid." : "The claims API is unavailable. Retry shortly.";
      return fail(detail, upstream.status);
    }
    return Response.json(body, { status: init.method === "POST" ? upstream.status : 200, headers: HEADERS });
  } catch {
    return fail("Could not reach the claims API. Check the connection, then retry.", 503);
  }
}

/** Joins and decodes the catch-all segments; null on a malformed percent-escape. */
function decodeTarget(path: string[]): string | null {
  try { return path.map(decodeURIComponent).join("/"); } catch { return null; }
}

/** Reads at most `limit` bytes of the body; null when it is larger. */
async function readCapped(request: Request, limit: number): Promise<string | null> {
  const declared = Number(request.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > limit) return null;
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > limit) { await reader.cancel().catch(() => {}); return null; }
    chunks.push(value);
  }
  const all = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { all.set(chunk, offset); offset += chunk.byteLength; }
  return new TextDecoder().decode(all);
}

/** Same-origin proxy: the browser's session in, a signed server assertion out. */
export async function GET(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const target = decodeTarget(path);
  const device = target !== null && deviceReadPath(target);
  if (target === null || !(device || readablePath(target))) return fail("Unknown admin route.", 404);
  const access = await adminAccess();
  if (!access.allowed) return fail("Admin access required.", access.signedIn ? 403 : 401);
  return forward(target, { method: "GET", search: device ? "" : new URL(request.url).search, purpose: device ? "device_approve" : "admin_read",
    email: access.email, timeoutMs: 30_000 });
}

/**
 * Registration writes (check, start a publish job), deposit key and CLI session
 * writes (issue, revoke), and CLI sign-in decisions. Only the writable paths,
 * only JSON, only from this origin, at most 16 KB, forwarded as-is under an
 * admin_write assertion.
 */
export async function POST(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const target = decodeTarget(path);
  const device = target !== null && deviceWritePath(target);
  if (target === null || !(device || writablePath(target))) return fail("Unknown admin route.", 404);
  const access = await adminAccess();
  if (!access.allowed) return fail("Admin access required.", access.signedIn ? 403 : 401);
  if (!jsonContentType(request.headers.get("content-type"))) return fail("Send the request body as application/json.", 415);
  if (!sameOrigin(request.headers.get("origin"), request.headers.get("host"))) return fail("Cross-origin admin writes are refused.", 403);
  const body = await readCapped(request, MAX_BODY);
  if (body === null) return fail("The request body is larger than 16 KB.", 413);
  // A check hashes every result file, which can take a while for a large release.
  // Approving or denying a CLI sign-in is signed device_approve, its own purpose.
  return forward(target, { method: "POST", body, purpose: device ? "device_approve" : "admin_write", email: access.email, timeoutMs: 120_000 });
}
