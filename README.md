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

### Validate an existing topology

Select **Validate topology against the data** when starting a run. This objective
is independent of conservative/exploratory mode and works with the Foundry and
offline heuristic planners, but not the profile-only planner.

The run details show evidence-backed findings marked **supported**, **mismatch**,
or **inconclusive**, with recommendations for revising keys, properties, or
relationship cardinality. Foundry can also recommend new types and test other
topology assumptions. The offline planner uses naming heuristics and key/join
checks; it does not interpret arbitrary business constraints. Untested elements
and constraints remain visible, including when a budget stops the run early.
Missing columns or sample overlap are not proof that a topology element is wrong.

Recommendations are advisory: they never change your topology or approve facts.
Review the evidence and business meaning, edit the topology yourself, then run
validation again. Resume keeps the original validation objective and findings
when the topology version is unchanged.

```powershell
uv run ddg run --validate-topology --planner heuristic --max-iterations 20
```

The API accepts `"validate_topology": true` on a run request. Run responses persist
`topology_findings`, including recommendations, limitations, and cited evidence.
CLI validation runs print these findings after execution.

### Discover relationships only

Choose **Discovery target > Relationships only** to investigate edges without
proposing new nodes, annotations, or observation candidates. Existing approved
node facts take precedence over pending node mappings; pending endpoint
dependencies are flagged in relationship proposals.

```powershell
uv run ddg run --planner heuristic --discovery-scope edges --max-iterations 20
```

The API accepts `"discovery_scope": "edges"` (`"all"` is the default). Resumed runs
keep their original scope; start a new run to change it. Edges-only discovery can
be combined with topology validation, which then produces relationship findings
only. Profile-only runs cannot use this scope.

Relationships without existing endpoint mappings are reported as blocked, not
silently treated as discovered. Run full discovery or review endpoint mappings
first. A run with blocked relationships ends with `missing_endpoint_mappings`.
The Foundry planner uses isolated shared rules, scope-specific prompts, and an
optional topology-validation prompt. Tool-side guards also reject non-edge
proposals, so scope is not enforced by prompting alone.

### Source-record identity and composite keys

Generated node IDs are single readable strings:
`<entity-type>:<identity-namespace>:<ordered-normalized-key-values>`.
For example, `Device:IMEI:012312389` or `Account:V21_BAN:12345`.
Each component is UTF-8 percent-escaped, including embedded colons and percent
signs, so composite values cannot collide through separator ambiguity.

Node mappings require an explicit `identity_namespace` describing the semantic
identifier scope. Include source system, issuer or line of business when needed
to distinguish identifiers. Tables, source connection names, and normalization
rules are not automatically embedded in the ID. Renaming a table or connection
does not change IDs when the semantic contract and normalized values stay the same.
Normalization changes that alter values can change IDs.

A single source ID column is valid inside this composite namespace. Multiple
columns are supported when the source-record grain requires them. Different
mappings of the same node type can have different key shapes; matching values
or matching key counts never establish shared identity. Mappings may deliberately
share a namespace only when evidence supports the same identity. Their ordered
`identity_key_parts` (semantic names, defaulting to `key_columns`) and
`key_transforms` must agree, even if physical column names differ. Choose different
namespaces for different identifier kinds. IMEI and serial-number records do not
become the same Device automatically.

The offline planner proposes conservative source-record-scoped namespaces and
flags that limitation for review. The Foundry planner can propose semantic
namespaces based on evidence; neither planner approves mappings.

Relationship mappings use `from_mapping_id` and `to_mapping_id` to select
specific endpoint contracts. Discovery exposes every active node contract and
its stable candidate ID; approved fact IDs are also accepted as references.
Each relationship must provide all endpoint key parts in order, with transforms
that produce the endpoint's normalized values. Discovery validates this in both
full and edges-only runs, and records the selected endpoint IDs before review.
Pending endpoint approvals remain explicit dependencies.

