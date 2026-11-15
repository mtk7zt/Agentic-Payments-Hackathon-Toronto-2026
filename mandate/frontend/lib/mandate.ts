/**
 * The cockpit's single data source.
 *
 * live       NEXT_PUBLIC_MANDATE_API_URL is set and the gateway answers. State
 *            comes from GET /api/v1/state and events from the SSE stream at
 *            /api/events; every action is an API call.
 * simulated  No URL, or the gateway is unreachable. The in-browser ledger in
 *            lib/ledger.ts takes over, and the gateway is retried every 10 s.
 *
 * Components import everything from here, so they never need to know which
 * one is running.
 */
import { useSyncExternalStore } from "react";
import { API_URL, ApiError, api, subscribe } from "./api";
import type { PurchaseDTO, StateDTO } from "./api";
import * as sim from "./ledger";
import type { Clause, Evt, EvtType, LedgerEventDTO, Policy, Purchase, ScenarioKey, Stage, State, TxResult } from "./mandate.types";

export { BUDGET, CLAUSE_TEXT, GENESIS, available, entriesFor, fmt, short, verifyChain } from "./ledger";
export type * from "./mandate.types";

export type Mode = "connecting" | "live" | "simulated";

let mode: Mode = API_URL ? "connecting" : "simulated";
let live: State | null = null;
let events: Evt[] = [];
let token: string | null = null;
let stopStream: (() => void) | null = null;
let retry: ReturnType<typeof setTimeout> | null = null;
let refreshTimer: ReturnType<typeof setTimeout> | null = null;
let started = false;
const addressBook: Record<string, string> = {};
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

// ───────── mapping: gateway wire shapes → cockpit shapes ─────────

function mapEvent(e: LedgerEventDTO): Evt {
  const { prev, hash, ...raw } = e;
  return {
    seq: e.seq, ts: e.ts, type: e.type as EvtType, actor: e.actor as Evt["actor"], purchaseId: e.purchase_id,
    amount: e.amount, clause: e.clause as Clause | null, ref: e.ref, detail: e.detail, policyHash: e.policy_hash,
    prev, hash, raw,
  };
}

function mapPurchase(p: PurchaseDTO, unspent: number): Purchase {
  const tc = p.tool_call ?? {};
  const str = (k: string) => (typeof tc[k] === "string" ? (tc[k] as string) : "");
  const recovery = p.stage === "RECOVERY_UNKNOWN" ? "OUTCOME_UNKNOWN" : p.stage === "RECOVERY_UNDELIVERED" ? "DELIVERY_FAILED" : null;
  const revoked = events.find((e) => e.type === "SESSION_KEY_REVOKED" && e.purchaseId === p.id);
  return {
    id: p.id,
    scenario: "micro",
    vendor: p.seller_domain,
    item: p.item,
    itemLabel: str("label") || p.item,
    amount: p.amount_milli,
    toolCall: { tool: str("tool") || "buy_supplier_data", arguments: { vendor: p.seller_domain, item: p.item, price: p.amount } },
    rationale: str("rationale") || "No rationale supplied by the agent.",
    injected: str("injected") || null,
    attempt: p.attempt,
    idempotencyKey: p.request_key,
    stage: (recovery ? "RECOVERY" : p.stage) as Stage,
    decision: p.decision,
    clause: p.clause,
    policyVersion: p.policy_version,
    policyHash: p.policy_hash,
    risk: null,
    expiresAt: p.expires_at ? p.expires_at * 1000 : null,
    approvedByHuman: !!p.approved_by_human,
    qatomTx: p.rail_order_id,
    latencyMs: null,
    delivery: p.delivery,
    recovery,
    retriesSuppressed: 0,
    revokedKey: revoked?.ref ?? null,
    walletBefore: unspent,
    createdAt: p.created_at * 1000,
  };
}

function mapState(st: StateDTO): State {
  Object.assign(addressBook, st.policy.whitelisted_sellers);
  const keys = st.session_keys ?? [];
  const lastKey = keys[keys.length - 1];
  const policy: Policy = {
    version: st.policy.version,
    autoApprove: st.policy.max_single_spend,
    txCeiling: st.policy.transaction_ceiling,
    strict: st.policy.strict_whitelist,
    vendors: Object.keys(st.policy.whitelisted_sellers),
    maxRetries: st.policy.max_attempts,
    paused: st.policy.paused,
  };
  const unspent = st.balances.budget - st.balances.settled;
  const claims: State["claims"] = {};
  for (const e of events) if (e.type === "DISPATCH_CLAIMED" && e.ref && e.purchaseId) claims[e.ref] = { key: e.ref, purchaseId: e.purchaseId, state: "CLAIMED" };
  return {
    policy,
    policyHash: st.policy.hash,
    held: st.balances.held,
    settled: st.balances.settled,
    wallet: unspent,
    purchases: st.purchases.map((p) => mapPurchase(p, unspent)),
    events,
    claims,
    session: { key: lastKey?.key_id ?? "no key issued", n: keys.length, state: lastKey && lastKey.state === "active" && !policy.paused ? "active" : lastKey ? "revoked" : "active" },
    counter: st.purchases.length,
  };
}

// ───────── connection ─────────

async function refresh() {
  try {
    live = mapState(await api.state());
    emit();
  } catch {
    goSimulated();
  }
}

function scheduleRefresh() {
  if (refreshTimer) clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, 60);
}

function goSimulated() {
  stopStream?.();
  stopStream = null;
  mode = "simulated";
  emit();
  if (API_URL && !retry) retry = setTimeout(() => { retry = null; connect(); }, 10_000);
}

