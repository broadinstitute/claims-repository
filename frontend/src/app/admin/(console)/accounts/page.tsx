"use client";

import { Suspense, type ReactNode } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DataTable } from "@/components/admin/data-table";
import { Deposits, Edges, ObjectRef, ObjectSheet, TripleText, isDapperId, text } from "@/components/admin/object-sheet";
import { Callout, CopyButton, Hash, KeyValues, Mono, PageHeading, Pagination, Panel, QueryFeedback, Status, When, number, plural, shortId, type Tone } from "@/components/admin/shared";
import { useUrlState } from "@/components/admin/use-url-state";
import { adminGet, type AccountClaim, type AccountDetail, type AccountEvidence, type AccountList, type AccountRow, type StoredNode } from "@/lib/admin/api";

const PAGE_SIZE = 25;
type Open = (id: string) => void;

const columns: ColumnDef<AccountRow, unknown>[] = [
  { id: "question", header: "Question", enableSorting: false, cell: ({ row }) => <div className="max-w-2xl">
    <div className="line-clamp-2 break-words font-medium">{row.original.question ?? "No question stored"}</div>
    <div className="mt-0.5 font-mono text-[11px] text-muted-foreground" title={row.original.id}>{shortId(row.original.id)}</div>
  </div> },
  { id: "claims", header: "Claims", enableSorting: false, meta: { align: "right" }, cell: ({ row }) => number(row.original.claims) },
  { id: "attributedTo", header: "Attributed to", enableSorting: false, cell: ({ row }) => row.original.attributedTo.length
    ? <span className="break-words">{row.original.attributedTo.join(", ")}</span> : <span className="text-muted-foreground">—</span> },
  { id: "depositor", header: "Deposited by", enableSorting: false, cell: ({ row }) => row.original.lastDeposit
    ? <span>{row.original.lastDeposit.depositor.name}{row.original.deposits > 1 && <span className="text-muted-foreground"> · {plural(row.original.deposits, "deposit")}</span>}</span>
    : <span className="text-muted-foreground">No deposit</span> },
  { id: "storedAt", header: "First stored", enableSorting: false, meta: { muted: true }, cell: ({ row }) => <When value={row.original.storedAt} /> },
];

const directionTone = (direction: string | null): Tone => direction === "SUPPORTS" ? "good" : direction === "REFUTES" || direction === "CONTRADICTS" ? "bad" : "neutral";

/** A referenced object: opens in the object sheet; flagged when the repository holds no document for it. */
function Ref({ node, onOpen }: { node: StoredNode | null; onOpen: Open }) {
  if (!node) return <span className="text-muted-foreground">—</span>;
  if (!isDapperId(node.id)) return <Mono>{node.id}</Mono>;
  return <ObjectRef id={node.id} summary={{ id: node.id, className: null, label: null, stored: node.document !== null }} onOpen={onOpen} />;
}

function Prose({ children }: { children: ReactNode }) {
  return <p className="whitespace-pre-wrap break-words text-sm leading-6">{children}</p>;
}

