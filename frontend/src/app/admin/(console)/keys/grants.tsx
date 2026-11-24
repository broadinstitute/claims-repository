"use client";

import { useId, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Mono } from "@/components/admin/shared";
import { adminPost, type DepositKey, type KeyGrants } from "@/lib/admin/api";

const COLLECTION = /^[a-z0-9][a-z0-9._-]{0,63}$/;

/** A key's collection grants: which collections it may deposit into. A key with none can still make
 *  deposits that name no collection. Granting and withdrawing are logged, like issuing and revoking. */
export function Grants({ apiKey, queryKey }: { apiKey: DepositKey; queryKey: readonly unknown[] }) {
  const client = useQueryClient();
  const inputId = useId();
  const [collection, setCollection] = useState("");
  const settle = () => client.invalidateQueries({ queryKey });
  const grant = useMutation({
    mutationFn: (key: string) => adminPost<KeyGrants>(`keys/${encodeURIComponent(apiKey.keyId)}/grants`, { collection: key }),
    retry: false, onSuccess: () => setCollection(""), onSettled: settle,
  });
  const withdraw = useMutation({
    mutationFn: (key: string) => adminPost<KeyGrants>(
      `keys/${encodeURIComponent(apiKey.keyId)}/grants/${encodeURIComponent(key)}/withdraw`, {}),
    retry: false, onSettled: settle,
  });
  const wanted = collection.trim();
  const valid = COLLECTION.test(wanted);
  const busy = grant.isPending || withdraw.isPending;
  return <div className="min-w-48 space-y-1.5">
    {apiKey.collections.length ? <ul className="flex flex-wrap gap-1">
      {apiKey.collections.map(c => <li key={c} className="inline-flex items-center gap-1 rounded-md border border-border/60 px-1.5 py-0.5">
        <Mono>{c}</Mono>
        {!apiKey.revokedAt && <button type="button" disabled={busy} aria-label={`Withdraw ${apiKey.name}'s grant for ${c}`}
          className="text-xs text-muted-foreground hover:text-destructive disabled:opacity-50"
          onClick={() => withdraw.mutate(c)}>×</button>}
      </li>)}
    </ul> : <span className="text-xs text-muted-foreground">No collections</span>}
    {!apiKey.revokedAt && <form className="flex items-center gap-1" noValidate
      onSubmit={e => { e.preventDefault(); if (valid && !busy) grant.mutate(wanted); }}>
      <label htmlFor={inputId} className="sr-only">Grant {apiKey.name} a collection</label>
      <Input id={inputId} value={collection} onChange={e => { setCollection(e.target.value); grant.reset(); }}
        placeholder="collection key" className="h-7 w-36 text-xs" autoComplete="off" spellCheck={false} disabled={busy}
        aria-invalid={(wanted !== "" && !valid) || undefined} />
      <Button type="submit" size="sm" variant="outline" className="h-7" disabled={!valid || busy}>Grant</Button>
    </form>}
    {(grant.isError || withdraw.isError) && <p role="alert" className="text-xs text-destructive">
      {(grant.error ?? withdraw.error)?.message}</p>}
  </div>;
}
