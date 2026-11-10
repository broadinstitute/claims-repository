"use client";

import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { adminGet, type ClaimDetail, type ScoreDoc } from "@/lib/admin/api";
import { DataTable } from "./data-table";
import { CopyButton, FamilyBadge, Json, KeyValues, MintBadge, QueryFeedback, Status, When, number, shortId } from "./shared";

const scoreColumns: ColumnDef<ScoreDoc, unknown>[] = [
  { id: "metric", accessorKey: "metric", header: "Metric", cell: ({ row }) => row.original.missing ? <span className="text-muted-foreground">Missing score</span> : <span className="font-mono text-xs">{row.original.metric}</span> },
  { id: "kind", accessorKey: "score_kind", header: "Kind", cell: ({ row }) => row.original.score_kind ?? "—" },
  { id: "value", accessorKey: "value", header: "Value", meta: { align: "right" }, cell: ({ row }) => row.original.value ?? "—" },
  { id: "interpretation", accessorKey: "interpretation", header: "Interpretation", enableSorting: false, cell: ({ row }) => <span className="text-xs text-muted-foreground">{row.original.interpretation}</span> },
];

/** URL-backed claim inspection (?claim=…); opening re-verifies the claim but never asserts it. */
export function ClaimSheet({ claimId, onClose }: { claimId: string | null; onClose: () => void }) {
  const query = useQuery({
    queryKey: ["admin", "claim", claimId], enabled: !!claimId,
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
          {!d ? <QueryFeedback pending={query.isPending} error={query.error} onRetry={() => void query.refetch()} /> : <>
            <Verification d={d} />
            <KeyValues rows={[
              ["Statement", d.statement],
              ["Triple", d.triple && <span className="font-mono text-xs">{d.triple.subject} <span className="text-muted-foreground">—{d.triple.predicate}→</span> {d.triple.object}</span>],
              ["Status", <span key="s" className="inline-flex flex-wrap gap-1.5"><MintBadge minted={d.minted} />{d.method && <Status>{d.method}</Status>}{d.indexRow && <FamilyBadge family={d.indexRow.family} />}</span>],
              ["Assertions", number(d.timesAsserted)],
              ["Minted", <When key="m" value={d.mintedAt} empty="Not yet" />],
              ["Last asserted", <When key="a" value={d.lastAssertedAt} empty="Never" />],
              ["Identity profile", <span key="i" className="font-mono text-xs">{d.identityProfile}</span>],
              ["Stored claims on proposition", number(d.claimsAssessingProposition)],
              ["Resolver", <span key="r" className="inline-flex gap-3"><a href={d.resolverUrl} target="_blank" rel="noopener noreferrer" className="underline-offset-4 hover:underline">Open</a><CopyButton text={d.claimId} label="Copy ID" /></span>],
            ]} />
            {d.scores.length > 0 && <section>
              <h3 className="mb-2 text-sm font-semibold">Scores</h3>
              <div className="rounded-lg border border-border/50"><DataTable label="Scores" data={d.scores} columns={scoreColumns} getRowId={s => s.id} /></div>
            </section>}
            {d.evidence.length > 0 && <section>
              <h3 className="mb-2 text-sm font-semibold">Evidence ({d.evidence.length})</h3>
              <div className="space-y-2 text-sm">
                {d.evidence.map(ev => <details key={ev.id}><summary className="cursor-pointer">{ev.missing ? `${shortId(ev.id)} (missing)` : ev.name ?? shortId(ev.id)}</summary><div className="mt-2"><Json value={ev} /></div></details>)}
              </div>
            </section>}
            {d.indexRow && <section>
              <h3 className="mb-2 text-sm font-semibold">Index row</h3>
              <KeyValues rows={Object.entries(d.indexRow.columns).map(([k, v]) => [k, <span key={k} className="font-mono text-xs">{v === null ? "—" : String(v)}</span>])} />
            </section>}
            {d.indexRow?.synthesisInputs && <details><summary className="cursor-pointer text-sm font-semibold">Synthesis inputs</summary><div className="mt-2"><Json value={d.indexRow.synthesisInputs} /></div></details>}
            <details><summary className="cursor-pointer text-sm font-semibold">Claim document</summary><div className="mt-2"><Json value={d.claim} /></div></details>
            {d.proposition && <details><summary className="cursor-pointer text-sm font-semibold">Proposition document</summary><div className="mt-2"><Json value={d.proposition} /></div></details>}
          </>}
        </div>
      </SheetContent>
    </Sheet>
  );
}

function Verification({ d }: { d: ClaimDetail }) {
  const v = d.verification;
  if (!v.templateBacked) {
    return <p className="rounded-lg border border-blue-500/25 bg-blue-500/5 px-4 py-3 text-sm text-blue-700 dark:text-blue-300">Deposited claim: no template row implies it, so the stored document is shown as deposited.</p>;
  }
  if (v.synthesizes && v.storedMatches !== false) {
    return <p className="rounded-lg border border-emerald-500/25 bg-emerald-500/5 px-4 py-3 text-sm text-emerald-700 dark:text-emerald-300">
      {d.minted ? "Verified: the template reproduces this claim’s content address and the stored document matches." : "Verified: the template reproduces this claim’s content address. It has not been served yet, so nothing is stored."}
    </p>;
  }
  return <p role="alert" className="rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">Verification failed: {v.error}</p>;
}
