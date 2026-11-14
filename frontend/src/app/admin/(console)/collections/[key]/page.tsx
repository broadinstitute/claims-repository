"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { Suspense, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Button } from "@/components/ui/button";
import { ClaimSheet } from "@/components/admin/claim-sheet";
import { DataTable } from "@/components/admin/data-table";
import {
  Callout, CopyButton, Coverage, Dimensions, Hash, Json, KeyValues, Mono, PageHeading, Panel, Pre, QueryFeedback, Status, When, bytes, number, plural,
} from "@/components/admin/shared";
import { useUrlState } from "@/components/admin/use-url-state";
import {
  adminGet, type CollectionDataset, type CollectionDetail, type CollectionQuery, type CollectionTemplate, type DatasetFile,
  type MetricInfo, type QueryRun, type SourceFile, type Spec, type TemplateClaim,
} from "@/lib/admin/api";

const selectClass = "h-8 max-w-full rounded-md border border-input bg-background px-2 text-sm";
const claimsHref = (key: string, extra: Record<string, string> = {}) => `/admin/claims?${new URLSearchParams({ collection: key, ...extra })}`;
const asText = (value: unknown) => (typeof value === "string" ? value : JSON.stringify(value, null, 2));
const isEmpty = (value: unknown) => value === null || value === undefined || value === "" || (typeof value === "object" && Object.keys(value as object).length === 0);

/** A manifest spec as "field value" pairs: scalars and lists inline, nested objects as compact JSON. */
function SpecFields({ spec }: { spec: unknown }) {
  if (spec === null || typeof spec !== "object" || Array.isArray(spec)) return <Mono>{asText(spec)}</Mono>;
  const entries = Object.entries(spec as Spec);
  if (!entries.length) return <span className="text-muted-foreground">—</span>;
  return (
    <span className="inline-flex flex-wrap gap-x-4 gap-y-0.5 text-xs">
      {entries.map(([k, v]) => <span key={k} className="min-w-0 break-words">
        <span className="text-muted-foreground">{k}</span>{" "}
        <span className="font-mono">{Array.isArray(v) && v.every(x => typeof x !== "object") ? v.join(", ") : typeof v === "object" && v !== null ? JSON.stringify(v) : String(v)}</span>
      </span>)}
    </span>
  );
}

