const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
  get kind() { return this.status === 0 ? "offline" : this.status === 401 || this.status === 403 ? "auth" : "server"; }
}

function model() {
  const c = { fail: null, callbacks: null, credentials: [], intervals: 0 };
  const fixture = { policy: { version: 1, max_single_spend: 100, transaction_ceiling: 2000,
    strict_whitelist: true, whitelisted_sellers: {}, max_attempts: 2, paused: false, hash: "policy" },
    purchases: [], balances: { budget: 5000, held: 40, settled: 60 }, session_keys: [] };
  const sim = { getState: () => ({ held: -1 }), subscribeLedger: () => () => {}, run: () => ({ ok: true }) };
  const api = { API_URL: "http://localhost:8000", ApiError,
    kindOf: (e) => (e instanceof ApiError ? e.kind : "offline"), describe: (e) => String(e && e.message),
    setOwnerCredential: (v) => c.credentials.push(v),
    api: { state: async () => { if (c.fail) throw c.fail; return structuredClone(fixture); }, wallet: async () => null },
    subscribe: (...args) => { c.callbacks = args; return () => { c.callbacks = null; }; } };
  const context = { exports: {}, window: {}, setTimeout: () => 1, clearTimeout: () => {},
    setInterval: () => ++c.intervals, clearInterval: () => {},
    require: (name) => name === "react" ? { useSyncExternalStore: (subscribe, snapshot) => { subscribe(() => {}); return snapshot(); } }
      : name === "./api" ? api : name === "./ledger" ? sim : {} };
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, "../lib/mandate.ts"), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInNewContext(code, context);
  c.store = context.exports;
  return c;
}

const tick = () => new Promise(setImmediate);

test("a connected cockpit opens the authenticated stream and keeps polling as the fallback", async () => {
  const c = model(); c.store.useMandate(); await tick();
  assert.equal(c.store.useMode(), "live");
  assert.equal(c.store.useMandate().held, 40);
  assert.ok(c.callbacks, "stream subscription opened");
  assert.equal(c.intervals, 1, "poller still running");
  c.callbacks[1]("open"); await tick();
  assert.equal(c.store.useStreaming(), true);
  c.callbacks[1]("stale"); await tick();
  assert.equal(c.store.useStreaming(), false);
  assert.equal(c.store.useMode(), "live", "a dropped stream alone is not an outage");
});

test("an auth refusal asks for sign-in, and signing in keeps the credential in memory only", async () => {
  const c = model(); c.fail = new ApiError(401, "owner token required");
  c.store.useMandate(); await tick();
  assert.equal(c.store.useMode(), "signin");
  assert.equal(c.callbacks, null, "no stream without an owner");
  c.fail = null;
  const result = await c.store.connectOwner("synthetic");
  assert.equal(result.ok, true);
  assert.deepEqual(c.credentials, ["synthetic"]);
  assert.equal(c.store.useMode(), "live");
  assert.ok(c.callbacks);
});

test("a failed sign-in reports the failure and stays out of live mode", async () => {
  const c = model(); c.fail = new ApiError(403, "bad token");
  c.store.useMandate(); await tick();
  const result = await c.store.connectOwner("wrong");
  assert.equal(result.ok, false);
  assert.equal(c.store.useMode(), "signin");
});
