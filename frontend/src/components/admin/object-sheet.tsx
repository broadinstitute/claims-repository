"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { adminGet, type DepositRef, type Doc, type ObjectDetail, type ObjectEdge, type ObjectSummary, type Triple } from "@/lib/admin/api";
import { Callout, CopyButton, Json, KeyValues, Mono, QueryFeedback, Status, When, number, plural, shortId } from "./shared";

export const DAPPER_ID = /^dapper:[A-Za-z]+\.[A-Za-z0-9_-]{1,200}$/;
export const isDapperId = (value: unknown): value is string => typeof value === "string" && DAPPER_ID.test(value);
export const classOf = (id: string) => id.slice(id.indexOf(":") + 1, id.indexOf("."));
export const text = (value: unknown): string | null => (typeof value === "string" && value ? value : null);

type Open = (id: string) => void;

/** A dapper id as a button that opens that object: its class, then its label when known, else the shortened id. */
export function ObjectRef({ id, summary, onOpen }: { id: string; summary?: ObjectSummary | null; onOpen: Open }) {
  const missing = summary ? !summary.stored : false;
  return (
    <button type="button" title={id} onClick={() => onOpen(id)}
      className="inline-flex max-w-full items-baseline gap-1.5 rounded-sm text-left align-baseline hover:underline focus-visible:outline-2 focus-visible:outline-ring">
      <Status tone={missing ? "warn" : "neutral"}>{summary?.className ?? classOf(id)}{missing ? " · not stored" : ""}</Status>
      <span className={summary?.label ? "min-w-0 break-words text-xs" : "min-w-0 break-all font-mono text-xs"}>{summary?.label ? clip(summary.label) : shortId(id)}</span>
    </button>
  );
}

const clip = (value: string, max = 110) => (value.length > max ? `${value.slice(0, max - 1)}…` : value);

export function TripleText({ triple }: { triple: Triple | null }) {
  if (!triple) return <span className="text-muted-foreground">None: this proposition is a statement only (no subject, relation or object).</span>;
  return <span className="break-all font-mono text-xs">{triple.subject} <span className="text-muted-foreground">—{triple.predicate}→</span> {triple.object}</span>;
}

/** One slot's value as stored: dapper ids open their object, text wraps, anything nested is shown as JSON. */
export function SlotValue({ value, known, onOpen }: { value: unknown; known?: Map<string, ObjectSummary>; onOpen: Open }): ReactNode {
  if (isDapperId(value)) return <ObjectRef id={value} summary={known?.get(value)} onOpen={onOpen} />;
  if (typeof value === "string") {
    return /^https?:\/\//.test(value)
      ? <a href={value} target="_blank" rel="noopener noreferrer" className="break-all underline-offset-4 hover:underline">{value}</a>
      : <span className="whitespace-pre-wrap break-words">{value}</span>;
  }
  if (typeof value === "number" || typeof value === "boolean") return <span className="font-mono text-xs">{String(value)}</span>;
  if (value === null || value === undefined) return <span className="text-muted-foreground">null</span>;
  if (Array.isArray(value) && value.every(item => typeof item === "string")) {
    if (!value.length) return <span className="text-muted-foreground">[] (empty list)</span>;
    return <div className="space-y-1">{value.map((item, i) => <div key={`${i}-${item}`}><SlotValue value={item} known={known} onOpen={onOpen} /></div>)}</div>;
  }
  return <Json value={value} />;
}

/** Every slot of a stored document, in stored order, with the id last since the header already shows it. */
export function Slots({ document, known, onOpen }: { document: Doc; known?: Map<string, ObjectSummary>; onOpen: Open }) {
  const rows = Object.entries(document).filter(([slot]) => slot !== "id");
  if (!rows.length) return <p className="text-sm text-muted-foreground">This object has an id and no other slot.</p>;
  return <KeyValues rows={rows.map(([slot, value]) => [slot, <SlotValue key={slot} value={value} known={known} onOpen={onOpen} />])} />;
}

export function Deposits({ deposits }: { deposits: DepositRef[] }) {
  if (!deposits.length) return <p className="text-sm text-muted-foreground">No deposit carried this object: the service minted it when serving a query, or a loader wrote it.</p>;
  return (
    <ul className="space-y-1.5 text-sm">
      {deposits.map(d => <li key={d.depositId} className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <Link href={`/admin/objects?deposit=${d.depositId}`} className="font-mono text-xs underline-offset-4 hover:underline" title="Every object this deposit carried">{d.depositId}</Link>
        <span>{d.depositor.name}</span>
        <span className="text-muted-foreground"><When value={d.receivedAt} /> · {plural(d.objectCount, "object")}</span>
        {d.status && <Status tone={d.status === "created" ? "good" : d.status === "differs" ? "warn" : "neutral"}>{d.status === "created" ? "created by this deposit" : d.status === "differs" ? "already stored, content differs" : "already stored"}</Status>}
      </li>)}
    </ul>
  );
}

