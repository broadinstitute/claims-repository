// Browser-side reads of the admin API, always through the same-origin proxy
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

export type Family = "gene" | "gene_set" | "roster" | "deposited";

export type FamilyStats = { family: Exclude<Family, "deposited">; table: string; implied: number; minted: number; assertions: number; lastAssertedAt: string | null };

export type Overview = {
  asOf: string;
  database: { backend: "sqlite" | "mysql"; name: string; pingMs: number };
  claims: { implied: number; minted: number; deposited: number; assertions: number; lastAssertedAt: string | null; minted24h: number; asserted24h: number };
  families: FamilyStats[];
  objectsByClass: { className: string; count: number }[];
  mintTimeline: { date: string; minted: number }[];
  phenotypes: { id: string; legacyId: string | null; name: string | null; geneRows: number; setRows: number; mintedGeneClaims: number }[];
  traitRegistry: null | { version: string; commit: string | null; published: boolean; traits: number; mappings: number; loadedAt: string | null; rows: number };
  api: { startedAt: string; uptimeSeconds: number; requests: number; errors5xx: number; graphql: { requests: number; ok: number; failed: number; minted: number; reasserted: number } };
};

export type ClaimRow = {
  claimId: string; family: Family; subject: string | null; phenotype: string | null; collection: string | null;
  minted: boolean; timesAsserted: number; mintedAt: string | null; lastAssertedAt: string | null;
};
export type ClaimList = { total: number; page: number; pageSize: number; rows: ClaimRow[] };

export type ScoreDoc = { id: string; missing?: boolean; metric?: string; score_kind?: string; value?: number; interpretation?: string };
export type EvidenceDoc = { id: string; missing?: boolean; name?: string; explanation?: string; [key: string]: unknown };
export type Triple = { subject: string; predicate: string; object: string };

export type ClaimDetail = {
  claimId: string; minted: boolean; identityProfile: string; timesAsserted: number;
  mintedAt: string | null; lastAssertedAt: string | null; method: string | null;
  triple: Triple | null; statement: string | null;
  verification: { templateBacked: boolean; synthesizes: boolean | null; storedMatches: boolean | null; error: string | null };
  claim: Record<string, unknown>; proposition: Record<string, unknown> | null;
  scores: ScoreDoc[]; evidence: EvidenceDoc[];
  indexRow: null | { family: Family; columns: Record<string, unknown>; synthesisInputs: Record<string, unknown> | null };
  claimsAssessingProposition: number; resolverUrl: string;
};

export type FileInfo = { path: string; sha256: string; lines: number; bytes: number };
export type Templates = {
  methods: {
    name: string; identityProfile: string; dapperProfile: string; contractVersion: string;
    template: FileInfo; graphqlModule: FileInfo;
    families: (FamilyStats & { relation: string; metrics: string[]; example: { claimId: string; statement: string | null } | null })[];
    metrics: { metric: string; scoreKind: string; scale: string; better: string; confirmed: boolean; description: string; claimInterpretation: string | null; families: string[]; kindAgrees: boolean }[];
  }[];
  coreSchema: FileInfo;
  queryTemplates: (FileInfo & { name: string; document: string; variables: Record<string, unknown> | null; hasFixture: boolean })[];
};

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
  authEvents: { at: string; event: "login" | "login_denied" | "logout"; email: string | null; detail: string | null }[];
  approvedAdmins: string[];
};
