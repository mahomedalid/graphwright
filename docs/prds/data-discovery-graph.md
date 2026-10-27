---
title: Data Discovery Graph - Product Requirements Document
description: Requirements for iterative tabular data discovery, SME review, and graph construction.
ms.date: 2026-10-27
---
<!-- markdownlint-disable-file -->
<!-- markdown-table-prettify-ignore-start -->

## Data Discovery Graph - Product Requirements Document

Version 0.2 | Status: Discovery draft, not approved for implementation | Owner: TBD | Team: TBD | Target release: TBD

## Progress Tracker

| Area | Captured | Remaining |
|------|----------|-----------|
| Context and problem | Initial product vision | Business baseline and pilot domain |
| Users | SME and data engineer working personas | Roles and authorization boundaries |
| Scope | Domain-agnostic business graph, Markdown topology, generation-only code | Graph import and storage boundaries |
| Requirements | Initial functional requirements | Confirm proposed safeguards and numeric targets |
| Operations | Manual invocation, configurable time/cost limits, checkpoint/resume | Hosting, numeric budgets, cancellation, recovery |
| Finalization | Not started | Resolve open questions and obtain approval |

Confirmed requirements come from REF-001 and REF-003. Items explicitly labeled proposed are recommendations, not user-approved decisions. TBD items block final approval where they affect scope, safety, acceptance, or delivery.

## 1. Executive Summary

Companies have fragmented tabular data with unclear entities, joins, and business relationships. Data Discovery Graph will explore schemas, columns, metadata, and bounded row samples to propose evidence-backed mappings to a predefined graph topology. Subject matter experts (SMEs) will approve or reject discoveries with notes through a UI. Later discovery runs will use that feedback.

The product will also generate code that constructs a graph from source tables and provide a graph visualizer. Manual invocation starts autonomous, iterative work; a human need not supervise every exploration step.

### Goals

| ID | Goal | Baseline | Target and measurement | Priority |
|----|------|----------|------------------------|----------|
| G-001 | Reduce manual effort to discover useful source-to-graph relationships | TBD: measure manual pilot discovery time | TBD: agreed time reduction on the same pilot dataset | Must |
| G-002 | Preserve SME authority over accepted knowledge | No workflow exists in this workspace | Proposed: every promoted fact has a recorded SME decision | Must |
| G-003 | Turn validated mappings into a reproducible graph | No graph builder exists in this workspace | Proposed: rebuild identical logical nodes and edges from unchanged inputs and mapping versions | Must |
| G-004 | Make exploration and results understandable | No UI exists in this workspace | SME can inspect evidence, decide, and trace a graph element to its source in pilot acceptance tests | Must |

## 2. Problem Definition

### Current Situation

Relevant information is distributed across many tables. Names and declared keys alone may not reveal business meaning. SMEs have domain knowledge, but reviewing an unstructured stream of agent guesses would be difficult to audit and reuse.

### Problem Statement

Data teams need an iterative way to discover candidate entities, identifiers, relationships, and source mappings, distinguish hypotheses from accepted knowledge, and translate approved understanding into graph-building logic.

### Root Causes to Validate

* Inconsistent naming and incomplete metadata
* Implicit joins and business rules not represented by database constraints
* Domain knowledge disconnected from executable data transformations

Without this workflow, teams repeat discovery work and risk constructing misleading graphs from unverified assumptions. Quantified business impact is TBD.

## 3. Users and Personas

| ID | Working persona | Need | Primary workflow |
|----|-----------------|------|------------------|
| P-001 | SME | Validate meaning and reject misleading conclusions | Inspect candidate, evidence, and scope; approve or reject with notes |
| P-002 | Data engineer | Produce traceable graph construction logic | Configure sources and topology; inspect mappings; build graph |
| P-003 | Discovery operator | Run and monitor autonomous exploration | Start a run; inspect progress, resource use, and failures |

These are working roles, not a confirmed permission model.

## 4. Scope

### In Scope

* A user-authored Markdown graph topology in the existing `facts` folder defining business node types, edge types, allowed relationships, and constraints without a hardcoded industry domain
* SQLite for local development and Databricks CLI for the first production-data integration
* Iterative discovery using metadata and selected row evidence
* JSON persistence for candidate discoveries and human review decisions
* A UI for run management, candidate review, and graph visualization
* Manual run invocation followed by autonomous iterations using Microsoft Foundry and a Microsoft agent framework
* Generated code to construct a graph from tabular source data, executed separately by a human
* Configurable time/cost limits with checkpoint/resume
* Allowlisted raw row samples sent to Foundry under strict row limits

### Proposed Exclusions

* Agent writes to source databases
* Automatic promotion of hypotheses without SME approval
* Execution of generated graph-building code by the product, locally or in Databricks; the first release generates code only
* Additional source connectors, scheduled triggers, and multi-tenant SaaS operations in the first release

