// Browser-side calls to the admin API, always through the same-origin proxy
// (/api/admin/*), which adds the server's signed assertion.

export class AdminApiError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

type Params = Record<string, string | number | boolean | null | undefined>;

export async function adminGet<T>(path: string, params?: Params): Promise<T> {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const res = await fetch(`/api/admin/${path}${search.size ? `?${search}` : ""}`, { cache: "no-store", headers: { Accept: "application/json" } });
  const body = await res.json().catch(() => ({}));
  if (res.status === 401) { window.location.assign("/admin/signin"); }
  if (!res.ok) throw new AdminApiError(typeof body.error === "string" ? body.error : `${res.status} ${res.statusText}`, res.status);
  return body as T;
}

/** A write through the proxy (registration and deposit keys only). Same 401 redirect and errors as adminGet. */
export async function adminPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`/api/admin/${path}`, {
    method: "POST", cache: "no-store",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) { window.location.assign("/admin/signin"); }
  if (!res.ok) throw new AdminApiError(typeof data.error === "string" ? data.error : `${res.status} ${res.statusText}`, res.status);
  return data as T;
}

export type Release = { id: string; version: string; publishedAt: string | null; gitCommit: string | null };
export type ReleaseRef = Release & { isCurrent: boolean };
/** A dimension value on a dataset: the stored value and its label (null when the registry has none). */
export type DimensionValues = Record<string, { value: string; label: string | null }>;

export type CollectionSummary = {
  key: string; title: string; description: string | null; methods: string[]; owners: string[];
  currentRelease: Release | null; releases: number; datasets: number;
  claims: { mintable: number; minted: number }; updatedAt: string | null;
};

export type TraitRegistry = { version: string; commit: string | null; published: boolean; traits: number; mappings: number; loadedAt: string | null; rows: number };

export type Overview = {
  asOf: string;
  database: { backend: "sqlite" | "mysql"; name: string; pingMs: number };
  claims: { mintable: number; minted: number; uncollected: number; assertions: number; lastAssertedAt: string | null; minted24h: number; asserted24h: number };
  collections: CollectionSummary[];
  objectsByClass: { className: string; count: number }[];
  mintTimeline: { date: string; minted: number }[];
  traitRegistry: TraitRegistry | null;
  api: { startedAt: string; uptimeSeconds: number; requests: number; errors5xx: number; graphql: { requests: number; ok: number; failed: number; minted: number; reasserted: number } };
  registration: { enabled: boolean; roots: string[] };
};

export type Spec = Record<string, unknown>;

export type DatasetFile = { role: string; fileId: string; filename: string | null; sha256: string | null; sizeBytes: number | null; location: string | null };
export type CollectionDataset = {
  id: string; dimensions: DimensionValues; files: DatasetFile[];
  counts: Record<string, number>; claims: { mintable: number; minted: number };
};
export type TemplateClaim = {
  key: string; statement: string;
  proposition: { statement?: string; subject?: string; relation?: string; object?: string; kind?: string };
  classify: string | null; scores: string[];
  mintable: number; minted: number; assertions: number;
  example: { claimId: string; statement: string | null } | null;
};
export type CollectionTemplate = {
  name: string; version: string; for: string; groupBy: string | null; fileId: string; path: string; sha256: string;
  claims: TemplateClaim[];
};
export type MetricInfo = { name: string; scoreKind: string; scale: string; better: string; confirmed: boolean; description: string | null; interpretation: string | null };
export type QueryExample = { variables: Record<string, unknown>; rest: unknown; body: unknown; graphql: unknown };
export type CollectionQuery = {
  name: string; description: string | null; params: Record<string, Spec>; plan: unknown;
  example: QueryExample | null; fileId: string; path: string; sha256: string;
};
export type SourceFile = { kind: "manifest" | "template" | "query"; name: string; path: string; fileId?: string; sha256: string; sizeBytes: number | null };

export type CollectionDetail = CollectionSummary & {
  dapperProfile: string | null;
  release: ReleaseRef;
  releaseHistory: ReleaseRef[];
  dimensions: { name: string; resolve: string | null; description: string | null }[];
  entities: Record<string, Spec>;
  rowKinds: Record<string, Spec>; edgeKinds: Record<string, Spec>;
  fileRoles: Record<string, Spec>;
  metrics: MetricInfo[];
  tierRules: Record<string, Spec>;
  datasets: CollectionDataset[];
  templates: CollectionTemplate[];
  queries: CollectionQuery[];
  sourceFiles: SourceFile[];
  dapperDocumentUrl: string | null;
  manifest: unknown;
};

/** run_query's result object, returned unchanged by the read-only example endpoint. */
export type QueryRun = {
  collection: string; release: { id: string; version: string }; query: string;
  variables: Record<string, unknown>; rows: Record<string, unknown>[]; warnings: string[];
};

export type ClaimRow = {
  claimId: string; collection: string | null; claimKey: string | null; kind: string | null;
  subject: string | null; datasetId: string | null; dimensions: DimensionValues | null;
  minted: boolean; timesAsserted: number; mintedAt: string | null; lastAssertedAt: string | null;
};
export type ClaimList = { total: number; page: number; pageSize: number; rows: ClaimRow[] };

export type ScoreDoc = { id: string; missing?: boolean; metric?: string; score_kind?: string; value?: number; interpretation?: string };
export type EvidenceDoc = { id: string; missing?: boolean; name?: string; explanation?: string; [key: string]: unknown };
export type Triple = { subject: string; predicate: string; object: string };

