"""Discovery tools shared by the Foundry agent and the offline planner (FR-002..FR-006, FR-012).

Every tool:
* checks budgets and cancellation before touching the source (NFR-004),
* returns only privacy-filtered data (NFR-002),
* records an evidence entry with a stable ID that candidates must cite (FR-004),
* logs a correlated event without raw row contents (NFR-005).
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

from .adapters.base import ReadOnlyViolation, SourceAdapter, UnknownObject, value_shape
from .confidence import TIER_SCORE, TIERS, assess
from .config import DISCOVERY_SCOPES, MODES, Settings
from .control import RunControl, StopRun
from .mappings import (
    identity_contract_errors, node_mapping_catalog, resolve_edge_endpoints,
    validate_edge_mapping, validate_node_mapping,
)
from .privacy import PrivacyGuard
from .store import ProposalRejected, Store, new_id, utcnow
from .topology import Topology
from .transforms import TRANSFORM_NAMES, canonical_key

CANDIDATE_KINDS = ("node_mapping", "edge_mapping", "annotation", "observation")
COMPAT_SAMPLE = 500


class ToolError(Exception):
    pass


class DiscoveryTools:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        adapter: SourceAdapter,
        topology: Topology,
        control: RunControl,
        run_id: str,
        evidence_index: dict[str, dict] | None = None,
        mode: str = "conservative",
        independent_samples: bool = False,
        validate_topology: bool = False,
        discovery_scope: str = "all",
    ):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        if discovery_scope not in DISCOVERY_SCOPES:
            raise ValueError(f"discovery_scope must be one of {', '.join(DISCOVERY_SCOPES)}")
        self.settings = settings
        self.store = store
        self.adapter = adapter
        self.topology = topology
        self.control = control
        self.run_id = run_id
        self.mode = mode
        self.independent_samples = independent_samples
        self.validate_topology = validate_topology
        self.discovery_scope = discovery_scope
        self.topology_findings: list[dict] = []
        self.privacy = PrivacyGuard(settings.sampling)
        self.iteration = 0
        self.evidence_index: dict[str, dict] = dict(evidence_index or {})
        self.proposed: list[dict] = []
        self.proposal_errors: list[str] = []
        self.notes: list[str] = []
        self.inventory: dict[str, dict] = {}
        # Resumed runs already described these tables; reuse them instead of re-querying.
        for ev in self.evidence_index.values():
            if ev.get("tool") == "describe_table" and ev.get("result", {}).get("table"):
                self.inventory[ev["result"]["table"]] = ev["result"]

    # ----- plumbing -----------------------------------------------------
    def _call(self, tool: str, args: dict, fn) -> dict:
        self.control.before_tool()
        call_id = new_id("call")
        before = self.adapter.query_count
        started = time.monotonic()
        self.store.append_event(self.run_id, {
            "type": "tool_started", "iteration": self.iteration, "call_id": call_id, "tool": tool,
        })
        try:
            result = fn()
            status = "ok"
        except (UnknownObject, ToolError, ReadOnlyViolation, ValueError, TimeoutError) as exc:
            result = {"error": str(exc)}
            status = "error"
        except StopRun as exc:
            self.store.append_event(self.run_id, {
                "type": "tool_failed", "iteration": self.iteration, "call_id": call_id, "tool": tool,
                "reason": exc.reason, "duration_seconds": round(time.monotonic() - started, 2),
            })
            raise
        except Exception as exc:
            self.store.append_event(self.run_id, {
                "type": "tool_failed", "iteration": self.iteration, "call_id": call_id, "tool": tool,
                "error_type": type(exc).__name__, "duration_seconds": round(time.monotonic() - started, 2),
            })
            raise
        finally:
            self.control.usage.queries += self.adapter.query_count - before
        self.store.append_event(
            self.run_id,
            {
                "type": "tool",
                "iteration": self.iteration,
                "call_id": call_id,
                "tool": tool,
                "args": args,
                "status": status,
                "duration_seconds": round(time.monotonic() - started, 2),
                "evidence_id": result.get("evidence_id") if isinstance(result, dict) else None,
                "summary": _summarize(result),
            },
        )
        return result

    def _evidence(self, tool: str, args: dict, result: dict, scope: str) -> dict:
        evidence_id = new_id("ev")
        result["evidence_id"] = evidence_id
        result["scope"] = scope
        evidence = {
            "id": evidence_id,
            "run_id": self.run_id,
            "iteration": self.iteration,
            "tool": tool,
            "args": args,
            "scope": scope,
            "observed_at": utcnow(),
            "source": self.adapter.name,
            "result": result,
        }
        self.store.save_evidence(self.run_id, evidence)
        self.evidence_index[evidence["id"]] = evidence
        return result

    # ----- tools ----------------------------------------------------------
    def list_tables(self) -> dict:
        def run():
            tables = self.adapter.tables()
            return {"source": self.adapter.name, "dialect": self.adapter.dialect, "tables": [
                {"table": name, "type": info.get("type"), "comment": info.get("comment")} for name, info in tables.items()
            ]}

        return self._call("list_tables", {}, run)

    def describe_table(self, table: str) -> dict:
        def run():
            resolved = self.adapter.resolve_table(table)
            cached = self.inventory.get(resolved)
            if cached and cached.get("evidence_id") in self.evidence_index:
                # Same run, same table: reuse the earlier result and evidence id instead of querying again.
                return {**cached, "cached": True}
            info = self.adapter.describe_table(resolved)
            self.inventory[info["table"]] = info
            return self._evidence("describe_table", {"table": info["table"]}, info, "full")

        return self._call("describe_table", {"table": table}, run)

    def profile_column(self, table: str, column: str) -> dict:
        def run():
            stats = self.adapter.column_stats(table, column)
            t, c = stats["table"], stats["column"]
            if self.privacy.policy.is_allowed(t, c):
                stats["top_values"] = [
                    {"value": self.privacy.value_or_redacted(t, c, v), "count": n}
                    for v, n in self.adapter.top_values(t, c, self.settings.sampling.max_top_values)
                ]
            else:
                stats["top_values"] = "blocked: column not on sampling allowlist"
            return self._evidence("profile_column", {"table": t, "column": c}, stats, "full")

        return self._call("profile_column", {"table": table, "column": column}, run)

    def sample_rows(self, table: str, columns: list[str] | None = None, limit: int = 5) -> dict:
        def run():
            resolved = self.adapter.resolve_table(table)
            wanted = columns or [c["name"] for c in self.adapter.columns(resolved)]
            wanted = [self.adapter.resolve_column(resolved, c) for c in wanted]
            allowed, blocked = self.privacy.split_columns(resolved, wanted)
            capped = self.privacy.clamp_rows(limit)
            rows: list[tuple] = []
            if allowed and capped:
                _, rows = self.adapter.sample_rows(resolved, allowed, capped)
            filtered = self.privacy.filter_sample(resolved, allowed, rows, limit)
            filtered.blocked_columns = blocked
            result = {"table": resolved, **filtered.to_dict()}
            if int(limit) > capped:
                result["note"] = (result.get("note", "") + f" Requested {limit} rows; capped at {capped}.").strip()
            return self._evidence("sample_rows", {"table": resolved, "columns": wanted, "limit": int(limit)}, result, "sample")

        return self._call("sample_rows", {"table": table, "columns": columns, "limit": limit}, run)

    def check_uniqueness(self, table: str, columns: list[str], transforms: list[list[str]] | None = None) -> dict:
        def run():
            resolved = self.adapter.resolve_table(table)
            cols = [self.adapter.resolve_column(resolved, c) for c in columns]
            chains = _chains(transforms, cols)
            counts, complete = self.adapter.key_counts(resolved, cols, self.settings.limits.max_join_keys)
            canon: Counter[str] = Counter()
            null_rows = 0
            raw_dupes = 0
            for row in counts:
                values, n = row[:-1], int(row[-1])
                if n > 1:
                    raw_dupes += 1
                key = canonical_key(values, chains)
                if key is None:
                    null_rows += n
                else:
                    canon[key] += n
            dupes = {k: n for k, n in canon.items() if n > 1}
            result = {
                "table": resolved,
                "columns": cols,
                "transforms": chains,
                "distinct_keys": len(canon),
                "rows_with_null_key": null_rows,
                "duplicate_keys": len(dupes),
                "duplicate_keys_before_transforms": raw_dupes,
                "max_rows_per_key": max(canon.values(), default=0),
                "unique": not dupes and null_rows == 0,
            }
            if not complete:
                result["limitation"] = f"Only the first {self.settings.limits.max_join_keys} distinct keys were checked"
            return self._evidence(
                "check_uniqueness", {"table": resolved, "columns": cols, "transforms": chains}, result, "full" if complete else "sample"
            )

        return self._call("check_uniqueness", {"table": table, "columns": columns, "transforms": transforms}, run)

    def check_join(
        self,
        left_table: str,
        left_columns: list[str],
        right_table: str,
        right_columns: list[str],
        left_transforms: list[list[str]] | None = None,
        right_transforms: list[list[str]] | None = None,
    ) -> dict:
        args = {
            "left_table": left_table,
            "left_columns": left_columns,
            "left_transforms": left_transforms,
            "right_table": right_table,
            "right_columns": right_columns,
            "right_transforms": right_transforms,
        }

        def run():
            lt = self.adapter.resolve_table(left_table)
            rt = self.adapter.resolve_table(right_table)
            lcols = [self.adapter.resolve_column(lt, c) for c in left_columns]
            rcols = [self.adapter.resolve_column(rt, c) for c in right_columns]
            if len(lcols) != len(rcols):
                raise ToolError("left_columns and right_columns must have the same length")
            lchains, rchains = _chains(left_transforms, lcols), _chains(right_transforms, rcols)
            limit = self.settings.limits.max_join_keys
            lcounts, lcomplete = self.adapter.key_counts(lt, lcols, limit)
            rcounts, rcomplete = self.adapter.key_counts(rt, rcols, limit)
            left, left_null, left_raw = _canonicalize(lcounts, lchains)
            right, right_null, _ = _canonicalize(rcounts, rchains)
            matched = [k for k in left if k in right]
            left_rows = sum(left.values())
            matched_rows = sum(left[k] for k in matched)
            unmatched = [k for k in left if k not in right]
            left_multi = max(left.values(), default=0)
            right_multi = max(right.values(), default=0)
            cardinality = (
                ("one" if left_multi <= 1 else "many") + "-to-" + ("one" if right_multi <= 1 else "many")
            )
            examples_allowed = all(self.privacy.policy.is_allowed(lt, c) for c in lcols)
            unmatched_examples: Any = (
                unmatched[:5] if examples_allowed else sorted({value_shape(left_raw[k]) for k in unmatched[:50]})[:5]
            )
            result = {
                "left": {"table": lt, "columns": lcols, "transforms": lchains},
                "right": {"table": rt, "columns": rcols, "transforms": rchains},
                "left_distinct_keys": len(left),
                "right_distinct_keys": len(right),
                "matched_distinct_keys": len(matched),
                "unmatched_left_keys": len(unmatched),
                "left_null_key_rows": left_null,
                "right_null_key_rows": right_null,
                "key_coverage": round(len(matched) / len(left), 4) if left else 0.0,
                "row_coverage": round(matched_rows / left_rows, 4) if left_rows else 0.0,
                "right_keys_referenced": round(len(matched) / len(right), 4) if right else 0.0,
                "left_max_rows_per_key": left_multi,
                "right_max_rows_per_key": right_multi,
                "observed_cardinality_left_to_right": cardinality,
                "unmatched_examples" if examples_allowed else "unmatched_value_shapes": unmatched_examples,
                "interpretation_note": "Coverage measures value overlap only; it does not prove business meaning.",
            }
            if not (lcomplete and rcomplete):
                result["limitation"] = f"Key sets truncated at {limit} distinct values; results are sampled"
            return self._evidence(
                "check_join",
                {"left_table": lt, "left_columns": lcols, "left_transforms": lchains,
                 "right_table": rt, "right_columns": rcols, "right_transforms": rchains},
                result,
                "full" if lcomplete and rcomplete else "sample",
            )

        return self._call("check_join", args, run)

    def check_compatibility(
        self,
        left_table: str,
        left_column: str,
        right_table: str,
        right_column: str,
        left_transforms: list[str] | None = None,
        right_transforms: list[str] | None = None,
    ) -> dict:
        """Compare key formats without needing value overlap (works on independently sampled tables).

        Raw values are read internally and never returned: the result has types, value shapes,
        length ranges, and naming signals only.
        """
        args = {"left_table": left_table, "left_column": left_column, "right_table": right_table,
                "right_column": right_column, "left_transforms": left_transforms, "right_transforms": right_transforms}

        def run():
            from .agent.heuristic import tokens

            lt, rt = self.adapter.resolve_table(left_table), self.adapter.resolve_table(right_table)
            lc, rc = self.adapter.resolve_column(lt, left_column), self.adapter.resolve_column(rt, right_column)
            lchain = _chains([left_transforms] if left_transforms is not None else None, [lc])[0]
            rchain = _chains([right_transforms] if right_transforms is not None else None, [rc])[0]

            def side(table: str, column: str, chain: list[str]) -> tuple[dict, list[str]]:
                _, rows = self.adapter.run(
                    f"SELECT {self.adapter.quote(column)} FROM {self.adapter.qualified(table)} "
                    f"WHERE {self.adapter.quote(column)} IS NOT NULL LIMIT {COMPAT_SAMPLE}",
                    COMPAT_SAMPLE,
                )
                values = [v for v in (canonical_key([r[0]], [chain]) for r in rows) if v is not None]
                shapes = Counter(value_shape(v) for v in values)
                col_type = next((c.get("type") for c in self.adapter.columns(table) if c["name"] == column), None)
                lengths = [len(v) for v in values]
                info = {
                    "table": table, "column": column, "type": col_type, "transforms": chain,
                    "sampled_values": len(values),
                    "top_shapes": [{"shape": s, "share": round(n / len(values), 3)} for s, n in shapes.most_common(5)] if values else [],
                    "length_range": [min(lengths), max(lengths)] if lengths else None,
                }
                return info, values

            left, lvals = side(lt, lc, lchain)
            right, rvals = side(rt, rc, rchain)
            right_shapes = {value_shape(v) for v in rvals}
            shape_agreement = (sum(1 for v in lvals if value_shape(v) in right_shapes) / len(lvals)) if lvals else 0.0
            rset = set(rvals)
            sample_overlap = (sum(1 for v in lvals if v in rset) / len(lvals)) if lvals else 0.0
            ref_tokens = set(tokens(rt)) | set(tokens(rc))
            name_signal = sorted(set(tokens(lc)) & ref_tokens - {"id", "key", "no", "num", "code", "cd"})
            if not lvals or not rvals:
                verdict = "unknown"
            elif shape_agreement >= 0.8:
                verdict = "compatible"
            elif shape_agreement >= 0.3:
                verdict = "partial"
            else:
                verdict = "incompatible"
            result = {
                "left": left, "right": right, "verdict": verdict,
                "shape_agreement": round(shape_agreement, 3),
                "sample_value_overlap": round(sample_overlap, 3),
                "name_signal": name_signal,
                "note": "Format compatibility supports, but cannot prove, a relationship. Use check_join for overlap when samples are consistent.",
            }
            return self._evidence("check_compatibility", {
                "left_table": lt, "left_column": lc, "left_transforms": lchain,
                "right_table": rt, "right_column": rc, "right_transforms": rchain,
            }, result, "sample")

        return self._call("check_compatibility", args, run)

    def exploration_ledger(self, max_lines: int = 160) -> str:
        """Compact memory of everything checked in this run (and resumed runs), with citable evidence ids."""
        attempts, profiles, described = [], [], []
        for ev in sorted(self.evidence_index.values(), key=lambda e: (e.get("iteration", 0), e.get("observed_at", ""))):
            r, a, eid = ev.get("result", {}), ev.get("args", {}), ev["id"]
            tool = ev.get("tool")
            if tool == "describe_table":
                pk = [c["name"] for c in r.get("columns", []) if c.get("primary_key")]
                described.append(f"{eid} describe {r.get('table')}: {r.get('row_count')} rows, {len(r.get('columns', []))} cols"
                                 + (f", pk={pk}" if pk else ""))
            elif tool == "check_uniqueness":
                attempts.append(f"{eid} uniqueness {a.get('table')}{a.get('columns')} tf={a.get('transforms')}: unique={r.get('unique')}, "
                                f"distinct={r.get('distinct_keys')}, duplicate_keys={r.get('duplicate_keys')}, null_rows={r.get('rows_with_null_key')}")
            elif tool == "check_join":
                attempts.append(f"{eid} join {a.get('left_table')}{a.get('left_columns')} tf={a.get('left_transforms')} -> "
                                f"{a.get('right_table')}{a.get('right_columns')} tf={a.get('right_transforms')}: "
                                f"rows {r.get('row_coverage', 0):.1%}, keys {r.get('key_coverage', 0):.1%}, "
                                f"unmatched={r.get('unmatched_left_keys')}, cardinality={r.get('observed_cardinality_left_to_right')}")
            elif tool == "check_compatibility":
                attempts.append(f"{eid} compatibility {a.get('left_table')}.{a.get('left_column')} -> {a.get('right_table')}.{a.get('right_column')}: "
                                f"{r.get('verdict')} (shapes agree {r.get('shape_agreement', 0):.0%}, names share {r.get('name_signal')})")
            elif tool == "profile_column":
                shapes = [s["shape"] for s in r.get("value_shapes", [])][:3]
                profiles.append(f"{eid} profile {a.get('table')}.{a.get('column')}: {r.get('null_count')} nulls of {r.get('row_count')}, "
                                f"{r.get('distinct_count')} distinct, shapes {shapes}")
        lines = []
        if attempts:
            lines += ["Checks already done (do not repeat; cite these ids):", *attempts]
        if profiles:
            lines += ["Column profiles:", *profiles[-40:]]
        if described:
            lines += ["Tables described (describe_table again returns the cached result):", *described]
        if len(lines) > max_lines:
            lines = lines[:max_lines] + [f"... {len(lines) - max_lines} more lines omitted"]
        return "\n".join(lines) if lines else "Nothing checked yet in this run."

    def schema_digest(self, max_columns: int = 80) -> str:
        """Column names of described tables so the agent does not re-describe to recall them."""
        out = []
        for name in sorted(self.inventory, key=str.lower):
            cols = [c["name"] for c in self.inventory[name].get("columns", [])]
            more = f" (+{len(cols) - max_columns} more)" if len(cols) > max_columns else ""
            out.append(f"{name}: {', '.join(cols[:max_columns])}{more}")
        return "\n".join(out) if out else "No tables described yet."

    def get_review_feedback(self) -> dict:
        def run():
            snapshot = self.store.review_snapshot()
            source = self.adapter.name
            def mine(c):
                return c.get("scope", {}).get("source") == source
            return {
                "review_snapshot_version": snapshot["version"],
                "approved": [_brief(c) for c in snapshot["approved"] if mine(c)],
                "rejected": [
                    {**_brief(c), "sme_notes": [d.get("notes") for d in c["decisions"]],
                     "sme_observations": [d.get("observations") for d in c["decisions"]]}
                    for c in snapshot["rejected"] if mine(c)
                ],
                "pending": [_brief(c) for c in snapshot["pending"] if mine(c)],
            }

        return self._call("get_review_feedback", {}, run)

    def _validate_annotation(self, mapping: dict) -> list[str]:
        """Annotation = dictionary knowledge: {table, column?, description?, values?: {code: meaning}, rules?: [..]}."""
        errors: list[str] = []
        try:
            mapping["table"] = self.adapter.resolve_table(str(mapping.get("table", "")))
            if mapping.get("column"):
                mapping["column"] = self.adapter.resolve_column(mapping["table"], str(mapping["column"]))
        except UnknownObject as exc:
            return [str(exc)]
        description = mapping.get("description") or ""
        values = mapping.get("values") or {}
        rules = mapping.get("rules") or []
        if not isinstance(description, str) or not isinstance(values, dict) or not isinstance(rules, list):
            errors.append("description must be text, values an object of code -> meaning, rules a list of text")
        elif not (description.strip() or values or rules):
            errors.append("annotation needs a description, values, or rules")
        if values and not mapping.get("column"):
            errors.append("values (code meanings) require a column")
        unknown = set(mapping) - {"table", "column", "description", "values", "rules"}
        if unknown:
            errors.append(f"unknown annotation fields {sorted(unknown)}")
        return errors

    def get_dictionary(self, table: str | None = None) -> dict:
        """Curated dictionary knowledge (SME edits and approved annotations) for this source."""

        def run():
            from .dictionary import build_dictionary

            dictionary = build_dictionary(self.settings, self.store, self.adapter.name)
            out = []
            for t in dictionary["tables"]:
                if table and t["name"].lower() != table.lower():
                    continue
                cols = [
                    {k: c[k] for k in ("name", "description", "status", "values", "notes") if c.get(k)}
                    for c in t["columns"] if c["description"] or c.get("values") or c["notes"]
                ]
                if t["description"] or t["notes"] or cols or t["history"]:
                    out.append({
                        "table": t["name"], "description": t["description"], "status": t["status"], "notes": t["notes"],
                        "columns": cols,
                        "rejected_interpretations": [h["hypothesis"] for h in t["history"]],
                    })
            return {"source": self.adapter.name, "tables": out, "summary": dictionary["summary"]}

        return self._call("get_dictionary", {"table": table}, run)

    def record_note(self, note: str) -> dict:
        def run():
            self.notes.append(note[:1000])
            return {"recorded": True}

        return self._call("record_note", {"note": note[:200]}, run)

    def record_topology_finding(
        self, element: str, aspect: str, status: str, finding: str,
        recommendation: str, evidence_ids: list[str], limitations: list[str] | None = None,
    ) -> dict:
        """Record an evidence-backed advisory finding, never a fact or a topology edit."""
        def run():
            if not self.validate_topology:
                raise ToolError("Topology validation is not enabled for this run")
            if self.discovery_scope == "edges" and not element.startswith("edge:"):
                raise ToolError("Edges-only runs can record topology findings only for relationships")
            if status not in ("supported", "mismatch", "inconclusive"):
                raise ToolError("status must be supported, mismatch, or inconclusive")
            if not all(isinstance(value, str) and value.strip() for value in (element, aspect, finding)):
                raise ToolError("element, aspect, and finding are required text")
            if not element.startswith(("node:", "edge:", "constraint:")):
                raise ToolError("element must start with node:, edge:, or constraint:")
            if status != "supported" and not recommendation.strip():
                raise ToolError("A mismatch or inconclusive finding requires a recommendation")
            if not evidence_ids:
                raise ToolError("Topology findings must cite source evidence")
            evidence = []
            for eid in evidence_ids:
                ev = self.evidence_index.get(eid)
                if ev is None or ev.get("source") != self.adapter.name:
                    raise ToolError(f"Unknown evidence id '{eid}' for this source")
                evidence.append(ev)
            caveats = list(limitations or [])
            if any(ev.get("scope") != "full" for ev in evidence):
                caveats.append("Evidence includes bounded samples; this is not full-source proof.")
            if self.independent_samples:
                caveats.append("Tables contain independent samples; missing overlap does not disprove a relationship.")
            record = {
                "element": element, "aspect": aspect, "status": status, "finding": finding,
                "recommendation": recommendation, "evidence_ids": list(evidence_ids),
                "evidence": evidence,
                "limitations": caveats, "topology_version": self.topology.version,
                "source": self.adapter.name, "run_id": self.run_id, "iteration": self.iteration,
            }
            self.topology_findings[:] = [
                f for f in self.topology_findings if (f["element"], f["aspect"]) != (element, aspect)
            ]
            self.topology_findings.append(record)
            return {"recorded": True, **record}

        return self._call("record_topology_finding", {"element": element, "aspect": aspect}, run)

    def propose_candidate(
        self,
        kind: str,
        hypothesis: str,
        rationale: str,
        evidence_ids: list[str],
        mapping: dict | None = None,
        limitations: list[str] | None = None,
        confidence: float | None = None,
        new_evidence_note: str | None = None,
        confidence_tier: str | None = None,
    ) -> dict:
        args = {"kind": kind, "hypothesis": hypothesis[:200], "evidence_ids": evidence_ids, "mapping": mapping,
                "confidence_tier": confidence_tier}

        def run():
            if kind not in CANDIDATE_KINDS:
                raise ToolError(f"kind must be one of {CANDIDATE_KINDS}")
            if self.discovery_scope == "edges" and kind != "edge_mapping":
                raise ToolError("Edges-only discovery accepts only edge_mapping proposals; node mappings and annotations are disabled")
            if not hypothesis.strip():
                raise ToolError("hypothesis is required")
            evidence = []
            for eid in evidence_ids or []:
                ev = self.evidence_index.get(eid)
                if ev is None:
                    raise ToolError(f"Unknown evidence id '{eid}'. Cite ids returned by discovery tools.")
                evidence.append(ev)
            if kind != "observation" and not evidence:
                raise ToolError("Mapping candidates must cite at least one evidence id")
            errors: list[str] = []
            tables: set[str] = set()
            if kind == "node_mapping":
                errors = validate_node_mapping(mapping or {}, self.topology, self.adapter)
                if not errors:
                    catalog = node_mapping_catalog(self.store.review_snapshot(), self.adapter.name)
                    errors.extend(identity_contract_errors(mapping, catalog.get(mapping["node_type"], [])))
                    tables.add(self.adapter.resolve_table(mapping["source_table"]))
            elif kind == "edge_mapping":
                errors = validate_edge_mapping(mapping or {}, self.topology, self.adapter)
                if not errors:
                    edge = self.topology.edge_types[mapping["edge_type"]]
                    mapping.setdefault("from_node_type", edge.from_type)
                    mapping.setdefault("to_node_type", edge.to_type)
                    tables.add(self.adapter.resolve_table(mapping["source_table"]))
            elif kind == "annotation":
                errors = self._validate_annotation(mapping or {})
                if not errors:
                    tables.add(mapping["table"])
            if errors:
                raise ToolError("Invalid mapping: " + "; ".join(errors))
            for ev in evidence:
                for key in ("table", "left_table", "right_table"):
                    if ev["args"].get(key):
                        tables.add(ev["args"][key])
            lims = list(limitations or [])
            if kind == "edge_mapping":
                catalog = node_mapping_catalog(self.store.review_snapshot(), self.adapter.name)
                bindings, problems = resolve_edge_endpoints(mapping, catalog)
                if problems:
                    raise ToolError("Invalid endpoint identity: " + "; ".join(problems))
                for side, node in bindings.items():
                    endpoint = node["node_type"]
                    problems = validate_node_mapping(node, self.topology, self.adapter)
                    if problems:
                        raise ToolError(f"Endpoint '{endpoint}' mapping needs correction: {'; '.join(problems)}")
                    conflicts = identity_contract_errors(
                        node, [other for other in catalog.get(endpoint, []) if other is not node],
                    )
                    if conflicts:
                        raise ToolError("Invalid endpoint identity: " + "; ".join(conflicts))
                    mapping[f"{side}_mapping_id"] = node["mapping_id"]
                    if node["_status"] == "pending":
                        lims.append(f"Depends on the {endpoint} node mapping, which is still pending SME review.")
            if any(ev["scope"] == "sample" for ev in evidence):
                lims.append("Some supporting evidence is from bounded samples, not full-data checks.")
            lims.append("Model or heuristic confidence is not proof of business meaning; SME validation required.")
            declared = None
            if kind == "edge_mapping" and mapping and mapping.get("edge_type") in self.topology.edge_types:
                declared = self.topology.edge_types[mapping["edge_type"]].cardinality
            stated = (confidence_tier or "").lower() or None
            if stated is not None and stated not in TIERS:
                raise ToolError(f"confidence_tier must be one of {', '.join(TIERS)}")
            assessment = assess(kind, mapping, evidence, declared_cardinality=declared, stated_tier=stated,
                                independent_samples=self.independent_samples)
            if assessment["tier"] == "low" and self.mode != "exploratory":
                raise ToolError(
                    "Low-confidence proposal not accepted in conservative mode ("
                    + "; ".join(assessment["reasons"])
                    + "). Gather stronger evidence, or record the idea with record_note."
                )
            # Keep the legacy numeric score consistent with the tier (a model cannot claim 0.9 for low evidence).
            cap = {"low": 0.45, "medium": 0.75, "high": 0.95}[assessment["tier"]]
            numeric = min(float(confidence if confidence is not None else TIER_SCORE[assessment["tier"]]), cap)
            candidate = {
                "kind": kind,
                "hypothesis": hypothesis.strip(),
                "rationale": rationale.strip(),
                "mapping": mapping if kind != "observation" else None,
                "evidence": [
                    {"id": ev["id"], "tool": ev["tool"], "args": ev["args"], "scope": ev["scope"],
                     "observed_at": ev["observed_at"], "run_id": ev["run_id"], "result": ev["result"]}
                    for ev in evidence
                ],
                "limitations": lims,
                "confidence": round(numeric, 2),
                "confidence_tier": assessment["tier"],
                "confidence_reasons": assessment["reasons"],
                "evidence_tier": assessment["evidence_tier"],
                "discovery_mode": self.mode,
                "discovery_scope": self.discovery_scope,
                "source_refs": sorted(f"{self.adapter.name}:{t}" for t in tables),
                "scope": {"source": self.adapter.name, "tables": sorted(tables)},
                "schema_fingerprints": {t: self.adapter.schema_fingerprint(t) for t in sorted(tables)},
                "topology_version": self.topology.version,
                "run_id": self.run_id,
                "iteration": self.iteration,
                "new_evidence_note": new_evidence_note,
            }
            try:
                stored = self.store.add_candidate(candidate)
            except ProposalRejected as exc:
                self.proposal_errors.append(str(exc))
                raise ToolError(str(exc)) from exc
            self.proposed.append(stored)
            return {"candidate_id": stored["id"], "status": "pending SME review",
                    "confidence_tier": assessment["tier"], "confidence_reasons": assessment["reasons"]}

        return self._call("propose_candidate", args, run)


def _chains(transforms: list[list[str]] | None, cols: list[str]) -> list[list[str]]:
    if transforms is None:
        return [[] for _ in cols]
    if len(transforms) != len(cols):
        raise ToolError("transforms must have one list per column")
    for chain in transforms:
        bad = [t for t in chain if t not in TRANSFORM_NAMES]
        if bad:
            raise ToolError(f"Unknown transforms {bad}; allowed: {', '.join(TRANSFORM_NAMES)}")
    return [list(c) for c in transforms]


def _canonicalize(counts: list[tuple], chains: list[list[str]]) -> tuple[dict[str, int], int, dict[str, Any]]:
    canon: dict[str, int] = {}
    raw: dict[str, Any] = {}
    nulls = 0
    for row in counts:
        values, n = row[:-1], int(row[-1])
        key = canonical_key(values, chains)
        if key is None:
            nulls += n
            continue
        canon[key] = canon.get(key, 0) + n
        raw.setdefault(key, values[0] if len(values) == 1 else values)
    return canon, nulls, raw


def _brief(candidate: dict) -> dict:
    return {
        "id": candidate["id"],
        "kind": candidate["kind"],
        "hypothesis": candidate["hypothesis"],
        "mapping": candidate.get("mapping"),
        "status": candidate["status"],
    }


def _summarize(result: Any) -> dict:
    """Event summary with counts only, never row values."""
    if not isinstance(result, dict):
        return {}
    summary: dict[str, Any] = {}
    for key in (
        "error", "row_count", "distinct_count", "null_count", "unique", "returned_rows", "blocked_columns",
        "key_coverage", "row_coverage", "observed_cardinality_left_to_right", "duplicate_keys", "candidate_id", "scope",
    ):
        if key in result:
            summary[key] = result[key]
    if "tables" in result:
        summary["table_count"] = len(result["tables"])
    return summary
