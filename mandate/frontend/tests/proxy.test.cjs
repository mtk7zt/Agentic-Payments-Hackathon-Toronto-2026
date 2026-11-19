const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

function load() {
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, "../lib/proxy.ts"), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const context = {
    exports: {},
    require: (name) => {
      if (name === "./console-access") {
        return { requireApproved: async () => { throw new Error("session check should not run"); } };
      }
      throw new Error(`unexpected import ${name}`);
    },
    process, URL, Headers, Request, Response,
    fetch: async () => { throw new Error("gateway should not be called"); },
  };
  vm.runInNewContext(code, context);
  return context.exports;
}

test("console user paths are refused before the session or the gateway", async () => {
  const { proxyGateway, refusesConsoleUsers } = load();
  const blocked = [
    "/api/v1/console-users",
    "/api/v1/console-users/",
    "/api/v1/console-users/guest@example.com/approve",
    "/api/v1/console-users/guest@example.com/reject",
    "/API/v1/Console-Users?unused=1",
  ];
  for (const path of blocked) {
    assert.equal(refusesConsoleUsers(path), true);
    const response = await proxyGateway(new Request("http://localhost/x"), path);
    assert.equal(response.status, 403);
  }
  assert.equal(refusesConsoleUsers("/api/v1/state"), false);
  await assert.rejects(() => proxyGateway(new Request("http://localhost/x"), "/api/v1/state"));
});