Existing approved facts are not rewritten. Node approvals without an explicit
semantic namespace are flagged for corrected proposals and review, not assigned
one automatically. A legacy relationship without endpoint
references can be built only when each endpoint has one available approved
mapping. Ambiguous references, unavailable targets, and missing key parts appear
in fact-validation and code-generation errors. Review a corrected mapping or
explicitly exclude the relationship; renewing an old approval cannot repair it.

Newly generated builders record `inputs.identity_format = "semantic/v1"` and
the identity contracts in graph metadata. Their node IDs differ from older
`Type:key` and table-scoped hashed IDs. Regenerate and review your builder, then rebuild your graph to
use the new IDs; existing graph snapshots and saved approvals stay unchanged.
Key-value search remains available in both JSON and SQLite graphs.

### Databricks notebook export

Export approved SQLite-discovered rules for execution against the original
Databricks sources. In Facts, choose the Databricks notebooks target, review
the selected facts and table-binding overrides, then download the ZIP.
The existing standalone builder remains the default.

```powershell
uv run ddg --workspace mydata codegen --source local --target databricks-notebooks
```

Bindings come from the SQLite `_sample_manifest` (`sqlite_table` to
`source_table`). Missing or ambiguous bindings require explicit
`--table-bindings '{"local_table":"catalog.schema.original_table"}'` overrides;
names are never guessed from flattened underscores. The workspace API accepts
the same `target`, `table_bindings`, and `exclude_fact_ids` options on its
code-generation endpoint and provides an authorized ZIP download.

Import the four source notebooks into one Databricks folder:

1. `00_configure_preflight`: edit bindings, Delta versions, output names and
   policies; verify schemas and GraphFrames Python/JVM compatibility.
2. `10_build_validate`: build distributed `vertices_df`, `edges_df`, and `g`,
   then inspect aggregate diagnostics and restricted invalid-row DataFrames.
3. `20_publish`: rebuild in its own session, review that exact build, and
   separately confirm replacement of the two current-state managed Delta tables.
4. `30_explore`: choose a fresh in-memory build or a version-pinned,
   consistency-checked published graph.

The package embeds frozen mapping/endpoint contracts, approval revisions,
topology, exclusions and a deterministic rules fingerprint. It contains no
source rows, credentials, saved outputs, or repository imports. Export does
not execute notebooks or change approvals. Unsupported derivations, stale
approvals, missing semantic namespaces, and unstable `dedupe=false` edge
identities block export with per-fact errors.

Python worker UDFs preserve the local builder's exact Unicode, normalization
and UTF-8 percent-escaping semantics. Keys support strings, integral numbers
and booleans. Scalar properties preserve JSON types; decimals, dates,
timestamps and binary use the local builder's string representation.
Complex properties and other key types require reviewed mappings.
Timestamp properties require UTC workers as well as a UTC Spark session.
Legacy `int` normalization still has its documented float-conversion precision
limit; review large business identifiers rather than changing identity only
in Spark.

> [!WARNING]
> Publication is disabled by default. It requires dedicated target ownership,
> a protected scratch directory, an externally coordinated single writer, and
> confirmation bound to both targets and the exact build. Two Delta overwrites
> are not atomic. Guarded readers reject mixed pairs; other readers can observe
> a replacement window. Failed stages are retained for manual recovery, not
> automatically rolled back or deleted. Excluded types disappear on replacement.

Run local export/guard tests with `python -m pytest
tests/test_databricks_export.py tests/test_notebook_integration.py`.
Optional distributed tests in `tests/test_notebook_spark.py` require
PySpark 3.5.8 and Java 17 (or `jdk4py==17.0.9.2`). These are test-only
prerequisites, not application dependencies. Local export tests do not certify
a Databricks Runtime/GraphFrames combination. Your administrator must provision
compatible compute; the notebooks install nothing automatically.
See the [notebook export PRD](docs/prds/databricks-graph-notebook-export.md)
and the package README for execution, replacement and recovery details.

