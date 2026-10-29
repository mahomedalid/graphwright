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
  ({ runs: loadRuns, review: loadCandidates, facts: loadFacts, dictionary: loadDictionary, graph: loadGraphFiles, topology: loadTopology })[name]?.();
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
    topo ? `topology ${topo.parsing_mode === "pending_foundry" ? "awaiting Foundry parsing" : topo.is_valid ? "valid" : `invalid (${topo.errors} error${topo.errors === 1 ? "" : "s"})`}` : "",
    `${s.sources.length} source${s.sources.length === 1 ? "" : "s"}`,
  ].filter(Boolean).join(" · ");
  $("#status-line").textContent = `Planner: ${state.status.foundry_configured ? "Foundry configured" : "offline heuristic (Foundry not configured)"} · sample rows ≤ ${s.sampling.max_rows}`;
  for (const id of ["#run-source", "#facts-source", "#meta-source", "#dict-source"]) {
    const select = clear($(id));
    s.sources.forEach((src) => select.append(h("option", { value: src.name }, `${src.name} (${src.type})`)));
  }
  const noSources = !s.sources.length;
  $("#run-form [name=max_iterations]").placeholder = String(s.limits.max_iterations);
  $("#run-mode option[value='']").textContent = `Workspace default (${s.default_mode || "conservative"})`;
  $("#run-form button[type=submit]").disabled = noSources;
  $("#generate-btn").disabled = noSources;
  $("#dict-profile-btn").disabled = noSources;
  $("#dict-export-btn").disabled = noSources;
  state.dict = null;
  state.dictTable = null;
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
    toast(`Created workspace ${ws.name}. Edit its topology (context/topology.md for new workspaces) and workspace.toml next.`);
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

// ---------- safe markdown (DOM nodes only; never innerHTML) ----------
function mdInline(text) {
  const out = [];
  const re = /(\*\*[^*]+\*\*|__[^_]+__|`[^`]+`|\*[^*\s][^*]*\*)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith("**") || tok.startsWith("__")) out.push(h("strong", {}, tok.slice(2, -2)));
    else if (tok.startsWith("`")) out.push(h("code", {}, tok.slice(1, -1)));
    else out.push(h("em", {}, tok.slice(1, -1)));
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function renderMarkdown(text) {
  const root = h("div", { className: "md" });
  const lines = String(text || "").replace(/\r\n/g, "\n").split("\n");
  let para = [];
  const stack = []; // [{ indent, el }]
  const flushPara = () => { if (para.length) { root.append(h("p", {}, mdInline(para.join(" ")))); para = []; } };
  const closeLists = (indent = -1) => { while (stack.length && stack[stack.length - 1].indent > indent) stack.pop(); };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      flushPara(); closeLists();
      const code = [];
      for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) code.push(lines[i]);
      root.append(h("pre", {}, code.join("\n")));
      continue;
    }
    const heading = line.match(/^\s*(#{1,6})\s+(.*)$/);
    const item = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (!line.trim()) {
      flushPara();
      const next = lines.slice(i + 1).find((l) => l.trim());
      // Blank lines between items (or before indented continuation) keep the list open.
      if (!next || !(/^(\s*)([-*+]|\d+[.)])\s+/.test(next) || (stack.length && /^\s+\S/.test(next)))) closeLists();
      continue;
    }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { flushPara(); closeLists(); root.append(h("hr", {})); continue; }
    if (heading) {
      flushPara(); closeLists();
      root.append(h(`h${Math.min(6, heading[1].length + 3)}`, {}, mdInline(heading[2])));
      continue;
    }
    if (item) {
      flushPara();
      const indent = item[1].replace(/\t/g, "  ").length;
      const ordered = /\d/.test(item[2]);
      closeLists(indent);
      let top = stack[stack.length - 1];
      if (!top || top.indent < indent) {
        const start = ordered ? parseInt(item[2], 10) : null;
        const list = h(ordered ? "ol" : "ul", start && start !== 1 ? { start } : {});
        const parent = top ? (top.el.lastElementChild || top.el) : root;
        parent.append(list);
        stack.push({ indent, el: list });
        top = stack[stack.length - 1];
      }
      top.el.append(h("li", {}, mdInline(item[3])));
      continue;
    }
    if (stack.length && /^\s+\S/.test(line)) { // continuation of a list item
      stack[stack.length - 1].el.lastElementChild?.append(" ", ...mdInline(line.trim()));
      continue;
    }
    closeLists();
    para.push(line.trim());
  }
  flushPara();
  return root;
}