### Assumptions and Constraints

The graph topology is an input, not something the agent may silently redefine. JSON is the initial workflow artifact format, not a confirmed production concurrency solution. SQLite and Databricks must expose the same conceptual discovery operations without assuming equivalent SQL dialects or metadata capabilities.

## 5. Product Overview

### Discovery and Review Loop

1. Operator selects a data source, user-authored Markdown topology, exploration scope, and time/cost limits.
2. Agent loads the topology, accepted facts, prior decisions, and available progress.
3. Agent inventories metadata, forms hypotheses, and chooses bounded queries to investigate them.
4. Agent records candidates with evidence, limitations, and proposed source-to-graph mappings.
5. SME reviews candidates in the UI and approves or rejects them with notes.
6. Subsequent iterations incorporate accepted facts and rejection feedback.
7. Product generates inspectable graph-building code applying approved mappings to source tables.
8. A human runs the generated code outside the product.
9. The UI visualizes the resulting graph and its provenance through an output-loading contract that remains TBD.

Whether an active run consumes new decisions immediately or only at an iteration boundary is TBD. Proposed behavior: consume an immutable review snapshot at each boundary and record its version.

### Terminology

* Topology: allowed graph types, relationships, and constraints
* Candidate: unapproved hypothesis supported by identifiable evidence
* Fact: SME-approved assertion with explicit scope and provenance, not a guarantee of universal truth
* Mapping: executable specification connecting tables, columns, joins, and transformations to graph types
* Graph instance: concrete nodes and edges produced from source rows

The target is a business-entity graph, not a metadata/lineage graph. The product is domain-agnostic: users describe their own business graph in Markdown rather than adopting built-in entity types. Metadata and lineage support evidence and provenance; they are not the primary graph product.

The Markdown-to-validated-topology contract remains TBD. Proposed: interpret the Markdown into a structured representation, validate endpoint types and constraints, and expose ambiguities for confirmation rather than silently inventing definitions.

### Technology Direction

Microsoft Foundry is requested for model/agent capabilities. Microsoft Agent Framework is the preferred candidate for code-based orchestration, subject to API, hosting, and long-running-workflow validation during technical planning. No SDK versions, hosting services, UI framework, or graph database are selected by this PRD.

## 6. Functional Requirements

| ID | Capability | Requirement and acceptance criteria | Goal / persona | Priority |
|----|------------|-------------------------------------|----------------|----------|
| FR-001 | Topology input | Accept user-authored Markdown in `facts` defining business node types, edge types, endpoints, and constraints without hardcoded domain entities; invalid or ambiguous input produces actionable errors before discovery starts. Proposed: record topology version on every run. | G-003 / P-002 | Must |
| FR-002 | Source adapters | Discover schemas, tables, columns, available metadata, and bounded samples through SQLite or Databricks CLI; report unsupported metadata explicitly rather than inventing it. | G-001 / P-002 | Must |
| FR-003 | Iterative discovery | Agent chooses subsequent inspection steps based on evidence, topology, and accepted knowledge; a run record shows multiple evidence-driven steps without additional operator prompts. | G-001 / P-003 | Must |
| FR-004 | Candidate records | Persist JSON candidates with stable ID, hypothesis, candidate kind, source references, evidence/query references, scope, limitations, topology version, run ID, and proposed mapping where applicable. A reviewer can locate the supporting evidence. | G-002 / P-001 | Must |
| FR-005 | SME review | UI supports approval and rejection with notes and observations; persisted decisions remain available after restart. Proposed: record reviewer, timestamp, candidate version, and decision history. | G-002 / P-001 | Must |
| FR-006 | Feedback reuse | Approved facts and rejection notes inform later exploration; an acceptance test demonstrates that a rejected interpretation is not resubmitted unchanged without new evidence. | G-001, G-002 / P-001 | Must |
| FR-007 | Run management | Manual invocation starts autonomous work with configurable time/cost limits and checkpoint/resume. A run stops at its configured limits and can resume from persisted progress. Proposed: cancellation and additional query/token budgets. Exact defaults and in-flight work handling are TBD. | G-001 / P-003 | Must |
| FR-008 | Mapping specification | Proposed: approved mappings describe entity keys, source columns, joins, properties, null handling, edge direction, and deduplication rules; missing mandatory fields block graph generation with explicit errors. | G-003 / P-002 | Must |
| FR-009 | Code generation | Generate inspectable graph-building code from source tables and approved mappings; the artifact identifies input versions and has a testable build entry point. The product does not execute this code; a human runs it separately. | G-003 / P-002 | Must |
| FR-010 | Graph construction | Proposed: build typed nodes and edges with stable identity and provenance; identical input snapshots and mappings produce equal logical graphs with no duplicate logical elements. Target storage is TBD. | G-003 / P-002 | Must |
| FR-011 | Graph visualization | UI loads the output of the externally run builder and displays nodes, edges, types, properties, and source provenance. The output-loading contract is TBD. Proposed: search, type filters, and bounded neighborhood expansion rather than loading an entire production graph. | G-004 / P-001, P-002 | Must |
| FR-012 | Evidence limitations | Proposed: distinguish sampled observations from full-data checks, show join coverage and unmatched/null-key counts for mapping checks, and never describe model confidence as proof of truth. | G-002, G-003 / P-001 | Must |
| FR-013 | Knowledge changes | Proposed: retain versioned facts and review history; detect topology/source-schema changes and flag affected mappings for revalidation instead of silently reusing incompatible facts. | G-002, G-003 / P-002 | Should |

