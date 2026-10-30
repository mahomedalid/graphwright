---
title: Databricks Graph Notebook Export - Product Requirements Document
description: Export SQLite-discovered mappings into manually executed Databricks notebooks, GraphFrames, and two current-state Delta graph tables.
ms.date: 2026-10-30
---

## 1. Status and Decision Summary

Version 0.2. Status: implementation authorized; Databricks acceptance pending.

The first implementation provides portable notebook generation, manifest/override
bindings, distributed projections and Python-worker identity parity, validation
gates, guarded sequential publication, and UI/CLI/API integration. Publication
requires an externally enforced exclusive writer lease; the notebook records
the lease reference but does not implement a distributed lock. The transactional
multi-table alternative is not implemented. Successful scratch snapshots are
retained for explicit operator cleanup under the deployment retention policy.

Local generated-package and publication-failure tests do not establish AC-15.
Runtime/GraphFrames compatibility, source permissions, protected scratch storage,
full-source behavior, and publication recovery still require user-run validation
on the selected Databricks compute. The optional local Spark suite requires
PySpark 3.5.8 and Java 17.

This feature is feasible. Export mapping rules, identity contracts, source-table
bindings, and review provenance learned from SQLite samples. Execute those rules
against the original Databricks sources using Spark. Do not copy the sampled
graph and present it as the graph of the full database.

The user confirmed these product decisions:

* Produce PySpark vertex and edge DataFrames and a GraphFrames graph object.
* Publish two current-state Delta tables, nodes and edges, replaced on each
  explicitly confirmed build. Do not retain all builds as rows in those tables.
* Generate a small numbered notebook set for configuration/preflight,
  build/validation, publication, and exploration.
* The user imports, reviews, executes, and validates the notebooks in Databricks.
  Graphwright generates artifacts only.

All other design choices below are proposed requirements for review. In
particular, the target runtime, scratch storage, publication transaction mode,
and data-quality tolerances must be confirmed before delivery acceptance.

## 2. Problem and Goals

SQLite exploration operates on independently sampled exports of larger
Databricks tables. It can reveal schemas, candidate keys, normalizations,
relationship hypotheses, and SME-approved mappings, but it does not establish
full-source uniqueness, completeness, cardinality, or business meaning.

Today, approved mappings feed a standalone graph builder. Its Databricks mode
reads rows through a SQL connector into a Python process. That is not a
distributed Spark graph build, does not create GraphFrames, and does not
publish two Delta graph tables.

The new export must:

1. Reuse reviewed mappings without rediscovering the graph in Databricks.
2. Resolve local sample tables to configurable original source tables.
3. Build the graph using distributed Spark DataFrame operations.
4. Preserve semantic IDs and endpoint contracts across local and Spark builds.
5. Expose measurable full-data diagnostics before publication.
6. Deliver runnable, inspectable notebooks without repository imports or hidden
   dependencies on unavailable notebook helpers.
7. Leave execution, source access, and publication under the user's control.

Success means the same supported rules produce equivalent logical graphs on
identical inputs, and the user can evaluate those rules on full Databricks data.
It does not mean that sample approvals become production certifications.

## 3. Users and Workflow

The primary user is the data engineer who performs local discovery and later
executes the exported notebooks. An SME reviews semantic identity and relationship
meaning. A Databricks administrator supplies compatible compute, GraphFrames
dependencies, read access, and narrowly scoped output permissions.

The intended workflow is:

1. Review and approve mappings in Graphwright.
2. Select included facts, excluded facts, discovery source, and export target.
3. Review the proposed local-to-Databricks table bindings.
4. Export the notebook package locally without a Databricks connection.
5. Import the package into Databricks and set source and target variables.
6. Run preflight, then build `vertices_df`, `edges_df`, and `g`.
7. Inspect diagnostics, graph queries, exclusions, and business semantics.
8. Explicitly confirm publication of the validated build to both output tables.
9. Reload the published pair and validate its build identity and graph integrity.