function SpecTable({ specs, empty }: { specs: Record<string, unknown>; empty: string }) {
  const entries = Object.entries(specs ?? {});
  if (!entries.length) return <p className="text-sm text-muted-foreground">{empty}</p>;
  return (
    <dl className="grid gap-x-4 gap-y-2 text-sm sm:grid-cols-[max-content_minmax(0,1fr)]">
      {entries.map(([name, spec]) => [
        <dt key={`${name}-k`} className="font-mono text-xs font-medium sm:pt-px">{name}</dt>,
        <dd key={`${name}-v`} className="min-w-0 pb-1 sm:pb-0"><SpecFields spec={spec} /></dd>,
      ])}
    </dl>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return <section className="space-y-2"><h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{title}</h3>{children}</section>;
}

// Identity and data model

function Identity({ d }: { d: CollectionDetail }) {
  const r = d.release;
  return (
    <Panel title="Identity" padded description="Who owns the collection and which immutable release you are looking at. A release pins the templates, queries, manifest and every dataset's files.">
      <KeyValues rows={[
        ["Description", d.description || <span className="text-muted-foreground">None given</span>],
        ["Key", <Mono key="k">{d.key}</Mono>],
        ["Owners", d.owners.length ? d.owners.join(", ") : "—"],
        ["Methods", d.methods.length ? <span key="m" className="inline-flex flex-wrap gap-1">{d.methods.map(m => <Status key={m}>{m}</Status>)}</span> : "—"],
        ["DAPPER profile", d.dapperProfile ? <Mono key="p">{d.dapperProfile}</Mono> : "—"],
        ["Release", <span key="v" className="inline-flex flex-wrap items-center gap-2"><Mono>{r.version}</Mono>{r.isCurrent ? <Status tone="good">current</Status> : <Status tone="warn">not current</Status>}</span>],
        ["Release ID", <span key="i" className="inline-flex flex-wrap items-center gap-2"><Mono>{r.id}</Mono><CopyButton text={r.id} /></span>],
        ["Git commit", r.gitCommit ? <span key="g" className="inline-flex flex-wrap items-center gap-2"><Mono>{r.gitCommit.slice(0, 12)}</Mono><CopyButton text={r.gitCommit} /></span> : "—"],
        ["Published", r.publishedAt ? <span key="t">{new Date(r.publishedAt).toLocaleString()}</span> : "—"],
        ["Releases", number(d.releases)],
        ["DAPPER record", d.dapperDocumentUrl ? <a key="u" href={d.dapperDocumentUrl} target="_blank" rel="noopener noreferrer" className="break-all underline-offset-4 hover:underline">Open the ResultCollection document</a> : "—"],
      ]} />
    </Panel>
  );
}

function DataModel({ d }: { d: CollectionDetail }) {
  return (
    <Panel title="Data model" padded description="How the manifest reads result files. Dimensions place each dataset; entities turn file values into identifiers; file roles say what each file yields: rows (a subject, an object and metrics) or edges between entities.">
      <div className="space-y-5">
        <Section title="Dimensions">
          {d.dimensions.length ? <dl className="grid gap-x-4 gap-y-1.5 text-sm sm:grid-cols-[max-content_minmax(0,1fr)]">
            {d.dimensions.map(dim => [
              <dt key={`${dim.name}-k`} className="font-mono text-xs font-medium sm:pt-px">{dim.name}</dt>,
              <dd key={`${dim.name}-v`} className="min-w-0 text-xs text-muted-foreground">
                {dim.resolve ? <>Resolved through <span className="font-mono text-foreground">{dim.resolve}</span>. </> : "Stored as given. "}
                {dim.description}
              </dd>,
            ])}
          </dl> : <p className="text-sm text-muted-foreground">No dimensions: the release has a single dataset.</p>}
        </Section>
        <Section title="Entities"><SpecTable specs={d.entities} empty="No entities declared." /></Section>
        <Section title="Row kinds"><SpecTable specs={d.rowKinds} empty="No row kinds declared." /></Section>
        <Section title="Edge kinds"><SpecTable specs={d.edgeKinds} empty="No edge kinds declared." /></Section>
        <Section title="File roles"><SpecTable specs={d.fileRoles} empty="No file roles declared." /></Section>
      </div>
    </Panel>
  );
}

// Datasets

const fileColumns: ColumnDef<DatasetFile, unknown>[] = [
  { id: "role", accessorKey: "role", header: "Role", cell: ({ row }) => <Mono>{row.original.role}</Mono> },
  { id: "filename", accessorKey: "filename", header: "File", cell: ({ row }) => <div className="min-w-40">
    <div className="break-all">{row.original.filename ?? <span className="text-muted-foreground">Unnamed</span>}</div>
    <div className="mt-0.5 break-all font-mono text-[11px] text-muted-foreground">{row.original.fileId}</div>
  </div> },
  { id: "size", accessorFn: r => r.sizeBytes ?? -1, header: "Size", meta: { align: "right" }, cell: ({ row }) => bytes(row.original.sizeBytes) },
  { id: "sha256", accessorKey: "sha256", header: "sha256", enableSorting: false, cell: ({ row }) => <Hash value={row.original.sha256} /> },
  { id: "location", accessorKey: "location", header: "Location", enableSorting: false, cell: ({ row }) => row.original.location
    ? <span className="block min-w-48 break-all font-mono text-[11px] text-muted-foreground">{row.original.location}</span> : <span className="text-muted-foreground">—</span> },
];

function Dataset({ d, ds }: { d: CollectionDetail; ds: CollectionDataset }) {
  const kindLabel = (kind: string) => (kind in d.edgeKinds ? "edges" : "rows");
  return (
    <div className="space-y-3 px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <Dimensions value={ds.dimensions} className="text-sm" />
          <div className="break-all font-mono text-[11px] text-muted-foreground">{ds.id}</div>
        </div>
        <Button asChild variant="outline" size="sm"><Link href={claimsHref(d.key, { dataset: ds.id })}>View claims</Link></Button>
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-2 text-xs">
        {Object.entries(ds.counts).map(([kind, n]) => <span key={kind}><span className="tabular-nums font-medium">{number(n)}</span> <Mono className="text-muted-foreground">{kind}</Mono> {kindLabel(kind)}</span>)}
        <span className="flex min-w-56 items-center gap-2">
          <span className="whitespace-nowrap"><span className="tabular-nums font-medium">{number(ds.claims.minted)}</span> of {number(ds.claims.mintable)} mintable claims minted</span>
          <Coverage minted={ds.claims.minted} mintable={ds.claims.mintable} />
        </span>
      </div>
      <div className="rounded-lg border border-border/50">
        <DataTable label="Dataset files" data={ds.files} columns={fileColumns} getRowId={f => `${f.role}-${f.fileId}`} empty="This dataset lists no files." />
      </div>
    </div>
  );
}

// Claim templates

function TemplateKey({ d, claim, onOpen }: { d: CollectionDetail; claim: TemplateClaim; onOpen: (id: string) => void }) {
  const p = claim.proposition;
  return (
    <div className="space-y-2 py-4 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Link href={claimsHref(d.key, { claim_key: claim.key })} className="font-mono text-sm font-medium underline-offset-4 hover:underline">{claim.key}</Link>
          {p.kind && <Status>{p.kind}</Status>}
          {claim.classify && <Status tone="info">tiers: {claim.classify}</Status>}
        </div>
        <div className="flex flex-wrap gap-x-4 text-xs text-muted-foreground">
          <span><span className="font-medium tabular-nums text-foreground">{number(claim.mintable)}</span> mintable</span>
          <span><span className="font-medium tabular-nums text-foreground">{number(claim.minted)}</span> minted</span>
          <span><span className="font-medium tabular-nums text-foreground">{number(claim.assertions)}</span> assertions</span>
        </div>
      </div>
      <KeyValues rows={[
        ["Statement", <Pre key="s" className="max-h-40">{claim.statement}</Pre>],
        ["Proposition", <div key="p" className="space-y-1 text-xs">
          {p.statement && <div>{p.statement}</div>}
          <div className="break-all font-mono text-muted-foreground">{p.subject} <span>—{p.relation}→</span> {p.object}</div>
        </div>],
        ["Scores", claim.scores.length ? <span key="sc" className="inline-flex flex-wrap gap-1">{claim.scores.map(s => <Status key={s}>{s}</Status>)}</span> : "None"],
        ["Example", claim.example
          ? <button key="e" type="button" onClick={() => onOpen(claim.example!.claimId)} className="text-left text-xs underline-offset-4 hover:underline">{claim.example.statement ?? claim.example.claimId}</button>
          : <span key="e" className="text-xs text-muted-foreground">None minted yet in this release.</span>],
      ]} />
    </div>
  );
}

function Template({ d, t, onOpen }: { d: CollectionDetail; t: CollectionTemplate; onOpen: (id: string) => void }) {
  const edge = t.for in d.edgeKinds;
  return (
    <div className="px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3 pb-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2"><span className="font-mono text-sm font-semibold">{t.name}</span><span className="text-xs text-muted-foreground">v{t.version}</span></div>
          <p className="mt-1 text-xs text-muted-foreground">
            {edge
              ? <>Mints claims for each group of <Mono className="text-foreground">{t.for}</Mono> edges{t.groupBy ? <> grouped by <Mono className="text-foreground">{t.groupBy}</Mono></> : null}.</>
              : <>Mints claims from each <Mono className="text-foreground">{t.for}</Mono> row.</>}
            {" "}{plural(t.claims.length, "claim key")} per {edge ? "group" : "row"}.
          </p>
        </div>
        <div className="text-right text-[11px] text-muted-foreground"><div className="break-all font-mono">{t.path}</div><Hash value={t.sha256} /></div>
      </div>
      <div className="divide-y divide-border/50 rounded-lg border border-border/50 px-4 py-4">
        {t.claims.length ? t.claims.map(c => <TemplateKey key={c.key} d={d} claim={c} onOpen={onOpen} />) : <p className="text-sm text-muted-foreground">This template declares no claims.</p>}
      </div>
    </div>
  );
}

// Metrics and tier rules

const metricColumns: ColumnDef<MetricInfo, unknown>[] = [
  { id: "name", accessorKey: "name", header: "Metric", cell: ({ row }) => <Mono className="font-medium">{row.original.name}</Mono> },
  { id: "scoreKind", accessorKey: "scoreKind", header: "Score kind", cell: ({ row }) => <Mono>{row.original.scoreKind}</Mono> },
  { id: "scale", accessorKey: "scale", header: "Scale" },
  { id: "better", accessorKey: "better", header: "Better" },
  { id: "confirmed", accessorFn: r => (r.confirmed ? 1 : 0), header: "Status", cell: ({ row }) => row.original.confirmed ? <Status tone="good">confirmed</Status> : <Status tone="warn">provisional</Status> },
  { id: "interpretation", header: "Meaning", enableSorting: false, meta: { width: 380 }, cell: ({ row }) => <div className="min-w-64 space-y-1 text-xs">
    {row.original.description && <div>{row.original.description}</div>}
    {row.original.interpretation && <div className="text-muted-foreground">{row.original.interpretation}</div>}
  </div> },
];

type Tier = { label?: string; min?: number; max?: number; [k: string]: unknown };
function tierRange(t: Tier) {
  const parts = [t.min !== undefined ? `≥ ${t.min}` : null, t.max !== undefined ? `< ${t.max}` : null].filter(Boolean);
  return parts.length ? parts.join(" and ") : "otherwise";
}

function TierRule({ name, spec }: { name: string; spec: Spec }) {
  const { version, status, description, tiers, ...rest } = spec as { version?: string; status?: string; description?: string; tiers?: Tier[]; [k: string]: unknown };
  return (
    <div className="space-y-2 py-4 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-center gap-2">
        <Mono className="text-sm font-medium">{name}</Mono>
        {version && <span className="text-xs text-muted-foreground">v{String(version)}</span>}
        {status && <Status tone={status === "provisional" ? "warn" : "neutral"}>{String(status)}</Status>}
      </div>
      {description && <p className="text-xs text-muted-foreground">{String(description)}</p>}
      {Array.isArray(tiers) && <ol className="space-y-0.5 text-xs">
        {tiers.map((t, i) => <li key={i} className="flex gap-3"><span className="w-28 shrink-0 font-mono tabular-nums text-muted-foreground">{tierRange(t)}</span><span>{t.label}</span></li>)}
      </ol>}
      {Object.keys(rest).length > 0 && <SpecFields spec={rest} />}
    </div>
  );
}

// Queries

function ParamTable({ params }: { params: Record<string, Spec> }) {
  const entries = Object.entries(params ?? {});
  if (!entries.length) return <p className="text-sm text-muted-foreground">No parameters.</p>;
  return (
    <div className="overflow-x-auto rounded-lg border border-border/50">
      <table className="w-full text-left text-xs [&_td]:px-3 [&_td]:py-2 [&_td]:align-top [&_th]:px-3 [&_th]:py-2 [&_th]:font-medium [&_th]:text-muted-foreground [&_tr]:border-b [&_tr]:border-border/50 [&_tbody_tr:last-child]:border-0">
        <thead><tr><th>Name</th><th>Type</th><th>Default</th><th>Description</th></tr></thead>
        <tbody>{entries.map(([name, spec]) => {
          const { type, required, default: dflt, description, ...rest } = spec as Spec & { type?: string; required?: boolean; default?: unknown; description?: string };
          return <tr key={name}>
            <td className="font-mono font-medium">{name}{required ? <span className="ml-1 text-destructive" title="Required">*</span> : null}</td>
            <td className="font-mono">{type ? String(type) : "—"}</td>
            <td className="font-mono">{dflt === undefined ? <span className="text-muted-foreground">—</span> : asText(dflt)}</td>
            <td className="min-w-48">{description ? String(description) : null}{Object.keys(rest).length > 0 && <div className="mt-0.5 text-muted-foreground"><SpecFields spec={rest} /></div>}</td>
          </tr>;
        })}</tbody>
      </table>
    </div>
  );
}

function ExampleForm({ label, value }: { label: string; value: unknown }) {
  if (isEmpty(value)) return null;
  const text = asText(value);
  return <div className="space-y-1">
    <div className="flex items-center justify-between gap-2"><span className="text-xs font-medium">{label}</span><CopyButton text={text} /></div>
    <Pre className="max-h-64">{text}</Pre>
  </div>;
}

/** Reads a nested field of a run_query row without trusting its shape. */
const field = (value: unknown, ...path: string[]): unknown => path.reduce<unknown>((v, k) => (v && typeof v === "object" ? (v as Record<string, unknown>)[k] : undefined), value);
const str = (value: unknown) => (value === null || value === undefined ? null : String(value));

function RunRows({ run }: { run: QueryRun }) {
  if (!run.rows.length) return <p className="text-sm text-muted-foreground">The query returned no rows for its example variables.</p>;
  const joins = Array.from(new Set(run.rows.flatMap(r => Object.entries(r).filter(([, v]) => Array.isArray(v)).map(([k]) => k))));
  return (
    <div className="overflow-x-auto rounded-lg border border-border/50">
      <table className="w-full text-left text-xs [&_td]:px-3 [&_td]:py-2 [&_td]:align-top [&_th]:px-3 [&_th]:py-2 [&_th]:font-medium [&_th]:text-muted-foreground [&_tr]:border-b [&_tr]:border-border/50 [&_tbody_tr:last-child]:border-0">
        <thead><tr><th>#</th><th>Subject</th><th>Object</th><th>Dataset</th><th>Metrics</th>{joins.map(j => <th key={j} className="text-right">{j}</th>)}</tr></thead>
        <tbody>{run.rows.map((r, i) => {
          const subject = str(field(r, "subject", "label")) ?? str(field(r, "subject", "value")) ?? str(field(r, "subject", "id"));
          const metrics = field(r, "metrics");
          const dims = field(r, "dataset", "dimensions");
          return <tr key={i}>
            <td className="tabular-nums text-muted-foreground">{i + 1}</td>
            <td><div className="font-medium">{subject ?? "—"}</div><div className="font-mono text-[11px] text-muted-foreground">{str(field(r, "kind"))}</div></td>
            <td>{str(field(r, "object", "label")) ?? str(field(r, "object", "id")) ?? "—"}</td>
            <td className="min-w-32">{dims && typeof dims === "object" ? <SpecFields spec={dims} /> : "—"}</td>
            <td className="min-w-40">{metrics && typeof metrics === "object" ? <SpecFields spec={metrics} /> : "—"}</td>
            {joins.map(j => <td key={j} className="text-right tabular-nums">{Array.isArray(r[j]) ? number((r[j] as unknown[]).length) : "—"}</td>)}
          </tr>;
        })}</tbody>
      </table>
    </div>
  );
}

function QueryRunner({ collection, release, name }: { collection: string; release: string; name: string }) {
  const query = useQuery({
    queryKey: ["admin", "query-example", collection, release, name],
    queryFn: () => adminGet<QueryRun>(`collections/${encodeURIComponent(collection)}/queries/${encodeURIComponent(name)}/example`, { release }),
    staleTime: 0,
  });
  const run = query.data;
  return (
    <div className="space-y-3">
      {!run ? <QueryFeedback pending={query.isPending} error={query.error} onRetry={() => void query.refetch()} /> : <>
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
          <span>{plural(run.rows.length, "row")} from release <Mono>{run.release.version}</Mono></span>
          <Button variant="outline" size="sm" disabled={query.isFetching} onClick={() => void query.refetch()}>Run again</Button>
        </div>
        {run.warnings.map((w, i) => <Callout key={i} tone="warn" role="status">{w}</Callout>)}
        <RunRows run={run} />
        <details><summary className="cursor-pointer text-xs text-muted-foreground">Raw result</summary><div className="mt-2"><Json value={run} /></div></details>
      </>}
    </div>
  );
}

function Query({ d, q }: { d: CollectionDetail; q: CollectionQuery }) {
  const [running, setRunning] = useState(false);
  return (
    <div className="space-y-4 px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="font-mono text-sm font-semibold">{q.name}</div>
          {q.description && <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{q.description}</p>}
        </div>
        <div className="text-right text-[11px] text-muted-foreground"><div className="break-all font-mono">{q.path}</div><Hash value={q.sha256} /></div>
      </div>
      <Section title="Parameters"><ParamTable params={q.params} /></Section>
      <details><summary className="cursor-pointer text-xs font-medium uppercase tracking-wide text-muted-foreground">Plan</summary><div className="mt-2"><Json value={q.plan} /></div></details>
      {q.example ? <Section title="Example">
        <div className="grid gap-3 lg:grid-cols-2">
          <ExampleForm label="Variables" value={q.example.variables} />
          <ExampleForm label="REST request" value={q.example.rest} />
          <ExampleForm label="REST body" value={q.example.body} />
          <ExampleForm label="GraphQL" value={q.example.graphql} />
        </div>
        <div className="space-y-3 rounded-lg border border-border/50 p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="max-w-2xl text-xs text-muted-foreground">Run example executes the query with these variables and returns rows only. It serves no claims, so nothing is minted or asserted.</p>
            {!running && <Button size="sm" variant="outline" onClick={() => setRunning(true)}>Run example</Button>}
          </div>
          {running && <QueryRunner collection={d.key} release={d.release.id} name={q.name} />}
        </div>
      </Section> : <p className="text-sm text-muted-foreground">This query has no example variables, so it cannot be run from here.</p>}
    </div>
  );
}

// Source files

const sourceColumns: ColumnDef<SourceFile, unknown>[] = [
  { id: "kind", accessorKey: "kind", header: "Kind", cell: ({ row }) => <Status>{row.original.kind}</Status> },
  { id: "name", accessorKey: "name", header: "Name", cell: ({ row }) => <div className="min-w-40"><Mono className="font-medium">{row.original.name}</Mono><div className="mt-0.5 break-all font-mono text-[11px] text-muted-foreground">{row.original.path}</div></div> },
  { id: "size", accessorFn: r => r.sizeBytes ?? -1, header: "Size", meta: { align: "right" }, cell: ({ row }) => bytes(row.original.sizeBytes) },
  { id: "sha256", accessorKey: "sha256", header: "sha256", enableSorting: false, cell: ({ row }) => <Hash value={row.original.sha256} /> },
];

// Page

function CollectionView() {
  const { key } = useParams<{ key: string }>();
    const { params, update } = useUrlState();
  const release = params.get("release") ?? "";
  const claim = params.get("claim");
  const query = useQuery({
    queryKey: ["admin", "collection", key, release],
    queryFn: () => adminGet<CollectionDetail>(`collections/${encodeURIComponent(key)}`, { release }),
  });
  const d = query.data;
  const open = (id: string) => update({ claim: id });
  const picker = d && d.releaseHistory.length > 0 && (
    <label className="flex items-center gap-2 text-xs text-muted-foreground">Release
      <select aria-label="Release" className={selectClass} value={d.release.version}
        onChange={e => { const r = d.releaseHistory.find(x => x.version === e.target.value); update({ release: r?.isCurrent ? undefined : e.target.value, claim: undefined }); }}>
        {d.releaseHistory.map(r => <option key={r.id} value={r.version}>{r.version}{r.isCurrent ? " (current)" : ""}</option>)}
      </select>
    </label>
  );
  return <>
    <div className="text-xs"><Link href="/admin/collections" className="text-muted-foreground underline-offset-4 hover:text-foreground hover:underline">Collections</Link></div>
    <PageHeading title={d?.title ?? key}
      subtitle={d ? <span className="inline-flex flex-wrap items-center gap-x-2"><Mono>{d.key}</Mono>· release {d.release.version} · {plural(d.datasets.length, "dataset")} · {number(d.claims.mintable)} mintable claims, {number(d.claims.minted)} minted</span> : undefined}
      actions={d && <>{picker}<Button asChild size="sm" variant="outline"><Link href={claimsHref(d.key)}>View claims</Link></Button></>} />
    {!d ? <QueryFeedback pending={query.isPending} error={query.error} onRetry={() => void query.refetch()} /> : <>
      {!d.release.isCurrent && <Callout tone="warn" role="status">
        You are viewing release {d.release.version}, which is not current. Queries default to the current release{d.currentRelease ? ` (${d.currentRelease.version})` : ""}.{" "}
        <button type="button" className="underline underline-offset-4" onClick={() => update({ release: undefined, claim: undefined })}>Show the current release</button>
      </Callout>}
      <Identity d={d} />
      <DataModel d={d} />
      <Panel title="Datasets" description={`Each dataset is one slice of the release along the collection's dimensions${d.dimensions.length ? ` (${d.dimensions.map(x => x.name).join(", ")})` : ""}. Its files are pinned by sha256; location is where the bytes live upstream.`}>
        {d.datasets.length ? <div className="divide-y divide-border/50">{d.datasets.map(ds => <Dataset key={ds.id} d={d} ds={ds} />)}</div>
          : <p className="px-5 py-4 text-sm text-muted-foreground">This release has no datasets.</p>}
      </Panel>
      <Panel title="Claim templates" description="Each template turns one row (or one group of edges) into claims. Placeholders in braces are filled from the row, its dataset and the collection. Counts are for this release; an example is the most recently asserted claim for that key.">
        {d.templates.length ? <div className="divide-y divide-border/50">{d.templates.map(t => <Template key={t.name} d={d} t={t} onOpen={open} />)}</div>
          : <p className="px-5 py-4 text-sm text-muted-foreground">This release has no claim templates.</p>}
      </Panel>
      <Panel title="Metrics" description="What each score means. Provisional interpretations are marked as such in the claims until the method's owners confirm them.">
        <DataTable label="Metrics" data={d.metrics} columns={metricColumns} getRowId={m => m.name} empty="This collection declares no metrics." />
      </Panel>
      <Panel title="Tier rules" padded description="Declared thresholds that classify a score into an evidence tier. Tiers are checked top-down and the first match wins.">
        {Object.keys(d.tierRules ?? {}).length
          ? <div className="divide-y divide-border/50">{Object.entries(d.tierRules).map(([name, spec]) => <TierRule key={name} name={name} spec={spec} />)}</div>
          : <p className="text-sm text-muted-foreground">This collection declares no tier rules.</p>}
      </Panel>
      <Panel title="Queries" description="Query templates clients run against this collection: parameters, the plan the executor follows, and ready-to-copy example requests.">
        {d.queries.length ? <div className="divide-y divide-border/50">{d.queries.map(q => <Query key={q.name} d={d} q={q} />)}</div>
          : <p className="px-5 py-4 text-sm text-muted-foreground">This collection declares no queries.</p>}
      </Panel>
      <Panel title="Source files" description="The manifest, templates and queries this release pinned, by sha256.">
        <DataTable label="Source files" data={d.sourceFiles} columns={sourceColumns} getRowId={f => `${f.kind}-${f.path}`} empty="No source files recorded." />
      </Panel>
      <Panel title="Manifest" padded description="The collection.yaml this release was published from, as stored.">
        <details><summary className="cursor-pointer text-sm text-muted-foreground">Show the raw manifest</summary><div className="mt-2"><Json value={d.manifest} /></div></details>
      </Panel>
    </>}
    <ClaimSheet claimId={claim} onClose={() => update({ claim: undefined })} />
  </>;
}

export default function CollectionPage() {
  return <Suspense fallback={<QueryFeedback pending error={null} />}><CollectionView /></Suspense>;
}
