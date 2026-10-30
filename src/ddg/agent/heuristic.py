"""Deterministic offline planner.

Used for local development, tests, and demos when no Foundry model is configured.
It relies only on generic naming, uniqueness, and value-overlap signals plus the
user's topology; it contains no domain-specific rules.
"""

from __future__ import annotations

import re

from ..tools import DiscoveryTools
from ..mappings import available_node_mappings
from ..topology import EdgeType, NodeType
from .base import IterationContext, IterationResult

NEUTRAL_TOKENS = {"tbl", "tab", "table", "vw", "view", "dim", "fact", "ref", "master", "data", "info", "hdr", "header", "v", "lkp", "lookup"}
SUSPECT_TOKENS = {"bak", "backup", "tmp", "temp", "old", "archive", "copy", "test", "staging", "stg", "log", "audit", "export", "dump", "scratch"}
KEY_TOKENS = {"id", "key", "code", "cd", "no", "num", "nbr", "number", "sku", "ref", "uuid", "guid"}
TRANSFORM_OPTIONS = [[], ["trim", "upper"], ["int"], ["digits_only", "int"], ["trim", "upper", "strip_leading_zeros"]]
MAX_DESCRIBE_PER_ITERATION = 40
MIN_COVERAGE = 0.5
MIN_PROPERTY_SIM = 0.5


