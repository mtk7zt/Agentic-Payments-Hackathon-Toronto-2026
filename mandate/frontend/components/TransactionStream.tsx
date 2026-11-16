"use client";

// Live stream of settlement slips: intent -> policy -> rail settlement -> payload validation.
import { useMemo, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { Badge, stageBadge } from "./kit";
import type { Tone } from "./kit";
import { LEDC, TXT, deliveryLine, milli, settlementLine, utc } from "./shared";
import { CLAUSE_TEXT, entriesFor, fmt, reconcile, short } from "@/lib/mandate";
import type { Evt, Purchase, State } from "@/lib/mandate";

type Filter = "all" | "auto" | "pending" | "blocked";
const FILTERS: [Filter, string][] = [
  ["all", "All Events"],
  ["auto", "Auto-Settled"],
  ["pending", "Pending Review"],
  ["blocked", "Blocked Attacks"],
];
const inFilter = (p: Purchase, f: Filter) =>
  f === "all" ||
  (f === "auto" && p.decision === "AUTO_APPROVED" && (p.stage === "SETTLED" || p.stage === "VERIFIED")) ||
  (f === "pending" && (p.stage === "PENDING_APPROVAL" || p.stage === "RECOVERY")) ||
  (f === "blocked" && p.stage === "BLOCKED");

function Row({ p, events, onReview, onInspect }: { p: Purchase; events: Evt[]; onReview: () => void; onInspect: () => void }) {
  const b = stageBadge(p);
  const dTone: Tone = p.decision === "AUTO_APPROVED" ? "success" : p.decision === "BLOCKED" ? "danger" : "warning";
  const entries = entriesFor(events, p.id);
  const mine = events.filter((e) => e.purchaseId === p.id);
  const lastEvt = mine[mine.length - 1];
  const walletDelta = p.qatomTx ? -p.amount : 0;
  const A = settlementLine(p);
  const B = deliveryLine(p);
  return (
    <motion.li layout initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.14 }} className="plate relative overflow-hidden rounded-md">
      <span aria-hidden="true" className={`absolute inset-y-0 left-0 w-[3px] ${LEDC[b.tone]}`} />
      {/* slip head */}
      <div className="flex flex-wrap items-center justify-between gap-x-5 gap-y-2 border-b border-white/[0.08] py-2.5 pl-5 pr-4">
        <div className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1">
          <span className="font-mono text-[11px] tabular-nums text-slate-500">{p.id} · {utc(p.createdAt)}Z</span>
          <h3 className="text-[14px] font-medium">{p.itemLabel}</h3>
          <span className={`font-mono text-[12.5px] ${p.stage === "BLOCKED" ? "text-rose-300" : "text-slate-400"}`}>{p.vendor}</span>
        </div>
        <div className="flex items-center gap-4">
          <span className="font-mono text-[18px] tabular-nums leading-none text-content">
            {fmt(p.amount)} <span className="text-[10px] text-slate-500">USD-TDN</span>
          </span>
          <Badge tone={b.tone}>{b.label}</Badge>
        </div>
      </div>

      <div className="grid gap-px bg-white/[0.08] pl-[3px] lg:grid-cols-[1.05fr_1fr_1.15fr]">
        <Cell title="Intent → evaluation">
          <pre className="well overflow-x-auto whitespace-pre-wrap break-all rounded-[5px] p-2.5 font-mono text-[11.5px] leading-relaxed text-slate-300">
            {p.toolCall.tool}
            {"("}
            {Object.entries(p.toolCall.arguments).map(([k, v], i) => (
              <span key={k}>
                {i ? ", " : ""}
                {k}=<span className={k === "vendor" && p.stage === "BLOCKED" ? "text-rose-300" : "text-slate-100"}>"{v}"</span>
              </span>
            ))}
            {")"}
          </pre>
          {!p.decision ? (
            <p className="mt-2.5 font-mono text-[11.5px] text-sky-300">checking policy…</p>
          ) : (
            <>
              <div className="mt-2.5 flex flex-wrap gap-1.5">
                <Badge tone={dTone}>{p.decision.replace(/_/g, " ")}</Badge>
                {p.approvedByHuman && <Badge tone="success">Human authorized</Badge>}
              </div>
              <p className={`mt-2 break-all font-mono text-[11.5px] ${TXT[dTone]}`}>{p.clause}</p>
              <p className="mt-0.5 text-[12.5px] leading-snug text-slate-400">{CLAUSE_TEXT[p.clause!]}</p>
              <p className="mt-1.5 font-mono text-[10.5px] text-slate-500">policy v{p.policyVersion} · {short(p.policyHash ?? "", 6)}</p>
            </>
          )}
        </Cell>

        <Cell title="Ledger entries · ‰ USD-TDN">
          <table className="w-full font-mono text-[11.5px] tabular-nums [&_td:not(:last-child)]:pr-3 [&_th:not(:last-child)]:pr-3">
            <thead>
              <tr className="text-left text-[9.5px] uppercase tracking-[0.12em] text-slate-600">
                <th className="pb-1.5 font-normal">Entry</th>
                <th className="pb-1.5 font-normal">Debit</th>
                <th className="pb-1.5 font-normal">Credit</th>
                <th className="pb-1.5 text-right font-normal">Amount</th>
              </tr>
            </thead>
            <tbody>
              {entries.length === 0 ? (
                <tr>
                  <td colSpan={4} className="border-t border-white/[0.08] py-2 text-slate-500">
                    {p.stage === "BLOCKED" ? "no entries · nothing reserved, nothing moved" : "none yet"}
                  </td>
                </tr>
              ) : (
                entries.map((e) => (
                  <tr key={e.seq} className="border-t border-white/[0.08]">
                    <td className="whitespace-nowrap py-1.5 text-slate-500">#{e.seq} {e.memo}</td>
                    <td className="py-1.5 text-slate-300">{e.debit}</td>
                    <td className="py-1.5 text-slate-300">{e.credit}</td>
                    <td className="whitespace-nowrap py-1.5 text-right text-content">{milli(e.amount)}</td>
                  </tr>
                ))
              )}
            </tbody>
            <tfoot>
              <tr className="border-t border-white/20">
                <td colSpan={3} className="pt-1.5 text-slate-500">wallet Δ</td>
                <td className={`whitespace-nowrap pt-1.5 text-right ${walletDelta ? "text-content" : "text-emerald-300"}`}>{walletDelta ? `−${milli(-walletDelta)}` : "0"}</td>
              </tr>
            </tfoot>
          </table>
        </Cell>

        <Cell title="Protocol">
          <dl className="space-y-2 font-mono text-[11.5px]">
            <div>
              <dt className="flex items-center gap-2 text-slate-500">
                <span aria-hidden="true" className={`led ${LEDC[A.tone]}`} /> A · Rail settlement
              </dt>
              <dd className={`mt-0.5 break-all pl-3.5 ${TXT[A.tone]}`}>{A.text}</dd>
            </div>
            <div>
              <dt className="flex items-center gap-2 text-slate-500">
                <span aria-hidden="true" className={`led ${LEDC[B.tone]}`} /> B · Vendor payload verification
              </dt>
              <dd className={`mt-0.5 pl-3.5 ${TXT[B.tone]}`}>{B.text}</dd>
            </div>
            {p.revokedKey && (
              <div>
                <dt className="flex items-center gap-2 text-slate-500">
                  <span aria-hidden="true" className="led bg-rose-500" /> Session key
                </dt>
                <dd className="mt-0.5 pl-3.5 text-rose-300">{p.revokedKey} · revoked</dd>
              </div>
            )}
          </dl>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            {lastEvt && (
              <button type="button" onClick={onInspect} aria-label={`Inspect audit record, event ${lastEvt.seq}`} className="btn well min-h-[44px] gap-2 rounded-[4px] px-2.5 font-mono text-[11px] text-emerald-300 hover:border-emerald-400/50">
                <span aria-hidden="true" className="led bg-emerald-400" />#{lastEvt.seq} · {lastEvt.hash.slice(0, 10)}… <span className="text-slate-500">audit</span>
              </button>
            )}
            {!lastEvt && (
              <button type="button" onClick={onInspect} aria-label={`Open details for purchase ${p.id}`} className="btn-ghost min-h-[44px] px-3 text-[12px]">
                Details
              </button>
            )}
            {p.stage === "PENDING_APPROVAL" && (
              <button type="button" onClick={onReview} className="btn-amber lockable min-h-[44px] px-3 text-[12px]">Review</button>
            )}
            {p.stage === "RECOVERY" && (
              <button type="button" onClick={() => reconcile(p.id)} className="btn-ghost min-h-[44px] px-3 text-[12px] text-amber-200">
                {p.recovery === "DELIVERY_FAILED" ? "Re-request delivery" : "Reconcile with rail"}
              </button>
            )}
          </div>
        </Cell>
      </div>
    </motion.li>
  );
}