Full-data validation results stay in the notebook/package workflow. They do not
silently rewrite local approvals, namespaces, or confidence tiers.

## 4. Existing Evidence and Integration Points

| Existing surface | Reuse or limitation |
| --- | --- |
| [Mapping validation and BuildSpec](../../src/ddg/mappings.py) | Reuse semantic namespaces, ordered parts, fact selection, endpoint resolution, and actionable per-fact errors |
| [Code generation](../../src/ddg/codegen.py) | Add a separate export target; preserve existing standalone JSON, SQLite, GraphML, and Cypher behavior |
| [Key transforms](../../src/ddg/transforms.py) | Reference behavior for Spark identity parity; differences must fail tests rather than silently change IDs |
| [Approved-fact UI](../../src/ddg/ui/static/app.js) | Reuse fact exclusions and review errors; add notebook-export configuration and download |
| [Rogers topology](../../workspaces/rogers/context/topology.md) | Defines identity boundaries, provisional service keys, and unresolved Person semantics |
| [Notebook reference](../../workspaces/rogers/context/notebook-reference.md) | Useful derivation hypotheses, not executable approved mapping rules |
| [Base product PRD](data-discovery-graph.md) | Preserves generation-only execution boundaries and SME authority |

Read-only inspection of the Rogers SQLite export found `_sample_manifest` with
`source_table`, `sqlite_table`, `requested_limit`, `rows_loaded`, `column_count`,
`statement_id`, `extracted_at_utc`, and `sample_method`.
There are 44 distinct manifest mappings at the time of this draft.

For example:

```text
edl_prod_vw_rawstd__ela_crm_table_address
    -> edl_prod.vw_rawstd__ela_crm.table_address
```

This is the authoritative available name binding, not proof that the original
table is still accessible or unchanged. The manifest count is not a requirement
to export every table.

## 5. Scope

### Required First Release

* Export selected approved node and edge mappings to a portable notebook package.
* Read the manifest locally and create editable Databricks table variables.
* Preserve original discovery-source and approval provenance while targeting a
  different execution environment.
* Compile supported mappings to distributed Spark projections and unions.
* Produce explicit vertex and edge DataFrames, including isolated nodes.
* Construct `g = GraphFrame(vertices_df, edges_df)`.
* Validate full-data keys, namespace contracts, properties, and endpoint coverage.
* Explicitly replace two current-state Delta output tables after validation.
* Provide graph exploration and post-publication verification notebooks.
* Expose export through the UI, CLI, and workspace-scoped API.

### Not Included

* Automatic notebook upload, job execution, cluster provisioning, or publication.
* Rediscovery, automatic promotion of pending candidates, or automatic fact repair.
* Automatic translation of narrative Markdown or arbitrary notebook SQL into
  filters, joins, aggregations, or identity-resolution logic.
* CDC, scheduling, streaming, historical graph tables, or cross-source Person
  resolution.
* Cosmos DB or Neo4j deployment. The two tables remain portable graph inputs.
* Guaranteed consistency across changing non-versioned source views.

The first release compiles the existing single-source-table mapping contract:
key columns, transforms, mapped properties, null policy, and edge deduplication.
An unsupported selected mapping blocks export with a per-fact explanation.
Explicit exclusions allow a smaller export and remain visible in its manifest.

Multi-table derivations, active-status filters, effective-date selection, and
grouped Service instances need a separately reviewed derivation contract or an
already available, explicitly bound source view. The exporter must not invent
such views or execute prose from the Rogers reference notebooks.

## 6. Export Inputs and Source Bindings

### FR-01: Freeze the Export Contract

The export captures topology content/version, selected fact IDs and revisions,
candidate references, identity contracts, included/excluded types, source
fingerprints, export version, and a deterministic rules fingerprint.

