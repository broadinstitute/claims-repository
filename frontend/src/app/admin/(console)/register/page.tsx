"use client";

import Link from "next/link";
import { Suspense, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Check, CircleAlert, CircleDashed, CircleX, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { DataTable } from "@/components/admin/data-table";
import {
  Callout, CopyButton, Hash, Json, KeyValues, Mono, PageHeading, Panel, Pre, QueryFeedback, Status, When, bytes, number, plural, type Tone,
} from "@/components/admin/shared";
import { useUrlState } from "@/components/admin/use-url-state";
import { cn } from "@/lib/utils";
import { adminGet, adminPost, type CheckResult, type Job, type RegistrationSources, type StepStatus } from "@/lib/admin/api";

const selectClass = "h-9 max-w-full rounded-md border border-input bg-background px-3 text-sm disabled:cursor-not-allowed disabled:opacity-50";
const STATUS_TONE: Record<StepStatus, Tone> = { ok: "good", warning: "warn", error: "bad", skipped: "neutral" };
const JOB_TONE: Record<Job["state"], Tone> = { running: "info", succeeded: "good", failed: "bad" };
const basename = (path: string) => path.replace(/\/+$/, "").split("/").pop() || path;
const duration = (sec: number) => {
  const s = Math.max(0, Math.round(sec));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
};
const cliCommand = (dir: string, file: string) =>
  `.venv/bin/python scripts/publish_collection.py ${dir.trim() || "<collection dir>"} --release ${file.trim() || "<release file>"}`;
/** Names in a manifest block: a list's names (or items) or an object's keys. */
const names = (value: unknown): string[] => Array.isArray(value)
  ? value.map(v => (v && typeof v === "object" && "name" in v ? String((v as { name: unknown }).name) : String(v)))
  : value && typeof value === "object" ? Object.keys(value) : [];

// Step frame

type StepState = "todo" | "current" | "done" | "warn" | "error";
const STEP_ICON: Record<StepState, ReactNode> = {
  todo: <CircleDashed className="size-4 text-muted-foreground" aria-hidden />,
  current: <CircleDashed className="size-4 text-foreground" aria-hidden />,
  done: <Check className="size-4 text-emerald-600 dark:text-emerald-400" aria-hidden />,
  warn: <CircleAlert className="size-4 text-amber-600 dark:text-amber-400" aria-hidden />,
  error: <CircleX className="size-4 text-destructive" aria-hidden />,
};

function Step({ n, title, state, intro, children }: { n: number; title: string; state: StepState; intro: ReactNode; children?: ReactNode }) {
  return (
    <section aria-labelledby={`step-${n}`} className={cn("rounded-lg border border-border/50 bg-card", state === "todo" && "bg-transparent")}>
      <header className="flex items-start gap-3 px-5 py-4">
        <span className={cn("flex size-6 shrink-0 items-center justify-center rounded-full border text-xs font-semibold tabular-nums",
          state === "todo" ? "border-border text-muted-foreground" : "border-foreground/30")}>{n}</span>
        <div className="min-w-0 flex-1">
          <h2 id={`step-${n}`} className={cn("flex items-center gap-2 text-base font-semibold tracking-tight", state === "todo" && "text-muted-foreground")}>
            {title}{STEP_ICON[state]}
          </h2>
          <div className="mt-1 max-w-3xl text-xs leading-5 text-muted-foreground">{intro}</div>
        </div>
      </header>
      {children && <div className="space-y-4 border-t border-border/50 px-5 py-4">{children}</div>}
    </section>
  );
}

function SubHeading({ children }: { children: ReactNode }) {
  return <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{children}</h3>;
}

// Step 1: what registration needs, when it is off

function Requirements({ dir, file }: { dir: string; file: string }) {
  const command = cliCommand(dir, file);
  return (
    <div className="space-y-4 text-sm leading-6">
      <div className="space-y-1">
        <SubHeading>What registration needs</SubHeading>
        <p>The API process must be started with <Mono>CLAIMS_COLLECTION_ROOTS</Mono> set to one or more directories, separated by the system path separator (<Mono>:</Mono> on macOS and Linux). The console can only read a collection directory, a release file and result files that sit inside one of those roots. QA and production leave it unset, so they stay read-only.</p>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="space-y-1">
          <SubHeading>A collection directory</SubHeading>
          <ul className="list-disc space-y-1 pl-4 text-xs leading-5">
            <li><Mono>collection.yaml</Mono>: the manifest. The collection key (its short, permanent name, for example <Mono>pigean</Mono>), title, owners and methods; the dataset dimensions; how file values become identifiers (entities); the row and edge kinds each file role yields; metric meanings; tier rules; and the lists of templates and queries.</li>
            <li><Mono>templates/</Mono>: claim templates. Each one turns a row, or a group of edges, into claims.</li>
            <li><Mono>queries/</Mono>: query templates clients run against the collection.</li>
            <li><Mono>releases/</Mono>: one release file per published version.</li>
          </ul>
        </div>
        <div className="space-y-1">
          <SubHeading>A release file</SubHeading>
          <p className="text-xs leading-5">A version and its datasets. Each dataset gives its dimension values and, for each file role, a path relative to the release file and the location the bytes come from upstream.</p>
          <Pre>{`version: 2026.11.0
datasets:
  - dimensions: {trait: T2D, cohort: all}
    files:
      results:
        path: ../data/T2D/results.ndjson
        location: s3://bucket/T2D/results.ndjson`}</Pre>
        </div>
      </div>
      <div className="space-y-1">
        <div className="flex items-center justify-between gap-2"><SubHeading>The same publish from the command line</SubHeading><CopyButton text={command} /></div>
        <p className="text-xs text-muted-foreground">Run from the claims repository root on the API machine. It does the same checks and the same publish.</p>
        <Pre>{command}</Pre>
      </div>
    </div>
  );
}

function SourcePicker({ sources, dir, file, onPick }: { sources: RegistrationSources; dir: string; file: string; onPick: (dir: string, file: string) => void }) {
  if (!sources.collections.length) {
    return <p className="text-sm text-muted-foreground">No <Mono>collection.yaml</Mono> was found under the roots. Put a collection directory inside one of them, or type the paths below.</p>;
  }
  return (
    <ul className="divide-y divide-border/50 rounded-lg border border-border/50">
      {sources.collections.map(c => (
        <li key={c.path} className={cn("space-y-2 px-4 py-3", c.path === dir && "bg-muted/40")}>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2"><span className="font-medium">{c.title ?? c.key ?? basename(c.path)}</span>{c.key && <Mono className="text-muted-foreground">{c.key}</Mono>}</div>
            <div className="break-all font-mono text-[11px] text-muted-foreground">{c.path}</div>
          </div>
          {c.releases.length ? <div className="flex flex-wrap gap-2">
            {c.releases.map(r => {
              const selected = c.path === dir && r.path === file;
              return <Button key={r.path} size="sm" variant={selected ? "default" : "outline"} aria-pressed={selected} title={r.path} onClick={() => onPick(c.path, r.path)}>
                {r.version ?? basename(r.path)}
              </Button>;
            })}
          </div> : <p className="text-xs text-muted-foreground">No release files in its <Mono>releases/</Mono> directory.</p>}
        </li>
      ))}
    </ul>
  );
}

// Step 2: the check report

function ReleaseState({ release, collectionKey }: { release: NonNullable<CheckResult["release"]>; collectionKey: string | null }) {
  const v = <Mono>{release.version}</Mono>;
  const key = collectionKey ? <Mono>{collectionKey}</Mono> : "This collection";
  switch (release.state) {
    case "new-collection":
      return <Callout tone="info">New collection: {key} is not registered yet. Publishing registers it with release {v} as its first and current release.</Callout>;
    case "new-version":
      return <Callout tone="info">New version of an existing collection: publishing adds release {v} and makes it current{release.currentVersion ? <>, replacing <Mono>{release.currentVersion}</Mono> as the default</> : null}. Earlier releases stay readable.</Callout>;
    case "already-published":
      return <Callout tone="neutral">Already published: release {v} exists with exactly this content. Publishing again changes nothing.</Callout>;
    case "unknown":
      return <Callout tone="neutral">Release {v} already exists, but its content could not be compared with these files. Fix the dataset problems below, then check again.</Callout>;
    case "conflict":
      return <Callout tone="bad" role="alert">Conflict: release {v} already exists with different content. A version cannot be republished with new content. Choose a new version in the release file, then check again.</Callout>;
  }
}

type CheckDataset = CheckResult["datasets"][number];
type CheckFile = CheckDataset["files"][number];
const fileColumns: ColumnDef<CheckFile, unknown>[] = [
  { id: "role", accessorKey: "role", header: "Role", cell: ({ row }) => <Mono>{row.original.role}</Mono> },
  { id: "path", accessorKey: "path", header: "Path", cell: ({ row }) => <span className="block min-w-48 break-all font-mono text-[11px]">{row.original.path}</span> },
  { id: "state", header: "On disk", enableSorting: false, cell: ({ row }) => <div className="flex flex-wrap gap-1">
    {row.original.exists ? <Status tone="good">exists</Status> : <Status tone="bad">missing</Status>}
    {row.original.insideRoots ? null : <Status tone="bad">outside roots</Status>}
  </div> },
  { id: "size", accessorFn: r => r.sizeBytes ?? -1, header: "Size", meta: { align: "right" }, cell: ({ row }) => bytes(row.original.sizeBytes) },
  { id: "sha256", header: "sha256", enableSorting: false, cell: ({ row }) => <Hash value={row.original.sha256} /> },
  { id: "location", accessorKey: "location", header: "Location", enableSorting: false, cell: ({ row }) => row.original.location
    ? <span className="block min-w-40 break-all font-mono text-[11px] text-muted-foreground">{row.original.location}</span> : <span className="text-muted-foreground">—</span> },
];

function DatasetCheck({ ds }: { ds: CheckDataset }) {
  const dims = Object.entries(ds.dimensions);
  return (
    <div className="space-y-3 rounded-lg border border-border/50 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">Dataset {ds.index + 1}</span>
        {ds.alreadyIngested ? <Status>already ingested</Status> : <Status tone="info">new</Status>}
        {ds.datasetId && <span className="break-all font-mono text-[11px] text-muted-foreground">{ds.datasetId}</span>}
      </div>
      <p className="text-xs text-muted-foreground">{ds.alreadyIngested
        ? "Its files are already stored under this dataset id, so publishing reuses them."
        : "Publishing reads these files and stores their rows."}</p>
      {dims.length > 0 && <div className="overflow-x-auto">
        <table className="w-full text-left text-xs [&_td]:py-1.5 [&_td]:pr-4 [&_td]:align-top [&_th]:py-1.5 [&_th]:pr-4 [&_th]:font-medium [&_th]:text-muted-foreground">
          <thead><tr><th>Dimension</th><th>In the release file</th><th>Stored as</th><th>Label</th><th /></tr></thead>
          <tbody>{dims.map(([name, d]) => <tr key={name}>
            <td className="font-mono">{name}</td><td className="font-mono">{d.raw}</td><td className="break-all font-mono">{d.value}</td><td>{d.label ?? <span className="text-muted-foreground">—</span>}</td>
            <td>{d.resolved ? null : <Status tone="warn">unresolved</Status>}</td>
          </tr>)}</tbody>
        </table>
      </div>}
      {dims.some(([, d]) => !d.resolved) && <p className="text-xs text-amber-800 dark:text-amber-300">An unresolved value is not in the trait registry, so it is stored as written and claims will carry it unlabelled.</p>}
      <div className="rounded-lg border border-border/50"><DataTable label={`Dataset ${ds.index + 1} files`} data={ds.files} columns={fileColumns} getRowId={f => `${f.role}-${f.path}`} empty="This dataset lists no files." /></div>
    </div>
  );
}

function Chips({ items, empty = "None" }: { items: string[]; empty?: string }) {
  return items.length ? <span className="inline-flex flex-wrap gap-1">{items.map(i => <Status key={i}>{i}</Status>)}</span> : <span className="text-muted-foreground">{empty}</span>;
}

function CollectionSummary({ c }: { c: NonNullable<CheckResult["collection"]> }) {
  return (
    <div className="space-y-4">
      <KeyValues rows={[
        ["Key", <Mono key="k">{c.key}</Mono>],
        ["Title", c.title],
        ["Description", c.description || <span className="text-muted-foreground">None given</span>],
        ["Methods", <Chips key="m" items={c.methods} />],
        ["Owners", c.owners.length ? c.owners.join(", ") : "—"],
        ["DAPPER profile", c.dapperProfile ? <Mono key="p">{c.dapperProfile}</Mono> : "—"],
        ["Dimensions", <Chips key="d" items={names(c.dimensions)} empty="None: a single dataset" />],
        ["Entities", <Chips key="e" items={names(c.entities)} />],
        ["Row kinds", <Chips key="r" items={names(c.rowKinds)} />],
        ["Edge kinds", <Chips key="ed" items={names(c.edgeKinds)} />],
        ["File roles", <Chips key="f" items={names(c.fileRoles)} />],
        ["Metrics", <Chips key="mt" items={names(c.metrics)} />],
        ["Tier rules", <Chips key="t" items={names(c.tierRules)} />],
        ["Queries", <Chips key="q" items={c.queries} />],
        ["Minting digest", c.mintingDigest ? <Hash key="md" value={c.mintingDigest} /> : "—"],
      ]} />
      <div className="space-y-2">
        <SubHeading>Templates</SubHeading>
        {c.templates.length ? <ul className="space-y-1.5 text-sm">
          {c.templates.map(t => <li key={t.name} className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <Mono className="font-medium">{t.name}</Mono><span className="text-xs text-muted-foreground">v{t.version} · for <span className="font-mono">{t.for}</span> ·</span><Chips items={t.claims} />
          </li>)}
        </ul> : <p className="text-sm text-muted-foreground">No templates.</p>}
      </div>
      <div className="space-y-2">
        <SubHeading>Source files</SubHeading>
        <div className="rounded-lg border border-border/50">
          <DataTable label="Source files" data={c.sourceFiles} getRowId={f => `${f.kind}-${f.path}`} empty="No source files." columns={[
            { id: "kind", accessorKey: "kind", header: "Kind", cell: ({ row }) => <Status>{row.original.kind}</Status> },
            { id: "name", accessorKey: "name", header: "File", cell: ({ row }) => <div className="min-w-40"><Mono className="font-medium">{row.original.name}</Mono><div className="break-all font-mono text-[11px] text-muted-foreground">{row.original.path}</div></div> },
            { id: "size", accessorFn: r => r.sizeBytes ?? -1, header: "Size", meta: { align: "right" }, cell: ({ row }) => bytes(row.original.sizeBytes) },
            { id: "sha256", header: "sha256", enableSorting: false, cell: ({ row }) => <Hash value={row.original.sha256} /> },
          ]} />
        </div>
      </div>
      <details><summary className="cursor-pointer text-xs text-muted-foreground">Full definitions</summary>
        <div className="mt-2"><Json value={{ dimensions: c.dimensions, entities: c.entities, rowKinds: c.rowKinds, edgeKinds: c.edgeKinds, fileRoles: c.fileRoles, metrics: c.metrics, tierRules: c.tierRules }} /></div>
      </details>
    </div>
  );
}

function CheckReport({ r }: { r: CheckResult }) {
  return (
    <div className="space-y-5">
      {r.ok
        ? <Callout tone="good" role="status">The check passed{r.steps.some(s => s.status === "warning") ? " with warnings" : ""}. Nothing was written.</Callout>
        : <Callout tone="bad" role="alert">The check found problems, so this release cannot be published yet. Nothing was written. Fix the errors below, then check again.</Callout>}
      <ol className="space-y-2">
        {r.steps.map(s => <li key={s.id} className="rounded-lg border border-border/50 px-4 py-3">
          <div className="flex flex-wrap items-center gap-2"><span className="text-sm font-medium">{s.title}</span><Status tone={STATUS_TONE[s.status]}>{s.status}</Status></div>
          {s.messages.length > 0 && <div className="mt-2 space-y-2">{s.messages.map((m, i) => <Pre key={i} className={s.status === "error" ? "border-destructive/30" : undefined}>{m}</Pre>)}</div>}
        </li>)}
      </ol>
      {r.warnings.length > 0 && <div className="space-y-2"><SubHeading>Warnings</SubHeading>{r.warnings.map((w, i) => <Callout key={i} tone="warn">{w}</Callout>)}</div>}
      {r.release && <div className="space-y-2"><SubHeading>Release</SubHeading>
        <ReleaseState release={r.release} collectionKey={r.collection?.key ?? null} />
        <KeyValues rows={[
          ["Version", <Mono key="v">{r.release.version}</Mono>],
          ["Release ID", r.release.releaseId ? <span key="i" className="inline-flex flex-wrap items-center gap-2"><Mono>{r.release.releaseId}</Mono><CopyButton text={r.release.releaseId} /></span> : "Not computed"],
          ["Current version", r.release.currentVersion ? <Mono key="c">{r.release.currentVersion}</Mono> : "None"],
          ["Revision of", r.release.previousReleaseId ? <Mono key="p">{r.release.previousReleaseId}</Mono> : "Nothing (first release)"],
        ]} />
      </div>}
      {r.git && <div className="space-y-2"><SubHeading>Git</SubHeading>
        {r.git.dirty && <Callout tone="warn">The collection's git working tree has uncommitted changes. The release records the commit, so those edits would not be traceable to it. Commit first, or require a clean tree when you publish.</Callout>}
        <KeyValues rows={[
          ["Commit", r.git.commit ? <span key="c" className="inline-flex flex-wrap items-center gap-2"><Mono>{r.git.commit.slice(0, 12)}</Mono><CopyButton text={r.git.commit} /></span> : "Not a git checkout"],
          ["Working tree", r.git.dirty ? <Status key="d" tone="warn">uncommitted changes</Status> : <Status key="d" tone="good">clean</Status>],
          ["Code version", r.git.codeVersion ? <Mono key="v">{r.git.codeVersion}</Mono> : "—"],
        ]} />
      </div>}
      {r.collection && <div className="space-y-2"><SubHeading>Collection</SubHeading><CollectionSummary c={r.collection} /></div>}
      {r.datasets.length > 0 && <div className="space-y-2"><SubHeading>Datasets</SubHeading>{r.datasets.map(ds => <DatasetCheck key={ds.index} ds={ds} />)}</div>}
    </div>
  );
}

// Step 3: sample claims

function Samples({ r }: { r: CheckResult }) {
  const groups = useMemo(() => {
    const by = new Map<string, CheckResult["samples"]>();
    for (const s of r.samples) by.set(s.template, [...(by.get(s.template) ?? []), s]);
    return Array.from(by.entries());
  }, [r.samples]);
  if (!groups.length) return <p className="text-sm text-muted-foreground">No sample claims were rendered.</p>;
  const dsLabel = (i: number) => {
    const ds = r.datasets.find(d => d.index === i);
    const dims = ds ? Object.entries(ds.dimensions).map(([k, v]) => `${k} ${v.label ?? v.value}`).join(", ") : "";
    return `Dataset ${i + 1}${dims ? ` (${dims})` : ""}`;
  };
  return (
    <div className="space-y-4">
      {groups.map(([template, samples]) => <div key={template} className="space-y-2">
        <div className="flex flex-wrap items-center gap-2"><Mono className="text-sm font-semibold">{template}</Mono><span className="text-xs text-muted-foreground">for {samples[0].kind} · {plural(samples.length, "sample")}</span></div>
        <div className="divide-y divide-border/50 rounded-lg border border-border/50">
          {samples.map((s, i) => <div key={i} className="space-y-2 px-4 py-3">
            <div className="text-xs"><span className="font-medium">{s.subject}</span> <span className="text-muted-foreground">· {dsLabel(s.dataset)}</span></div>
            <ul className="space-y-2">
              {s.claims.map(c => <li key={c.claimId} className="grid gap-1 text-sm sm:grid-cols-[minmax(7rem,max-content)_minmax(0,1fr)] sm:gap-x-4">
                <Mono className="font-medium sm:pt-0.5">{c.key}</Mono>
                <div className="min-w-0"><div>{c.statement}</div><div className="mt-0.5 break-all font-mono text-[11px] text-muted-foreground">{c.claimId}</div></div>
              </li>)}
            </ul>
          </div>)}
        </div>
      </div>)}
    </div>
  );
}

// Step 4: the publish job

function JobView({ id, onClose }: { id: string; onClose: () => void }) {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["admin", "registration", "job", id],
    queryFn: () => adminGet<Job>(`registration/jobs/${encodeURIComponent(id)}`),
    refetchInterval: q => (q.state.data ? (q.state.data.state !== "running" ? false : 2000) : q.state.status === "error" ? false : 2000),
    staleTime: 0,
  });
  const job = query.data;
  const settled = useRef<string | null>(null);
  useEffect(() => {
    if (!job || job.state === "running" || settled.current === job.id) return;
    settled.current = job.id;
    void client.invalidateQueries({ queryKey: ["admin", "registration", "jobs"] });
    if (job.state === "succeeded") {
      for (const key of ["overview", "collections", "collection", "claims", "claim", "query-example"]) void client.invalidateQueries({ queryKey: ["admin", key] });
    }
  }, [job, client]);
  if (!job) {
    return <div className="space-y-3">
      <QueryFeedback pending={query.isPending} error={query.error} onRetry={() => void query.refetch()} />
      {query.error && <p className="text-xs text-muted-foreground">Jobs live in the API process's memory, so a job from before an API restart is gone. Check the collection page to see whether its release was published.</p>}
      <Button size="sm" variant="outline" onClick={onClose}>Back to the flow</Button>
    </div>;
  }
  const key = job.collection;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Status tone={JOB_TONE[job.state]}>{job.state}</Status>
        {job.state === "running" && <span className="inline-flex items-center gap-2 text-sm"><Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden />{job.phase ?? "Starting"}</span>}
        <span className="text-xs tabular-nums text-muted-foreground">{job.state === "running" ? "Running for" : "Took"} {duration(job.seconds)}</span>
      </div>
      <KeyValues rows={[
        ["Collection", key ? <span key="c" className="inline-flex flex-wrap items-center gap-2"><Mono>{key}</Mono>{job.version && <span className="text-xs text-muted-foreground">release {job.version}</span>}</span> : "Not known yet"],
        ["Release ID", job.releaseId ? <span key="r" className="inline-flex flex-wrap items-center gap-2"><Mono>{job.releaseId}</Mono><CopyButton text={job.releaseId} /></span> : "—"],
        ["Collection directory", <Mono key="d">{job.collectionDir}</Mono>],
        ["Release file", <Mono key="f">{job.releaseFile}</Mono>],
        ["Clean git tree required", job.requireClean ? "Yes" : "No"],
        ["Started", <span key="s">{new Date(job.startedAt).toLocaleString()} by {job.startedBy}</span>],
        ["Finished", job.finishedAt ? new Date(job.finishedAt).toLocaleString() : "—"],
      ]} />
      {job.state === "failed" && <div className="space-y-2"><Callout tone="bad" role="alert">The publish failed{job.phase ? ` while: ${job.phase}` : ""}. See the error below.</Callout>{job.error && <Pre>{job.error}</Pre>}</div>}
      {job.state === "succeeded" && <Callout tone="good" role="status">Published. Release {job.version ?? ""} of {key ?? "the collection"} is now current, and its claims can be served.</Callout>}
      {job.stats && Object.keys(job.stats).length > 0 && <div className="space-y-2"><SubHeading>What the publish did</SubHeading>
        <KeyValues rows={Object.entries(job.stats).map(([k, v]) => [k.replaceAll("_", " "), typeof v === "number" ? <span key={k} className="tabular-nums">{number(v)}</span> : <Mono key={k}>{typeof v === "object" ? JSON.stringify(v) : String(v)}</Mono>])} />
      </div>}
      {job.warnings.length > 0 && <div className="space-y-2"><SubHeading>Warnings</SubHeading>{job.warnings.map((w, i) => <Callout key={i} tone="warn">{w}</Callout>)}</div>}
      <div className="flex flex-wrap gap-2">
        {job.state === "succeeded" && key && <>
          <Button asChild size="sm"><Link href={`/admin/collections/${encodeURIComponent(key)}`}>Open the collection</Link></Button>
          <Button asChild size="sm" variant="outline"><Link href={`/admin/claims?collection=${encodeURIComponent(key)}`}>View its claims</Link></Button>
        </>}
        {job.state !== "running" && <Button size="sm" variant="outline" onClick={onClose}>Start another</Button>}
      </div>
    </div>
  );
}

