/**
 * Typed client for the Mandate gateway (backend/api/main.py).
 *
 * Set NEXT_PUBLIC_MANDATE_API_URL (for example http://localhost:8000) and the
 * cockpit runs against the gateway. With it unset, or when the gateway cannot
 * be reached, lib/mandate.ts falls back to the simulated ledger.
 *
 * Owner routes need the X-Owner-Token header. For local development only, the
 * token is read from NEXT_PUBLIC_MANDATE_OWNER_TOKEN. A NEXT_PUBLIC_ value is
 * compiled into the browser bundle, so it must never be set for a shared or
 * deployed build. Signed-in owners pass the token through setOwnerCredential,
 * which keeps it in memory only.
 *
 * Route map (owner = needs X-Owner-Token):
 *   GET   /health                                    public    { status, rail }
 *   GET   /api/v1/state                              owner     StateDTO
 *   POST  /api/v1/purchases                          agent     PurchaseDTO (X-Session-Key)
 *   GET   /api/v1/catalog                            agent     catalog (not used by the cockpit)
 *   GET   /api/v1/purchases/{id}                     agent     PurchaseDTO (not used by the cockpit)
 *   GET   /api/v1/purchases/{id}/approval            owner     ApprovalSummaryDTO (exact terms + digest)
 *   POST  /api/v1/purchases/{id}/approve             owner     { digest } -> PurchaseDTO
 *   POST  /api/v1/purchases/{id}/reject              owner     PurchaseDTO
 *   POST  /api/v1/purchases/{id}/reconcile           owner     PurchaseDTO (?rail_order_id=)
 *   GET   /api/v1/purchases/{id}/evidence            owner     EvidenceDTO
 *   GET   /api/v1/evidence/verify                    owner     ChainDTO
 *   PATCH /api/v1/policy                             owner     PolicyDTO (expected_version; 409 if stale)
 *   POST  /api/v1/kill-switch                        owner     KillSwitchDTO
 *   GET   /api/v1/session-keys                       owner     SessionKeyDTO[]
 *   POST  /api/v1/session-keys                       owner     { key_id, token, note } (token shown once)
 *   POST  /api/v1/session-keys/{id}/state            owner     SessionKeyDTO
 *   GET   /api/v1/wallet                             owner     WalletDTO
 *   POST  /api/v1/wallet/configure                   owner     WalletDTO
 *   POST  /api/v1/wallet/observations                owner     WalletDTO
 *   POST  /api/v1/wallet/reconcile                   owner     WalletDTO
 *   POST  /api/v1/stream-session                     owner     { expires_in } + read-only stream cookie
 *   GET   /api/v1/stream, /api/events (SSE)          owner     ledger events (header or stream cookie)
 */
import type { BackendPolicyClause, BackendPurchaseStage, LedgerEventDTO, PurchaseRequestDTO } from "./mandate.types";
export type { LedgerEventDTO } from "./mandate.types";

export const API_URL = (process.env.NEXT_PUBLIC_MANDATE_API_URL ?? "").replace(/\/$/, "");
/** Dev-only. See the note at the top of this file and frontend/.env.example. */
const DEV_OWNER_TOKEN = process.env.NEXT_PUBLIC_MANDATE_OWNER_TOKEN ?? "";
let ownerCredential = "";
/** The signed-in owner credential. Memory-only: never localStorage, sessionStorage, a URL or build-time env. */
export function setOwnerCredential(value: string) { ownerCredential = value; }
const ownerToken = () => ownerCredential || DEV_OWNER_TOKEN;

export type RailName = "mock" | "goat" | "qatom" | (string & {});
export type SessionState = "active" | "frozen" | "revoked";

/** What the owner approves. Any change to these terms invalidates the approval. */
export interface ApprovalSummaryDTO {
  purchase_id: string;
  mandate_id: string;
  seller_domain: string;
  seller_address: string;
  item: string;
  amount_milli: number;
  amount: string;
  arguments_sha256: string;
  policy_version: number;
  digest: string;
  expires_at: number | null;
}

export interface PurchaseDTO {
  id: string;
  mandate_id?: string;
  seller_domain: string;
  seller_address: string;
  item: string;
  amount: string; // decimal USD-TDN
  amount_milli: number; // integer thousandths
  attempt: number;
  request_key: string;
  tool_call: Record<string, unknown>;
  stage: BackendPurchaseStage;
  decision: "AUTO_APPROVED" | "HELD_FOR_REVIEW" | "BLOCKED" | null;
  clause: BackendPolicyClause | null;
  policy_version: number | null;
  policy_hash: string | null;
  session_key_id: string | null;
  expires_at: number | null; // Unix seconds
  approved_by_human: number;
  rail_order_id: string | null;
  /** Rail that handled the purchase (mock, goat-flow-x402, qatom). Null until dispatched. */
  rail?: string | null;
  /** Rail-side approval id while the owner approves in the provider (Qatom). */
  approval_id?: string | null;
  /** Final transaction id once settled. */
  rail_tx_id?: string | null;
  delivery: "NONE" | "PENDING" | "VERIFIED" | "FAILED";
  created_at: number; // Unix seconds
  updated_at: number;
}

