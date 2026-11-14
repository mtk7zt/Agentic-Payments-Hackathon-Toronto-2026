import type { ReactNode } from "react";
import type { Purchase } from "@/lib/ledger";
import { fmt } from "@/lib/ledger";

export type Tone = "intent" | "success" | "warning" | "danger" | "neutral";

const TONE: Record<Tone, string> = {
  intent: "border-sky-400/30 text-sky-300",
  success: "border-emerald-400/30 text-emerald-300",
  warning: "border-amber-400/35 text-amber-300",
  danger: "border-rose-400/45 text-rose-300",
  neutral: "border-white/12 text-slate-300",
};
const LED: Record<Tone, string> = { intent: "bg-sky-400", success: "bg-emerald-400", warning: "bg-amber-400", danger: "bg-rose-500", neutral: "bg-slate-500" };

/** Stamped status tag: a square LED plus a word. State is never colour alone. */
export function Badge({ tone, children, className = "" }: { tone: Tone; children: ReactNode; icon?: ReactNode; className?: string }) {
  return (
    <span className={`well inline-flex items-center gap-1.5 whitespace-nowrap rounded-[4px] px-2 py-[3px] font-mono text-[10.5px] font-medium uppercase tracking-[0.1em] ${TONE[tone]} ${className}`}>
      <span aria-hidden="true" className={`led ${LED[tone]}`} />
      {children}
    </span>
  );
}

export function Money({ v, className = "" }: { v: number; className?: string }) {
  return (
    <span className={`font-mono tabular-nums ${className}`}>
      {fmt(v)}
      <span className="sr-only"> USD-TDN</span>
    </span>
  );
}

export function stageBadge(p: Purchase): { tone: Tone; label: string } {
  switch (p.stage) {
    case "PROPOSED":
      return { tone: "intent", label: "Evaluating" };
    case "RESERVED":
      return { tone: "intent", label: "Reserved" };
    case "DISPATCHED":
      return { tone: "intent", label: "At Qatom" };
    case "PENDING_APPROVAL":
      return { tone: "warning", label: "Pending review" };
    case "SETTLED":
      return { tone: "success", label: "Settled · verifying" };
    case "VERIFIED":
      return { tone: "success", label: "Settled · delivered" };
    case "BLOCKED":
      return { tone: "danger", label: "Halted" };
    case "REJECTED":
      return { tone: "neutral", label: "Rejected" };
    case "EXPIRED":
      return { tone: "neutral", label: "Expired" };
    default:
      return { tone: "warning", label: p.recovery === "DELIVERY_FAILED" ? "Recovery · undelivered" : "Recovery · unknown" };
  }
}

export function Logo({ className = "" }: { className?: string }) {
  return (
    <span className={`inline-flex items-center gap-2.5 ${className}`}>
      <svg viewBox="0 0 24 24" className="h-[22px] w-[22px]" aria-hidden="true">
        <rect x="0.5" y="0.5" width="23" height="23" rx="4" fill="#0b1222" stroke="rgba(255,255,255,.16)" />
        <path d="M6 16.5V7.5l6 5.5 6-5.5v9" fill="none" stroke="#f1f5f9" strokeWidth="1.6" strokeLinecap="square" strokeLinejoin="miter" />
        <rect x="6" y="18" width="12" height="1.6" fill="#10b981" />
      </svg>
      <span className="text-[14px] font-semibold tracking-[0.24em] text-content">MANDATE</span>
    </span>
  );
}

/** Hairline corner ticks, like registration marks on an instrument plate. */
export function Ticks() {
  const c = "pointer-events-none absolute h-2 w-2 border-white/25";
  return (
    <span aria-hidden="true">
      <span className={`${c} left-1.5 top-1.5 border-l border-t`} />
      <span className={`${c} right-1.5 top-1.5 border-r border-t`} />
      <span className={`${c} bottom-1.5 left-1.5 border-b border-l`} />
      <span className={`${c} bottom-1.5 right-1.5 border-b border-r`} />
    </span>
  );
}
