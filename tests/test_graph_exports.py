"""Export schemas, safe identities, and parity between graph backends."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from ddg.graph import GraphFormatError, GraphIndex, GraphRepository
from ddg.graph_exports import write_cypher, write_sqlite
from ddg.sqlite_graph import SQLiteGraphIndex


@pytest.fixture
def data():
    return {
        "format": "ddg-graph/v1", "inputs": {"source": "fixture"}, "built_at": "now",
        "stats": {"nodes": 3, "edges": 3}, "warnings": ["fixture warning"],
        "nodes": [
            {"id": node_id, "type": node_type, "key": node_id,
             "properties": {"name": name, "number": 12, "enabled": True, "missing": None},
             "provenance": [{"fact_id": "fact-1", "rows": 1}]}
            for node_id, node_type, name in [("A", "Person", "CAFÉ"), ("B", "Account", "user"), ("Z", "Person", "alone")]
        ],
        "edges": [
            {"id": edge_id, "type": kind, "source": source, "target": target,
             "properties": {"amount": 1.5}, "provenance": []}
            for edge_id, kind, source, target in [
                ("e1", "owns", "A", "B"), ("e2", "owns", "A", "B"), ("loop", "self", "A", "A"),
            ]
        ],
    }


def test_given_graph_when_exporting_sqlite_then_preserves_columns_and_json(data, tmp_path):
    path = tmp_path / "graph.sqlite"

    write_sqlite(data, path)

    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = list(conn.execute("SELECT * FROM nodes ORDER BY system_id"))
        assert [row["system_id"] for row in rows] == [1, 2, 3]
        assert [row["id"] for row in rows] == ["A", "B", "Z"]
        assert rows[0]["key"] == "A" and rows[0]["type"] == "Person"
        assert json.loads(rows[0]["properties"]) == data["nodes"][0]["properties"]
        assert json.loads(rows[0]["provenance"]) == data["nodes"][0]["provenance"]
        assert dict(conn.execute("SELECT source, target FROM edges WHERE id = 'e1'").fetchone()) == {"source": "A", "target": "B"}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {row["name"] for row in conn.execute("PRAGMA index_list(edges)")} >= {"edges_source", "edges_target", "edges_type"}


def test_given_exported_graph_when_querying_then_matches_json_for_every_filter(data, tmp_path):
    path = tmp_path / "graph.sqlite"
    write_sqlite(data, path)
    sql = GraphRepository(tmp_path).get(path.name)
    static = GraphIndex(data, tmp_path / "graph.json")

    summary = sql.summary()
    summary["name"] = "graph.json"
    assert summary == static.summary()
    for sort in ("source", "id", "degree", "neighbors", "edge_types"):
        for direction in ("both", "in", "out"):
            for edge_type in (None, "owns", "missing"):
                for neighbor_type in (None, "Person", "Account"):
                    filters = dict(sort=sort, direction=direction, edge_type=edge_type, neighbor_type=neighbor_type)
                    assert sql.search(limit=2, **filters) == static.search(limit=2, **filters)
    assert sql.search("café") == static.search("café")
    assert sql.search("true") == static.search("true")
    assert sql.search("none") == static.search("none")
    for depth in (1, 2, 3):
        for limit in (1, 2, 100):
            assert sql.neighborhood("A", depth, limit) == static.neighborhood("A", depth, limit)
    with pytest.raises(KeyError):
        sql.neighborhood("not-a-node")
    assert not hasattr(sql, "nodes") and not hasattr(sql, "edges")


def test_given_existing_snapshot_when_rebuilding_then_replaces_without_stale_elements(data, tmp_path):
    path = tmp_path / "graph.sqlite"
    write_sqlite(data, path)
    graph = GraphRepository(tmp_path).get(path.name)
    data["edges"] = []

    write_sqlite(data, path)

    assert graph.summary()["edge_count"] == 0
    assert graph.search(min_degree=1) == []


def test_given_invalid_endpoints_when_rebuilding_then_keeps_previous_database(data, tmp_path):
    path = tmp_path / "graph.sqlite"
    write_sqlite(data, path)
    original = path.read_bytes()
    data["edges"][0]["target"] = "missing"

    with pytest.raises(sqlite3.IntegrityError):
        write_sqlite(data, path)

    assert path.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["graph.sqlite"]


def test_given_empty_graph_when_querying_sqlite_then_matches_static(data, tmp_path):
    data.update(nodes=[], edges=[])
    path = tmp_path / "empty.db"
    write_sqlite(data, path)
    graph = GraphRepository(tmp_path).get(path.name)
    expected = GraphIndex(data, path)

    assert graph.summary() == expected.summary()
    assert graph.search(sort="degree") == []


def test_given_concurrent_queries_when_reading_sqlite_then_uses_request_local_connections(data, tmp_path):
    path = tmp_path / "graph.sqlite"
    write_sqlite(data, path)
    graph = GraphRepository(tmp_path).get(path.name)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: graph.neighborhood("A"), range(12)))

    assert all(result == results[0] for result in results)


def test_given_sqlite_graph_when_opening_query_session_then_database_is_read_only(data, tmp_path):
    path = tmp_path / "graph.sqlite"
    write_sqlite(data, path)
    graph = SQLiteGraphIndex(path)

    with graph._session() as session:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            session.conn.execute("DELETE FROM nodes")

    assert graph.summary()["node_count"] == 3


def test_given_graph_files_when_listing_then_identifies_backends_and_blocks_other_paths(data, tmp_path):
    write_sqlite(data, tmp_path / "graph.sqlite")
    (tmp_path / "graph.json").write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "import.cypher").write_text("", encoding="utf-8")
    (tmp_path / "directory.json").mkdir()
    repo = GraphRepository(tmp_path)

    assert [(item["name"], item["backend"]) for item in repo.list()] == [("graph.json", "json"), ("graph.sqlite", "sqlite")]
    for name in ("..\\outside.sqlite", "import.cypher", "missing.db", "directory.json"):
        with pytest.raises(FileNotFoundError):
            repo.get(name)


def test_given_non_graph_database_when_opening_then_reports_format_error(tmp_path):
    path = tmp_path / "other.sqlite"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER)")

    with pytest.raises(GraphFormatError, match="cannot query SQLite graph"):
        GraphRepository(tmp_path).get(path.name)


def test_given_special_characters_when_exporting_cypher_then_escapes_literals_and_preserves_identity(data, tmp_path):
    data["nodes"][0]["type"] = "Person`\\name"
    data["nodes"][0]["properties"].update({"display`name": "O'Brien \"quoted\"\n\\text", "ddg_id": "user-defined"})
    data["edges"][0]["type"] = "HAS`ACCOUNT"
    path = tmp_path / "nested" / "graph.cypher"

    write_cypher(data, path)

    text = path.read_text(encoding="utf-8")
    assert "CREATE CONSTRAINT ddg_node_id IF NOT EXISTS" in text
    assert 'SET n:`Person``\\u005cname`' in text
    assert '`display``name`: "O\'Brien \\"quoted\\"\\n\\\\text"' in text
    assert '`ddg_id`: "A"' in text
    assert '`enabled`: true' in text
    assert "`missing`: null" not in text
    assert "\\u00c9" in text
    assert "MERGE (s)-[r:`HAS``ACCOUNT` {ddg_id: \"e1\"}]" in text
    assert 'MERGE (s)-[r:`owns` {ddg_id: "e2"}]' in text
    assert "`ddg_properties_json`:" in text and "`ddg_provenance_json`:" in text
    assert "APOC" not in text


def test_given_literal_unicode_escape_in_identifier_when_exporting_then_preserves_it_as_text(data, tmp_path):
    data["nodes"][0]["type"] = r"Person\u0060"
    path = tmp_path / "graph.cypher"

    write_cypher(data, path)

    assert r"SET n:`Person\u005cu0060`" in path.read_text(encoding="utf-8")
