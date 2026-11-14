"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { DataTable } from "@/components/admin/data-table";
import { Callout, CopyButton, Mono, PageHeading, Panel, Pre, QueryFeedback, Status, When, clock, number, plural } from "@/components/admin/shared";
import { adminGet, adminPost, type DepositKey, type DepositKeyList, type IssuedDepositKey, type RevokedDepositKey } from "@/lib/admin/api";

const KEYS = ["admin", "keys"];
const MAX_NAME = 120;

/** Why a name cannot be used, or null. Mirrors the API: 1-120 printable characters after trimming. */
function nameProblem(raw: string): string | null {
  const name = raw.trim();
  if (!name) return "Enter a name.";
  const length = Array.from(name).length;
  if (length > MAX_NAME) return `Use at most ${MAX_NAME} characters. This name has ${number(length)}.`;
  if (/[\u0000-\u001f]/.test(raw)) return "Use printable characters only (no tabs or line breaks).";
  return null;
}

/** The HTTP status of a failed admin call, or null when the request never got an answer. */
const statusOf = (error: Error | null): number | null => (error instanceof Error && "status" in error && typeof error.status === "number" ? error.status : null);

const USAGE = `curl -X POST <claims API>/v1/deposits \\
  -H "X-API-Key: <the key above>" \\
  -H "Content-Type: application/json" \\
  -d @deposit.json`;

