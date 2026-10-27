from ddg.agent.runner import DiscoveryRunner


def _run(settings, store, **limits):
    runner = DiscoveryRunner(settings, store)
    run = runner.create_run("retail", "heuristic", limits or None)
    return runner, runner.execute(run["id"])


def _proposed(store):
    found = {}
    for c in store.list_candidates():
        m = c.get("mapping") or {}
        found[(c["kind"], m.get("node_type") or m.get("edge_type"))] = c
    return found


def test_heuristic_discovers_ground_truth_and_avoids_decoys(settings, store, expected):
    _, run = _run(settings, store)
    assert run["status"] == "completed" and run["termination_reason"] == "no_remaining_work"
    assert len(run["iterations"]) > 2  # evidence-driven steps without operator prompts
    found = _proposed(store)
    for node, exp in expected["nodes"].items():
        m = found[("node_mapping", node)]["mapping"]
        assert (m["source_table"], m["key_columns"]) == (exp["source_table"], exp["key_columns"])
    for edge, exp in expected["edges"].items():
        m = found[("edge_mapping", edge)]["mapping"]
        assert (m["source_table"], m["from_key_columns"], m["to_key_columns"]) == (
            exp["source_table"], exp["from_key_columns"], exp["to_key_columns"])
    used_tables = {c["mapping"]["source_table"] for c in store.list_candidates() if c.get("mapping")}
    assert not used_tables & set(expected["decoys"])
    contains = found[("edge_mapping", "CONTAINS")]["mapping"]
    assert contains["properties"] == {"quantity": "qty", "unit price": "unit_prc"}


def test_candidates_carry_traceable_evidence(settings, store):
    _run(settings, store)
    for c in store.list_candidates():
        assert c["evidence"] and c["topology_version"] and c["run_id"] and c["source_refs"]
        for ev in c["evidence"]:
            assert store.get_evidence(c["run_id"], ev["id"])["result"] == ev["result"]
    inventory = settings.metadata_dir / "retail" / "inventory.json"
    assert inventory.exists()


def test_rejected_interpretation_is_not_resubmitted(settings, store):
    _run(settings, store)
    customer = _proposed(store)[("node_mapping", "Customer")]
    store.decide(customer["id"], "reject", "sme", "CUST_MASTER is the CRM export, not the system of record", "", customer["version"])

    _, second = _run(settings, store)
    customers = [c for c in store.list_candidates() if (c.get("mapping") or {}).get("node_type") == "Customer"]
    fingerprints = [c["fingerprint"] for c in customers]
    assert len(fingerprints) == len(set(fingerprints))
    new = [c for c in customers if c["status"] == "pending"]
    assert new and new[0]["mapping"]["source_table"] != "CUST_MASTER"
    assert any("backup" in lim.lower() for lim in new[0]["limitations"])
    blocked = [err for it in second["iterations"] for err in it["proposal_errors"]]
    assert any("CRM export" in err for err in blocked)


def test_iteration_limit_and_checkpoint_resume(settings, store):
    runner, first = _run(settings, store, max_iterations=2)
    assert first["termination_reason"] == "iteration_limit"
    assert store.load_checkpoint(first["id"])["iteration"] == 2
    resumed = runner.create_run("retail", "heuristic", resume_from=first["id"])
    result = runner.execute(resumed["id"])
    assert result["resumed_from"] == first["id"]
    assert result["iterations"][0]["iteration"] == 3
    assert result["termination_reason"] == "no_remaining_work"


def test_cancellation_stops_before_tool_dispatch(settings, store):
    runner = DiscoveryRunner(settings, store)
    run = runner.create_run("retail", "heuristic")
    store.request_cancel(run["id"])
    result = runner.execute(run["id"])
    assert result["status"] == "cancelled"
    assert result["usage"]["tool_calls"] == 0 and result["usage"]["queries"] == 0


def test_multi_pass_inventory_is_progress_not_a_stall(settings, store, monkeypatch):
    from ddg.agent import heuristic

    monkeypatch.setattr(heuristic, "MAX_DESCRIBE_PER_ITERATION", 1)
    _, run = _run(settings, store, stall_iterations=2)
    assert run["termination_reason"] == "no_remaining_work"
    assert len(store.list_candidates()) == 7


def test_time_budget(settings, store):
    _, run = _run(settings, store, max_minutes=0.0001)
    assert run["termination_reason"] == "time_limit"


def test_invalid_topology_fails_before_discovery(settings, store):
    settings.topology_file.write_text("## Edge Types\n### X\n- From: A\n- To: B\n", encoding="utf-8")
    _, run = _run(settings, store)
    assert run["status"] == "failed" and run["termination_reason"] == "invalid_topology"
    assert "No node types defined" in run["error"]
    assert run["usage"]["queries"] == 0
