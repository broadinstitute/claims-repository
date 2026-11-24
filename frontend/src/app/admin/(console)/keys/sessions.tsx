"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Button } from "@/components/ui/button";
import { DataTable } from "@/components/admin/data-table";
import { Callout, Mono, Panel, QueryFeedback, Status, When, clock } from "@/components/admin/shared";
import { adminGet, adminPost, type CliSession, type CliSessionList } from "@/lib/admin/api";

const SESSIONS = ["admin", "tokens"];

/** CLI sessions: publisher tokens issued by approved `claims login` sign-ins, with Revoke. */
export function CliSessions() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: SESSIONS, queryFn: () => adminGet<CliSessionList>("tokens") });
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const revoke = useMutation({
    mutationFn: (s: CliSession) => adminPost(`tokens/${encodeURIComponent(s.tokenId)}/revoke`, {}),
    retry: false,
    onSettled: () => { setConfirmId(null); return client.invalidateQueries({ queryKey: SESSIONS }); },
  });
  const revoking = revoke.isPending;
  const columns = useMemo<ColumnDef<CliSession, unknown>[]>(() => [
    { id: "email", accessorKey: "email", header: "Signed in as", cell: ({ row }) => <span className="font-medium">{row.original.email}</span> },
    { id: "host", accessorFn: r => r.host ?? "", header: "Host", cell: ({ row }) => <Mono>{row.original.host ?? "—"}</Mono> },
    { id: "tokenId", accessorKey: "tokenId", header: "Session", cell: ({ row }) => <Mono>{row.original.tokenId}</Mono> },
    { id: "state", accessorKey: "state", header: "State", cell: ({ row }) => row.original.state === "active" ? <Status tone="good">Active</Status>
      : row.original.state === "expired" ? <Status>Expired</Status>
      : <div><Status>Revoked</Status><div className="mt-1 text-xs text-muted-foreground"><When value={row.original.revokedAt} />{row.original.revokedBy ? <div>by {row.original.revokedBy}</div> : null}</div></div> },
    { id: "createdAt", accessorKey: "createdAt", header: "Signed in", meta: { muted: true }, cell: ({ row }) => <When value={row.original.createdAt} /> },
    { id: "lastUsedAt", accessorFn: r => r.lastUsedAt ?? "", header: "Last used", meta: { muted: true }, cell: ({ row }) => <When value={row.original.lastUsedAt} empty="Never" /> },
    { id: "expiresAt", accessorKey: "expiresAt", header: "Expires", meta: { muted: true }, cell: ({ row }) => <When value={row.original.expiresAt} /> },
    { id: "actions", header: () => <span className="relative"><span className="sr-only">Actions</span></span>, enableSorting: false, meta: { align: "right" },
      cell: ({ row }) => row.original.state !== "active" ? null : confirmId === row.original.tokenId
        ? <span className="inline-flex gap-1">
            <Button type="button" size="sm" variant="destructive" disabled={revoking} onClick={() => revoke.mutate(row.original)}>{revoking ? "Revoking…" : "Revoke"}</Button>
            <Button type="button" size="sm" variant="outline" disabled={revoking} onClick={() => setConfirmId(null)}>Cancel</Button>
          </span>
        : <Button type="button" size="sm" variant="outline" disabled={revoking} aria-label={`Revoke the session of ${row.original.email} on ${row.original.host ?? "an unknown host"}`}
            className="border-destructive/30 text-destructive hover:bg-destructive/5 hover:text-destructive"
            onClick={() => { revoke.reset(); setConfirmId(row.original.tokenId); }}>Revoke…</Button> },
  ], [confirmId, revoking, revoke]);
  const d = query.data;
  return <Panel title="CLI sessions"
    description={d ? `Machines signed in with claims login (approved at /admin/device), newest first, as of ${clock(d.asOf)}. A session lasts 30 days; revoking ends it at once.` : undefined}
    action={<Button variant="outline" size="sm" disabled={query.isFetching} onClick={() => void query.refetch()}>Refresh</Button>}>
    {!d ? <div className="px-4"><QueryFeedback pending={query.isPending} error={query.error} onRetry={() => void query.refetch()} /></div> : <>
      {revoke.isError && <div className="border-b border-border/50 px-4 py-3"><Callout tone="bad" role="alert">The session could not be revoked. {revoke.error.message}</Callout></div>}
      {revoke.isSuccess && <div className="border-b border-border/50 px-4 py-3"><Callout tone="good" role="status">Revoked <Mono>{revoke.variables?.tokenId}</Mono>. That machine must run claims login again.</Callout></div>}
      <DataTable label="CLI sessions" data={d.tokens} columns={columns} getRowId={s => s.tokenId}
        empty="No CLI sessions yet. Run claims login on a machine, then approve its code at /admin/device." />
    </>}
  </Panel>;
}
