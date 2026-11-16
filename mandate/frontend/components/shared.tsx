"use client";

import type { ReactNode } from "react";
import type { Tone } from "./kit";
import type { Purchase } from "@/lib/mandate";

export const overlay = "fixed inset-0 z-50 bg-black/75";
export const sheet = "plate fixed z-50 rounded-lg shadow-[0_30px_90px_rgba(0,0,0,.85)] focus:outline-none";

export const utc = (ms: number) => new Date(ms).toISOString().slice(11, 19);
export const milli = (n: number) => n.toLocaleString("en-US").replace(/,/g, " ");

export function settlementLine(p: Purchase): { tone: Tone; text: string } {
  if (p.stage === "BLOCKED") return { tone: "danger", text: "not called · 0 checkout calls" };
  if (p.stage === "PROPOSED") return { tone: "neutral", text: "—" };
  if (p.stage === "PENDING_APPROVAL") return { tone: "warning", text: "awaiting human authorization" };
  if (p.stage === "REJECTED" || p.stage === "EXPIRED") return { tone: "neutral", text: "not called · hold released" };
  if (p.stage === "RESERVED") return { tone: "intent", text: "reserved · claim pending" };
  if (p.stage === "DISPATCHED") return { tone: "intent", text: `claim ${p.idempotencyKey} · awaiting rail` };
  if (p.recovery === "OUTCOME_UNKNOWN") return { tone: "warning", text: `unknown · hold retained · ${p.retriesSuppressed} retry suppressed` };
  return { tone: "success", text: p.latencyMs === null ? `${p.qatomTx}` : `${p.qatomTx} · ${p.latencyMs} ms` };
}
export function deliveryLine(p: Purchase): { tone: Tone; text: string } {
  if (p.delivery === "VERIFIED") return { tone: "success", text: "verified · schema · freshness · request match" };
  if (p.delivery === "FAILED") return { tone: "danger", text: "failed · seller 502 after settlement" };
  if (p.delivery === "PENDING") return { tone: "intent", text: "checking vendor payload" };
  return { tone: "neutral", text: p.stage === "BLOCKED" ? "not applicable" : "—" };
}
export const TXT: Record<Tone, string> = { intent: "text-sky-300", success: "text-emerald-300", warning: "text-amber-300", danger: "text-rose-300", neutral: "text-slate-500" };
export const LEDC: Record<Tone, string> = { intent: "bg-sky-400", success: "bg-emerald-400", warning: "bg-amber-400", danger: "bg-rose-500", neutral: "bg-slate-600" };


export function Line({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-6 border-b border-white/[0.08] py-2 last:border-0">
      <dt className="shrink-0 text-[12.5px] text-slate-500">{k}</dt>
      <dd className="min-w-0 text-right text-[13px] [overflow-wrap:anywhere]">{children}</dd>
    </div>
  );
}

