"use strict";

// ---------- helpers ----------
const $ = (sel) => document.querySelector(sel);

// Builds DOM nodes with textContent only, so untrusted source data can never inject markup.
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "className") el.className = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
// Like Element.append, but flattens arrays and skips null/false (native append would render them as text).
function appendAll(el, ...children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}
function pill(text) { return h("span", { className: `pill ${text}` }, text); }
function fmtTime(iso) { return iso ? new Date(iso).toLocaleString() : ""; }
function json(value) { return h("pre", {}, JSON.stringify(value, null, 2)); }

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast${isError ? " error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.add("hidden"), 5000);
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const token = localStorage.getItem("ddg-token");
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    const entered = prompt("API token required");
    if (entered) { localStorage.setItem("ddg-token", entered); return api(path, options); }
  }
  const body = response.headers.get("content-type")?.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    const err = new Error(typeof body === "object" ? (body.detail || JSON.stringify(body)) : body);
    err.status = response.status;
    err.body = body;
    throw err;
  }
  return body;
}

const state = { status: null, ws: null, wsStatus: null, workspaces: [], selectedRun: null, selectedCandidate: null, cy: null, graph: null };

function wsApi(path, options) {
  if (!state.ws) return Promise.reject(new Error("No workspace selected"));
  return api(`/api/w/${encodeURIComponent(state.ws)}${path}`, options);
}

// ---------- tabs ----------
function currentTab() { return document.querySelector("[role=tab][aria-selected=true]")?.dataset.tab || "runs"; }

