"""Mapping specification validation and graph-build spec assembly (FR-008, FR-013)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .adapters.base import SourceAdapter, UnknownObject
from .topology import Topology
from .transforms import TRANSFORM_NAMES

NULL_KEY_POLICIES = ("skip", "error")


def identity_contract_errors(mapping: dict, others: list[dict]) -> list[str]:
    """Require a shared semantic namespace to use the same ordered parts and normalization."""
    namespace = mapping.get("identity_namespace")
    if not isinstance(namespace, str) or not namespace.strip():
        return []
    parts = mapping.get("identity_key_parts") or mapping["key_columns"]
    chains = mapping.get("key_transforms") or [[] for _ in mapping["key_columns"]]
    errors = []
    for other in others:
        if (other.get("node_type"), other.get("identity_namespace")) != (mapping["node_type"], namespace):
            continue
        other_parts = other.get("identity_key_parts") or other["key_columns"]
        other_chains = other.get("key_transforms") or [[] for _ in other["key_columns"]]
        if parts != other_parts or chains != other_chains:
            errors.append(
                f"Identity namespace '{namespace}' for {mapping['node_type']} has incompatible contracts: "
                f"ordered identity_key_parts and key_transforms must agree with {other['source_table']} "
                f"({', '.join(other_parts)}). Review the semantic parts and normalization, or use a distinct "
                "namespace for a different identity; source column names may differ."
            )
    return errors


def node_mapping_catalog(snapshot: dict, source: str) -> dict[str, list[dict]]:
    """Expose every active endpoint contract, retaining stable IDs across approval."""
    result: dict[str, list[dict]] = {}
    for fact in reversed(snapshot["facts"]):
        if fact["kind"] == "node_mapping" and fact.get("source") == source:
            mapping = fact["mapping"]
            result.setdefault(mapping["node_type"], []).append({
                **mapping, "mapping_id": fact["candidate_id"], "fact_id": fact["id"],
                "_status": "approved", "identity_namespace": mapping.get("identity_namespace"),
            })
    for candidate in snapshot["pending"]:
        if candidate["kind"] == "node_mapping" and candidate.get("scope", {}).get("source") == source:
            mapping = candidate["mapping"]
            result.setdefault(mapping["node_type"], []).append({
                **mapping, "mapping_id": candidate["id"], "_status": "pending",
                "identity_namespace": mapping.get("identity_namespace"),
            })
    return result


def available_node_mappings(snapshot: dict, source: str) -> dict[str, dict]:
    """Choose a preferred contract for offline discovery, never for graph endpoint binding."""
    return {name: mappings[0] for name, mappings in node_mapping_catalog(snapshot, source).items()}


def resolve_edge_endpoints(mapping: dict, catalog: dict[str, list[dict]]) -> tuple[dict[str, dict], list[str]]:
    """Resolve and validate each endpoint against one specific node identity contract."""
    bindings: dict[str, dict] = {}
    errors: list[str] = []
    for side in ("from", "to"):
        node_type = mapping[f"{side}_node_type"]
        options = catalog.get(node_type, [])
        reference = mapping.get(f"{side}_mapping_id")
        if not options:
            errors.append(
                f"Missing node mapping for endpoint '{node_type}': edge {mapping['edge_type']} needs an "
                f"approved node mapping for '{node_type}' ({side} endpoint). "
                "Approve a valid node mapping first, or leave this relationship out of this build."
            )
            continue
        if reference:
            options = [node for node in options if reference in (node.get("mapping_id"), node.get("fact_id"))]
            if not options:
                errors.append(
                    f"{side}_mapping_id '{reference}' does not identify an available node mapping for '{node_type}'. "
                    "Select an active endpoint mapping for this source; do not substitute another identity."
                )
                continue
        if len(options) != 1:
            choices = ", ".join(
                f"{node['mapping_id']} ({node['source_table']}: {' + '.join(node['key_columns'])})"
                for node in options
            )
            errors.append(
                f"Relationship '{mapping['edge_type']}' has an ambiguous {node_type} endpoint. "
                f"Set {side}_mapping_id to the intended identity contract: {choices}. "
                "Review a corrected mapping, or leave this relationship out of this build."
            )
            continue
        node = options[0]
        keys = mapping[f"{side}_key_columns"]
        if len(keys) != len(node["key_columns"]):
            errors.append(
                f"Relationship '{mapping['edge_type']}' cannot identify its {node_type} "
                f"{'starting' if side == 'from' else 'ending'} node: "
                f"{side}_key_columns supplies {len(keys)} ID part(s) ({', '.join(keys)}), "
                f"but endpoint {node['mapping_id']} needs {len(node['key_columns'])} "
                f"({' + '.join(node['key_columns'])}). "
                "The relationship must supply the same ID parts in the same order. "
                "Review a corrected mapping, or leave this relationship out of this build; "
                "revalidating an old approval cannot repair missing ID parts."
            )
            continue
        if mapping["source_table"].lower() == node["source_table"].lower():
            transforms = mapping.get(f"{side}_key_transforms") or [[] for _ in keys]
            node_transforms = node.get("key_transforms") or [[] for _ in node["key_columns"]]
            own_keys = [key.lower() for key in keys]
            node_keys = [key.lower() for key in node["key_columns"]]
            if set(own_keys) == set(node_keys) and (own_keys != node_keys or transforms != node_transforms):
                errors.append(
                    f"{side}_key_columns and {side}_key_transforms must use the exact ordered key contract "
                    f"of endpoint {node['mapping_id']} on the same table "
                    f"({' + '.join(node['key_columns'])}); matching key arity alone is not enough."
                )
                continue
        bindings[side] = node
    return bindings, errors


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
    namespace = mapping.get("identity_namespace")
    if not isinstance(namespace, str) or not namespace.strip() or namespace != namespace.strip():
        errors.append(
            "identity_namespace must be an explicit, non-empty semantic namespace without surrounding whitespace "
            "(for example V21_BAN or IMEI). Review a corrected mapping; renewing an old approval cannot choose it."
        )
    keys = mapping.get("key_columns") or []
    if not isinstance(keys, list) or not keys:
        errors.append("key_columns must be a non-empty list")
        keys = []
    elif any(not isinstance(key, str) or not key.strip() for key in keys) or len(set(keys)) != len(keys):
        errors.append("key_columns must contain distinct, non-empty column names")
        keys = []
    _check_transforms(mapping.get("key_transforms"), keys, "key_transforms", errors)
    parts = mapping.get("identity_key_parts", keys)
    if (not isinstance(parts, list) or len(parts) != len(keys)
            or any(not isinstance(part, str) or not part.strip() or part != part.strip() for part in parts)
            or len(set(parts)) != len(parts)):
        errors.append("identity_key_parts must contain one distinct, non-empty semantic name per ordered key column")
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
    for side, keys in (("from", from_keys), ("to", to_keys)):
        if not isinstance(keys, list) or not keys or any(not isinstance(key, str) or not key.strip() for key in keys):
            errors.append(f"{side}_key_columns must be a non-empty list of column names")
            if side == "from":
                from_keys = []
            else:
                to_keys = []
        reference = mapping.get(f"{side}_mapping_id")
        if reference is not None and (not isinstance(reference, str) or not reference.strip()):
            errors.append(f"{side}_mapping_id must be a non-empty mapping ID")
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
        "identity_namespace": mapping["identity_namespace"],
        "identity_key_parts": list(mapping.get("identity_key_parts", keys)),
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
        **{f"{side}_mapping_id": mapping[f"{side}_mapping_id"]
           for side in ("from", "to") if mapping.get(f"{side}_mapping_id")},
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
    fact_errors: dict[str, list[str]] = field(default_factory=dict)

    def add_fact_errors(self, fact_id: str, problems: list[str]) -> None:
        """Keep actionable per-fact errors alongside the CLI-compatible flat list."""
        self.fact_errors.setdefault(fact_id, []).extend(problems)
        self.errors.extend(f"{fact_id}: {problem}" for problem in problems)

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
                spec.add_fact_errors(fact["id"], problems)
                continue
            node = normalize_node_mapping(mapping, topology)
            node["fact_id"] = fact["id"]
            node["mapping_id"] = fact["candidate_id"]
            spec.nodes.append(node)
        else:
            problems = validate_edge_mapping(mapping, topology, adapter)
            if problems:
                spec.add_fact_errors(fact["id"], problems)
                continue
            edge = normalize_edge_mapping(mapping, topology)
            edge["fact_id"] = fact["id"]
            spec.edges.append(edge)
        spec.fact_versions.append({
            "fact_id": fact["id"], "candidate_id": fact["candidate_id"],
            "candidate_version": fact["candidate_version"], "fact_revision": fact.get("revision", 1),
        })

    mapped_types = {n["node_type"] for n in spec.nodes}
    catalog: dict[str, list[dict]] = {}
    for node in spec.nodes:
        problems = identity_contract_errors(node, [other for other in spec.nodes if other is not node])
        if problems:
            spec.add_fact_errors(node["fact_id"], problems)
        catalog.setdefault(node["node_type"], []).append(node)
    for edge in spec.edges:
        bindings, problems = resolve_edge_endpoints(edge, catalog)
        if problems:
            spec.add_fact_errors(edge["fact_id"], problems)
        for side, node in bindings.items():
            edge[f"{side}_mapping_id"] = node["mapping_id"]
            edge[f"{side}_identity_namespace"] = node["identity_namespace"]
    if not spec.nodes:
        spec.errors.append(f"No approved node mappings for source '{source}'. Approve candidates before generating code.")
    for node_type in topology.node_types:
        if node_type not in mapped_types:
            spec.warnings.append(f"Node type '{node_type}' has no approved mapping and will be absent from the graph")
    return spec
