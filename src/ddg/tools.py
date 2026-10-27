"""Discovery tools shared by the Foundry agent and the offline planner (FR-002..FR-006, FR-012).

Every tool:
* checks budgets and cancellation before touching the source (NFR-004),
* returns only privacy-filtered data (NFR-002),
* records an evidence entry with a stable ID that candidates must cite (FR-004),
* logs a correlated event without raw row contents (NFR-005).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .adapters.base import ReadOnlyViolation, SourceAdapter, UnknownObject, value_shape
from .config import Settings
from .control import RunControl, StopRun
from .mappings import validate_edge_mapping, validate_node_mapping
from .privacy import PrivacyGuard
from .store import ProposalRejected, Store, new_id, utcnow
from .topology import Topology
from .transforms import TRANSFORM_NAMES, canonical_key

CANDIDATE_KINDS = ("node_mapping", "edge_mapping", "observation")


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
    ):
        self.settings = settings
        self.store = store
        self.adapter = adapter
        self.topology = topology
        self.control = control
        self.run_id = run_id
        self.privacy = PrivacyGuard(settings.sampling)
        self.iteration = 0
        self.evidence_index: dict[str, dict] = dict(evidence_index or {})
        self.proposed: list[dict] = []
        self.proposal_errors: list[str] = []
        self.notes: list[str] = []
        self.inventory: dict[str, dict] = {}

    # ----- plumbing -----------------------------------------------------
    def _call(self, tool: str, args: dict, fn) -> dict:
        self.control.before_tool()
        call_id = new_id("call")
        before = self.adapter.query_count
        try:
            result = fn()
            status = "ok"
        except (UnknownObject, ToolError, ReadOnlyViolation, ValueError, TimeoutError) as exc:
            result = {"error": str(exc)}
            status = "error"
        except StopRun:
            raise
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
            info = self.adapter.describe_table(table)
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

    def record_note(self, note: str) -> dict:
        def run():
            self.notes.append(note[:1000])
            return {"recorded": True}

        return self._call("record_note", {"note": note[:200]}, run)

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
    ) -> dict:
        args = {"kind": kind, "hypothesis": hypothesis[:200], "evidence_ids": evidence_ids, "mapping": mapping}

        def run():
            if kind not in CANDIDATE_KINDS:
                raise ToolError(f"kind must be one of {CANDIDATE_KINDS}")
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
                    tables.add(self.adapter.resolve_table(mapping["source_table"]))
            elif kind == "edge_mapping":
                errors = validate_edge_mapping(mapping or {}, self.topology, self.adapter)
                if not errors:
                    edge = self.topology.edge_types[mapping["edge_type"]]
                    mapping.setdefault("from_node_type", edge.from_type)
                    mapping.setdefault("to_node_type", edge.to_type)
                    tables.add(self.adapter.resolve_table(mapping["source_table"]))
            if errors:
                raise ToolError("Invalid mapping: " + "; ".join(errors))
            for ev in evidence:
                for key in ("table", "left_table", "right_table"):
                    if ev["args"].get(key):
                        tables.add(ev["args"][key])
            lims = list(limitations or [])
            if any(ev["scope"] == "sample" for ev in evidence):
                lims.append("Some supporting evidence is from bounded samples, not full-data checks.")
            lims.append("Model or heuristic confidence is not proof of business meaning; SME validation required.")
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
                "confidence": confidence,
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
            return {"candidate_id": stored["id"], "status": "pending SME review"}

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
