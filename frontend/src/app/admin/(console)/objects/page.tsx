"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { X } from "lucide-react";
import { Input } from "@/components/ui/input";
import { DataTable } from "@/components/admin/data-table";
import { ObjectSheet } from "@/components/admin/object-sheet";
import { Mono, PageHeading, Pagination, Panel, QueryFeedback, When, number, plural, shortAgo, shortId } from "@/components/admin/shared";
import { useUrlState } from "@/components/admin/use-url-state";
import { adminGet, type DepositList, type ObjectClasses, type ObjectList, type ObjectRow } from "@/lib/admin/api";
import { cn } from "@/lib/utils";

const selectClass = "h-9 max-w-full rounded-md border border-input bg-background px-3 text-sm";
const PAGE_SIZE = 25;

const columns: ColumnDef<ObjectRow, unknown>[] = [
  { id: "className", header: "Class", enableSorting: false, cell: ({ row }) => <span className="whitespace-nowrap font-mono text-xs">{row.original.className}</span> },
  { id: "label", header: "Label", enableSorting: false, cell: ({ row }) => row.original.label
    ? <span className="line-clamp-2 max-w-xl break-words">{row.original.label}</span> : <span className="text-muted-foreground">—</span> },
  { id: "id", header: "ID", enableSorting: false, cell: ({ row }) => <span className="whitespace-nowrap font-mono text-xs" title={row.original.id}>{shortId(row.original.id)}</span> },
  { id: "slots", header: "Slots", enableSorting: false, meta: { align: "right" }, cell: ({ row }) => <span title={row.original.slots.join(", ")}>{number(row.original.slots.length)}</span> },
  { id: "timesSeen", header: "Seen", enableSorting: false, meta: { align: "right" }, cell: ({ row }) => number(row.original.timesSeen) },
  { id: "createdAt", header: "First stored", enableSorting: false, meta: { muted: true }, cell: ({ row }) => <When value={row.original.createdAt} /> },
];

function Objects() {
  const { params, update } = useUrlState();
  const q = params.get("q") ?? "";
  const className = params.get("class") ?? "";
  const deposit = params.get("deposit") ?? "";
  const references = params.get("references") ?? "";
  const page = Math.max(1, Number(params.get("page")) || 1);
  const object = params.get("object");
  const [search, setSearch] = useState(q);
  useEffect(() => { setSearch(cur => (cur.trim() === q ? cur : q)); }, [q]);
  useEffect(() => {
    const id = setTimeout(() => { if (search.trim() !== q) update({ q: search.trim(), page: undefined }, { push: false }); }, 300);
    return () => clearTimeout(id);
  }, [search, q, update]);

  const classes = useQuery({ queryKey: ["admin", "object-classes"], queryFn: () => adminGet<ObjectClasses>("objects/classes") });
  const deposits = useQuery({ queryKey: ["admin", "deposits", 1], queryFn: () => adminGet<DepositList>("deposits", { page_size: 50 }) });
  const filters = useMemo(() => ({ class_name: className, q, deposit, references, page, page_size: PAGE_SIZE }), [className, q, deposit, references, page]);
  const query = useQuery({ queryKey: ["admin", "objects", filters], queryFn: () => adminGet<ObjectList>("objects", filters), placeholderData: keepPreviousData });

  const chip = (value: string, label: string, count: number) => (
    <button key={value || "all"} type="button" aria-pressed={className === value} onClick={() => update({ class: value, page: undefined })}
      className={cn("rounded-md border border-border/50 px-2.5 py-1 text-xs transition-colors hover:bg-muted/40",
        className === value && "border-foreground/30 bg-muted font-medium text-foreground")}>
      {label} <span className="tabular-nums text-muted-foreground">{number(count)}</span>
    </button>
  );
  const depositOptions = deposits.data?.rows ?? [];

  return <>
    <PageHeading title="Objects" subtitle="Every object the repository stores, of any class, exactly as stored. Opening one shows each slot of its document, what it references, what references it, its edges and the deposits that carried it. Nothing here mints or asserts." />
    {classes.data ? <div className="space-y-2">
      <div className="flex flex-wrap gap-1.5" role="group" aria-label="Class">
        {chip("", "All", classes.data.total)}
        {classes.data.classes.map(c => chip(c.className, c.className, c.count))}
      </div>
      <p className="text-xs text-muted-foreground">
        {plural(classes.data.deposits, "deposit")}.{" "}
        {classes.data.edgeClasses.length
          ? <>Deposited edges are not objects and are shown on the objects they join: {classes.data.edgeClasses.map(e => `${number(e.count)} ${e.className}`).join(", ")}.</>
          : "No deposited edges."}
      </p>
    </div> : <QueryFeedback pending={classes.isPending} error={classes.error} onRetry={() => void classes.refetch()} />}
    <div className="flex flex-wrap items-center gap-2">
      <Input type="search" aria-label="Search objects" placeholder="Search statement, name or description, or paste an ID…" className="w-96 max-w-full" value={search} onChange={e => setSearch(e.target.value)} />
      <select aria-label="Deposit" className={selectClass} value={deposit} onChange={e => update({ deposit: e.target.value, page: undefined })}>
        <option value="">Any origin (deposited, minted or loaded)</option>
        {depositOptions.map(d => <option key={d.depositId} value={d.depositId}>
          {d.depositor.name} · {d.receivedAt ? shortAgo(d.receivedAt) : "unknown time"} · {plural(d.objectCount, "object")}
        </option>)}
        {deposit && !depositOptions.some(d => d.depositId === deposit) && <option value={deposit}>{deposit}</option>}
      </select>
    </div>
    {(references || deposit) && <div className="flex flex-wrap items-center gap-2 text-xs">
      {references && <span className="inline-flex items-center gap-2 rounded-md border border-border/50 px-2 py-1">
        <span className="text-muted-foreground">References</span><Mono>{shortId(references)}</Mono>
        <button type="button" aria-label="Clear the references filter" className="text-muted-foreground hover:text-foreground" onClick={() => update({ references: undefined, page: undefined })}><X className="size-3.5" aria-hidden /></button>
      </span>}
      {deposit && <span className="inline-flex items-center gap-2 rounded-md border border-border/50 px-2 py-1">
        <span className="text-muted-foreground">Deposit</span><Mono>{deposit}</Mono>
        <button type="button" aria-label="Clear the deposit filter" className="text-muted-foreground hover:text-foreground" onClick={() => update({ deposit: undefined, page: undefined })}><X className="size-3.5" aria-hidden /></button>
      </span>}
    </div>}
    <Panel title={className || "All objects"} description="Ordered by ID. Text search reads the statement, name and description slots; a pasted ID is looked up directly.">
      {!query.data ? <div className="px-4"><QueryFeedback pending={query.isPending} error={query.error} onRetry={() => void query.refetch()} /></div> : <>
        <DataTable label="Objects" data={query.data.rows} columns={columns} getRowId={r => r.id} selectedId={object}
          onRowClick={r => update({ object: r.id })} rowLabel={r => `Inspect ${r.className} ${r.label ?? r.id}`} empty="No objects match these filters." />
        <Pagination page={query.data.page} total={query.data.total} pageSize={query.data.pageSize} fetching={query.isFetching} onPage={n => update({ page: n > 1 ? n : undefined })} />
      </>}
    </Panel>
    <ObjectSheet objectId={object} onOpen={id => update({ object: id })} onClose={() => update({ object: undefined })} />
  </>;
}

export default function ObjectsPage() {
  return <Suspense fallback={<QueryFeedback pending error={null} />}><Objects /></Suspense>;
}
