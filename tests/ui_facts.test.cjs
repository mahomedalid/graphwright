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
  classList = { toggle() {} };
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

test("review Markdown exports every status and complete facts with safe fences", () => {
  const app = harness();
  app.run(`globalThis.exportCandidates = [
    {id: "candidate-rejected", status: "rejected", kind: "edge_mapping", scope: {source: "other"},
      rationale: "<script>unsafe</script> \`\`\`json", decisions: [{decision: "reject", notes: "missing endpoint"}]},
    {id: "candidate-pending", status: "pending", evidence: [{result: {coverage: 0.9}}]},
    {id: "candidate-approved", status: "approved", hypothesis: "<b>record</b>"}
  ];`);

  const markdown = app.run('reviewMarkdown("retail", exportCandidates, [{...state.facts[0], candidate_id: "candidate-approved"}], "2026-10-30T12:00:00Z")');

  assert.match(markdown, /^---\ntitle: "retail review and facts export"/);
  assert.match(markdown, /Candidates: 3\. Approved facts: 1\./);
  for (const status of ["pending", "rejected", "approved"]) assert.match(markdown, new RegExp(`Status: ${status}`));
  assert.match(markdown, /missing endpoint/);
  assert.match(markdown, /"coverage": 0.9/);
  assert.match(markdown, /"candidate_id": "candidate-approved"/);
  assert.match(markdown, /````json\n[\s\S]*````/);
  assert.match(markdown, /\\<b\\>record\\<\/b\\>/);
  assert.match(markdown, /"source": "other"/);
  assert.match(markdown, /Deleted facts and archived records/);
});

test("empty review export remains a valid Markdown document", () => {
  const app = harness();
  const markdown = app.run('reviewMarkdown("retail", [], [], "2026-10-30T12:00:00Z")');
  assert.match(markdown, /Candidates: 0\. Approved facts: 0\./);
  assert.match(markdown, /No review candidates/);
  assert.match(markdown, /No approved facts/);
});

test("review export ignores filters, captures workspace, and restores button state", async () => {
  const app = harness();
  app.run(`
    globalThis.exportCalls = [];
    globalThis.exportDownloads = [];
    globalThis.Blob = class { constructor(parts, options) { this.parts = parts; this.options = options; } };
    api = async (url) => {
      exportCalls.push(url);
      return url.endsWith("/candidates") ? [{id: "rejected", status: "rejected"}] : state.facts;
    };
    downloadBlob = (blob, filename) => exportDownloads.push({blob, filename});
  `);
  app.get("#review-status").value = "pending";
  app.get("#review-tier").value = "high";

  await app.run("exportReviewMarkdown()");

  assert.deepEqual(Array.from(app.run("exportCalls")), ["/api/w/retail/candidates", "/api/w/retail/facts"]);
  assert.equal(app.run("exportDownloads[0].filename"), "retail-review-facts.md");
  assert.equal(app.run("exportDownloads[0].blob.options.type"), "text/markdown;charset=utf-8");
  assert.match(app.run("exportDownloads[0].blob.parts[0]"), /Status: rejected/);
  assert.equal(app.get("#review-export").disabled, false);
  assert.match(app.errors[0], /Exported 1 candidates and 1 approved facts/);
});

test("failed review export reports errors without downloading a partial report", async () => {
  const app = harness();
  app.run(`
    globalThis.exportDownloads = [];
    api = async () => { throw new Error("Access denied"); };
    downloadBlob = (...args) => exportDownloads.push(args);
  `);

  await app.run("exportReviewMarkdown()");

  assert.equal(app.run("exportDownloads.length"), 0);
  assert.match(app.errors[0], /Markdown export failed: Access denied/);
  assert.equal(app.get("#review-export").disabled, false);
});

test("workspace switch during review export blocks downloading stale workspace records", async () => {
  const app = harness();
  app.run(`
    globalThis.exportDownloads = [];
    api = async () => { state.ws = "other"; return []; };
    downloadBlob = (...args) => exportDownloads.push(args);
  `);

  await app.run("exportReviewMarkdown()");

  assert.equal(app.run("exportDownloads.length"), 0);
  assert.match(app.errors[0], /Workspace changed during export/);
  assert.equal(app.get("#review-export").disabled, false);
});

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

