"use client";

// Operator cockpit: header, policy matrix, transaction stream, and the bottom deck.
import { useEffect, useRef, useState } from "react";
import { MotionConfig } from "motion/react";
import { CockpitHeader } from "./CockpitHeader";
import { EscalationDrawer } from "./EscalationDrawer";
import { EvidenceModal } from "./EvidenceModal";
import { KillSwitch, RevocationSlip } from "./KillSwitch";
import { PolicyMatrix } from "./PolicyMatrix";
import { SessionKeysPanel, WalletPanel } from "./LivePanels";
import { TransactionStream } from "./TransactionStream";
import { clearNotice, isLive, release, reloadState, reset, run, scenarioAvailable, useLive, useMandate, useMode, useNotice } from "@/lib/mandate";
import type { ScenarioKey } from "@/lib/mandate";

const TRIGGERS: { k: ScenarioKey; label: string; led: string }[] = [
  { k: "micro", label: "1. Run Micro-Purchase ($0.040)", led: "bg-emerald-400" },
  { k: "escalation", label: "2. Trigger High-Value Escalation ($2.500)", led: "bg-amber-400" },
  { k: "injection", label: "3. Simulate Prompt Injection Attack", led: "bg-rose-500" },
  { k: "sellerFail", label: "4. Seller fails after settlement", led: "bg-slate-500" },
  { k: "timeout", label: "5. Rail timeout", led: "bg-slate-500" },
];

export function Cockpit() {
  const s = useMandate();
  const mode = useMode();
  const x = useLive();
  const notice = useNotice();
  const locked = s.policy.paused;
  const [kill, setKill] = useState(false);
  const [reviewId, setReviewId] = useState<string | null>(null);
  const [inspectId, setInspectId] = useState<string | null>(null);
  const [toast, setToast] = useState("");
  const [now, setNow] = useState(Date.now());
  const seenPending = useRef<Set<string>>(new Set(s.purchases.filter((p) => p.stage === "PENDING_APPROVAL").map((p) => p.id)));

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    for (const p of s.purchases) {
      if (p.stage !== "PENDING_APPROVAL") continue;
      if ((p.expiresAt ?? 0) <= now) { if (!isLive()) release(p.id, "EXPIRED"); }
      else if (!seenPending.current.has(p.id)) {
        seenPending.current.add(p.id);
        setReviewId(p.id);
      }
    }
  }, [s.purchases, now]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(""), 4200);
    return () => clearTimeout(t);
  }, [toast]);

  const review = s.purchases.find((p) => p.id === reviewId) ?? null;
  const inspect = s.purchases.find((p) => p.id === inspectId) ?? null;

  return (
    <MotionConfig reducedMotion="user">
    <div data-locked={locked} className="relative flex min-h-screen flex-col">
      <CockpitHeader s={s} mode={mode} onKill={() => setKill(true)} />

      {/* Connection and action status. Polite, so it never interrupts a screen reader mid-sentence. */}
      <div role="status" aria-live="polite" aria-atomic="true">
        {(notice && notice.kind !== "ok") || mode === "signin" ? (
          <div className={`border-b ${notice?.kind === "conflict" || mode === "signin" ? "border-amber-700/50 bg-[#140d05] text-amber-300" : "border-rose-700/40 bg-[#14070b] text-rose-300"}`}>
            <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-3 px-5 py-2 text-[13px] lg:px-8">
              <span className="min-w-0 flex-1">
                {mode === "signin" && notice?.kind !== "conflict"
                  ? "Sign in needed. The gateway refused the owner token, so the simulated ledger is shown. Set NEXT_PUBLIC_MANDATE_OWNER_TOKEN for local development."
                  : notice?.text}
              </span>
              {notice?.kind === "conflict" && (
                <button type="button" className="btn-amber" onClick={() => { void reloadState(); clearNotice(); }}>
                  Reload rules
                </button>
              )}
              {notice && (
                <button type="button" className="btn-ghost" aria-label="Dismiss message" onClick={clearNotice}>
                  Dismiss
                </button>
              )}
            </div>
          </div>
        ) : notice ? (
          <p className="sr-only">{notice.text}</p>
        ) : null}
      </div>

      {locked && (
        <div className="border-b border-amber-700/50 bg-[#140d05]">
          <p className="mx-auto flex max-w-[1600px] items-center gap-3 px-5 py-2 font-mono text-[11.5px] uppercase tracking-[0.12em] text-amber-500 lg:px-8">
            <span aria-hidden="true" className="h-2 w-10" style={{ backgroundImage: "repeating-linear-gradient(135deg,#f59e0b 0 5px,transparent 5px 10px)" }} />
            Lockdown · session key revoked · controls disabled · no new dispatch claims
          </p>
        </div>
      )}

      <div className={`relative mx-auto grid w-full max-w-[1600px] flex-1 gap-5 px-5 pt-5 lg:grid-cols-[372px_minmax(0,1fr)] lg:px-8 ${locked ? "pb-64" : "pb-32"}`}>
        <aside className="space-y-3 self-start">
          <PolicyMatrix s={s} />
          {x && <SessionKeysPanel x={x} s={s} />}
          {x && <WalletPanel x={x} s={s} />}
        </aside>

        <TransactionStream s={s} onReview={setReviewId} onInspect={setInspectId} />
      </div>

      {/* fixed bottom deck: revocation slip when locked, then the demo keys */}
      <div className="fixed inset-x-0 bottom-0 z-30 border-t border-white/[0.08] bg-slate-950">
        {locked && <RevocationSlip s={s} />}
        <div className={`mx-auto flex max-w-[1600px] items-center gap-2 overflow-x-auto px-5 py-2.5 lg:px-8 ${locked ? "border-t border-white/[0.08]" : ""}`}>
          <span className="eyebrow mr-1 hidden shrink-0 md:block">Demo keys</span>
          {TRIGGERS.map((t) => (
            <button key={t.k} type="button" disabled={locked || !scenarioAvailable(t.k)} title={scenarioAvailable(t.k) ? undefined : "Needs a failing seller or a silent rail: simulated ledger only"} onClick={() => run(t.k)} className="btn-ghost lockable shrink-0 text-[12.5px]">
              <span aria-hidden="true" className={`led ${locked ? "bg-amber-800" : t.led}`} />
              {t.label}
            </button>
          ))}
          {mode === "simulated" ? (
            <button type="button" onClick={() => { reset(); seenPending.current.clear(); }} className="btn ml-auto shrink-0 text-[12px] text-slate-500 hover:text-content">
              Reset
            </button>
          ) : (
            <span className="ml-auto shrink-0 font-mono text-[10.5px] text-slate-500">{mode === "live" ? `live gateway · ${x?.rail ?? "?"} rail` : mode === "signin" ? "sign in needed" : "connecting…"}</span>
          )}
        </div>
      </div>

      <KillSwitch open={kill} onOpenChange={setKill} s={s} />
      <EscalationDrawer p={review} s={s} now={now} onClose={() => setReviewId(null)} onError={setToast} />
      <EvidenceModal key={inspectId ?? "none"} p={inspect} s={s} onClose={() => setInspectId(null)} onToast={setToast} />

      {toast && (
        <div role="status" className="plate fixed bottom-24 left-1/2 z-[60] -translate-x-1/2 rounded-md px-4 py-2 text-[13px]">
          {toast}
        </div>
      )}
    </div>
    </MotionConfig>
  );
}