Pending and rejected candidates are not executable rules. Missing namespaces,
incompatible semantic contracts, stale approvals, unsupported transforms, and
unresolved endpoints block a publishable export. A draft error report may be
downloaded, but must not resemble a successfully executable package.

Discovery source `local` remains the provenance of the approvals. Databricks
table bindings do not move those approvals to a new source or change semantic IDs.

### FR-02: Resolve Physical Source Names Explicitly

Resolve each referenced local table through `_sample_manifest`. Never reverse
underscore flattening: that transformation is ambiguous. Permit explicit,
reviewed overrides for exports without a manifest or for renamed source objects.

Every included source table must have exactly one binding. Missing, conflicting,
or duplicate manifest entries produce actionable export errors. Local helper
tables such as ground-truth match indexes are not production sources unless the
user supplies an intentional binding.

The generated configuration has readable aliases and a central binding map:

```python
SOURCE_TABLES = {
    "v21_billing_account": "edl_prod.vw_rawstd__ela_v21.billing_account",
    "v21_subscriber": "edl_prod.vw_rawstd__ela_v21.subscriber",
}
OUTPUT_NODES_TABLE = "<catalog>.<schema>.<nodes_table>"
OUTPUT_EDGES_TABLE = "<catalog>.<schema>.<edges_table>"
ENABLE_PUBLICATION = False
```

These are illustrative bindings, not a declaration that both mappings are
approved. Source reads use the configured variables rather than hardcoded names
throughout generated cells.

Validate multipart identifiers and quote their components correctly. Do not
interpolate unchecked strings into SQL. Display the complete effective source
and target list before any write, and reject equal targets or any target that
resolves to an input table. Column overrides, if needed, are explicit and recorded;
schema drift must never trigger guessed column substitutions.

## 7. Notebook Package and Execution Contract

### FR-03: Package Layout

Proposed downloadable ZIP contents:

| Artifact | Responsibility |
| --- | --- |
| `00_configure_preflight.py` | Central variables, package contract, helpers, runtime and source checks |
| `10_build_validate.py` | Distributed projections, graph assembly, validation report, in-memory GraphFrame |
| `20_publish.py` | Guarded materialization and replacement of both current-state tables |
| `30_explore.py` | In-memory or published graph loading, integrity checks, bounded exploration |
| `export_manifest.json` | Rules fingerprint, fact versions, bindings, exclusions, validation policy |
| `README.md` | Import/run order, prerequisites, publication risks, recovery instructions |

Use Databricks source-notebook format with `# Databricks notebook source` and
`# COMMAND ----------` cell separators. Embed the executable rules in the
configuration notebook so file-loader behavior is not a hidden prerequisite.
The JSON manifest is a reviewable duplicate whose fingerprint must agree.
No sample rows, customer identifiers, credentials, or saved cell outputs are
included in the generated package.

### FR-04: Do Not Rely on Shared Notebook Memory

Import all four notebooks into one directory. Document `%run`-based invocation:
the build notebook loads configuration; the publication notebook executes the
build/validation notebook in its own session before confirming a write.
The exploration notebook explicitly chooses a fresh in-memory build or the
published tables.

Running notebook 10 and then separately opening notebook 20 must not assume that
variables, cached DataFrames, or validation results survived. Rebuilding in
notebook 20 can be expensive and must be stated before execution.
Operators may inspect that rebuilt result, then execute a separate confirmation
cell. A changed configuration, rules fingerprint, source snapshot, or build ID
invalidates earlier confirmation.

### FR-05: Runtime and Dependency Preflight

Check the chosen Databricks Runtime, Spark/Python versions, compute access mode,
and installed GraphFrames Python/JVM support. Pin and test a compatible
combination during implementation; do not assume that a pip-only installation
or a successful Python import proves GraphFrames can execute.

Preflight runs a tiny synthetic GraphFrame operation, checks selected source
columns/types, and distinguishes missing permissions from missing objects.
Missing dependencies fail with installation guidance. No packages are installed
automatically, and no existing cluster configuration is changed.

