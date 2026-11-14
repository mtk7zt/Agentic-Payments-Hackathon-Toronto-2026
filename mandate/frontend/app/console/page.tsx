"use client";

import dynamic from "next/dynamic";

// The cockpit reads its ledger from the browser, so it renders client-side only.
const Cockpit = dynamic(() => import("@/components/Cockpit").then((m) => m.Cockpit), {
  ssr: false,
  loading: () => <p className="p-8 font-mono text-[12px] text-slate-500">loading cockpit…</p>,
});

export default function ConsolePage() {
  return <Cockpit />;
}
