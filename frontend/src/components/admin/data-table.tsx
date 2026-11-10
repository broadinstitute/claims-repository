"use client";

import { useState } from "react";
import { flexRender, getCoreRowModel, getSortedRowModel, useReactTable, type ColumnDef, type SortingState } from "@tanstack/react-table";
import { ArrowDown, ArrowUp } from "lucide-react";
import { cn } from "@/lib/utils";
import { Empty } from "./shared";

declare module "@tanstack/react-table" {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData, TValue> { align?: "right"; muted?: boolean; width?: number }
}

/** Akleao Admin V2 table look: hairline rows, muted headers, optional client sort for bounded snapshots. */
export function DataTable<T>({ data, columns, defaultSort, onRowClick, getRowId, rowLabel, empty = "Nothing in this view.", selectedId, label }: {
  data: T[]; columns: ColumnDef<T, unknown>[]; defaultSort?: SortingState[number]; onRowClick?: (row: T) => void;
  getRowId?: (row: T) => string; rowLabel?: (row: T) => string; empty?: string; selectedId?: string | null; label: string;
}) {
  const [sorting, setSorting] = useState<SortingState>(defaultSort ? [defaultSort] : []);
  const table = useReactTable({
    data, columns, state: { sorting }, onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(), getSortedRowModel: getSortedRowModel(),
    getRowId: getRowId ? row => getRowId(row) : undefined,
  });
  if (!data.length) return <Empty>{empty}</Empty>;
  return (
    <div className="overflow-x-auto">
      <table aria-label={label} className="w-full text-left text-sm [&_td]:px-4 [&_td]:py-3 [&_td]:align-top [&_th]:px-4 [&_th]:py-3 [&_th]:font-medium [&_th]:text-muted-foreground [&_tr]:border-b [&_tr]:border-border/50 [&_tbody_tr:last-child]:border-0">
        <thead>
          {table.getHeaderGroups().map(group => (
            <tr key={group.id}>
              {group.headers.map(header => {
                const meta = header.column.columnDef.meta;
                const sorted = header.column.getIsSorted();
                const content = flexRender(header.column.columnDef.header, header.getContext());
                return (
                  <th key={header.id} scope="col" style={meta?.width ? { width: meta.width } : undefined}
                    className={cn("whitespace-nowrap text-xs", meta?.align === "right" && "text-right")}
                    aria-sort={sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : undefined}>
                    {header.column.getCanSort() ? (
                      <button type="button" onClick={header.column.getToggleSortingHandler()}
                        className={cn("inline-flex items-center gap-1 rounded-sm hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring", meta?.align === "right" && "flex-row-reverse")}>
                        {content}
                        {sorted === "asc" ? <ArrowUp className="size-3" aria-hidden /> : sorted === "desc" ? <ArrowDown className="size-3" aria-hidden /> : null}
                      </button>
                    ) : content}
                  </th>
                );
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map(row => {
            const original = row.original;
            const clickable = !!onRowClick;
            return (
              <tr key={row.id}
                className={cn(clickable && "cursor-pointer hover:bg-muted/40 focus-visible:bg-muted/40 focus-visible:outline-none", selectedId && row.id === selectedId && "bg-muted/50")}
                tabIndex={clickable ? 0 : undefined} aria-label={clickable && rowLabel ? rowLabel(original) : undefined}
                aria-selected={selectedId ? row.id === selectedId : undefined}
                onClick={clickable ? () => onRowClick(original) : undefined}
                onKeyDown={clickable ? event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onRowClick(original); } } : undefined}>
                {row.getVisibleCells().map(cell => {
                  const meta = cell.column.columnDef.meta;
                  return (
                    <td key={cell.id} className={cn(meta?.align === "right" && "text-right tabular-nums", meta?.muted && "text-muted-foreground")}>
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