export type ClaimSource = {
  collection: string | null; claimKey: string;
  template: { name: string; version: string; fileId: string } | null;
  releases: string[];
  dataset: { id: string; dimensions: DimensionValues };
  row: null | {
    rowId: number | string; kind: string;
    subject: { id: string; value: string; label: string | null };
    object: { id: string; label: string | null };
    columns: Record<string, unknown>; fileId: string;
  };
  group: null | { edgeKind: string; id: string; label: string | null; count: number };
};

export type ClaimDetail = {
  claimId: string; minted: boolean; identityProfile: string; timesAsserted: number;
  mintedAt: string | null; lastAssertedAt: string | null; method: string | null;
  collection: string | null; claimKey: string | null;
  triple: Triple | null; statement: string | null;
  verification: { collectionBacked: boolean; renders: boolean | null; storedMatches: boolean | null; error: string | null };
  claim: Record<string, unknown>; proposition: Record<string, unknown> | null;
  scores: ScoreDoc[]; evidence: EvidenceDoc[];
  source: ClaimSource | null;
  claimsAssessingProposition: number; resolverUrl: string;
};

// Registration (the Add collection flow).

export type RegistrationSources = {
  enabled: boolean; roots: string[];
  collections: { path: string; key: string | null; title: string | null; releases: { path: string; version: string | null }[] }[];
};

export type StepStatus = "ok" | "warning" | "error" | "skipped";
export type ReleaseState = "new-collection" | "new-version" | "already-published" | "conflict" | "unknown";

export type CheckResult = {
  ok: boolean;
  steps: { id: string; title: string; status: StepStatus; messages: string[] }[];
  collection: null | {
    key: string; title: string; description: string | null; methods: string[]; owners: string[]; dapperProfile: string | null;
    dimensions: unknown; entities: unknown; rowKinds: unknown; edgeKinds: unknown; fileRoles: unknown; metrics: unknown; tierRules: unknown;
    templates: { name: string; version: string; for: string; claims: string[] }[];
    queries: string[];
    sourceFiles: { kind: string; name: string; path: string; sha256: string; sizeBytes: number | null }[];
    mintingDigest: string | null;
  };
  git: null | { commit: string | null; dirty: boolean; codeVersion: string | null };
  release: null | { version: string; releaseId: string | null; state: ReleaseState; previousReleaseId: string | null; currentVersion: string | null };
  datasets: {
    index: number;
    dimensions: Record<string, { raw: string; value: string; label: string | null; resolved: boolean }>;
    datasetId: string | null; alreadyIngested: boolean;
    files: { role: string; path: string; exists: boolean; insideRoots: boolean; sizeBytes: number | null; sha256: string | null; location: string | null }[];
  }[];
  samples: { template: string; kind: string; dataset: number; subject: string; claims: { key: string; claimId: string; statement: string | null; name: string | null }[] }[];
  warnings: string[];
};

export type Job = {
  id: string; state: "running" | "succeeded" | "failed"; phase: string | null;
  collectionDir: string; releaseFile: string; requireClean: boolean; startedBy: string;
  startedAt: string; finishedAt: string | null; seconds: number;
  collection: string | null; version: string | null; releaseId: string | null;
  stats: Record<string, unknown> | null; warnings: string[]; error: string | null;
};

// Deposit keys (the Keys page). The list never carries a key or its hash; `key` exists only in the issue response.

export type DepositKey = {
  keyId: string; name: string; createdAt: string; createdBy: string | null;
  revokedAt: string | null; revokedBy: string | null; deposits: number; lastDepositAt: string | null;
};
export type DepositKeyList = { asOf: string; keys: DepositKey[] };
/** The 201 from POST keys: the only time the key leaves the service, which stores just its SHA-256. */
export type IssuedDepositKey = { keyId: string; name: string; key: string };
export type RevokedDepositKey = { keyId: string; revoked: true };

export type TableInfo = { name: string; available: boolean; rows?: number; countMs?: number; columns?: number; primaryKey?: string[]; dataBytes?: number | null; indexBytes?: number | null };
export type Tables = { asOf: string; backend: "sqlite" | "mysql"; tables: TableInfo[]; migrations: { version: string; appliedAt: string | null }[]; pendingMigrations: string[] };
export type Cell = string | number | boolean | null | { text: string; truncated: number };
export type TableRows = { table: string; columns: { name: string; type: string; pk: number }[]; orderBy: string[]; total: number; page: number; pageSize: number; rows: Record<string, Cell>[] };

export type RouteStats = { method: string; route: string; count: number; errors4xx: number; errors5xx: number; meanMs: number | null; p50Ms: number | null; p95Ms: number | null; samples: number; lastAt: string | null; lastStatus: number | null };
export type Activity = {
  asOf: string;
  telemetry: {
    startedAt: string; uptimeSeconds: number; sampleWindow: number; routes: RouteStats[];
    graphql: { requests: number; ok: number; failed: number; minted: number; reasserted: number; lastAt: string | null; rootFields: { field: string; count: number }[] };
  };
  recentAssertions: { claimId: string; timesAsserted: number; mintedAt: string | null; lastAssertedAt: string | null; triple: Triple | null }[];
  authEvents: { at: string; event: string; email: string | null; detail: string | null }[];
  approvedAdmins: string[];
};
