from fastapi.testclient import TestClient

from ddg.agent.runner import DiscoveryRunner
from ddg.api import create_app
from ddg.narrative import (
    clean_story,
    facts_story,
    fallback_story,
    iteration_activity,
    run_story,
    short_table,
    split_model_reply,
)


def test_split_model_reply_and_cleaning():
    story, details = split_model_reply(
        "STORY: I looked at **billing** tables (ev-0123abcd) and found `BAN` is a solid key.\n\nDETAILS:\n## Notes\n- ev-0123abcd unique"
    )
    assert story == "I looked at billing tables and found BAN is a solid key."
    assert details.startswith("## Notes")
    assert split_model_reply("Just a long essay without the format.") == (None, "Just a long essay without the format.")
    assert "edl_prod__ela_x" in clean_story("table edl_prod__ela_x and __bold__ text")
    assert "bold text" in clean_story("table edl_prod__ela_x and __bold__ text")
    long = clean_story("word " * 400)
    assert len(long) <= 701


def test_facts_story_from_activity():
    evidence = [
        {"tool": "describe_table", "args": {"table": "edl_prod_vw_rawstd__ela_crm_table_contact"}},
        {"tool": "check_join", "args": {"left_table": "orders", "right_table": "customers"}},
        {"tool": "check_join", "args": {"left_table": "orders", "right_table": "stores"}},
        {"tool": "check_uniqueness", "args": {"table": "orders"}},
    ]
    proposed = [{"id": "cand-1", "kind": "node_mapping", "confidence_tier": "high",
                 "mapping": {"node_type": "Person", "source_table": "edl_prod_vw_rawstd__ela_crm_table_contact"}}]
    refusals = ["Low-confidence proposal not accepted in conservative mode (x)", "Duplicate of pending candidate cand-9"]
    activity = iteration_activity(evidence, proposed, refusals)
    story = facts_story(activity)
    assert story.startswith("Looked at ela_crm_table_contact, orders, customers and 1 more, running 2 relationship checks and 1 key check.")
    assert "Proposed Person from ela_crm_table_contact (high confidence) for review." in story
    assert "1 idea was held back because the evidence was too weak for conservative mode." in story
    assert "1 idea was held back as already proposed." in story
    assert facts_story({}) .startswith("Reviewed what is already known")
    assert short_table("edl_prod_vw_rawstd__slam_address_serviceability") == "slam_address_serviceability"


def test_fallback_story_uses_opening_paragraph_and_list_heads():
    summary = (
        "Two concrete hypotheses explored in this iteration:\n"
        "1. **V21 billing accounts as Account nodes** - Evidence: lots of detail\n"
        "   - nested evidence ev-1234abcd\n"
        "2. **CRM financial accounts** - more detail\n\n## Next\n- something"
    )
    assert fallback_story(summary) == (
        "Two concrete hypotheses explored in this iteration: V21 billing accounts as Account nodes; CRM financial accounts."
    )
    assert fallback_story("First sentence here. Second one. Third one.\n\n- list") == "First sentence here. Second one."


def test_runs_record_stories_and_activity(settings, store):
    runner = DiscoveryRunner(settings, store)
    run = runner.execute(runner.create_run("retail", "heuristic", {"max_iterations": 30})["id"])
    first, second = run["iterations"][0], run["iterations"][1]
    assert first["story"].startswith("Looked at ") and "8 tables" not in first["story"]
    assert first["activity"]["tables"] and not first["activity"]["proposed"]
    assert second["activity"]["proposed"][0]["label"] == "Customer from CUST_MASTER"
    assert "Proposed Customer from CUST_MASTER (high confidence) for review." in second["story"]
    assert run["story"].startswith("The offline planner worked on source 'retail' in conservative mode for")
    assert "It proposed 7 candidates for review (7 high confidence)." in run["story"]
    assert run["story"].endswith("It stopped because it had nothing left to investigate.")
    assert run_story({"planner": "profile", "source": "s", "iterations": [{}], "termination_reason": "stalled"}) == (
        "The profiler worked on source 's' for 1 step. It collected column statistics for the data dictionary. "
        "It stopped because it stopped finding anything new."
    )


def test_api_backfills_stories_for_old_runs(project, settings, store):
    runner = DiscoveryRunner(settings, store)
    run = runner.execute(runner.create_run("retail", "heuristic", {"max_iterations": 30})["id"])
    # Simulate a run recorded before stories existed.
    for it in run["iterations"]:
        it.pop("story", None)
        it.pop("activity", None)
        it.pop("facts", None)
    run["iterations"][1]["summary"] = "Proposed a Customer mapping.\n\n- details ev-1234abcd"
    run.pop("story")
    store.save_run(run)
    client = TestClient(create_app(project))
    data = client.get(f"/api/w/retail/runs/{run['id']}").json()
    assert data["iterations"][1]["story"] == "Proposed a Customer mapping."
    assert data["iterations"][1]["activity"]["proposed"][0]["tier"] == "high"
    assert "It proposed 7 candidates" in data["story"]
    stored = store.get_run(run["id"])
    assert "story" not in stored  # backfill is display-only