function showTab(name) {
  document.querySelectorAll("[role=tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("hidden", s.id !== `tab-${name}`));
  location.hash = name;
  if (!state.ws) return;
  ({ runs: loadRuns, review: loadCandidates, facts: loadFacts, graph: loadGraphFiles, topology: loadTopology })[name]?.();
}

// ---------- workspaces ----------
async function loadWorkspaces(preferred) {
  const data = await api("/api/workspaces");
  state.workspaces = data.workspaces;
  const names = data.workspaces.map((w) => w.name);
  const select = clear($("#workspace-select"));
  data.workspaces.forEach((w) => select.append(h("option", { value: w.name }, w.name)));
  const wanted = [preferred, localStorage.getItem("ddg-workspace"), data.active].find((n) => n && names.includes(n));
  if (!wanted) {
    state.ws = null;
    $("#workspace-info").textContent = "No workspaces yet. Create one to start.";
    return;
  }
  select.value = wanted;
  await switchWorkspace(wanted, { persist: false });
}

async function switchWorkspace(name, { persist = true } = {}) {
  state.ws = name;
  localStorage.setItem("ddg-workspace", name);
  state.selectedRun = null;
  state.selectedCandidate = null;
  state.graph = null;
  state.cy?.elements().remove();
  $("#run-detail").classList.add("hidden");
  clear($("#candidate-detail")).append(h("p", { className: "muted" }, "Select a candidate to review its evidence."));
  clear($("#graph-detail")).append(h("p", { className: "muted" }, "Search, then select a node to load its neighborhood."));
  clear($("#codegen-result"));
  clear($("#graph-text"));
  if (persist) {
    // Also make it the default for the CLI and new browser sessions.
    api(`/api/workspaces/${encodeURIComponent(name)}/activate`, { method: "POST" }).catch(() => {});
  }
  await loadWorkspaceStatus();
  refreshBadge();
  showTab(currentTab());
}

async function loadWorkspaceStatus() {
  const s = await wsApi("/status");
  state.wsStatus = s;
  const info = state.workspaces.find((w) => w.name === s.workspace);
  const topo = info?.topology;
  $("#workspace-info").textContent = [
    s.description,
    topo ? `topology ${topo.is_valid ? "valid" : `invalid (${topo.errors} error${topo.errors === 1 ? "" : "s"})`}` : "",
    `${s.sources.length} source${s.sources.length === 1 ? "" : "s"}`,
  ].filter(Boolean).join(" · ");
  $("#status-line").textContent = `Planner: ${state.status.foundry_configured ? "Foundry configured" : "offline heuristic (Foundry not configured)"} · sample rows ≤ ${s.sampling.max_rows}`;
  for (const id of ["#run-source", "#facts-source", "#meta-source"]) {
    const select = clear($(id));
    s.sources.forEach((src) => select.append(h("option", { value: src.name }, `${src.name} (${src.type})`)));
  }
  const noSources = !s.sources.length;
  $("#run-form button[type=submit]").disabled = noSources;
  $("#generate-btn").disabled = noSources;
  $("#no-sources").classList.toggle("hidden", !noSources);
  $("#no-sources-path").textContent = `${s.path}${s.path.includes("\\") ? "\\" : "/"}workspace.toml`;
}

function openNewWorkspaceDialog() {
  const dialog = $("#new-workspace-dialog");
  const from = clear($("#new-ws-from"));
  from.append(h("option", { value: "_template" }, "Blank template"));
  state.workspaces.forEach((w) => from.append(h("option", { value: w.name }, `Copy configuration of ${w.name}`)));
  $("#new-ws-form").reset();
  $("#new-ws-error").textContent = "";
  dialog.showModal();
  $("#new-ws-name").focus();
}

async function createWorkspace(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  try {
    const ws = await api("/api/workspaces", {
      method: "POST",
      body: JSON.stringify({ name: form.get("name").trim(), description: form.get("description").trim(), copy_from: form.get("copy_from") }),
    });
    $("#new-workspace-dialog").close();
    toast(`Created workspace ${ws.name}. Edit its topology.md and workspace.toml next.`);
    await loadWorkspaces(ws.name);
    api(`/api/workspaces/${encodeURIComponent(ws.name)}/activate`, { method: "POST" }).catch(() => {});
    showTab("topology");
  } catch (e) {
    $("#new-ws-error").textContent = e.message;
  }
}

// ---------- status ----------
async function loadStatus() {
  state.status = await api("/api/status");
}

// ---------- runs ----------
let runPoll = null;
async function loadRuns() {
  const runs = await wsApi("/runs");
  const tbody = clear($("#runs-table tbody"));
  for (const run of runs) {
    const active = ["queued", "running", "stopping"].includes(run.status);
    tbody.append(h("tr", {},
      h("td", {}, h("button", { className: "link", onclick: () => selectRun(run.id) }, run.id)),
      h("td", {}, pill(run.status)),
      h("td", {}, run.source),
      h("td", {}, run.planner || ""),
      h("td", {}, run.iterations),
      h("td", {}, run.candidates),
      h("td", {}, run.termination_reason || ""),
      h("td", {},
        active ? h("button", { onclick: () => cancelRun(run.id) }, "Cancel") : null,
        !active ? h("button", { onclick: () => resumeRun(run.id) }, "Resume") : null),
    ));
  }
  const anyActive = runs.some((r) => ["queued", "running", "stopping"].includes(r.status));
  clearTimeout(runPoll);
  if (anyActive) runPoll = setTimeout(() => { loadRuns(); if (state.selectedRun) selectRun(state.selectedRun, true); refreshBadge(); }, 2500);
}

async function selectRun(runId, quiet = false) {
  state.selectedRun = runId;
  const run = await wsApi(`/runs/${encodeURIComponent(runId)}?events=150`);
  const panel = clear($("#run-detail"));
  panel.classList.remove("hidden");
  const u = run.usage || {};
  appendAll(panel, 
    h("h2", {}, `Run ${run.id} `, pill(run.status)),
    h("dl", { className: "kv" },
      h("dt", {}, "Source"), h("dd", {}, run.source),
      h("dt", {}, "Planner"), h("dd", {}, run.planner || ""),
      h("dt", {}, "Topology version"), h("dd", {}, run.topology_version || ""),
      h("dt", {}, "Resumed from"), h("dd", {}, run.resumed_from || "—"),
      h("dt", {}, "Limits"), h("dd", {}, Object.entries(run.limits || {}).map(([k, v]) => `${k}=${v}`).join(", ")),
      h("dt", {}, "Usage"), h("dd", {}, `${u.tool_calls ?? 0} tool calls · ${u.queries ?? 0} queries · ${u.model_calls ?? 0} model calls · ${u.input_tokens ?? 0}/${u.output_tokens ?? 0} tokens · $${(u.cost_usd ?? 0).toFixed(4)}`),
      h("dt", {}, "Termination"), h("dd", {}, run.termination_reason || "—"),
      run.error ? [h("dt", {}, "Error"), h("dd", { className: "error" }, run.error)] : null,
    ),
    h("h3", {}, "Iterations"),
    h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "#"), h("th", {}, "Summary"), h("th", {}, "New candidates"), h("th", {}, "Review snapshot"), h("th", {}, "Seconds"))),
      h("tbody", {}, (run.iterations || []).map((it) => h("tr", {},
        h("td", {}, it.iteration), h("td", {}, it.summary || "", it.proposal_errors?.length ? h("div", { className: "warn small" }, `Blocked proposals: ${it.proposal_errors.join(" | ")}`) : null),
        h("td", {}, (it.new_candidates || []).join(", ")), h("td", {}, it.review_snapshot_version), h("td", {}, it.duration_seconds))))),
    h("details", {}, h("summary", {}, `Events (${run.events.length})`),
      h("div", { className: "table-wrap" }, h("table", {}, h("tbody", {}, run.events.slice().reverse().map((ev) => h("tr", {},
        h("td", { className: "small muted" }, fmtTime(ev.at)), h("td", {}, ev.type), h("td", {}, ev.tool || ""),
        h("td", { className: "small" }, JSON.stringify(ev.summary || ev.error || ev.summary || ev.reason || ev.message || "")))))))),
  );
  if (!quiet) panel.scrollIntoView({ behavior: "smooth" });
}

