"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { signOut } from "next-auth/react";
import { Activity, Database, FileCode2, LayoutDashboard, ScrollText } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/admin", label: "Overview", icon: LayoutDashboard },
  { href: "/admin/claims", label: "Claims", icon: ScrollText },
  { href: "/admin/templates", label: "Templates", icon: FileCode2 },
  { href: "/admin/tables", label: "Tables", icon: Database },
  { href: "/admin/activity", label: "Activity", icon: Activity },
];

/** Sidebar + the Admin V2 top bar; page titles start at the same height everywhere. */
export function AdminShell({ email, name, bypass, children }: { email: string; name: string | null; bypass: boolean; children: ReactNode }) {
  const pathname = usePathname();
  const current = NAV.find(item => (item.href === "/admin" ? pathname === "/admin" : pathname.startsWith(item.href))) ?? NAV[0];
  return (
    <div className="grid min-h-svh md:grid-cols-[224px_minmax(0,1fr)]">
      <aside className="flex flex-wrap items-center gap-1 border-b border-sidebar-border bg-sidebar p-3 text-sidebar-foreground md:sticky md:top-0 md:h-svh md:flex-col md:flex-nowrap md:items-stretch md:border-r md:border-b-0">
        <div className="px-2 py-1 md:pb-4">
          <div className="text-sm font-semibold">Claims repository</div>
          <div className="text-[11px] text-muted-foreground">Admin console</div>
        </div>
        <nav aria-label="Admin" className="flex flex-wrap gap-0.5 md:flex-col">
          {NAV.map(({ href, label, icon: Icon }) => {
            const active = current.href === href;
            return (
              <Link key={href} href={href} aria-current={active ? "page" : undefined}
                className={cn("flex items-center gap-2.5 rounded-md px-2 py-1.5 text-[13px] font-medium text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
                  active && "bg-card text-foreground shadow-xs ring-1 ring-border/50")}>
                <Icon className="size-4" aria-hidden />{label}
              </Link>
            );
          })}
        </nav>
        <div className="ml-auto px-2 text-xs md:mt-auto md:ml-0 md:border-t md:border-sidebar-border md:pt-3">
          <div className="font-medium break-all">{name || email}</div>
          {name && <div className="break-all text-muted-foreground">{email}</div>}
          {bypass
            ? <div className="mt-1.5 text-muted-foreground">Local development (sign-in bypassed)</div>
            : <button type="button" onClick={() => signOut({ callbackUrl: "/admin/signin" })} className="mt-1.5 text-muted-foreground hover:text-foreground hover:underline">Sign out</button>}
        </div>
      </aside>
      <div className="min-w-0">
        <div className="flex h-12 items-center justify-between gap-3 border-b border-border/50 px-4 text-xs sm:px-6">
          <span className="font-medium">{current.label}</span>
          <span className="text-muted-foreground">Claims repository administration</span>
        </div>
        <main className="w-full space-y-6 px-4 py-6 sm:px-6">{children}</main>
      </div>
    </div>
  );
}
