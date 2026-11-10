"use client";

import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Button } from "@/components/ui/button";
import { DataTable } from "@/components/admin/data-table";
import { Metric, PageHeading, Panel, QueryFeedback, Status, When, clock, ms, number, shortId, uptime } from "@/components/admin/shared";
import { adminGet, type Activity, type RouteStats } from "@/lib/admin/api";

type Assertion = Activity["recentAssertions"][number];
type AuthEvent = Activity["authEvents"][number];
type RootField = Activity["telemetry"]["graphql"]["rootFields"][number];

const routeColumns: ColumnDef<RouteStats, unknown>[] = [
  { id: "route", accessorFn: r => `${r.method} ${r.route}`, header: "Route", cell: ({ row }) => <span className="font-mono text-xs">{row.original.method} {row.original.route}</span> },
  { id: "count", accessorKey: "count", header: "Requests", meta: { align: "right" }, cell: ({ row }) => number(row.original.count) },
  { id: "errors4xx", accessorKey: "errors4xx", header: "4xx", meta: { align: "right" }, cell: ({ row }) => number(row.original.errors4xx) },
  { id: "errors5xx", accessorKey: "errors5xx", header: "5xx", meta: { align: "right" }, cell: ({ row }) => <span className={row.original.errors5xx ? "text-destructive" : undefined}>{number(row.original.errors5xx)}</span> },
  { id: "p50Ms", accessorKey: "p50Ms", header: "p50", meta: { align: "right" }, cell: ({ row }) => ms(row.original.p50Ms) },
  { id: "p95Ms", accessorKey: "p95Ms", header: "p95", meta: { align: "right" }, cell: ({ row }) => ms(row.original.p95Ms) },
  { id: "meanMs", accessorKey: "meanMs", header: "Mean", meta: { align: "right" }, cell: ({ row }) => ms(row.original.meanMs) },
  { id: "lastAt", accessorKey: "lastAt", header: "Last", meta: { muted: true }, cell: ({ row }) => <When value={row.original.lastAt} /> },
];

const assertionColumns: ColumnDef<Assertion, unknown>[] = [
  { id: "claim", header: "Claim", enableSorting: false, cell: ({ row }) => <div>
    <div className="font-medium">{row.original.triple ? `${row.original.triple.subject} ${row.original.triple.predicate} ${row.original.triple.object}` : shortId(row.original.claimId)}</div>
    <div className="mt-0.5 font-mono text-xs text-muted-foreground">{shortId(row.original.claimId)}</div>
  </div> },
  { id: "timesAsserted", accessorKey: "timesAsserted", header: "Assertions", meta: { align: "right" }, cell: ({ row }) => number(row.original.timesAsserted) },
  { id: "lastAssertedAt", accessorKey: "lastAssertedAt", header: "Last asserted", meta: { muted: true }, cell: ({ row }) => <When value={row.original.lastAssertedAt} /> },
];

const EVENT_TONE = { login: "good", logout: "neutral", login_denied: "bad" } as const;
const eventColumns: ColumnDef<AuthEvent, unknown>[] = [
  { id: "event", accessorKey: "event", header: "Event", cell: ({ row }) => <Status tone={EVENT_TONE[row.original.event] ?? "neutral"}>{row.original.event.replace("_", " ")}</Status> },
  { id: "email", accessorKey: "email", header: "Account", cell: ({ row }) => row.original.email ?? <span className="text-muted-foreground">Unknown</span> },
  { id: "detail", accessorKey: "detail", header: "Detail", enableSorting: false, meta: { muted: true }, cell: ({ row }) => <span className="text-xs">{row.original.detail}</span> },
  { id: "at", accessorKey: "at", header: "When", meta: { muted: true }, cell: ({ row }) => <When value={row.original.at} /> },
];

const fieldColumns: ColumnDef<RootField, unknown>[] = [
  { id: "field", accessorKey: "field", header: "Field", cell: ({ row }) => <span className="font-mono text-xs">{row.original.field}</span> },
  { id: "count", accessorKey: "count", header: "Requests", meta: { align: "right" }, cell: ({ row }) => number(row.original.count) },
];

export default function ActivityPage() {
  const router = useRouter();
  const query = useQuery({ queryKey: ["admin", "activity"], queryFn: () => adminGet<Activity>("activity"), refetchInterval: 30_000 });
  const d = query.data;
  const t = d?.telemetry;
  const sum = (key: "count" | "errors4xx" | "errors5xx") => (t?.routes ?? []).reduce((n, r) => n + r[key], 0);
  return <>
    <PageHeading title="Activity"
      subtitle={d && t ? `Updated ${clock(d.asOf)} · API counters cover this process since ${new Date(t.startedAt).toLocaleString()} and reset on deploy` : undefined}
      actions={<Button variant="outline" size="sm" disabled={query.isFetching} onClick={() => void query.refetch()}>Refresh</Button>} />
    {!d || !t ? <QueryFeedback pending={query.isPending} error={query.error} onRetry={() => void query.refetch()} /> : <>
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Metric title="Uptime" value={uptime(t.uptimeSeconds)} hint={`Since ${clock(t.startedAt)}`} />
        <Metric title="API requests" value={number(sum("count"))} hint={`${number(sum("errors5xx"))} server errors · ${number(sum("errors4xx"))} client errors`} />
        <Metric title="GraphQL" value={number(t.graphql.requests)} hint={`${number(t.graphql.ok)} succeeded · ${number(t.graphql.failed)} failed`} />
        <Metric title="Claims asserted" value={number(t.graphql.minted + t.graphql.reasserted)} hint={`${number(t.graphql.minted)} first mints · ${number(t.graphql.reasserted)} re-assertions`} />
      </div>
      <Panel title="API routes" description={`Latency p50/p95 over the latest ${t.sampleWindow} requests per route; mean over the process lifetime. Admin API requests are excluded.`}>
        <DataTable label="API routes" data={t.routes} columns={routeColumns} defaultSort={{ id: "count", desc: true }} getRowId={r => `${r.method} ${r.route}`} empty="No API requests since this process started." />
      </Panel>
      <div className="grid gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2">
          <Panel title="Recent assertions" description="Claims most recently served, newest first.">
            <DataTable label="Recent assertions" data={d.recentAssertions} columns={assertionColumns} getRowId={r => r.claimId}
              onRowClick={r => router.push(`/admin/claims?claim=${encodeURIComponent(r.claimId)}`)} rowLabel={r => `Inspect claim ${r.claimId}`} empty="No claims have been served yet." />
          </Panel>
        </div>
        <Panel title="GraphQL root fields" description="What clients asked for since this process started.">
          <DataTable label="GraphQL root fields" data={t.graphql.rootFields} columns={fieldColumns} defaultSort={{ id: "count", desc: true }} getRowId={r => r.field} empty="No GraphQL requests yet." />
        </Panel>
      </div>
      <div className="grid gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2">
          <Panel title="Admin sign-ins" description="Latest 50 sign-in, denial and sign-out events recorded by this console.">
            <DataTable label="Admin sign-ins" data={d.authEvents} columns={eventColumns} getRowId={r => `${r.at}-${r.event}-${r.email}`} empty="No sign-in events yet." />
          </Panel>
        </div>
        <Panel title="Approved admins" description="The API's CLAIMS_ADMIN_EMAILS. The console's ADMIN_EMAILS must also list an account." padded>
          <ul className="list-disc space-y-1 pl-4 text-sm">{d.approvedAdmins.map(e => <li key={e}>{e}</li>)}</ul>
        </Panel>
      </div>
    </>}
  </>;
}
