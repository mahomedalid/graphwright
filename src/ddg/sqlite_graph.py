"""Read-only SQL-backed queries with the JSON graph visualizer's response contract."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator

from .codegen import GRAPH_FORMAT
from .graph import Direction, GraphFormatError, GraphIndex, GraphSort
from .graph_exports import SQLITE_GRAPH_VERSION

_METRIC_COLUMNS = ("degree", "in_degree", "out_degree", "neighbor_count", "edge_type_count")
_SELECT_METRICS = ", ".join(f"m.{name}" for name in (*_METRIC_COLUMNS, "edge_types"))


def _element(row: sqlite3.Row, *, node: bool) -> dict:
    columns = ("id", "type", "key") if node else ("id", "type", "source", "target")
    return {
        **{key: row[key] for key in columns},
        "properties": json.loads(row["properties"]),
        "provenance": json.loads(row["provenance"]),
    }


def _metrics(row: sqlite3.Row) -> dict:
    return {
        **{key: row[key] or 0 for key in _METRIC_COLUMNS},
        "edge_types": sorted(json.loads(row["edge_types"] or "[]")),
    }


def _matches(query: str, node_id: str, key: str, properties: str) -> bool:
    values = json.loads(properties).values()
    return query in " ".join([node_id, key, *(str(value) for value in values)]).lower()


def _metrics_cte(
    edge_types: list[str] | None = None, direction: Direction = "both",
    neighbor_type: str | None = None, node_id: str | None = None,
) -> tuple[str, list[str | int]]:
    conditions: list[str] = []
    params: list[str | int] = []
    if node_id is not None:
        conditions.append("n.id = ?")
        params.append(node_id)
    if edge_types:
        conditions.append("e.type IN (" + ", ".join("?" for _ in edge_types) + ")")
        params.extend(edge_types)
    if direction != "both":
        conditions.append("e.target = n.id" if direction == "in" else "e.source = n.id")
    if neighbor_type:
        conditions.append("other.type = ?")
        params.append(neighbor_type)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    return """
        WITH matching AS (
            SELECT n.id AS node_id, e.*,
                   CASE WHEN e.source = n.id THEN e.target ELSE e.source END AS neighbor
            FROM nodes n JOIN edges e ON (e.source = n.id OR e.target = n.id)
            JOIN nodes other ON other.id = CASE WHEN e.source = n.id THEN e.target ELSE e.source END
    """ + where + """
        ), metrics AS (
            SELECT node_id, count(*) AS degree,
                   sum(source = node_id) AS out_degree, sum(target = node_id) AS in_degree,
                   count(DISTINCT CASE WHEN neighbor != node_id THEN neighbor END) AS neighbor_count,
                   count(DISTINCT type) AS edge_type_count,
                   json_group_array(DISTINCT type) AS edge_types
            FROM matching GROUP BY node_id
        )
    """, params


class _SQLiteSession(GraphIndex):
    """Reuse bounded traversal while resolving elements and metrics through SQL."""

    def __init__(self, conn: sqlite3.Connection, path: Path):
        self.conn = conn
        self.path = path

    def _node(self, node_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            raise KeyError(node_id)
        return _element(row, node=True)

    def _edge(self, edge_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM edges WHERE id = ?", (edge_id,)).fetchone()
        if row is None:
            raise KeyError(edge_id)
        return _element(row, node=False)

    def _matching_edges(
        self, node_id: str, edge_types: list[str] | None = None,
        direction: Direction = "both", neighbor_type: str | None = None,
    ) -> list[dict]:
        cte, params = _metrics_cte(edge_types, direction, neighbor_type, node_id)
        return [_element(row, node=False) for row in self.conn.execute(
            cte + " SELECT * FROM matching ORDER BY system_id", params,
        )]

    def _metrics(
        self, node_id: str, edge_types: list[str] | None = None,
        direction: Direction = "both", neighbor_type: str | None = None,
    ) -> dict:
        cte, params = _metrics_cte(edge_types, direction, neighbor_type, node_id)
        row = self.conn.execute(
            cte + f" SELECT {_SELECT_METRICS} FROM nodes n LEFT JOIN metrics m ON n.id = m.node_id WHERE n.id = ?",
            [*params, node_id],
        ).fetchone()
        if row is None:
            raise KeyError(node_id)
        return _metrics(row)

    def summary(self) -> dict:
        metadata = {row["key"]: json.loads(row["value"]) for row in self.conn.execute("SELECT * FROM graph_metadata")}
        cte, params = _metrics_cte()
        counts = self.conn.execute(cte + """
            SELECT count(*) AS node_count,
                   coalesce(sum(coalesce(m.degree, 0) > 0), 0) AS connected_nodes,
                   coalesce(sum(coalesce(m.degree, 0) = 0), 0) AS isolated_nodes,
                   coalesce(sum(coalesce(m.degree, 0) >= 2), 0) AS multi_edge_nodes,
                   coalesce(max(m.degree), 0) AS max_degree
            FROM nodes n LEFT JOIN metrics m ON n.id = m.node_id
        """, params).fetchone()
        return {
            **{key: metadata.get(key) for key in ("format", "inputs", "built_at", "stats", "warnings")},
            "name": self.path.name,
            "node_count": counts["node_count"],
            "edge_count": self.conn.execute("SELECT count(*) FROM edges").fetchone()[0],
            "node_types": dict(self.conn.execute("SELECT type, count(*) FROM nodes GROUP BY type ORDER BY type")),
            "edge_types": dict(self.conn.execute("SELECT type, count(*) FROM edges GROUP BY type ORDER BY type")),
            "connectivity": {key: counts[key] for key in ("connected_nodes", "isolated_nodes", "multi_edge_nodes", "max_degree")},
        }

    def search(
        self, query: str = "", node_type: str | None = None, limit: int = 50, *,
        edge_type: str | None = None, direction: Direction = "both", neighbor_type: str | None = None,
        min_degree: int = 0, min_neighbors: int = 0, min_edge_types: int = 0, sort: GraphSort = "source",
    ) -> list[dict]:
        cte, params = _metrics_cte([edge_type] if edge_type else None, direction, neighbor_type)
        conditions = ["coalesce(m.degree, 0) >= ?", "coalesce(m.neighbor_count, 0) >= ?", "coalesce(m.edge_type_count, 0) >= ?"]
        params.extend([min_degree, min_neighbors, min_edge_types])
        if edge_type or direction != "both" or neighbor_type:
            conditions.append("m.degree > 0")
        if node_type:
            conditions.append("n.type = ?")
            params.append(node_type)
        if query.strip():
            conditions.append("ddg_matches(?, n.id, n.key, n.properties)")
            params.append(query.lower().strip())
        order = {
            "source": "n.system_id", "id": "n.id",
            "degree": "coalesce(m.degree, 0) DESC, n.id",
            "neighbors": "coalesce(m.neighbor_count, 0) DESC, coalesce(m.degree, 0) DESC, n.id",
            "edge_types": "coalesce(m.edge_type_count, 0) DESC, coalesce(m.degree, 0) DESC, n.id",
        }[sort]
        rows = self.conn.execute(
            cte + f" SELECT n.*, {_SELECT_METRICS} FROM nodes n LEFT JOIN metrics m ON n.id = m.node_id WHERE "
            + " AND ".join(conditions) + f" ORDER BY {order} LIMIT ?", [*params, limit],
        )
        return [{**_element(row, node=True), "connectivity": _metrics(row)} for row in rows]


class SQLiteGraphIndex(GraphIndex):
    """Open one read-only transaction per request, without an in-memory graph cache."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        with self._session():
            pass

    @contextmanager
    def _session(self) -> Iterator[_SQLiteSession]:
        try:
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                conn.create_function("ddg_matches", 4, _matches, deterministic=True)
                conn.execute("BEGIN")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                metadata = conn.execute("SELECT value FROM graph_metadata WHERE key = 'format'").fetchone()
                if version != SQLITE_GRAPH_VERSION or metadata is None or json.loads(metadata[0]) != GRAPH_FORMAT:
                    raise GraphFormatError(f"{self.path.name}: expected a ddg SQLite graph version {SQLITE_GRAPH_VERSION}")
                yield _SQLiteSession(conn, self.path)
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise GraphFormatError(f"{self.path.name}: cannot query SQLite graph: {exc}") from exc

    def summary(self) -> dict:
        """Return live counts and metadata from the SQLite graph."""
        with self._session() as session:
            return session.summary()

    def search(
        self, query: str = "", node_type: str | None = None, limit: int = 50, *,
        edge_type: str | None = None, direction: Direction = "both", neighbor_type: str | None = None,
        min_degree: int = 0, min_neighbors: int = 0, min_edge_types: int = 0, sort: GraphSort = "source",
    ) -> list[dict]:
        """Filter and rank in SQL, materializing only the requested results."""
        with self._session() as session:
            return session.search(
                query, node_type, limit, edge_type=edge_type, direction=direction, neighbor_type=neighbor_type,
                min_degree=min_degree, min_neighbors=min_neighbors, min_edge_types=min_edge_types, sort=sort,
            )

    def neighborhood(
        self, node_id: str, depth: int = 1, limit: int = 150, edge_types: list[str] | None = None, *,
        direction: Direction = "both", neighbor_type: str | None = None,
    ) -> dict:
        """Traverse indexed incident edges in a consistent read-only transaction."""
        with self._session() as session:
            return session.neighborhood(node_id, depth, limit, edge_types, direction=direction, neighbor_type=neighbor_type)
