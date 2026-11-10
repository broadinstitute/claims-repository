"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Input } from "@/components/ui/input";
import { ClaimSheet } from "@/components/admin/claim-sheet";
import { DataTable } from "@/components/admin/data-table";
import { FamilyBadge, MintBadge, PageHeading, Pagination, Panel, QueryFeedback, When, number, shortId } from "@/components/admin/shared";
import { useUrlState } from "@/components/admin/use-url-state";
import { adminGet, type ClaimList, type ClaimRow } from "@/lib/admin/api";

const selectClass = "h-9 max-w-full rounded-md border border-input bg-background px-3 text-sm";
const PAGE_SIZE = 25;

const columns: ColumnDef<ClaimRow, unknown>[] = [
  { id: "subject", header: "Claim", enableSorting: false, cell: ({ row }) => <div>
    <div className="flex flex-wrap items-center gap-2"><span className="font-medium">{row.original.subject ?? "Deposited claim"}</span><FamilyBadge family={row.original.family} /></div>
    <div className="mt-0.5 font-mono text-xs text-muted-foreground" title={row.original.claimId}>{shortId(row.original.claimId)}</div>
  </div> },
  { id: "phenotype", header: "Phenotype", enableSorting: false, cell: ({ row }) => row.original.phenotype ? <span className="font-mono text-xs">{row.original.phenotype}</span> : <span className="text-muted-foreground">—</span> },
  { id: "status", header: "Status", enableSorting: false, cell: ({ row }) => <MintBadge minted={row.original.minted} /> },
  { id: "timesAsserted", header: "Assertions", enableSorting: false, meta: { align: "right" }, cell: ({ row }) => number(row.original.timesAsserted) },
  { id: "lastAssertedAt", header: "Last asserted", enableSorting: false, meta: { muted: true }, cell: ({ row }) => <When value={row.original.lastAssertedAt} empty="Never" /> },
  { id: "mintedAt", header: "Minted", enableSorting: false, meta: { muted: true }, cell: ({ row }) => <When value={row.original.mintedAt} empty="Not yet" /> },
];

function Claims() {
  const { params, update } = useUrlState();
  const q = params.get("q") ?? "";
  const family = params.get("family") ?? "";
  const status = params.get("status") ?? "";
  const phenotype = params.get("phenotype") ?? "";
  const sort = params.get("sort") ?? "";
  const page = Math.max(1, Number(params.get("page")) || 1);
  const claim = params.get("claim");
  const [search, setSearch] = useState(q);
  const [pheno, setPheno] = useState(phenotype);
  useEffect(() => { setSearch(q); }, [q]);
  useEffect(() => { setPheno(phenotype); }, [phenotype]);
  useEffect(() => {
    const id = setTimeout(() => { if (search !== q || pheno !== phenotype) update({ q: search.trim(), phenotype: pheno.trim(), page: undefined }, { push: false }); }, 300);
    return () => clearTimeout(id);
  }, [search, pheno, q, phenotype, update]);
  const filters = useMemo(() => ({ q, family, status, phenotype, sort: sort || "recent", page, page_size: PAGE_SIZE }), [q, family, status, phenotype, sort, page]);
  const query = useQuery({ queryKey: ["admin", "claims", filters], queryFn: () => adminGet<ClaimList>("claims", filters), placeholderData: keepPreviousData });
  const select = (key: string, value: string, label: string, options: [string, string][]) => (
    <select aria-label={label} className={selectClass} value={value} onChange={e => update({ [key]: e.target.value, page: undefined })}>
      {options.map(([v, t]) => <option key={v} value={v}>{t}</option>)}
    </select>
  );
  return <>
    <PageHeading title="Claims" subtitle="Every claim the loaded data implies plus every deposited claim. Opening one re-verifies it without asserting it." />
    <div className="flex flex-wrap items-center gap-2">
      <Input type="search" aria-label="Search claims" placeholder="Search subject or claim ID…" className="w-72 max-w-full" value={search} onChange={e => setSearch(e.target.value)} />
      {select("family", family, "Family", [["", "All families"], ["gene", "Gene"], ["gene_set", "Gene set"], ["roster", "Roster"], ["deposited", "Deposited"]])}
      {select("status", status, "Status", [["", "Minted and implied"], ["minted", "Minted"], ["unminted", "Implied, not minted"]])}
      <Input aria-label="Phenotype" placeholder="Phenotype (any key)" className="w-48 max-w-full" value={pheno} onChange={e => setPheno(e.target.value)} />
      {select("sort", sort, "Sort", [["", "Recently asserted"], ["asserted", "Most asserted"], ["minted", "Recently minted"], ["subject", "Subject A–Z"]])}
    </div>
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