function Cell({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0 bg-slate-900 p-4">
      <p className="eyebrow mb-2.5">{title}</p>
      {children}
    </div>
  );
}


export function TransactionStream({ s, onReview, onInspect }: { s: State; onReview: (id: string) => void; onInspect: (id: string) => void }) {
  const locked = s.policy.paused;
  const [filter, setFilter] = useState<Filter>("all");
  const last = s.events[s.events.length - 1];
  const assertive = last && (last.type === "PURCHASE_BLOCKED" || last.type === "AGENT_FROZEN" || last.type === "SESSION_KEY_REVOKED") ? last.detail : "";
  const counts = useMemo(() => Object.fromEntries(FILTERS.map(([f]) => [f, s.purchases.filter((p) => inFilter(p, f)).length])) as Record<Filter, number>, [s.purchases]);
  const shown = s.purchases.filter((p) => inFilter(p, filter));
  return (
        <section aria-labelledby="stream-h" className="min-w-0">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h2 id="stream-h" className="flex items-center gap-2.5 text-[14px] font-semibold tracking-tight">
        <span className={`h-1.5 w-1.5 rounded-full ${locked ? "bg-amber-600" : "pulse-dot bg-emerald-400"}`} aria-hidden="true" />
        Live event pipeline
        <span className="font-mono text-[10.5px] font-normal text-slate-500">agent intent → mandate evaluation → payment rail</span>
      </h2>
      <div role="tablist" aria-label="Filter events" className="well flex max-w-full gap-0.5 overflow-x-auto rounded-md p-[3px]">
        {FILTERS.map(([f, label]) => (
          <button key={f} role="tab" type="button" aria-selected={filter === f} onClick={() => setFilter(f)} className={`shrink-0 whitespace-nowrap rounded-[4px] px-3 py-1.5 text-[12px] transition-colors duration-75 ${filter === f ? "btn-ghost min-h-0 text-content" : "text-slate-500 hover:text-slate-300"}`}>
            {label} <span className="font-mono text-[10.5px] text-slate-500">{counts[f]}</span>
          </button>
        ))}
      </div>
    </div>

    <div aria-live="polite" className="sr-only">{last?.detail ?? (s.purchases[0] ? `${s.purchases[0].itemLabel}: ${stageBadge(s.purchases[0]).label}` : "")}</div>
    <div aria-live="assertive" role="alert" className="sr-only">{assertive}</div>

    {shown.length === 0 ? (
      <p className="well mt-3 rounded-md p-8 text-center font-mono text-[12px] text-slate-500">
        {s.purchases.length ? "Nothing in this view." : "No events. Fire a scenario from the keys below."}
      </p>
    ) : (
      <ul className="mt-3 space-y-2.5">
        <AnimatePresence initial={false}>
          {shown.map((p) => (
            <Row key={p.id} p={p} events={s.events} onReview={() => onReview(p.id)} onInspect={() => onInspect(p.id)} />
          ))}
        </AnimatePresence>
      </ul>
    )}
  </section>
  );
}
