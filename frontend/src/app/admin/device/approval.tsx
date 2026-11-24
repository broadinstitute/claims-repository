"use client";

import { useId, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Callout, KeyValues, Mono, PageHeading, Panel, When } from "@/components/admin/shared";
import { AdminApiError, adminGet, adminPost, type DeviceDecision, type DeviceRequest } from "@/lib/admin/api";
import { normalizeUserCode } from "@/lib/admin/policy";

const shown = (code: string) => `${code.slice(0, 4)}-${code.slice(4)}`;

/** Look up a CLI sign-in by its code, then approve or deny it as the signed-in admin. */
export function DeviceApproval({ initialCode }: { initialCode: string | null }) {
  const [typed, setTyped] = useState(initialCode ? shown(initialCode) : "");
  const [code, setCode] = useState<string | null>(initialCode);
  const [touched, setTouched] = useState(false);
  const inputId = useId();
  const typedCode = normalizeUserCode(typed);

  const request = useQuery({
    queryKey: ["admin", "device", code],
    queryFn: () => adminGet<DeviceRequest>(`device/${code}`),
    enabled: !!code, retry: false, gcTime: 0, staleTime: Infinity,
  });
  const decide = useMutation({
    mutationFn: (approve: boolean) => adminPost<DeviceDecision>(`device/${code}/${approve ? "approve" : "deny"}`, {}),
    retry: false,
  });
  const d = request.data;
  const notFound = request.error instanceof AdminApiError && request.error.status === 404;

  return <>
    <PageHeading title="Approve a CLI sign-in"
      subtitle={<>The <Mono>claims</Mono> CLI signs in with a code it prints (<Mono>claims login</Mono>). Approving gives that machine a publisher token in your name for 30 days. You can end it any time under Keys, CLI sessions.</>} />

    <Panel title="Code" description="The 8-letter code the CLI printed, such as WDJB-MJHT." padded>
      <form className="flex flex-wrap items-start gap-2" noValidate onSubmit={e => {
        e.preventDefault(); setTouched(true);
        if (typedCode) { decide.reset(); setCode(typedCode); }
      }}>
        <label htmlFor={inputId} className="sr-only">Code</label>
        <Input id={inputId} value={typed} onChange={e => { setTyped(e.target.value); setTouched(false); }} placeholder="WDJB-MJHT"
          className="w-48 font-mono uppercase tracking-widest" autoComplete="off" spellCheck={false} disabled={decide.isPending}
          aria-invalid={(touched && !typedCode) || undefined} />
        <Button type="submit" size="sm" className="h-9" variant="outline" disabled={decide.isPending}>Look up</Button>
        {touched && !typedCode && <p role="alert" className="basis-full text-xs text-destructive">Enter the 8 letters the CLI printed (vowels and digits are never used).</p>}
      </form>
    </Panel>

    {code && <Panel title={`Sign-in ${shown(code)}`} padded>
      {request.isPending ? <p className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden />Looking up the code…</p>
        : decide.isSuccess ? (decide.data.status === "approved"
          ? <Callout tone="good" role="status">Approved. The CLI on <Mono>{decide.data.host ?? "the requesting machine"}</Mono> receives its token within a few seconds; it prints the account it signed in as.</Callout>
          : <Callout tone="warn" role="status">Denied. The CLI stops and nothing is issued.</Callout>)
        : notFound ? <Callout tone="bad" role="alert">No sign-in is waiting with this code. It may have expired (codes last 10 minutes), been decided already, or been mistyped. Run <Mono>claims login</Mono> again for a new code.</Callout>
        : request.error ? <Callout tone="bad" role="alert">The code could not be looked up. {request.error.message}</Callout>
        : d ? <div className="space-y-4">
          <Callout tone="warn">Only approve a code you started yourself, on a machine you recognize. Someone who sends you a code is asking for access in your name.</Callout>
          <KeyValues rows={[
            ["Host (reported by the CLI)", <Mono key="h">{d.host ?? "not given"}</Mono>],
            ["IP address", <Mono key="i">{d.ip ?? "unknown"}</Mono>],
            ["Client", <Mono key="c">{d.client ?? "not given"}</Mono>],
            ["Requested", <When key="r" value={d.requestedAt} />],
            ["Code expires", <When key="e" value={d.expiresAt} />],
          ]} />
          {decide.isError && <Callout tone="bad" role="alert">{decide.error instanceof AdminApiError && decide.error.status === 404
            ? "This sign-in expired or was decided in the meantime. Run claims login again for a new code."
            : `The decision was not recorded. ${decide.error.message}`}</Callout>}
          <div className="flex flex-wrap gap-2">
            <Button type="button" size="sm" disabled={decide.isPending} onClick={() => decide.mutate(true)}>
              {decide.isPending && decide.variables ? <><Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden />Approving…</> : "Approve"}
            </Button>
            <Button type="button" size="sm" variant="outline" disabled={decide.isPending} onClick={() => decide.mutate(false)}
              className="border-destructive/30 text-destructive hover:bg-destructive/5 hover:text-destructive">
              {decide.isPending && decide.variables === false ? "Denying…" : "Deny"}
            </Button>
          </div>
        </div> : null}
    </Panel>}
  </>;
}
