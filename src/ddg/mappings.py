"""Mapping specification validation and graph-build spec assembly (FR-008, FR-013)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .adapters.base import SourceAdapter, UnknownObject
from .topology import Topology
from .transforms import TRANSFORM_NAMES

NULL_KEY_POLICIES = ("skip", "error")


def _check_transforms(value: Any, columns: list[str], label: str, errors: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, list) or len(value) != len(columns):
        errors.append(f"{label} must be a list with one transform list per key column ({len(columns)})")
        return
    for chain in value:
        if not isinstance(chain, list) or any(t not in TRANSFORM_NAMES for t in chain):
            errors.append(f"{label} contains unknown transforms {chain!r}; allowed: {', '.join(TRANSFORM_NAMES)}")


def _check_columns(adapter: SourceAdapter | None, table: str, columns: list[str], errors: list[str]) -> None:
    if adapter is None:
        return
    try:
        adapter.resolve_table(table)
        for column in columns:
            adapter.resolve_column(table, column)
    except UnknownObject as exc:
        errors.append(str(exc))


def validate_node_mapping(mapping: dict, topology: Topology, adapter: SourceAdapter | None = None) -> list[str]:
    errors: list[str] = []
    node_type = mapping.get("node_type")
    if node_type not in topology.node_types:
        errors.append(f"node_type '{node_type}' is not defined in the topology")
    if not mapping.get("source_table"):
        errors.append("source_table is required")
    keys = mapping.get("key_columns") or []
    if not isinstance(keys, list) or not keys:
        errors.append("key_columns must be a non-empty list")
        keys = []
    _check_transforms(mapping.get("key_transforms"), keys, "key_transforms", errors)
    props = mapping.get("properties") or {}
    if not isinstance(props, dict):
        errors.append("properties must be an object mapping property name to source column")
        props = {}
    if mapping.get("null_key_policy", "skip") not in NULL_KEY_POLICIES:
        errors.append(f"null_key_policy must be one of {NULL_KEY_POLICIES}")
    if mapping.get("source_table"):
        _check_columns(adapter, mapping["source_table"], [*keys, *props.values()], errors)
    return errors


def validate_edge_mapping(mapping: dict, topology: Topology, adapter: SourceAdapter | None = None) -> list[str]:
    errors: list[str] = []
    edge_type = topology.edge_types.get(mapping.get("edge_type", ""))
    if edge_type is None:
        errors.append(f"edge_type '{mapping.get('edge_type')}' is not defined in the topology")
    else:
        if mapping.get("from_node_type", edge_type.from_type) != edge_type.from_type:
            errors.append(f"from_node_type must be '{edge_type.from_type}' for edge '{edge_type.name}' (direction is fixed by the topology)")
        if mapping.get("to_node_type", edge_type.to_type) != edge_type.to_type:
            errors.append(f"to_node_type must be '{edge_type.to_type}' for edge '{edge_type.name}' (direction is fixed by the topology)")
    if not mapping.get("source_table"):
        errors.append("source_table is required")
    from_keys = mapping.get("from_key_columns") or []
    to_keys = mapping.get("to_key_columns") or []
    if not from_keys:
        errors.append("from_key_columns must be a non-empty list")
    if not to_keys:
        errors.append("to_key_columns must be a non-empty list")
    _check_transforms(mapping.get("from_key_transforms"), from_keys, "from_key_transforms", errors)
    _check_transforms(mapping.get("to_key_transforms"), to_keys, "to_key_transforms", errors)
    props = mapping.get("properties") or {}
    if not isinstance(props, dict):
        errors.append("properties must be an object mapping property name to source column")
        props = {}
    if mapping.get("null_key_policy", "skip") not in NULL_KEY_POLICIES:
        errors.append(f"null_key_policy must be one of {NULL_KEY_POLICIES}")
    if not isinstance(mapping.get("dedupe", True), bool):
        errors.append("dedupe must be true or false")
    if mapping.get("source_table"):
        _check_columns(adapter, mapping["source_table"], [*from_keys, *to_keys, *props.values()], errors)
    return errors


def normalize_node_mapping(mapping: dict, topology: Topology) -> dict:
    keys = list(mapping["key_columns"])
    return {
        "node_type": mapping["node_type"],
        "source_table": mapping["source_table"],
        "key_columns": keys,
        "key_transforms": mapping.get("key_transforms") or [[] for _ in keys],
        "properties": dict(mapping.get("properties") or {}),
        "null_key_policy": mapping.get("null_key_policy", "skip"),
    }


def normalize_edge_mapping(mapping: dict, topology: Topology) -> dict:
    edge_type = topology.edge_types[mapping["edge_type"]]
    from_keys = list(mapping["from_key_columns"])
    to_keys = list(mapping["to_key_columns"])
    return {
        "edge_type": edge_type.name,
        "from_node_type": edge_type.from_type,
        "to_node_type": edge_type.to_type,
        "source_table": mapping["source_table"],
        "from_key_columns": from_keys,
        "from_key_transforms": mapping.get("from_key_transforms") or [[] for _ in from_keys],
        "to_key_columns": to_keys,
        "to_key_transforms": mapping.get("to_key_transforms") or [[] for _ in to_keys],
        "properties": dict(mapping.get("properties") or {}),
        "null_key_policy": mapping.get("null_key_policy", "skip"),
        "dedupe": bool(mapping.get("dedupe", True)),
    }


@dataclass
class BuildSpec:
    source: str
    topology_version: str
    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    fact_versions: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.nodes)


def revalidation_issues(fact: dict, topology: Topology, adapter: SourceAdapter | None) -> list[str]:
    """Detect topology or schema drift that makes an approved fact unsafe to reuse (FR-013)."""
    issues = []
    if fact.get("topology_version") and fact["topology_version"] != topology.version:
        mapping = fact.get("mapping") or {}
        type_name = mapping.get("node_type") or mapping.get("edge_type")
        if type_name and type_name not in topology.node_types and type_name not in topology.edge_types:
            issues.append(f"{fact['id']}: type '{type_name}' no longer exists in topology {topology.version}")
        else:
            issues.append(f"{fact['id']}: approved under topology {fact['topology_version']}, current is {topology.version}; revalidate")
    if adapter is not None:
        for table, fingerprint in (fact.get("schema_fingerprints") or {}).items():
            try:
                current = adapter.schema_fingerprint(table)
            except UnknownObject:
                issues.append(f"{fact['id']}: table '{table}' no longer exists in source")
                continue
            if current != fingerprint:
                issues.append(f"{fact['id']}: schema of '{table}' changed since approval; revalidate")
    return issues


def build_spec(
    facts: list[dict],
    topology: Topology,
    source: str,
    adapter: SourceAdapter | None = None,
    allow_stale: bool = False,
) -> BuildSpec:
    spec = BuildSpec(source=source, topology_version=topology.version)
    key_arity: dict[str, int] = {}
    for fact in facts:
        if fact.get("source") != source or fact.get("kind") not in ("node_mapping", "edge_mapping"):
            continue
        drift = revalidation_issues(fact, topology, adapter)
        if drift:
            (spec.warnings if allow_stale else spec.errors).extend(drift)
            if not allow_stale:
                continue
        mapping = fact.get("mapping") or {}
        if fact["kind"] == "node_mapping":
            problems = validate_node_mapping(mapping, topology, adapter)
            if problems:
                spec.errors.extend(f"{fact['id']}: {p}" for p in problems)
                continue
            node = normalize_node_mapping(mapping, topology)
            node["fact_id"] = fact["id"]
            arity = len(node["key_columns"])
            if key_arity.setdefault(node["node_type"], arity) != arity:
                spec.errors.append(f"{fact['id']}: key arity {arity} conflicts with other mappings for {node['node_type']}")
                continue
            spec.nodes.append(node)
        else:
            problems = validate_edge_mapping(mapping, topology, adapter)
            if problems:
                spec.errors.extend(f"{fact['id']}: {p}" for p in problems)
                continue
            edge = normalize_edge_mapping(mapping, topology)
            edge["fact_id"] = fact["id"]
            spec.edges.append(edge)
        spec.fact_versions.append({"fact_id": fact["id"], "candidate_id": fact["candidate_id"], "candidate_version": fact["candidate_version"]})

    mapped_types = {n["node_type"] for n in spec.nodes}
    for edge in spec.edges:
        for side, node_type, keys in (
            ("from", edge["from_node_type"], edge["from_key_columns"]),
            ("to", edge["to_node_type"], edge["to_key_columns"]),
        ):
            if node_type not in mapped_types:
                spec.errors.append(
                    f"{edge['fact_id']}: edge {edge['edge_type']} needs an approved node mapping for '{node_type}' ({side} endpoint)"
                )
            elif key_arity.get(node_type) != len(keys):
                spec.errors.append(
                    f"{edge['fact_id']}: {side}_key_columns has {len(keys)} columns but {node_type} keys have {key_arity.get(node_type)}"
                )
    if not spec.nodes:
        spec.errors.append(f"No approved node mappings for source '{source}'. Approve candidates before generating code.")
    for node_type in topology.node_types:
        if node_type not in mapped_types:
            spec.warnings.append(f"Node type '{node_type}' has no approved mapping and will be absent from the graph")
    return spec
