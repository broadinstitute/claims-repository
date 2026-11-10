import "server-only";
import type { NextAuthOptions } from "next-auth";
import { getServerSession } from "next-auth";
import GoogleProvider from "next-auth/providers/google";
import { adminBypass, allowedAdmin } from "./admin/policy";
import { auditEvent } from "./admin/gateway";

// Google only, approved admins only (the reveal-mechanisms pattern): the
// same Google OAuth client, JWT sessions with no database adapter, and an
// allowlist checked against the Google-verified email at sign-in and on every
// request (removing an address ends that admin's access at once).

const providers: NextAuthOptions["providers"] = [];
if (process.env.AUTH_GOOGLE_ID && process.env.AUTH_GOOGLE_SECRET) {
  providers.push(GoogleProvider({
    clientId: process.env.AUTH_GOOGLE_ID, clientSecret: process.env.AUTH_GOOGLE_SECRET,
    authorization: { params: { prompt: "select_account" } },
  }));
}

export const authOptions: NextAuthOptions = {
  providers,
  secret: process.env.AUTH_SECRET,
  session: { strategy: "jwt", maxAge: 12 * 60 * 60 },
  pages: { signIn: "/admin/signin", error: "/admin/signin" },
  callbacks: {
    async signIn({ account, profile }) {
      if (account?.provider !== "google") return false;
      const p = (profile ?? {}) as Record<string, unknown>;
      const email = typeof p.email === "string" ? p.email.trim().toLowerCase() : null;
      if (allowedAdmin(email, p.email_verified, process.env.ADMIN_EMAILS)) return true;
      await auditEvent("login_denied", email, p.email_verified === true ? "not an approved admin" : "unverified email");
      return false;
    },
    async jwt({ token, account, profile }) {
      // Only a verified OAuth callback sets identity; client session updates are ignored.
      if (account && profile) {
        const p = profile as Record<string, unknown>;
        token.adminEmail = typeof p.email === "string" ? p.email.trim().toLowerCase() : undefined;
        token.emailVerified = p.email_verified === true;
      }
      return token;
    },
    async session({ session, token }) {
      session.admin = { email: token.adminEmail ?? null, verified: token.emailVerified === true };
      return session;
    },
  },
  events: {
    async signIn({ profile }) {
      const email = typeof profile?.email === "string" ? profile.email.trim().toLowerCase() : null;
      await auditEvent("login", email);
    },
    async signOut({ token }) {
      await auditEvent("logout", (token?.adminEmail as string | undefined) ?? null);
    },
  },
};

export type AdminAccess =
  | { allowed: true; email: string; name: string | null; bypass: boolean }
  | { allowed: false; signedIn: boolean };

export async function adminAccess(): Promise<AdminAccess> {
  if (adminBypass(process.env)) {
    const email = (process.env.ADMIN_DEV_EMAIL || "").trim().toLowerCase();
    return { allowed: true, email, name: "Local development", bypass: true };
  }
  const session = await getServerSession(authOptions);
  const admin = session?.admin;
  if (admin?.email && allowedAdmin(admin.email, admin.verified, process.env.ADMIN_EMAILS)) {
    return { allowed: true, email: admin.email, name: session?.user?.name ?? null, bypass: false };
  }
  return { allowed: false, signedIn: !!session };
}

declare module "next-auth" {
  interface Session { admin?: { email: string | null; verified: boolean } }
}
declare module "next-auth/jwt" {
  interface JWT { adminEmail?: string; emailVerified?: boolean }
}