Output permissions are verified separately before publication. Source access is
read-only; writing requires privileges only on designated targets and any
explicitly configured scratch location.

## 8. Graph DataFrame and Table Contracts

### FR-06: Two Unified Graph Tables

Use one nodes table and one edges table for all included types, not one table per
entity. Both have explicit schemas, including when a selected type produces zero
rows. DataFrames are:

```python
vertices_df   # nodes, with a unique string id
edges_df      # relationships, with string src and dst referencing vertices
g = GraphFrame(vertices_df, edges_df)
```

Proposed persisted schemas:

| Nodes column | Spark type | Meaning |
| --- | --- | --- |
| `id` | string, non-null | Semantic graph ID |
| `type` | string, non-null | Topology node type |
| `identity_namespace` | string, non-null | Reviewed semantic identifier scope |
| `identity_key_parts` | array of string | Ordered semantic part names |
| `key_values` | array of string | Ordered normalized identifier values |
| `properties_json` | string | Deterministically serialized, typed JSON object |
| `provenance_json` | string | Source bindings, source record references where approved, and contributing fact revisions |
| `build_id` | string, non-null | Publication/build identity, not part of node ID |
| `rules_fingerprint` | string, non-null | Exact exported rule set |
| `built_at` | timestamp, non-null | Build observation time, not business-effective time |

| Edges column | Spark type | Meaning |
| --- | --- | --- |
| `id` | string, non-null | Stable relationship identity |
| `src` / `dst` | string, non-null | Complete semantic vertex IDs |
| `type` | string, non-null | Topology relationship type |
| `from_type` / `to_type` | string, non-null | Expected endpoint types |
| `properties_json` / `provenance_json` | string | Same serialization and provenance rules as nodes |
| `build_id` / `rules_fingerprint` / `built_at` | string / string / timestamp | Same build metadata as nodes |

JSON envelopes avoid a union of hundreds of sparsely populated columns and
unapproved string coercion of every property. Define serialization for dates,
timestamps/timezones, decimals, binary, arrays, nested values, nulls, and map-key
ordering. Reject unsupported source types explicitly. Generate type-specific
property parsing examples for exploration.

GraphFrames consumes `id`, `src`, and `dst` directly; additional columns are
attributes. Do not infer vertices solely from edges, because that loses isolated
nodes. Published `src`/`dst` correspond to `source`/`target` in the local graph
format and `from_id`/`to_id` in Rogers reference prose; document that translation.

### FR-07: Preserve Semantic Identity Exactly

Use `semantic/v1`: entity type, explicit namespace, and ordered normalized
values, with UTF-8 percent-escaping per component. Preserve leading zeros unless
the approved transform explicitly removes them. Do not include table names,
connection aliases, build IDs, or physical column names automatically.

Spark and Python implementations must be byte-for-byte equivalent for every
supported transform and key. Native Spark expressions are preferred, but they
cannot silently substitute different Unicode whitespace, case conversion,
numeric rounding, regex, or URL-escaping semantics. In particular, Spark
`url_encode` behavior must not be assumed equivalent to Python `quote`.

Golden tests cover colons, percent signs, slashes, spaces, Unicode, leading zeros,
large numeric identifiers, nulls, composite keys, and normalization chains.
Lossy legacy numeric normalization is surfaced for review, not silently corrected
only in the Spark target. An unsupported or unproven parity case blocks that
mapping until resolved.

### FR-08: Deterministic Merging and Edge Grain

The same semantic node ID may occur in multiple physical tables if contracts
agree. Merge provenance and agreeing properties. Conflicting non-null values
produce a diagnostic and block publication unless a separately approved,
deterministic precedence rule exists. Never use unordered `first`,
`dropDuplicates`, or row order to choose a business value.

For `dedupe=true`, deduplicate by relationship type and semantic endpoints only
when that represents the approved association grain. Diagnose property conflicts.
If distinct roles/services between the same endpoints require distinct edge
identities, an approved relationship key or discriminator must exist first.

