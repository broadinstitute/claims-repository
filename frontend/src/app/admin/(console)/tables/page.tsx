"use client";

import { Suspense, useEffect, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { DataTable } from "@/components/admin/data-table";
import { Json, PageHeading, Pagination, Panel, QueryFeedback, Status, When, bytes, clock, ms, number } from "@/components/admin/shared";
import { useUrlState } from "@/components/admin/use-url-state";
import { adminGet, type Cell, type TableInfo, type TableRows, type Tables } from "@/lib/admin/api";

const selectClass = "h-9 max-w-full rounded-md border border-input bg-background px-3 text-sm";

const tableColumns: ColumnDef<TableInfo, unknown>[] = [
  { id: "name", accessorKey: "name", header: "Table", cell: ({ row }) => <div>
    <div className="font-mono text-xs font-medium">{row.original.name}</div>
    {row.original.available
      ? <div className="mt-0.5 text-xs text-muted-foreground">{row.original.columns} columns · key {row.original.primaryKey?.join(", ") || "—"}</div>
      : <div className="mt-1"><Status tone="bad">missing</Status></div>}
  </div> },
  { id: "rows", accessorFn: r => r.rows ?? -1, header: "Rows", meta: { align: "right" }, cell: ({ row }) => number(row.original.rows) },
  { id: "dataBytes", accessorFn: r => r.dataBytes ?? -1, header: "Data", meta: { align: "right" }, cell: ({ row }) => bytes(row.original.dataBytes) },
  { id: "indexBytes", accessorFn: r => r.indexBytes ?? -1, header: "Indexes", meta: { align: "right" }, cell: ({ row }) => bytes(row.original.indexBytes) },
  { id: "countMs", accessorFn: r => r.countMs ?? -1, header: "Count time", meta: { align: "right", muted: true }, cell: ({ row }) => ms(row.original.countMs) },
];

const cellText = (value: Cell) => (value !== null && typeof value === "object" ? value.text : value === null ? null : String(value));

function CellPreview({ value }: { value: Cell }) {
  const text = cellText(value);
  if (text === null) return <span className="text-muted-foreground">—</span>;
  return <span className="inline-block max-w-64 truncate align-top font-mono text-xs" title={text.slice(0, 300)}>{text}</span>;
}

function FullValue({ value }: { value: Cell }) {
  const text = cellText(value);
  if (text === null) return <span className="text-muted-foreground">NULL</span>;
  const note = value !== null && typeof value === "object" ? <p className="mt-1 text-xs text-muted-foreground">First 2,000 of {number(value.truncated)} characters.</p> : null;
  if (/^[[{]/.test(text.trim())) { try { return <><Json value={JSON.parse(text)} />{note}</>; } catch { /* not complete JSON */ } }
  return <><span className="break-all font-mono text-xs">{text}</span>{note}</>;
}

function Browser({ table }: { table: string }) {
  const { params, update } = useUrlState();
  const column = params.get("col") ?? "";
  const op = params.get("op") ?? "contains";
  const value = params.get("val") ?? "";
  const page = Math.max(1, Number(params.get("page")) || 1);
  const [draft, setDraft] = useState({ column, op, value });
  const [row, setRow] = useState<Record<string, Cell> | null>(null);
  useEffect(() => { setDraft({ column, op, value }); }, [column, op, value]);
  const query = useQuery({
    queryKey: ["admin", "table-rows", table, column, op, value, page],
    queryFn: () => adminGet<TableRows>(`tables/${table}/rows`, { page, page_size: 25, column, op, value }),
    placeholderData: keepPreviousData,
  });
  const d = query.data?.table === table ? query.data : undefined;
  const columns: ColumnDef<Record<string, Cell>, unknown>[] = (d?.columns ?? []).map(c => ({
    id: c.name, header: c.name, enableSorting: false, cell: ({ row: r }) => <CellPreview value={r.original[c.name]} />,
  }));
  return (
    <Panel title={`Rows · ${table}`} description={d ? `Read-only, in primary-key order (${d.orderBy.join(", ")}).` : undefined}
      action={<Button variant="ghost" size="sm" onClick={() => update({ table: undefined, col: undefined, op: undefined, val: undefined, page: undefined })}>Close</Button>}>
      <form className="flex flex-wrap items-center gap-2 border-b border-border/50 px-4 py-3" onSubmit={e => { e.preventDefault(); update({ col: draft.column, op: draft.op === "contains" ? undefined : draft.op, val: draft.value, page: undefined }); }}>
        <select aria-label="Filter column" className={selectClass} value={draft.column || d?.columns[0]?.name || ""} onChange={e => setDraft({ ...draft, column: e.target.value })}>
          {(d?.columns ?? []).map(c => <option key={c.name} value={c.name}>{c.name}</option>)}
        </select>
        <select aria-label="Match" className={selectClass} value={draft.op} onChange={e => setDraft({ ...draft, op: e.target.value })}>
          <option value="contains">Contains</option><option value="equals">Equals</option>
        </select>
        <Input aria-label="Filter value" placeholder="Value" className="w-64 max-w-full" value={draft.value} onChange={e => setDraft({ ...draft, value: e.target.value })} />
        <Button type="submit" variant="outline" size="sm" onClick={() => { if (!draft.column && d?.columns[0]) setDraft({ ...draft, column: d.columns[0].name }); }}>Apply</Button>
      </form>
      {!d ? <div className="px-4"><QueryFeedback pending={query.isPending || query.isFetching} error={query.error} onRetry={() => void query.refetch()} /></div> : <>
        <DataTable label={`${table} rows`} data={d.rows} columns={columns} onRowClick={setRow} rowLabel={() => "Inspect row"} empty="No rows match." />
        <Pagination page={d.page} total={d.total} pageSize={d.pageSize} fetching={query.isFetching} onPage={n => update({ page: n > 1 ? n : undefined })} />
      </>}
      <Sheet open={!!row} onOpenChange={open => { if (!open) setRow(null); }}>
        <SheetContent side="right" className="w-full gap-0 overflow-y-auto sm:max-w-2xl">
          <SheetHeader className="border-b border-border/50"><SheetTitle>{table} row</SheetTitle><SheetDescription>Stored values, read-only.</SheetDescription></SheetHeader>
          {row && <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-2 p-4 text-sm">
            {Object.entries(row).map(([k, v]) => [<dt key={`${k}-k`} className="text-muted-foreground">{k}</dt>, <dd key={`${k}-v`} className="min-w-0"><FullValue value={v} /></dd>])}
          </dl>}
        </SheetContent>
      </Sheet>
    </Panel>
  );
}

function TablesView() {
  const { params, update } = useUrlState();
  const selected = params.get("table");
  const [fresh, setFresh] = useState(false);
  const query = useQuery({ queryKey: ["admin", "tables", fresh], queryFn: () => adminGet<Tables>("tables", fresh ? { fresh: "true" } : undefined) });
  const d = query.data;
  return <>
    <PageHeading title="Tables" subtitle={d ? `${d.backend === "mysql" ? "Aurora MySQL" : "SQLite"} · exact row counts as of ${clock(d.asOf)}` : undefined}
      actions={<Button variant="outline" size="sm" disabled={query.isFetching} onClick={() => { if (fresh) void query.refetch(); else setFresh(true); }}>Recount</Button>} />
    {!d ? <QueryFeedback pending={query.isPending} error={query.error} onRetry={() => void query.refetch()} /> : <>
      {d.pendingMigrations.length > 0 && <p role="status" className="rounded-lg border border-amber-500/25 bg-amber-500/5 px-4 py-3 text-sm text-amber-800 dark:text-amber-300">Migrations not applied: {d.pendingMigrations.join(", ")}</p>}
      <Panel title="Database tables" description="Read-only. Select a table to browse its rows.">
        <DataTable label="Tables" data={d.tables} columns={tableColumns} defaultSort={{ id: "rows", desc: true }} getRowId={r => r.name} selectedId={selected}
          onRowClick={r => { if (r.available) update({ table: r.name, col: undefined, op: undefined, val: undefined, page: undefined }); }} rowLabel={r => `Browse ${r.name}`} />
      </Panel>
      {selected && <Browser key={selected} table={selected} />}
      {d.migrations.length > 0 && <Panel title="Migrations">
        <DataTable label="Migrations" data={d.migrations} getRowId={r => r.version} columns={[
          { id: "version", accessorKey: "version", header: "Version", cell: ({ row }) => <span className="font-mono text-xs">{row.original.version}</span> },
          { id: "appliedAt", accessorKey: "appliedAt", header: "Applied", meta: { muted: true }, cell: ({ row }) => <When value={row.original.appliedAt} /> },
        ]} />
      </Panel>}
    </>}
  </>;
}

export default function TablesPage() {
  return <Suspense fallback={<QueryFeedback pending error={null} />}><TablesView /></Suspense>;
}
