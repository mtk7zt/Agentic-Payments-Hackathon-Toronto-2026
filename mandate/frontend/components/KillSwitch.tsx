"use client";

// Kill-switch confirmation and the session revocation slip it prints.
import { useEffect, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { motion, useReducedMotion } from "motion/react";
import { overlay, sheet, utc } from "./shared";
import { setPaused, verifyChain } from "@/lib/mandate";
import type { State, Verification } from "@/lib/mandate";

export function KillSwitch({ open, onOpenChange, s }: { open: boolean; onOpenChange: (o: boolean) => void; s: State }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const previous = useRef<HTMLElement | null>(null);
  useEffect(() => { if (open) setError(""); }, [open]);
  return (
    <Dialog.Root open={open} onOpenChange={(value) => { if (!pending) onOpenChange(value); }}>
      <Dialog.Portal>
        <Dialog.Overlay className={overlay} />
        <Dialog.Content onOpenAutoFocus={() => { previous.current = document.activeElement as HTMLElement; }} onCloseAutoFocus={(event) => { event.preventDefault(); previous.current?.focus(); }} className={`${sheet} left-1/2 top-1/2 w-[min(92vw,460px)] -translate-x-1/2 -translate-y-1/2 overflow-hidden`}>
          <div className="h-2" style={{ backgroundImage: "repeating-linear-gradient(135deg,#f43f5e 0 10px,#0f172a 10px 20px)" }} aria-hidden="true" />
          <div className="p-6">
            <p className="eyebrow text-rose-300">Emergency control</p>
            <Dialog.Title className="mt-2 text-[20px] font-semibold tracking-tight">Engage the kill-switch?</Dialog.Title>
            <Dialog.Description className="mt-2 text-[14px] leading-relaxed text-slate-400">
              After the gateway acknowledges pause, session keys are revoked and no new dispatch claim can be made. A payment already dispatched may still complete; its amount stays held until the outcome is known.
            </Dialog.Description>
            <div className="mt-6 flex justify-end gap-3">
              <Dialog.Close disabled={pending} className="btn-ghost">Cancel</Dialog.Close>
              <button type="button" disabled={pending} className="btn-danger uppercase tracking-[0.08em]" onClick={async () => {
                setPending(true); setError("");
                try { const result = await setPaused(true); if (result.ok) onOpenChange(false); else setError(result.error); }
                finally { setPending(false); }
              }}>
                {pending ? "Waiting for acknowledgment…" : "Revoke and lock"}
              </button>
            </div>
            {error && <p role="alert" className="mt-3 text-sm text-rose-300">{error}</p>}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/** Printed the moment the kill-switch commits; verifies itself against the chain. */
export function RevocationSlip({ s }: { s: State }) {
  const reduced = useReducedMotion();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  // The key revocation is the slip. If no key was active, the freeze event itself stands in.
  const recent = [...s.events].reverse();
  const evt = recent.find((e) => e.type === "SESSION_KEY_REVOKED" && e.clause === "AGENT_FROZEN") ?? recent.find((e) => e.type === "AGENT_FROZEN");
  const [v, setV] = useState<Verification>({ status: "running" });
  const seq = evt?.seq;
  useEffect(() => {
    let live = true;
    verifyChain(s.events).then((r) => live && setV(r));
    return () => {
      live = false;
    };
    // re-verify only when a new revocation is printed
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seq]);
  if (!evt) return null;
  return (
    <motion.div initial={reduced ? false : { y: 40, opacity: 0 }} animate={{ y: 0, opacity: 1 }} transition={{ duration: reduced ? 0 : 0.16, ease: "easeOut" }} role="status" className="mx-auto max-w-[1600px] px-5 py-3 lg:px-8">
      <div className="well grid gap-x-8 gap-y-3 rounded-md border-amber-700/50 p-4 lg:grid-cols-[auto_1fr_auto] lg:items-center">
        <div>
          <p className="eyebrow text-amber-400">Session revocation slip</p>
          <p className="mt-1 font-mono text-[17px] tabular-nums text-rose-300">{evt.ref ?? "agent"} · {evt.type === "SESSION_KEY_REVOKED" ? "REVOKED" : "FROZEN"}</p>
          <p className="font-mono text-[10.5px] text-slate-500">event #{evt.seq} · {utc(evt.ts)}Z · actor owner</p>
        </div>
        <dl className="grid gap-x-6 gap-y-1 font-mono text-[11px] sm:grid-cols-[auto_1fr]">
          <dt className="text-slate-500">sha256</dt>
          <dd className="break-all text-emerald-300">{evt.hash}</dd>
          <dt className="text-slate-500">previous</dt>
          <dd className="break-all text-slate-400">{evt.prev}</dd>
          <dt className="text-slate-500">chain</dt>
          <dd>
            {v.status === "intact" ? (
              <span className="text-emerald-300">INTACT · {v.count} events re-hashed · {v.engine}</span>
            ) : v.status === "broken" ? (
              <span className="text-rose-300">BROKEN at event #{v.at}</span>
            ) : (
              <span className="text-slate-400">re-hashing…</span>
            )}
          </dd>
        </dl>
        <div><p className="mb-2 text-sm">Paused. Payments already dispatched may still complete.</p>
          <button type="button" disabled={pending} onClick={async () => {
            setPending(true); setError("");
            try { const result = await setPaused(false); if (!result.ok) setError(result.error); }
            finally { setPending(false); }
          }} className="btn-ghost">{pending ? "Waiting for acknowledgment…" : "Release lockdown · owner"}</button>
          {error && <p role="alert" className="mt-2 text-sm text-rose-300">{error}</p>}
        </div>
      </div>
    </motion.div>
  );
}

