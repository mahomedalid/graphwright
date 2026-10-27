---
title: Data Discovery Graph
description: Iterative tabular data discovery with SME review, graph-builder code generation, and a graph visualizer.
ms.date: 2026-10-27
---

## Getting Started

### 1. Try the sample

```powershell
uv sync
uv run ddg init-sample
uv run ddg serve
```

Open <http://127.0.0.1:8000> and select **Start run**.

### 2. Create your workspace

```powershell
uv run ddg workspace create mydata --use
```

This creates `workspaces/mydata/` with a blank `workspace.toml` and `topology.md`.

### 3. Add a data source

Edit `workspaces/mydata/workspace.toml`.

SQLite (put the file in `workspaces/mydata/data/`):

```toml
[sources.local]
type = "sqlite"
path = "data/my.sqlite"
```

Databricks (requires the [Databricks CLI](https://docs.databricks.com/dev-tools/cli/) and `databricks auth login`):

```toml
[sources.prod]
type = "databricks"
profile = "DEFAULT"
warehouse_id = "<sql-warehouse-id>"
catalog = "<catalog>"
schema = "<schema>"
```

Optionally allow raw sample values for non-sensitive columns:

```toml
[sampling]
allowlist = ["products.*", "*.category"]
```

### 4. Describe your graph

Edit `workspaces/mydata/topology.md`:

```markdown
## Node Types

### Customer

A person who buys things.

- Key: customer id
- Properties: name, email

### Order

A purchase.

- Key: order number

## Edge Types

### PLACED

- From: Customer
- To: Order
- Cardinality: one-to-many
```

Check it:

```powershell
uv run ddg topology
```

### 5. Connect Microsoft Foundry (optional)

Without Foundry, an offline rule-based planner runs instead.

```powershell
copy .env.example .env
az login
```

Set these in `.env`:

```text
FOUNDRY_PROJECT_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project>
FOUNDRY_MODEL=<deployment-name>
```

### 6. Run it

```powershell
uv run ddg serve
```

In the UI, with **mydata** selected:

1. **Runs**: select **Start run**.
2. **Review**: approve or reject each candidate, with notes.
3. **Facts & Code**: select **Generate graph builder code**, then run the command it shows.
4. **Graph**: search and explore.

## Overview

Data Discovery Graph explores messy tabular sources (tables, columns, metadata, and bounded row samples), proposes evidence-backed mappings onto a business graph topology you describe in Markdown, and routes every proposal to a subject matter expert (SME) for approval. Approved facts become inputs to generated graph-building code, and the UI visualizes the resulting graph with provenance back to source tables.

The implementation follows the [PRD](docs/prds/data-discovery-graph.md) "SQLite vertical slice" plus a Databricks CLI adapter.

Everything lives in a workspace: one self-contained folder per dataset or domain, with its own sources, topology, candidates, facts, runs, and graph output.

```text
workspaces/<name>/topology.md ──► discovery run (Foundry agent or offline planner)
                                     │ read-only, privacy-filtered tools
                                     ▼
                         candidates/*.json ──► SME review in UI ──► facts/*.json
                                                                       │
                               generated builder (sandbox/generated) ◄─┘
                                         │ run by a human
                                         ▼
                               graph_output/*.json ──► graph visualizer
```

## Workspaces

```text
ddg.toml                      global defaults: [run], [agent], [sampling], [model], default workspace
workspaces/
  _template/                  blank starter copied by "New workspace"
    workspace.toml
    topology.md
  retail/                     synthetic sample
    workspace.toml            sources, sampling allowlist, optional [run]/[agent] overrides
    topology.md               your business graph
    expected_mappings.json    ground truth for the sample (tests only)
    data/                     local data such as SQLite files
    candidates/               proposals and review history       (created at runtime)
    facts/                    SME-approved facts                 (created at runtime)
    metadata/                 source inventories                 (created at runtime)
    runs/                     run records, checkpoints, evidence (created at runtime)
    sandbox/generated/        generated graph builders           (created at runtime)
    graph_output/             builder output for the visualizer  (created at runtime)
```

Source paths in `workspace.toml` are relative to the workspace folder. Values in `workspace.toml` override `ddg.toml`.

The active workspace is resolved in this order: `-w/--workspace`, `DDG_WORKSPACE`, `workspaces/.active` (set by `ddg workspace use` or by switching in the UI), then `[workspaces].default` in `ddg.toml`.

```powershell
uv run ddg workspace list                              # * marks the active workspace
uv run ddg workspace create sales --description "Sales pilot" --use
uv run ddg workspace create retail-v2 --from retail     # copies workspace.toml and topology.md only
uv run ddg workspace use retail
uv run ddg -w sales topology                            # any command accepts -w
```

In the UI, the **Workspace** selector in the top bar switches every tab to that workspace, and **+ New** creates one from the blank template or by copying another workspace's configuration. A new workspace stays blocked until you add a source to its `workspace.toml` and at least one node type to its `topology.md`; the UI and CLI show what is missing.

## Quick Start

Prerequisites: Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```powershell
uv sync
uv run ddg init-sample          # builds workspaces/retail/data/retail.sqlite
uv run ddg serve                # http://127.0.0.1:8000
```

In the UI:

1. Pick the **retail** workspace, open **Runs**, and select **Start run**. Without Foundry settings, the offline heuristic planner runs.
2. Open **Review**, inspect each candidate's evidence, and approve or reject it with notes.
3. Open **Facts & Code** and select **Generate graph builder code**.
4. Review the generated script, then run the suggested command yourself from the repository root, for example:

   ```powershell
   python workspaces/retail/sandbox/generated/build_graph_retail_<timestamp>.py --sqlite workspaces/retail/data/retail.sqlite --out workspaces/retail/graph_output/retail.graph.json
   ```

5. Open **Graph**, search for a node, and expand its neighborhood.

The same workflow is available from the CLI (add `-w <workspace>` to target a non-active workspace):

```powershell
uv run ddg topology                                  # validate the Markdown topology
uv run ddg run --max-minutes 5                       # --source is optional when the workspace has one source
uv run ddg candidates --status pending
uv run ddg review <candidate-id> reject --reviewer "Ana" --notes "Backup table, not the system of record"
uv run ddg resume <run-id>                           # continue from a checkpoint
uv run ddg cancel <run-id>
uv run ddg generate --source retail
```

## Connecting Microsoft Foundry

The Foundry planner uses [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) (`Agent` with `FoundryChatClient`) and authenticates with `DefaultAzureCredential` (for example `az login`).

1. Copy `.env.example` to `.env`.
2. Set `FOUNDRY_PROJECT_ENDPOINT` and `FOUNDRY_MODEL` (a deployment that supports tool calling).
3. Keep `planner = "auto"` in `ddg.toml`, or choose **Foundry agent** when starting a run.

Each iteration gives the agent the topology, the review snapshot (approved, pending, and rejected candidates with SME notes), and its own notes from earlier iterations. Agent Framework middleware checks run budgets before every model and tool call, records token usage for cost limits, and fails closed when a limit or cancellation is hit.

> [!IMPORTANT]
> Allowlisted raw values from `[sampling].allowlist` in each `workspace.toml` can be sent to the model. Review the allowlist and `max_rows` before pointing the agent at production data.

## Connecting Databricks

Install and authenticate the [Databricks CLI](https://docs.databricks.com/dev-tools/cli/), then add a source to the workspace's `workspace.toml`:

```toml
[sources.prod]
type = "databricks"
profile = "DEFAULT"
warehouse_id = "<sql-warehouse-id>"
catalog = "<catalog>"
schema = "<schema>"
```

Statements run through the SQL Statement Execution API (`databricks api post /api/2.0/sql/statements`) with a wait timeout and `CANCEL` on timeout. Use a principal with read-only grants. Generated builders read Databricks through `databricks-sql-connector` using `DATABRICKS_SERVER_HOSTNAME`, `DATABRICKS_HTTP_PATH`, and `DATABRICKS_TOKEN`.

## Topology Format

Each workspace's `topology.md` defines its business graph. The product has no built-in entity types; `workspaces/_template/topology.md` contains a format reference.

```markdown
# Graph Topology: My Domain

## Node Types

### Customer

A person or organization that buys products.

- Key: customer identifier
- Properties: name, email, city
- Synonyms: client, buyer

## Edge Types

### PLACED

- From: Customer
- To: Order
- Cardinality: one-to-many
- Properties: channel

## Constraints

- Every Order is placed by exactly one Customer.

## Hints

- Identifiers may differ in prefix, padding, or case across systems.
```

Validation runs before every discovery run. Undefined edge endpoints, duplicate names, invalid cardinalities, and missing node types are errors. Missing descriptions or key hints are warnings. Every candidate and fact records the topology version (a content hash).

## Safeguards

| Area                | Behavior                                                                                                                         |
|---------------------|----------------------------------------------------------------------------------------------------------------------------------|
| Source protection   | The agent never writes SQL. Adapters build statements from inventory-validated identifiers, and a guard rejects non-SELECT text. |
| SQLite              | Opened with `mode=ro` and `PRAGMA query_only`, with a per-query timeout.                                                         |
| Privacy             | Raw values only for allowlisted `table.column` globs, capped at `max_rows`. Other columns expose counts and value shapes only.   |
| Prompt injection    | Tool output is labeled as untrusted data in agent instructions; tools cannot change policy or permissions.                       |
| Budgets             | Time, iterations, model calls, tool calls, and cost limits; stall detection; cancellation before the next dispatch.              |
| Feedback reuse      | A rejected interpretation cannot be resubmitted without different evidence and a `new_evidence_note`.                           |
| Persistence         | Atomic writes, single-writer lock, optimistic version checks on review, fact-first promotion with startup reconciliation.        |
| Revalidation        | Topology or schema fingerprint changes block code generation until facts are revalidated (or `--allow-stale`).                   |
| Generated code      | The product only writes code. A human reviews and runs it; approving a candidate never triggers execution.                       |
| API access          | Set `DDG_API_TOKEN` to require `Authorization: Bearer <token>` on every API call.                                                |

## Artifacts

All paths are relative to `workspaces/<name>/`.

| Location                   | Contents                                                                            |
|----------------------------|-------------------------------------------------------------------------------------|
| `topology.md`              | User-authored topology                                                              |
| `workspace.toml`           | Sources, sampling allowlist, and overrides of `ddg.toml`                            |
| `facts/fact-*.json`        | SME-approved facts with mapping, evidence, reviewer, notes, and versions            |
| `candidates/cand-*.json`   | Candidates with evidence, limitations, and decision history                         |
| `metadata/<source>/`       | Source inventory with schema fingerprints                                           |
| `runs/<run-id>/`           | Run record, checkpoint, evidence files, and correlated events (no raw row contents) |
| `sandbox/generated/`       | Generated builders and manifests                                                    |
| `graph_output/`            | Builder output (`ddg-graph/v1` JSON, optional GraphML) loaded by the visualizer     |

Runtime folders and local SQLite files are ignored by Git; `workspace.toml`, `topology.md`, and fixtures are tracked.

## Graph Output Contract

Generated builders write `ddg-graph/v1` JSON with `inputs` (workspace, source, topology version, fact and candidate versions), `stats`, `warnings`, and sorted `nodes` and `edges`. Node IDs are `<Type>:<canonical key>`. Edge IDs are `<EDGE>:<from>-><to>`, deduplicated by default. Every element lists provenance (fact, table, and row count). Identical inputs produce identical nodes and edges.

## Development

```powershell
uv run pytest
```

The suite copies `ddg.toml`, `workspaces/_template`, and the retail workspace into a temporary root. It uses the synthetic retail fixture (implicit joins with inconsistent formats, orphan keys, and decoy tables) and checks discovery against `workspaces/retail/expected_mappings.json`.

## Open Items From the PRD

* Numeric production defaults for time, cost, and sample limits (current values live in `ddg.toml` and each `workspace.toml`)
* Identity provider and role-based permissions (only an optional shared API token today)
* Hosting for long-running runs (runs execute in the API process or the CLI)
* Concurrency policy beyond the single-writer lock for multiple reviewers and runs