def tokens(name: str) -> list[str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    parts = re.split(r"[^A-Za-z0-9]+|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", spaced)
    return [p.lower() for p in parts if p and not p.isdigit() and not re.fullmatch(r"v\d*", p.lower())]


def _is_subsequence(short: str, long: str) -> bool:
    it = iter(long)
    return all(ch in it for ch in short)


def token_sim(concept: str, source: str) -> float:
    if concept == source:
        return 1.0
    if concept.rstrip("s") == source.rstrip("s"):
        return 0.95
    if len(source) >= 3 and concept.startswith(source):
        return 0.8
    if len(concept) >= 3 and source.startswith(concept):
        return 0.85
    if len(source) >= 2 and source[0] == concept[0] and _is_subsequence(source, concept):
        return 0.6
    return 0.0


def name_sim(concept_tokens: list[str], source_name: str) -> float:
    src = [t for t in tokens(source_name) if t not in NEUTRAL_TOKENS]
    return max((token_sim(c, s) for c in concept_tokens for s in src), default=0.0)


def phrase_sim(phrase: str, column: str) -> float:
    """Average best-match similarity of each phrase token against the column tokens."""
    p_tokens = tokens(phrase)
    c_tokens = tokens(column)
    if not p_tokens or not c_tokens:
        return 0.0
    return sum(max(token_sim(p, c) for c in c_tokens) for p in p_tokens) / len(p_tokens)


def concept_tokens(item: NodeType | EdgeType) -> list[str]:
    out = tokens(item.name)
    for synonym in item.synonyms:
        out.extend(tokens(synonym))
    return out


def canonical_transforms(column_type: str) -> list[str]:
    return ["int"] if "INT" in (column_type or "").upper() else ["trim", "upper"]


class HeuristicPlanner:
    name = "heuristic"

    def run_iteration(self, ctx: IterationContext, tools: DiscoveryTools) -> IterationResult:
        memory = dict(ctx.memory)
        memory.setdefault("exhausted", [])
        node_maps = available_node_mappings(ctx.snapshot, ctx.source)
        if tools.discovery_scope == "edges" and not any(
            edge.from_type in node_maps and edge.to_type in node_maps for edge in ctx.topology.edge_types.values()
        ):
            return IterationResult(
                "Edges-only discovery has no relationships with existing endpoint mappings. "
                "Run full discovery or review node mappings first.", memory, done=True,
            )

        listing = tools.list_tables()
        all_tables = [t["table"] for t in listing.get("tables", [])]
        missing = [t for t in all_tables if t not in tools.inventory]
        if missing:
            batch = missing[:MAX_DESCRIBE_PER_ITERATION]
            for table in batch:
                tools.describe_table(table)
            return IterationResult(f"Inventoried {len(batch)} of {len(all_tables)} tables.", memory)

        covered = _covered_elements(ctx)
        if tools.validate_topology:
            memory.setdefault("validation_visited", [])
        for node in ctx.topology.node_types.values():
            if tools.discovery_scope == "edges":
                continue
            element = f"node:{node.name}"
            if tools.validate_topology:
                if element in memory["validation_visited"]:
                    continue
            elif node.name in covered["nodes"] or element in memory["exhausted"]:
                continue
            summary = self._discover_node(node, tools, ctx)
            if tools.validate_topology:
                memory["validation_visited"].append(element)
            if summary is None:
                memory["exhausted"].append(element)
                summary = f"No acceptable table found for node type {node.name}."
                if tools.validate_topology and not any(f["element"] == element for f in tools.topology_findings):
                    self._unresolved(element, tools, "No suitable source table or key was identified.")
            return IterationResult(summary, memory)

        for edge in ctx.topology.edge_types.values():
            element = f"edge:{edge.name}"
            if tools.validate_topology:
                if element in memory["validation_visited"]:
                    continue
            elif edge.name in covered["edges"] or element in memory["exhausted"]:
                continue
            if edge.from_type not in node_maps or edge.to_type not in node_maps:
                if tools.validate_topology:
                    self._unresolved(element, tools, "Endpoint mappings are unavailable; relationship not tested.")
                    memory["validation_visited"].append(element)
                continue
            summary = self._discover_edge(edge, node_maps, tools, ctx)
            if tools.validate_topology:
                memory["validation_visited"].append(element)
                if not any(f["element"] == element for f in tools.topology_findings):
                    self._unresolved(element, tools, "No sufficiently supported join was identified.")
            if summary is None:
                memory["exhausted"].append(element)
                summary = f"No acceptable source found for edge type {edge.name}."
            return IterationResult(summary, memory)

        summary = "No remaining topology elements to investigate."
        if tools.discovery_scope == "edges" and memory.get("blocked_edges"):
            summary = "Relationship discovery finished for available endpoints. Missing node mappings block: " + ", ".join(
                f"{edge} ({', '.join(nodes)})" for edge, nodes in memory["blocked_edges"].items()
            )
        return IterationResult(summary, memory, done=True)

    def _unresolved(self, element: str, tools: DiscoveryTools, finding: str) -> None:
        evidence_ids = [info["evidence_id"] for info in tools.inventory.values() if info.get("evidence_id")]
        if evidence_ids:
            tools.record_topology_finding(
                element, "coverage", "inconclusive", finding,
                "Review source scope, synonyms, and endpoint mappings before changing or removing this topology element.",
                evidence_ids, ["Offline discovery uses naming heuristics, not semantic proof."],
            )

    def _validate_node(self, node: NodeType, table: str, tools: DiscoveryTools, *, tentative: bool = False) -> None:
        info = tools.inventory[table]
        element = f"node:{node.name}"
        caveats = ["Source table and column associations are inferred from names; confirm business meaning with an SME."]
        if tentative:
            caveats.append("No acceptable node mapping was established; this table is only a naming-based hypothesis.")
        properties = _match_properties(node.properties, info["columns"], exclude=set())
        missing = [prop for prop in node.properties if prop not in properties]
        tools.record_topology_finding(
            element, "properties", "inconclusive" if missing else "supported",
            f"Compared properties against '{table}'. "
            + (f"No column matched: {', '.join(missing)}." if missing else "All declared properties have plausible columns."),
            "Clarify property names or synonyms, find another source, or consider removing unavailable properties."
            if missing else "",
            [info["evidence_id"]], caveats,
        )
        if not node.key:
            tools.record_topology_finding(
                element, "key", "inconclusive", "The topology does not specify a key.",
                "Document an entity key after checking uniqueness, nulls, and business identity.",
                [info["evidence_id"]], caveats,
            )
            return
        score, column = max(
            ((phrase_sim(node.key, col["name"]), col["name"]) for col in info["columns"]),
            default=(0.0, ""),
        )
        if score < MIN_PROPERTY_SIM:
            tools.record_topology_finding(
                element, "key", "inconclusive", f"No column confidently matched declared key '{node.key}' in '{table}'.",
                "Clarify the key's source columns or specify a composite key; do not replace it from naming alone.",
                [info["evidence_id"]], caveats,
            )
            return
        check = tools.check_uniqueness(table, [column])
        if check.get("error"):
            self._unresolved(element, tools, f"Key check failed: {check['error']}")
            return
        empty = check["distinct_keys"] == 0
        status = "inconclusive" if tentative or empty or check["scope"] != "full" else "supported" if check["unique"] else "mismatch"
        tools.record_topology_finding(
            element, "key", status,
            f"Possible declared key '{node.key}' maps to '{table}.{column}': "
            f"{check['duplicate_keys']} duplicate keys, {check['rows_with_null_key']} null rows, "
            f"{check['distinct_keys']} distinct non-null keys.",
            "" if status == "supported" else
            "Review the entity grain and key: test a composite or alternative key, document normalization, "
            "or address source-quality issues before revising the topology.",
            [info["evidence_id"], check["evidence_id"]], caveats,
        )

    # ----- nodes -------------------------------------------------------------
    def _discover_node(self, node: NodeType, tools: DiscoveryTools, ctx: IterationContext) -> str | None:
        if tools.validate_topology:
            existing = available_node_mappings(ctx.snapshot, ctx.source).get(node.name)
            if existing and existing["source_table"] in tools.inventory:
                self._validate_node(node, existing["source_table"], tools)
                return f"Validated assumptions for {node.name} using its existing mapping to {existing['source_table']}."
        concepts = concept_tokens(node)
        scored = []
        for table, info in tools.inventory.items():
            score = name_sim(concepts, table)
            if score <= 0:
                continue
            if set(tokens(table)) & SUSPECT_TOKENS:
                score *= 0.5
            scored.append((score, info.get("row_count", 0), table))
        scored.sort(key=lambda s: (-s[0], -s[1], s[2]))

        for table_score, _, table in scored[:4]:
            info = tools.inventory[table]
            key_cols = sorted(
                info["columns"],
                key=lambda c: -(2.0 * c.get("primary_key", False) + _key_score(c["name"], concepts)),
            )
            for column in key_cols[:4]:
                if not column.get("primary_key") and _key_score(column["name"], concepts) <= 0:
                    continue
                chain = canonical_transforms(column.get("type", ""))
                uniq = tools.check_uniqueness(table, [column["name"]], [chain])
                if uniq.get("error") or not uniq.get("unique"):
                    continue
                properties = _match_properties(node.properties, info["columns"], exclude={column["name"]})
                mapping = {
                    "node_type": node.name,
                    "source_table": table,
                    "key_columns": [column["name"]],
                    "key_transforms": [chain],
                    "identity_namespace": f"{ctx.source}.{table}.{column['name']}",
                    "identity_key_parts": [column["name"]],
                    "properties": properties,
                    "null_key_policy": "skip",
                }
                confidence = round(min(0.95, 0.6 * table_score + 0.3 + 0.05 * bool(column.get("primary_key"))), 2)
                unmatched = [p for p in node.properties if p not in properties]
                limitations = [f"No column matched topology properties: {', '.join(unmatched)}"] if unmatched else []
                limitations.append(
                    "Identity namespace is conservatively source-record scoped by the offline planner; "
                    "review a semantic namespace before sharing identity across tables or systems."
                )
                if set(tokens(table)) & SUSPECT_TOKENS:
                    limitations.append("Table name suggests a backup, temporary, or log table.")
                result = tools.propose_candidate(
                    kind="node_mapping",
                    hypothesis=f"Rows of '{table}' represent {node.name} entities identified by '{column['name']}'.",
                    rationale=(
                        f"Table name similarity {table_score:.2f} to {node.name}; '{column['name']}' is unique and non-null "
                        f"across {uniq.get('distinct_keys')} rows after transforms {chain}."
                    ),
                    evidence_ids=[info["evidence_id"], uniq["evidence_id"]],
                    mapping=mapping,
                    limitations=limitations,
                    confidence=confidence,
                )
                if "candidate_id" in result:
                    if tools.validate_topology:
                        self._validate_node(node, table, tools)
                    return f"Proposed {node.name} -> {table}.{column['name']} ({result['candidate_id']})."
        if tools.validate_topology and scored:
            self._validate_node(node, scored[0][2], tools, tentative=True)
        return None

    # ----- edges -------------------------------------------------------------
    def _discover_edge(self, edge: EdgeType, node_maps: dict, tools: DiscoveryTools, ctx: IterationContext) -> str | None:
        a, b = node_maps[edge.from_type], node_maps[edge.to_type]
        if len(a["key_columns"]) != 1 or len(b["key_columns"]) != 1:
            return None
        a_concepts = concept_tokens(ctx.topology.node_types[edge.from_type]) + tokens(a["key_columns"][0])
        b_concepts = concept_tokens(ctx.topology.node_types[edge.to_type]) + tokens(b["key_columns"][0])
        options = []

        def best_reference(table: str, target: dict, concepts: list[str], exclude: set[str]):
            best = None
            for col in tools.inventory[table]["columns"]:
                if col["name"] in exclude or name_sim(concepts, col["name"]) < 0.6:
                    continue
                for chain in TRANSFORM_OPTIONS:
                    res = tools.check_join(
                        table, [col["name"]], target["source_table"], target["key_columns"],
                        [chain], target["key_transforms"],
                    )
                    if res.get("error"):
                        continue
                    if best is None or res["row_coverage"] > best[2]["row_coverage"]:
                        best = (col["name"], chain, res)
                    if res["row_coverage"] >= 0.9:
                        break
            return best

        def accept(table: str, ref, target: dict) -> tuple[list[str], list[str]] | None:
            """(extra evidence ids, transform chain) that make `ref` acceptable, or None.

            Exploratory mode accepts low overlap when some transform makes the key formats compatible.
            """
            if not ref:
                return None
            if ref[2]["row_coverage"] >= MIN_COVERAGE:
                return [], ref[1]
            if tools.mode != "exploratory":
                return None
            for chain in [ref[1], *[c for c in TRANSFORM_OPTIONS if c != ref[1]]]:
                compat = tools.check_compatibility(
                    table, ref[0], target["source_table"], target["key_columns"][0], chain, target["key_transforms"][0]
                )
                if compat.get("verdict") == "compatible":
                    return [compat["evidence_id"]], chain
            return None

        ta, tb = a["source_table"], b["source_table"]
        ref = best_reference(ta, b, b_concepts, set(a["key_columns"]))
        ok = accept(ta, ref, b)
        if ok is not None:
            # Each source row is one `from` node; repeated `to` keys mean many-to-one.
            card = ("many" if ref[2]["left_max_rows_per_key"] > 1 else "one") + "-to-one"
            options.append((ref[2]["row_coverage"], ta, a["key_columns"][0], a["key_transforms"][0], ref[0], ok[1], [ref[2]], card, ok[0]))
        ref = best_reference(tb, a, a_concepts, set(b["key_columns"]))
        ok = accept(tb, ref, a)
        if ok is not None:
            card = "one-to-" + ("many" if ref[2]["left_max_rows_per_key"] > 1 else "one")
            options.append((ref[2]["row_coverage"], tb, ref[0], ok[1], b["key_columns"][0], b["key_transforms"][0], [ref[2]], card, ok[0]))
        if not options:
            edge_concepts = concept_tokens(edge)
            bridges = sorted(
                (t for t in tools.inventory if t not in (ta, tb) and not set(tokens(t)) & SUSPECT_TOKENS),
                key=lambda t: -max(name_sim(edge_concepts + a_concepts + b_concepts, t), 0),
            )
            for table in bridges[:6]:
                left = best_reference(table, a, a_concepts, set())
                left_ok = accept(table, left, a)
                if left_ok is None:
                    continue
                right = best_reference(table, b, b_concepts, {left[0]})
                right_ok = accept(table, right, b)
                if right_ok is None:
                    continue
                coverage = min(left[2]["row_coverage"], right[2]["row_coverage"])
                card = ("many" if right[2]["left_max_rows_per_key"] > 1 else "one") + "-to-" + (
                    "many" if left[2]["left_max_rows_per_key"] > 1 else "one"
                )
                options.append((coverage, table, left[0], left_ok[1], right[0], right_ok[1], [left[2], right[2]], card,
                                left_ok[0] + right_ok[0]))
                break

        options.sort(key=lambda o: -o[0])
        for coverage, table, from_col, from_chain, to_col, to_chain, joins, observed, extra_evidence in options:
            if tools.validate_topology:
                mismatch = bool(edge.cardinality and not _cardinality_compatible(observed, edge.cardinality))
                incomplete = bool(extra_evidence) or any(j["scope"] != "full" for j in joins)
                status = "inconclusive" if incomplete or tools.independent_samples else "mismatch" if mismatch else "supported"
                tools.record_topology_finding(
                    f"edge:{edge.name}", "cardinality", status,
                    f"Possible relationship via '{table}' has observed {edge.from_type}->{edge.to_type} "
                    f"cardinality '{observed}', declared '{edge.cardinality or 'unspecified'}', "
                    f"minimum row coverage {coverage:.0%}.",
                    f"Consider changing Cardinality to {observed} after confirming endpoint mappings and entity grain."
                    if status == "mismatch" else
                    "Validate joins on representative data and confirm endpoint mappings before revising cardinality."
                    if status == "inconclusive" else "",
                    [tools.inventory[table]["evidence_id"], *[j["evidence_id"] for j in joins], *extra_evidence],
                    ["Observed one-sided uniqueness does not justify narrowing a declared many-side.",
                     "Join coverage and cardinality do not prove the relationship's business meaning."],
                )
            properties = _match_properties(edge.properties, tools.inventory[table]["columns"], exclude={from_col, to_col})
            mapping = {
                "edge_type": edge.name,
                "from_node_type": edge.from_type,
                "to_node_type": edge.to_type,
                "from_mapping_id": a["mapping_id"],
                "to_mapping_id": b["mapping_id"],
                "source_table": table,
                "from_key_columns": [from_col],
                "from_key_transforms": [from_chain],
                "to_key_columns": [to_col],
                "to_key_transforms": [to_chain],
                "properties": properties,
                "null_key_policy": "skip",
                "dedupe": True,
            }
            limitations = []
            for j in joins:
                if j["unmatched_left_keys"]:
                    limitations.append(
                        f"{j['unmatched_left_keys']} distinct values in {j['left']['table']}.{j['left']['columns'][0]} "
                        f"have no match in {j['right']['table']} and would produce dangling edges."
                    )
            if edge.cardinality and not _cardinality_compatible(observed, edge.cardinality):
                limitations.append(
                    f"Observed cardinality {observed} conflicts with topology cardinality '{edge.cardinality}'."
                )
            for node_type in (edge.from_type, edge.to_type):
                if node_maps[node_type].get("_status") == "pending":
                    limitations.append(f"Depends on the {node_type} node mapping, which is still pending SME review.")
            result = tools.propose_candidate(
                kind="edge_mapping",
                hypothesis=(
                    f"{edge.from_type} -[{edge.name}]-> {edge.to_type} is derived from '{table}' using "
                    f"'{from_col}' for the {edge.from_type} key and '{to_col}' for the {edge.to_type} key."
                ),
                rationale=(
                    f"Value-overlap checks show row coverage {coverage:.0%}; observed {edge.from_type}->{edge.to_type} "
                    f"cardinality '{observed}'; topology cardinality '{edge.cardinality or 'unspecified'}'."
                ),
                evidence_ids=[tools.inventory[table]["evidence_id"], *[j["evidence_id"] for j in joins], *extra_evidence],
                mapping=mapping,
                limitations=limitations + (
                    ["Low value overlap; supported mainly by column naming and compatible key formats."] if extra_evidence else []
                ),
                confidence=round(min(0.9, 0.5 + 0.4 * coverage), 2),
            )
            if "candidate_id" in result:
                return f"Proposed {edge.name} via {table} ({result['candidate_id']})."
        return None


def _cardinality_compatible(observed: str, declared: str) -> bool:
    """Observed 'one' sides are compatible with declared 'many' (samples may not show repeats)."""
    obs_from, obs_to = observed.split("-to-")
    dec_from, dec_to = declared.split("-to-")
    return (obs_from == dec_from or dec_from == "many") and (obs_to == dec_to or dec_to == "many")


def _key_score(column: str, concepts: list[str]) -> float:
    col_tokens = tokens(column)
    score = 1.0 if set(col_tokens) & KEY_TOKENS else 0.0
    score += 0.5 * max((token_sim(c, t) for c in concepts for t in col_tokens), default=0.0)
    return score


def _match_properties(properties: list[str], columns: list[dict], exclude: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    used = set(exclude)
    for prop in properties:
        best = max(
            ((phrase_sim(prop, c["name"]), c["name"]) for c in columns if c["name"] not in used),
            default=(0.0, None),
        )
        if best[1] and best[0] >= MIN_PROPERTY_SIM:
            result[prop] = best[1]
            used.add(best[1])
    return result


def _covered_elements(ctx: IterationContext) -> dict[str, set[str]]:
    nodes, edges = set(), set()
    for cand in ctx.snapshot["pending"] + ctx.snapshot["approved"]:
        if cand.get("scope", {}).get("source") != ctx.source or not cand.get("mapping"):
            continue
        if cand["kind"] == "node_mapping":
            nodes.add(cand["mapping"]["node_type"])
        elif cand["kind"] == "edge_mapping":
            edges.add(cand["mapping"]["edge_type"])
    return {"nodes": nodes, "edges": edges}