async function cancelRun(runId) {
  try { await wsApi(`/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" }); toast("Cancellation requested"); loadRuns(); }
  catch (e) { toast(e.message, true); }
}

async function resumeRun(runId) {
  try {
    const run = await wsApi(`/runs/${encodeURIComponent(runId)}/resume`, { method: "POST", body: JSON.stringify({}) });
    toast(`Resumed as ${run.id}`); loadRuns(); selectRun(run.id);
  } catch (e) { toast(e.message, true); }
}

async function startRun(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  const limits = {};
  for (const key of ["max_minutes", "max_iterations", "max_cost_usd"]) if (form.get(key)) limits[key] = Number(form.get(key));
  try {
    const run = await wsApi("/runs", { method: "POST", body: JSON.stringify({ source: form.get("source"), planner: form.get("planner") || null, limits }) });
    toast(`Started ${run.id}`);
    loadRuns(); selectRun(run.id);
  } catch (e) { toast(e.message, true); }
}

// ---------- review ----------
async function refreshBadge() {
  const pending = await wsApi("/candidates?status=pending");
  $("#pending-badge").textContent = pending.length ? String(pending.length) : "";
}

async function loadCandidates() {
  const status = $("#review-status").value;
  const items = await wsApi(`/candidates${status ? `?status=${status}` : ""}`);
  const list = clear($("#candidate-list"));
  if (!items.length) list.append(h("p", { className: "muted" }, "No candidates."));
  for (const c of items) {
    list.append(h("button", { className: "item", "aria-current": String(state.selectedCandidate === c.id), onclick: () => selectCandidate(c.id) },
      h("span", { className: "title" }, c.hypothesis),
      h("span", { className: "small muted" }, `${c.kind} · `, pill(c.status), ` · ${c.scope?.source || ""} · ${c.id}`)));
  }
  refreshBadge();
}

function mappingTable(mapping) {
  if (!mapping) return h("p", { className: "muted" }, "No mapping (observation).");
  const rows = Object.entries(mapping).map(([k, v]) => h("tr", {}, h("th", {}, k), h("td", {}, typeof v === "object" ? JSON.stringify(v) : String(v))));
  return h("table", {}, h("tbody", {}, rows));
}

function evidenceView(ev) {
  const r = ev.result || {};
  const headline = r.key_coverage !== undefined
    ? `coverage ${(r.row_coverage * 100).toFixed(1)}% rows / ${(r.key_coverage * 100).toFixed(1)}% keys · unmatched ${r.unmatched_left_keys} · nulls ${r.left_null_key_rows} · ${r.observed_cardinality_left_to_right}`
    : r.unique !== undefined ? `unique=${r.unique} · distinct ${r.distinct_keys} · duplicates ${r.duplicate_keys} · null-key rows ${r.rows_with_null_key}`
    : r.row_count !== undefined ? `${r.row_count} rows` : "";
  return h("details", {},
    h("summary", {}, `${ev.tool} `, h("span", { className: `pill ${ev.scope === "full" ? "completed" : "pending"}` }, ev.scope === "full" ? "full-data check" : "sampled"), ` ${headline}`),
    h("div", { className: "small muted" }, `${ev.id} · run ${ev.run_id} · ${fmtTime(ev.observed_at)}`),
    json({ args: ev.args, result: ev.result }));
}

async function selectCandidate(id) {
  state.selectedCandidate = id;
  document.querySelectorAll("#candidate-list .item").forEach((el) => el.setAttribute("aria-current", String(el.textContent.includes(id))));
  const c = await wsApi(`/candidates/${encodeURIComponent(id)}`);
  const panel = clear($("#candidate-detail"));
  appendAll(panel, 
    h("h2", {}, c.hypothesis),
    h("p", {}, pill(c.status), ` ${c.kind} · version ${c.version} · confidence ${c.confidence ?? "n/a"} (not proof)`),
    h("dl", { className: "kv" },
      h("dt", {}, "Rationale"), h("dd", {}, c.rationale || ""),
      h("dt", {}, "Source refs"), h("dd", {}, (c.source_refs || []).join(", ")),
      h("dt", {}, "Run / iteration"), h("dd", {}, `${c.run_id} / ${c.iteration}`),
      h("dt", {}, "Topology version"), h("dd", {}, c.topology_version),
      c.new_evidence_note ? [h("dt", {}, "Revisits rejection"), h("dd", {}, c.new_evidence_note)] : null),
    h("h3", {}, "Proposed mapping"), mappingTable(c.mapping),
    h("h3", {}, "Limitations"), h("ul", {}, (c.limitations || []).map((l) => h("li", {}, l))),
    h("h3", {}, `Evidence (${(c.evidence || []).length})`), (c.evidence || []).map(evidenceView),
  );
  if (c.decisions?.length) {
    appendAll(panel, h("h3", {}, "Decision history"), h("ul", {}, c.decisions.map((d) =>
      h("li", {}, `${d.decision} by ${d.reviewer} at ${fmtTime(d.at)} (v${d.candidate_version})`, d.notes ? ` — notes: ${d.notes}` : "", d.observations ? ` — observations: ${d.observations}` : ""))));
  }
  if (c.status === "pending") {
    const notes = h("textarea", { id: "decision-notes", "aria-label": "Notes" });
    const obs = h("textarea", { id: "decision-observations", "aria-label": "Observations" });
    const decide = async (decision) => {
      const reviewer = $("#reviewer").value.trim();
      if (!reviewer) { toast("Enter your reviewer name first", true); $("#reviewer").focus(); return; }
      if (decision === "reject" && !notes.value.trim()) { toast("Add notes explaining the rejection so the agent can learn from it", true); notes.focus(); return; }
      localStorage.setItem("ddg-reviewer", reviewer);
      try {
        await wsApi(`/candidates/${encodeURIComponent(c.id)}/decision`, { method: "POST", body: JSON.stringify({ decision, reviewer, notes: notes.value, observations: obs.value, expected_version: c.version }) });
        toast(`${decision === "approve" ? "Approved" : "Rejected"} ${c.id}`);
        await loadCandidates();
        selectCandidate(c.id);
      } catch (e) {
        toast(e.status === 409 ? `Conflict: ${e.message}` : e.message, true);
        if (e.status === 409) selectCandidate(c.id);
      }
    };
    appendAll(panel, h("h3", {}, "Decision"),
      h("label", {}, "Notes (required for rejection; the agent reads these)", notes),
      h("label", {}, "Observations", obs),
      h("div", { className: "row" },
        h("button", { className: "approve", onclick: () => decide("approve") }, "Approve → promote to fact"),
        h("button", { className: "reject", onclick: () => decide("reject") }, "Reject")));
  }
}

// ---------- facts & codegen ----------
async function loadFacts() {
  const source = $("#facts-source").value;
  if (!source) {
    clear($("#facts-table tbody")).append(h("tr", {}, h("td", { colspan: 5, className: "muted" }, "This workspace has no sources yet.")));
    clear($("#revalidation"));
    clear($("#generated-list"));
    return;
  }
  const [facts, generated] = await Promise.all([wsApi(`/facts?source=${encodeURIComponent(source)}`), wsApi("/codegen")]);
  const tbody = clear($("#facts-table tbody"));
  for (const f of facts) {
    const m = f.mapping || {};
    const summary = f.kind === "node_mapping" ? `${m.node_type} ← ${m.source_table}(${(m.key_columns || []).join(", ")})`
      : f.kind === "edge_mapping" ? `${m.from_node_type} -[${m.edge_type}]-> ${m.to_node_type} ← ${m.source_table}` : f.hypothesis;
    tbody.append(h("tr", {}, h("td", { className: "small" }, f.id), h("td", {}, f.kind), h("td", {}, summary),
      h("td", {}, `${f.approved_by} · ${fmtTime(f.approved_at)}`), h("td", {}, f.notes || "")));
  }
  if (!facts.length) tbody.append(h("tr", {}, h("td", { colspan: 5, className: "muted" }, "No approved facts yet.")));
  const reval = clear($("#revalidation"));
  try {
    const r = await wsApi(`/facts/revalidation?source=${encodeURIComponent(source)}`);
    const issues = Object.values(r.issues).flat();
    if (!r.source_reachable) reval.append(h("p", { className: "warn" }, "Source unreachable; schema drift not checked."));
    if (issues.length) reval.append(h("div", { className: "warn" }, h("strong", {}, "Needs revalidation:"), h("ul", {}, issues.map((i) => h("li", {}, i)))));
  } catch (e) { reval.append(h("p", { className: "error" }, e.message)); }
  const list = clear($("#generated-list"));
  for (const g of generated) list.append(h("li", {}, h("code", {}, g.builder), h("div", { className: "small muted" }, `${fmtTime(g.generated_at)} · ${g.source} · ${g.facts.length} facts`), h("code", { className: "small" }, g.suggested_command)));
}

async function generateCode() {
  const out = clear($("#codegen-result"));
  try {
    const result = await wsApi("/codegen", { method: "POST", body: JSON.stringify({ source: $("#facts-source").value, allow_stale: $("#allow-stale").checked }) });
    appendAll(out, h("p", {}, "Generated ", h("code", {}, result.path)),
      result.warnings.length ? h("ul", { className: "warn" }, result.warnings.map((w) => h("li", {}, w))) : null,
      h("p", {}, "Review it, then run:"), h("pre", {}, result.suggested_command),
      h("details", {}, h("summary", {}, "View code"), h("pre", {}, result.code)));
    loadFacts();
  } catch (e) {
    const errors = e.body?.errors || [e.message];
    appendAll(out, h("p", { className: "error" }, "Code generation is blocked:"), h("ul", { className: "error" }, errors.map((x) => h("li", {}, x))),
      e.body?.warnings?.length ? h("ul", { className: "warn" }, e.body.warnings.map((w) => h("li", {}, w))) : null);
  }
}

// ---------- graph ----------
const PALETTE = ["#0f62c9", "#d1495b", "#2a9d8f", "#e9a03b", "#7b4fc9", "#3d7a2a", "#c25a9e", "#5b6475", "#00838f", "#a0522d"];
function colorFor(type) {
  let hash = 0;
  for (const ch of type) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[hash % PALETTE.length];
}
function nodeLabel(n) {
  const p = n.properties || {};
  const named = p.name ?? p.title ?? p.label;
  return `${n.type}: ${named ?? n.key}`;
}

async function loadGraphFiles() {
  const files = await wsApi("/graphs");
  const select = $("#graph-file");
  const current = select.value;
  clear(select);
  if (!files.length) {
    select.append(h("option", { value: "" }, "No graph output yet"));
    $("#graph-summary").textContent = "Generate builder code from approved facts, run it, and write its JSON output to graph_output/.";
    return;
  }
  files.forEach((f) => select.append(h("option", { value: f.name }, f.name)));
  if (current && files.some((f) => f.name === current)) select.value = current;
  await loadGraphSummary();
}

async function loadGraphSummary() {
  const name = $("#graph-file").value;
  if (!name) return;
  try {
    const s = await wsApi(`/graphs/${encodeURIComponent(name)}/summary`);
    state.graph = name;
    const typeSelect = clear($("#graph-type"));
    typeSelect.append(h("option", { value: "" }, "All types"));
    Object.keys(s.node_types).forEach((t) => typeSelect.append(h("option", { value: t }, `${t} (${s.node_types[t]})`)));
    const inputs = s.inputs || {};
    $("#graph-summary").textContent = `${s.node_count} nodes · ${s.edge_count} edges · edges: ${Object.entries(s.edge_types).map(([k, v]) => `${k} ${v}`).join(", ")} · built ${fmtTime(s.built_at)} · topology ${inputs.topology_version} · ${inputs.facts?.length ?? 0} facts${(s.warnings || []).length ? " · warnings: " + s.warnings.join("; ") : ""}`;
    await graphSearch();
  } catch (e) { toast(e.message, true); }
}

async function graphSearch() {
  if (!state.graph) return;
  const q = encodeURIComponent($("#graph-q").value);
  const type = encodeURIComponent($("#graph-type").value);
  const results = await wsApi(`/graphs/${encodeURIComponent(state.graph)}/search?q=${q}&type=${type}&limit=100`);
  const list = clear($("#graph-results"));
  if (!results.length) list.append(h("p", { className: "muted" }, "No matches."));
  for (const n of results) list.append(h("button", { className: "item", onclick: () => expandNode(n.id, true) },
    h("span", { className: "title" }, nodeLabel(n)), h("span", { className: "small muted" }, n.id)));
}

function ensureCy() {
  if (state.cy) return state.cy;
  if (!window.cytoscape) { toast("Cytoscape failed to load (offline?). Use the text view.", true); return null; }
  state.cy = cytoscape({
    container: $("#cy"),
    style: [
      { selector: "node", style: { "background-color": "data(color)", label: "data(label)", "font-size": 9, color: "#1d2330", "text-wrap": "ellipsis", "text-max-width": 110, width: 22, height: 22, "min-zoomed-font-size": 7 } },
      { selector: "node.center", style: { "border-width": 3, "border-color": "#1d2330" } },
      { selector: "edge", style: { width: 1.5, "line-color": "#9aa5b8", "target-arrow-color": "#9aa5b8", "target-arrow-shape": "triangle", "curve-style": "bezier", label: "data(type)", "font-size": 7, color: "#5b6475", "min-zoomed-font-size": 9 } },
      { selector: ":selected", style: { "border-width": 3, "border-color": "#e9a03b", "line-color": "#e9a03b" } },
    ],
    layout: { name: "grid" },
  });
  state.cy.on("tap", "node", (evt) => showNodeDetail(evt.target.data("raw")));
  state.cy.on("tap", "edge", (evt) => showEdgeDetail(evt.target.data("raw")));
  return state.cy;
}

async function expandNode(nodeId, reset = false) {
  const depth = $("#graph-depth").value;
  const data = await wsApi(`/graphs/${encodeURIComponent(state.graph)}/neighborhood?node_id=${encodeURIComponent(nodeId)}&depth=${depth}&limit=200`);
  const cy = ensureCy();
  if (cy) {
    if (reset) cy.elements().remove();
    cy.nodes().removeClass("center");
    const add = [];
    for (const n of data.nodes) if (cy.getElementById(n.id).empty()) add.push({ group: "nodes", data: { id: n.id, label: nodeLabel(n), color: colorFor(n.type), raw: n } });
    for (const e of data.edges) if (cy.getElementById(e.id).empty()) add.push({ group: "edges", data: { id: e.id, source: e.source, target: e.target, type: e.type, raw: e } });
    cy.add(add);
    cy.getElementById(nodeId).addClass("center");
    cy.layout({ name: "cose", animate: false, nodeRepulsion: 9000, idealEdgeLength: 90 }).run();
  }
  const center = data.nodes.find((n) => n.id === nodeId);
  showNodeDetail(center, data);
  renderTextView();
}

function provenanceList(prov) {
  return h("ul", {}, (prov || []).map((p) => h("li", {}, `${p.source}.${p.table} via ${p.fact_id} (${p.rows} rows)`)));
}

function showNodeDetail(node, neighborhood) {
  if (!node) return;
  const panel = clear($("#graph-detail"));
  appendAll(panel, h("h2", {}, nodeLabel(node)),
    h("p", { className: "small muted" }, node.id),
    neighborhood ? h("p", { className: "small" }, `Degree ${neighborhood.degree}${neighborhood.truncated ? " · neighborhood truncated" : ""}`) : null,
    h("h3", {}, "Properties"), mappingTable(node.properties),
    h("h3", {}, "Provenance"), provenanceList(node.provenance),
    h("button", { onclick: () => expandNode(node.id) }, "Expand neighborhood"));
}

function showEdgeDetail(edge) {
  const panel = clear($("#graph-detail"));
  appendAll(panel, h("h2", {}, edge.type), h("p", { className: "small" }, `${edge.source} → ${edge.target}`),
    h("h3", {}, "Properties"), mappingTable(edge.properties), h("h3", {}, "Provenance"), provenanceList(edge.provenance));
}

function renderTextView() {
  const out = clear($("#graph-text"));
  const cy = state.cy;
  if (!cy) return;
  const nodes = cy.nodes().map((n) => n.data("raw"));
  const edges = cy.edges().map((e) => e.data("raw"));
  appendAll(out, 
    h("h3", {}, `Nodes (${nodes.length})`),
    h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Type"), h("th", {}, "Key"), h("th", {}, "Properties"), h("th", {}, "Sources"))),
      h("tbody", {}, nodes.map((n) => h("tr", {}, h("td", {}, n.type), h("td", {}, n.key), h("td", {}, JSON.stringify(n.properties)), h("td", {}, (n.provenance || []).map((p) => p.table).join(", ")))))),
    h("h3", {}, `Edges (${edges.length})`),
    h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Type"), h("th", {}, "From"), h("th", {}, "To"), h("th", {}, "Properties"))),
      h("tbody", {}, edges.map((e) => h("tr", {}, h("td", {}, e.type), h("td", {}, e.source), h("td", {}, e.target), h("td", {}, JSON.stringify(e.properties)))))));
}

// ---------- topology & metadata ----------
async function loadTopology() {
  const t = await wsApi("/topology");
  const panel = clear($("#topology-view"));
  appendAll(panel, h("h2", {}, `Topology: ${t.title} `, h("span", { className: `pill ${t.is_valid ? "completed" : "failed"}` }, t.is_valid ? "valid" : "invalid")),
    h("p", { className: "small muted" }, `Workspace ${state.ws} · version ${t.version} · ${t.source_path}`),
    t.issues.length ? h("ul", {}, t.issues.map((i) => h("li", { className: i.level === "error" ? "error" : "warn" }, `${i.level}${i.line ? ` (line ${i.line})` : ""}: ${i.message}`))) : null,
    h("h3", {}, "Node types"),
    h("table", {}, h("tbody", {}, t.node_types.map((n) => h("tr", {}, h("th", {}, n.name), h("td", {}, n.description, h("div", { className: "small muted" }, `key: ${n.key || "?"} · properties: ${n.properties.join(", ")}${n.synonyms.length ? " · synonyms: " + n.synonyms.join(", ") : ""}`)))))),
    h("h3", {}, "Edge types"),
    h("table", {}, h("tbody", {}, t.edge_types.map((e) => h("tr", {}, h("th", {}, e.name), h("td", {}, `(${e.from_type}) → (${e.to_type}) ${e.cardinality}`, h("div", { className: "small muted" }, e.description)))))),
    t.constraints.length ? [h("h3", {}, "Constraints"), h("ul", {}, t.constraints.map((c) => h("li", {}, c)))] : null,
    t.hints.length ? [h("h3", {}, "Hints"), h("ul", {}, t.hints.map((c) => h("li", {}, c)))] : null);
  await loadMetadata();
}

async function loadMetadata() {
  const source = $("#meta-source").value;
  if (!source) { clear($("#metadata-view")).append(h("p", { className: "muted" }, "This workspace has no sources yet.")); return; }
  const data = await wsApi(`/metadata/${encodeURIComponent(source)}`);
  const view = clear($("#metadata-view"));
  const tables = Object.values(data.tables || {});
  if (!tables.length) { appendAll(view, h("p", { className: "muted" }, "No inventory yet. Start a run to discover metadata.")); return; }
  appendAll(view, h("p", { className: "small muted" }, `Observed ${fmtTime(data.observed_at)} · ${data.dialect}`));
  for (const t of tables) {
    appendAll(view, h("details", {}, h("summary", {}, `${t.table} — ${t.row_count} rows, ${t.columns.length} columns`),
      h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Column"), h("th", {}, "Type"), h("th", {}, "Nullable"), h("th", {}, "PK"))),
        h("tbody", {}, t.columns.map((c) => h("tr", {}, h("td", {}, c.name), h("td", {}, c.type), h("td", {}, c.nullable ? "yes" : "no"), h("td", {}, c.primary_key ? "yes" : ""))))),
      h("p", { className: "small muted" }, `Foreign keys: ${Array.isArray(t.foreign_keys) ? (t.foreign_keys.length ? JSON.stringify(t.foreign_keys) : "none declared") : t.foreign_keys} · schema fingerprint ${t.schema_fingerprint}`)));
  }
}

// ---------- boot ----------
document.addEventListener("DOMContentLoaded", async () => {
  document.querySelectorAll("[role=tab]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  $("#run-form").addEventListener("submit", startRun);
  $("#review-status").addEventListener("change", loadCandidates);
  $("#review-refresh").addEventListener("click", loadCandidates);
  $("#reviewer").value = localStorage.getItem("ddg-reviewer") || "";
  $("#facts-source").addEventListener("change", loadFacts);
  $("#generate-btn").addEventListener("click", generateCode);
  $("#graph-file").addEventListener("change", loadGraphSummary);
  $("#graph-search-btn").addEventListener("click", graphSearch);
  $("#graph-q").addEventListener("keydown", (e) => { if (e.key === "Enter") graphSearch(); });
  $("#graph-type").addEventListener("change", graphSearch);
  $("#graph-clear-btn").addEventListener("click", () => { state.cy?.elements().remove(); renderTextView(); });
  $("#meta-source").addEventListener("change", loadMetadata);
  $("#workspace-select").addEventListener("change", (e) => switchWorkspace(e.target.value).catch((err) => toast(err.message, true)));
  $("#new-workspace-btn").addEventListener("click", openNewWorkspaceDialog);
  $("#new-ws-form").addEventListener("submit", createWorkspace);
  $("#new-ws-cancel").addEventListener("click", () => $("#new-workspace-dialog").close());
  window.addEventListener("hashchange", () => {
    const tab = location.hash.slice(1);
    const current = document.querySelector("[role=tab][aria-selected=true]")?.dataset.tab;
    if (tab && tab !== current && $(`#tab-${tab}`)) showTab(tab);
  });
  try {
    await loadStatus();
    const params = new URLSearchParams(location.search);
    showTab((location.hash || "#runs").slice(1));
    await loadWorkspaces(params.get("workspace"));
  } catch (e) { toast(`Failed to load: ${e.message}`, true); }
});