export interface PolicyDTO {
  version: number;
  max_single_spend: number;
  transaction_ceiling: number;
  task_budget_cap: number;
  max_attempts: number;
  strict_whitelist: boolean;
  paused: boolean;
  whitelisted_sellers: Record<string, string>;
  expires_at?: number | null;
  argument_constraints?: Record<string, unknown>;
  hash: string;
}

export interface ChainDTO {
  intact: boolean;
  events: number;
  head?: string;
  first_bad_seq?: number;
}

export interface SessionKeyDTO {
  key_id: string;
  agent_id?: string;
  state: SessionState;
}

export interface StateDTO {
  mandate_id: string;
  rail: RailName;
  balances: { budget: number; held: number; settled: number; available: number };
  policy: PolicyDTO;
  purchases: PurchaseDTO[];
  chain: ChainDTO;
  session_keys?: SessionKeyDTO[];
}

export type WalletDTO =
  | { configured: false; mismatch: false; dispatch_blocked: false; provenance: "unconfigured" }
  | {
      configured: true;
      mismatch: boolean;
      dispatch_blocked: boolean;
      expected_milli: number;
      observed_milli: number;
      observed_at_settled_milli: number;
      allocation_covered: boolean;
      reference: string;
      recorded_at: number;
      provenance: "owner_recorded";
    };

export interface EvidenceDTO {
  schema: string;
  purchase: PurchaseDTO;
  events: LedgerEventDTO[];
  chain: ChainDTO;
  redactions: string[];
}

export interface KillSwitchDTO {
  engaged: boolean;
  revoked?: string[];
  slip?: LedgerEventDTO[];
  chain?: ChainDTO;
  notice?: string;
}

export interface PolicyPatch {
  expected_version: number;
  max_single_spend?: string;
  strict_whitelist?: boolean;
  max_attempts?: number;
  whitelisted_sellers?: Record<string, string>;
}

/** offline → fall back to simulation · auth → sign in needed · conflict → rules changed, reload. */
export type ErrorKind = "offline" | "auth" | "conflict" | "invalid" | "notfound" | "server";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
  get kind(): ErrorKind {
    return errorKind(this.status);
  }
}

export function errorKind(status: number): ErrorKind {
  if (status === 0) return "offline";
  if (status === 401 || status === 403) return "auth";
  if (status === 409) return "conflict";
  if (status === 404) return "notfound";
  if (status === 400 || status === 422) return "invalid";
  return "server";
}

export function kindOf(err: unknown): ErrorKind {
  return err instanceof ApiError ? err.kind : "offline";
}

/** Plain words for each failure, shown in the cockpit's status line. */
export function describe(err: unknown): string {
  const kind = kindOf(err);
  const detail = err instanceof Error ? err.message : String(err);
  switch (kind) {
    case "offline":
      return "Gateway unreachable. Showing the simulated ledger.";
    case "auth":
      return "Sign in needed. The gateway refused the owner token.";
    case "conflict":
      return `Rules changed, reload. ${detail}`;
    case "notfound":
      return "Not found on the gateway.";
    case "invalid":
      return `The gateway rejected the request: ${detail}`;
    default:
      return `Gateway error: ${detail}`;
  }
}

function formatDetail(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => (d && typeof d === "object" && "msg" in d ? String(d.msg) : JSON.stringify(d))).join("; ");
  return detail === undefined ? fallback : JSON.stringify(detail);
}

