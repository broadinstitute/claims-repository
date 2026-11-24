import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { adminAccess } from "@/lib/auth";
import { safeNext } from "@/lib/admin/policy";
import { SignInButton } from "./sign-in-button";

export const metadata: Metadata = { title: "Sign in" };
export const dynamic = "force-dynamic";

const MESSAGES: Record<string, string> = {
  AccessDenied: "This Google account is not an approved admin for this console.",
  OAuthCallback: "Google sign-in did not complete. Try again.",
  OAuthSignin: "Could not start Google sign-in. Try again.",
  Callback: "Google sign-in did not complete. Try again.",
  Configuration: "Sign-in is not configured on this deployment.",
};

export default async function SignInPage({ searchParams }: { searchParams: Promise<{ error?: string; next?: string }> }) {
  const { error, next: wanted } = await searchParams;
  // Where to go after signing in, such as a CLI sign-in's /admin/device?code=... link. Console paths only.
  const next = safeNext(wanted);
  const access = await adminAccess();
  if (access.allowed) redirect(next);
  const configured = Boolean(process.env.AUTH_GOOGLE_ID && process.env.AUTH_GOOGLE_SECRET);
  const message = error ? MESSAGES[error] ?? "Sign-in did not complete. Try again." : null;
  return (
    <main className="grid min-h-svh place-items-center p-4">
      <div className="w-full max-w-sm rounded-xl border border-border/50 bg-card p-7">
        <div className="text-xs font-medium text-muted-foreground">Claims repository</div>
        <h1 className="mt-1.5 text-[22px] font-semibold tracking-tight">Admin console</h1>
        <p className="mt-1 mb-5 text-sm text-muted-foreground">Sign in with an approved Google account.</p>
        {message && <p role="alert" className="mb-4 text-sm text-destructive">{message}</p>}
        {configured ? <SignInButton next={next} /> : <p className="text-sm text-muted-foreground">Google sign-in is not configured on this deployment.</p>}
      </div>
    </main>
  );
}
