"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { DataTable } from "@/components/admin/data-table";
import { CopyButton, FAMILY_LABEL, Json, KeyValues, PageHeading, Panel, QueryFeedback, Status, number } from "@/components/admin/shared";
import { adminGet, type Templates } from "@/lib/admin/api";

type Method = Templates["methods"][number];
type FamilyRow = Method["families"][number];
type MetricRow = Method["metrics"][number];

const familyColumns: ColumnDef<FamilyRow, unknown>[] = [
  { id: "family", accessorKey: "family", header: "Family", cell: ({ row }) => <div><div className="font-medium">{FAMILY_LABEL[row.original.family]}</div><div className="mt-0.5 font-mono text-xs text-muted-foreground">{row.original.relation} · {row.original.table}</div></div> },
  { id: "metrics", header: "Scores", enableSorting: false, cell: ({ row }) => row.original.metrics.length ? <div className="flex flex-wrap gap-1">{row.original.metrics.map(m => <Status key={m}>{m}</Status>)}</div> : <span className="text-muted-foreground">None</span> },
  { id: "implied", accessorKey: "implied", header: "Implied", meta: { align: "right" }, cell: ({ row }) => number(row.original.implied) },
  { id: "minted", accessorKey: "minted", header: "Minted", meta: { align: "right" }, cell: ({ row }) => number(row.original.minted) },
  { id: "assertions", accessorKey: "assertions", header: "Assertions", meta: { align: "right" }, cell: ({ row }) => number(row.original.assertions) },
  { id: "example", header: "Latest minted statement", enableSorting: false, meta: { width: 360 }, cell: ({ row }) => row.original.example
    ? <Link href={`/admin/claims?claim=${encodeURIComponent(row.original.example.claimId)}`} className="text-xs underline-offset-4 hover:underline">{row.original.example.statement}</Link>
    : <span className="text-xs text-muted-foreground">None minted yet</span> },
];

const metricColumns: ColumnDef<MetricRow, unknown>[] = [
  { id: "metric", accessorKey: "metric", header: "Metric", cell: ({ row }) => <div><div className="font-mono text-xs font-medium">{row.original.metric}</div><div className="mt-0.5 text-xs text-muted-foreground">{row.original.families.map(f => FAMILY_LABEL[f as keyof typeof FAMILY_LABEL] ?? f).join(", ")}</div></div> },
  { id: "scoreKind", accessorKey: "scoreKind", header: "Kind", cell: ({ row }) => <span className="inline-flex flex-wrap items-center gap-1.5"><span className="font-mono text-xs">{row.original.scoreKind}</span>{!row.original.kindAgrees && <Status tone="bad">template disagrees</Status>}</span> },
  { id: "scale", accessorKey: "scale", header: "Scale" },
  { id: "better", accessorKey: "better", header: "Better" },
  { id: "confirmed", accessorFn: r => (r.confirmed ? 1 : 0), header: "Status", cell: ({ row }) => row.original.confirmed ? <Status tone="good">confirmed</Status> : <Status tone="warn">provisional</Status> },
  { id: "description", accessorKey: "description", header: "Meaning", enableSorting: false, meta: { width: 320 }, cell: ({ row }) => <span className="text-xs text-muted-foreground">{row.original.description}</span> },
];

const mono = (text: string) => <span className="font-mono text-xs">{text}</span>;

export default function TemplatesPage() {
  const query = useQuery({ queryKey: ["admin", "templates"], queryFn: () => adminGet<Templates>("templates") });
  const d = query.data;
  return <>
    <PageHeading title="Templates" subtitle="Claim templates, their metric semantics, and the persisted query templates clients run." />
    {!d ? <QueryFeedback pending={query.isPending} error={query.error} onRetry={() => void query.refetch()} /> : <>
      {d.methods.map(m => <div key={m.name} className="space-y-6">
        <Panel title={`${m.name.toUpperCase()} claim template`} padded
          description="Claims are synthesized from index rows by this template on every serve and verified against their precomputed content address.">
          <KeyValues rows={[
            ["Template source", <span key="t" className="inline-flex flex-wrap gap-2">{mono(m.template.path)}<span className="text-xs text-muted-foreground">{number(m.template.lines)} lines</span></span>],
            ["Template sha256", <span key="s" className="inline-flex flex-wrap gap-3">{mono(m.template.sha256.slice(0, 16))}<CopyButton text={m.template.sha256} /></span>],
            ["GraphQL module", <span key="g" className="inline-flex flex-wrap gap-2">{mono(m.graphqlModule.path)}<span className="font-mono text-xs text-muted-foreground">{m.graphqlModule.sha256.slice(0, 12)}</span></span>],
            ["Identity profile", mono(m.identityProfile)],
            ["DAPPER profile", mono(m.dapperProfile)],
            ["Contract", m.contractVersion],
          ]} />
        </Panel>
        <Panel title="Claim families" description="One row per claim shape the template produces.">
          <DataTable label="Claim families" data={m.families} columns={familyColumns} getRowId={r => r.family} />
        </Panel>
        <Panel title="Metric semantics" description="Unconfirmed interpretations render as provisional until the method leads sign off.">
          <DataTable label="Metrics" data={m.metrics} columns={metricColumns} getRowId={r => r.metric} />
        </Panel>
      </div>)}
      <Panel title="Query templates" description="Persisted GraphQL documents with their default variables." padded>
        {d.queryTemplates.length ? <div className="divide-y divide-border/50">
          {d.queryTemplates.map(q => <div key={q.name} className="py-3 first:pt-0 last:pb-0">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div><div className="font-mono text-sm font-medium">{q.name}</div><div className="mt-0.5 text-xs text-muted-foreground">{q.path} · {number(q.lines)} lines · sha256 {q.sha256.slice(0, 12)}</div></div>
              <div className="flex gap-1.5">{q.hasFixture && <Status>fixture</Status>}{q.variables && <Status>{Object.keys(q.variables).length} variables</Status>}</div>
            </div>
            <details className="mt-2"><summary className="cursor-pointer text-xs text-muted-foreground">Document and variables</summary>
              <div className="mt-2 space-y-2"><pre className="max-h-96 overflow-auto rounded-md border border-border/50 bg-muted/30 p-3 font-mono text-[11.5px] leading-5">{q.document}</pre>{q.variables && <Json value={q.variables} />}</div>
            </details>
          </div>)}
        </div> : <p className="text-sm text-muted-foreground">No persisted queries.</p>}
      </Panel>
      <Panel title="Core schema" padded>
        <KeyValues rows={[["Path", mono(d.coreSchema.path)], ["sha256", mono(d.coreSchema.sha256.slice(0, 16))], ["Lines", number(d.coreSchema.lines)]]} />
      </Panel>
    </>}
  </>;
}