The current local `dedupe=false` implementation appends a row index. That is not
a stable distributed identity. First-release notebook export must reject such
mappings with an explanation; it must not fabricate a stable key or silently
drop parallel relationships. Adding a reviewed relationship-key contract is a
separate prerequisite for supporting them.

## 9. Full-Data Validation and Scale

### FR-09: Validation Report

Validation operates on the target sources, not on copied SQLite evidence. It
reports, per mapping/type and overall:

* Source row counts and selected source versions or observation bounds.
* Null key rows and rows excluded by an approved null policy.
* Duplicate raw keys, duplicate normalized IDs, and property conflicts.
* Namespace-contract and endpoint-part compatibility.
* Edge counts, duplicate edge identities, unmatched endpoints, and match ratios.
* Directed endpoint types and cardinality observations where meaningful.
* Isolated vertex counts, missing types, and unexpected empty outputs.
* Rules fingerprint, approved fact versions, bindings, and all explicit exclusions.

A sample-level unique key may be duplicated in the full source; overlapping
samples may have understated real joins. Report both without treating either
as an automatic change to business meaning or local confidence.

After deliberate deduplication, publishable vertices must have zero null or
duplicate IDs, edges must have zero null endpoint IDs and zero unresolved
endpoints, and both must have zero unresolved property/identity conflicts.
Dangling candidate edges may be excluded only through an explicit reviewed policy
with counts and a configured tolerance; they are not silently dropped.
Empty selected types and an empty graph block publication by default.
Threshold overrides require explicit values and are captured in the report.

Invalid rows remain diagnosable in restricted in-memory DataFrames. Display only
aggregate diagnostics by default; raw examples require an explicit operator
choice within the Databricks security boundary. Do not send full-source rows,
diagnostics, or identifiers to Foundry.

### FR-10: Distributed Execution and Input Consistency

Source reads use `spark.table` with projected columns. Transformations, unions,
joins, deduplication, and validation are distributed. Do not collect the graph
to the driver, use pandas for graph construction, or loop over source rows.
Small configuration/manifest objects and bounded aggregate reports are allowed.

Pin Delta source versions where permitted, recording the versions used.
For views and non-versioned inputs, disclose that a common transactional source
snapshot is not guaranteed. Such inputs require an explicit operator
acknowledgment or a separately approved snapshot strategy.

Persist/materialize reused DataFrames deliberately; clean them up in `finally`.
Before publication, validate the exact materialized output that will be written,
not a lazily recomputed query over changing sources. Any scratch writes are
publication-stage actions requiring confirmation and an explicit protected
location; read-only build/validation does not create persistent staging data.

Runtime targets depend on source sizes and compute. Establish a representative
scale benchmark before release; do not claim throughput from the SQLite sample.

## 10. Guarded Current-State Publication

### FR-11: Explicit Replacement Authorization

Publication is disabled by default and isolated from exploration. Require a
separate confirmation cell naming both fully qualified targets, build ID,
rules fingerprint, and replacement intent. Reject source/target overlap,
concurrent publication, stale validation, and mismatched package fingerprints.

Replacement is authoritative for the selected export scope. Warn that excluded
types and rows absent from the newly built graph will disappear from these
current-state tables. It is not a partial upsert and does not preserve previous
graph history. Users must not point this exporter at shared tables owned by
other loaders unless replacement ownership is explicitly agreed.

Overwrite only the two graph targets. Do not mutate sources, approved local facts,
or unrelated schemas, and do not infer schema migrations automatically.

### FR-12: Pair Consistency and Failure Recovery

Two independent Delta DataFrame writes must not be described as a single atomic
publication. The baseline design uses serialized writers and guarded sequential
replacement unless compatible multi-table transactions are verified.

Proposed baseline:

1. Capture the effective configuration, target versions/existence, and prior
   build metadata. Check that the existing pair is consistent before replacing it.
