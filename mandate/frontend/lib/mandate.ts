/**
 * The cockpit's single data source.
 *
 * live       NEXT_PUBLIC_MANDATE_API_URL is set and the gateway answers. State
 *            comes from GET /api/v1/state (polled every 3 s) and every action is
 *            an owner API call.
 * signin     The gateway answered 401/403: the owner token is missing or wrong.
 *            The simulated ledger is shown until a token works.
 * simulated  No URL, or the gateway is unreachable. The in-browser ledger in
 *            lib/ledger.ts takes over, and the gateway is retried every 10 s.
 *
 * Components import everything from here, so they never need to know which
 * one is running.
 */
import { useSyncExternalStore } from "react";
import { API_URL, ApiError, api, describe, kindOf } from "./api";
import type { ErrorKind, KillSwitchDTO, PurchaseDTO, RailName, SessionKeyDTO, SessionState, StateDTO, WalletDTO } from "./api";
import * as sim from "./ledger";
import type { Clause, Evt, EvtType, LedgerEventDTO, Policy, Purchase, ScenarioKey, Stage, State, TxResult } from "./mandate.types";

export { BUDGET, CLAUSE_TEXT, GENESIS, available, entriesFor, fmt, short, verifyChain } from "./ledger";
export type * from "./mandate.types";
export type { RailName, SessionKeyDTO, SessionState, WalletDTO } from "./api";

export type Mode = "connecting" | "live" | "signin" | "simulated";

/** Gateway facts the simulated ledger has no equivalent for. */
export interface LiveExtras {
  mandateId: string;
  rail: RailName;
  keys: SessionKeyDTO[];
  wallet: WalletDTO | null;
  chain: StateDTO["chain"] | null;
  /** The policy version the gateway last confirmed; sent as expected_version. */
  serverVersion: number;
  /** A freshly issued session token. The gateway shows it once. */
  issued: { keyId: string; token: string } | null;
}

/** One status line for the cockpit's aria-live region. */
export interface Notice {
  kind: ErrorKind | "ok";
  text: string;
  at: number;
}

const POLL_MS = 3000;
const RETRY_MS = 10_000;

let mode: Mode = API_URL ? "connecting" : "simulated";
let live: State | null = null;
let extras: LiveExtras | null = null;
let notice: Notice | null = null;
let events: Evt[] = [];
let token: string | null = null;
let poller: ReturnType<typeof setInterval> | null = null;
let retry: ReturnType<typeof setTimeout> | null = null;
let refreshTimer: ReturnType<typeof setTimeout> | null = null;
let started = false;
const addressBook: Record<string, string> = {};
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

function say(kind: Notice["kind"], text: string) {
  notice = { kind, text, at: Date.now() };
  emit();
}
export function clearNotice() {
  notice = null;
  emit();
}

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
    session: { key: lastKey?.key_id ?? "no key issued", n: keys.length, state: lastKey ? lastKey.state : "active" },
    counter: st.purchases.length,
    budget: st.balances.budget,
  };
}

// ───────── connection ─────────
// TODO(MND-17): the SSE stream (/api/events) is owner-only and EventSource cannot
// send X-Owner-Token. Until owner auth for the stream lands, poll /api/v1/state.

function apply(st: StateDTO, wallet: WalletDTO | null) {
  live = mapState(st);
  extras = {
    mandateId: st.mandate_id,
    rail: st.rail,
    keys: st.session_keys ?? [],
    wallet,
    chain: st.chain,
    serverVersion: st.policy.version,
    issued: extras?.issued ?? null,
  };
  // A policy edit still waiting to be sent stays visible over the polled copy.
  if (Object.keys(pendingPolicy).length) live = { ...live, policy: { ...live.policy, ...pendingPolicy } };
}

// Sequential on purpose: the gateway's ledger can fail two overlapping reads (see the PR notes).
async function fetchAll(timeoutMs?: number) {
  const st = await api.state(timeoutMs);
  const wallet = await api.wallet().catch(() => extras?.wallet ?? null);
  apply(st, wallet);
}

let misses = 0;

let inFlight: Promise<void> | null = null;
let again = false;
/** One read at a time; a request made meanwhile runs once the current one ends. */
function refresh(): Promise<void> {
  if (inFlight) {
    again = true;
    return inFlight;
  }
  inFlight = refreshOnce().finally(() => {
    inFlight = null;
    if (again) {
      again = false;
      void refresh();
    }
  });
  return inFlight;
}