// ---------- runs ----------
function iterationView(it, openSet) {
  const activity = it.activity || {};
  const proposed = activity.proposed || [];
  const details = it.summary && it.summary !== it.story
    ? h("details", { className: "iteration-details", "data-iteration": String(it.iteration), open: openSet.has(String(it.iteration)) },
        h("summary", {}, `Agent notes (${Math.max(1, Math.round(it.summary.length / 1000))}k characters)`),
        it.facts ? h("p", { className: "small muted" }, h("strong", {}, "Recorded activity: "), it.facts) : null,
        renderMarkdown(it.summary))
    : null;
  return h("li", { className: "iteration" },
    h("div", { className: "iteration-head" },
      h("span", { className: "iteration-step" }, `Step ${it.iteration}`),
      h("span", { className: "small muted" }, `${it.duration_seconds ?? "?"}s`)),
    h("p", { className: "story" }, it.story || "No summary recorded."),
    proposed.length ? h("div", { className: "chips" }, proposed.map((p) =>
      h("button", { type: "button", className: "chip proposal", title: p.id, onclick: () => { showTab("review"); setTimeout(() => selectCandidate(p.id), 300); } },
        tierBadge(p.tier), " ", p.label))) : null,
    it.proposal_errors?.length ? h("details", { className: "small" },
      h("summary", { className: "warn" }, `${it.proposal_errors.length} idea${it.proposal_errors.length === 1 ? "" : "s"} held back`),
      h("ul", {}, it.proposal_errors.map((e) => h("li", {}, e)))) : null,
    details);
}
let runPoll = null;
let runLoadVersion = 0;
const RUN_POLL_INTERVAL_MS = 5000;
async function loadRuns() {
  clearTimeout(runPoll);
  const version = ++runLoadVersion;
  const workspace = state.ws;
  let anyActive = true;
  try {
    const runs = await wsApi("/runs");
    if (workspace !== state.ws || version !== runLoadVersion) return;
    anyActive = runs.some((r) => ["queued", "running", "stopping"].includes(r.status));
    const tbody = clear($("#runs-table tbody"));
    for (const run of runs) {
      const active = ["queued", "running", "stopping"].includes(run.status);
      tbody.append(h("tr", {},
        h("td", {}, h("button", { className: "link", onclick: () => selectRun(run.id) }, run.id)),
        h("td", {}, pill(run.status)),
        h("td", {}, run.source),
        h("td", {}, run.planner || "", run.mode ? h("div", { className: "small muted" }, run.mode) : null),
        h("td", {}, run.iterations),
        h("td", {}, run.candidates),
        h("td", {}, run.termination_reason || ""),
        h("td", {},
          active ? h("button", { onclick: () => cancelRun(run.id) }, "Cancel") : null,
          !active ? h("button", { onclick: () => resumeRun(run.id) }, "Resume") : null),
      ));
    }
    if (state.selectedRun) await selectRun(state.selectedRun, true);
    if (workspace === state.ws && version === runLoadVersion) await refreshBadge();
  } catch (e) {
    if (workspace === state.ws && version === runLoadVersion) {
      anyActive = true;
      toast(`Run refresh failed: ${e.message}. Retrying in 5 seconds.`, true);
    }
  } finally {
    if (workspace === state.ws && version === runLoadVersion && anyActive) {
      runPoll = setTimeout(loadRuns, RUN_POLL_INTERVAL_MS);
    }
  }
}