const jobColumns: ColumnDef<Job, unknown>[] = [
  { id: "state", accessorKey: "state", header: "State", cell: ({ row }) => <Status tone={JOB_TONE[row.original.state]}>{row.original.state}</Status> },
  { id: "collection", header: "Collection", enableSorting: false, cell: ({ row }) => <div>
    <div className="font-mono text-xs">{row.original.collection ?? basename(row.original.collectionDir)}{row.original.version ? ` ${row.original.version}` : ""}</div>
    <div className="mt-0.5 break-all font-mono text-[11px] text-muted-foreground">{basename(row.original.releaseFile)}</div>
  </div> },
  { id: "startedBy", accessorKey: "startedBy", header: "Started by", meta: { muted: true } },
  { id: "startedAt", accessorKey: "startedAt", header: "Started", meta: { muted: true }, cell: ({ row }) => <When value={row.original.startedAt} /> },
  { id: "seconds", accessorKey: "seconds", header: "Duration", meta: { align: "right", muted: true }, cell: ({ row }) => duration(row.original.seconds) },
];

// Page

type Checked = { dir: string; file: string; result: CheckResult };

function AddCollection() {
  const { params, update } = useUrlState();
  const client = useQueryClient();
  const jobId = params.get("job");
  const [dir, setDir] = useState(params.get("dir") ?? "");
  const [file, setFile] = useState(params.get("file") ?? "");
  const [sample, setSample] = useState(3);
  const [requireClean, setRequireClean] = useState(false);
  const [checked, setChecked] = useState<Checked | null>(null);

  const sources = useQuery({ queryKey: ["admin", "registration", "sources"], queryFn: () => adminGet<RegistrationSources>("registration/sources") });
  const enabled = sources.data?.enabled === true;
  const jobs = useQuery({
    queryKey: ["admin", "registration", "jobs"], enabled,
    queryFn: () => adminGet<{ jobs: Job[] }>("registration/jobs"),
    refetchInterval: q => (q.state.data?.jobs.some(j => j.state === "running") ? 5000 : false),
  });

  const check = useMutation({
    mutationFn: (body: { collectionDir: string; releaseFile: string; sample: number }) => adminPost<CheckResult>("registration/check", body),
    onSuccess: (result, body) => setChecked({ dir: body.collectionDir, file: body.releaseFile, result }),
    onMutate: () => setChecked(null),
  });
  const publish = useMutation({
    mutationFn: (body: { collectionDir: string; releaseFile: string; requireClean: boolean }) => adminPost<Job>("registration/jobs", body),
    onSuccess: job => { void client.invalidateQueries({ queryKey: ["admin", "registration", "jobs"] }); update({ job: job.id }); },
  });

  const pick = (d: string, f: string) => { setDir(d); setFile(f); if (check.isError) check.reset(); update({ dir: d, file: f, job: undefined }, { push: false }); };
  const runCheck = () => {
    const body = { collectionDir: dir.trim(), releaseFile: file.trim(), sample };
    update({ dir: body.collectionDir, file: body.releaseFile, job: undefined }, { push: false });
    publish.reset();
    check.mutate(body);
  };
  const stale = !!checked && (checked.dir !== dir.trim() || checked.file !== file.trim());
  const result = checked && !stale ? checked.result : null;
  const canCheck = enabled && !!dir.trim() && !!file.trim() && !check.isPending;
  const canPublish = !!result?.ok && !publish.isPending && !jobId;

  const s1: StepState = result || check.isPending ? "done" : "current";
  const s2: StepState = check.isPending ? "current" : check.isError ? "error" : !result ? "todo" : !result.ok ? "error" : result.steps.some(s => s.status === "warning") ? "warn" : "done";
  const s3: StepState = !result ? "todo" : result.samples.length ? "done" : result.ok ? "warn" : "todo";
  const s4: StepState = jobId ? "current" : result?.ok ? "current" : "todo";

  return <>
    <div className="text-xs"><Link href="/admin/collections" className="text-muted-foreground underline-offset-4 hover:text-foreground hover:underline">Collections</Link></div>
    <PageHeading title="Add a collection"
      subtitle="Registering reads a collection directory and a release file from the API machine's disk, checks them, previews sample claims, then publishes the release in the background. Nothing is written until the last step." />

    <Step n={1} title="Choose the source" state={s1}
      intro={<>A collection directory holds the manifest (<Mono>collection.yaml</Mono>), claim templates and queries. A release file names one version and lists its datasets and result files. Both must be on the API machine, inside the roots it allows.</>}>
      {!sources.data ? <QueryFeedback pending={sources.isPending} error={sources.error} onRetry={() => void sources.refetch()} /> : <>
        {!enabled && <Callout tone="warn" role="status">Registration is not enabled on this deployment: the API was started without <Mono>CLAIMS_COLLECTION_ROOTS</Mono>. You can read how registration works below, but the actions are turned off.</Callout>}
        {enabled && <div className="space-y-2">
          <SubHeading>Collections found under the roots</SubHeading>
          <p className="text-xs text-muted-foreground">Roots: {sources.data.roots.map((r, i) => <span key={r}>{i > 0 && ", "}<Mono>{r}</Mono></span>)}. Pick a release to fill in both paths.</p>
          <SourcePicker sources={sources.data} dir={dir.trim()} file={file.trim()} onPick={pick} />
        </div>}
        <form className="space-y-3" onSubmit={e => { e.preventDefault(); if (canCheck) runCheck(); }}>
          <SubHeading>{enabled ? "Or type the paths" : "Paths"}</SubHeading>
          <div className="grid gap-3 lg:grid-cols-2">
            <label className="space-y-1 text-xs"><span className="text-muted-foreground">Collection directory</span>
              <Input value={dir} onChange={e => { setDir(e.target.value); if (check.isError) check.reset(); }} disabled={!enabled} placeholder="/path/to/collections/example" className="font-mono" spellCheck={false} autoComplete="off" />
            </label>
            <label className="space-y-1 text-xs"><span className="text-muted-foreground">Release file</span>
              <Input value={file} onChange={e => { setFile(e.target.value); if (check.isError) check.reset(); }} disabled={!enabled} placeholder="/path/to/collections/example/releases/2026.11.0.yaml" className="font-mono" spellCheck={false} autoComplete="off" />
            </label>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2 text-xs text-muted-foreground">Sample rows per template and dataset
              <select aria-label="Sample rows per template and dataset" className={selectClass} value={sample} disabled={!enabled} onChange={e => setSample(Number(e.target.value))}>
                {[1, 3, 5, 10].map(n => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>
            <Button type="submit" size="sm" disabled={!canCheck}>{check.isPending ? <><Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden />Checking…</> : "Check"}</Button>
          </div>
        </form>
        {enabled
          ? <details><summary className="cursor-pointer text-xs text-muted-foreground">What goes in a collection directory and a release file, and the command-line equivalent</summary><div className="mt-3"><Requirements dir={dir} file={file} /></div></details>
          : <Requirements dir={dir} file={file} />}
      </>}
    </Step>

    <Step n={2} title="Check" state={s2}
      intro="The API loads the manifest and validates it, reads the git state, works out whether this version is new, resolves each dataset's dimensions, hashes every result file and renders a few sample claims. It writes nothing.">
      {check.isPending ? <div role="status" className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden />Checking. Hashing large result files can take a minute.</div>
        : check.isError ? <div className="space-y-2"><Callout tone="bad" role="alert">The check could not run.</Callout><Pre>{check.error.message}</Pre></div>
        : stale ? <Callout tone="warn" role="status">The paths changed since the last check. Check again to continue.</Callout>
        : result ? <CheckReport r={result} /> : null}
    </Step>

    <Step n={3} title="Preview sample claims" state={s3}
      intro="A few claims rendered exactly as a publish would record them, grouped by template. The claim ids are final: the same rows always produce the same ids. Nothing is stored yet.">
      {result ? <Samples r={result} /> : null}
    </Step>

    <Step n={4} title="Publish" state={s4}
      intro="Publishing ingests the result files, records the release and makes it the collection's current release. It runs in the background on the API.">
      {jobId ? <JobView id={jobId} onClose={() => { setChecked(null); update({ job: undefined }); }} />
        : !result ? (checked || check.isPending || !enabled ? null : <p className="text-sm text-muted-foreground">Run a check first. Publishing is only available once the check passes.</p>)
        : !result.ok ? <p className="text-sm text-muted-foreground">Publishing is blocked until the check passes.</p>
        : <div className="space-y-4">
            <div className="space-y-1 text-sm leading-6">
              <p>When you publish {result.collection ? <Mono>{result.collection.key}</Mono> : "this collection"} release {result.release ? <Mono>{result.release.version}</Mono> : null}:</p>
              <ul className="list-disc space-y-1 pl-5">
                <li>The new release becomes the current release immediately. Queries and claim serves use it by default.</li>
                <li>A version cannot be republished with different content. To change anything later, publish a new version.</li>
                <li>It can take a couple of minutes for a large dataset. You can leave this page; the job keeps running and its id stays in the address.</li>
                {result.release?.state === "already-published" && <li>This version is already published with the same content, so publishing changes nothing.</li>}
              </ul>
            </div>
            <label className="flex items-start gap-2 text-sm">
              <input type="checkbox" className="mt-1 size-4 accent-foreground" checked={requireClean} onChange={e => setRequireClean(e.target.checked)} />
              <span>Require a clean git tree<span className="block text-xs text-muted-foreground">Refuse to publish if the collection's directory has uncommitted changes, so the recorded commit describes exactly what was published.{result.git?.dirty ? " The tree has uncommitted changes now." : ""}</span></span>
            </label>
            {publish.isError && <div className="space-y-2"><Callout tone="bad" role="alert">{publish.error instanceof Error && "status" in publish.error && publish.error.status === 409 ? "Another publish is already running. Wait for it to finish, then try again." : "The publish could not start."}</Callout><Pre>{publish.error.message}</Pre></div>}
            <Button disabled={!canPublish} onClick={() => publish.mutate({ collectionDir: checked!.dir, releaseFile: checked!.file, requireClean })}>
              {publish.isPending ? <><Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden />Starting…</> : `Publish release ${result.release?.version ?? ""}`}
            </Button>
          </div>}
    </Step>

    {enabled && <Panel title="Recent publish jobs" description="The latest 20 jobs. They are kept in the API process's memory, so an API restart clears this list (published releases stay).">
      {!jobs.data ? <div className="px-4"><QueryFeedback pending={jobs.isPending} error={jobs.error} onRetry={() => void jobs.refetch()} /></div>
        : <DataTable label="Recent publish jobs" data={jobs.data.jobs} columns={jobColumns} getRowId={j => j.id} selectedId={jobId}
            onRowClick={j => update({ job: j.id })} rowLabel={j => `Open job ${j.id}`} empty="No publish jobs since the API started." />}
    </Panel>}
  </>;
}

export default function AddCollectionPage() {
  return <Suspense fallback={<QueryFeedback pending error={null} />}><AddCollection /></Suspense>;
}