async function refreshOnce() {
  try {
    await fetchAll();
    misses = 0;
    if (mode !== "live") {
      mode = "live";
      say("ok", "Connected to the gateway.");
    }
    emit();
  } catch (err) {
    // One failed poll is not an outage; an auth refusal is acted on at once.
    if (mode === "live" && kindOf(err) !== "auth" && ++misses < 2) return;
    misses = 0;
    fallBack(err);
  }
}

function scheduleRefresh() {
  if (refreshTimer) clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, 60);
}

function stopPolling() {
  if (poller) clearInterval(poller);
  poller = null;
}

function fallBack(err: unknown) {
  stopPolling();
  const next: Mode = kindOf(err) === "auth" ? "signin" : "simulated";
  const changed = next !== mode;
  mode = next;
  if (changed) say(kindOf(err) === "auth" ? "auth" : "offline", describe(err));
  else emit();
  if (API_URL && !retry) retry = setTimeout(() => { retry = null; connect(); }, RETRY_MS);
}

async function connect() {
  try {
    await fetchAll(1500);
    const was = mode;
    mode = "live";
    if (was !== "connecting") say("ok", "Connected to the gateway.");
    else emit();
    stopPolling();
    poller = setInterval(refresh, POLL_MS);
  } catch (err) {
    fallBack(err);
  }
}

function start() {
  if (started || typeof window === "undefined") return;
  started = true;
  if (API_URL) connect();
}

