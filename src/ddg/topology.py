"""Parse and validate the user-authored Markdown graph topology (FR-001).

Expected structure (domain-agnostic; any business entities may be used)::

    # Graph Topology: <title>

    ## Node Types
    ### <NodeTypeName>
    Free-text description of the business entity.
    - Key: what uniquely identifies it
    - Properties: name, email, city
    - Synonyms: client, buyer

    ## Edge Types
    ### <EDGE_TYPE_NAME>
    Free-text description of the relationship.
    - From: <NodeTypeName>
    - To: <NodeTypeName>
    - Cardinality: one-to-many | many-to-one | one-to-one | many-to-many
    - Properties: quantity

    ## Constraints
    - Free-text constraints the graph must respect.

    ## Hints
    - Known relationships or tips for the discovery agent (never treated as approved facts).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings
    from .control import RunControl

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
CARDINALITIES = {"one-to-one", "one-to-many", "many-to-one", "many-to-many"}
NODE_FIELDS = {"key", "properties", "synonyms"}
EDGE_FIELDS = {"from", "to", "cardinality", "properties", "synonyms"}
SECTION_ALIASES = {
    "node types": "nodes",
    "nodes": "nodes",
    "edge types": "edges",
    "edges": "edges",
    "relationships": "edges",
    "constraints": "constraints",
    "hints": "hints",
    "known relationships": "hints",
    "notes": "hints",
}


@dataclass
class NodeType:
    name: str
    description: str = ""
    key: str = ""
    properties: list[str] = field(default_factory=list)
    synonyms: list[str] = field(default_factory=list)


@dataclass
class EdgeType:
    name: str
    from_type: str = ""
    to_type: str = ""
    description: str = ""
    cardinality: str = ""
    properties: list[str] = field(default_factory=list)
    synonyms: list[str] = field(default_factory=list)


@dataclass
class Issue:
    level: str  # "error" or "warning"
    message: str
    line: int | None = None


@dataclass
class Topology:
    title: str
    version: str
    node_types: dict[str, NodeType]
    edge_types: dict[str, EdgeType]
    constraints: list[str]
    hints: list[str]
    issues: list[Issue]
    source_path: str = ""
    parsing_mode: str = "structured"

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "version": self.version,
            "source_path": self.source_path,
            "node_types": [asdict(n) for n in self.node_types.values()],
            "edge_types": [asdict(e) for e in self.edge_types.values()],
            "constraints": self.constraints,
            "hints": self.hints,
            "issues": [asdict(i) for i in self.issues],
            "is_valid": self.is_valid,
            "parsing_mode": self.parsing_mode,
        }

    def summary_for_agent(self) -> str:
        lines = [f"Topology '{self.title}' (version {self.version})", "Node types:"]
        for n in self.node_types.values():
            lines.append(
                f"- {n.name}: {n.description} | key: {n.key or 'unspecified'} | properties: {', '.join(n.properties) or 'none'}"
                + (f" | synonyms: {', '.join(n.synonyms)}" if n.synonyms else "")
            )
        lines.append("Edge types:")
        for e in self.edge_types.values():
            lines.append(
                f"- {e.name}: ({e.from_type})-[{e.name}]->({e.to_type}) {e.cardinality} | {e.description}"
                + (f" | properties: {', '.join(e.properties)}" if e.properties else "")
            )
        if self.constraints:
            lines.append("Constraints:")
            lines.extend(f"- {c}" for c in self.constraints)
        if self.hints:
            lines.append("Hints (unverified, not approved facts):")
            lines.extend(f"- {h}" for h in self.hints)
        return "\n".join(lines)


class TopologyError(ValueError):
    def __init__(self, topology: Topology):
        self.topology = topology
        details = "; ".join(f"line {i.line}: {i.message}" if i.line else i.message for i in topology.errors)
        super().__init__(f"Invalid topology: {details}")


def _split_list(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;]", value) if part.strip()]


def parse_topology(text: str, source_path: str = "") -> Topology:
    issues: list[Issue] = []
    title = ""
    section: str | None = None
    current: NodeType | EdgeType | None = None
    nodes: dict[str, NodeType] = {}
    edges: dict[str, EdgeType] = {}
    constraints: list[str] = []
    hints: list[str] = []
    in_code = False

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code or not line.strip() or line.strip().startswith("<!--"):
            continue
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            title = re.sub(r"^graph topology\s*:?\s*", "", title, flags=re.I) or title
            continue
        if stripped.startswith("## "):
            heading = stripped[3:].strip().lower()
            section = SECTION_ALIASES.get(heading)
            current = None
            if section is None:
                issues.append(Issue("warning", f"Unrecognized section '{stripped[3:].strip()}' is ignored", lineno))
            continue
        if stripped.startswith("### "):
            name = stripped[4:].strip().strip("`")
            if section not in ("nodes", "edges"):
                issues.append(Issue("error", f"'{name}' must be under '## Node Types' or '## Edge Types'", lineno))
                current = None
                continue
            if not NAME_RE.match(name):
                issues.append(Issue("error", f"Type name '{name}' must start with a letter and use only letters, digits, or underscores", lineno))
            registry = nodes if section == "nodes" else edges
            if name in registry or (name in nodes) or (name in edges):
                issues.append(Issue("error", f"Duplicate type name '{name}'", lineno))
            current = NodeType(name=name) if section == "nodes" else EdgeType(name=name)
            registry[name] = current  # type: ignore[assignment]
            continue

        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        if section in ("constraints", "hints"):
            item = bullet.group(1).strip() if bullet else stripped
            (constraints if section == "constraints" else hints).append(item)
            continue
        if current is None:
            if section in ("nodes", "edges"):
                issues.append(Issue("warning", "Text outside a '### Type' block is ignored", lineno))
            continue

        field_match = re.match(r"^\*{0,2}([A-Za-z ]+?)\*{0,2}\s*:\s*(.*)$", bullet.group(1)) if bullet else None
        if field_match:
            key = field_match.group(1).strip().lower()
            value = field_match.group(2).strip()
            allowed = NODE_FIELDS if isinstance(current, NodeType) else EDGE_FIELDS
            if key not in allowed:
                issues.append(Issue("warning", f"Unknown field '{key}' on '{current.name}' (allowed: {', '.join(sorted(allowed))})", lineno))
                continue
            if key in ("properties", "synonyms"):
                setattr(current, key, _split_list(value))
            elif key == "from":
                current.from_type = value.strip("`")  # type: ignore[union-attr]
            elif key == "to":
                current.to_type = value.strip("`")  # type: ignore[union-attr]
            elif key == "cardinality":
                current.cardinality = value.lower()  # type: ignore[union-attr]
            else:
                setattr(current, key, value)
        else:
            text_part = bullet.group(1) if bullet else stripped
            current.description = f"{current.description} {text_part}".strip()

    if not nodes:
        issues.append(Issue("error", "No node types defined. Add a '## Node Types' section with '### TypeName' entries."))
    for node in nodes.values():
        if not node.description:
            issues.append(Issue("warning", f"Node type '{node.name}' has no description; discovery will rely on its name only"))
        if not node.key:
            issues.append(Issue("warning", f"Node type '{node.name}' has no '- Key:' hint; identifier selection is ambiguous"))
    for edge in edges.values():
        if not edge.from_type or not edge.to_type:
            issues.append(Issue("error", f"Edge type '{edge.name}' must define both '- From:' and '- To:'"))
            continue
        for endpoint in (edge.from_type, edge.to_type):
            if endpoint not in nodes:
                issues.append(Issue("error", f"Edge type '{edge.name}' references undefined node type '{endpoint}'"))
        if not edge.cardinality:
            issues.append(Issue("warning", f"Edge type '{edge.name}' has no cardinality; uniqueness checks are skipped"))
        elif edge.cardinality not in CARDINALITIES:
            issues.append(Issue("error", f"Edge type '{edge.name}' has invalid cardinality '{edge.cardinality}' (allowed: {', '.join(sorted(CARDINALITIES))})"))

    normalized = "\n".join(line.rstrip() for line in text.strip().splitlines())
    version = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return Topology(
        title=title or "Untitled topology",
        version=version,
        node_types=nodes,
        edge_types=edges,
        constraints=constraints,
        hints=hints,
        issues=issues,
        source_path=source_path,
    )


def load_topology(path: Path, *, strict: bool = True) -> Topology:
    if not path.exists():
        topology = Topology("Missing", "none", {}, {}, [], [], [Issue("error", f"Topology file not found: {path}")], str(path))
        if strict:
            raise TopologyError(topology)
        return topology
    topology = parse_topology(path.read_text(encoding="utf-8"), source_path=str(path))
    if strict and not topology.is_valid:
        raise TopologyError(topology)
    return topology


def load_workspace_topology(
    settings: Settings, *, strict: bool = True, resolve: bool = False, control: RunControl | None = None
) -> Topology:
    """Load structured or cached topology; explicitly resolve free-form text with Foundry.

    Read-only callers never invoke a model. Successful extractions are cached under
    metadata and invalidated whenever the original source text changes.
    """
    from .store import read_json

    source = load_topology(settings.topology_file, strict=False)
    topology = source
    cache_path = settings.metadata_dir / "topology.json"
    if not source.is_valid and source.version != "none":
        cached = read_json(cache_path)
        source_path = str(settings.topology_file.resolve())
        if cached and cached.get("source_version") == source.version and cached.get("source_path") == source_path:
            topology = _resolved_topology(cached["markdown"], source)
        elif resolve and settings.model.configured:
            topology = _parse_and_cache(settings, source, control)
        elif settings.model.configured:
            topology.parsing_mode = "pending_foundry"
            topology.issues = [
                Issue("warning" if issue.level == "error" else issue.level, issue.message, issue.line)
                for issue in topology.issues
            ]
            topology.issues.append(Issue(
                "error", "Free-form topology needs Foundry parsing. Start a discovery run with auto/foundry "
                "or run 'ddg topology' to parse and validate it."
            ))
        else:
            topology.issues.append(Issue(
                "error", "Foundry is not configured and the topology is not valid structured Markdown. "
                "Either set FOUNDRY_PROJECT_ENDPOINT and FOUNDRY_MODEL to parse free-form text, "
                "or use '## Node Types' / '### TypeName' and '## Edge Types' with From/To fields."
            ))
    if strict and not topology.is_valid:
        raise TopologyError(topology)
    return topology


def _parse_and_cache(settings: Settings, source: Topology, control: RunControl | None) -> Topology:
    from .agent.topology_parser import parse_with_foundry
    from .control import RunControl
    from .store import atomic_write_json, file_lock, read_json

    control = control or RunControl(settings.limits, settings.model, cancel_requested=lambda: False)
    control.check()
    cache_path = settings.metadata_dir / "topology.json"
    source_path = str(settings.topology_file.resolve())
    control.emit("topology_parse_waiting", message="Waiting for the topology parsing lock.")
    with file_lock(
        cache_path.with_suffix(".lock"),
        timeout=control.remaining_seconds(),
        stale_after=max(3600.0, settings.limits.max_minutes * 60 + 60),
    ):
        cached = read_json(cache_path)
        if cached and cached.get("source_version") == source.version and cached.get("source_path") == source_path:
            return _resolved_topology(cached["markdown"], source)
        text = settings.topology_file.read_text(encoding="utf-8")
        control.emit("topology_parse_started", characters=len(text))
        if parse_topology(text).version != source.version:
            raise ValueError("Topology changed before Foundry parsing; retry with the updated document")
        markdown = parse_with_foundry(text, settings, control)
        topology = _resolved_topology(markdown, source)
        if not topology.is_valid:
            raise TopologyError(topology)
        if load_topology(settings.topology_file, strict=False).version != source.version:
            raise ValueError("Topology changed during Foundry parsing; retry with the updated document")
        atomic_write_json(cache_path, {
            "source_version": source.version,
            "source_path": source_path,
            "markdown": markdown,
            "model": settings.model.model,
        })
    return topology


def _resolved_topology(markdown: str, source: Topology) -> Topology:
    topology = parse_topology(markdown, source_path=source.source_path)
    topology.version = hashlib.sha256(f"{source.version}:{topology.version}".encode("utf-8")).hexdigest()[:12]
    topology.parsing_mode = "foundry"
    topology.issues.append(Issue(
        "warning", "Topology interpreted by Foundry from free-form text. Review the extracted node and edge types."
    ))
    return topology