test("builder commands expose JSON, SQLite and Neo4j choices while preserving older manifests", () => {
  const app = harness();
  const choices = app.run(`builderCommands({suggested_commands: {
    json: "python builder.py --out graph.json",
    sqlite: "python builder.py --sqlite-out graph.sqlite",
    cypher: "python builder.py --cypher graph.cypher"
  }})`);

  assert.match(choices.textContent, /JSON snapshot/);
  assert.match(choices.textContent, /SQLite graph database/);
  assert.match(choices.textContent, /Neo4j Cypher import/);
  assert.match(choices.textContent, /--sqlite-out graph.sqlite/);
  assert.match(app.run('builderCommands({suggested_command: "legacy command"})').textContent, /legacy command/);
});

test("notebook target exposes bindings and disables only the standalone stale bypass", () => {
  const app = harness();
  app.get("#codegen-target").value = "databricks-notebooks";
  app.run("updateCodegenTarget()");
  assert.equal(app.get("#allow-stale").disabled, true);
  assert.match(app.get("#generate-btn").textContent, /Generate notebook package/);

  app.get("#codegen-target").value = "standalone";
  app.run("updateCodegenTarget()");
  assert.equal(app.get("#allow-stale").disabled, false);
  assert.match(app.get("#generate-btn").textContent, /Generate graph builder code/);
});

test("notebook generation forwards bindings and exclusions without bypassing stale approvals", async () => {
  const app = harness(async () => { const error = new Error("blocked"); error.body = {errors: ["Missing source binding"]}; throw error; });
  app.get("#codegen-target").value = "databricks-notebooks";
  app.get("#table-bindings").value = '{"customers":"catalog.schema.customers"}';
  app.get("#allow-stale").checked = true;
  app.run('state.factExclusions.add("fact-1")');

  await app.run("generateCode()");

  const body = JSON.parse(app.calls[0].options.body);
  assert.equal(body.target, "databricks-notebooks");
  assert.equal(body.allow_stale, false);
  assert.deepEqual(body.table_bindings, {customers: "catalog.schema.customers"});
  assert.deepEqual(body.exclude_fact_ids, ["fact-1"]);
  assert.match(app.get("#codegen-result").textContent, /supply explicit JSON overrides/);
  assert.match(app.get("#codegen-result").textContent, /Missing source binding/);
});

test("notebook generation omits optional bindings when the override input is blank", async () => {
  const app = harness(async () => { throw new Error("blocked"); });
  app.get("#codegen-target").value = "databricks-notebooks";

  await app.run("generateCode()");

  assert.equal(JSON.parse(app.calls[0].options.body).table_bindings, undefined);
});

test("malformed notebook overrides fail locally with a correction message", async () => {
  for (const value of ["broken", "[]", "null", '{"customers":3}']) {
    const app = harness();
    app.get("#codegen-target").value = "databricks-notebooks";
    app.get("#table-bindings").value = value;

    await app.run("generateCode()");

    assert.equal(app.calls.length, 0);
    assert.match(app.get("#codegen-result").textContent, /Correct the JSON/);
  }
});

test("standalone generation ignores notebook-only form values and keeps legacy commands", async () => {
  const app = harness(async () => { throw new Error("blocked"); });
  app.get("#codegen-target").value = "standalone";
  app.get("#table-bindings").value = "not valid JSON";
  app.get("#allow-stale").checked = true;

  await app.run("generateCode()");

  const body = JSON.parse(app.calls[0].options.body);
  assert.equal(body.target, "standalone");
  assert.equal(body.allow_stale, true);
  assert.equal(body.table_bindings, undefined);
});

