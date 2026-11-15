"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { adminGet, type ClaimDetail, type ClaimSource, type ScoreDoc } from "@/lib/admin/api";
import { DataTable } from "./data-table";
import { Callout, CopyButton, Dimensions, Json, KeyValues, MintBadge, Mono, NoCollection, QueryFeedback, Status, When, number, shortId } from "./shared";

const scoreColumns: ColumnDef<ScoreDoc, unknown>[] = [
  { id: "metric", accessorKey: "metric", header: "Metric", cell: ({ row }) => row.original.missing ? <span className="text-muted-foreground">Missing score</span> : <span className="font-mono text-xs">{row.original.metric}</span> },
  { id: "kind", accessorKey: "score_kind", header: "Kind", cell: ({ row }) => row.original.score_kind ?? "—" },
  { id: "value", accessorKey: "value", header: "Value", meta: { align: "right" }, cell: ({ row }) => row.original.value ?? "—" },
  { id: "interpretation", accessorKey: "interpretation", header: "Interpretation", enableSorting: false, cell: ({ row }) => <span className="text-xs text-muted-foreground">{row.original.interpretation}</span> },
];

/** URL-backed claim inspection (?claim=…); opening re-renders the claim from its template but never asserts or mints it. */
const CLAIM_ID = /^dapper:[A-Za-z]+\.[A-Za-z0-9_-]{1,200}$/;

export function ClaimSheet({ claimId, onClose }: { claimId: string | null; onClose: () => void }) {
  const valid = !!claimId && CLAIM_ID.test(claimId);
  const query = useQuery({
    queryKey: ["admin", "claim", claimId], enabled: valid,
    queryFn: () => adminGet<ClaimDetail>(`claims/${claimId}`),
  });
  const d = query.data;
  return (
    <Sheet open={!!claimId} onOpenChange={open => { if (!open) onClose(); }}>
      <SheetContent side="right" className="w-full gap-0 overflow-y-auto sm:max-w-2xl">
        <SheetHeader className="border-b border-border/50">
          <SheetTitle>Claim</SheetTitle>
          <SheetDescription className="break-all font-mono text-xs">{claimId}</SheetDescription>
        </SheetHeader>
        <div className="space-y-6 p-4">
          {claimId && !valid ? <QueryFeedback pending={false} error={new Error("This is not a claim ID.")} />
            : !d ? <QueryFeedback pending={query.isPending} error={query.error} onRetry={() => void query.refetch()} /> : <>
            <Verification d={d} />
            <KeyValues rows={[
              ["Statement", d.statement],
              ["Triple", d.triple && <span className="font-mono text-xs">{d.triple.subject} <span className="text-muted-foreground">—{d.triple.predicate}→</span> {d.triple.object}</span>],
              ["Status", <span key="s" className="inline-flex flex-wrap gap-1.5"><MintBadge minted={d.minted} />{d.method && <Status>{d.method}</Status>}</span>],
              ["Collection", d.collection
                ? <span key="c" className="inline-flex flex-wrap items-center gap-2"><Link href={`/admin/collections/${encodeURIComponent(d.collection)}`} className="font-mono text-xs underline-offset-4 hover:underline">{d.collection}</Link>{d.claimKey && <Mono className="text-muted-foreground">{d.claimKey}</Mono>}</span>
                : <NoCollection key="c" />],
              ["Assertions", number(d.timesAsserted)],
              ["Minted", <When key="m" value={d.mintedAt} empty="Not yet" />],
              ["Last asserted", <When key="a" value={d.lastAssertedAt} empty="Never" />],
              ["Identity profile", <span key="i" className="font-mono text-xs">{d.identityProfile}</span>],
              ["Stored claims on proposition", number(d.claimsAssessingProposition)],
              ["Resolver", <span key="r" className="inline-flex gap-3"><a href={d.resolverUrl} target="_blank" rel="noopener noreferrer" className="underline-offset-4 hover:underline">Open</a><CopyButton text={d.claimId} label="Copy ID" /></span>],
              ["Stored objects", <span key="o" className="inline-flex flex-wrap gap-3">
                {d.minted && <ObjectLink id={d.claimId}>Claim</ObjectLink>}
                {typeof d.proposition?.id === "string" && <ObjectLink id={d.proposition.id}>Proposition</ObjectLink>}
                {!d.minted && !d.proposition && <span className="text-muted-foreground">Nothing stored yet</span>}
              </span>],
            ]} />
            {d.scores.length > 0 && <section>
              <h3 className="mb-2 text-sm font-semibold">Scores</h3>
              <div className="rounded-lg border border-border/50"><DataTable label="Scores" data={d.scores} columns={scoreColumns} getRowId={s => s.id} /></div>
            </section>}
            {d.evidence.length > 0 && <section>
              <h3 className="mb-2 text-sm font-semibold">Evidence ({d.evidence.length})</h3>
              <div className="space-y-2 text-sm">
                {d.evidence.map(ev => <details key={ev.id}><summary className="cursor-pointer">{ev.missing ? `${shortId(ev.id)} (missing)` : ev.name ?? shortId(ev.id)}</summary><div className="mt-2 space-y-2">{!ev.missing && <ObjectLink id={ev.id}>Open with what it references</ObjectLink>}<Json value={ev} /></div></details>)}
              </div>
            </section>}
            {d.source && <Source source={d.source} />}
            <details><summary className="cursor-pointer text-sm font-semibold">Claim document</summary><div className="mt-2"><Json value={d.claim} /></div></details>
            {d.proposition && <details><summary className="cursor-pointer text-sm font-semibold">Proposition document</summary><div className="mt-2"><Json value={d.proposition} /></div></details>}
          </>}
        </div>
      </SheetContent>
    </Sheet>
  );
}

