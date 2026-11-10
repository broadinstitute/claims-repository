import { adminAccess } from "@/lib/auth";
import { readablePath } from "@/lib/admin/policy";
import { apiBase, assertion } from "@/lib/admin/gateway";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const HEADERS = { "Cache-Control": "private, no-store", Vary: "Cookie" };

/** Same-origin proxy: the browser's session in, a signed server assertion out. */
export async function GET(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const target = path.map(decodeURIComponent).join("/");
  if (!readablePath(target)) return Response.json({ error: "Unknown admin route." }, { status: 404, headers: HEADERS });
  const access = await adminAccess();
  if (!access.allowed) {
    return Response.json({ error: "Admin access required." }, { status: access.signedIn ? 403 : 401, headers: HEADERS });
  }
  try {
    const token = await assertion(access.email, "admin_read", { email: access.email, email_verified: true });
    const search = new URL(request.url).search;
    const encoded = target.split("/").map(encodeURIComponent).join("/");
    const upstream = await fetch(`${apiBase()}/v1/admin/api/${encoded}${search}`, {
      headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
      cache: "no-store", signal: AbortSignal.timeout(30_000),
    });
    const body = await upstream.json().catch(() => ({}));
    if (!upstream.ok) {
      const detail = typeof body.detail === "string" ? body.detail
        : Array.isArray(body.detail) ? "The request was not valid." : "The claims API is unavailable. Retry shortly.";
      return Response.json({ error: detail }, { status: upstream.status, headers: HEADERS });
    }
    return Response.json(body, { headers: HEADERS });
  } catch {
    return Response.json({ error: "Could not reach the claims API. Check the connection, then retry." }, { status: 503, headers: HEADERS });
  }
}