### 2. Create your workspace

```powershell
uv run ddg workspace create mydata --use
```

This creates `workspaces/mydata/` with a blank `workspace.toml` and `context/topology.md`.

### 3. Add a data source

Edit `workspaces/mydata/workspace.toml`.

SQLite (put the file in `workspaces/mydata/data/`):

```toml
[sources.local]
type = "sqlite"
path = "data/my.sqlite"
```

Databricks (see [Connecting Databricks](#connecting-databricks) for the full setup):

```toml
[sources.prod]
type = "databricks"
profile = "customer"
warehouse_id = "<sql-warehouse-id>"
catalog = "<catalog>"
schema = "<schema>"
tables = ["orders", "customers", "crm.accounts"]
```

Check the connection (lists tables in scope, never row values):

```powershell
uv run ddg sources check
```

Optionally allow raw sample values for non-sensitive columns:

```toml
[sampling]
allowlist = ["products.*", "*.category"]
```

### 4. Describe your graph

Edit `workspaces/mydata/context/topology.md`:

With Foundry configured, you can write an open-format description instead of using
the headings below. For example:

```text
Customers place orders. A customer is identified by customer number and has a name.
An order is identified by order number and has an order date.
Each order belongs to one customer; a customer can place many orders.
```

For offline use without Foundry, use the structured format:

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

For free-form input, this command calls Foundry to extract a structured topology,
then validates it. An auto/Foundry discovery run also does this at startup, within
its model-call, token-cost, time, and cancellation budgets. Structured input that
already validates does not need an extraction call. Authentication or permission
errors are reported; configuring Foundry does not guarantee access.

The original document is never overwritten. Validated interpretations are cached in
`metadata/topology.json` and reused by the UI, discovery, fact revalidation, and code
generation. Editing the source invalidates the cache. Review the extracted node and
edge types in **Topology & Metadata**, because LLM interpretation can be incorrect.
Additional reference documents cannot define or override the topology.

Opening the UI or listing workspaces never calls the model. Until extraction runs,
the UI shows **awaiting Foundry parsing**. Without Foundry or a current cached
interpretation, invalid structured input reports both remedies: configure Foundry
or use the structured format. An explicitly selected heuristic run does not call
Foundry, but it can reuse an existing validated interpretation.

Put additional Markdown references, such as data dictionaries or previous findings, in
`workspaces/mydata/context/`. All `.md` files are read recursively, including subfolders;
other file types are ignored. Only the configured topology file defines graph types.
Additional documents are passed verbatim, with filenames, to the Foundry planner as
unverified background material in every iteration. They do not become approved facts
or executable instructions. The offline heuristic planner does not interpret them.

Documents are loaded once at the start of each run; start a new run to pick up edits.
The run record lists additional documents in `context_files`. Open a run's
**Run settings and usage** to see **Context references loaded**. The `context_loaded`
technical event records `file_count` and the workspace-relative `files` list, without
document contents. These list files loaded, not proof that the model used every
reference in its reasoning. The configured topology is loaded separately and is
not included in this reference list. Files must be UTF-8; unreadable files fail the
run rather than being silently omitted.

> [!WARNING]
> Free-form topology and reference documents are sent to the configured model without the source-column
> sampling filter. Include only content you are authorized to share with that model.
> All reference text is included without truncation, so keep documents concise to
> control token costs and avoid model context-limit errors.

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

Runs default to 3 iterations, with an allowed range of 1 through 99. Override
`[run].max_iterations` in your configuration, use `--max-iterations` in the CLI,
or enter a limit in the UI. A short run can finish before mapping every graph type;
resume it or raise the limit when more discovery is needed.

The Runs UI refreshes every 5 seconds while runs are active. Refresh requests run
sequentially, and transient refresh failures are shown and retried. The Events panel
shows startup stages, topology parsing, model request start/completion/failure,
tool start/completion/failure with durations, and iteration/run outcomes. Model
events contain operational metadata and usage, not prompts or model responses.
Model waits are bounded by the remaining run time budget.

In the UI, with **mydata** selected:

1. **Runs**: select **Start run**.
2. **Review**: approve or reject each candidate, with notes.
3. **Facts & Code**: select **Generate graph builder code**, then run the command it shows.

For an approved fact that cannot be corrected or revalidated, select **Delete fact**
in **Facts & Code**. Supply your reviewer name and reason, and type its exact fact
ID to confirm. Only that fact is removed from active knowledge. Its original
record, evidence, approval history, and deletion audit remain stored, and its
candidate becomes rejected so the same interpretation is not immediately
rediscovered without new evidence.

Dependent relationship facts are not deleted automatically. Review them after
deleting a node fact. Previously generated builders and graph snapshots are
unchanged; generate a new builder to use the remaining active facts.
Deletion is also available for structurally invalid mappings and when the source
is unreachable. The API uses `DELETE /api/w/{workspace}/facts/{fact_id}` with
`reviewer`, `notes`, `confirm` (the fact ID), and `expected_token` from the fact
revalidation report. Stale review tokens are rejected.
4. **Dictionary**: select **Profile columns**, then document tables and columns (see [Data Dictionary](#data-dictionary)).
5. **Graph**: search and explore.

### Recover from blocked code generation

The **Facts & Code** tab distinguishes old approvals from invalid mappings:

* An old approval means the graph definition or source schema changed since
  review. Select **Review old approval**, read the mapping and its limitations,
  enter your name and review notes, and confirm only if its business meaning
  is still correct. **Validate and confirm approval** checks source columns,
  schema, and endpoint ID structure before recording a new fact revision.
* An invalid mapping, such as a relationship supplying one ID part for a node
  with a two-part ID, cannot be fixed by renewing an approval. Review a corrected
  mapping, or uncheck **Include** on that fact to leave it out of this build.
  Excluding a node mapping can also require excluding its dependent relationships.

Revalidation preserves the original approval, evidence, and previous versions
in a review history. It does not rerun profiling, uniqueness checks, or joins,
and structural compatibility does not prove business meaning. Source access
is required; concurrent changes require you to reload and review again.

Build exclusions never delete or retire approved facts. They apply to the
requested builder and are recorded in its manifest and warnings. Nothing is
excluded automatically. Recheck the selection when you reload or switch sources.
Generate again after resolving the blockers to get the smaller, selected graph.

**Bypass old-approval warnings** is an explicit override, not revalidation.
It cannot fix missing endpoint mappings or incompatible IDs.
The CLI also supports repeated `--exclude-fact FACT_ID` options on `ddg generate`.

### Find connected nodes

The **Graph** tab ranks starting nodes by their connectivity across the whole
generated graph, not just the nodes currently displayed. Use **2+ edges** for
branching records, **Hubs: 5+ neighbors** for nodes connected to many distinct
records, or **2+ relationship types** for nodes with varied relationships.
Shortcuts reset the other search filters.

Combine node text/type searches with a relationship type, incoming/outgoing
direction, neighbor type, and minimum edge, neighbor, or relationship-type counts.
Sort by most edges, most neighbors, most relationship types, or node ID.
Counts and ranking use edges matching the selected filters. Parallel edges
count separately; distinct neighbors exclude self-links.

Select a result to load its neighborhood. Relationship, direction, and neighbor
filters apply at each expansion hop; minimum counts only filter starting nodes.
Use **Depth** to explore up to three hops, **Fit view** to recenter the canvas,
and the text view to inspect nodes and edges without the visualization library.
Search displays at most 100 results and each neighborhood request is limited
to 200 nodes, with a visible notice when either limit is reached.

A graph with no edges shows an explicit warning. Filters cannot recover edges
that were not generated or were skipped because their endpoint nodes were missing.

## Data Dictionary

The **Dictionary** tab shows everything known about each table and column in one place, and you can start using it before writing a topology.

| What you see                          | Where it comes from                                                                              |
|---------------------------------------|--------------------------------------------------------------------------------------------------|
| Tables, columns, types, row counts    | Source inventory collected by any run                                                           |
| Null %, distinct counts, value shapes | **Profile columns** (a `profile` run); top values only for allowlisted columns                  |
| Graph roles                           | Approved facts and pending candidates (node keys, properties, edge keys with join coverage)     |
| Descriptions and code meanings        | SME edits in the tab, approved `annotation` facts proposed by the agent, or source comments     |
| Rejected interpretations              | Rejected candidates and their SME notes, so nobody repeats them                                 |

Description precedence is SME edit, then approved annotation fact, then source comment; the tab shows where each came from. Each entry has a status (`provisional`, `verified`, `deprecated`), notes for assumptions and open questions, and a revision so concurrent edits are detected.

The Foundry agent reads the dictionary (`get_dictionary` tool) and can propose `annotation` candidates, such as code meanings or business rules, backed by evidence. They appear in **Review** and as suggestions in the Dictionary until an SME approves them.

```powershell
uv run ddg run --planner profile --max-iterations 50   # collect column statistics only
uv run ddg dictionary                                  # export Markdown + JSON to dictionary/exports/
```

**Export Markdown** in the tab downloads the same document.

## Confidence, Modes, and Resetting Discovery

### Confidence tiers

Every candidate is labeled **high**, **medium**, or **low** confidence, with the reasons shown in **Review**. The tier is computed from the cited evidence; the agent can lower it but never raise it.

| Kind         | High                                                | Medium                                                | Low                                           |
|--------------|-----------------------------------------------------|-------------------------------------------------------|-----------------------------------------------|
| Node mapping | Key unique and non-null on full data                | Unique in a sample, or under 1% duplicate keys        | Key not unique, or never checked              |
| Edge mapping | 90% or more of rows match the referenced key (full) | 50% or more match, or high overlap only in a sample   | Low or no overlap; at best compatible formats |
| Annotation   | (not used)                                          | Backed by a profile or sample of the annotated column | No profile or sample of the column cited      |

An edge whose observed cardinality conflicts with the topology drops one tier. **Review** sorts by tier and has a tier filter; `ddg candidates --tier high` does the same.

### Conservative and exploratory modes

| Mode                     | Behavior                                                                                                                        |
|--------------------------|---------------------------------------------------------------------------------------------------------------------------------|
| `conservative` (default) | Low-confidence proposals are refused; the agent records them as notes instead                                                   |
| `exploratory`            | Low-confidence hypotheses are accepted and labeled, so the agent proposes its best guess for every uncovered node and edge type |

Pick the mode per run in the UI or with `ddg run --mode exploratory`, or set a workspace default:

```toml
[agent]
mode = "exploratory"
```

### Independently sampled data

When every table is sampled separately (for example `LIMIT 4000` per table), true foreign keys rarely overlap, so join checks cannot confirm relationships. Mark the source so the agent stops treating zero overlap as disproof and relies on the `check_compatibility` tool (types, value shapes after transforms, and naming) instead:

```toml
[sources.v2]
type = "sqlite"
path = "data/household-samples-v2.sqlite"
independent_samples = true
```

Relationships found this way are at most **low** confidence. To get high-confidence relationships, extract samples that keep keys consistent, for example sample a set of accounts first and then pull related rows by those keys.

### Reset discovery

**Reset discovery…** on the **Runs** tab (or `ddg reset`) archives discovery state to `archive/reset-<timestamp>/` so the next run starts fresh. Nothing is deleted, and topology, context documents, dictionary annotations, and configuration are never touched.

```powershell
uv run ddg reset --fresh                 # runs, pending candidates, inventories (keeps approved and rejected)
uv run ddg reset --rejected --yes        # lets the agent propose previously rejected ideas again
uv run ddg reset --approved              # archives approved facts too (they need re-approval)
```

Archiving runs also clears column profiles in the Dictionary. Active runs must be cancelled first.

## Overview

Data Discovery Graph explores messy tabular sources (tables, columns, metadata, and bounded row samples), proposes evidence-backed mappings onto a business graph topology you describe in Markdown, and routes every proposal to a subject matter expert (SME) for approval. Approved facts become inputs to generated graph-building code, and the UI visualizes the resulting graph with provenance back to source tables.

The implementation follows the [PRD](docs/prds/data-discovery-graph.md) "SQLite vertical slice" plus a Databricks CLI adapter.

Everything lives in a workspace: one self-contained folder per dataset or domain, with its own sources, topology, candidates, facts, runs, and graph output.

```text
workspace topology + context ──► discovery run (Foundry agent or offline planner)
                                     │ read-only, privacy-filtered tools
                                     ▼
                         candidates/*.json ──► SME review in UI ──► facts/*.json
                                                                       │
                               generated builder (sandbox/generated) ◄─┘
                                         │ run by a human
                                         ▼
                               graph_output/*.json or *.sqlite ──► graph visualizer
                                         └─ *.cypher ──► Neo4j (manual import)
```

## Workspaces

```text
ddg.toml                      global defaults: [run], [agent], [sampling], [model], default workspace
workspaces/
  _template/                  blank starter copied by "New workspace"
    workspace.toml
    context/
      topology.md             graph definition
      notes.md                optional Markdown references, including subfolders
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

New workspaces use `context/topology.md`. Existing root-level `topology.md` files
remain supported. Without an explicit setting, `context/topology.md` takes precedence
when present, otherwise the root-level file is used. To select a different topology:

```toml
[topology]
file = "context/topology.md"
```

Additional references are always read from the workspace's `context/` folder, regardless
of the topology location. The configured topology is excluded from those references
because its parsed summary is already supplied separately.

The active workspace is resolved in this order: `-w/--workspace`, `DDG_WORKSPACE`, `workspaces/.active` (set by `ddg workspace use` or by switching in the UI), then `[workspaces].default` in `ddg.toml`.

```powershell
uv run ddg workspace list                              # * marks the active workspace
uv run ddg workspace create sales --description "Sales pilot" --use
uv run ddg workspace create retail-v2 --from retail     # copies config, topology, and context Markdown
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

Discovery talks to Databricks only through the [Databricks CLI](https://docs.databricks.com/dev-tools/cli/), using the SQL Statement Execution API on a SQL warehouse. Authentication is whatever your CLI profile uses (OAuth sign-in, Azure CLI, or a token); no credentials are stored in this repository.

1. Install the CLI and sign in to the customer workspace:

   ```powershell
   winget install Databricks.DatabricksCLI
   databricks auth login --host https://adb-<id>.<n>.azuredatabricks.net --profile customer
   databricks current-user me --profile customer
   ```

2. Find a SQL warehouse ID (a small serverless warehouse is enough):

   ```powershell
   databricks warehouses list --profile customer
   ```

3. Make sure the signed-in identity has read-only access: `CAN USE` on the warehouse, plus `USE CATALOG`, `USE SCHEMA`, and `SELECT` on the tables in scope. Ideally use an identity that has no write grants.

4. Create a workspace and add the source with your initial set of tables:

   ```powershell
   uv run ddg workspace create customer --description "Customer pilot" --use
   ```

   ```toml
   # workspaces/customer/workspace.toml
   [sources.prod]
   type = "databricks"
   profile = "customer"
   warehouse_id = "<sql-warehouse-id>"
   catalog = "<catalog>"
   schema = "sales"                                   # default schema for bare table names
   tables = ["orders", "order_lines", "crm.accounts", "ref_*"]
   query_timeout_seconds = 120
   ```

   `tables` limits discovery to these tables; anything else in the catalog cannot be described, sampled, or joined. Entries are `table` (in `schema`) or `schema.table`, and `*` globs work in the table part. Omit `tables` to scan the whole default schema. Tables from other schemas appear as `schema.table`.

5. Check the connection and scope:

   ```powershell
   uv run ddg sources check
   ```

   It lists each table in scope with its column count and flags entries that do not exist or are not visible to you. No row values are read.

6. Decide which columns may expose raw sample values to the agent (and to Foundry). Patterns match `table.column`, where `table` is the name shown by `sources check`:

   ```toml
   [sampling]
   allowlist = ["ref_*.*", "orders.status", "crm.accounts.region"]
   ```

7. Write `topology.md`, then run discovery from the UI or with `uv run ddg run`.

Each discovery query runs as a separate CLI call, so expect a second or two of overhead per query. Statements that exceed `query_timeout_seconds` are cancelled on the warehouse.

Generated builders read Databricks through `databricks-sql-connector` (`pip install databricks-sql-connector`). Set `DATABRICKS_HOST`; the builder uses the discovery warehouse unless `DATABRICKS_HTTP_PATH` is set, and signs in with `DATABRICKS_TOKEN` when present or a browser OAuth flow otherwise.

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

| Location                      | Contents                                                                            |
|-------------------------------|-------------------------------------------------------------------------------------|
| `topology.md`                 | User-authored topology                                                              |
| `workspace.toml`              | Sources, sampling allowlist, and overrides of `ddg.toml`                            |
| `facts/fact-*.json`           | SME-approved facts with mapping, evidence, reviewer, notes, and versions            |
| `candidates/cand-*.json`      | Candidates with evidence, limitations, and decision history                         |
| `metadata/<source>/`          | Source inventory with schema fingerprints                                           |
| `runs/<run-id>/`              | Run record, checkpoint, evidence files, and correlated events (no raw row contents) |
| `sandbox/generated/`          | Generated builders and manifests                                                    |
| `graph_output/`               | JSON or SQLite graphs for the visualizer; optional Cypher and GraphML exports         |
| `dictionary/annotations.json` | SME-curated table and column descriptions, code meanings, notes, and status         |
| `dictionary/history.jsonl`    | Audit log of dictionary edits (who, when, what)                                     |
| `dictionary/exports/`         | Generated Markdown and JSON dictionary exports                                      |

Runtime folders, dictionary exports, and local SQLite files are ignored by Git; `workspace.toml`, `topology.md`, dictionary annotations and history, and fixtures are tracked.

## Graph Output Contract

Generated builders write `ddg-graph/v1` JSON with `inputs` (workspace, source, topology version, fact and candidate versions), `stats`, `warnings`, and sorted `nodes` and `edges`. Node IDs are `<Type>:<canonical key>`. Edge IDs are `<EDGE>:<from>-><to>`, deduplicated by default. Every element lists provenance (fact, table, and row count). Identical inputs produce identical nodes and edges.

### Choose graph storage and export formats

The JSON graph is a static snapshot, not a connection to the source tables. Approved
mappings produce a standalone Python builder; you review and run it yourself.
Existing `--out graph.json` commands still work. Newly generated builders also support:

| Builder flag             | Output                                            | Where to use it                  |
|--------------------------|---------------------------------------------------|----------------------------------|
| `--out PATH`             | Existing `ddg-graph/v1` JSON snapshot              | Graph tab                        |
| `--sqlite-out PATH`      | Indexed SQLite graph database                     | Graph tab or your own SQL queries |
| `--cypher PATH`          | Neo4j 5+ import script, with no APOC dependency    | Import using `cypher-shell`       |
| `--graphml PATH`         | GraphML                                           | External graph tools             |

Use the JSON, SQLite, or Neo4j command shown under **Generated code** and **Previous
builders**. You can combine output flags to build multiple formats from the same
source read, or omit `--out` for a SQLite-only or Cypher-only build. At least one
output is required. Output paths must be different and cannot overwrite the SQLite
source database. Regenerate older builders to get these flags.

```powershell
python .\path\to\builder.py --sqlite .\source.sqlite --out .\graph_output\graph.json --sqlite-out .\graph_output\graph.sqlite --cypher .\graph_output\graph.cypher
cypher-shell -a neo4j://localhost:7687 -u neo4j -f .\graph_output\graph.cypher
```

In the Graph tab, choose either the JSON or SQLite entry under **Graph dataset**.
Both use the same search, relationship filters, connectivity rankings, neighborhood
expansion, and provenance panels. Save outputs under the workspace's `graph_output`
directory; SQLite files use `.sqlite` or `.db`. Cypher files are not visualizer
datasets, and this release does not connect the UI directly to Neo4j.

### SQLite graph schema and lifecycle

SQLite stores graph elements in two tables, with a small `graph_metadata` table
for snapshot-level inputs, statistics, warnings, format, and build time:

| Table   | Identity and relationship columns                   | JSON text columns          |
|---------|------------------------------------------------------|----------------------------|
| `nodes` | `system_id`, `id`, `type`, `key`                      | `properties`, `provenance` |
| `edges` | `system_id`, `id`, `type`, `source`, `target`           | `properties`, `provenance` |

`system_id` is an integer storage surrogate, local to each table and snapshot.
Use the deterministic, unique `id` for integration and cross-format identity, not
`system_id`. Node `key` is the canonical business key. Edge `source` and `target`
reference node `id` with foreign keys. Properties named `id`, `key`, or `system_id`
remain inside the JSON payload and do not overwrite identity columns.

The database has indexes on node type and edge source, target, and type, with JSON
validity checks. Its schema version is `PRAGMA user_version = 1`.

```sql
SELECT id, type, key, json_extract(properties, '$.name') AS name
FROM nodes
WHERE type = 'Customer';

SELECT source, type, target, properties
FROM edges
WHERE source = 'Customer:12';
```

The backend uses a read-only transaction per request and performs filtering,
ranking, and incident-edge lookup in SQL instead of caching the entire database
in memory. Valid committed database changes are visible on the next request;
refresh or reselect the dataset to refresh the UI. The source tables are not
automatically synchronized: rerun the builder to rebuild the snapshot.

Rebuilding SQLite atomically replaces the whole graph, removing obsolete elements.
It does not incrementally merge or preserve manual edits, and surrogate
`system_id` values may change. The builder still constructs the graph in memory
before writing exports; SQLite query support does not make graph generation streaming.

### Neo4j import semantics

Cypher creates a unique `DDGNode.ddg_id` constraint, business-type node labels,
and directed relationships using the topology's relationship types. It uses
`MERGE` with deterministic IDs, so rerunning an import does not duplicate nodes,
parallel relationships, or self-links. Each relationship retains its own `ddg_id`.

Source properties are native Neo4j properties where supported. The reserved
`ddg_id`, `ddg_type`, and node `ddg_key` fields hold identity.
`ddg_properties_json` preserves the full original property map, including nulls
and any properties colliding with reserved names; `ddg_provenance_json` preserves
provenance. Neo4j does not store nested maps as native properties.

Imports merge into the selected Neo4j database, not a workspace namespace.
Use separate databases when graphs should remain isolated. Importing updates
present properties but does not delete elements, old labels, or properties absent
from a later snapshot. Review the script and import into a dedicated database.

> [!WARNING]
> Exports contain source property values and provenance. Protect JSON, SQLite,
> Cypher, and GraphML outputs as source data. Source reads remain read-only;
> `cypher-shell` performs writes only when you explicitly run the import.

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