/** Opens a stored object in the object browser, where every slot and reference is shown. */
function ObjectLink({ id, children }: { id: string; children: ReactNode }) {
  return <Link href={`/admin/objects?object=${encodeURIComponent(id)}`} className="text-xs underline-offset-4 hover:underline">{children}</Link>;
}

function Source({ source: s }: { source: ClaimSource }) {
  return (
    <section>
      <h3 className="mb-2 text-sm font-semibold">Source</h3>
      <p className="mb-3 text-xs text-muted-foreground">The collection row (or group of edges) the template rendered this claim from.</p>
      <KeyValues rows={[
        ["Collection", s.collection
          ? <Link key="c" href={`/admin/collections/${encodeURIComponent(s.collection)}`} className="font-mono text-xs underline-offset-4 hover:underline">{s.collection}</Link>
          : <span key="c" className="font-mono text-xs">unknown</span>],
        ["Claim key", <Mono key="k">{s.claimKey}</Mono>],
        ["Template", <span key="t" className="inline-flex flex-wrap items-center gap-2">{s.template ? <><Mono>{s.template.name} v{s.template.version}</Mono><span className="break-all font-mono text-[11px] text-muted-foreground">{s.template.fileId}</span></> : <span className="text-muted-foreground">unknown</span>}</span>],
        ["Releases", s.releases.length ? <span key="r" className="inline-flex flex-wrap gap-1">{s.releases.map(r => <Status key={r}>{r}</Status>)}</span> : "—"],
        ["Dataset", <div key="d" className="space-y-0.5"><Dimensions value={s.dataset.dimensions} /><div className="break-all font-mono text-[11px] text-muted-foreground">{s.dataset.id}</div></div>],
        ...(s.row ? [
          ["Row kind", <Mono key="rk">{s.row.kind}</Mono>],
          ["Subject", <span key="su">{s.row.subject.label ?? s.row.subject.value} <Mono className="text-muted-foreground">{s.row.subject.id}</Mono></span>],
          ["Object", <span key="ob">{s.row.object.label ?? "—"} <Mono className="text-muted-foreground">{s.row.object.id}</Mono></span>],
          ["Row", <Mono key="ri" className="text-muted-foreground">{String(s.row.rowId)} in {s.row.fileId}</Mono>],
        ] as [string, ReactNode][] : []),
        ...(s.group ? [
          ["Edge group", <span key="g"><Mono>{s.group.edgeKind}</Mono> to {s.group.label ?? s.group.id} ({number(s.group.count)} {s.group.count === 1 ? "edge" : "edges"})</span>],
          ["Group ID", <Mono key="gi" className="text-muted-foreground">{s.group.id}</Mono>],
        ] as [string, ReactNode][] : []),
      ]} />
      {s.row && Object.keys(s.row.columns).length > 0 && <div className="mt-3">
        <h4 className="mb-2 text-xs font-medium text-muted-foreground">Row columns</h4>
        <KeyValues rows={Object.entries(s.row.columns).map(([k, v]) => [k, <span key={k} className="break-all font-mono text-xs">{v === null || v === undefined ? "—" : typeof v === "object" ? JSON.stringify(v) : String(v)}</span>])} />
      </div>}
    </section>
  );
}

function Verification({ d }: { d: ClaimDetail }) {
  const v = d.verification;
  if (!v.collectionBacked) {
    return <Callout tone="info">No collection: no collection template produces this claim id. It was deposited or minted outside a collection, so the stored document is shown as stored and cannot be re-rendered.</Callout>;
  }
  if (v.renders === false) {
    return <Callout tone="bad" role="alert">Verification failed: the template no longer renders this claim id. {v.error}</Callout>;
  }
  if (v.renders && v.storedMatches === false) {
    return <Callout tone="bad" role="alert">The template renders this claim id, but the stored document differs from the re-rendered one.{v.error ? ` ${v.error}` : ""}</Callout>;
  }
  if (v.renders) {
    return <Callout tone="good">
      {d.minted
        ? "Verified: the collection template re-renders this exact claim id and the stored document matches."
        : "Verified: the collection template re-renders this exact claim id. It has not been served yet, so nothing is stored; the documents below are the re-rendered ones."}
    </Callout>;
  }
  return <Callout tone="warn" role="status">This claim could not be verified.{v.error ? ` ${v.error}` : ""}</Callout>;
}
