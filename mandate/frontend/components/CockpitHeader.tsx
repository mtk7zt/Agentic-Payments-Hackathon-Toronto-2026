"use client";

// Kill-switch, live balances and rail status.
import { useState } from "react";
import Link from "next/link";
import { ArrowLeft, Check, ChevronsUpDown } from "lucide-react";
import { Logo } from "./kit";
import { BUDGET, available, fmt } from "@/lib/mandate";
import type { Mode, State } from "@/lib/mandate";
import { API_URL } from "@/lib/api";

function Stat({ label, v, tone = "text-content" }: { label: string; v: number; tone?: string }) {
  return (
    <div className="whitespace-nowrap px-4 py-2">
      <p className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-slate-500">{label}</p>
      <p className={`tint font-mono text-[16px] tabular-nums leading-tight ${tone}`}>{fmt(v)}</p>
    </div>
  );
}

export function CockpitHeader({ s, mode, onKill }: { s: State; mode: Mode; onKill: () => void }) {
  const [menu, setMenu] = useState(false);
  const locked = s.policy.paused;
  return (
    <header className="sticky top-0 z-30 border-b border-white/[0.08] bg-slate-950">
      <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-x-5 gap-y-3 px-5 py-2.5 lg:px-8">
        <Link href="/" className="flex items-center gap-2 py-1 pr-2 text-slate-500 transition-colors duration-75 hover:text-content" aria-label="Back to the landing page">
          <ArrowLeft aria-hidden="true" className="h-4 w-4" />
          <Logo />
        </Link>

        <div className="relative">
          <button type="button" aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((m) => !m)} className="btn-ghost min-h-[44px] gap-3 px-3 text-left">
            <span className="well flex h-7 w-7 items-center justify-center rounded-[4px] font-mono text-[10.5px] text-emerald-300">AC</span>
            <span>
              <span className="block text-[13px] font-medium leading-tight">Acme Corp · Procurement Agent #04</span>
              <span className="block font-mono text-[10px] leading-tight text-slate-500">mnd_acme_04 · {mode === "live" ? "live gateway" : "sample data"}</span>
            </span>
            <ChevronsUpDown aria-hidden="true" className="h-3.5 w-3.5 text-slate-500" />
          </button>
          {menu && (
            <ul role="menu" className="plate absolute left-0 top-full z-40 mt-1.5 w-72 rounded-md p-1 text-[13px] shadow-[0_18px_50px_rgba(0,0,0,.7)]">
              <li role="menuitem" className="well flex items-center justify-between rounded-[4px] px-3 py-2">
                Procurement Agent #04 <Check aria-hidden="true" className="h-3.5 w-3.5 text-emerald-400" />
              </li>
              {["Research Agent #02", "Travel Agent #07"].map((m) => (
                <li key={m} role="menuitem" aria-disabled="true" className="flex items-center justify-between px-3 py-2 text-slate-500">
                  {m} <span className="font-mono text-[10px]">not in demo</span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className={`well lockable order-last flex w-full divide-x divide-white/[0.08] overflow-x-auto rounded-md lg:order-none lg:w-auto ${locked ? "border-amber-700/50" : ""}`}>
          <Stat label="Allocation" v={s.budget ?? BUDGET} />
          <Stat label="Spent" v={s.settled} tone="text-emerald-300" />
          <Stat label="Held" v={s.held} tone="text-amber-300" />
          <Stat label="Available" v={available(s)} />
          <div className="hidden whitespace-nowrap px-4 py-2 min-[1380px]:block">
            <p className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-slate-500">Unit</p>
            <p className="font-mono text-[12px] leading-[20px] text-slate-400">USD-TDN · ‰</p>
          </div>
          <div className="hidden whitespace-nowrap px-4 py-2 min-[1280px]:block">
            <p className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-slate-500">Source</p>
            <p className={`flex items-center gap-2 font-mono text-[12px] leading-[20px] ${mode === "live" ? "text-emerald-300" : "text-slate-400"}`}>
              <span aria-hidden="true" className={`led ${mode === "live" ? "bg-emerald-400" : mode === "connecting" ? "bg-amber-400" : "bg-slate-500"}`} />
              {mode === "live" ? "gateway · polling 3 s" : mode === "connecting" ? "connecting" : mode === "signin" ? "sign in needed · simulated" : API_URL ? "gateway offline · simulated" : "simulated"}
            </p>
          </div>
        </div>

        <div className="ml-auto flex items-center gap-3">
          <span className={`well hidden items-center gap-2 rounded-[4px] px-2.5 py-2 font-mono text-[11px] md:inline-flex ${locked ? "border-rose-500/50 text-rose-300" : "text-slate-300"}`}>
            <span aria-hidden="true" className={`led ${locked || s.session.state === "revoked" ? "bg-rose-500" : s.session.state === "frozen" ? "bg-amber-400" : "bg-emerald-400"}`} />
            {s.session.key} · {s.session.state}
          </span>
          <button
            type="button"
            disabled={locked}
            onClick={onKill}
            className="btn-danger min-h-[44px] px-4 uppercase tracking-[0.08em]"
            style={locked ? undefined : { outline: "1px solid rgba(244,63,94,.35)", outlineOffset: 3 }}
          >
            <span aria-hidden="true" className="h-2.5 w-2.5 rounded-[1px] bg-slate-950" />
            {locked ? "Engaged" : "Kill-switch"}
          </button>
        </div>
      </div>
    </header>
  );
}

