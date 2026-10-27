"""Load builder output and serve bounded graph queries for the visualizer (FR-011)."""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from pathlib import Path

from .codegen import GRAPH_FORMAT


class GraphFormatError(ValueError):
    pass


class GraphIndex:
    def __init__(self, data: dict, path: Path):
        if data.get("format") != GRAPH_FORMAT:
            raise GraphFormatError(f"{path.name}: expected format '{GRAPH_FORMAT}', got '{data.get('format')}'")
        self.path = path
        self.meta = {k: data.get(k) for k in ("format", "inputs", "built_at", "stats", "warnings")}
        self.nodes: dict[str, dict] = {n["id"]: n for n in data.get("nodes", [])}
        self.edges: dict[str, dict] = {e["id"]: e for e in data.get("edges", [])}
        self.adjacency: dict[str, list[str]] = defaultdict(list)
        for edge in self.edges.values():
            self.adjacency[edge["source"]].append(edge["id"])
            self.adjacency[edge["target"]].append(edge["id"])

    def summary(self) -> dict:
        node_types: dict[str, int] = defaultdict(int)
        edge_types: dict[str, int] = defaultdict(int)
        for n in self.nodes.values():
            node_types[n["type"]] += 1
        for e in self.edges.values():
            edge_types[e["type"]] += 1
        return {
            **self.meta,
            "name": self.path.name,
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "node_types": dict(sorted(node_types.items())),
            "edge_types": dict(sorted(edge_types.items())),
        }

    def search(self, query: str = "", node_type: str | None = None, limit: int = 50) -> list[dict]:
        q = query.lower().strip()
        results = []
        for node in self.nodes.values():
            if node_type and node["type"] != node_type:
                continue
            if q:
                haystack = " ".join([node["id"], *(str(v) for v in node["properties"].values())]).lower()
                if q not in haystack:
                    continue
            results.append(node)
            if len(results) >= limit:
                break
        return results

    def neighborhood(self, node_id: str, depth: int = 1, limit: int = 150, edge_types: list[str] | None = None) -> dict:
        if node_id not in self.nodes:
            raise KeyError(node_id)
        depth = max(1, min(depth, 3))
        seen_nodes = {node_id}
        seen_edges: set[str] = set()
        frontier = [node_id]
        truncated = False
        for _ in range(depth):
            next_frontier = []
            for current in frontier:
                for edge_id in self.adjacency.get(current, []):
                    edge = self.edges[edge_id]
                    if edge_types and edge["type"] not in edge_types:
                        continue
                    other = edge["target"] if edge["source"] == current else edge["source"]
                    if other not in seen_nodes and len(seen_nodes) >= limit:
                        truncated = True
                        continue
                    seen_edges.add(edge_id)
                    if other not in seen_nodes:
                        seen_nodes.add(other)
                        next_frontier.append(other)
            frontier = next_frontier
        return {
            "center": node_id,
            "nodes": [self.nodes[n] for n in sorted(seen_nodes)],
            "edges": [self.edges[e] for e in sorted(seen_edges)],
            "truncated": truncated,
            "degree": len(self.adjacency.get(node_id, [])),
        }


class GraphRepository:
    """Caches GraphIndex objects per file, reloading when the file changes."""

    def __init__(self, directory: Path):
        self.directory = directory
        self._cache: dict[str, tuple[float, GraphIndex]] = {}
        self._lock = threading.Lock()

    def list(self) -> list[dict]:
        items = []
        for path in sorted(self.directory.glob("*.json")):
            items.append({"name": path.name, "size": path.stat().st_size, "modified": path.stat().st_mtime})
        return items

    def get(self, name: str) -> GraphIndex:
        path = (self.directory / name).resolve()
        if path.parent != self.directory.resolve() or path.suffix != ".json" or not path.exists():
            raise FileNotFoundError(name)
        mtime = path.stat().st_mtime
        with self._lock:
            cached = self._cache.get(name)
            if cached and cached[0] == mtime:
                return cached[1]
            index = GraphIndex(json.loads(path.read_text(encoding="utf-8")), path)
            self._cache[name] = (mtime, index)
            return index
