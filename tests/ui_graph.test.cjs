const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "src", "ddg", "ui", "static", "app.js"), "utf8");

class Element {
  constructor(tag = "div") {
    this.tag = tag;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.value = "";
    this.text = "";
  }
  get firstChild() { return this.children[0]; }
  get textContent() { return this.text + this.children.map((child) => child.textContent ?? child).join(""); }
  set textContent(value) { this.text = String(value); this.children = []; }
  append(...children) { this.children.push(...children); }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener() {}
  reportValidity() { return true; }
}

function node(id = "A") {
  return {
    id, key: id, type: "Account", properties: {}, provenance: [],
    connectivity: { degree: 3, neighbor_count: 2, edge_type_count: 2, in_degree: 1, out_degree: 2, edge_types: ["has", "owns"] },
  };
}

function harness(respond = async () => []) {
  const calls = [];
  const errors = [];
  const elements = new Map();
  const defaults = {
    "#graph-q": "", "#graph-type": "", "#graph-edge-type": "", "#graph-direction": "both",
    "#graph-neighbor-type": "", "#graph-min-degree": "0", "#graph-min-neighbors": "0",
    "#graph-min-edge-types": "0", "#graph-sort": "degree", "#graph-depth": "1",
  };
  const get = (selector) => {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  };
  for (const [selector, value] of Object.entries(defaults)) get(selector).value = value;
  get("#graph-search-form").reset = () => {
    for (const [selector, value] of Object.entries(defaults)) {
      if (selector !== "#graph-depth") get(selector).value = value;
    }
  };
  const context = vm.createContext({
    Node: Element,
    URLSearchParams,
    document: {
      addEventListener() {},
      querySelector: get,
      querySelectorAll: () => [],
      createElement: (tag) => new Element(tag),
      createTextNode: (text) => { const el = new Element("#text"); el.textContent = text; return el; },
    },
    async testApi(url) { calls.push(url); return respond(url); },
    testToast(message) { errors.push(message); },
  });
  vm.runInContext(source, context);
  vm.runInContext(`
    state.ws = "retail";
    state.graph = "demo.graph.json";
    wsApi = testApi;
    toast = testToast;
    ensureCy = () => null;
  `, context);
  return { calls, errors, get, run: (code) => vm.runInContext(code, context) };
}

test("node search submits connectivity filters and renders matching metrics", async () => {
  const app = harness(async () => [node()]);
  app.get("#graph-min-degree").value = "2";
  app.get("#graph-edge-type").value = "owns & has";
  app.get("#graph-direction").value = "out";
  app.get("#graph-neighbor-type").value = "Person";

  await app.run("graphSearch()");

  const url = new URL(app.calls[0], "http://localhost");
  assert.equal(url.searchParams.get("min_degree"), "2");
  assert.equal(url.searchParams.get("edge_type"), "owns & has");
  assert.equal(url.searchParams.get("direction"), "out");
  assert.equal(url.searchParams.get("neighbor_type"), "Person");
  assert.equal(url.searchParams.get("sort"), "degree");
  assert.match(app.get("#graph-results").textContent, /3 edges2 neighbors2 types/);
  assert.equal(app.get("#graph-search-status").textContent, "1 matching node.");
});

test("search reports its result cap rather than claiming all matches were returned", async () => {
  const app = harness(async () => Array.from({ length: 101 }, (_, i) => node(String(i))));

  await app.run("graphSearch()");

  assert.equal(app.get("#graph-results").children.length, 100);
  assert.match(app.get("#graph-search-status").textContent, /first 100 matches/);
});

test("empty results explain how to broaden connectivity filters", async () => {
  const app = harness();

  await app.run("graphSearch()");

  assert.match(app.get("#graph-results").textContent, /Lower the minimum counts or choose All nodes/);
});

