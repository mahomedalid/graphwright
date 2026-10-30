"""Topology validation remains advisory, evidence-backed, and resumable."""

import pytest

from ddg.adapters import create_adapter
from ddg.agent.foundry_planner import build_agent_tools
from ddg.agent.runner import DiscoveryRunner
from ddg.cli import main
from ddg.control import RunControl
from ddg.tools import DiscoveryTools
from ddg.topology import load_topology


def _validate(settings, store, **limits):
    runner = DiscoveryRunner(settings, store)
    run = runner.create_run(
        "retail", "heuristic", {"max_iterations": 20, **limits}, validate_topology=True,
    )
    return runner.execute(run["id"])


def test_given_validation_when_running_then_findings_are_persisted_without_editing_topology(settings, store):
    before = settings.topology_file.read_bytes()

    run = _validate(settings, store)

    assert run["status"] == "completed", run["error"]
    assert run["termination_reason"] == "no_remaining_work"
    assert settings.topology_file.read_bytes() == before
    assert store.list_facts() == []
    assert run["topology_findings"] == store.get_run(run["id"])["topology_findings"]
    elements = {finding["element"] for finding in run["topology_findings"]}
    assert {"node:Customer", "node:Order", "node:Product", "node:Store", "edge:PLACED", "edge:CONTAINS", "edge:SOLD_AT"} <= elements
    assert any(target.startswith("constraint:") for target in run["validation_targets"])
    assert not any(f["status"] == "mismatch" for f in run["topology_findings"])
    for finding in run["topology_findings"]:
        assert finding["evidence_ids"]
        assert finding["topology_version"] == run["topology_version"]
        for evidence in finding["evidence"]:
            assert store.get_evidence(evidence["run_id"], evidence["id"]) == evidence


def test_given_wrong_cardinality_when_validating_then_recommends_change(settings, store):
    text = settings.topology_file.read_text(encoding="utf-8")
    settings.topology_file.write_text(text.replace("- Cardinality: one-to-many", "- Cardinality: one-to-one"), encoding="utf-8")

    run = _validate(settings, store)

    finding = next(f for f in run["topology_findings"] if f["element"] == "edge:PLACED")
    assert finding["status"] == "mismatch"
    assert "one-to-many" in finding["recommendation"]


def test_given_missing_property_when_validating_then_does_not_claim_topology_is_wrong(settings, store):
    text = settings.topology_file.read_text(encoding="utf-8")
    settings.topology_file.write_text(text.replace("name, email, city", "name, email, city, nonexistent_property_xyz"), encoding="utf-8")

    run = _validate(settings, store)

    finding = next(f for f in run["topology_findings"] if f["element"] == "node:Customer" and f["aspect"] == "properties")
    assert finding["status"] == "inconclusive"
    assert "nonexistent_property_xyz" in finding["finding"]
    assert finding["recommendation"]


def test_given_existing_candidates_when_validating_then_checks_covered_elements(settings, store):
    runner = DiscoveryRunner(settings, store)
    runner.execute(runner.create_run("retail", "heuristic", {"max_iterations": 20})["id"])

    run = _validate(settings, store)

    assert {f["element"] for f in run["topology_findings"]} >= {"node:Customer", "edge:PLACED"}


def test_given_validation_checkpoint_when_resuming_then_keeps_objective_and_findings(settings, store):
    runner = DiscoveryRunner(settings, store)
    first = _validate(settings, store, max_iterations=2)

    resumed = runner.create_run("retail", "heuristic", {"max_iterations": 20}, resume_from=first["id"])
    result = runner.execute(resumed["id"])

    assert result["validate_topology"] is True
    assert result["termination_reason"] == "no_remaining_work"
    assert all(f in result["topology_findings"] for f in first["topology_findings"])
    with pytest.raises(ValueError, match="Start a new run"):
        runner.create_run("retail", "heuristic", resume_from=first["id"], validate_topology=False)


def test_given_profile_planner_when_validation_requested_then_rejects(settings, store):
    with pytest.raises(ValueError, match="not profile"):
        DiscoveryRunner(settings, store).create_run("retail", "profile", validate_topology=True)


def test_given_default_discovery_when_running_then_no_topology_findings(settings, store):
    runner = DiscoveryRunner(settings, store)

    run = runner.execute(runner.create_run("retail", "heuristic")["id"])

    assert run["validate_topology"] is False
    assert run["topology_findings"] == []


def test_given_validation_tool_when_citing_unknown_evidence_then_rejects(settings, store):
    control = RunControl(settings.limits, settings.model, cancel_requested=lambda: False)
    adapter = create_adapter(settings, settings.source("retail"))
    try:
        tools = DiscoveryTools(
            settings, store, adapter, load_topology(settings.topology_file), control,
            "run-validation", validate_topology=True,
        )
        wrappers = {function.__name__: function for function in build_agent_tools(tools)}

        result = tools.record_topology_finding(
            "node:Customer", "key", "mismatch", "Invalid key", "Use a composite key", ["ev-unknown"],
        )

        assert "Unknown evidence" in result["error"]
        assert tools.topology_findings == []
        assert "record_topology_finding" in wrappers
        evidence = tools.describe_table("CUST_MASTER")
        result = tools.record_topology_finding(
            "node:Customer", "properties", "inconclusive", "Missing property", "Clarify the property",
            [evidence["evidence_id"]],
        )
        assert result["recorded"]
        assert store.list_candidates() == []
    finally:
        adapter.close()


def test_given_declared_nonunique_key_when_validating_then_recommends_key_review(settings, store):
    settings.topology_file.write_text(
        "# Graph Topology: Key validation\n\n## Node Types\n### Customer\n"
        "- Key: city\n- Properties: name\n", encoding="utf-8",
    )

    run = _validate(settings, store)

    finding = next(f for f in run["topology_findings"] if f["aspect"] == "key")
    assert finding["status"] == "mismatch"
    assert "composite or alternative key" in finding["recommendation"]


def test_given_bounded_key_check_when_validating_then_reports_inconclusive(settings, store):
    settings.limits.max_join_keys = 1
    settings.topology_file.write_text(
        "# Graph Topology: Sample validation\n\n## Node Types\n### Customer\n"
        "- Key: cust no\n", encoding="utf-8",
    )

    run = _validate(settings, store)

    finding = next(f for f in run["topology_findings"] if f["aspect"] == "key")
    assert finding["status"] == "inconclusive"
    assert any("bounded samples" in caveat for caveat in finding["limitations"])


def test_given_changed_topology_when_resuming_then_drops_old_findings(settings, store):
    first = _validate(settings, store, max_iterations=2)
    with settings.topology_file.open("a", encoding="utf-8") as handle:
        handle.write("\nNew topology context.\n")
    runner = DiscoveryRunner(settings, store)

    result = runner.execute(runner.create_run(
        "retail", "heuristic", {"max_iterations": 20}, resume_from=first["id"],
    )["id"])

    assert result["topology_findings"]
    assert all(f["topology_version"] == result["topology_version"] for f in result["topology_findings"])
    assert result["topology_version"] != first["topology_version"]


def test_given_cli_validation_option_when_running_then_prints_findings(project, monkeypatch, capsys):
    monkeypatch.chdir(project)

    result = main(["run", "--planner", "heuristic", "--validate-topology", "--max-iterations", "2"])

    assert result == 0
    assert "node:Customer" in capsys.readouterr().out
