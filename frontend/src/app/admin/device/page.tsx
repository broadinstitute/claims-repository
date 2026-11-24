import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { AdminShell } from "@/components/admin/shell";
import { adminAccess } from "@/lib/auth";
import { normalizeUserCode } from "@/lib/admin/policy";
import { DeviceApproval } from "./approval";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Approve CLI sign-in · Claims admin", robots: { index: false, follow: false } };

/**
 * Where `claims login` sends you: approve or deny a CLI sign-in. Outside the (console) group so that signing in
 * returns here with the code (the console layout's redirect would drop it).
 */
export default async function DevicePage({ searchParams }: { searchParams: Promise<{ code?: string }> }) {
  const { code } = await searchParams;
  const normalized = normalizeUserCode(code);
  const access = await adminAccess();
  if (!access.allowed) {
    const back = normalized ? `/admin/device?code=${normalized}` : "/admin/device";
    redirect(access.signedIn ? "/admin/signin?error=AccessDenied" : `/admin/signin?next=${encodeURIComponent(back)}`);
  }
  return <AdminShell email={access.email} name={access.name} bypass={access.bypass}>
    <DeviceApproval initialCode={normalized} />
  </AdminShell>;
}
