"use client";

// Human-in-the-loop review for a held purchase, with the approval window countdown.
import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { Badge, Money } from "./kit";
import type { Tone } from "./kit";
import { Line, overlay, sheet } from "./shared";
import { useState } from "react";
import { CLAUSE_TEXT, approveAndRun, available, fmt, isLive, release, short } from "@/lib/mandate";
import type { Purchase, State } from "@/lib/mandate";

export function EscalationDrawer({ p, s, now, onClose, onError }: { p: Purchase | null; s: State; now: number; onClose: () => void; onError: (m: string) => void }) {
  const [busy, setBusy] = useState<"approve" | "decline" | null>(null);
  if (!p) return null;
  const decide = async (kind: "approve" | "decline") => {
    setBusy(kind);
    const r = kind === "approve" ? await approveAndRun(p.id) : await release(p.id, "REJECTED");
    setBusy(null);
    if (r.ok) onClose();
    else onError(r.error);
  };
  const left = Math.max(0, (p.expiresAt ?? 0) - now);
  const live = p.stage === "PENDING_APPROVAL" && left > 0 && !s.policy.paused;
  const mm = String(Math.floor(left / 60000)).padStart(2, "0");
  const ss = String(Math.floor((left % 60000) / 1000)).padStart(2, "0");
  const mins = Math.ceil(left / 60000);
  const risk = p.risk ?? 0;
  const rTone: Tone = risk >= 70 ? "danger" : risk >= 40 ? "warning" : "success";
  return (
    <Dialog.Root open onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className={overlay} />
        <Dialog.Content className={`${sheet} inset-y-3 right-3 flex w-[min(94vw,470px)] flex-col overflow-y-auto`}>
          <div className="flex items-start justify-between gap-4 border-b border-white/[0.08] p-5">
            <div>
              <div className="flex flex-wrap gap-2">
                <Badge tone="warning">Human review</Badge>
                {p.risk !== null && <Badge tone={rTone}>Risk {risk} · rule-based</Badge>}
              </div>
              <Dialog.Title className="mt-3 text-[19px] font-semibold tracking-tight">Authorize this purchase?</Dialog.Title>
              <Dialog.Description className="mt-1.5 text-[14px] leading-relaxed text-slate-300">
                Agent requested {fmt(p.amount)} USD-TDN to acquire {p.itemLabel} from {p.vendor}.
              </Dialog.Description>
            </div>
            <Dialog.Close aria-label="Close" className="btn-ghost min-h-[44px] w-11 px-0">
              <X aria-hidden="true" className="h-4 w-4" />
            </Dialog.Close>
          </div>

          <div className="space-y-4 p-5">
            <div className="well rounded-md p-4">
              <p className="eyebrow">Window closes in</p>
              <p aria-hidden="true" className="mt-1 font-mono text-[46px] leading-none tabular-nums text-amber-300">
                {mm}:{ss}
              </p>
              <p className="sr-only" aria-live="polite">{live ? `About ${mins} minute${mins === 1 ? "" : "s"} left.` : "This request is closed."}</p>
              <div className="mt-3 flex gap-[3px]" aria-hidden="true">
                {Array.from({ length: 30 }, (_, i) => (
                  <span key={i} className={`h-2 flex-1 rounded-[1px] ${i < Math.ceil((left / (15 * 60000)) * 30) ? "bg-amber-400" : "bg-white/10"}`} />
                ))}
              </div>
              <p className="mt-2 text-[11.5px] text-slate-500">{isLive() ? "Deadline set by the gateway. After it passes, the gateway refuses a late approval." : "Simulated deadline."}</p>
            </div>

            <dl className="well rounded-md px-4 py-1">
              <Line k="Vendor"><span className="font-mono">{p.vendor}</span></Line>
              <Line k="Item">{p.itemLabel}</Line>
              <Line k="Amount"><Money v={p.amount} className="text-content" /> <span className="font-mono text-[10px] text-slate-500">USD-TDN</span></Line>
              <Line k="Reserved now"><Money v={p.amount} className="text-amber-300" /></Line>
              <Line k="Available if approved"><Money v={available(s)} /></Line>
              <Line k="Why you are asked">{CLAUSE_TEXT[p.clause!]}</Line>
              <Line k="Policy"><span className="font-mono text-[11.5px]">v{p.policyVersion} · {short(p.policyHash ?? "", 8)}</span></Line>
            </dl>
          </div>

          <div className="mt-auto space-y-2.5 border-t border-white/[0.08] p-5">
            <button
              type="button"
              disabled={!live || busy !== null}
              aria-busy={busy === "approve"}
              className="btn-primary min-h-[46px] w-full text-[14.5px]"
              onClick={() => decide("approve")}
            >
              {busy === "approve" ? "Approving…" : "Approve purchase"}
            </button>
            <button type="button" disabled={!live || busy !== null} aria-busy={busy === "decline"} className="btn-ghost min-h-[44px] w-full text-rose-300" onClick={() => decide("decline")}>
              {busy === "decline" ? "Declining…" : "Decline purchase"}
            </button>
            <p className="text-[11.5px] leading-snug text-slate-500">{isLive() ? "Approving sends the purchase to the gateway, which pays through its configured rail. Declining releases the hold." : "Simulated callback. Declining releases the hold."}</p>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