2. Materialize both new outputs at explicitly configured restricted scratch Delta
   paths after confirmation. Validate those exact snapshots before target writes.
   These paths are temporary artifacts, not additional permanent graph tables.
3. Verify that neither target changed since preflight and that the deployment's
   single-writer coordination is in effect.
4. Replace nodes, then edges from the staged outputs, with identical build
   metadata. Capture resulting Delta versions.
5. Verify the pair using version-pinned reads, row counts, uniqueness, endpoint
   integrity, build IDs, and rules fingerprints.
6. Mark success only after both targets pass. Print the two committed versions
   and a compact validation receipt.

A failure after the first commit leaves publication incomplete. Report the exact
state and do not return a success-shaped result. Provide operator-run recovery
cells to restore captured previous versions or retry the same staged build.
Never automatically drop a newly created target or overwrite another writer's
changes. Preserve required scratch artifacts for recovery on failure; on success
clean up only the exact build-owned paths according to an approved retention
policy. Delta time-travel availability depends on retention and is not guaranteed
history storage.

Reader checks compare table versions/metadata and per-row build IDs, including
empty tables, before accepting a published graph. Pin both reads to accepted
versions and fail closed on a mixed pair. Cooperating readers may see an
unavailable graph during replacement; arbitrary SQL readers that bypass the
checks can see an inconsistent pair. This limitation must be prominent.

Databricks now documents multi-table transactions for compatible managed tables
with catalog commits and supported compute. Where available, evaluate a
transactional DML replacement path to avoid the mixed-pair window. Do not assume
ordinary `saveAsTable` overwrites participate in it. Capability checks and an
integration test are required before advertising atomic pair publication.

If neither writer coordination nor the operational mixed-pair/recovery contract
can be met, publication remains disabled. The in-memory graph can still be built
and validated. Strict always-available atomic publication may require the
transactional path or a future active-build pointer design, which is not the
chosen two-current-table baseline.

## 11. Product Integration and Delivery Sequence

### FR-13: User-Facing Export

Add a notebook-export target alongside existing builder generation:

* UI: selected approved facts, exclusions, source bindings, target placeholders,
  validation-policy preview, actionable errors, and package download.
* CLI: proposed `ddg codegen --target databricks-notebooks --source local`, with
  manifest/override configuration and existing fact-exclusion semantics.
* API: workspace-scoped export request returning the package manifest and
  authorized artifact download, not a notebook-execution request.

Names and payloads are proposed until implementation design review. All surfaces
must use the same validated export contract. Generate and download successfully
without Databricks credentials; defer target-runtime/schema/permission checks to
the preflight notebook. Restrict artifact paths to the selected workspace.

### Proposed Implementation Phases

1. Add immutable notebook-export contracts, manifest name bindings, capability
   checks, and per-fact export errors without changing existing builder behavior.
2. Implement and parity-test Spark identity/projection compilation and the unified
   graph envelopes, including unsupported-edge diagnostics.
3. Render the numbered notebook package, dependency preflight, GraphFrames build,
   validation, publication guards, and recovery instructions.
4. Wire UI/CLI/API export and download using existing fact-selection semantics.
5. Run local generated-artifact tests and a synthetic Spark integration suite.
6. Deliver the package for user-run Databricks acceptance; record runtime,
   diagnostics, and failures without automatically ingesting customer data.

## 12. Acceptance Criteria