async function selectRun(runId, quiet = false) {
  state.selectedRun = runId;
  const workspace = state.ws;
  const run = await wsApi(`/runs/${encodeURIComponent(runId)}?events=150`);
  if (workspace !== state.ws || state.selectedRun !== runId) return;
  const panel = $("#run-detail");
  const eventsOpen = quiet ? (panel.querySelector("#run-events")?.open ?? false) : false;
  const openSet = new Set(quiet ? [...panel.querySelectorAll(".iteration-details[open]")].map((d) => d.dataset.iteration) : []);
  const metaOpen = quiet ? (panel.querySelector(".run-meta")?.open ?? false) : false;
  clear(panel);
  panel.classList.remove("hidden");
  const u = run.usage || {};
  const eventRows = run.events.slice().reverse().map((ev) => {
    const details = Object.fromEntries(
      Object.entries(ev).filter(([key]) => !["at", "run_id", "type", "tool", "args", "trace"].includes(key))
    );
    return h("tr", {},
      h("td", { className: "small muted" }, fmtTime(ev.at)), h("td", {}, ev.type), h("td", {}, ev.tool || ""),
      h("td", { className: "small" }, JSON.stringify(details)));
  });
  appendAll(panel, 
    h("h2", {}, `Run ${run.id} `, pill(run.status)),
    run.story ? h("p", { className: "story run-story" }, run.story) : null,
    h("details", { className: "run-meta", open: metaOpen }, h("summary", {}, "Run settings and usage"),
    h("dl", { className: "kv" },
      h("dt", {}, "Source"), h("dd", {}, run.source),
      h("dt", {}, "Planner"), h("dd", {}, (run.planner || "") + (run.mode ? ` · ${run.mode} mode` : "")),
      h("dt", {}, "Topology version"), h("dd", {}, run.topology_version || ""),
      h("dt", {}, "Resumed from"), h("dd", {}, run.resumed_from || "—"),
      h("dt", {}, "Limits"), h("dd", {}, Object.entries(run.limits || {}).map(([k, v]) => `${k}=${v}`).join(", ")),
      h("dt", {}, "Usage"), h("dd", {}, `${u.tool_calls ?? 0} tool calls · ${u.queries ?? 0} queries · ${u.model_calls ?? 0} model calls · ${u.input_tokens ?? 0}/${u.output_tokens ?? 0} tokens · $${(u.cost_usd ?? 0).toFixed(4)}`),
      h("dt", {}, "Termination"), h("dd", {}, run.termination_reason || "—"),
      run.events.length ? [h("dt", {}, "Latest activity"), h("dd", {},
        `${run.events[run.events.length - 1].type} · ${fmtTime(run.events[run.events.length - 1].at)}`)] : null,
    )),
    run.error ? h("p", { className: "error" }, run.error) : null,
    h("h3", {}, `Steps (${(run.iterations || []).length})`),
    (run.iterations || []).length
      ? h("ol", { className: "iterations" }, run.iterations.map((it) => iterationView(it, openSet)))
      : h("p", { className: "muted" }, "No steps completed yet."),
    h("details", { id: "run-events", open: eventsOpen }, h("summary", {}, `Technical events (${run.events.length})`),
      h("div", { className: "table-wrap" }, h("table", {}, h("tbody", {}, eventRows)))),
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
    const run = await wsApi("/runs", { method: "POST", body: JSON.stringify({ source: form.get("source"), planner: form.get("planner") || null, mode: form.get("mode") || null, limits }) });
    toast(`Started ${run.id}`);
    loadRuns(); selectRun(run.id);
  } catch (e) { toast(e.message, true); }
}

// ---------- review ----------
async function refreshBadge() {
  const workspace = state.ws;
  const pending = await wsApi("/candidates?status=pending");
  if (workspace !== state.ws) return;
  $("#pending-badge").textContent = pending.length ? String(pending.length) : "";
}

const TIER_ORDER = { high: 0, medium: 1, low: 2 };

function tierBadge(tier) {
  if (!tier) return null;
  return h("span", { className: `tier tier-${tier}`, title: `${tier} confidence` }, tier);
}