async function connect() {
  try {
    const st = await api.state(1500);
    events = [];
    live = mapState(st);
    mode = "live";
    emit();
    stopStream = subscribe(
      (e) => {
        if (events.some((x) => x.seq === e.seq)) return; // replayed after a reconnect
        events = [...events, mapEvent(e)].sort((a, b) => a.seq - b.seq);
        scheduleRefresh();
      },
      (status) => {
        if (status === "stale") refresh(); // refresh() drops to simulated if the gateway is gone
      },
    );
  } catch {
    goSimulated();
  }
}

function start() {
  if (started || typeof window === "undefined") return;
  started = true;
  if (API_URL) connect();
}

// ───────── hooks ─────────

function subscribeAll(l: () => void) {
  start();
  listeners.add(l);
  const off = sim.subscribeLedger(l);
  return () => {
    listeners.delete(l);
    off();
  };
}
const snapshot = (): State => (mode === "live" && live ? live : sim.getState());

export const useMandate = (): State => useSyncExternalStore(subscribeAll, snapshot, snapshot);
export const useMode = (): Mode => useSyncExternalStore(subscribeAll, () => mode, () => mode);
export const isLive = () => mode === "live";
/** Vendors the policy panel offers as toggles. */
export const knownVendors = (s: State): string[] =>
  Array.from(new Set([...(isLive() ? Object.keys(addressBook) : ["seller-a", "seller-b"]), ...s.policy.vendors]));

// ───────── actions (same names and meaning in both modes) ─────────

const ok: TxResult = { ok: true };
async function guard(fn: () => Promise<unknown>): Promise<TxResult> {
  try {
    await fn();
    return ok;
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : String(err) };
  } finally {
    scheduleRefresh();
  }
}

const A1 = "0x00000000000000000000000000000000000000a1";
const B2 = "0x00000000000000000000000000000000000000b2";
const LIVE_SELLER: Partial<Record<ScenarioKey, { domain: string; address: string }>> = {
  micro: { domain: "seller-a.example", address: A1 },
  escalation: { domain: "seller-b.example", address: B2 },
  injection: { domain: "attacker.eth", address: "0x000000000000000000000000000000000000dead" },
};
/** Scenarios that need a failing seller or a silent rail exist only in the simulated ledger. */
export const scenarioAvailable = (k: ScenarioKey) => !isLive() || k in LIVE_SELLER;

async function proposeLive(k: ScenarioKey) {
  const sc = sim.SCENARIOS[k];
  const seller = LIVE_SELLER[k]!;
  const body = {
    request_key: `cockpit-${Date.now()}-${k}`,
    seller_domain: seller.domain,
    seller_address: seller.address,
    item: sc.item,
    amount: sim.fmt(sc.amount),
    tool_call: { tool: sc.toolCall.tool, label: sc.itemLabel, rationale: sc.rationale, ...(sc.injected ? { injected: sc.injected } : {}) },
  };
  for (let attempt = 0; attempt < 2; attempt++) {
    if (!token) token = (await api.issueKey()).token;
    try {
      return await api.propose(body, token);
    } catch (err) {
      // The gateway revokes a key after an out-of-mandate seller; the demo driver asks the owner route for a new one.
      if (err instanceof ApiError && err.status === 401 && attempt === 0) token = null;
      else throw err;
    }
  }
}

export function run(k: ScenarioKey): Promise<TxResult> {
  if (!isLive()) {
    sim.run(k);
    return Promise.resolve(ok);
  }
  return guard(() => proposeLive(k));
}

export function approveAndRun(id: string): Promise<TxResult> {
  return isLive() ? guard(() => api.approve(id)) : Promise.resolve(sim.approveAndRun(id));
}

export function release(id: string, why: "REJECTED" | "EXPIRED"): Promise<TxResult> {
  if (!isLive()) return Promise.resolve(sim.release(id, why));
  // The gateway refuses a late approval itself; only an explicit rejection is sent.
  return why === "REJECTED" ? guard(() => api.reject(id)) : Promise.resolve(ok);
}

export function reconcile(id: string): Promise<TxResult> {
  if (!isLive()) {
    sim.reconcile(id);
    return Promise.resolve(ok);
  }
  return guard(() => api.reconcile(id));
}

let pendingPolicy: Partial<Omit<Policy, "version" | "paused">> = {};
let policyTimer: ReturnType<typeof setTimeout> | null = null;

export function setPolicy(patch: Partial<Omit<Policy, "version" | "paused">>): Promise<TxResult> {
  if (!isLive() || !live) return Promise.resolve(sim.setPolicy(patch));
  // Show the change at once, then send one PATCH after the control settles (a slider fires many times).
  live = { ...live, policy: { ...live.policy, ...patch } };
  emit();
  pendingPolicy = { ...pendingPolicy, ...patch };
  if (policyTimer) clearTimeout(policyTimer);
  policyTimer = setTimeout(() => {
    const p = pendingPolicy;
    pendingPolicy = {};
    const body: Record<string, unknown> = {};
    if (p.autoApprove !== undefined) body.max_single_spend = sim.fmt(p.autoApprove);
    if (p.strict !== undefined) body.strict_whitelist = p.strict;
    if (p.maxRetries !== undefined) body.max_attempts = p.maxRetries;
    if (p.vendors !== undefined)
      body.whitelisted_sellers = Object.fromEntries(p.vendors.map((v) => [v, addressBook[v] ?? "0x0000000000000000000000000000000000000000"]));
    void guard(() => api.patchPolicy(body));
  }, 250);
  return Promise.resolve(ok);
}

export function setPaused(paused: boolean): Promise<TxResult> {
  if (!isLive()) return Promise.resolve(sim.setPaused(paused));
  if (paused) token = null; // the gateway revokes every active key
  return guard(() => api.killSwitch(paused));
}

/** Only the simulated ledger can be reset from the cockpit. */
export function reset() {
  if (!isLive()) sim.reset();
}
