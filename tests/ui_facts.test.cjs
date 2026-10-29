const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "src", "ddg", "ui", "static", "app.js"), "utf8");

class Element {
  constructor(tag = "div") { this.tag = tag; this.children = []; this.events = {}; this.value = ""; this.text = ""; }
  get firstChild() { return this.children[0]; }
  get textContent() { return this.text + this.children.map((c) => c.textContent ?? c).join(""); }
  set textContent(value) { this.text = String(value); this.children = []; }
  append(...children) { this.children.push(...children); }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  setAttribute(key, value) { this[key] = ["disabled", "checked"].includes(key) ? true : value; }
  addEventListener(name, callback) { this.events[name] = callback; }
  close() { this.open = false; }
  showModal() { this.open = true; }
  reset() {}
}

const fact = {
  id: "fact-1", kind: "node_mapping", hypothesis: "Customer records", mapping: {
    node_type: "Customer", source_table: "customers", key_columns: ["id"],
  },
  approved_by: "sme", approved_at: "2026-01-01", limitations: ["Evidence predates this review."],
};
const check = { token: "fact-token", needs_revalidation: true, can_revalidate: true, errors: [] };
const report = {
  topology_version: "current", source_reachable: true, issues: {"fact-1": ["older topology"]},
  validations: {"fact-1": check}, facts: [fact],
};

function harness(respond) {
  const elements = new Map();
  const calls = [];
  const errors = [];
  const get = (selector) => {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  };
  get("#facts-source").value = "retail";
  const context = vm.createContext({
    Node: Element,
    document: {
      addEventListener() {}, querySelector: get,
      createElement: (tag) => new Element(tag),
      createTextNode: (text) => { const el = new Element(); el.textContent = text; return el; },
    },
    localStorage: { getItem() { return ""; }, setItem() {} },
    async testApi(url, options) {
      calls.push({url, options});
      if (respond) return respond(url, options);
      if (url.startsWith("/facts/revalidation")) return report;
      if (url.startsWith("/facts?")) return [fact];
      return [];
    },
    testToast(message) { errors.push(message); },
  });
  vm.runInContext(source, context);
  vm.runInContext(`state.ws = "retail"; state.facts = [${JSON.stringify(fact)}]; wsApi = testApi; toast = testToast;`, context);
  return { calls, errors, get, run: (code) => vm.runInContext(code, context) };
}

test("fact review shows actionable controls and keeps opaque versions in technical details", async () => {
  const app = harness();

  await app.run("loadFacts()");

  const table = app.get("#facts-table tbody");
  assert.match(table.textContent, /Review old approval/);
  assert.match(table.textContent, /older graph definition/);
  assert.match(app.get("#revalidation").textContent, /Technical version details/);
  assert.match(app.get("#facts-selection-status").textContent, /1 mappings selected; 0 left out/);
});

test("unchecking a fact changes build selection without mutating its approval", async () => {
  const app = harness();
  await app.run("loadFacts()");
  const include = app.get("#facts-table tbody").children[0].children[0].children[0];

  include.events.change({target: {checked: false}});

  assert.equal(app.run('state.factExclusions.has("fact-1")'), true);
  assert.match(app.get("#facts-selection-status").textContent, /0 mappings selected; 1 left out/);
  assert.match(app.get("#codegen-result").textContent, /Build selection changed/);
  assert.equal(app.calls.filter((call) => call.options?.method === "POST").length, 0);
});

test("source changes clear old exclusions before loading the new source", async () => {
  const app = harness();
  await app.run("loadFacts()");
  app.run('state.factExclusions.add("fact-1")');
  app.get("#facts-source").value = "other";

  await app.run("loadFacts()");

  assert.equal(app.run("state.factExclusions.size"), 0);
});

