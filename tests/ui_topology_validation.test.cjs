const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "src", "ddg", "ui", "static", "app.js"), "utf8");

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.text = ""; }
  get textContent() { return this.text + this.children.map((child) => child.textContent ?? child).join(""); }
  set textContent(value) { this.text = String(value); }
  append(...children) { this.children.push(...children); }
  setAttribute(key, value) { this[key] = value; }
  addEventListener() {}
}

function harness(checked = true, discoveryScope = "all") {
  const calls = [];
  const values = new Map([["source", "retail"], ["mode", "exploratory"], ["discovery_scope", discoveryScope]]);
  if (checked) values.set("validate_topology", "on");
  const context = vm.createContext({
    Node: Element,
    document: {
      addEventListener() {},
      createElement: (tag) => new Element(tag),
      createTextNode: (text) => { const element = new Element("text"); element.textContent = text; return element; },
    },
    FormData: class {
      get(key) { return values.get(key) || null; }
      has(key) { return values.has(key); }
    },
    async testApi(url, options) { calls.push({url, options}); return {id: "run-validation"}; },
  });
  vm.runInContext(source, context);
  vm.runInContext("const renderRun = selectRun; wsApi = testApi; toast = () => {}; loadRuns = () => {}; selectRun = () => {};", context);
  return {calls, run: (code) => vm.runInContext(code, context)};
}

test("relationships-only is submitted independently of confidence mode and topology validation", async () => {
  const app = harness(true, "edges");

  await app.run("startRun({preventDefault() {}, target: {}})");

  const payload = JSON.parse(app.calls[0].options.body);
  assert.equal(payload.discovery_scope, "edges");
  assert.equal(payload.mode, "exploratory");
  assert.equal(payload.validate_topology, true);
});

for (const checked of [false, true]) {
  test(`run form submits validation=${checked} independently of exploratory mode`, async () => {
    const app = harness(checked);

    await app.run("startRun({preventDefault() {}, target: {}})");

    const payload = JSON.parse(app.calls[0].options.body);
    assert.equal(payload.validate_topology, checked);
    assert.equal(payload.mode, "exploratory");
  });
}

test("topology report renders recommendations, evidence, and untested constraints as text", () => {
  const app = harness();
  const report = app.run(`topologyFindingsView({
    validation_targets: ["node:Customer", "constraint:Every order has a customer"],
    topology_findings: [{
      element: "node:Customer", aspect: "key", status: "mismatch",
      finding: "<script>unsafe source name</script>", recommendation: "Use a composite key",
      source: "retail", topology_version: "v1", limitations: ["Confirm business meaning"],
      evidence_ids: ["ev-1"], evidence: [{id: "ev-1", tool: "check_uniqueness", scope: "full",
        result: {unique: false, distinct_keys: 2, duplicate_keys: 1, rows_with_null_key: 0}}],
    }],
  })`);

  assert.match(report.textContent, /Recommendation: Use a composite key/);
  assert.match(report.textContent, /Not yet tested: constraint:Every order has a customer/);
  assert.match(report.textContent, /ev-1/);
  assert.match(report.textContent, /<script>unsafe source name<\/script>/);
  assert.match(report.textContent, /Advisory only/);
});

test("empty reports explicitly describe incomplete validation", () => {
  const app = harness();

  const report = app.run("topologyFindingsView({topology_findings: []})");

  assert.match(report.textContent, /No validation findings recorded yet/);
  assert.match(report.textContent, /may be incomplete/);
});

for (const files of [["context/uroj.md", "context/prior/<reference>.MD"], [], null]) {
  test(`run details show the loaded context manifest: ${JSON.stringify(files)}`, async () => {
    const app = harness();
    app.run(`
      const panel = new Node("section");
      panel.classList = {remove() {}};
      panel.scrollIntoView = () => {};
      document.querySelector = () => panel;
      clear = (element) => {element.children = []; return element;};
      wsApi = async () => ({
        id: "run-context", status: "completed", planner: "heuristic", events: [], iterations: [],
        ${files === null ? "" : `context_files: ${JSON.stringify(files)},`}
      });
    `);

    await app.run('renderRun("run-context")');
    const text = app.run("panel.textContent");

    if (files === null) {
      assert.doesNotMatch(text, /Context references loaded/);
    } else {
      assert.match(text, /Context references loaded/);
      assert.match(text, /offline planners do not/);
      if (files.length) {
        for (const file of files) assert.ok(text.includes(file));
      } else {
        assert.match(text, /No additional Markdown references found/);
      }
    }
  });
}