/** "Rules changed, reload": drop local edits and take the gateway's current copy. */
export function reloadState(): Promise<void> {
  pendingPolicy = {};
  return refresh();
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
/** Gateway-only facts (rail, session keys, wallet). Null while simulated. */
export const useLive = (): LiveExtras | null =>
  useSyncExternalStore(subscribeAll, () => (mode === "live" ? extras : null), () => null);
export const useNotice = (): Notice | null => useSyncExternalStore(subscribeAll, () => notice, () => null);
export const isLive = () => mode === "live";
/** Vendors the policy panel offers as toggles. */
export const knownVendors = (s: State): string[] =>
  Array.from(new Set([...(isLive() ? Object.keys(addressBook) : ["seller-a", "seller-b"]), ...s.policy.vendors]));
/** Payment address on file for a seller (live mode only). */
export const vendorAddress = (v: string): string | undefined => addressBook[v];

// ───────── actions (same names and meaning in both modes) ─────────

const ok: TxResult = { ok: true };
async function guard(fn: () => Promise<unknown>, success?: string): Promise<TxResult> {
  try {
    await fn();
    if (success) say("ok", success);
    return ok;
  } catch (err) {
    const text = describe(err);
    say(kindOf(err), text);
    if (kindOf(err) === "offline") scheduleRefresh(); // refresh() decides whether the gateway is really gone
    return { ok: false, error: text };
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
  return isLive() ? guard(() => api.approve(id), "Purchase approved and sent to the rail.") : Promise.resolve(sim.approveAndRun(id));
}

export function release(id: string, why: "REJECTED" | "EXPIRED"): Promise<TxResult> {
  if (!isLive()) return Promise.resolve(sim.release(id, why));
  // The gateway refuses a late approval itself; only an explicit rejection is sent.
  return why === "REJECTED" ? guard(() => api.reject(id), "Purchase declined. The hold is released.") : Promise.resolve(ok);
}

export function reconcile(id: string, railOrderId?: string): Promise<TxResult> {
  if (!isLive()) {
    sim.reconcile(id);
    return Promise.resolve(ok);
  }
  return guard(() => api.reconcile(id, railOrderId), "Reconciled with the rail.");
}

/** Ledger events for one purchase. Live mode fetches the evidence bundle from the gateway. */
export async function loadEvidence(id: string): Promise<Evt[] | null> {
  if (!isLive()) return null;
  try {
    const ev = await api.evidence(id);
    mergeEvents(ev.events);
    return ev.events.map(mapEvent);
  } catch (err) {
    say(kindOf(err), describe(err));
    return null;
  }
}

function mergeEvents(list: LedgerEventDTO[]) {
  const seen = new Set(events.map((e) => e.seq));
  const fresh = list.filter((e) => !seen.has(e.seq)).map(mapEvent);
  if (!fresh.length) return;
  events = [...events, ...fresh].sort((a, b) => a.seq - b.seq);
  if (live) live = { ...live, events };
  emit();
}

let pendingPolicy: Partial<Omit<Policy, "version" | "paused">> = {};
let policyTimer: ReturnType<typeof setTimeout> | null = null;

export function setPolicy(patch: Partial<Omit<Policy, "version" | "paused">>): Promise<TxResult> {
  if (!isLive() || !live || !extras) return Promise.resolve(sim.setPolicy(patch));
  // Show the change at once, then send one PATCH after the control settles (a slider fires many times).
  live = { ...live, policy: { ...live.policy, ...patch } };
  emit();
  pendingPolicy = { ...pendingPolicy, ...patch };
  if (policyTimer) clearTimeout(policyTimer);
  return new Promise((resolve) => {
    policyTimer = setTimeout(() => {
      const p = pendingPolicy;
      pendingPolicy = {};
      const body: Parameters<typeof api.patchPolicy>[0] = { expected_version: extras!.serverVersion };
      if (p.autoApprove !== undefined) body.max_single_spend = sim.fmt(p.autoApprove);
      if (p.strict !== undefined) body.strict_whitelist = p.strict;
      if (p.maxRetries !== undefined) body.max_attempts = p.maxRetries;
      if (p.vendors !== undefined) {
        const missing = p.vendors.filter((v) => !addressBook[v]);
        if (missing.length) {
          say("invalid", `Add a payment address for ${missing.join(", ")} before trusting it.`);
          scheduleRefresh();
          return resolve({ ok: false, error: "missing address" });
        }
        body.whitelisted_sellers = Object.fromEntries(p.vendors.map((v) => [v, addressBook[v]]));
      }
      guard(async () => {
        const pol = await api.patchPolicy(body);
        if (extras) extras = { ...extras, serverVersion: pol.version };
      }, "Rules saved.").then(resolve);
    }, 250);
  });
}

/** Trust a new seller. The gateway pays only the address on file for a domain. */
export function addVendor(domain: string, address: string): Promise<TxResult> {
  const d = domain.trim().toLowerCase();
  if (!isLive() || !live) return Promise.resolve(sim.setPolicy({ vendors: [...sim.getState().policy.vendors, d] }));
  addressBook[d] = address.trim().toLowerCase();
  return setPolicy({ vendors: Array.from(new Set([...live.policy.vendors, d])) });
}

export function setPaused(paused: boolean): Promise<TxResult> {
  if (!isLive()) return Promise.resolve(sim.setPaused(paused));
  if (paused) token = null; // the gateway revokes every active key
  return guard(async () => {
    const r: KillSwitchDTO = await api.killSwitch(paused);
    if (r.slip?.length) mergeEvents(r.slip);
    if (r.notice) say("ok", r.notice);
  }, paused ? undefined : "Kill-switch released. Issue a new session key to resume.");
}

// session keys (live only)

export function issueSessionKey(): Promise<TxResult> {
  return guard(async () => {
    const r = await api.issueKey();
    if (extras) extras = { ...extras, issued: { keyId: r.key_id, token: r.token } };
    emit();
  }, "Session key issued. Copy the token now; it is shown once.");
}

export function dismissIssuedToken() {
  if (extras) extras = { ...extras, issued: null };
  emit();
}

export function setSessionState(keyId: string, state: SessionState): Promise<TxResult> {
  const words: Record<SessionState, string> = { active: "resumed", frozen: "frozen", revoked: "revoked" };
  return guard(() => api.setKeyState(keyId, state), `Session key ${keyId} ${words[state]}.`);
}

// wallet (live only; owner-recorded snapshots)

export type WalletAction = "configure" | "observe" | "reconcile";
export function recordWallet(action: WalletAction, balanceMilli: number, reference: string): Promise<TxResult> {
  const fn = { configure: api.configureWallet, observe: api.observeWallet, reconcile: api.reconcileWallet }[action];
  const words = { configure: "Wallet baseline recorded.", observe: "Wallet observation recorded.", reconcile: "Wallet reconciled." }[action];
  return guard(async () => {
    const w = await fn(balanceMilli, reference);
    if (extras) extras = { ...extras, wallet: w };
  }, words);
}

/** Only the simulated ledger can be reset from the cockpit. */
export function reset() {
  if (!isLive()) sim.reset();
}

/** Live mode re-hashes the whole chain on the gateway; the browser only holds part of it. */
export async function verifyOnGateway(): Promise<import("./mandate.types").Verification> {
  try {
    const c = await api.verify();
    return c.intact
      ? { status: "intact", count: c.events, head: c.head ?? "", engine: "gateway" }
      : { status: "broken", at: c.first_bad_seq ?? 0, count: c.events };
  } catch (err) {
    say(kindOf(err), describe(err));
    return { status: "idle" };
  }
}