test("an invalid mapping displays its correction reason and disables renewing approval", async () => {
  const invalid = {...report, validations: {"fact-1": {...check, can_revalidate: false, errors: ["Endpoint needs two ID parts."]}}};
  const app = harness(async (url) => url.startsWith("/facts/revalidation") ? invalid : url.startsWith("/facts?") ? [fact] : []);

  await app.run("loadFacts()");

  const row = app.get("#facts-table tbody").children[0];
  assert.match(row.textContent, /Endpoint needs two ID parts/);
  assert.match(row.textContent, /A new approval cannot fix this mapping/);
  assert.equal(row.children[4].children[0].disabled, true);
});

test("generation sends only explicit exclusions and explains how to recover from failures", async () => {
  const app = harness(async () => { const e = new Error("blocked"); e.body = {errors: ["fact-1: invalid endpoint"]}; throw e; });
  app.run('state.factExclusions.add("fact-1")');

  await app.run("generateCode()");

  const body = JSON.parse(app.calls[0].options.body);
  assert.deepEqual(body.exclude_fact_ids, ["fact-1"]);
  assert.match(app.get("#codegen-result").textContent, /Nothing is excluded automatically/);
  assert.match(app.get("#codegen-result").textContent, /fact-1: invalid endpoint/);
});

test("revalidation sends explicit reviewer confirmation and current review tokens", async () => {
  const app = harness(async (url) => {
    if (url.endsWith("/revalidate")) return {...fact, revision: 2};
    if (url.startsWith("/facts/revalidation")) return report;
    if (url.startsWith("/facts?")) return [fact];
    return [];
  });
  app.run(`openFactRevalidation(${JSON.stringify(fact)}, ${JSON.stringify(check)}, "current")`);
  app.get("#fact-revalidation-reviewer").value = "reviewer";
  app.get("#fact-revalidation-notes").value = "Reviewed the current business definition.";
  app.get("#fact-revalidation-confirm").checked = true;

  await app.run("submitFactRevalidation({preventDefault() {}})");

  const body = JSON.parse(app.calls.find((call) => call.url.endsWith("/revalidate")).options.body);
  assert.equal(body.confirm_meaning, true);
  assert.equal(body.expected_token, "fact-token");
  assert.equal(body.expected_topology_version, "current");
  assert.equal(body.reviewer, "reviewer");
  assert.match(app.errors[0], /Data evidence was not rerun/);
  assert.equal(app.get("#fact-revalidation-dialog").open, false);
});

test("revalidation failures stay in the review dialog without a success notification", async () => {
  const app = harness(async () => { const e = new Error("conflict"); e.body = {errors: ["Mapping changed; reload."]}; throw e; });
  app.run(`openFactRevalidation(${JSON.stringify(fact)}, ${JSON.stringify(check)}, "current")`);

  await app.run("submitFactRevalidation({preventDefault() {}})");

  assert.equal(app.get("#fact-revalidation-dialog").open, true);
  assert.equal(app.get("#fact-revalidation-error").textContent, "Mapping changed; reload.");
  assert.equal(app.get("#fact-revalidation-form button[type=submit]").disabled, false);
  assert.equal(app.errors.length, 0);
});

test("outstanding fact responses are discarded after switching workspaces", async () => {
  let release;
  const waiting = new Promise((resolve) => { release = resolve; });
  const app = harness(() => waiting);
  const loading = app.run("loadFacts()");

  app.run('state.ws = "other"');
  release([]);
  await loading;

  assert.equal(app.get("#facts-table tbody").textContent, "");
});

test("generation is blocked while facts from the selected source are still loading", async () => {
  const app = harness();
  app.run("state.factsLoading = true");

  await app.run("generateCode()");

  assert.equal(app.calls.length, 0);
  assert.match(app.errors[0], /finish loading/);
});

test("a review cannot be opened for a fact no longer present in the current source", () => {
  const app = harness();
  app.run("state.facts = []");

  app.run(`openFactRevalidation(${JSON.stringify(fact)}, ${JSON.stringify(check)}, "current")`);

  assert.equal(app.run("state.factReview"), null);
  assert.notEqual(app.get("#fact-revalidation-dialog").open, true);
});
