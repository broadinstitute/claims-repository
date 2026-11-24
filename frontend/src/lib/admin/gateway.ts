import "server-only";
import { SignJWT } from "jose";

// The claims API trusts this server through short-lived HS256 assertions
// signed with CLAIMS_ADMIN_GATEWAY_SECRET (the reveal gateway pattern). The
// browser never sees the secret or an assertion.

export const apiBase = () => (process.env.CLAIMS_API_URL || "http://127.0.0.1:8100").replace(/\/+$/, "");

function secret() {
  const value = process.env.CLAIMS_ADMIN_GATEWAY_SECRET;
  if (!value || value.length < 32) throw new Error("CLAIMS_ADMIN_GATEWAY_SECRET must contain at least 32 characters");
  return new TextEncoder().encode(value);
}

// admin_read: GET proxy; admin_write: the registration, deposit key and token POSTs; auth_event: sign-in audit;
// device_approve: looking up, approving and denying a CLI sign-in. The API accepts each purpose only on its own routes.
export type Purpose = "admin_read" | "admin_write" | "auth_event" | "device_approve";

export async function assertion(subject: string, purpose: Purpose, claims: Record<string, unknown> = {}) {
  return new SignJWT({ purpose, ...claims })
    .setProtectedHeader({ alg: "HS256", typ: "JWT" })
    .setSubject(subject)
    .setIssuer(process.env.CLAIMS_ADMIN_GATEWAY_ISSUER || "claims-admin")
    .setAudience(process.env.CLAIMS_ADMIN_GATEWAY_AUDIENCE || "claims-api")
    .setIssuedAt().setExpirationTime("2m").setJti(crypto.randomUUID())
    .sign(secret());
}

/** Records a sign-in, denial or sign-out in the API's audit table. Never throws: auditing must not block sign-in. */
export async function auditEvent(event: "login" | "login_denied" | "logout", email: string | null, detail?: string) {
  try {
    const token = await assertion("claims-admin-console", "auth_event");
    await fetch(`${apiBase()}/v1/admin/api/auth-events`, {
      method: "POST", cache: "no-store", signal: AbortSignal.timeout(5000),
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ event, email, detail: detail ?? null }),
    });
  } catch (error) {
    console.error("claims admin audit event failed", error instanceof Error ? error.message : error);
  }
}
