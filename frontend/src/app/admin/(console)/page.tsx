"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Button } from "@/components/ui/button";
import { BarChart } from "@/components/admin/bar-chart";
import { DataTable } from "@/components/admin/data-table";
import { KeyValues, Metric, PageHeading, Panel, QueryFeedback, Status, When, clock, ms, number, plural, uptime } from "@/components/admin/shared";
import { adminGet, type CollectionSummary, type Overview } from "@/lib/admin/api";

const collectionColumns: ColumnDef<CollectionSummary, unknown>[] = [
  { id: "title", accessorKey: "title", header: "Collection", cell: ({ row }) => <div>
    <div className="font-medium">{row.original.title}</div>
    <div className="mt-0.5 font-mono text-xs text-muted-foreground">{row.original.key}</div>
  </div> },
  { id: "version", accessorFn: r => r.currentRelease?.version ?? "", header: "Current release", cell: ({ row }) => row.original.currentRelease
    ? <span className="font-mono text-xs">{row.original.currentRelease.version}</span> : <span className="text-muted-foreground">None</span> },
  { id: "minted", accessorFn: r => r.claims.minted, header: "Claims served", meta: { align: "right" }, cell: ({ row }) => number(row.original.claims.minted) },
];

type ClassCount = Overview["objectsByClass"][number];
const classColumns: ColumnDef<ClassCount, unknown>[] = [
  { id: "className", accessorKey: "className", header: "Class", cell: ({ row }) => <span className="font-mono text-xs">{row.original.className}</span> },
  { id: "count", accessorKey: "count", header: "Objects", meta: { align: "right" }, cell: ({ row }) => number(row.original.count) },
];

export default function OverviewPage() {
  const router = useRouter();
  const query = useQuery({ queryKey: ["admin", "overview"], queryFn: () => adminGet<Overview>("overview"), refetchInterval: 30_000 });
  const d = query.data;
  const subtitle = d
    ? `Updated ${clock(d.asOf)} · ${d.database.backend === "mysql" ? "Aurora MySQL" : "SQLite"} ${d.database.name} · ping ${ms(d.database.pingMs)}`
    : query.error ? "Snapshot unavailable" : "Loading snapshot…";
  return <>
    <PageHeading title="System overview" subtitle={query.error && d ? `Last refresh failed; showing ${clock(d.asOf)} data` : subtitle}
      actions={<Button variant="outline" size="sm" onClick={() => void query.refetch()} disabled={query.isFetching}>Refresh</Button>} />
    {!d ? <QueryFeedback pending={query.isPending} error={query.error} onRetry={() => void query.refetch()} /> : <>
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Metric title="Collections" value={number(d.collections.length)} hint={`Claims are rendered from their rows when a query serves them`} href="/admin/collections" />
        <Metric title="Minted" value={number(d.claims.minted)} hint={`Claims served at least once · ${number(d.claims.uncollected)} with no collection`} href="/admin/claims" />
        <Metric title="Assertions" value={number(d.claims.assertions)} hint={`In 24h: ${plural(d.claims.asserted24h, "claim")} asserted, ${number(d.claims.minted24h)} first minted`} href="/admin/claims?sort=asserted" />
        <Metric title="API requests" value={number(d.api.requests)} hint={`Up ${uptime(d.api.uptimeSeconds)} · ${number(d.api.errors5xx)} server errors · ${number(d.api.graphql.requests)} GraphQL`} href="/admin/activity" />
      </div>
      <div className="grid gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2">
          <Panel title="Collections" description="Each collection is a released set of result files with the templates that mint claims from them. Counts are for the current release: claims served so far."
            action={<Button asChild variant="outline" size="sm"><Link href="/admin/collections">All collections</Link></Button>}>
            <DataTable label="Collections" data={d.collections} columns={collectionColumns} defaultSort={{ id: "minted", desc: true }}
              getRowId={r => r.key} onRowClick={r => router.push(`/admin/collections/${encodeURIComponent(r.key)}`)} rowLabel={r => `Open collection ${r.title}`}
              empty="No collections are registered yet." />
          </Panel>
        </div>
        <Panel title="KPN trait registry" description="Shared by every collection that resolves a trait dimension." padded>
          {d.traitRegistry ? <KeyValues rows={[
            ["Release", <span key="r" className="inline-flex items-center gap-2">{d.traitRegistry.version}{d.traitRegistry.published ? <Status tone="good">published</Status> : <Status tone="warn">unpublished</Status>}</span>],
            ["Commit", d.traitRegistry.commit ? <span className="font-mono text-xs">{d.traitRegistry.commit.slice(0, 12)}</span> : "—"],
            ["Traits", number(d.traitRegistry.traits)],
            ["Mappings", number(d.traitRegistry.mappings)],
            ["Loaded", <When key="l" value={d.traitRegistry.loadedAt} />],
          ]} /> : <p className="text-sm text-muted-foreground">No trait registry is loaded.</p>}
        </Panel>
      </div>
      <div className="grid gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2">
          <Panel title="Claims minted per day" description="First serves over the last 30 UTC days. Re-assertions are counted per claim, not per day." padded>
            <BarChart label="Claims minted" data={d.mintTimeline.map(p => ({ value: p.minted, label: new Date(`${p.date}T00:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" }) }))} />
          </Panel>
        </div>
        <Panel title="Stored objects by class" description="Every DAPPER document in the object store.">
          <DataTable label="Objects by class" data={d.objectsByClass} columns={classColumns} defaultSort={{ id: "count", desc: true }} getRowId={r => r.className} empty="The object store is empty." />
        </Panel>
      </div>
    </>}
  </>;
}