function Evidence({ item, index, onOpen }: { item: AccountEvidence; index: number; onOpen: Open }) {
  const doc = item.document;
  if (!doc) return <Callout tone="warn" role="status">Evidence item {shortId(item.id)} is referenced by the claim but not stored.</Callout>;
  const publication = item.publication?.document;
  const pmid = text(publication?.pmid);
  const doi = text(publication?.doi)?.replace(/^doi:/, "");
  return (
    <div className="space-y-3 rounded-md border border-border/50 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium text-muted-foreground">Evidence {index + 1}</span>
        {text(doc.direction) && <Status tone={directionTone(text(doc.direction))}>{text(doc.direction)}</Status>}
        <Ref node={item} onOpen={onOpen} />
      </div>
      {text(doc.snippet) && <blockquote className="border-l-2 border-border pl-3 text-sm italic leading-6">{text(doc.snippet)}</blockquote>}
      <KeyValues rows={[
        ["Explanation", text(doc.explanation)],
        ["Context", text(doc.context)],
        ["Publication", item.publication ? <div key="p" className="space-y-1">
          <div>{text(publication?.title) ?? text(doc.reference_title) ?? "Title not stored"}</div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
            {pmid && <a href={`https://pubmed.ncbi.nlm.nih.gov/${encodeURIComponent(pmid)}/`} target="_blank" rel="noopener noreferrer" className="underline-offset-4 hover:underline">PMID {pmid}</a>}
            {doi && <a href={`https://doi.org/${doi}`} target="_blank" rel="noopener noreferrer" className="break-all underline-offset-4 hover:underline">doi:{doi}</a>}
            <Ref node={item.publication} onOpen={onOpen} />
          </div>
        </div> : null],
        ["Located quotes", item.quotes.length ? <div key="q" className="space-y-3">
          {item.quotes.map(quote => {
            const locator = quote.document;
            const file = quote.file?.document;
            return <div key={quote.id} className="space-y-1 text-xs">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <Ref node={quote} onOpen={onOpen} />
                {locator && <span className="font-mono text-muted-foreground">{text(locator.selector)} · bytes {String(locator.byte_start ?? "?")}+{String(locator.byte_length ?? "?")}</span>}
              </div>
              {quote.file && <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className="text-muted-foreground">in</span><Ref node={quote.file} onOpen={onOpen} />
                {file && <><Mono>{text(file.filename)}</Mono><Hash value={text(file.sha256)} /></>}
              </div>}
              {quote.derivedFrom.map(source => <div key={source.id} className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className="text-muted-foreground">derived from</span><Ref node={source} onOpen={onOpen} />
                {text(source.document?.location) && <a href={text(source.document?.location) ?? "#"} target="_blank" rel="noopener noreferrer" className="break-all underline-offset-4 hover:underline">public source</a>}
              </div>)}
            </div>;
          })}
        </div> : <span key="q" className="text-muted-foreground">None: this evidence has no located quote.</span>],
      ]} />
    </div>
  );
}

function Claim({ claim, index, onOpen }: { claim: AccountClaim; index: number; onOpen: Open }) {
  const doc = claim.document;
  const proposition = claim.proposition?.document;
  return (
    <Panel padded title={`Claim ${index + 1}`} description={text(proposition?.statement) ?? "The proposition has no statement."}
      action={text(doc?.direction) ? <Status tone={directionTone(text(doc?.direction))}>{text(doc?.direction)}</Status> : undefined}>
      {!doc ? <Callout tone="warn" role="status">This claim is listed by the account but not stored.</Callout> : <div className="space-y-4">
        <KeyValues rows={[
          ["Assessment", <Prose key="s">{text(doc.statement) ?? "—"}</Prose>],
          ["Status", text(doc.status) ? <Status key="st">{text(doc.status)}</Status> : null],
          ["Proposition", <div key="p" className="space-y-1"><Ref node={claim.proposition} onOpen={onOpen} /><div><TripleText triple={claim.triple} /></div></div>],
          ["Claim", <span key="c" className="inline-flex flex-wrap items-center gap-3"><Ref node={claim} onOpen={onOpen} /><CopyButton text={claim.id} label="Copy ID" /></span>],
        ]} />
        <div className="space-y-3">
          <h3 className="text-sm font-semibold">Evidence ({number(claim.evidence.length)})</h3>
          {claim.evidence.length ? claim.evidence.map((item, i) => <Evidence key={item.id} item={item} index={i} onOpen={onOpen} />)
            : <p className="text-sm text-muted-foreground">This claim cites no evidence.</p>}
        </div>
      </div>}
    </Panel>
  );
}

