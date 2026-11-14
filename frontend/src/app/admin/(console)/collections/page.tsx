"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DataTable } from "@/components/admin/data-table";
import { Coverage, PageHeading, Panel, QueryFeedback, Status, When, number } from "@/components/admin/shared";
import { adminGet, type CollectionSummary } from "@/lib/admin/api";

const columns: ColumnDef<CollectionSummary, unknown>[] = [
  { id: "title", accessorKey: "title", header: "Collection", cell: ({ row }) => <div className="min-w-48">
    <div className="font-medium">{row.original.title}</div>
    <div className="mt-0.5 font-mono text-xs text-muted-foreground">{row.original.key}</div>
  </div> },
  { id: "methods", accessorFn: r => r.methods.join(", "), header: "Methods", cell: ({ row }) => row.original.methods.length
    ? <div className="flex flex-wrap gap-1">{row.original.methods.map(m => <Status key={m}>{m}</Status>)}</div> : <span className="text-muted-foreground">—</span> },
  { id: "version", accessorFn: r => r.currentRelease?.version ?? "", header: "Current release", cell: ({ row }) => row.original.currentRelease ? <div>
    <div className="font-mono text-xs">{row.original.currentRelease.version}</div>
    <div className="mt-0.5 text-xs text-muted-foreground">{number(row.original.releases)} {row.original.releases === 1 ? "release" : "releases"}</div>
  </div> : <span className="text-muted-foreground">None</span> },
  { id: "datasets", accessorKey: "datasets", header: "Datasets", meta: { align: "right" }, cell: ({ row }) => number(row.original.datasets) },
  { id: "mintable", accessorFn: r => r.claims.mintable, header: "Mintable", meta: { align: "right" }, cell: ({ row }) => number(row.original.claims.mintable) },
  { id: "minted", accessorFn: r => r.claims.minted, header: "Minted", meta: { align: "right" }, cell: ({ row }) => number(row.original.claims.minted) },
  { id: "coverage", accessorFn: r => (r.claims.mintable ? r.claims.minted / r.claims.mintable : 0), header: "Served", meta: { width: 160 },
    cell: ({ row }) => <Coverage minted={row.original.claims.minted} mintable={row.original.claims.mintable} /> },
  { id: "publishedAt", accessorFn: r => r.currentRelease?.publishedAt ?? "", header: "Last published", meta: { muted: true },
    cell: ({ row }) => <When value={row.original.currentRelease?.publishedAt} empty="Never" /> },
];

const addButton = <Button asChild size="sm"><Link href="/admin/register"><Plus aria-hidden />Add collection</Link></Button>;

export default function CollectionsPage() {
  const router = useRouter();
  const query = useQuery({ queryKey: ["admin", "collections"], queryFn: () => adminGet<{ collections: CollectionSummary[] }>("collections") });
  const d = query.data;
  return <>
    <PageHeading title="Collections" actions={addButton}
      subtitle="A collection is a method's result files, released together with the templates that mint claims from them and the queries that read them." />
    {!d ? <QueryFeedback pending={query.isPending} error={query.error} onRetry={() => void query.refetch()} /> : d.collections.length === 0
      ? <Panel title="No collections yet" padded>
          <div className="max-w-2xl space-y-3 text-sm leading-6">
            <p>No collection is registered, so the repository has no claims to mint from result files.</p>
            <p className="text-muted-foreground">Registering one reads a collection directory (its <span className="font-mono text-xs">collection.yaml</span>, claim templates and queries) and a release file that lists the datasets and their result files. The console checks both, previews a few claims, then publishes the release.</p>
            <div>{addButton}</div>
          </div>
        </Panel>
      : <Panel title="Registered collections" description="Counts are for each collection's current release. Served is the share of mintable claims that have been minted. Select a collection to see how it is built.">
          <DataTable label="Collections" data={d.collections} columns={columns} defaultSort={{ id: "title", desc: false }} getRowId={r => r.key}
            onRowClick={r => router.push(`/admin/collections/${encodeURIComponent(r.key)}`)} rowLabel={r => `Open collection ${r.title}`} />
        </Panel>}
  </>;
}
