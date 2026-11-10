import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { AdminShell } from "@/components/admin/shell";
import { adminAccess } from "@/lib/auth";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: { default: "Admin", template: "%s · Claims admin" }, robots: { index: false, follow: false } };

/** Every console page requires an approved admin; the API proxy checks again. */
export default async function AdminLayout({ children }: { children: React.ReactNode }) {
  const access = await adminAccess();
  if (!access.allowed) redirect(access.signedIn ? "/admin/signin?error=AccessDenied" : "/admin/signin");
  return <AdminShell email={access.email} name={access.name} bypass={access.bypass}>{children}</AdminShell>;
}
