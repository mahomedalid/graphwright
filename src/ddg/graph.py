"""Load builder output and serve bounded graph queries for the visualizer (FR-011)."""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from pathlib import Path
from typing import Literal

from .codegen import GRAPH_FORMAT

Direction = Literal["both", "in", "out"]
GraphSort = Literal["source", "id", "degree", "neighbors", "edge_types"]


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
            if edge["target"] != edge["source"]:
                self.adjacency[edge["target"]].append(edge["id"])
        self.connectivity = {
            node_id: self._connection_metrics(node_id, self._matching_edges(node_id))
            for node_id in self.nodes
        }

    def _matching_edges(
        self, node_id: str, edge_types: list[str] | None = None,
        direction: Direction = "both", neighbor_type: str | None = None,
    ) -> list[dict]:
        edges = []
        for edge_id in self.adjacency.get(node_id, []):
            edge = self.edges[edge_id]
            if edge_types and edge["type"] not in edge_types:
                continue
            if direction == "in" and edge["target"] != node_id:
                continue
            if direction == "out" and edge["source"] != node_id:
                continue
            other = edge["target"] if edge["source"] == node_id else edge["source"]
            if other not in self.nodes or (neighbor_type and self.nodes[other]["type"] != neighbor_type):
                continue
            edges.append(edge)
        return edges

    @staticmethod
    def _connection_metrics(node_id: str, edges: list[dict]) -> dict:
        neighbors = {edge["target"] if edge["source"] == node_id else edge["source"] for edge in edges}
        neighbors.discard(node_id)
        edge_types = sorted({edge["type"] for edge in edges})
        return {
            "degree": len(edges),
            "in_degree": sum(edge["target"] == node_id for edge in edges),
            "out_degree": sum(edge["source"] == node_id for edge in edges),
            "neighbor_count": len(neighbors),
            "edge_type_count": len(edge_types),
            "edge_types": edge_types,
        }

    def _metrics(
        self, node_id: str, edge_types: list[str] | None = None,
        direction: Direction = "both", neighbor_type: str | None = None,
    ) -> dict:
        if not edge_types and direction == "both" and not neighbor_type:
            return self.connectivity[node_id]
        return self._connection_metrics(node_id, self._matching_edges(node_id, edge_types, direction, neighbor_type))

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
            "connectivity": {
                "connected_nodes": sum(m["degree"] > 0 for m in self.connectivity.values()),
                "isolated_nodes": sum(m["degree"] == 0 for m in self.connectivity.values()),
                "multi_edge_nodes": sum(m["degree"] >= 2 for m in self.connectivity.values()),
                "max_degree": max((m["degree"] for m in self.connectivity.values()), default=0),
            },
        }

    def search(
        self, query: str = "", node_type: str | None = None, limit: int = 50, *,
        edge_type: str | None = None, direction: Direction = "both", neighbor_type: str | None = None,
        min_degree: int = 0, min_neighbors: int = 0, min_edge_types: int = 0, sort: GraphSort = "source",
    ) -> list[dict]:
        """Find nodes using text and matching incident edges, ranked before applying the limit."""
        q = query.lower().strip()
        results = []
        for node in self.nodes.values():
            if node_type and node["type"] != node_type:
                continue
            if q:
                haystack = " ".join([node["id"], *(str(v) for v in node["properties"].values())]).lower()
                if q not in haystack:
                    continue
            metrics = self._metrics(node["id"], [edge_type] if edge_type else None, direction, neighbor_type)
            if (edge_type or direction != "both" or neighbor_type) and not metrics["degree"]:
                continue
            if (metrics["degree"] < min_degree or metrics["neighbor_count"] < min_neighbors
                    or metrics["edge_type_count"] < min_edge_types):
                continue
            results.append({**node, "connectivity": metrics})
            if sort == "source" and len(results) >= limit:
                break
        if sort == "id":
            results.sort(key=lambda n: n["id"])
        elif sort != "source":
            metric = {"degree": "degree", "neighbors": "neighbor_count", "edge_types": "edge_type_count"}[sort]
            results.sort(key=lambda n: (-n["connectivity"][metric], -n["connectivity"]["degree"], n["id"]))
        return results[:limit]

    def neighborhood(
        self, node_id: str, depth: int = 1, limit: int = 150, edge_types: list[str] | None = None, *,
        direction: Direction = "both", neighbor_type: str | None = None,
    ) -> dict:
        """Traverse a bounded neighborhood using the same edge filters as node search."""
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
                for edge in self._matching_edges(current, edge_types, direction, neighbor_type):
                    edge_id = edge["id"]
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
            "nodes": [
                {**self.nodes[n], "connectivity": self._metrics(n, edge_types, direction, neighbor_type)}
                for n in sorted(seen_nodes)
            ],
            "edges": [self.edges[e] for e in sorted(seen_edges)],
            "truncated": truncated,
            "degree": self._metrics(node_id, edge_types, direction, neighbor_type)["degree"],
            "total_degree": self.connectivity[node_id]["degree"],
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
