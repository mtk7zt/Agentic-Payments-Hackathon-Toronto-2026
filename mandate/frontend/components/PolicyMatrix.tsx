"use client";

// Threshold, whitelist and attempt controls. Every change makes a new policy snapshot hash.
import { useState } from "react";
import { Minus, Plus } from "lucide-react";
import { Badge, Ticks } from "./kit";
import { available, fmt, knownVendors, setPolicy, short } from "@/lib/mandate";
import type { State } from "@/lib/mandate";

function Toggle({ id, checked, onChange, label, hint, disabled }: { id: string; checked: boolean; onChange: (v: boolean) => void; label: string; hint: string; disabled: boolean }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <label htmlFor={id} className="text-[13px] font-medium">{label}</label>
        <p id={`${id}-h`} className="mt-0.5 text-[12px] leading-snug text-slate-500">{hint}</p>
      </div>
      {/* physical slide switch: recessed track, raised cap */}
      <button
        id={id}
        type="button"
        role="switch"
        aria-checked={checked}
        aria-describedby={`${id}-h`}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className="well relative mt-0.5 h-7 w-[52px] shrink-0 rounded-[5px]"
      >
        <span className={`absolute left-1.5 top-1/2 -mt-[3px] h-[6px] w-[6px] rounded-[1px] ${checked ? (disabled ? "bg-amber-700" : "bg-emerald-400") : "bg-slate-700"}`} />
        <span
          className="absolute top-[3px] h-5 w-6 rounded-[3px] border border-white/15"
          style={{
            left: checked ? 23 : 3,
            transition: "left 50ms linear",
            background: disabled ? "#2a1a08" : "linear-gradient(180deg,#3b4a63,#1e293b)",
            boxShadow: "inset 0 1px 0 rgba(255,255,255,.18), 0 1px 0 #000",
          }}
        />
      </button>
    </div>
  );
}