## 7. Non-Functional Requirements

All targets below are proposed unless otherwise stated. Performance and capacity thresholds remain TBD and are not release-ready acceptance criteria.

| ID | Category | Requirement / validation | Goal / persona | Priority |
|----|----------|--------------------------|----------------|----------|
| NFR-001 | Source protection | Read-only credentials and connector enforcement prevent data mutation; tests attempt writes and verify rejection. | G-002 / P-002 | Must |
| NFR-002 | Privacy | Allowlisted raw row samples may reach Foundry under strict row limits; non-allowlisted columns and oversized samples must be blocked before model dispatch. Test both cases. Numeric limits, evidence retention, and any additional redaction policies are TBD. | G-002 / P-001 | Must |
| NFR-003 | Reliability | Persist decisions and checkpoints atomically; crash tests leave valid prior or new records, never partially promoted facts. Concurrency policy is TBD. | G-002 / P-001 | Must |
| NFR-004 | Control | Enforce configured query timeout, sample limit, and run budgets; cancellation prevents subsequent tool dispatch. Numeric limits and treatment of in-flight queries are TBD. | G-001 / P-003 | Must |
| NFR-005 | Observability | Correlate run, iteration, query, candidate, review, mapping, and build IDs; expose failures and consumption without logging secrets or unrestricted row contents. | G-004 / P-003 | Must |
| NFR-006 | Performance | Set measurable inventory latency, review response time, graph render latency, and supported table/node counts after pilot scope is known. | G-001, G-004 / P-001 | Must |
| NFR-007 | Access control | Production reviewers and operators authenticate and receive explicit permissions; negative authorization tests cover review, run-control, and artifact-access endpoints. Identity provider and role mapping are TBD. | G-002 / P-001 | Must |
| NFR-008 | Accessibility | Proposed: review and run-control flows meet WCAG 2.2 AA, with a keyboard-accessible textual alternative to graph-only information. | G-004 / P-001 | Should |

## 8. Data and Analytics

### Proposed Artifact Responsibilities

| Location | Responsibility | Rule |
|----------|----------------|------|
| `facts` | Topology and approved assertions/mappings | Keep authored topology distinct from promoted facts; preserve versions |
| `candidates` | Proposed discoveries and review history | Rejected candidates remain traceable and available as feedback |
| `metadata` | Source inventories, profiles, and schema fingerprints | Record source and observation time; apply privacy and retention policy |
| `sandbox` | Generated code and isolated validation artifacts | Do not treat generated artifacts as trusted executable code |
| Additional run/graph storage | Checkpoints, events, and graph output | Exact location and format TBD |

Folder presence does not imply these schemas are already implemented.

### Metrics

Track discovery time to the first accepted mapping, review duration, candidate acceptance/rejection by kind, repeated rejected hypotheses, graph validation failures, query volume, model usage/cost, and run termination reasons. Acceptance rate alone is not a quality metric; a labeled pilot fixture must also test false relationships and missed expected mappings. Baselines, targets, owners, and evaluation windows are TBD.

## 9. Dependencies

| Dependency | Needed for | Open decision |
|------------|------------|---------------|
| Pilot topology and representative data | Meaningful discovery tests | User-authored Markdown, fixture, allowed relationships, expected mappings; the fixture must not hardcode the product to a domain |
| Foundry project and model deployment | Agent reasoning | Existing versus new project; permitted data boundary |
| Databricks workspace and SQL warehouse | Production-data inspection | CLI profile, Unity Catalog scope, read permissions, query limits |
| Graph representation | Builder and visualizer | Export format, in-memory graph, or persistent graph store |
| SME availability | Ground truth and promotion | Reviewer ownership and review cadence |

## 10. Risks and Mitigations