async function loadCandidates() {
  const status = $("#review-status").value;
  const tier = $("#review-tier").value;
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (tier) params.set("tier", tier);
  const items = await wsApi(`/candidates${params.toString() ? `?${params}` : ""}`);
  // Highest confidence first so SMEs can clear easy ones quickly; ties keep creation order.
  items.sort((a, b) => (TIER_ORDER[a.confidence_tier] ?? 3) - (TIER_ORDER[b.confidence_tier] ?? 3));
  const list = clear($("#candidate-list"));
  if (!items.length) list.append(h("p", { className: "muted" }, "No candidates."));
  for (const c of items) {
    list.append(h("button", { className: "item", "aria-current": String(state.selectedCandidate === c.id), onclick: () => selectCandidate(c.id) },
      h("span", { className: "title" }, tierBadge(c.confidence_tier), " ", c.hypothesis),
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
    h("p", {}, pill(c.status), " ", tierBadge(c.confidence_tier), ` ${c.kind} · version ${c.version}` +
      (c.discovery_mode ? ` · proposed in ${c.discovery_mode} mode` : "")),
    c.confidence_reasons?.length ? h("div", { className: `tier-reasons tier-${c.confidence_tier}` },
      h("strong", {}, `Why ${c.confidence_tier} confidence:`),
      h("ul", {}, c.confidence_reasons.map((r) => h("li", {}, r)))) : null,
    h("dl", { className: "kv" },
      h("dt", {}, "Rationale"), h("dd", {}, renderMarkdown(c.rationale || "")),
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
  const pending = t.parsing_mode === "pending_foundry";
  appendAll(panel, h("h2", {}, `Topology: ${t.title} `, h("span", { className: `pill ${pending ? "queued" : t.is_valid ? "completed" : "failed"}` }, pending ? "awaiting Foundry parsing" : t.is_valid ? "valid" : "invalid")),
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

// ---------- data dictionary ----------
function dictAuthor() {
  const author = $("#dict-author").value.trim();
  if (author) localStorage.setItem("ddg-reviewer", author);
  return author;
}

async function loadDictionary() {
  const source = $("#dict-source").value;
  $("#dict-author").value = $("#dict-author").value || localStorage.getItem("ddg-reviewer") || "";
  if (!source) {
    clear($("#dict-tables")).append(h("p", { className: "muted" }, "This workspace has no sources yet."));
    clear($("#dict-detail"));
    $("#dict-summary").textContent = "";
    return;
  }
  try {
    state.dict = await wsApi(`/dictionary/${encodeURIComponent(source)}`);
  } catch (e) { toast(e.message, true); return; }
  const s = state.dict.summary;
  $("#dict-summary").textContent = s.tables
    ? `${s.tables} tables (${s.tables_described} described, ${s.tables_mapped} mapped) · ${s.columns} columns (${s.columns_described} described, ${s.columns_profiled} profiled)` +
      (s.pending_suggestions ? ` · ${s.pending_suggestions} pending suggestion${s.pending_suggestions === 1 ? "" : "s"}` : "") +
      ` · inventory ${state.dict.inventory_observed_at ? fmtTime(state.dict.inventory_observed_at) : "not collected yet"}`
    : "No tables discovered yet. Start a discovery run or select Profile columns.";
  renderDictionaryList();
  const keep = state.dictTable && state.dict.tables.some((t) => t.name === state.dictTable);
  if (keep) renderDictionaryTable(state.dictTable);
  else clear($("#dict-detail")).append(h("p", { className: "muted" }, "Select a table to see what we know about it."));
}

function dictMatches(t) {
  const q = $("#dict-filter").value.trim().toLowerCase();
  const show = $("#dict-show").value;
  if (q && !t.name.toLowerCase().includes(q) && !t.columns.some((c) => c.name.toLowerCase().includes(q))) return false;
  if (show === "mapped" && !t.roles.length) return false;
  if (show === "undescribed" && t.description && t.coverage.described === t.coverage.columns) return false;
  if (show === "suggestions" && !t.suggestions.length && !t.columns.some((c) => c.suggestions.length)) return false;
  return true;
}

function renderDictionaryList() {
  const list = clear($("#dict-tables"));
  const tables = (state.dict?.tables || []).filter(dictMatches);
  if (!tables.length) { list.append(h("p", { className: "muted" }, "No tables match.")); return; }
  for (const t of tables) {
    const roles = t.roles.map((r) => r.node_type || r.edge_type).filter(Boolean);
    list.append(h("button", { className: "item", "aria-current": String(state.dictTable === t.name), onclick: () => renderDictionaryTable(t.name) },
      h("span", { className: "title" }, t.name),
      h("span", { className: "small muted" },
        `${t.row_count ?? "?"} rows · ${t.coverage.described}/${t.coverage.columns} described · ${t.coverage.profiled}/${t.coverage.columns} profiled`,
        roles.length ? ` · ${[...new Set(roles)].join(", ")}` : "",
        t.in_inventory ? "" : " · not in latest inventory")));
  }
}

function roleChip(r) {
  return h("span", { className: `chip ${r.status}`, title: r.ref }, r.label + (r.status === "approved" ? "" : ` (${r.status})`));
}

function profileCell(p) {
  if (!p) return h("span", { className: "muted small" }, "not profiled");
  return h("div", { className: "small" },
    `${p.null_pct ?? "?"}% null · ${p.distinct_count ?? "?"} distinct${p.unique ? " · unique" : ""}`,
    p.value_shapes?.length ? h("div", { className: "muted" }, `shapes: ${p.value_shapes.join(", ")}`) : null,
    p.top_values?.length ? h("div", { className: "muted" }, `top: ${p.top_values.slice(0, 3).map((v) => `${v.value} (${v.count})`).join(", ")}`) : null);
}

function statusPill(status, source) {
  if (!status) return h("span", { className: "muted small" }, "undocumented");
  const cls = { verified: "completed", provisional: "pending", deprecated: "failed" }[status] || "";
  return h("span", { className: `pill ${cls}`, title: source ? `from ${source}` : "" }, status);
}

function valuesToText(values) { return Object.entries(values || {}).map(([k, v]) => `${k} = ${v}`).join("\n"); }

function textToValues(text) {
  const values = {};
  for (const line of text.split("\n")) {
    const i = line.indexOf("=");
    if (i > 0) values[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return values;
}

function editorForm(entry, { isColumn, onSave, onCancel }) {
  const description = h("textarea", { "aria-label": "Description" });
  description.value = entry.description_source === "sme" ? entry.description : (entry.description || "");
  const notes = h("textarea", { "aria-label": "Notes" });
  notes.value = entry.notes || "";
  const status = h("select", { "aria-label": "Status" }, ["provisional", "verified", "deprecated"].map((s) => h("option", { value: s }, s)));
  status.value = entry.status || "provisional";
  const values = isColumn ? h("textarea", { "aria-label": "Value meanings", placeholder: "C = Wireless\nO = Internet" }) : null;
  if (values) values.value = valuesToText(entry.values);
  const form = h("form", { className: "dict-editor" },
    h("label", {}, "Description", description),
    values ? h("label", {}, "Value meanings (one code = meaning per line)", values) : null,
    h("label", {}, "Notes (assumptions, caveats, open questions)", notes),
    h("label", {}, "Status", status),
    h("div", { className: "row" },
      h("button", { type: "submit", className: "primary" }, "Save"),
      h("button", { type: "button", onclick: onCancel }, "Cancel")));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const body = { description: description.value, notes: notes.value, status: status.value };
    if (values) body.values = textToValues(values.value);
    onSave(body);
  });
  setTimeout(() => description.focus(), 0);
  return form;
}

async function saveDictionaryEntry(table, column, entry, changes) {
  const author = dictAuthor();
  if (!author) { toast("Enter your name in Editor first", true); $("#dict-author").focus(); return false; }
  const source = encodeURIComponent($("#dict-source").value);
  const path = `/dictionary/${source}/tables/${encodeURIComponent(table)}` + (column ? `/columns/${encodeURIComponent(column)}` : "");
  try {
    await wsApi(path, { method: "PUT", body: JSON.stringify({ ...changes, author, expected_revision: entry.revision || 0 }) });
    toast(`Saved ${column ? `${table}.${column}` : table}`);
    await loadDictionary();
    return true;
  } catch (e) {
    toast(e.status === 409 ? `Someone else edited this entry. Reloaded; please re-apply your change.` : e.message, true);
    if (e.status === 409) await loadDictionary();
    return false;
  }
}

function suggestionList(items) {
  if (!items?.length) return null;
  return h("div", { className: "suggestions" }, items.map((s) => h("div", { className: "small" },
    h("span", { className: "pill pending" }, "suggested"), " ", s.description || s.hypothesis,
    Object.keys(s.values || {}).length ? ` (${Object.entries(s.values).map(([k, v]) => `${k} = ${v}`).join(", ")})` : "",
    " ", h("button", { type: "button", className: "inline-link", onclick: () => { showTab("review"); setTimeout(() => selectCandidate(s.ref), 300); } }, "Review"))));
}

function renderDictionaryTable(name) {
  state.dictTable = name;
  document.querySelectorAll("#dict-tables .item").forEach((el) => el.setAttribute("aria-current", String(el.querySelector(".title")?.textContent === name)));
  const t = state.dict.tables.find((x) => x.name === name);
  const panel = clear($("#dict-detail"));
  if (!t) return;
  const descriptionBlock = h("div", { id: "dict-table-description" });
  const showDescription = () => {
    clear(descriptionBlock);
    appendAll(descriptionBlock,
      h("p", {}, t.description || h("span", { className: "muted" }, "No description yet."),
        t.description_source && t.description_source !== "sme" ? h("span", { className: "muted small" }, ` (from ${t.description_source})`) : null),
      t.notes ? h("p", { className: "small" }, h("strong", {}, "Notes: "), t.notes) : null,
      t.rules?.length ? h("ul", { className: "small" }, t.rules.map((r) => h("li", {}, r))) : null,
      t.updated_by ? h("p", { className: "muted small" }, `Edited by ${t.updated_by} · ${fmtTime(t.updated_at)} · revision ${t.revision}`) : null,
      h("button", { type: "button", onclick: () => {
        clear(descriptionBlock).append(editorForm(t, {
          isColumn: false,
          onSave: (changes) => saveDictionaryEntry(t.name, null, t, changes),
          onCancel: showDescription,
        }));
      } }, "Edit table description"));
  };
  showDescription();

  const rows = [];
  for (const c of t.columns) {
    const row = h("tr", {},
      h("th", { scope: "row" }, c.name, c.primary_key ? h("span", { className: "muted small" }, " PK") : null,
        h("div", { className: "muted small" }, c.type || "")),
      h("td", {},
        c.description || h("span", { className: "muted" }, "—"),
        c.description_source && c.description_source !== "sme" ? h("div", { className: "muted small" }, `from ${c.description_source}`) : null,
        Object.keys(c.values || {}).length ? h("ul", { className: "values small" }, Object.entries(c.values).map(([k, v]) => h("li", {}, h("code", {}, k), ` ${v}`))) : null,
        c.notes ? h("div", { className: "small" }, h("strong", {}, "Notes: "), c.notes) : null,
        suggestionList(c.suggestions)),
      h("td", {}, c.roles.length ? c.roles.map((r) => h("div", {}, roleChip(r),
        r.references ? h("div", { className: "muted small" }, `→ ${r.references} · ${(r.row_coverage * 100).toFixed(1)}% rows match · ${r.unmatched_keys} unmatched`) : null))
        : h("span", { className: "muted small" }, "—")),
      h("td", {}, profileCell(c.profile)),
      h("td", {}, statusPill(c.status, c.description_source)),
      h("td", {}, h("button", { type: "button", "aria-label": `Edit ${c.name}`, onclick: () => {
        const editRow = h("tr", { className: "edit-row" }, h("td", { colspan: 6 }, editorForm(c, {
          isColumn: true,
          onSave: (changes) => saveDictionaryEntry(t.name, c.name, c, changes),
          onCancel: () => editRow.remove(),
        })));
        document.querySelectorAll("#dict-detail .edit-row").forEach((r) => r.remove());
        row.after(editRow);
      } }, "Edit")));
    rows.push(row);
  }

  appendAll(panel,
    h("h2", {}, t.name, " ", statusPill(t.status, t.description_source)),
    h("p", { className: "small muted" }, [
      t.row_count != null ? `${t.row_count} rows` : null, t.type, t.schema_fingerprint ? `schema ${t.schema_fingerprint}` : null,
      t.in_inventory ? null : "not in the latest inventory",
    ].filter(Boolean).join(" · ")),
    descriptionBlock,
    t.roles.length ? [h("h3", {}, "Graph roles"), h("div", { className: "chips" }, t.roles.map(roleChip))] : null,
    t.suggestions.length ? [h("h3", {}, "Pending suggestions"), suggestionList(t.suggestions)] : null,
    t.history.length ? [h("h3", {}, "Rejected interpretations"), h("ul", { className: "small" }, t.history.map((x) =>
      h("li", {}, x.hypothesis, x.sme_notes.length ? h("span", { className: "muted" }, ` (SME: ${x.sme_notes.join("; ")})`) : null)))] : null,
    h("h3", {}, `Columns (${t.coverage.described}/${t.coverage.columns} described, ${t.coverage.profiled} profiled)`),
    h("div", { className: "table-wrap dict-columns" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Column", "What we know", "Graph role", "Profile", "Status", ""].map((x) => h("th", {}, x)))),
      h("tbody", {}, rows))));
}

async function profileDictionary() {
  const source = $("#dict-source").value;
  try {
    const run = await wsApi("/runs", { method: "POST", body: JSON.stringify({ source, planner: "profile", limits: { max_iterations: 99 } }) });
    toast(`Profiling started (${run.id}). The dictionary refreshes when it finishes.`);
    const button = $("#dict-profile-btn");
    button.disabled = true;
    const poll = async () => {
      const r = await wsApi(`/runs/${encodeURIComponent(run.id)}?events=0`).catch(() => null);
      if (r && ["queued", "running", "stopping"].includes(r.status)) {
        $("#dict-summary").textContent = `Profiling… ${r.iterations?.at(-1)?.summary || "starting"}`;
        setTimeout(poll, 2000);
        return;
      }
      button.disabled = false;
      if (r) toast(`Profiling ${r.status}${r.termination_reason ? ` (${r.termination_reason})` : ""}`, r.status === "failed");
      if (state.ws && currentTab() === "dictionary") loadDictionary();
    };
    setTimeout(poll, 1500);
  } catch (e) { toast(e.message, true); }
}

async function exportDictionary() {
  const source = $("#dict-source").value;
  try {
    const headers = {};
    const token = localStorage.getItem("ddg-token");
    if (token) headers.Authorization = `Bearer ${token}`;
    const response = await fetch(`/api/w/${encodeURIComponent(state.ws)}/dictionary/${encodeURIComponent(source)}/markdown`, { headers });
    if (!response.ok) throw new Error(await response.text());
    const url = URL.createObjectURL(await response.blob());
    const link = h("a", { href: url, download: `${state.ws}-${source}-dictionary.md` });
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  } catch (e) { toast(`Export failed: ${e.message}`, true); }
}

// ---------- reset discovery ----------
async function openResetDialog() {
  const dialog = $("#reset-dialog");
  $("#reset-form").reset();
  $("#reset-error").textContent = "";
  $("#reset-confirm").placeholder = state.ws;
  try {
    const preview = await wsApi("/reset");
    document.querySelectorAll("#reset-dialog [data-count]").forEach((el) => {
      const n = preview.counts[el.dataset.count];
      el.textContent = `(${n} item${n === 1 ? "" : "s"})`;
    });
  } catch (e) { $("#reset-error").textContent = e.message; }
  dialog.showModal();
}

async function submitReset(event) {
  event.preventDefault();
  const scopes = [...document.querySelectorAll("#reset-form input[name=scope]:checked")].map((el) => el.value);
  if (!scopes.length) { $("#reset-error").textContent = "Select at least one item to archive."; return; }
  const actor = ($("#reviewer").value || localStorage.getItem("ddg-reviewer") || "").trim() || "ui";
  try {
    const result = await wsApi("/reset", { method: "POST", body: JSON.stringify({ scopes, actor, confirm: $("#reset-confirm").value.trim() }) });
    $("#reset-dialog").close();
    const moved = Object.entries(result.moved || {}).map(([k, v]) => `${v} ${k}`).join(", ");
    toast(moved ? `Archived ${moved}. The next run starts fresh.` : "Nothing to archive.");
    state.selectedRun = null;
    $("#run-detail").classList.add("hidden");
    loadRuns();
    refreshBadge();
  } catch (e) {
    $("#reset-error").textContent = e.message;
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
  $("#review-tier").addEventListener("change", loadCandidates);
  $("#reset-btn").addEventListener("click", openResetDialog);
  $("#reset-form").addEventListener("submit", submitReset);
  $("#reset-cancel").addEventListener("click", () => $("#reset-dialog").close());
  $("#dict-source").addEventListener("change", () => { state.dictTable = null; loadDictionary(); });
  $("#dict-filter").addEventListener("input", renderDictionaryList);
  $("#dict-show").addEventListener("change", renderDictionaryList);
  $("#dict-profile-btn").addEventListener("click", profileDictionary);
  $("#dict-export-btn").addEventListener("click", exportDictionary);
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
