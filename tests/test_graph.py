"""Connectivity search and bounded graph traversal."""

from pathlib import Path

import pytest

from ddg.graph import GraphIndex


@pytest.fixture
def graph() -> GraphIndex:
    nodes = [
        {"id": node_id, "type": node_type, "key": node_id, "properties": {"name": name}}
        for node_id, node_type, name in [
            ("Z", "Device", "isolated"), ("D", "Service", "leaf"),
            ("C", "Address", "location"), ("B", "Account", "account"),
            ("S", "Device", "self"), ("A", "Person", "hub"),
        ]
    ]
    edges = [
        {"id": edge_id, "type": edge_type, "source": source, "target": target}
        for edge_id, edge_type, source, target in [
            ("e1", "owns", "A", "B"), ("e2", "owns", "A", "B"),
            ("e3", "located", "C", "A"), ("e4", "uses", "A", "D"),
            ("e5", "billed", "B", "C"), ("e6", "loop", "S", "S"),
        ]
    ]
    return GraphIndex({"format": "ddg-graph/v1", "nodes": nodes, "edges": edges}, Path("test.json"))


def test_given_connected_graph_when_summarizing_then_reports_connectivity(graph):
    summary = graph.summary()

    assert summary["connectivity"] == {
        "connected_nodes": 5, "isolated_nodes": 1, "multi_edge_nodes": 3, "max_degree": 4,
    }


def test_given_hub_after_first_nodes_when_ranking_then_limits_after_ranking(graph):
    results = graph.search(limit=1, sort="degree")

    assert results[0]["id"] == "A"
    assert results[0]["connectivity"] == {
        "degree": 4, "in_degree": 1, "out_degree": 3,
        "neighbor_count": 3, "edge_type_count": 3, "edge_types": ["located", "owns", "uses"],
    }


@pytest.mark.parametrize(("filters", "expected"), [
    ({}, ["Z", "D", "C", "B", "S", "A"]),
    ({"min_degree": 2, "sort": "degree"}, ["A", "B", "C"]),
    ({"min_neighbors": 2, "sort": "neighbors"}, ["A", "B", "C"]),
    ({"min_edge_types": 2, "sort": "edge_types"}, ["A", "B", "C"]),
    ({"edge_type": "owns", "sort": "id"}, ["A", "B"]),
    ({"edge_type": "owns", "direction": "out"}, ["A"]),
    ({"edge_type": "owns", "direction": "in"}, ["B"]),
    ({"neighbor_type": "Address", "sort": "id"}, ["A", "B"]),
    ({"neighbor_type": "Address", "direction": "in"}, ["A"]),
    ({"node_type": "Person", "min_degree": 2}, ["A"]),
    ({"query": "HUB", "min_edge_types": 3}, ["A"]),
    ({"query": "hub", "edge_type": "billed"}, []),
    ({"edge_type": "missing"}, []),
    ({"min_degree": 100}, []),
])
def test_given_filters_when_searching_then_combines_text_and_connectivity(graph, filters, expected):
    results = graph.search(**filters)

    assert [node["id"] for node in results] == expected


def test_given_parallel_edges_when_filtering_then_distinguishes_edges_from_neighbors(graph):
    edges = graph.search(edge_type="owns", min_degree=2, sort="id")
    neighbors = graph.search(edge_type="owns", min_neighbors=2)

    assert [node["connectivity"]["degree"] for node in edges] == [2, 2]
    assert [node["connectivity"]["neighbor_count"] for node in edges] == [1, 1]
    assert neighbors == []


def test_given_self_loop_when_searching_then_counts_one_incident_edge_without_neighbor(graph):
    metrics = graph.search(query="self")[0]["connectivity"]

    assert metrics["degree"] == 1
    assert metrics["neighbor_count"] == 0
    assert metrics["in_degree"] == metrics["out_degree"] == 1


def test_given_relationship_filters_when_expanding_then_uses_matching_edges(graph):
    neighborhood = graph.neighborhood("A", edge_types=["owns"], direction="out", neighbor_type="Account")

    assert [node["id"] for node in neighborhood["nodes"]] == ["A", "B"]
    assert [edge["id"] for edge in neighborhood["edges"]] == ["e1", "e2"]
    assert neighborhood["degree"] == 2
    assert neighborhood["total_degree"] == 4
    assert neighborhood["nodes"][0]["connectivity"]["neighbor_count"] == 1


def test_given_two_hops_when_expanding_outgoing_then_keeps_direction_at_each_hop(graph):
    neighborhood = graph.neighborhood("A", depth=2, direction="out")

    assert [node["id"] for node in neighborhood["nodes"]] == ["A", "B", "C", "D"]
    assert {edge["id"] for edge in neighborhood["edges"]} == {"e1", "e2", "e4", "e5"}


def test_given_small_limit_when_expanding_then_reports_truncation_and_valid_endpoints(graph):
    neighborhood = graph.neighborhood("A", depth=3, limit=2)
    node_ids = {node["id"] for node in neighborhood["nodes"]}

    assert len(node_ids) == 2
    assert neighborhood["truncated"] is True
    assert all(edge["source"] in node_ids and edge["target"] in node_ids for edge in neighborhood["edges"])


def test_given_no_matching_edges_when_expanding_then_keeps_center(graph):
    neighborhood = graph.neighborhood("A", edge_types=["missing"])

    assert [node["id"] for node in neighborhood["nodes"]] == ["A"]
    assert neighborhood["edges"] == []
    assert neighborhood["degree"] == 0


def test_given_empty_graph_when_exploring_then_reports_zero_counts():
    graph = GraphIndex({"format": "ddg-graph/v1", "nodes": [], "edges": []}, Path("empty.json"))

    assert graph.search(sort="degree") == []
    assert graph.summary()["connectivity"] == {
        "connected_nodes": 0, "isolated_nodes": 0, "multi_edge_nodes": 0, "max_degree": 0,
    }
