const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

function client() {
  const sources = [], requests = [], timers = new Map();
  let id = 0;
  class Source {
    constructor(url, options) { this.url = url; this.options = options; this.handlers = {}; sources.push(this); }
    addEventListener(name, fn) { this.handlers[name] = fn; }
    close() { this.closed = true; }
    event(seq) { this.handlers["ledger.appended"]({ data: JSON.stringify({ seq }) }); }
  }
  const context = {
    exports: {}, process: { env: { NEXT_PUBLIC_MANDATE_API_URL: "http://localhost:8000" } },
    AbortController, EventSource: Source,
    setTimeout: (fn, ms) => { timers.set(++id, { fn, ms }); return id; },
    clearTimeout: (key) => timers.delete(key),
    fetch: async (url, options) => { requests.push({ url, options }); return { ok: true, json: async () => ({ expires_in: 60 }) }; },
  };
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, "../lib/api.ts"), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInNewContext(code, context);
  return { api: context.exports, sources, requests, timers, async settle() { await new Promise(setImmediate); },
    async retry() { const timer = [...timers.values()].find((t) => t.ms === 2000); assert.ok(timer); timers.clear(); await timer.fn(); } };
}

test("owner header stays out of URLs; SSE uses cookie credentials and renews from last cursor", async () => {
  const c = client();
  c.api.setOwnerCredential("synthetic-owner-credential");
  const events = [], statuses = [];
  const stop = c.api.subscribe((event) => events.push(event.seq), (status) => statuses.push(status));
  await c.settle();
  assert.equal(c.requests[0].options.headers["X-Owner-Token"], "synthetic-owner-credential");
  assert.equal(c.requests[0].options.credentials, "include");
  assert.equal(c.sources[0].options.withCredentials, true);
  assert.ok(!c.sources[0].url.includes("credential"));
  c.sources[0].onopen(); c.sources[0].event(1); c.sources[0].event(2);
  c.sources[0].onerror();
  assert.equal(statuses.at(-1), "stale");
  assert.equal(c.sources[0].closed, true);
  await c.retry(); await c.settle();
  assert.equal(c.requests.length, 2);
  assert.ok(c.sources[1].url.endsWith("after_seq=2"));
  c.sources[1].event(3);
  assert.deepEqual(events, [1, 2, 3]);
  stop(); assert.equal(c.sources[1].closed, true);
});

test("gap causes state-refresh callback and replay from zero; expiry preserves cursor", async () => {
  const c = client(); let resets = 0;
  const stop = c.api.subscribe(() => {}, () => {}, () => resets++);
  await c.settle();
  c.sources[0].event(1);
  c.sources[0].handlers["session.expired"]();
  await c.retry(); await c.settle();
  assert.ok(c.sources[1].url.endsWith("after_seq=1"));
  c.sources[1].event(3);
  assert.equal(resets, 1);
  await c.retry(); await c.settle();
  assert.ok(c.sources[2].url.endsWith("after_seq=0"));
  c.sources[2].handlers["stream.reset"]();
  assert.equal(resets, 2);
  stop(); assert.equal(c.timers.size, 0);
});
