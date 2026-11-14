"use client";

// Operator cockpit: header, policy matrix, transaction stream, and the bottom deck.
import { useEffect, useRef, useState } from "react";
import { CockpitHeader } from "./CockpitHeader";
import { EscalationDrawer } from "./EscalationDrawer";
import { EvidenceModal } from "./EvidenceModal";
import { KillSwitch, RevocationSlip } from "./KillSwitch";
import { PolicyMatrix } from "./PolicyMatrix";
import { TransactionStream } from "./TransactionStream";
import { release, reset, run, useLedger } from "@/lib/ledger";
import type { ScenarioKey } from "@/lib/ledger";

const TRIGGERS: { k: ScenarioKey; label: string; led: string }[] = [
  { k: "micro", label: "1. Run Micro-Purchase ($0.040)", led: "bg-emerald-400" },
  { k: "escalation", label: "2. Trigger High-Value Escalation ($2.500)", led: "bg-amber-400" },
  { k: "injection", label: "3. Simulate Prompt Injection Attack", led: "bg-rose-500" },
  { k: "sellerFail", label: "4. Seller fails after settlement", led: "bg-slate-500" },
  { k: "timeout", label: "5. Rail timeout", led: "bg-slate-500" },
];

export function Cockpit() {
  const s = useLedger();
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
      if ((p.expiresAt ?? 0) <= now) release(p.id, "EXPIRED");
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
    <div data-locked={locked} className="relative flex min-h-screen flex-col">
      <CockpitHeader s={s} onKill={() => setKill(true)} />

      {locked && (
        <div className="border-b border-amber-700/50 bg-[#140d05]">
          <p className="mx-auto flex max-w-[1600px] items-center gap-3 px-5 py-2 font-mono text-[11.5px] uppercase tracking-[0.12em] text-amber-500 lg:px-8">
            <span aria-hidden="true" className="h-2 w-10" style={{ backgroundImage: "repeating-linear-gradient(135deg,#f59e0b 0 5px,transparent 5px 10px)" }} />
            Lockdown · session key revoked · controls disabled · no new dispatch claims
          </p>
        </div>
      )}

      <div className={`relative mx-auto grid w-full max-w-[1600px] flex-1 gap-5 px-5 pt-5 lg:grid-cols-[372px_minmax(0,1fr)] lg:px-8 ${locked ? "pb-64" : "pb-32"}`}>
        <aside className="self-start lg:sticky lg:top-[84px]">
          <PolicyMatrix s={s} />
        </aside>

        <TransactionStream s={s} onReview={setReviewId} onInspect={setInspectId} />
      </div>

      {/* fixed bottom deck: revocation slip when locked, then the demo keys */}
      <div className="fixed inset-x-0 bottom-0 z-30 border-t border-white/[0.08] bg-slate-950">
        {locked && <RevocationSlip s={s} />}
        <div className={`mx-auto flex max-w-[1600px] items-center gap-2 overflow-x-auto px-5 py-2.5 lg:px-8 ${locked ? "border-t border-white/[0.08]" : ""}`}>
          <span className="eyebrow mr-1 hidden shrink-0 md:block">Demo keys</span>
          {TRIGGERS.map((t) => (
            <button key={t.k} type="button" disabled={locked} onClick={() => run(t.k)} className="btn-ghost lockable shrink-0 text-[12.5px]">
              <span aria-hidden="true" className={`led ${locked ? "bg-amber-800" : t.led}`} />
              {t.label}
            </button>
          ))}
          <button type="button" onClick={() => { reset(); seenPending.current.clear(); }} className="btn ml-auto shrink-0 text-[12px] text-slate-500 hover:text-content">
            Reset
          </button>
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
  );
}