export function PolicyMatrix({ s }: { s: State }) {
  const p = s.policy;
  const locked = p.paused;
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");
  const known = knownVendors(s);
  const toggleVendor = (v: string) => setPolicy({ vendors: p.vendors.includes(v) ? p.vendors.filter((x) => x !== v) : [...p.vendors, v] });
  const add = () => {
    const v = draft.trim().toLowerCase();
    if (v && !p.vendors.includes(v)) setPolicy({ vendors: [...p.vendors, v] });
    setDraft("");
    setAdding(false);
  };

  return (
    <div className="space-y-3">
      <section aria-labelledby="pol-h" className="plate relative overflow-hidden rounded-lg">
        <Ticks />
        <div className="flex items-center justify-between border-b border-white/[0.08] px-5 py-3">
          <h2 id="pol-h" className="eyebrow text-slate-300">Policy</h2>
          {locked ? <Badge tone="warning">Locked</Badge> : <span className="font-mono text-[11px] text-slate-500">v{p.version}</span>}
        </div>

        <div className="lockable hazard space-y-5 p-5">
          <div className="well rounded-md p-3">
            <p className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-slate-500">Policy snapshot · sha256 · v{p.version}</p>
            <p key={s.policyHash} className="tint mt-1 break-all font-mono text-[11.5px] leading-snug text-emerald-300" title={s.policyHash}>
              {s.policyHash}
            </p>
          </div>

          <div>
            <div className="flex items-baseline justify-between">
              <label htmlFor="auto" className="text-[13px] font-medium">Auto-approve threshold</label>
              <span className="tint font-mono text-[14px] tabular-nums text-emerald-300">{fmt(p.autoApprove)}</span>
            </div>
            <div className="well mt-2.5 rounded-[5px] px-2 py-2">
              <input id="auto" type="range" min={0} max={500} step={10} value={p.autoApprove} disabled={locked} onChange={(e) => setPolicy({ autoApprove: Number(e.target.value) })} className="block w-full cursor-pointer accent-emerald-400" />
            </div>
            <div className="mt-1.5 flex justify-between font-mono text-[10px] text-slate-600">
              <span>0.000</span>
              <span>above this, a person decides</span>
              <span>0.500</span>
            </div>
          </div>

          <div>
            <p className="text-[13px] font-medium">Whitelisted vendors</p>
            <div className="mt-2.5 flex flex-wrap gap-2">
              {known.map((v) => {
                const on = p.vendors.includes(v);
                return (
                  <button key={v} type="button" aria-pressed={on} disabled={locked} onClick={() => toggleVendor(v)} className={`btn min-h-[34px] gap-2 px-3 font-mono text-[12px] ${on ? "well text-emerald-200" : "btn-ghost text-slate-500 line-through"}`}>
                    <span aria-hidden="true" className={`led ${on ? "bg-emerald-400" : "bg-slate-700"}`} />
                    {v}
                  </button>
                );
              })}
              {adding ? (
                <form onSubmit={(e) => { e.preventDefault(); add(); }} className="inline-flex">
                  <input autoFocus aria-label="Vendor id" value={draft} onChange={(e) => setDraft(e.target.value)} onBlur={add} placeholder="vendor id" className="well h-[34px] w-32 rounded-md px-3 font-mono text-[12px] text-content placeholder:text-slate-600" />
                </form>
              ) : (
                <button type="button" disabled={locked} onClick={() => setAdding(true)} className="btn-ghost min-h-[34px] gap-1.5 px-3 font-mono text-[12px]">
                  <Plus aria-hidden="true" className="h-3 w-3" /> Add vendor
                </button>
              )}
            </div>
          </div>

          <Toggle id="strict" checked={p.strict} disabled={locked} onChange={(v) => setPolicy({ strict: v })} label="Strict mode" hint="On: an unlisted vendor is halted outright. Off: it is held for a person." />

          <div className="flex items-center justify-between gap-4">
            <div>
              <p className="text-[13px] font-medium">Max retries</p>
              <p className="mt-0.5 text-[12px] text-slate-500">Paid attempts per task, capped at 5.</p>
            </div>
            <div className="flex items-center gap-1.5">
              <button type="button" aria-label="Fewer retries" disabled={locked || p.maxRetries <= 1} onClick={() => setPolicy({ maxRetries: p.maxRetries - 1 })} className="btn-ghost min-h-[32px] w-8 px-0">
                <Minus aria-hidden="true" className="h-3.5 w-3.5" />
              </button>
              <output className="well tint flex h-8 w-9 items-center justify-center rounded-[4px] font-mono text-[14px] tabular-nums text-emerald-300">{p.maxRetries}</output>
              <button type="button" aria-label="More retries" disabled={locked || p.maxRetries >= 5} onClick={() => setPolicy({ maxRetries: p.maxRetries + 1 })} className="btn-ghost min-h-[32px] w-8 px-0">
                <Plus aria-hidden="true" className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        </div>
      </section>

      <section aria-labelledby="led-h" className="plate rounded-lg p-5">
        <h2 id="led-h" className="eyebrow text-slate-300">Ledger · integer thousandths</h2>
        <dl className="mt-3 space-y-1.5 font-mono text-[12px] tabular-nums">
          {[
            ["available", available(s)],
            ["+ held", s.held],
            ["+ spent", s.settled],
          ].map(([k, v]) => (
            <div key={k as string} className="flex justify-between gap-3">
              <dt className="text-slate-500">{k}</dt>
              <dd>{(v as number).toLocaleString("en-US").replace(/,/g, " ")}</dd>
            </div>
          ))}
          <div className="flex justify-between gap-3 border-t border-white/[0.08] pt-1.5">
            <dt className="text-slate-500">= allocation</dt>
            <dd className="text-emerald-300">{(available(s) + s.held + s.settled).toLocaleString("en-US").replace(/,/g, " ")} ✓</dd>
          </div>
          <div className="flex justify-between gap-3 pt-2">
            <dt className="text-slate-500">dispatch claims</dt>
            <dd>{Object.keys(s.claims).length}</dd>
          </div>
          <div className="flex justify-between gap-3">
            <dt className="text-slate-500">chain head · #{s.events.length}</dt>
            <dd className="text-emerald-300">{short(s.events[s.events.length - 1]?.hash ?? "", 6)}</dd>
          </div>
        </dl>
        <p className="mt-3 text-[11.5px] leading-snug text-slate-500">Each change commits as one checked transaction, saved in this browser. A reload resumes from the last committed stage.</p>
      </section>
    </div>
  );
}

