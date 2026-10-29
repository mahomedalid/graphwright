"""Standard-library graph writers, also embedded in standalone generated builders."""

import json
import math
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

SQLITE_GRAPH_VERSION = 1


def write_sqlite(graph: dict, path: str | Path) -> None:
    """Atomically replace a graph snapshot with indexed relational nodes and edges."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".sqlite", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with closing(sqlite3.connect(temporary)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript("""
                CREATE TABLE graph_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL CHECK(json_valid(value)));
                CREATE TABLE nodes (
                    system_id INTEGER PRIMARY KEY,
                    id TEXT NOT NULL UNIQUE,
                    type TEXT NOT NULL,
                    key TEXT NOT NULL,
                    properties TEXT NOT NULL CHECK(json_valid(properties)),
                    provenance TEXT NOT NULL CHECK(json_valid(provenance))
                );
                CREATE TABLE edges (
                    system_id INTEGER PRIMARY KEY,
                    id TEXT NOT NULL UNIQUE,
                    type TEXT NOT NULL,
                    source TEXT NOT NULL REFERENCES nodes(id),
                    target TEXT NOT NULL REFERENCES nodes(id),
                    properties TEXT NOT NULL CHECK(json_valid(properties)),
                    provenance TEXT NOT NULL CHECK(json_valid(provenance))
                );
                CREATE INDEX nodes_type ON nodes(type);
                CREATE INDEX edges_source ON edges(source);
                CREATE INDEX edges_target ON edges(target);
                CREATE INDEX edges_type ON edges(type);
            """)
            conn.execute(f"PRAGMA user_version = {SQLITE_GRAPH_VERSION}")
            conn.executemany(
                "INSERT INTO graph_metadata VALUES (?, ?)",
                [(key, json.dumps(value, allow_nan=False)) for key, value in graph.items() if key not in ("nodes", "edges")],
            )
            conn.executemany(
                "INSERT INTO nodes (id, type, key, properties, provenance) VALUES (?, ?, ?, ?, ?)",
                [(n["id"], n["type"], n["key"], json.dumps(n["properties"], allow_nan=False),
                  json.dumps(n.get("provenance", []), allow_nan=False)) for n in graph["nodes"]],
            )
            conn.executemany(
                "INSERT INTO edges (id, type, source, target, properties, provenance) VALUES (?, ?, ?, ?, ?, ?)",
                [(e["id"], e["type"], e["source"], e["target"], json.dumps(e.get("properties", {}), allow_nan=False),
                  json.dumps(e.get("provenance", []), allow_nan=False)) for e in graph["edges"]],
            )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _cypher_literal(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Cypher properties cannot contain non-finite numbers")
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    raise ValueError(f"Unsupported Cypher property value: {type(value).__name__}")


def _cypher_identifier(value: str) -> str:
    return "`" + value.replace("\\", "\\u005c").replace("`", "``") + "`"


def _cypher_properties(element: dict) -> str:
    # Keep user properties separate from reserved identity fields, without APOC.
    props = {k: v for k, v in element.get("properties", {}).items() if v is not None}
    props.update({
        "ddg_id": element["id"], "ddg_type": element["type"],
        "ddg_properties_json": json.dumps(element.get("properties", {}), allow_nan=False),
        "ddg_provenance_json": json.dumps(element.get("provenance", []), allow_nan=False),
    })
    if "key" in element:
        props["ddg_key"] = element["key"]
    return "{" + ", ".join(f"{_cypher_identifier(k)}: {_cypher_literal(v)}" for k, v in props.items()) + "}"


def write_cypher(graph: dict, path: str | Path) -> None:
    """Write an idempotent Neo4j 5+ import for cypher-shell, without APOC."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write("// Neo4j 5+. Run with cypher-shell -f <this-file>. Contains source data; protect accordingly.\n")
        handle.write("CREATE CONSTRAINT ddg_node_id IF NOT EXISTS FOR (n:DDGNode) REQUIRE n.ddg_id IS UNIQUE;\n")
        for node in graph["nodes"]:
            handle.write(
                f"MERGE (n:DDGNode {{ddg_id: {_cypher_literal(node['id'])}}}) "
                f"SET n:{_cypher_identifier(node['type'])} SET n += {_cypher_properties(node)};\n"
            )
        for edge in graph["edges"]:
            handle.write(
                f"MATCH (s:DDGNode {{ddg_id: {_cypher_literal(edge['source'])}}}), "
                f"(t:DDGNode {{ddg_id: {_cypher_literal(edge['target'])}}}) "
                f"MERGE (s)-[r:{_cypher_identifier(edge['type'])} "
                f"{{ddg_id: {_cypher_literal(edge['id'])}}}]->(t) SET r += {_cypher_properties(edge)};\n"
            )
