"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Family } from "@/lib/admin/api";

// Formatting and building blocks modeled on akleao's Admin V2 shared.tsx.

const nf = new Intl.NumberFormat("en-US");
export const number = (value: number | null | undefined) => (value === null || value === undefined ? "—" : nf.format(value));
export const plural = (n: number, one: string, many = `${one}s`) => `${number(n)} ${n === 1 ? one : many}`;
export const percent = (part: number, whole: number) => (whole ? `${Math.round((part / whole) * 1000) / 10}%` : "—");
export const ms = (v: number | null | undefined) => (v === null || v === undefined ? "—" : v < 10 ? `${v.toFixed(1)} ms` : `${Math.round(v)} ms`);
export const bytes = (v: number | null | undefined) => {
  if (v === null || v === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"]; let i = 0; let n = v;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n >= 10 || i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
};
export const uptime = (sec: number) => {
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
};
export const clock = (iso: string) => new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
export const shortId = (id: string) => (id.length > 30 ? `${id.slice(0, 18)}…${id.slice(-6)}` : id);

/** Twitter-style age: "now", "10m", "5h", "3d", then "Oct 3" (year only when not this year). */
export const shortAgo = (value: string, now = Date.now()) => {
  const date = new Date(value);
  const minutes = Math.floor((now - date.getTime()) / 60_000);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 60 * 24) return `${Math.floor(minutes / 60)}h`;
  if (minutes < 60 * 24 * 7) return `${Math.floor(minutes / (60 * 24))}d`;
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric", ...(date.getFullYear() === new Date(now).getFullYear() ? {} : { year: "numeric" }) });
};

/** A timestamp as a short age; hover shows the full date and time. */
export function When({ value, empty = "—" }: { value: string | null | undefined; empty?: string }) {
  if (!value) return <span className="text-muted-foreground">{empty}</span>;
  return (
    <Tooltip>
      <TooltipTrigger asChild><time dateTime={value} className="cursor-default tabular-nums">{shortAgo(value)}</time></TooltipTrigger>
      <TooltipContent>{new Date(value).toLocaleString()}</TooltipContent>
    </Tooltip>
  );
}

export function PageHeading({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-4 pb-1">
      <div className="min-w-0">
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <div className="mt-1 text-sm text-muted-foreground">{subtitle}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-start gap-2">{actions}</div>}
    </div>
  );
}

export function QueryFeedback({ pending, error, onRetry }: { pending: boolean; error: Error | null; onRetry?: () => void }) {
  if (error) {
    return (
      <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-destructive/30 p-4 text-sm text-destructive">
        <span>Could not load current data. {error.message}</span>
        {onRetry && <Button size="sm" variant="outline" onClick={onRetry}>Retry</Button>}
      </div>
    );
  }
  if (pending) {
    return (
      <div role="status" className="flex items-center gap-2 py-10 text-sm text-muted-foreground">
        <span className="size-3.5 animate-spin rounded-full border-2 border-muted border-t-muted-foreground motion-reduce:animate-none" aria-hidden />
        Loading claims data…
      </div>
    );
  }
  return null;
}

export function Panel({ title, description, children, action, footer, padded = false }: {
  title: string; description?: ReactNode; children: ReactNode; action?: ReactNode; footer?: ReactNode; padded?: boolean;
}) {
  return (
    <Card className="min-w-0 gap-0 overflow-hidden rounded-lg border-border/50 py-0">
      <CardHeader className="gap-1.5 border-b border-border/50 px-5 py-4">
        <CardTitle><h2 className="text-base font-semibold tracking-tight">{title}</h2></CardTitle>
        {description && <CardDescription className="max-w-3xl text-xs leading-5">{description}</CardDescription>}
        {action && <CardAction>{action}</CardAction>}
      </CardHeader>
      <CardContent className={padded ? "px-5 py-4" : "p-0"}>{children}</CardContent>
      {footer && <div className="border-t border-border/50 px-5 py-3 text-[11px] leading-5 text-muted-foreground">{footer}</div>}
    </Card>
  );
}