export default function KeysPage() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: KEYS, queryFn: () => adminGet<DepositKeyList>("keys") });
  const d = query.data;

  // Issue. The key lives only in this mutation's result: never in the URL, storage, a query key or a log.
  // No retry (a repeated POST would mint a second key) and no cache time (reset and unmount drop it at once).
  const [name, setName] = useState("");
  const [touched, setTouched] = useState(false);
  const issue = useMutation({
    mutationFn: async (wanted: string) => {
      const result = await adminPost<IssuedDepositKey>("keys", { name: wanted });
      if (typeof result.key !== "string" || !result.key) {
        throw new Error("A key was created, but the response did not carry it. Revoke the new key in the list below, then issue another.");
      }
      return result;
    },
    retry: false, gcTime: 0,
    onSuccess: () => { setName(""); setTouched(false); },
    // Not awaited: the key must show as soon as it arrives, whatever happens to the list refresh.
    onSettled: () => { void client.invalidateQueries({ queryKey: KEYS }); },
  });
  const issued = issue.data ?? null;
  const resetIssue = issue.reset;
  useEffect(() => resetIssue, [resetIssue]);

  const nameId = useId();
  const hintId = useId();
  const errorId = useId();
  const nameInput = useRef<HTMLInputElement>(null);
  const issuedBox = useRef<HTMLDivElement>(null);
  const issuedKeyId = issued?.keyId;
  // Focus follows the key: to the callout when it appears, back to the (re-enabled) name input after Done.
  const hadIssued = useRef(false);
  useEffect(() => {
    if (issuedKeyId) issuedBox.current?.focus(); else if (hadIssued.current) nameInput.current?.focus();
    hadIssued.current = !!issuedKeyId;
  }, [issuedKeyId]);

  const issueStatus = statusOf(issue.error);
  const problem = nameProblem(name);
  const showProblem = touched && !!problem;
  const canIssue = !problem && !issue.isPending && !issued;

  // Revoke: the row's button opens a confirmation that names the key; the POST is never retried.
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const confirmBox = useRef<HTMLDivElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const revoke = useMutation({
    mutationFn: (key: DepositKey) => adminPost<RevokedDepositKey>(`keys/${encodeURIComponent(key.keyId)}/revoke`, {}),
    retry: false,
    onSettled: () => { setConfirmId(null); return client.invalidateQueries({ queryKey: KEYS }); },
  });
  const confirming = d?.keys.find(k => k.keyId === confirmId && !k.revokedAt) ?? null;
  const confirmingId = confirming?.keyId;
  useEffect(() => { if (confirmingId) confirmBox.current?.focus(); }, [confirmingId]);
  const resetRevoke = revoke.reset;
  const revoking = revoke.isPending;
  const cancel = () => { setConfirmId(null); if (opener.current?.isConnected) opener.current.focus(); };

  const columns = useMemo<ColumnDef<DepositKey, unknown>[]>(() => [
    { id: "name", accessorKey: "name", header: "Name", cell: ({ row }) => <span className="block min-w-40 break-words font-medium">{row.original.name}</span> },
    { id: "keyId", accessorKey: "keyId", header: "Key ID", cell: ({ row }) => <span className="inline-flex items-center gap-2 whitespace-nowrap">
      <Mono>{row.original.keyId}</Mono><CopyButton text={row.original.keyId} />
    </span> },
    { id: "state", accessorFn: r => (r.revokedAt ? 1 : 0), header: "State", cell: ({ row }) => row.original.revokedAt
      ? <div><Status>Revoked</Status><div className="mt-1 text-xs text-muted-foreground"><When value={row.original.revokedAt} />{row.original.revokedBy ? <div className="mt-0.5">by {row.original.revokedBy}</div> : null}</div></div>
      : <Status tone="good">Active</Status> },
    { id: "createdAt", accessorKey: "createdAt", header: "Created", meta: { muted: true }, cell: ({ row }) => <div>
      <When value={row.original.createdAt} />{row.original.createdBy ? <div className="mt-0.5 text-xs">by {row.original.createdBy}</div> : null}
    </div> },
    { id: "deposits", accessorKey: "deposits", header: "Deposits", meta: { align: "right" }, cell: ({ row }) => number(row.original.deposits) },
    { id: "lastDepositAt", accessorFn: r => r.lastDepositAt ?? "", header: "Last deposit", meta: { muted: true }, cell: ({ row }) => <When value={row.original.lastDepositAt} empty="Never" /> },
    // sr-only is absolutely positioned; the relative wrapper keeps it inside the table's scroll container at narrow widths.
    { id: "actions", header: () => <span className="relative"><span className="sr-only">Actions</span></span>, enableSorting: false, meta: { align: "right" }, cell: ({ row }) => row.original.revokedAt ? null : (
      <Button type="button" size="sm" variant="outline" disabled={revoking} aria-label={`Revoke ${row.original.name}`}
        className="border-destructive/30 text-destructive hover:bg-destructive/5 hover:text-destructive"
        onClick={event => { opener.current = event.currentTarget; resetRevoke(); setConfirmId(row.original.keyId); }}>Revoke</Button>
    ) },
  ], [revoking, resetRevoke]);

  const revoked = revoke.variables;
  const revokeStatus = statusOf(revoke.error);
  const active = d?.keys.filter(k => !k.revokedAt).length ?? 0;

  return <>
    <PageHeading title="Deposit keys"
      subtitle={<>Deposit keys authenticate <Mono>POST /v1/deposits</Mono>, the write API depositors use. Issue one key per depositor (an agent, a pipeline or a person&apos;s client). A key&apos;s name is public: it is shown to anyone reading who deposited an object.</>}
      actions={<Button variant="outline" size="sm" disabled={query.isFetching} onClick={() => void query.refetch()}>Refresh</Button>} />

    <Panel title="Issue a key" description="The key is shown once, right after it is issued. The service stores only a hash of it." padded>
      <div className="space-y-4">
        <form className="space-y-2" noValidate onSubmit={e => { e.preventDefault(); setTouched(true); if (canIssue) issue.mutate(name.trim()); }}>
          <label htmlFor={nameId} className="block text-xs text-muted-foreground">Name</label>
          <div className="flex flex-wrap items-start gap-2">
            <Input id={nameId} ref={nameInput} value={name} onChange={e => { setName(e.target.value); setTouched(true); if (issue.isError) issue.reset(); }}
              disabled={issue.isPending || !!issued} placeholder="REVEAL agent service (QA)" className="w-96 max-w-full" autoComplete="off" spellCheck={false}
              aria-invalid={showProblem || undefined} aria-describedby={showProblem ? `${hintId} ${errorId}` : hintId} />
            <Button type="submit" size="sm" className="h-9" disabled={!canIssue}>
              {issue.isPending ? <><Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden />Issuing…</> : "Issue key"}
            </Button>
          </div>
          <p id={hintId} className="text-xs text-muted-foreground">
            {issued ? "Select Done below before issuing another key." : `Who or what will deposit with this key. 1 to ${MAX_NAME} characters, shown publicly with every deposit the key makes.`}
          </p>
          {showProblem && <p id={errorId} role="alert" className="text-xs text-destructive">{problem}</p>}
        </form>
        {issue.isError && <Callout tone="bad" role="alert">
          {issueStatus === 400 || issueStatus === 422 ? "The API did not accept this name." : "The key could not be issued."} {issue.error.message}
          {issueStatus === null || issueStatus >= 500 ? " If the list below shows a new key you did not expect, revoke it: its key was never shown." : null}
        </Callout>}
        {issued && <div ref={issuedBox} tabIndex={-1} className="rounded-lg outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50">
          <Callout tone="good" role="status">
            <div className="space-y-3">
              <p className="font-medium">Key issued for {issued.name}. Copy it now: it will not be shown again.</p>
              <div className="flex flex-wrap items-center gap-3">
                <code className="break-all rounded-md border border-border/50 bg-background px-3 py-2 font-mono text-sm text-foreground">{issued.key}</code>
                <CopyButton text={issued.key} label="Copy key" />
              </div>
              <p>The service keeps only a hash (SHA-256) of this key, so nobody can read it back later, here or in the database. If it is lost, revoke <Mono>{issued.keyId}</Mono> and issue a new key.</p>
              <p>Pass it to the depositor privately. They send it in the <Mono>X-API-Key</Mono> header on <Mono>POST /v1/deposits</Mono>:</p>
              <Pre className="text-foreground">{USAGE}</Pre>
              <Button type="button" size="sm" variant="outline" onClick={() => issue.reset()}>Done</Button>
            </div>
          </Callout>
        </div>}
      </div>
    </Panel>

    <Panel title="Keys" description={d ? `Every key ever issued, oldest first, as of ${clock(d.asOf)}. Revoked keys stay listed because their deposits remain attributed to them.` : undefined}>
      {!d ? <div className="px-4"><QueryFeedback pending={query.isPending} error={query.error} onRetry={() => void query.refetch()} /></div> : <>
        {(confirming || revoke.isSuccess || revoke.isError || (d.keys.length > 0 && active === 0)) && <div className="space-y-3 border-b border-border/50 px-4 py-3">
          {confirming && <div ref={confirmBox} tabIndex={-1} role="group" aria-label={`Confirm revoking ${confirming.name}`} className="rounded-lg outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50">
            <Callout tone="warn">
              <div className="space-y-2">
                <p><span className="font-medium">Revoke {confirming.name}</span> (<Mono>{confirming.keyId}</Mono>)?</p>
                <p>Revoking stops new deposits with this key at once. Its past deposits ({plural(confirming.deposits, "deposit")}) stay attributed to it. A revoked key cannot be made active again; issue a new key instead.</p>
                <div className="flex flex-wrap gap-2">
                  <Button type="button" size="sm" variant="destructive" disabled={revoking} onClick={() => revoke.mutate(confirming)}>
                    {revoking ? <><Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden />Revoking…</> : "Revoke key"}
                  </Button>
                  <Button type="button" size="sm" variant="outline" disabled={revoking} onClick={cancel}>Cancel</Button>
                </div>
              </div>
            </Callout>
          </div>}
          {revoke.isSuccess && revoked && <Callout tone="good" role="status">Revoked {revoked.name} (<Mono>{revoked.keyId}</Mono>). It can no longer deposit; its past deposits stay attributed to it.</Callout>}
          {revoke.isError && revoked && (revokeStatus === 409
            ? <Callout tone="warn" role="status">{revoked.name} (<Mono>{revoked.keyId}</Mono>) was already revoked, so nothing changed. The list has been refreshed.</Callout>
            : revokeStatus === 404
              ? <Callout tone="bad" role="alert">The API does not know the key <Mono>{revoked.keyId}</Mono> ({revoked.name}), so nothing was revoked. The list has been refreshed.</Callout>
              : <Callout tone="bad" role="alert">{revoked.name} (<Mono>{revoked.keyId}</Mono>) could not be revoked. {revoke.error.message} Check its state in the refreshed list, then try again.</Callout>)}
          {d.keys.length > 0 && active === 0 && <Callout tone="warn" role="status">No key is active, so every deposit is refused until a new key is issued.</Callout>}
        </div>}
        <DataTable label="Deposit keys" data={d.keys} columns={columns} getRowId={k => k.keyId}
          empty="No deposit keys yet. Deposits are refused until a key is issued: POST /v1/deposits answers 401 without a valid key." />
      </>}
    </Panel>
  </>;
}
