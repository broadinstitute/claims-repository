"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback } from "react";

type Value = string | number | null | undefined;

/** View state lives in the URL (Admin V2): deliberate changes push history, typing replaces. */
export function useUrlState() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const update = useCallback((updates: Record<string, Value>, { push = true }: { push?: boolean } = {}) => {
    const next = new URLSearchParams(params.toString());
    for (const [key, value] of Object.entries(updates)) {
      if (value === undefined || value === null || value === "") next.delete(key); else next.set(key, String(value));
    }
    const url = `${pathname}${next.size ? `?${next}` : ""}`;
    if (push) router.push(url, { scroll: false }); else router.replace(url, { scroll: false });
  }, [params, router, pathname]);
  return { params, update };
}
