// Client for the local-platform FastAPI surface.
//
// Every request goes to this Next server's own origin and is rewritten to the
// API (see next.config.js). Same-origin means no CORS preflight, which is why
// the API can keep its default-deny middleware without an OPTIONS exemption.
//
// Path note: the auth and health routers carry **no** `/api` prefix on the
// server — the real paths are `/auth/login`, `/auth/me`, `/health`. Only the
// remaining routers are mounted under `/api`. Getting this wrong produces a 404
// that looks like a missing feature, so the paths are spelled out per call
// rather than assembled from a base.

// ── error model ────────────────────────────────────────────────────────────

/** An API error that keeps its status, so callers can branch on *why*. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(status: number, message: string, code: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }

  /** The caller is authenticated but lacks the capability. */
  get isForbidden(): boolean {
    return this.status === 403;
  }

  /** The token is missing, expired or forged. */
  get isUnauthenticated(): boolean {
    return this.status === 401;
  }

  /** A budget ceiling refused the call. Deliberate, and retryable later. */
  get isBudgetExceeded(): boolean {
    return this.status === 429;
  }
}

/** Set by `request` on a 401 so the app can drop to the login screen. */
type UnauthenticatedHandler = () => void;
let onUnauthenticated: UnauthenticatedHandler | null = null;

export function setUnauthenticatedHandler(fn: UnauthenticatedHandler | null): void {
  onUnauthenticated = fn;
}

