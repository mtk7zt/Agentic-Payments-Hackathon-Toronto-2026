/**
 * Typed client for the Mandate gateway (backend/api/main.py).
 *
 * The cockpit currently renders from the simulated ledger in lib/ledger.ts.
 * These helpers are the seam for switching it to the live backend: set
 * NEXT_PUBLIC_MANDATE_API_URL and feed `subscribe` events into the store.
 */
import type { LedgerEventDTO } from "./mandate.types";
export type { LedgerEventDTO } from "./mandate.types";

export const API_URL = process.env.NEXT_PUBLIC_MANDATE_API_URL ?? "";

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${API_URL}${path}`, { ...init, headers: { "Content-Type": "application/json", ...init?.headers } });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json() as Promise<T>;
}

export const api = {
  state: () => call<Record<string, unknown>>("/api/v1/state"),
  approve: (id: string) => call(`/api/v1/purchases/${id}/approve`, { method: "POST" }),
  reject: (id: string) => call(`/api/v1/purchases/${id}/reject`, { method: "POST" }),
  reconcile: (id: string) => call(`/api/v1/purchases/${id}/reconcile`, { method: "POST" }),
  killSwitch: (engaged: boolean) => call("/api/v1/kill-switch", { method: "POST", body: JSON.stringify({ engaged }) }),
  verify: () => call<{ intact: boolean; events: number; head?: string; first_bad_seq?: number }>("/api/v1/evidence/verify"),
};

/** Server-sent ledger events. The browser resends Last-Event-ID on reconnect, so the stream resumes. */
export function subscribe(onEvent: (e: LedgerEventDTO) => void, onStatus?: (s: "open" | "stale") => void): () => void {
  if (!API_URL) return () => {};
  const es = new EventSource(`${API_URL}/api/v1/stream`);
  es.addEventListener("ledger.appended", (m) => onEvent(JSON.parse((m as MessageEvent).data) as LedgerEventDTO));
  es.onopen = () => onStatus?.("open");
  es.onerror = () => onStatus?.("stale");
  return () => es.close();
}