function Account({ id, onOpen, onBack }: { id: string; onOpen: Open; onBack: () => void }) {
  const query = useQuery({ queryKey: ["admin", "account", id], queryFn: () => adminGet<AccountDetail>(`accounts/${id}`), enabled: isDapperId(id) });
  const d = query.data;
  const back = <Button size="sm" variant="outline" onClick={onBack}><ArrowLeft className="size-4" aria-hidden />All accounts</Button>;
  if (!isDapperId(id)) return <><PageHeading title="Scientific account" actions={back} /><QueryFeedback pending={false} error={new Error("This is not a dapper identifier.")} /></>;
  if (!d) return <><PageHeading title="Scientific account" actions={back} /><QueryFeedback pending={query.isPending} error={query.error} onRetry={() => void query.refetch()} /></>;
  const activity = d.activity?.document;
  return <>
    <PageHeading title={text(d.question?.document?.text) ?? text(d.document.name) ?? "Scientific account"} subtitle={<Mono>{d.id}</Mono>} actions={back} />
    {d.missing.length > 0 && <Callout tone="warn" role="status">
      {plural(d.missing.length, "object")} this account points at {d.missing.length === 1 ? "is" : "are"} not stored: {d.missing.map(shortId).join(", ")}.
    </Callout>}
    <Panel padded title="Account" description="The ScientificAccount object and what it points at, each as stored.">
      <KeyValues rows={[
        ["Question", <div key="q" className="space-y-1"><Prose>{text(d.question?.document?.text) ?? "—"}</Prose><Ref node={d.question} onOpen={onOpen} /></div>],
        ["Context", text(d.document.context) ? <Prose key="c">{text(d.document.context)}</Prose> : null],
        ["Closing remarks", text(d.document.closing_remarks) ? <Prose key="r">{text(d.document.closing_remarks)}</Prose> : null],
        ["Attributed to", d.attributedTo.length ? <div key="a" className="space-y-1">{d.attributedTo.map(a => <div key={a.id} className="flex flex-wrap items-center gap-2">
          <span>{text(a.document?.name) ?? "—"}</span><Ref node={a} onOpen={onOpen} /></div>)}</div> : null],
        ["Built by", d.activity ? <div key="b" className="space-y-1">
          <div>{text(activity?.name) ?? "—"}{text(activity?.software_name) && <span className="text-muted-foreground"> · {text(activity?.software_name)} {text(activity?.software_version)}</span>}</div>
          {text(activity?.command) && <Mono className="block text-muted-foreground">{text(activity?.command)}</Mono>}
          <Ref node={d.activity} onOpen={onOpen} />
        </div> : null],
        ["First stored", <When key="s" value={d.storedAt} />],
        ["Objects", <span key="o" className="inline-flex flex-wrap gap-1">{d.objectsByClass.map(c => <Status key={c.className}>{number(c.count)} {c.className}</Status>)}</span>],
        ["Deposits", <Deposits key="d" deposits={d.deposits} />],
        ["Actions", <span key="x" className="inline-flex flex-wrap gap-3">
          <button type="button" className="underline-offset-4 hover:underline" onClick={() => onOpen(d.id)}>All slots of the account object</button>
          <a href={d.resolverUrl} target="_blank" rel="noopener noreferrer" className="underline-offset-4 hover:underline">Open in resolver</a>
          <CopyButton text={d.id} label="Copy ID" />
        </span>],
      ]} />
    </Panel>
    {d.claims.map((claim, i) => <Claim key={claim.id} claim={claim} index={i} onOpen={onOpen} />)}
    {d.claims.length === 0 && <Callout tone="warn" role="status">This account lists no component claims.</Callout>}
    {d.edges.length > 0 && <Panel padded title={`Edges (${number(d.edges.length)})`} description="Deposited provenance edges on the files this account quotes. Edges are not objects.">
      <Edges edges={d.edges} onOpen={onOpen} />
    </Panel>}
  </>;
}

function Accounts() {
  const { params, update } = useUrlState();
  const page = Math.max(1, Number(params.get("page")) || 1);
  const account = params.get("account");
  const object = params.get("object");
  const query = useQuery({
    queryKey: ["admin", "accounts", page], enabled: !account,
    queryFn: () => adminGet<AccountList>("accounts", { page, page_size: PAGE_SIZE }), placeholderData: keepPreviousData,
  });
  const openObject = (id: string) => update({ object: id });
  return <>
    {account ? <Account id={account} onOpen={openObject} onBack={() => update({ account: undefined, object: undefined })} /> : <>
      <PageHeading title="Scientific accounts" subtitle="Every deposited ScientificAccount: a question, the claims that answer it, and the evidence each claim cites. Opening one shows all of it as stored." />
      <Panel title="Accounts" description="Newest first.">
        {!query.data ? <div className="px-4"><QueryFeedback pending={query.isPending} error={query.error} onRetry={() => void query.refetch()} /></div> : <>
          <DataTable label="Scientific accounts" data={query.data.rows} columns={columns} getRowId={r => r.id}
            onRowClick={r => update({ account: r.id, page: undefined })} rowLabel={r => `Open account ${r.question ?? r.id}`} empty="No scientific account has been deposited." />
          <Pagination page={query.data.page} total={query.data.total} pageSize={query.data.pageSize} fetching={query.isFetching} onPage={n => update({ page: n > 1 ? n : undefined })} />
        </>}
      </Panel>
    </>}
    <ObjectSheet objectId={object} onOpen={openObject} onClose={() => update({ object: undefined })} />
  </>;
}

export default function AccountsPage() {
  return <Suspense fallback={<QueryFeedback pending error={null} />}><Accounts /></Suspense>;
}