export function Edges({ edges, onOpen }: { edges: ObjectEdge[]; onOpen: Open }) {
  return (
    <ul className="space-y-2 text-sm">
      {edges.map((e, i) => <li key={`${i}-${e.subject}-${e.object}`} className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        {isDapperId(e.subject) ? <ObjectRef id={e.subject} summary={e.subjectSummary} onOpen={onOpen} /> : <Mono>{e.subject}</Mono>}
        <span className="whitespace-nowrap font-mono text-xs text-muted-foreground">—{e.predicate ?? e.className}→</span>
        {isDapperId(e.object) ? <ObjectRef id={e.object} summary={e.objectSummary} onOpen={onOpen} /> : <Mono>{e.object}</Mono>}
      </li>)}
    </ul>
  );
}

function Section({ title, children, hint }: { title: string; children: ReactNode; hint?: ReactNode }) {
  return <section><h3 className="mb-2 text-sm font-semibold">{title}</h3>{hint && <p className="mb-2 text-xs text-muted-foreground">{hint}</p>}{children}</section>;
}

/** URL-backed inspection of any object (?object=…): the stored document slot by slot, what it references, what
 * references it, the deposited edges that touch it and the deposits that carried it. Opening one changes nothing. */
export function ObjectSheet({ objectId, onOpen, onClose }: { objectId: string | null; onOpen: Open; onClose: () => void }) {
  const valid = isDapperId(objectId);
  const query = useQuery({
    queryKey: ["admin", "object", objectId], enabled: valid,
    queryFn: () => adminGet<ObjectDetail>(`objects/${objectId}`),
  });
  const d = query.data;
  const known = new Map<string, ObjectSummary>((d?.references ?? []).map(r => [r.id, r]));
  return (
    <Sheet open={!!objectId} onOpenChange={open => { if (!open) onClose(); }}>
      <SheetContent side="right" className="w-full gap-0 overflow-y-auto sm:max-w-2xl">
        <SheetHeader className="border-b border-border/50">
          <SheetTitle>{d?.className ?? (valid && objectId ? classOf(objectId) : "Object")}</SheetTitle>
          <SheetDescription className="break-all font-mono text-xs">{objectId}</SheetDescription>
        </SheetHeader>
        <div className="space-y-6 p-4">
          {objectId && !valid ? <QueryFeedback pending={false} error={new Error("This is not a dapper identifier.")} />
            : !d ? <QueryFeedback pending={query.isPending} error={query.error} onRetry={() => void query.refetch()} /> : <>
            {!d.stored && <Callout tone="warn" role="status">Not stored: other objects or edges point at this id, but the repository holds no document for it.</Callout>}
            <KeyValues rows={[
              ["Class", <Mono key="c">{d.className ?? "unknown"}</Mono>],
              ...(d.className === "Proposition" ? [["Triple", <TripleText key="t" triple={d.triple} />] as [string, ReactNode]] : []),
              ["Identity profile", d.identityProfile ? <Mono key="i">{d.identityProfile}</Mono> : null],
              ["First stored", <When key="s" value={d.createdAt} empty="Not stored" />],
              ["Last seen", <When key="l" value={d.lastSeenAt} empty="Never" />],
              ["Times seen", number(d.timesSeen)],
              ["Actions", <span key="a" className="inline-flex flex-wrap gap-3">
                {d.resolverUrl && <a href={d.resolverUrl} target="_blank" rel="noopener noreferrer" className="underline-offset-4 hover:underline">Open in resolver</a>}
                {d.className === "Claim" && d.stored && <Link href={`/admin/claims?claim=${encodeURIComponent(d.id)}`} className="underline-offset-4 hover:underline">Open as claim</Link>}
                {d.className === "ScientificAccount" && d.stored && <Link href={`/admin/accounts?account=${encodeURIComponent(d.id)}`} className="underline-offset-4 hover:underline">Open as account</Link>}
                <CopyButton text={d.id} label="Copy ID" />
              </span>],
            ]} />
            {d.document && <Section title="Document" hint="Every slot the stored document has, as stored. A slot that is not listed is not in the database.">
              <Slots document={d.document} known={known} onOpen={onOpen} />
            </Section>}
            <Section title={`Referenced by (${number(d.referencedBy.total)})`}>
              {d.referencedBy.total === 0 ? <p className="text-sm text-muted-foreground">No stored object points at this one.</p> : <>
                <ul className="space-y-1.5">{d.referencedBy.objects.map(o => <li key={o.id}><ObjectRef id={o.id} summary={o} onOpen={onOpen} /></li>)}</ul>
                {d.referencedBy.total > d.referencedBy.objects.length && <p className="mt-2 text-xs text-muted-foreground">
                  Showing {number(d.referencedBy.objects.length)}. <Link href={`/admin/objects?references=${encodeURIComponent(d.id)}`} className="underline-offset-4 hover:underline">See all {number(d.referencedBy.total)}</Link>
                </p>}
              </>}
            </Section>
            {d.edges.length > 0 && <Section title={`Edges (${number(d.edges.length)})`} hint="Deposited edges that start or end here. An edge is not an object and has no id of its own.">
              <Edges edges={d.edges} onOpen={onOpen} />
            </Section>}
            <Section title="Deposits"><Deposits deposits={d.deposits} /></Section>
            {d.document && <details><summary className="cursor-pointer text-sm font-semibold">Raw JSON</summary><div className="mt-2"><Json value={d.document} /></div></details>}
          </>}
        </div>
      </SheetContent>
    </Sheet>
  );
}