test("notebook results expose package download and bindings rather than Python commands", async () => {
  const app = harness(async (url, options) => {
    if (options?.method === "POST") return {
      target: "databricks-notebooks", builder: "notebooks.zip", path: "notebooks.zip",
      warnings: [], source_bindings: {customers: "catalog.schema.customers"},
    };
    if (url.startsWith("/facts/revalidation")) return report;
    return [];
  });
  app.run('state.factsSourceKey = "retail:retail"');
  app.get("#codegen-target").value = "databricks-notebooks";
  await app.run("generateCode()");
  const text = app.get("#codegen-result").textContent;
  assert.match(text, /Download notebook ZIP/);
  assert.match(text, /Nothing was executed/);
  assert.match(text, /extract the ZIP/);
  assert.match(text, /four Python source notebooks into the same Databricks folder/);
  assert.match(text, /catalog.schema.customers/);
  assert.doesNotMatch(text, /View code|JSON snapshot|undefined/);
});

test("previous notebook artifacts retain their warnings and resolved bindings", async () => {
  const app = harness(async (url) => {
    if (url.startsWith("/facts/revalidation")) return report;
    return [{
      target: "databricks-notebooks", builder: "notebooks.zip", source: "retail", facts: [],
      warnings: ["Full-data validation still required"],
      source_bindings: {customers: "catalog.schema.customers"},
    }];
  });

  await app.run("loadFacts()");

  const text = app.get("#generated-list").textContent;
  assert.match(text, /Full-data validation still required/);
  assert.match(text, /Resolved source bindings/);
  assert.match(text, /catalog.schema.customers/);
  assert.match(text, /Download notebook ZIP/);
});

test("previous notebook package download stays scoped and offers actionable retry errors", async () => {
  const app = harness();
  app.run('downloadNotebookPackage = async (builder, workspace) => { if (workspace !== "retail") throw new Error("wrong workspace"); throw new Error("package missing"); }');
  const controls = app.run('builderCommands({target: "databricks-notebooks", builder: "notebooks.zip"})');
  app.run('state.ws = "other"');

  await controls.children[0].events.click();

  assert.equal(controls.children[0].disabled, false);
  assert.match(controls.textContent, /package missing/);
  assert.match(controls.textContent, /generate it again in its original workspace/);
  assert.doesNotMatch(controls.textContent, /wrong workspace/);
});

test("ZIP responses use authenticated binary fetch rather than the JSON or text parser", async () => {
  const app = harness();
  app.run(`
    localStorage.getItem = () => "download-token";
    fetch = async (path, options) => {
      globalThis.downloadFetch = {path, options};
      return {
        ok: true, status: 200,
        headers: {get: () => "application/zip"},
        blob: async () => ({kind: "zip-blob"}),
        text: async () => { throw new Error("ZIP decoded as text"); }
      };
    };
  `);

  const blob = await app.run('api("/api/w/retail/codegen/notebooks.zip/download")');

  assert.equal(blob.kind, "zip-blob");
  assert.equal(app.run("downloadFetch.options.headers.Authorization"), "Bearer download-token");
  assert.equal(app.run("downloadFetch.path"), "/api/w/retail/codegen/notebooks.zip/download");
});

test("failed package downloads preserve the API error for actionable retry feedback", async () => {
  const app = harness();
  app.run(`
    fetch = async () => ({
      ok: false, status: 404,
      headers: {get: () => "application/json"},
      json: async () => ({detail: "Generated notebook package not found"})
    });
  `);

  await assert.rejects(app.run('api("/api/w/retail/codegen/missing.zip/download")'),
    /Generated notebook package not found/);
});

test("switching sources clears notebook binding overrides", async () => {
  const app = harness();
  await app.run("loadFacts()");
  app.get("#table-bindings").value = '{"customers":"other.schema.customers"}';
  app.get("#facts-source").value = "other";
  await app.run("loadFacts()");
  assert.equal(app.get("#table-bindings").value, "");
});

test("uncorrectable facts still offer deletion even when revalidation is disabled", async () => {
  const invalid = {...report, validations: {"fact-1": {...check, can_revalidate: false, errors: ["Invalid key"]}}};
  const app = harness(async (url) => url.startsWith("/facts/revalidation") ? invalid : []);

  await app.run("loadFacts()");
  const actions = app.get("#facts-table tbody").children[0].children[4];

  assert.match(actions.textContent, /Delete fact/);
  assert.equal(actions.children[0].disabled, true);
  assert.notEqual(actions.children[1].disabled, true);
});