async function request<T>(
  path: string,
  init: RequestInit & { auth?: boolean } = {},
): Promise<T> {
  const { auth = true, headers, ...rest } = init;
  const merged: Record<string, string> = {
    "content-type": "application/json",
    ...((headers as Record<string, string>) || {}),
  };

  let res: Response;
  try {
    res = await fetch(path, {
      ...rest,
      headers: merged,
      // The browser session is an HttpOnly, SameSite=Strict cookie. Keeping it
      // out of localStorage prevents an XSS bug from becoming token theft.
      credentials: auth ? "same-origin" : "same-origin",
    });
  } catch (err) {
    // A network-level failure. Distinguished from an HTTP error because the
    // remedy is different: the API is unreachable rather than refusing.
    throw new ApiError(0, `Cannot reach the API. Is it running on :8100? (${String(err)})`);
  }

  if (res.status === 204) return undefined as T;

  const raw = await res.text();
  let body: any = null;
  if (raw) {
    try {
      body = JSON.parse(raw);
    } catch {
      body = { detail: raw };
    }
  }

  if (!res.ok) {
    // FastAPI puts the message in `detail`; validation errors put a list there.
    let detail = body?.detail ?? res.statusText;
    if (Array.isArray(detail)) {
      detail = detail
        .map((d: any) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`)
        .join("; ");
    }
    if (res.status === 401) {
      onUnauthenticated?.();
    }
    throw new ApiError(res.status, String(detail), body?.code ?? null);
  }

  return body as T;
}

// ── identity ───────────────────────────────────────────────────────────────

export type LoginResponse = {
  access_token: string | null;
  token_type: string;
  tenant: string;
  subject: string;
  roles: string[];
};

export type Me = {
  principal_id: string;
  tenant: string;
  subject: string;
  roles: string[];
  actor_type: string;
  capabilities: string[];
};

/** Capability strings, mirroring `platform_core.identity.capabilities`. */
export const Cap = {
  SESSION_READ: "session:read",
  QUERY_READ: "query:read",
  DOCUMENT_READ: "document:read",
  DOCUMENT_INGEST: "document:ingest",
  DOCUMENT_DELETE: "document:delete",
  RUN_READ: "run:read",
  RUN_CANCEL: "run:cancel",
  EVAL_READ: "eval:read",
  EVAL_RUN: "eval:run",
  USAGE_READ: "usage:read",
  AUDIT_READ: "audit:read",
  SCHEMA_READ: "schema:read",
  SCHEMA_AUTHOR: "schema:author",
  SCHEMA_APPROVE: "schema:approve",
  BUDGET_MANAGE: "budget:manage",
  MEMBER_MANAGE: "member:manage",
  RELEASE_PROMOTE: "release:promote",
  TOOL_INVOKE_READONLY: "tool:invoke:readonly",
  TOOL_INVOKE_WRITE: "tool:invoke:write",
  TOOL_APPROVE: "tool:approve",
} as const;

/**
 * Whether the console should *offer* an action.
 *
 * Presentation only. The server checks every call regardless, so a user who
 * forces a hidden control still gets a 403 — this exists to avoid showing
 * buttons that can only fail, not to enforce anything.
 */
export function can(me: Me | null, capability: string): boolean {
  return !!me && me.capabilities.includes(capability);
}

export function login(
  tenant: string,
  subject: string,
  password: string,
): Promise<LoginResponse> {
  return request<LoginResponse>("/auth/login", {
    method: "POST",
    auth: false,
    body: JSON.stringify({ tenant, subject, password, browser_session: true }),
  });
}

export function logout(): Promise<void> {
  return request<void>("/auth/logout", { method: "POST" });
}

export function fetchMe(): Promise<Me> {
  return request<Me>("/auth/me");
}

// ── chat ───────────────────────────────────────────────────────────────────

export type QueryMode = "dense" | "graph";

export type Source = {
  canonical_id: string;
  text: string;
  /** Present on dense results — cosine distance, lower is closer. */
  distance: number | null;
  /** Present on graph results — fused score, higher is better. */
  score: number | null;
  /**
   * Which retrievers put this chunk in context. Recorded at fusion time
   * because it is the first thing asked when a graph answer is wrong, and the
   * hardest thing to reconstruct afterwards.
   */
  signals: string[] | null;
};

export type GraphInfo = {
  nodes: number;
  edges: number;
  schema_domain: string;
  documents: number;
  build_ms: number;
  /**
   * An edgeless graph answers exactly like a populated one, with no error and
   * worse retrieval. The API surfaces it deliberately; the console must show
   * it just as deliberately.
   */
  edgeless: boolean;
};

export type QueryResponse = {
  answer: string;
  mode: string;
  sources: Source[];
  graph: GraphInfo | null;
  retrieval: Record<string, any> | null;
  session_id: string;
  grounded: boolean;
  cache_hit: boolean;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  latency_ms: number;
};

/**
 * The domain used when the caller names none.
 *
 * Exported rather than inlined because it is a real fallback with real
 * consequences: `build_graph` looks up published artifacts *by this name*, and a
 * domain that is not published resolves to no artifacts and an edgeless graph —
 * with no error. The console hardcoded "manufacturing" here while the only
 * published domain was something else, so graph mode silently never traversed.
 * The chat page now chooses from what is actually published; this remains only
 * for callers with nothing better to offer.
 */
export const DEFAULT_SCHEMA_DOMAIN = "manufacturing";

export function query(payload: {
  question: string;
  collection?: string;
  session_id?: string | null;
  mode?: QueryMode;
  schema_domain?: string;
}): Promise<QueryResponse> {
  return request<QueryResponse>("/api/query", {
    method: "POST",
    body: JSON.stringify({
      question: payload.question,
      collection: payload.collection || "maintenance",
      session_id: payload.session_id ?? null,
      mode: payload.mode || "dense",
      schema_domain: payload.schema_domain || DEFAULT_SCHEMA_DOMAIN,
    }),
  });
}

// ── usage and budgets ──────────────────────────────────────────────────────

export type Usage = {
  tenant: string;
  daily_token_cap: number;
  monthly_cost_cap_usd: number;
  /** Null means "not recorded yet", which is not the same as zero spend. */
  tokens_today: number | null;
  cost_this_month_usd: number | null;
  fail_closed: boolean;
};

export function fetchUsage(): Promise<Usage> {
  return request<Usage>("/api/usage");
}

export function updateCaps(payload: {
  daily_token_cap?: number | null;
  monthly_cost_cap_usd?: number | null;
}): Promise<{ tenant: string; updated: boolean }> {
  return request("/api/usage/caps", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

// ── members and grants ─────────────────────────────────────────────────────

export type Grant = {
  id: string;
  capability: string;
  resource: string;
  expires_at: string | null;
};

export type Member = {
  id: string;
  subject: string;
  roles: string[];
  actor_type: string;
  disabled: boolean;
  grants: Grant[];
};

export function fetchMembers(): Promise<{ members: Member[] }> {
  return request("/api/members");
}

export function createGrant(payload: {
  principal_id: string;
  capability: string;
  resource?: string;
  expires_at?: string | null;
}): Promise<{ id: string; principal_id: string; capability: string; resource: string }> {
  return request("/api/members/grants", {
    method: "POST",
    body: JSON.stringify({
      principal_id: payload.principal_id,
      capability: payload.capability,
      resource: payload.resource || "*",
      expires_at: payload.expires_at ?? null,
    }),
  });
}

// ── runs ───────────────────────────────────────────────────────────────────

export type Run = {
  id: string;
  workload: string;
  status: string;
  attempt: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  release: string | null;
};

export function fetchRuns(limit = 50): Promise<{ runs: Run[]; tenant: string }> {
  return request(`/api/runs?limit=${encodeURIComponent(String(limit))}`);
}

export function cancelRun(runId: string): Promise<{ id: string; status: string }> {
  return request(`/api/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
}

// ── documents ──────────────────────────────────────────────────────────────

export type PlatformDocument = {
  id: string;
  collection: string;
  filename: string;
  byte_size: number;
  content_sha256: string;
  created_at: string;
};

export function fetchDocuments(collection?: string): Promise<{ documents: PlatformDocument[] }> {
  const qs = collection ? `?collection=${encodeURIComponent(collection)}` : "";
  return request(`/api/documents${qs}`);
}

export function deleteDocument(id: string): Promise<{ id: string; deleted: boolean }> {
  return request(`/api/documents/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/**
 * Server-side allowlist, mirrored so the picker can filter before upload.
 *
 * Narrowed on 2026-08-14 to the formats the platform can actually extract text
 * from. It previously offered .pdf, .docx and .xlsx, which uploaded fine and
 * then contributed nothing retrievable — a file picker that accepts a document
 * the corpus will never contain is worse than one that refuses it.
 */
export const ALLOWED_SUFFIXES = [".txt", ".md", ".html", ".csv"];
export const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

/**
 * Upload a file as base64.
 *
 * `idempotency-key` is sent because the API dedupes on content hash within a
 * collection: a double-clicked upload must not fan out into two rebuilds
 * racing on the same collection.
 */
export async function uploadDocument(
  collection: string,
  file: File,
): Promise<PlatformDocument> {
  const buffer = await file.arrayBuffer();
  const bytes = new Uint8Array(buffer);

  // Chunked rather than `String.fromCharCode(...bytes)`, which overflows the
  // argument limit and throws on files of a few hundred KB.
  let binary = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }

  return request<PlatformDocument>("/api/documents", {
    method: "POST",
    headers: { "idempotency-key": `upload-${collection}-${file.name}-${file.size}` },
    body: JSON.stringify({
      collection,
      filename: file.name,
      content_base64: btoa(binary),
    }),
  });
}

// ── approvals ──────────────────────────────────────────────────────────────

export type Approval = {
  id: string;
  run_id: string;
  tool_name: string;
  side_effect: string;
  arguments: Record<string, any>;
  status: string;
  requested_by: string;
  requested_at: string;
  expires_at: string;
  /** True when the viewer raised this request; self-approval is refused. */
  is_own_request: boolean;
};

export function fetchApprovals(status = "pending"): Promise<{ approvals: Approval[] }> {
  return request(`/api/approvals?status=${encodeURIComponent(status)}`);
}

export function decideApproval(
  id: string,
  approved: boolean,
  note?: string,
): Promise<{ id: string; status: string }> {
  return request(`/api/approvals/${encodeURIComponent(id)}/decide`, {
    method: "POST",
    body: JSON.stringify({ approved, note: note || null }),
  });
}

// ── health ─────────────────────────────────────────────────────────────────

export function fetchHealth(): Promise<Record<string, any>> {
  return request("/health", { auth: false });
}

export function fetchReadiness(): Promise<Record<string, any>> {
  return request("/health/ready", { auth: false });
}

// ── onboarding ─────────────────────────────────────────────────────────────

export type OnboardingStatus =
  | "drafting"
  | "draft_ready"
  | "approved"
  | "published"
  | "failed"
  | "cancelled";

export type ProgressEntry = {
  step: string;
  detail?: string;
  index?: number;
  total?: number;
};

export type OnboardingStats = {
  /** Questions proposed from the corpus. 0 means none could be, not none needed. */
  candidate_queries?: number;
  candidate_queries_error?: string | null;
  instances?: number;
  predicates?: number;
  cache_files?: number;
  /** The one number that decides whether this domain traverses or only matches. */
  relations_available?: boolean;
  chunks_sampled?: number;
  entities_discovered?: number;
  repair_attempts?: number;
  validation?: Record<string, any>;
};

export type OnboardingSession = {
  id: string;
  domain: string;
  collection: string;
  status: OnboardingStatus;
  progress: ProgressEntry[];
  stats: OnboardingStats;
  error: string | null;
  run_id: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  approved_by: string | null;
  approved_at: string | null;
  published_at: string | null;
  /** Set once a human has rewritten the drafted taxonomy. */
  schema_edited_by?: string | null;
  schema_edited_at?: string | null;
  // Present only on the detail route.
  artifact_counts?: Record<string, number>;
  schema_yaml?: string | null;
  predicate_map?: Record<string, any> | null;
  /** What the stored taxonomy declares. Absent if it no longer parses. */
  schema?: SchemaReport;
  schema_error?: string;
  /**
   * Whether the taxonomy covers the entities drafted from the corpus. An entity
   * it does not declare is typed `raw:` and every relation touching one is
   * discarded when the graph is built — after approval, silently.
   */
  taxonomy_fit?: TaxonomyFit;
};

export function fetchOnboardingSessions(
  domain?: string,
): Promise<{ sessions: OnboardingSession[] }> {
  const qs = domain ? `?domain=${encodeURIComponent(domain)}` : "";
  return request(`/api/onboard/sessions${qs}`);
}

export function fetchOnboardingSession(id: string): Promise<OnboardingSession> {
  return request(`/api/onboard/sessions/${encodeURIComponent(id)}`);
}

export function startOnboarding(payload: {
  domain: string;
  collection: string;
  sample?: number;
}): Promise<{ id: string; domain: string; status: string; run_id: string }> {
  return request("/api/onboard/sessions", {
    method: "POST",
    body: JSON.stringify({
      domain: payload.domain,
      collection: payload.collection,
      sample: payload.sample ?? 120,
    }),
  });
}

export function approveOnboarding(id: string): Promise<{ id: string; status: string }> {
  return request(`/api/onboard/sessions/${encodeURIComponent(id)}/approve`, {
    method: "POST",
  });
}

export function publishOnboarding(
  id: string,
): Promise<{ id: string; domain: string; status: string; graphs_invalidated: number }> {
  return request(`/api/onboard/sessions/${encodeURIComponent(id)}/publish`, {
    method: "POST",
  });
}

export function cancelOnboarding(id: string): Promise<{ id: string; status: string }> {
  return request(`/api/onboard/sessions/${encodeURIComponent(id)}/cancel`, {
    method: "POST",
  });
}

export type OnboardedDomain = {
  domain: string;
  sessions: number;
  published: {
    id: string;
    collection: string;
    published_at: string | null;
    stats: OnboardingStats;
    relations_available: boolean;
  } | null;
};

export function fetchOnboardedDomains(): Promise<{ domains: OnboardedDomain[] }> {
  return request("/api/onboard/domains");
}

// ── evaluation gate ────────────────────────────────────────────────────────
//
// Reading a score, producing one, and deciding what it means are three
// capabilities, and the panel reflects that rather than showing one "eval"
// section that half-works. See platform_core/api/routes/eval.py.

export type EvalDatasetRow = {
  name: string;
  collection: string;
  content_sha256: string;
  item_count: number;
  created_at: string;
  baseline_run_id: string | null;
  baseline_promoted_at: string | null;
  baseline_note: string | null;
  latest_run: {
    run_id: string;
    status: string;
    started_at: string;
    answer_pass_rate: number | null;
    retrieval_recall: number | null;
    items_run: number;
    items_scoreable: number;
    is_baseline: boolean;
  } | null;
};

export type EvalGate = {
  would_promote: boolean;
  reasons: string[];
  deltas: Record<string, number>;
  baseline_run_id: string | null;
};

export type EvalRunDetail = {
  run_id: string;
  dataset: string | null;
  dataset_sha: string;
  code_rev: string;
  model_id: string;
  /** Null means "not measurable", which is not the same as zero. */
  answer_pass_rate: number | null;
  retrieval_recall: number | null;
  items_run: number;
  items_scoreable: number;
  elapsed_s: number;
  gate: EvalGate | null;
  outcomes: {
    item_id: string;
    question: string;
    must_cite: string[];
    retrieved: string[];
    retrieval_recall: number | null;
    passed: boolean | null;
    answer: string;
    detail: Record<string, unknown>;
  }[];
};

export function fetchEvalDatasets(): Promise<{ datasets: EvalDatasetRow[] }> {
  return request("/api/eval");
}

export function fetchEvalRun(runId: string): Promise<EvalRunDetail> {
  return request(`/api/eval/runs/${encodeURIComponent(runId)}`);
}

export function startEvalRun(dataset: string, contentSha?: string | null) {
  return request<{ run_id: string; status: string; content_sha256: string }>(
    "/api/eval/run",
    {
      method: "POST",
      body: JSON.stringify({
        dataset,
        // Pinned so a set edited after queueing does not change what is scored.
        content_sha256: contentSha ?? null,
      }),
    },
  );
}

export function promoteEvalRun(
  runId: string,
  dataset: string,
  opts: { note?: string; force?: boolean } = {},
) {
  return request<{ promoted: boolean; reasons: string[] }>(
    `/api/eval/runs/${encodeURIComponent(runId)}/promote`,
    {
      method: "POST",
      body: JSON.stringify({
        dataset,
        note: opts.note ?? null,
        force: opts.force ?? false,
      }),
    },
  );
}

// ── taxonomy editing ───────────────────────────────────────────────────────

export type TaxonomyFit = {
  instances: number;
  instances_unclassified: number;
  unclassified_share: number;
  declared_entity_types: string[];
  suggested_entity_types: { type: string; instances: number }[];
};

export type SchemaReport = {
  domain: string;
  version: number;
  entity_types: string[];
  edge_types: string[];
  unreachable_edge_types: {
    edge_type: string;
    undeclared_endpoint_types: string[];
  }[];
};

/**
 * Replace a drafted taxonomy, and retype the instances with it.
 *
 * `retype` is not optional in practice: declaring an entity type the instance
 * table never uses changes nothing about the graph, because the nodes still
 * carry their `raw:` types and edge validation compares against those.
 */
export function editSchema(
  sessionId: string,
  yaml: string,
  retype: Record<string, string> = {},
) {
  return request<{
    id: string;
    instances_retyped: number;
    original_retained: boolean;
    schema: SchemaReport;
    taxonomy_fit: TaxonomyFit;
  }>(`/api/onboard/sessions/${encodeURIComponent(sessionId)}/schema`, {
    method: "POST",
    body: JSON.stringify({ yaml, retype }),
  });
}

// ── eval set review ────────────────────────────────────────────────────────
//
// Two stores, deliberately. The expected answer is content and lives in the
// hash-versioned dataset; the labels are review state and live beside it, so
// confirming an item cannot orphan the baseline. See migration 0018.

export type EvalItemLabel = {
  item_id: string;
  /** empty | llm_drafted | sme_edited | sme_authored */
  answer_source: string;
  annotator_model: string | null;
  annotated_at: string | null;
  confirmed: boolean;
  confirmed_by: string | null;
  confirmed_at: string | null;
  requires_kg_hop: boolean;
  unusable_reason: string;
};

export type EvalReviewSummary = {
  total: number;
  with_expected_answer: number;
  with_evidence: number;
  drafted: number;
  sme_authored: number;
  confirmed: number;
  /**
   * Confirmed while still `llm_drafted` — approved without anyone editing it.
   * Reported because a set where every drafted answer was accepted unread is
   * not SME-attested ground truth, whatever the confirmed count says.
   */
  accepted_unedited: number;
  requires_kg_hop: number;
  unusable: number;
  annotator_models: string[];
};

export type EvalDatasetDetail = {
  name: string;
  collection: string;
  content_sha256: string;
  items: {
    id: string;
    question: string;
    expected_answer: string;
    must_cite: string[];
  }[];
  items_scoreable: number;
  labels: Record<string, EvalItemLabel>;
  review: EvalReviewSummary;
  history: Record<string, any>[];
};

export function fetchEvalDataset(name: string): Promise<EvalDatasetDetail> {
  return request(`/api/eval/datasets/${encodeURIComponent(name)}`);
}

export function draftEvalAnswers(name: string, limit = 15) {
  return request<{
    drafted: number;
    model: string;
    skipped_no_evidence: number;
    note?: string;
  }>(`/api/eval/datasets/${encodeURIComponent(name)}/draft`, {
    method: "POST",
    body: JSON.stringify({ limit }),
  });
}

export function reviewEvalItem(
  name: string,
  itemId: string,
  patch: {
    expected_answer?: string;
    confirmed?: boolean;
    requires_kg_hop?: boolean;
    unusable_reason?: string;
  },
) {
  return request<{ label: EvalItemLabel; version_changed?: boolean }>(
    `/api/eval/datasets/${encodeURIComponent(name)}/items/${encodeURIComponent(itemId)}`,
    { method: "PUT", body: JSON.stringify(patch) },
  );
}

// ── candidate queries ──────────────────────────────────────────────────────
//
// Questions proposed from the corpus during drafting, carrying the canonical
// ids of the chunks they were drawn from — so a set seeded from them cites
// chunks the retriever actually emits. Curating is `schema:author`; seeding a
// dataset is `release:promote`, like every other dataset write.

export type CandidateQuery = {
  id: string;
  text: string;
  evidence_chunk_ids: string[];
  source_file: string;
  page: number;
  entity_hints: string[];
  approved: boolean;
  /** A question a human rewrote, as opposed to one they waved through. */
  edited: boolean;
};

export function fetchCandidateQueries(sessionId: string): Promise<{
  queries: CandidateQuery[];
  approved: number;
  edited: number;
  without_evidence: number;
}> {
  return request(
    `/api/onboard/sessions/${encodeURIComponent(sessionId)}/queries`,
  );
}

export function curateQuery(
  sessionId: string,
  queryId: string,
  patch: { text?: string; approved?: boolean },
) {
  return request<{ query: CandidateQuery }>(
    `/api/onboard/sessions/${encodeURIComponent(sessionId)}/queries/${encodeURIComponent(queryId)}`,
    { method: "POST", body: JSON.stringify(patch) },
  );
}

export function seedEvalSet(sessionId: string, dataset?: string) {
  return request<{
    dataset: string;
    items: number;
    items_scoreable: number;
    note: string;
  }>(`/api/onboard/sessions/${encodeURIComponent(sessionId)}/seed-eval`, {
    method: "POST",
    body: JSON.stringify({ dataset: dataset ?? null }),
  });
}