export function Metric({ title, value, hint, href }: { title: string; value: string; hint?: ReactNode; href?: string }) {
  const content = <>
    <div className="text-xs text-muted-foreground">{title}</div>
    <div className="mt-2 text-2xl font-semibold tabular-nums">{value}</div>
    {hint && <div className="mt-2 text-xs text-muted-foreground">{hint}</div>}
  </>;
  const style = "block rounded-lg border border-border/50 bg-card p-4";
  return href
    ? <Link href={href} className={`${style} transition-colors hover:bg-muted/40 focus-visible:outline-2 focus-visible:outline-ring`}>{content}</Link>
    : <div className={style}>{content}</div>;
}

type Tone = "neutral" | "good" | "warn" | "bad" | "info";
const TONES: Record<Tone, string> = {
  neutral: "border-border/50 text-muted-foreground",
  good: "border-emerald-500/25 bg-emerald-500/5 text-emerald-700 dark:text-emerald-300",
  warn: "border-amber-500/25 bg-amber-500/5 text-amber-800 dark:text-amber-300",
  bad: "border-destructive/25 bg-destructive/5 text-destructive",
  info: "border-blue-500/25 bg-blue-500/5 text-blue-700 dark:text-blue-300",
};
export function Status({ children, tone = "neutral" }: { children: ReactNode; tone?: Tone }) {
  return <Badge variant="outline" className={`whitespace-nowrap rounded-md text-[11px] font-medium ${TONES[tone]}`}>{children}</Badge>;
}

export const FAMILY_LABEL: Record<Family, string> = { gene: "gene", gene_set: "gene set", roster: "roster", deposited: "deposited" };
export const FamilyBadge = ({ family }: { family: Family }) => <Status tone={family === "deposited" ? "info" : "neutral"}>{FAMILY_LABEL[family]}</Status>;
export const MintBadge = ({ minted }: { minted: boolean }) => minted ? <Status tone="good">minted</Status> : <Status tone="warn">implied</Status>;

export function Empty({ children }: { children: ReactNode }) {
  return <p className="px-4 py-8 text-sm text-muted-foreground">{children}</p>;
}

export function Pagination({ page, total, pageSize, onPage, fetching }: { page: number; total: number; pageSize: number; onPage: (page: number) => void; fetching: boolean }) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border/50 px-4 py-3 text-xs text-muted-foreground">
      <span>{plural(total, "result")} · Page {number(page)} of {number(pages)}</span>
      <div className="flex gap-2">
        <Button size="sm" variant="outline" disabled={page <= 1 || fetching} onClick={() => onPage(page - 1)}>Previous</Button>
        <Button size="sm" variant="outline" disabled={page >= pages || fetching} onClick={() => onPage(page + 1)}>Next</Button>
      </div>
    </div>
  );
}

export function Meter({ part, whole }: { part: number; whole: number }) {
  const width = whole ? Math.min(100, (part / whole) * 100) : 0;
  return (
    <div className="h-1.5 min-w-16 flex-1 overflow-hidden rounded-full bg-muted" role="meter" aria-valuemin={0} aria-valuemax={whole} aria-valuenow={part} aria-label={percent(part, whole)}>
      <span className="block h-full rounded-full bg-bar" style={{ width: `${width}%` }} />
    </div>
  );
}

export function KeyValues({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-sm">
      {rows.map(([k, v]) => [
        <dt key={`${k}-k`} className="text-muted-foreground">{k}</dt>,
        <dd key={`${k}-v`} className="min-w-0 break-words">{v ?? "—"}</dd>,
      ])}
    </dl>
  );
}

export function Json({ value }: { value: unknown }) {
  return <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border/50 bg-muted/30 p-3 font-mono text-[11.5px] leading-5">{JSON.stringify(value, null, 2)}</pre>;
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  return (
    <Button variant="link" className="h-auto p-0 text-xs text-muted-foreground" onClick={async event => {
      const target = event.currentTarget;
      try { await navigator.clipboard.writeText(text); target.textContent = "Copied"; } catch { target.textContent = "Copy failed"; }
      setTimeout(() => { target.textContent = label; }, 1200);
    }}>{label}</Button>
  );
}
