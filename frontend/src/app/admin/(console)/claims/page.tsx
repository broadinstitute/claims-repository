"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { X } from "lucide-react";
import { Input } from "@/components/ui/input";
import { ClaimSheet } from "@/components/admin/claim-sheet";
import { DataTable } from "@/components/admin/data-table";
import { Dimensions, MintBadge, Mono, NoCollection, PageHeading, Pagination, Panel, QueryFeedback, When, number, shortId } from "@/components/admin/shared";
import { useUrlState } from "@/components/admin/use-url-state";
import { adminGet, type ClaimList, type ClaimRow, type CollectionDetail, type CollectionSummary } from "@/lib/admin/api";

const selectClass = "h-9 max-w-full rounded-md border border-input bg-background px-3 text-sm disabled:cursor-not-allowed disabled:opacity-50";
const PAGE_SIZE = 25;

const columns: ColumnDef<ClaimRow, unknown>[] = [
  { id: "claimId", header: "Claim ID", enableSorting: false, cell: ({ row }) => <span className="whitespace-nowrap font-mono text-xs" title={row.original.claimId}>{shortId(row.original.claimId)}</span> },
  { id: "collection", header: "Collection", enableSorting: false, cell: ({ row }) => row.original.collection ? <Mono>{row.original.collection}</Mono> : <NoCollection /> },
  { id: "claimKey", header: "Claim key", enableSorting: false, cell: ({ row }) => row.original.claimKey ? <div>
    <Mono>{row.original.claimKey}</Mono>
    {row.original.kind && <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">{row.original.kind}</div>}
  </div> : <span className="text-muted-foreground">—</span> },
  { id: "subject", header: "Subject", enableSorting: false, cell: ({ row }) => row.original.subject ? <span className="font-medium">{row.original.subject}</span> : <span className="text-muted-foreground">—</span> },
  { id: "dataset", header: "Dataset", enableSorting: false, cell: ({ row }) => <Dimensions value={row.original.dimensions} className="min-w-40" /> },
  { id: "status", header: "Status", enableSorting: false, cell: ({ row }) => <MintBadge minted={row.original.minted} /> },
  { id: "timesAsserted", header: "Assertions", enableSorting: false, meta: { align: "right" }, cell: ({ row }) => number(row.original.timesAsserted) },
  { id: "lastAssertedAt", header: "Last asserted", enableSorting: false, meta: { muted: true }, cell: ({ row }) => <When value={row.original.lastAssertedAt} empty="Never" /> },
];

function Claims() {
  const { params, update } = useUrlState();
  const q = params.get("q") ?? "";
  const collection = params.get("collection") ?? "";
  const claimKey = params.get("claim_key") ?? "";
  const dataset = params.get("dataset") ?? "";
  const origin = params.get("origin") ?? "";
  const status = params.get("status") ?? "";
  const sort = params.get("sort") ?? "";
  const page = Math.max(1, Number(params.get("page")) || 1);
  const claim = params.get("claim");
  const [search, setSearch] = useState(q);
  useEffect(() => { setSearch(cur => (cur.trim() === q ? cur : q)); }, [q]);
  useEffect(() => {
    const id = setTimeout(() => { if (search.trim() !== q) update({ q: search.trim(), page: undefined }, { push: false }); }, 300);
    return () => clearTimeout(id);
  }, [search, q, update]);

  const collections = useQuery({ queryKey: ["admin", "collections"], queryFn: () => adminGet<{ collections: CollectionSummary[] }>("collections") });
  const detail = useQuery({
    queryKey: ["admin", "collection", collection, ""], enabled: !!collection,
    queryFn: () => adminGet<CollectionDetail>(`collections/${encodeURIComponent(collection)}`),
  });
  const claimKeys = useMemo(() => Array.from(new Set((detail.data?.templates ?? []).flatMap(t => t.claims.map(c => c.key)))), [detail.data]);
  const datasetLabel = useMemo(() => detail.data?.datasets.find(ds => ds.id === dataset)?.dimensions, [detail.data, dataset]);

  const filters = useMemo(() => ({ q, collection, claim_key: claimKey, dataset, origin, status, sort: sort || "recent", page, page_size: PAGE_SIZE }),
    [q, collection, claimKey, dataset, origin, status, sort, page]);
  const query = useQuery({ queryKey: ["admin", "claims", filters], queryFn: () => adminGet<ClaimList>("claims", filters), placeholderData: keepPreviousData });

  const select = (key: string, value: string, label: string, options: [string, string][], extra: Record<string, undefined> = {}, disabled = false) => (
    <select aria-label={label} className={selectClass} value={value} disabled={disabled} onChange={e => update({ [key]: e.target.value, page: undefined, ...extra })}>
      {options.map(([v, t]) => <option key={v} value={v}>{t}</option>)}
    </select>
  );
  const collectionOptions: [string, string][] = [["", collections.isError ? "All collections (list unavailable)" : "All collections"],
    ...(collections.data?.collections ?? []).map(c => [c.key, c.title === c.key ? c.key : `${c.title} (${c.key})`] as [string, string])];
  if (collection && !collectionOptions.some(([v]) => v === collection)) collectionOptions.push([collection, collection]);
  const keyOptions: [string, string][] = [["", !collection ? "Choose a collection first" : detail.isPending ? "Loading claim keys…" : "All claim keys"], ...claimKeys.map(k => [k, k] as [string, string])];
  if (claimKey && !claimKeys.includes(claimKey)) keyOptions.push([claimKey, claimKey]);

  return <>
    <PageHeading title="Claims" subtitle="Every claim the registered collections can mint, plus every stored claim that no collection produces. Opening one re-renders it from its template without asserting or minting it." />
    <div className="flex flex-wrap items-center gap-2">
      <Input type="search" aria-label="Search claims" placeholder="Search subject or claim ID…" className="w-72 max-w-full" value={search} onChange={e => setSearch(e.target.value)} />
      {select("collection", collection, "Collection", collectionOptions, { claim_key: undefined, dataset: undefined, ...(origin === "uncollected" ? { origin: undefined } : {}) })}
      {select("claim_key", claimKey, "Claim key", keyOptions, {}, !collection)}
      <select aria-label="Origin" className={selectClass} value={origin} onChange={e => update({
        origin: e.target.value, page: undefined,
        ...(e.target.value === "uncollected" ? { collection: undefined, claim_key: undefined, dataset: undefined } : {}),
      })}>
        <option value="">Any origin</option><option value="collection">From a collection</option><option value="uncollected">No collection</option>
      </select>
      {select("status", status, "Status", [["", "Minted and not minted"], ["minted", "Minted"], ["unminted", "Not minted yet"]])}
      {select("sort", sort, "Sort", [["", "Recently asserted"], ["asserted", "Most asserted"], ["minted", "Recently minted"], ["subject", "Subject A–Z"]])}
    </div>
    {(dataset || (origin === "uncollected" && (collection || claimKey))) && <div className="flex flex-wrap items-center gap-2 text-xs">
      {dataset && <span className="inline-flex items-center gap-2 rounded-md border border-border/50 px-2 py-1">
        <span className="text-muted-foreground">Dataset</span>{datasetLabel ? <Dimensions value={datasetLabel} /> : <Mono>{shortId(dataset)}</Mono>}
        <button type="button" aria-label="Clear the dataset filter" className="text-muted-foreground hover:text-foreground" onClick={() => update({ dataset: undefined, page: undefined })}><X className="size-3.5" aria-hidden /></button>
      </span>}
      {origin === "uncollected" && (collection || claimKey) && <span className="text-muted-foreground">Claims with no collection never match a collection or claim key filter.</span>}
    </div>}
    <Panel title="Claims">
      {!query.data ? <div className="px-4"><QueryFeedback pending={query.isPending} error={query.error} onRetry={() => void query.refetch()} /></div> : <>
        <DataTable label="Claims" data={query.data.rows} columns={columns} getRowId={r => r.claimId} selectedId={claim}
          onRowClick={r => update({ claim: r.claimId })} rowLabel={r => `Inspect claim ${r.subject ?? r.claimId}`} empty="No claims match these filters." />
        <Pagination page={query.data.page} total={query.data.total} pageSize={query.data.pageSize} fetching={query.isFetching} onPage={n => update({ page: n > 1 ? n : undefined })} />
      </>}
    </Panel>
    <ClaimSheet claimId={claim} onClose={() => update({ claim: undefined })} />
  </>;
}

export default function ClaimsPage() {
  return <Suspense fallback={<QueryFeedback pending error={null} />}><Claims /></Suspense>;
}