| ID | Risk | Proposed mitigation | Owner |
|----|------|---------------------|-------|
| R-001 | Similar values imply a false relationship | Test competing hypotheses, cardinality, coverage, and domain meaning; require SME review | TBD |
| R-002 | Autonomous exploration runs indefinitely or incurs excessive cost | Explicit budgets, progress detection, checkpoints, and cancellation | TBD |
| R-003 | Samples hide nulls, outliers, or bad joins | Label evidence scope; validate mappings on broader data before publishing a graph | TBD |
| R-004 | Generated code performs unsafe operations when a human runs it | Inspectable artifacts, validation guidance, read-only source access, and no product-initiated execution | TBD |
| R-005 | JSON updates lose concurrent decisions | Atomic persistence, version checks, and a confirmed single-writer or locking policy | TBD |
| R-006 | Data or metadata contains prompt injection | Treat all source content as untrusted evidence, never authority to alter tools or policy | TBD |
| R-007 | Approved knowledge becomes stale | Version source schemas and topology; revalidate affected mappings | TBD |

## 11. Privacy, Security, and Compliance

Allowlisted raw row samples may be sent to Foundry under strict row limits. Numeric limits, column allowlists, data classification, model processing boundaries, retention, deletion, and applicable regulations must be configured before production-data access. No credentials or raw sensitive records belong in the PRD or its session state. Discovery cannot expand its own permissions.

Generated code execution is outside the product's first-release boundary and is performed separately by a human. Approving a relationship must not trigger code execution.

## 12. Operational Considerations

Manual invocation, configurable time/cost limits, and checkpoint/resume are confirmed; hosting and uninterrupted run-duration requirements are not. Proposed run states are queued, running, stopping, completed, cancelled, and failed. Resume should link to a checkpoint rather than mask an interrupted run as successful.

Configurable time/cost limits bound autonomous exploration; checkpoint/resume supports longer investigations across runs. An additional proposed stalled-run condition is no new evidence or candidates after a configured number of iterations. Numeric limits, deployment, rollback, monitoring, support ownership, capacity, and recovery targets are TBD.

## 13. Rollout and Launch Plan

| Stage | Exit gate | Date / owner |
|-------|-----------|--------------|
| Requirements approval | Resolve topology interpretation, pilot fixture, numeric run/sample limits, graph output contract, and delivery ownership | TBD |
| SQLite vertical slice | Discover known mappings, review decisions, reuse rejection notes, generate code, then have a human run it and load its output into the visualizer | TBD |
| Databricks pilot | Repeat workflow against approved production-like data with enforced read-only access and query limits | TBD |
| Production readiness | Validate identity, persistence, recovery, measured capacity, monitoring, and operating ownership | TBD |

## 14. Open Questions

| ID | Decision | Status |
|----|----------|--------|
| Q-001 | Domain-agnostic business entities and relationships, defined by the user in Markdown | Resolved |
| Q-002 | What representative Markdown topology and SQLite fixture establish ground truth without making the product domain-specific? | Open |
| Q-003 | Configurable time/cost limits with checkpoint/resume; numeric defaults remain TBD | Policy resolved; defaults open |
| Q-004 | Generate code only; a human runs it separately | Resolved |
| Q-005 | Allowlisted raw row samples may reach Foundry under strict row limits; numeric defaults remain TBD | Policy resolved; defaults open |
| Q-006 | Which Foundry project/model and Databricks warehouse are available? | Deferred to technical setup |
| Q-007 | What graph storage, UI stack, hosting, and authentication are required? | Deferred until core scope is confirmed |
| Q-008 | Who owns delivery, what is the launch window, and what measurable pilot targets apply? | Open |
| Q-009 | Can SMEs amend/defer candidates, revise facts, or propose topology changes in the first release? | Open |
| Q-010 | Must active runs consume live reviews, and can multiple runs/reviewers operate concurrently? | Open |
| Q-011 | What Markdown structure and confirmation flow turn the topology into a validated contract? | Open |
| Q-012 | What output format and loading workflow connect externally executed graph code to the UI? | Open |

## 15. Changelog

| Version | Date | Summary |
|---------|------|---------|
| 0.1 | 2026-10-27 | Capture initial vision, proposed guardrails, requirements, and unresolved decisions |
| 0.2 | 2026-10-27 | Confirm domain-agnostic Markdown business topology, bounded resumable runs, generation-only code, and allowlisted raw samples |

## 16. References and Provenance

| ID | Source | Use |
|----|--------|-----|
| REF-001 | User's initial scenario, also present in [init.txt](../../init.txt) | Authoritative initial vision |
| REF-002 | Workspace inspection on 2026-10-27 | Existing `facts`, `candidates`, `metadata`, and `sandbox` folders contain no files at inspection time |
| REF-003 | User's first refinement answers on 2026-10-27 | Domain-agnostic business graph described in Markdown; configurable time/cost limits and checkpoint/resume; generate code only; allowlisted raw samples with strict row limits |

No external SDK or hosting capability has been validated for this draft. A Foundry prerequisite check reported missing Azure Developer CLI (`azd`); this does not block requirements work, and no deployment was attempted.

<!-- markdown-table-prettify-ignore-end -->
