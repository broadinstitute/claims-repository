"use client";

import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Button } from "@/components/ui/button";
import { BarChart } from "@/components/admin/bar-chart";
import { DataTable } from "@/components/admin/data-table";
import { FAMILY_LABEL, KeyValues, Meter, Metric, PageHeading, Panel, QueryFeedback, Status, When, clock, ms, number, percent, plural, uptime } from "@/components/admin/shared";
import { adminGet, type FamilyStats, type Overview } from "@/lib/admin/api";
import { useRouter } from "next/navigation";

const familyColumns: ColumnDef<FamilyStats, unknown>[] = [
  { id: "family", accessorKey: "family", header: "Family", cell: ({ row }) => <div><div className="font-medium">{FAMILY_LABEL[row.original.family]}</div><div className="mt-0.5 font-mono text-xs text-muted-foreground">{row.original.table}</div></div> },
  { id: "implied", accessorKey: "implied", header: "Implied", meta: { align: "right" }, cell: ({ row }) => number(row.original.implied) },
  { id: "minted", accessorKey: "minted", header: "Minted", meta: { align: "right" }, cell: ({ row }) => number(row.original.minted) },
  { id: "coverage", accessorFn: r => (r.implied ? r.minted / r.implied : 0), header: "Coverage", meta: { width: 180 },
    cell: ({ row }) => <div className="flex items-center gap-2"><Meter part={row.original.minted} whole={row.original.implied} /><span className="w-12 text-right text-xs tabular-nums text-muted-foreground">{percent(row.original.minted, row.original.implied)}</span></div> },
  { id: "assertions", accessorKey: "assertions", header: "Assertions", meta: { align: "right" }, cell: ({ row }) => number(row.original.assertions) },
  { id: "lastAssertedAt", accessorKey: "lastAssertedAt", header: "Last asserted", meta: { muted: true }, cell: ({ row }) => <When value={row.original.lastAssertedAt} empty="Never" /> },
];

type Phenotype = Overview["phenotypes"][number];
const phenotypeColumns: ColumnDef<Phenotype, unknown>[] = [
  { id: "name", accessorFn: r => r.name ?? r.id, header: "Trait", cell: ({ row }) => <div><div className="font-medium">{row.original.name ?? row.original.id}</div><div className="mt-0.5 font-mono text-xs text-muted-foreground">{row.original.legacyId ? `${row.original.id} · ${row.original.legacyId}` : row.original.id}</div></div> },
  { id: "geneRows", accessorKey: "geneRows", header: "Gene rows", meta: { align: "right" }, cell: ({ row }) => number(row.original.geneRows) },
  { id: "setRows", accessorKey: "setRows", header: "Gene-set rows", meta: { align: "right" }, cell: ({ row }) => number(row.original.setRows) },
  { id: "mintedGeneClaims", accessorKey: "mintedGeneClaims", header: "Minted gene claims", meta: { align: "right" }, cell: ({ row }) => number(row.original.mintedGeneClaims) },
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
        <Metric title="Implied claims" value={number(d.claims.implied)} hint="Template-backed index rows the loaded data implies" href="/admin/claims" />
        <Metric title="Minted" value={number(d.claims.minted)} hint={`${percent(d.claims.minted - d.claims.deposited, d.claims.implied)} of implied served at least once · ${number(d.claims.deposited)} deposited`} href="/admin/claims?status=minted" />
        <Metric title="Assertions" value={number(d.claims.assertions)} hint={`In 24h: ${plural(d.claims.asserted24h, "claim")} asserted, ${number(d.claims.minted24h)} first minted`} href="/admin/claims?sort=asserted" />
        <Metric title="API requests" value={number(d.api.requests)} hint={`Up ${uptime(d.api.uptimeSeconds)} · ${number(d.api.errors5xx)} server errors · ${number(d.api.graphql.requests)} GraphQL`} href="/admin/activity" />
      </div>
      <div className="grid gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2">
          <Panel title="Claim families" description="Each family is one claim template. Coverage is the share of implied claims that have been served (minted).">
            <DataTable label="Claim families" data={d.families} columns={familyColumns} defaultSort={{ id: "implied", desc: true }}
              getRowId={r => r.family} onRowClick={r => router.push(`/admin/claims?family=${r.family}`)} rowLabel={r => `Open ${FAMILY_LABEL[r.family]} claims`} />
          </Panel>
        </div>
        <Panel title="KPN trait registry" padded>
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
          <DataTable label="Objects by class" data={d.objectsByClass} columns={classColumns} defaultSort={{ id: "count", desc: true }} getRowId={r => r.className} />
        </Panel>
      </div>
      <Panel title="Phenotypes with results" description="Loaded PIGEAN results by KPN trait.">
        <DataTable label="Phenotypes" data={d.phenotypes} columns={phenotypeColumns} defaultSort={{ id: "geneRows", desc: true }} getRowId={r => r.id}
          onRowClick={r => router.push(`/admin/claims?phenotype=${encodeURIComponent(r.id)}`)} rowLabel={r => `Open claims for ${r.name ?? r.id}`} empty="No PIGEAN results are loaded." />
      </Panel>
    </>}
  </>;
}
