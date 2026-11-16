"use client";

// Settlement slip and forensic record: payload, clause, protocol split, SHA-256 chain verification.
import { useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { Badge, Money, stageBadge } from "./kit";
import { Line, TXT, deliveryLine, overlay, settlementLine, sheet, utc } from "./shared";
import { CLAUSE_TEXT, fmt, short, verifyChain } from "@/lib/mandate";
import type { Evt, Purchase, State, Verification } from "@/lib/mandate";

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="well rounded-md p-4">
      <h3 className="eyebrow mb-2.5">{title}</h3>
      {children}
    </section>
  );
}

export function VerifyResult({ v, count }: { v: Verification; count: number }) {
  return (
    <div role="status" aria-live="polite" className="min-h-[26px]">
      {v.status === "idle" && <p className="text-[12px] text-slate-500">Re-hashes all {count} events in your browser.</p>}
      {v.status === "running" && <Badge tone="neutral">Re-hashing…</Badge>}
      {v.status === "intact" && (
        <div>
          <Badge tone="success">Chain intact · {v.count} events</Badge>
          <p className="mt-1.5 font-mono text-[10.5px] text-slate-500">head {short(v.head, 10)} · checked with {v.engine}</p>
        </div>
      )}
      {v.status === "broken" && <Badge tone="danger">Chain broken at event #{v.at}</Badge>}
    </div>
  );
}

export function EvidenceModal({ p, s, onClose, onToast }: { p: Purchase | null; s: State; onClose: () => void; onToast: (m: string) => void }) {
  const [v, setV] = useState<Verification>({ status: "idle" });
  const [tampered, setTampered] = useState(false);
  const [sel, setSel] = useState<number | null>(null);
  if (!p) return null;

  const events: Evt[] = tampered
    ? s.events.map((e) => (e.purchaseId === p.id && e.type === "POLICY_EVALUATED" ? { ...e, detail: "AUTO_APPROVED · edited after the fact" } : e))
    : s.events;
  const mine = events.filter((e) => e.purchaseId === p.id);
  const cur = mine.find((e) => e.seq === sel) ?? mine[mine.length - 1];
  const blocked = p.stage === "BLOCKED";
  const payload = { tool: p.toolCall.tool, arguments: p.toolCall.arguments, idempotencyKey: p.idempotencyKey, redacted: ["credentials", "approval links"] };
  const bundle = { schema: "mandate.evidence.v3", mode: "mock", purchase: p, events: mine, head: s.events[s.events.length - 1]?.hash };

  const verify = async () => {
    setV({ status: "running" });
    setV(await verifyChain(events));
  };
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(bundle, null, 2));
      onToast("Verification JSON copied.");
    } catch {
      onToast("Copy is blocked in this view. Select the payload text instead.");
    }
  };

  return (
    <Dialog.Root open onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className={overlay} />
        <Dialog.Content className={`${sheet} left-1/2 top-1/2 max-h-[92vh] w-[min(96vw,1040px)] -translate-x-1/2 -translate-y-1/2 overflow-y-auto`}>
          <div className="flex items-start justify-between gap-4 border-b border-white/[0.08] p-5">
            <div>
              <div className="flex flex-wrap gap-2">
                <Badge tone={stageBadge(p).tone}>{stageBadge(p).label}</Badge>
                <Badge tone="neutral">mock mode</Badge>
              </div>
              <Dialog.Title className="mt-3 text-[19px] font-semibold tracking-tight">{blocked ? "Halted attempt · forensic record" : "Settlement slip · audit record"}</Dialog.Title>
              <Dialog.Description className="mt-1 font-mono text-[11.5px] text-slate-400">
                {p.id} · {p.itemLabel} · {p.vendor} · {fmt(p.amount)} USD-TDN
              </Dialog.Description>
            </div>
            <Dialog.Close aria-label="Close" className="btn-ghost min-h-[32px] w-8 px-0">
              <X aria-hidden="true" className="h-4 w-4" />
            </Dialog.Close>
          </div>

          <div className="grid gap-3 p-5 lg:grid-cols-2">
            <div className="space-y-3">
              <Panel title="Captured tool payload · sanitized">
                {p.injected && <p className="mb-2 break-all rounded-[4px] border border-rose-500/40 bg-rose-500/10 p-2.5 font-mono text-[11.5px] leading-relaxed text-rose-200">{p.injected}</p>}
                <pre tabIndex={0} className="overflow-x-auto whitespace-pre-wrap break-all font-mono text-[11.5px] leading-relaxed text-slate-300">{JSON.stringify(payload, null, 2)}</pre>
                {p.injected && <p className="mt-2 text-[11.5px] text-slate-500">Labelled attack fixture: the text above is what the agent read on the supplier page.</p>}
              </Panel>

              <Panel title="Violated / matched clause">
                <p className={`font-mono text-[13px] ${blocked ? "text-rose-300" : "text-emerald-300"}`}>{p.clause}</p>
                <p className="mt-1 text-[13px] leading-snug text-slate-300">{p.clause && CLAUSE_TEXT[p.clause]}</p>
                <dl className="mt-2">
                  <Line k="Policy snapshot"><span className="font-mono text-[11px] text-emerald-300">{p.policyHash}</span></Line>
                  <Line k="Version"><span className="font-mono">v{p.policyVersion}</span></Line>
                </dl>
              </Panel>

              <Panel title={blocked ? "Balance impact" : "Protocol · two separate records"}>
                {blocked ? (
                  <dl>
                    <Line k="Unspent before"><Money v={p.walletBefore} /></Line>
                    <Line k="Unspent after"><Money v={p.walletBefore} className="text-emerald-300" /> <span className="font-mono text-[11px] text-emerald-300">Δ 0</span></Line>
                    <Line k="Checkout calls"><span className="font-mono">0</span></Line>
                    <Line k="Agent session key"><span className="font-mono text-rose-300">{p.revokedKey ? `${p.revokedKey} · revoked` : "not revoked"}</span></Line>
                  </dl>
                ) : (
                  <dl>
                    <Line k="A · Qatom settlement"><span className={`font-mono text-[11.5px] ${TXT[settlementLine(p).tone]}`}>{settlementLine(p).text}</span></Line>
                    <Line k="Charged"><Money v={p.qatomTx ? p.amount : 0} /></Line>
                    <Line k="B · Vendor payload"><span className={`font-mono text-[11.5px] ${TXT[deliveryLine(p).tone]}`}>{deliveryLine(p).text}</span></Line>
                    <Line k="Receipt">Recorded · mock, not provider-verified</Line>
                  </dl>
                )}
              </Panel>
            </div>

            <div className="space-y-3">
              <Panel title="Event chain · select to inspect">
                <ol className="space-y-1">
                  {mine.map((e) => {
                    const on = cur?.seq === e.seq;
                    return (
                      <li key={e.seq}>
                        <button type="button" aria-pressed={on} onClick={() => setSel(e.seq)} className={`flex w-full items-baseline justify-between gap-3 rounded-[4px] border px-2.5 py-1.5 text-left font-mono text-[11.5px] transition-colors duration-75 ${on ? "border-emerald-400/50 bg-emerald-400/[0.06] text-content" : "border-transparent text-slate-400 hover:bg-white/[0.04]"}`}>
                          <span>#{e.seq} {e.type}</span>
                          <span className="text-emerald-300/90">{e.hash.slice(0, 10)}…</span>
                        </button>
                      </li>
                    );
                  })}
                </ol>
                {cur && (
                  <dl className="mt-3 border-t border-white/[0.08] pt-2">
                    <Line k="sha256"><span className="font-mono text-[11px] text-emerald-300">{cur.hash}</span></Line>
                    <Line k="previous"><span className="font-mono text-[11px] text-slate-400">{cur.prev}</span></Line>
                    <Line k="actor · time"><span className="font-mono text-[11.5px]">{cur.actor} · {utc(cur.ts)}Z</span></Line>
                    <Line k="detail">{cur.detail}</Line>
                  </dl>
                )}
              </Panel>

              <Panel title="Chain verification">
                <div className="flex flex-wrap gap-2">
                  <button type="button" onClick={verify} disabled={v.status === "running"} className="btn-primary">Verify SHA-256 Chain</button>
                  <button type="button" onClick={copy} className="btn-ghost">Copy JSON</button>
                </div>
                <div className="mt-3">
                  <VerifyResult v={v} count={events.length} />
                </div>
                <p className="mt-3 text-[11.5px] leading-snug text-slate-500">
                  Demo:{" "}
                  <button type="button" className="underline underline-offset-2 hover:text-slate-300" onClick={() => { setTampered((t) => !t); setV({ status: "idle" }); }}>
                    {tampered ? "undo the edit" : "secretly edit one stored event"}
                  </button>{" "}
                  and verify again. A local chain detects edits; detecting a full rewrite needs an outside checkpoint.
                </p>
              </Panel>
            </div>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

