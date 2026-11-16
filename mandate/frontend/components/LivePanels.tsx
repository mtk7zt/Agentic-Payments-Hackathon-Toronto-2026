"use client";

// Gateway-only panels: session keys and the wallet allocation monitor.
import { useState } from "react";
import { Badge, Ticks } from "./kit";
import type { Tone } from "./kit";
import { dismissIssuedToken, fmt, issueSessionKey, recordWallet, setSessionState } from "@/lib/mandate";
import type { LiveExtras, SessionState, State, WalletAction } from "@/lib/mandate";

const KEY_TONE: Record<SessionState, Tone> = { active: "success", frozen: "warning", revoked: "danger" };

export function SessionKeysPanel({ x, s }: { x: LiveExtras; s: State }) {
  const paused = s.policy.paused;
  const [busy, setBusy] = useState<string | null>(null);
  const act = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(id);
    await fn();
    setBusy(null);
  };
  return (
    <section aria-labelledby="keys-h" className="plate relative overflow-hidden rounded-lg">
      <Ticks />
      <div className="flex items-center justify-between border-b border-white/[0.08] px-5 py-3">
        <h2 id="keys-h" className="eyebrow text-slate-300">Session keys</h2>
        <span className="font-mono text-[11px] text-slate-500">{x.keys.length} issued</span>
      </div>
      <div className="space-y-3 p-5">
        {x.issued && (
          <div role="alert" className="well rounded-md border-emerald-400/30 p-3">
            <p className="text-[12.5px] font-medium">New token for {x.issued.keyId}</p>
            <p className="mt-1 text-[12px] text-slate-400">Shown once. Only its SHA-256 is stored. Give it to the agent, then hide it.</p>
            <code className="mt-2 block break-all font-mono text-[11.5px] text-emerald-300">{x.issued.token}</code>
            <button type="button" className="btn-ghost mt-2 w-full" onClick={dismissIssuedToken}>I have copied it · hide</button>
          </div>
        )}
        {x.keys.length === 0 ? (
          <p className="text-[12.5px] text-slate-500">No session keys yet. Issue one so an agent can propose purchases.</p>
        ) : (
          <ul className="space-y-2">
            {[...x.keys].reverse().map((k) => (
              <li key={k.key_id} className="well rounded-md p-3">
                <div className="flex items-center justify-between gap-3">
                  <span className="font-mono text-[12.5px]">{k.key_id}</span>
                  <Badge tone={KEY_TONE[k.state]}>{k.state}</Badge>
                </div>
                {k.state !== "revoked" && (
                  <div className="mt-2.5 flex gap-2">
                    {k.state === "active" ? (
                      <button type="button" disabled={busy === k.key_id} onClick={() => act(k.key_id, () => setSessionState(k.key_id, "frozen"))} className="btn-ghost flex-1" aria-label={`Freeze session key ${k.key_id}`}>
                        Freeze
                      </button>
                    ) : (
                      <button type="button" disabled={busy === k.key_id || paused} title={paused ? "Release the kill-switch first" : undefined} onClick={() => act(k.key_id, () => setSessionState(k.key_id, "active"))} className="btn-ghost flex-1" aria-label={`Resume session key ${k.key_id}`}>
                        Resume
                      </button>
                    )}
                    <button type="button" disabled={busy === k.key_id} onClick={() => act(k.key_id, () => setSessionState(k.key_id, "revoked"))} className="btn-ghost flex-1 text-rose-300" aria-label={`Revoke session key ${k.key_id} permanently`}>
                      Revoke
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
        <button type="button" disabled={paused || busy === "issue"} title={paused ? "Release the kill-switch first" : undefined} onClick={() => act("issue", issueSessionKey)} className="btn-primary w-full">
          Issue session key
        </button>
        <p className="text-[11.5px] leading-snug text-slate-500">Freeze stops a key until you resume it. Revoke is permanent.</p>
      </div>
    </section>
  );
}

/** Parses "12.345" into integer thousandths; null when it is not a plain amount. */
function toMilli(v: string): number | null {
  const m = /^(\d{1,9})(?:\.(\d{0,3}))?$/.exec(v.trim());
  if (!m) return null;
  return Number(m[1]) * 1000 + Number((m[2] ?? "").padEnd(3, "0") || 0);
}

export function WalletPanel({ x, s }: { x: LiveExtras; s: State }) {
  const w = x.wallet;
  const [amount, setAmount] = useState("");
  const [ref, setRef] = useState("");
  const [err, setErr] = useState("");
  const action: WalletAction = !w?.configured ? "configure" : w.mismatch ? "reconcile" : "observe";
  const label = { configure: "Record baseline", observe: "Record observation", reconcile: "Reconcile wallet" }[action];
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const milli = toMilli(amount);
    if (milli === null) return setErr("Enter an amount such as 12.000.");
    if (!ref.trim()) return setErr("Add a reference, such as a block explorer link or a statement id.");
    setErr("");
    const r = await recordWallet(action, milli, ref.trim());
    if (r.ok) {
      setAmount("");
      setRef("");
    }
  };
  const budget = s.budget ?? 0;
  return (
    <section aria-labelledby="wallet-h" className="plate rounded-lg">
      <div className="flex items-center justify-between border-b border-white/[0.08] px-5 py-3">
        <h2 id="wallet-h" className="eyebrow text-slate-300">Wallet · allocation</h2>
        {!w ? (
          <Badge tone="neutral">Unavailable</Badge>
        ) : !w.configured ? (
          <Badge tone="neutral">Not set up</Badge>
        ) : w.dispatch_blocked ? (
          <Badge tone="danger">Dispatch blocked</Badge>
        ) : (
          <Badge tone="success">Covered</Badge>
        )}
      </div>
      <div className="space-y-3 p-5">
        <dl className="space-y-1.5 font-mono text-[12px] tabular-nums">
          <Row k="allocation" v={fmt(budget)} />
          <Row k="spent" v={fmt(s.settled)} />
          <Row k="held" v={fmt(s.held)} />
          {w?.configured && (
            <>
              <Row k="expected in wallet" v={fmt(w.expected_milli)} />
              <Row k="last observed" v={fmt(w.observed_milli)} tone={w.mismatch ? "text-rose-300" : "text-emerald-300"} />
              <Row k="covers allocation" v={w.allocation_covered ? "yes" : "no"} tone={w.allocation_covered ? "text-emerald-300" : "text-rose-300"} />
              <Row k="reference" v={w.reference} />
            </>
          )}
        </dl>
        {w?.configured && w.mismatch && <p className="text-[12px] text-rose-300">The observed balance does not match what the ledger expects. Dispatch stays blocked until you reconcile.</p>}
        <form onSubmit={submit} className="space-y-2" aria-describedby="wallet-note">
          <label className="block text-[12.5px] font-medium" htmlFor="wallet-amt">Balance you see in the wallet (USD-TDN)</label>
          <input id="wallet-amt" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="12.000" className="well h-11 w-full rounded-md px-3 font-mono text-[13px] text-content placeholder:text-slate-600" />
          <label className="block text-[12.5px] font-medium" htmlFor="wallet-ref">Reference</label>
          <input id="wallet-ref" value={ref} onChange={(e) => setRef(e.target.value)} placeholder="explorer link or statement id" className="well h-11 w-full rounded-md px-3 font-mono text-[12px] text-content placeholder:text-slate-600" />
          {err && <p role="alert" className="text-[12px] text-rose-300">{err}</p>}
          <button type="submit" className="btn-ghost w-full">{label}</button>
        </form>
        <p id="wallet-note" className="text-[11.5px] leading-snug text-slate-500">Balances you record here are your own observations. The gateway does not read the provider balance.</p>
      </div>
    </section>
  );
}

function Row({ k, v, tone = "" }: { k: string; v: string; tone?: string }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-slate-500">{k}</dt>
      <dd className={`min-w-0 truncate text-right ${tone}`} title={v}>{v}</dd>
    </div>
  );
}