| ID | Measurable acceptance condition |
| --- | --- |
| AC-01 | A SQLite-only workspace exports the four notebooks and manifest without Databricks credentials, model calls, source writes, or notebook execution |
| AC-02 | Every selected local table resolves through a manifest entry or explicit override; ambiguous/missing bindings block export without guessed names |
| AC-03 | The package contains zero sample rows, secrets, saved outputs, or imports from unavailable repository/custom helper modules |
| AC-04 | On a synthetic identical dataset, Python and Spark produce exactly equal semantic IDs, logical endpoints, supported property values, and graph counts |
| AC-05 | Shared compatible namespaces merge deterministically with complete provenance; incompatible contracts and conflicting property values prevent publication |
| AC-06 | Tests cover reserved characters, Unicode, leading zeros, nulls, composites, every supported transform chain, and column/table renaming without unintended ID changes |
| AC-07 | `vertices_df.id` is unique and non-null; every `edges_df.src/dst` resolves to the correct vertex type; isolated nodes survive; `GraphFrame` executes a smoke query |
| AC-08 | Every selected unsupported derivation, missing namespace, stale approval, or `dedupe=false` mapping has a specific error; explicit exclusions are frozen in the package |
| AC-09 | Full-data duplicate keys, unmatched endpoints, empty outputs, and schema drift are detected before writes; failed gates prevent target publication |
| AC-10 | Importing or running configuration, validation, or exploration cannot write targets; publication requires confirmation bound to both target names and the exact build |
| AC-11 | Two unchanged-input builds have identical logical IDs/properties/endpoints apart from build-time metadata; replacements do not accumulate historical build rows |
| AC-12 | Injecting failure between node and edge commits produces an incomplete-publication error; guarded readers reject the pair and recovery instructions identify previous versions |
| AC-13 | Fresh-session publication does not depend on another notebook's variables and validates the exact staged output subsequently written |
| AC-14 | UI, CLI, and API export the same selected rules, exclusions, bindings, and fingerprints; existing standalone export regression tests continue to pass |
| AC-15 | The user imports and runs the package on the agreed Databricks runtime, inspects full-data diagnostics, explicitly publishes to test targets, and verifies the published GraphFrame |

Documentation-only approval of this PRD does not satisfy runtime acceptance.
The user owns Databricks execution; engineering owns reproducible export,
clear safeguards, and locally testable compiler behavior.

## 13. Risks and Decisions Before Implementation Sign-Off

| Risk or decision | Required resolution |
| --- | --- |
| Sample approvals do not hold on full data | Keep full-data gates and business review separate from sample confidence |
| Legacy Rogers facts lack semantic namespaces | Review corrected mappings; export cannot invent approved namespaces |
| Source tables are views or change during execution | Agree version pinning, snapshot strategy, or explicit bounded-consistency acknowledgment |
| Runtime/GraphFrames compatibility | Select and verify runtime, compute mode, Python package, and JVM integration |
| Two-table replacement is not automatically atomic | Select transactional capability or explicitly accept guarded sequential replacement and its availability window |
| Scratch access and retention | Select a restricted approved location and recovery retention; no hidden permanent staging tables |
| Property heterogeneity and conflicts | Approve JSON envelope typing and deterministic conflict handling |
| Service/addon logic exceeds current mapping model | Supply reviewed source views or separately scope a derivation contract; do not compile reference prose |
| Parallel relationships require stable identity | Exclude unsupported non-deduplicating mappings until an approved discriminator contract exists |
| Source and graph size exceed pilot assumptions | Agree scale benchmark and compute budget during implementation |

Exact destination catalog/schema/table names are intentionally unset. Names found
in the historical notebook reference are not authorization to replace those
tables. No source owner approval, compute compatibility, or target access has
been verified by this PRD.

## 14. References

* [GraphFrames: Creating GraphFrames](https://graphframes.io/04-user-guide/01-creating-graphframes.html):
  vertex `id`, edge `src`/`dst`, arbitrary additional attributes.
* [Databricks notebook import/export](https://docs.databricks.com/aws/en/notebooks/notebook-export-import):
  supported source-notebook format and cell markers.
* [Databricks transactions](https://docs.databricks.com/aws/en/transactions/):
  multi-table transaction prerequisites and supported compute. Documentation
  reviewed on 2026-10-30; availability must be verified for the user's cloud,
  runtime, and workspace rather than inferred from documentation alone.
