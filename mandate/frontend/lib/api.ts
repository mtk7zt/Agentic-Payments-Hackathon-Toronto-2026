/**
 * Typed client for the Mandate gateway (backend/api/main.py).
 *
 * Set NEXT_PUBLIC_MANDATE_API_URL (for example http://localhost:8000) and the
 * cockpit runs against the gateway: state over REST, events over the SSE
 * stream at /api/events. With it unset, or when the gateway cannot be
 * reached, lib/mandate.ts falls back to the simulated ledger.
 */
import type { BackendPolicyClause, BackendPurchaseStage, LedgerEventDTO, PurchaseRequestDTO } from "./mandate.types";
export type { LedgerEventDTO } from "./mandate.types";

export const API_URL = (process.env.NEXT_PUBLIC_MANDATE_API_URL ?? "").replace(/\/$/, "");

export interface PurchaseDTO {
  id: string;
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
  hash: string;
}

export interface StateDTO {
  mandate_id: string;
  rail: string;
  balances: { budget: number; held: number; settled: number; available: number };
  policy: PolicyDTO;
  purchases: PurchaseDTO[];
  chain: { intact: boolean; events: number; head?: string; first_bad_seq?: number };
  session_keys?: { key_id: string; state: "active" | "frozen" | "revoked" }[];
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<T> {
  const { timeoutMs = 8000, ...rest } = init ?? {};
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(`${API_URL}${path}`, { ...rest, signal: ctl.signal, headers: { "Content-Type": "application/json", ...rest.headers } });
    if (!r.ok) {
      let detail = r.statusText;
      try {
        detail = ((await r.json()) as { detail?: string }).detail ?? detail;
      } catch {
        /* non-JSON error body */
      }
      throw new ApiError(r.status, typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return (await r.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}

const post = (body?: unknown, headers?: Record<string, string>): RequestInit => ({ method: "POST", body: body === undefined ? undefined : JSON.stringify(body), headers });

export const api = {
  state: (timeoutMs = 8000) => call<StateDTO>("/api/v1/state", { timeoutMs }),
  issueKey: () => call<{ key_id: string; token: string }>("/api/v1/session-keys", post()),
  propose: (body: PurchaseRequestDTO, token: string) => call<PurchaseDTO>("/api/v1/purchases", post(body, { "X-Session-Key": token })),
  approve: (id: string) => call<PurchaseDTO>(`/api/v1/purchases/${id}/approve`, post()),
  reject: (id: string) => call<PurchaseDTO>(`/api/v1/purchases/${id}/reject`, post()),
  reconcile: (id: string) => call<PurchaseDTO>(`/api/v1/purchases/${id}/reconcile`, post()),
  patchPolicy: (body: Record<string, unknown>) => call<PolicyDTO>("/api/v1/policy", { method: "PATCH", body: JSON.stringify(body) }),
  killSwitch: (engaged: boolean) => call<{ engaged: boolean; revoked?: string[] }>("/api/v1/kill-switch", post({ engaged })),
  verify: () => call<StateDTO["chain"]>("/api/v1/evidence/verify"),
};

/**
 * Server-sent ledger events from /api/events. The browser reconnects by itself
 * and resends Last-Event-ID, so the stream resumes where it stopped.
 */
export function subscribe(onEvent: (e: LedgerEventDTO) => void, onStatus?: (s: "open" | "stale") => void): () => void {
  if (!API_URL || typeof EventSource === "undefined") return () => {};
  const es = new EventSource(`${API_URL}/api/events`);
  es.addEventListener("ledger.appended", (m) => onEvent(JSON.parse((m as MessageEvent).data) as LedgerEventDTO));
  es.onopen = () => onStatus?.("open");
  es.onerror = () => onStatus?.("stale");
  return () => es.close();
}
