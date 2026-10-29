const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "src", "ddg", "ui", "static", "app.js"), "utf8");

function harness({ listing = async () => [{ id: "run-1", status: "running" }], detail = async () => {} } = {}) {
  const timers = new Map();
  const calls = [];
  const errors = [];
  let nextTimer = 0;
  const context = vm.createContext({
    document: { addEventListener() {} },
    setTimeout(callback, delay) {
      const id = ++nextTimer;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    async testApi() { calls.push("runs"); return listing(); },
    async testDetail() { calls.push("detail"); return detail(); },
    async testBadge() { calls.push("badge"); },
    testToast(message) { errors.push(message); },
  });
  vm.runInContext(source, context);
  vm.runInContext(`
    state.ws = "retail";
    state.selectedRun = "run-1";
    wsApi = testApi;
    selectRun = testDetail;
    refreshBadge = testBadge;
    toast = testToast;
    h = () => ({});
    pill = () => ({});
    clear = () => ({ append() {} });
    document.querySelector = () => ({});
  `, context);
  return { context, timers, calls, errors, load: () => vm.runInContext("loadRuns()", context) };
}

test("active runs poll every 5000ms after refreshing details and badge", async () => {
  const app = harness();
  await app.load();
  assert.deepEqual(app.calls, ["runs", "detail", "badge"]);
  assert.equal(app.timers.size, 1);
  assert.equal([...app.timers.values()][0].delay, 5000);
});

test("slow detail requests complete before the next poll is scheduled", async () => {
  let release;
  const waiting = new Promise((resolve) => { release = resolve; });
  const app = harness({ detail: () => waiting });
  const refresh = app.load();
  await new Promise(setImmediate);
  assert.deepEqual(app.calls, ["runs", "detail"]);
  assert.equal(app.timers.size, 0);
  release();
  await refresh;
  assert.deepEqual(app.calls, ["runs", "detail", "badge"]);
  assert.equal(app.timers.size, 1);
});

test("failed refreshes are visible and retried after 5000ms", async () => {
  let attempts = 0;
  const app = harness({
    listing: async () => {
      if (++attempts === 1) throw new Error("temporary connection failure");
      return [{ id: "run-1", status: "completed" }];
    },
  });
  await app.load();
  assert.match(app.errors[0], /temporary connection failure/);
  const retry = [...app.timers.values()][0];
  assert.equal(retry.delay, 5000);
  await retry.callback();
  assert.equal(attempts, 2);
  assert.equal(app.timers.size, 0);
});

test("completed runs receive a final detail refresh without further polling", async () => {
  const app = harness({ listing: async () => [{ id: "run-1", status: "completed" }] });
  await app.load();
  assert.deepEqual(app.calls, ["runs", "detail", "badge"]);
  assert.equal(app.timers.size, 0);
});

test("a workspace switch discards an outstanding response and its polling timer", async () => {
  let release;
  const waiting = new Promise((resolve) => { release = resolve; });
  const app = harness({ listing: () => waiting });
  const refresh = app.load();
  vm.runInContext('state.ws = "telecom"', app.context);
  release([{ id: "run-1", status: "running" }]);
  await refresh;
  assert.deepEqual(app.calls, ["runs"]);
  assert.equal(app.timers.size, 0);
});