test("failed search and expansion requests surface errors", async () => {
  const app = harness(async () => { throw new Error("test failure"); });

  await app.run("graphSearch()");
  await app.run('expandNode("A", true)');

  assert.match(app.get("#graph-search-status").textContent, /Search failed: test failure/);
  assert.match(app.get("#graph-view-status").textContent, /Unable to load neighborhood: test failure/);
  assert.deepEqual(app.errors, ["test failure", "test failure"]);
});

test("newer search results cannot be overwritten by a slow prior response", async () => {
  let release;
  let count = 0;
  const waiting = new Promise((resolve) => { release = resolve; });
  const app = harness(async () => ++count === 1 ? waiting : [node("new")]);
  const old = app.run("graphSearch()");

  await app.run("graphSearch()");
  release([node("old")]);
  await old;

  assert.match(app.get("#graph-results").textContent, /new/);
  assert.doesNotMatch(app.get("#graph-results").textContent, /old/);
});

test("a workspace or graph reset discards outstanding search responses", async () => {
  let release;
  const waiting = new Promise((resolve) => { release = resolve; });
  const app = harness(() => waiting);
  const pending = app.run("graphSearch()");

  app.run("resetGraphExplorer()");
  release([node()]);
  await pending;

  assert.equal(app.get("#graph-results").textContent, "");
  assert.equal(app.get("#graph-search-status").textContent, "");
});

test("connectivity shortcuts reset other filters and set the requested threshold", async () => {
  const app = harness();
  app.get("#graph-q").value = "unrelated search";
  app.get("#graph-edge-type").value = "owns";

  await app.run('graphPreset("hubs")');

  const url = new URL(app.calls[0], "http://localhost");
  assert.equal(url.searchParams.get("q"), "");
  assert.equal(url.searchParams.get("edge_type"), "");
  assert.equal(url.searchParams.get("min_neighbors"), "5");
  assert.equal(url.searchParams.get("sort"), "neighbors");
});

for (const [preset, minimum, sort] of [
  ["all", "0", "degree"], ["branching", "2", "degree"], ["varied", "2", "edge_types"],
]) {
  test(`the ${preset} shortcut applies the correct minimum and ordering`, async () => {
    const app = harness();
    app.get("#graph-min-degree").value = "99";
    app.get("#graph-min-edge-types").value = "99";

    await app.run(`graphPreset("${preset}")`);

    const params = new URL(app.calls[0], "http://localhost").searchParams;
    assert.equal(params.get(preset === "varied" ? "min_edge_types" : "min_degree"), minimum);
    assert.equal(params.get("sort"), sort);
  });
}

test("expansion carries edge filters and renders a text graph without Cytoscape", async () => {
  const app = harness(async () => ({
    nodes: [node("A"), node("B")],
    edges: [{ id: "e1", type: "owns", source: "A", target: "B", properties: {} }],
    degree: 1, total_degree: 3, truncated: true,
  }));
  app.get("#graph-edge-type").value = "owns";
  app.get("#graph-direction").value = "out";
  app.get("#graph-neighbor-type").value = "Person";

  await app.run('expandNode("A", true)');

  const url = new URL(app.calls[0], "http://localhost");
  assert.equal(url.searchParams.get("edge_type"), "owns");
  assert.equal(url.searchParams.get("direction"), "out");
  assert.equal(url.searchParams.get("neighbor_type"), "Person");
  assert.equal(url.searchParams.get("limit"), "200");
  assert.match(app.get("#graph-text").textContent, /Nodes \(2\).*Edges \(1\)/);
  assert.match(app.get("#graph-view-status").textContent, /limited to 200 nodes/);
  assert.match(app.get("#graph-detail").textContent, /3 total incident edges/);
});

test("a cleared graph cannot be repopulated by an outstanding expansion", async () => {
  let release;
  const waiting = new Promise((resolve) => { release = resolve; });
  const app = harness(() => waiting);
  const pending = app.run('expandNode("A", true)');

  app.run("clearGraphView()");
  release({ nodes: [node()], edges: [], degree: 0, total_degree: 0 });
  await pending;

  assert.equal(app.run("state.graphNodes.size"), 0);
  assert.equal(app.get("#graph-text").textContent, "");
});
