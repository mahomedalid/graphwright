"""Edges-only discovery reuses endpoints and never produces other candidate kinds."""

import pytest

from ddg.adapters import create_adapter
from ddg.agent.runner import DiscoveryRunner
from ddg.cli import main
from ddg.control import RunControl
from ddg.tools import DiscoveryTools
from ddg.store import short_hash
from ddg.topology import load_topology


def _nodes(settings, store, *, approve=False):
    runner = DiscoveryRunner(settings, store)
    run = runner.execute(runner.create_run("retail", "heuristic", {"max_iterations": 5})["id"])
    assert run["status"] == "completed", run["error"]
    candidates = store.list_candidates()
    assert len(candidates) == 4 and all(candidate["kind"] == "node_mapping" for candidate in candidates)
    if approve:
        for candidate in candidates:
            store.decide(candidate["id"], "approve", "sme", "confirmed", expected_version=candidate["version"])
    return {candidate["id"] for candidate in candidates}


@pytest.mark.parametrize("approve", [False, True])
def test_given_existing_node_mappings_when_edges_only_runs_then_proposes_only_relationships(settings, store, approve):
    original_ids = _nodes(settings, store, approve=approve)
    runner = DiscoveryRunner(settings, store)

    run = runner.execute(runner.create_run(
        "retail", "heuristic", {"max_iterations": 20}, discovery_scope="edges",
    )["id"])

    new = [candidate for candidate in store.list_candidates() if candidate["id"] not in original_ids]
    assert run["status"] == "completed", run["error"]
    assert run["termination_reason"] == "no_remaining_work"
    assert {candidate["mapping"]["edge_type"] for candidate in new} == {"PLACED", "CONTAINS", "SOLD_AT"}
    assert all(candidate["kind"] == "edge_mapping" and candidate["discovery_scope"] == "edges" for candidate in new)
    if not approve:
        assert all(any("pending SME review" in limitation for limitation in candidate["limitations"]) for candidate in new)


def test_given_no_endpoints_when_edges_only_runs_then_reports_blocker_without_discovering_nodes(settings, store):
    runner = DiscoveryRunner(settings, store)

    run = runner.execute(runner.create_run("retail", "heuristic", discovery_scope="edges")["id"])

    assert run["termination_reason"] == "missing_endpoint_mappings"
    assert run["usage"]["queries"] == 0
    assert store.list_candidates() == []
    assert store.load_checkpoint(run["id"])["memory"]["blocked_edges"]["PLACED"] == ["Customer", "Order"]


def test_given_edges_only_checkpoint_when_resuming_then_scope_is_retained(settings, store):
    _nodes(settings, store)
    runner = DiscoveryRunner(settings, store)
    first = runner.execute(runner.create_run(
        "retail", "heuristic", {"max_iterations": 2}, discovery_scope="edges",
    )["id"])

    resumed = runner.create_run("retail", "heuristic", {"max_iterations": 20}, resume_from=first["id"])
    result = runner.execute(resumed["id"])

    assert result["discovery_scope"] == "edges"
    assert result["termination_reason"] == "no_remaining_work"
    with pytest.raises(ValueError, match="change the discovery scope"):
        runner.create_run("retail", "heuristic", resume_from=first["id"], discovery_scope="all")


def test_given_edges_only_validation_when_running_then_only_relationship_findings_are_recorded(settings, store):
    _nodes(settings, store)
    runner = DiscoveryRunner(settings, store)

    run = runner.execute(runner.create_run(
        "retail", "heuristic", {"max_iterations": 20}, discovery_scope="edges", validate_topology=True,
    )["id"])

    assert run["topology_findings"]
    assert all(finding["element"].startswith("edge:") for finding in run["topology_findings"])
    assert all(target.startswith("edge:") for target in run["validation_targets"])


@pytest.mark.parametrize("kind", ["node_mapping", "annotation", "observation"])
def test_given_edges_only_tools_when_non_edge_is_proposed_then_backend_rejects(settings, store, kind):
    adapter = create_adapter(settings, settings.source("retail"))
    try:
        tools = DiscoveryTools(
            settings, store, adapter, load_topology(settings.topology_file),
            RunControl(settings.limits, settings.model, lambda: False), "run-edges", discovery_scope="edges",
        )

        result = tools.propose_candidate(kind, "Invalid scope", "test", [])

        assert "only edge_mapping" in result["error"]
        assert store.list_candidates() == []
    finally:
        adapter.close()


def test_given_profile_planner_when_edges_only_requested_then_rejects(settings, store):
    with pytest.raises(ValueError, match="not profile"):
        DiscoveryRunner(settings, store).create_run("retail", "profile", discovery_scope="edges")


def test_given_deleted_endpoint_when_discovering_edges_then_never_reuses_revoked_approval(settings, store):
    original_ids = _nodes(settings, store, approve=True)
    customer = next(fact for fact in store.list_facts() if fact["mapping"]["node_type"] == "Customer")
    store.delete_fact(customer["id"], reviewer="sme", notes="Incorrect identity",
                      expected_token=short_hash(customer), confirm=customer["id"])
    runner = DiscoveryRunner(settings, store)

    run = runner.execute(runner.create_run(
        "retail", "heuristic", {"max_iterations": 20}, discovery_scope="edges",
    )["id"])

    new = [candidate for candidate in store.list_candidates() if candidate["id"] not in original_ids]
    assert run["termination_reason"] == "missing_endpoint_mappings"
    assert {candidate["mapping"]["edge_type"] for candidate in new} == {"CONTAINS", "SOLD_AT"}


def test_given_cli_edges_only_option_when_running_then_scope_and_blockers_are_visible(project, monkeypatch, capsys):
    monkeypatch.chdir(project)

    result = main(["run", "--planner", "heuristic", "--discovery-scope", "edges"])

    output = capsys.readouterr().out
    assert result == 0
    assert "Discovery scope: edges" in output
    assert "missing_endpoint_mappings" in output


def test_given_missing_endpoints_when_foundry_edges_only_runs_then_no_model_calls_are_made(settings, store):
    settings.model.project_endpoint = "https://example.services.ai.azure.com/api/projects/demo"
    settings.model.model = "not-called"
    runner = DiscoveryRunner(settings, store)

    run = runner.execute(runner.create_run("retail", "foundry", discovery_scope="edges")["id"])

    assert run["termination_reason"] == "missing_endpoint_mappings", run["error"]
    assert run["usage"]["model_calls"] == 0


def test_given_missing_endpoint_when_edge_proposed_then_tool_requires_existing_mapping(settings, store):
    adapter = create_adapter(settings, settings.source("retail"))
    try:
        tools = DiscoveryTools(
            settings, store, adapter, load_topology(settings.topology_file),
            RunControl(settings.limits, settings.model, lambda: False), "run-edges", discovery_scope="edges",
        )
        evidence = tools.describe_table("tbl_ord_hdr")

        result = tools.propose_candidate(
            "edge_mapping", "Customer places Order", "Test endpoint guard", [evidence["evidence_id"]],
            {"edge_type": "PLACED", "source_table": "tbl_ord_hdr",
             "from_key_columns": ["cust_ref"], "to_key_columns": ["order_no"]},
        )

        assert "Missing node mapping for endpoint" in result["error"]
        assert store.list_candidates() == []
    finally:
        adapter.close()