test("deletion requires the exact fact ID, reviewer, and a reason before sending any request", async () => {
  const app = harness();
  app.run(`openFactDeletion(${JSON.stringify(fact)}, "fact-token")`);
  app.get("#fact-deletion-reviewer").value = "reviewer";
  app.get("#fact-deletion-notes").value = "Incorrect meaning.";
  app.get("#fact-deletion-confirm").value = "wrong-id";

  await app.run("submitFactDeletion({preventDefault() {}})");

  assert.equal(app.calls.length, 0);
  assert.match(app.get("#fact-deletion-error").textContent, /exact fact ID/);
  assert.equal(app.get("#fact-deletion-dialog").open, true);
});

test("confirmed deletion sends a token and refreshes facts without rewriting previous builders", async () => {
  let deleted = false;
  const app = harness(async (url, options) => {
    if (options?.method === "DELETE") { deleted = true; return {deleted: true, dependent_fact_ids: []}; }
    if (url.startsWith("/facts/revalidation")) return {...report, facts: deleted ? [] : [fact]};
    return [];
  });
  await app.run("loadFacts()");
  app.run(`openFactDeletion(${JSON.stringify(fact)}, "fact-token")`);
  app.get("#fact-deletion-reviewer").value = "reviewer";
  app.get("#fact-deletion-notes").value = "Cannot establish business identity.";
  app.get("#fact-deletion-confirm").value = "fact-1";

  await app.run("submitFactDeletion({preventDefault() {}})");

  const request = app.calls.find((call) => call.options?.method === "DELETE");
  assert.equal(request.url, "/facts/fact-1");
  assert.deepEqual(JSON.parse(request.options.body), {
    reviewer: "reviewer", notes: "Cannot establish business identity.", confirm: "fact-1", expected_token: "fact-token",
  });
  assert.equal(app.get("#fact-deletion-dialog").open, false);
  assert.equal(app.run("state.facts.length"), 0);
  assert.match(app.get("#codegen-result").textContent, /Previous builders and graph snapshots are unchanged/);
  assert.match(app.errors[0], /audit history retained/);
});

test("a stale deletion remains open and explicitly asks for a fresh review", async () => {
  const app = harness(async () => { const error = new Error("The fact changed."); error.status = 409; throw error; });
  app.run(`openFactDeletion(${JSON.stringify(fact)}, "old-token")`);
  app.get("#fact-deletion-reviewer").value = "reviewer";
  app.get("#fact-deletion-notes").value = "Cannot repair.";
  app.get("#fact-deletion-confirm").value = fact.id;

  await app.run("submitFactDeletion({preventDefault() {}})");

  assert.equal(app.get("#fact-deletion-dialog").open, true);
  assert.match(app.get("#fact-deletion-error").textContent, /recheck mappings/);
  assert.equal(app.errors.length, 0);
  assert.equal(app.get("#fact-deletion-form button[type=submit]").disabled, false);
});

test("deletion cannot be submitted after switching source or for a missing fact", async () => {
  const app = harness();
  app.run(`openFactDeletion(${JSON.stringify(fact)}, "fact-token")`);
  app.get("#facts-source").value = "other";

  await app.run("submitFactDeletion({preventDefault() {}})");

  assert.equal(app.calls.length, 0);
  app.run("state.factDeletion = null; state.facts = [];");
  app.run(`openFactDeletion(${JSON.stringify(fact)}, "fact-token")`);
  assert.equal(app.run("state.factDeletion"), null);
});

test("node deletion previews dependent facts and clearly states there is no cascade", () => {
  const app = harness();
  app.run(`state.facts.push({id:"fact-edge", kind:"edge_mapping", hypothesis:"Customer places order",
    mapping:{from_node_type:"Customer", to_node_type:"Order"}});
    openFactDeletion(${JSON.stringify(fact)}, "fact-token");`);

  assert.match(app.get("#fact-deletion-preview").textContent, /fact-edge/);
  assert.match(app.get("#fact-deletion-preview").textContent, /remain for separate review/);
  assert.equal(app.calls.length, 0);
});
