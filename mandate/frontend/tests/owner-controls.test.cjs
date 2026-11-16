const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

function model() {
  const state = { policy: { version: 3, max_single_spend: 100, transaction_ceiling: 2000,
    strict_whitelist: true, whitelisted_sellers: { "seller.example": "0xa1" }, max_attempts: 2, paused: false, hash: "policy" },
    purchases: [], balances: { budget: 1800, held: 100, settled: 200 }, session_keys: [],
    wallet_monitoring: { configured: true, mismatch: true, last_reconciled_milli: 6000 } };
  const writes = [];
  let conflict = false;
  const react = { useSyncExternalStore: (subscribe, snapshot) => { subscribe(() => {}); return snapshot(); } };
  const api = { API_URL: "http://localhost:8000", ApiError: Error, kindOf: (e) => /version/.test(String(e && e.message)) ? "conflict" : "network", describe: (e) => String(e && e.message || e), api: {
    health: async () => ({ status: "ok", rail: "mock" }),
    state: async () => structuredClone(state),
    wallet: async () => null,
    patchPolicy: async (body) => {
      writes.push(body);
      if (conflict || body.expected_version !== state.policy.version) throw new Error("policy version mismatch");
      state.policy.version++;
      if (body.max_single_spend) state.policy.max_single_spend = Math.round(Number(body.max_single_spend) * 1000);
      if (body.max_attempts) state.policy.max_attempts = body.max_attempts;
      return { ...state.policy };
    },
  }, subscribe: () => () => {} };
  const modules = {};
  function load(name) {
    if (modules[name]) return modules[name];
    const context = { exports: {}, window: {}, TextEncoder, Error, setTimeout: (fn) => { const t = setTimeout(fn, 0); return t; }, clearTimeout: (t) => clearTimeout(t), setInterval: () => 1, clearInterval: () => {},
      require: (dependency) => dependency === "react" ? react : dependency === "./api" ? api : load(dependency.slice(2)) };
    const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, `../lib/${name}.ts`), "utf8"), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    }).outputText;
    vm.runInNewContext(code, context);
    return modules[name] = context.exports;
  }
  return { store: load("mandate"), writes, state, conflict: () => { conflict = true; }, settle: () => new Promise(setImmediate) };
}

test("gateway allocation, owner wallet metadata and exact formatting reach the command center", async () => {
  const c = model(); c.store.useMandate(); await c.settle();
  const state = c.store.useMandate();
  assert.equal(state.allocation, 1800);
  assert.equal(c.store.available(state), 1500);
  assert.equal(c.store.fmt(state.allocation), "1.800");
});

test("rapid policy edits fold into one versioned write and every caller gets the result", async () => {
  const c = model(); c.store.useMandate(); await c.settle(); await new Promise((r) => setTimeout(r, 20));
  const results = await Promise.all([c.store.setPolicy({ autoApprove: 80 }), c.store.setPolicy({ maxRetries: 3 })]);
  assert.equal(results.length, 2);
  assert.ok(results.every((result) => result.ok), JSON.stringify(results));
  assert.equal(c.writes.length, 1);
  assert.equal(c.writes[0].expected_version, 3);
  assert.equal(c.writes[0].max_single_spend, "0.080");
  assert.equal(c.writes[0].max_attempts, 3);
});