async function call<T>(path: string, init?: RequestInit & { timeoutMs?: number; owner?: boolean }): Promise<T> {
  const { timeoutMs = 8000, owner = true, ...rest } = init ?? {};
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  let r: Response;
  try {
    r = await fetch(`${API_URL}${path}`, {
      ...rest,
      cache: "no-store",
      credentials: "include",
      signal: ctl.signal,
      headers: { "Content-Type": "application/json", ...(owner && ownerToken() ? { "X-Owner-Token": ownerToken() } : {}), ...rest.headers },
    });
  } catch (err) {
    throw new ApiError(0, err instanceof Error ? err.message : "network error");
  } finally {
    clearTimeout(timer);
  }
  if (!r.ok) {
    let detail = r.statusText;
    try {
      detail = formatDetail(((await r.json()) as { detail?: unknown }).detail, detail);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(r.status, detail);
  }
  return (await r.json()) as T;
}

const post = (body?: unknown, headers?: Record<string, string>): RequestInit => ({
  method: "POST",
  body: body === undefined ? undefined : JSON.stringify(body),
  headers,
});
const enc = encodeURIComponent;

export const api = {
  health: () => call<{ status: string; rail: RailName }>("/health", { owner: false, timeoutMs: 1500 }),
  streamSession: () => call<{ expires_in: number }>("/api/v1/stream-session", post()),
  state: (timeoutMs = 8000) => call<StateDTO>("/api/v1/state", { timeoutMs }),

  // purchases
  propose: (body: PurchaseRequestDTO, token: string) =>
    call<PurchaseDTO>("/api/v1/purchases", { ...post(body, { "X-Session-Key": token }), owner: false }),
  approval: (id: string) => call<ApprovalSummaryDTO>(`/api/v1/purchases/${enc(id)}/approval`),
  /** Approves exactly the terms in `approval(id)`. A changed purchase or policy makes the digest stale (409). */
  approve: (id: string, digest: string) => call<PurchaseDTO>(`/api/v1/purchases/${enc(id)}/approve`, post({ digest })),
  reject: (id: string) => call<PurchaseDTO>(`/api/v1/purchases/${enc(id)}/reject`, post()),
  reconcile: (id: string, railOrderId?: string) =>
    call<PurchaseDTO>(`/api/v1/purchases/${enc(id)}/reconcile${railOrderId ? `?rail_order_id=${enc(railOrderId)}` : ""}`, post()),
  evidence: (id: string) => call<EvidenceDTO>(`/api/v1/purchases/${enc(id)}/evidence`),
  verify: () => call<ChainDTO>("/api/v1/evidence/verify"),

  // policy and kill-switch
  patchPolicy: (body: PolicyPatch) => call<PolicyDTO>("/api/v1/policy", { method: "PATCH", body: JSON.stringify(body) }),
  killSwitch: (engaged: boolean) => call<KillSwitchDTO>("/api/v1/kill-switch", post({ engaged })),

  // session keys
  sessionKeys: () => call<SessionKeyDTO[]>("/api/v1/session-keys"),
  issueKey: () => call<{ key_id: string; token: string; note?: string }>("/api/v1/session-keys", post()),
  setKeyState: (keyId: string, state: SessionState) => call<SessionKeyDTO>(`/api/v1/session-keys/${enc(keyId)}/state`, post({ state })),

  // wallet (owner-recorded snapshots, never a live provider balance)
  wallet: () => call<WalletDTO>("/api/v1/wallet"),
  configureWallet: (balance_milli: number, reference: string) => call<WalletDTO>("/api/v1/wallet/configure", post({ balance_milli, reference })),
  observeWallet: (balance_milli: number, reference: string) => call<WalletDTO>("/api/v1/wallet/observations", post({ balance_milli, reference })),
  reconcileWallet: (balance_milli: number, reference: string) => call<WalletDTO>("/api/v1/wallet/reconcile", post({ balance_milli, reference })),
};
/**
 * Server-sent ledger events from /api/events. The browser reconnects by itself
 * and resends Last-Event-ID, so the stream resumes where it stopped.
 */
export function subscribe(onEvent: (e: LedgerEventDTO) => void, onStatus?: (s: "open" | "stale") => void,
  onReset?: () => void): () => void {
  if (!API_URL || typeof EventSource === "undefined") return () => {};
  let stopped = false;
  let cursor = 0;
  let es: EventSource | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const retry = (reset = false) => {
    es?.close();
    if (timer) clearTimeout(timer);
    onStatus?.("stale");
    if (reset) { cursor = 0; onReset?.(); }
    if (!stopped) timer = setTimeout(open, 2000);
  };
  const open = async () => {
    try {
      await api.streamSession();
      if (stopped) return;
      es = new EventSource(`${API_URL}/api/events?after_seq=${cursor}`, { withCredentials: true });
      es.addEventListener("ledger.appended", (message) => {
        try {
          const event = JSON.parse((message as MessageEvent).data) as LedgerEventDTO;
          if (!Number.isSafeInteger(event.seq) || event.seq !== cursor + 1) { retry(true); return; }
          cursor = event.seq;
          onEvent(event);
        } catch { retry(true); }
      });
      es.addEventListener("stream.reset", () => retry(true));
      es.addEventListener("session.expired", () => retry());
      es.onopen = () => onStatus?.("open");
      es.onerror = () => retry();
    } catch { retry(); }
  };
  void open();
  return () => { stopped = true; es?.close(); if (timer) clearTimeout(timer); };
}
