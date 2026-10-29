import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ddg.adapters import create_adapter
from ddg.agent.foundry_planner import _coverage_checklist, build_agent_tools
from ddg.agent.base import IterationContext
from ddg.agent.runner import DiscoveryRunner
from ddg.api import create_app
from ddg.cli import main
from ddg.confidence import assess, ensure_confidence
from ddg.config import load_settings
from ddg.control import RunControl
from ddg.store import ConflictError, Store
from ddg.tools import DiscoveryTools
from ddg.topology import load_topology


def _tools(settings, store, mode="conservative", independent=False, source="retail"):
    control = RunControl(settings.limits, settings.model, cancel_requested=lambda: False)
    adapter = create_adapter(settings, settings.source(source))
    return DiscoveryTools(settings, store, adapter, load_topology(settings.topology_file), control, "run-t",
                          mode=mode, independent_samples=independent)


def _run(settings, store, mode=None, source="retail", **limits):
    runner = DiscoveryRunner(settings, store)
    return runner.execute(runner.create_run(source, "heuristic", {"max_iterations": 30, **limits}, mode=mode)["id"])


# ---------- tier rules ----------

def _ev(tool, result, scope="full", **args):
    return {"tool": tool, "result": result, "scope": scope, "args": args}


def test_node_tiers():
    m = {"source_table": "T", "key_columns": ["id"]}
    full = _ev("check_uniqueness", {"table": "T", "columns": ["id"], "unique": True, "distinct_keys": 10})
    assert assess("node_mapping", m, [full])["tier"] == "high"
    sample = {**full, "scope": "sample"}
    assert assess("node_mapping", m, [sample])["tier"] == "medium"
    dup = _ev("check_uniqueness", {"table": "T", "columns": ["id"], "unique": False, "distinct_keys": 1000, "duplicate_keys": 3,
                                   "rows_with_null_key": 0})
    assert assess("node_mapping", m, [dup])["tier"] == "medium"
    bad = _ev("check_uniqueness", {"table": "T", "columns": ["id"], "unique": False, "distinct_keys": 10, "duplicate_keys": 5})
    assert assess("node_mapping", m, [bad])["tier"] == "low"
    assert assess("node_mapping", m, [])["reasons"] == ["No uniqueness check on the proposed key"]


def test_edge_tiers_compatibility_cardinality_and_stated_cap():
    join = lambda cov, card="many-to-one", scope="full": _ev(  # noqa: E731
        "check_join", {"row_coverage": cov, "observed_cardinality_left_to_right": card, "left": {"columns": ["ref"]}}, scope)
    edge = {"from_key_columns": ["id"], "to_key_columns": ["ref"]}  # referencing column holds the to-node key
    assert assess("edge_mapping", edge, [join(0.95)])["tier"] == "high"
    assert assess("edge_mapping", edge, [join(0.95, scope="sample")])["tier"] == "medium"
    assert assess("edge_mapping", edge, [join(0.6)])["tier"] == "medium"
    low = assess("edge_mapping", edge, [join(0.0), _ev("check_compatibility", {"verdict": "compatible"}, "sample")],
                 independent_samples=True)
    assert low["tier"] == "low" and any("independent samples" in r for r in low["reasons"])
    conflict = assess("edge_mapping", edge, [join(0.95, "many-to-many")], declared_cardinality="one-to-many")
    assert conflict["tier"] == "medium" and any("conflicts" in r for r in conflict["reasons"])
    # Referencing column holds the from-node key: many-to-one reads as one-to-many in edge direction (no conflict).
    reversed_edge = {"from_key_columns": ["ref"], "to_key_columns": ["id"]}
    assert assess("edge_mapping", reversed_edge, [join(0.95)], declared_cardinality="one-to-many")["tier"] == "high"
    lowered = assess("edge_mapping", edge, [join(0.95)], stated_tier="low")
    assert (lowered["tier"], lowered["evidence_tier"]) == ("low", "high")
    not_raised = assess("edge_mapping", edge, [join(0.1)], stated_tier="high")
    assert not_raised["tier"] == "low"


def test_ensure_confidence_backfills_legacy_candidates():
    legacy = {"kind": "observation", "mapping": None, "evidence": []}
    out = ensure_confidence(legacy)
    assert out["confidence_tier"] == "low" and "after the fact" in out["confidence_reasons"][-1]
    assert ensure_confidence({**legacy, "confidence_tier": "high"})["confidence_tier"] == "high"


# ---------- proposals, modes, and duplicates ----------

def test_conservative_refuses_low_and_exploratory_accepts(settings, store):
    tools = _tools(settings, store)
    profile = tools.profile_column("prod_catalog", "category")
    note = tools.propose_candidate("observation", "category may drive pricing", "hunch", [profile["evidence_id"]])
    assert "not accepted in conservative mode" in note["error"]

    explore = _tools(settings, store, mode="exploratory")
    explore.evidence_index.update(tools.evidence_index)
    ok = explore.propose_candidate("observation", "category may drive pricing", "hunch", [profile["evidence_id"]], confidence=0.99)
    assert ok["confidence_tier"] == "low"
    stored = store.get_candidate(ok["candidate_id"])
    assert stored["confidence"] <= 0.45 and stored["discovery_mode"] == "exploratory" and stored["confidence_reasons"]
    for t in (tools, explore):
        t.adapter.close()


def test_heuristic_candidates_get_tiers(settings, store):
    _run(settings, store)
    tiers = {(c["mapping"].get("node_type") or c["mapping"]["edge_type"]): c["confidence_tier"] for c in store.list_candidates()}
    assert tiers["Customer"] == "high" and tiers["PLACED"] == "high" and tiers["CONTAINS"] == "high"


def test_same_mapping_on_another_source_is_not_a_duplicate(project, settings, store):
    path = settings.workspace_dir / "workspace.toml"
    path.write_text(path.read_text(encoding="utf-8") + '\n[sources.retail_v2]\ntype = "sqlite"\npath = "data/retail.sqlite"\n',
                    encoding="utf-8")
    settings = load_settings(project, "retail")
    store = Store(settings)
    _run(settings, store)
    for c in store.list_candidates("pending"):
        store.decide(c["id"], "approve", "sme", "", "", c["version"])
    second = _run(settings, store, source="retail_v2")
    v2 = [c for c in store.list_candidates() if c["scope"]["source"] == "retail_v2"]
    assert len(v2) == 7, second["iterations"][-1].get("proposal_errors")
    assert not [e for i in second["iterations"] for e in i["proposal_errors"] if "Duplicate" in e]

    # Same source still deduplicates, including records whose stored fingerprint predates source-awareness.
    legacy = store.list_candidates()[0]
    legacy["fingerprint"] = "legacy"
    from ddg.store import atomic_write_json

    atomic_write_json(settings.candidates_dir / f"{legacy['id']}.json", legacy)
    tools = _tools(settings, store)
    tools.evidence_index.update({e["id"]: {**e, "source": "retail", "iteration": 0} for e in legacy["evidence"]})
    dup = tools.propose_candidate(legacy["kind"], legacy["hypothesis"], "again", [e["id"] for e in legacy["evidence"]],
                                  dict(legacy["mapping"]))
    assert "Duplicate" in dup["error"]
    tools.adapter.close()


# ---------- compatibility, memory, and independent samples ----------

def test_check_compatibility_uses_transforms_and_hides_values(settings, store):
    tools = _tools(settings, store)
    raw = tools.check_compatibility("tbl_ord_hdr", "cust_ref", "CUST_MASTER", "cust_id")
    assert raw["verdict"] == "incompatible" and raw["left"]["top_shapes"][0]["shape"] == "A-99999"
    fixed = tools.check_compatibility("tbl_ord_hdr", "cust_ref", "CUST_MASTER", "cust_id", ["digits_only", "int"], ["int"])
    assert fixed["verdict"] == "compatible" and fixed["scope"] == "sample" and "cust" in fixed["name_signal"]
    text = str(fixed)
    assert "C-0" not in text and "values" not in fixed["left"]
    assert "compatibility tbl_ord_hdr.cust_ref -> CUST_MASTER.cust_id: compatible" in tools.exploration_ledger()
    tools.adapter.close()


def test_describe_is_cached_within_a_run_and_ledger_lists_checks(settings, store):
    tools = _tools(settings, store)
    first = tools.describe_table("CUST_MASTER")
    queries = tools.adapter.query_count
    second = tools.describe_table("cust_master")
    assert second["cached"] and second["evidence_id"] == first["evidence_id"] and tools.adapter.query_count == queries
    tools.check_join("tbl_ord_hdr", ["cust_ref"], "CUST_MASTER", ["cust_id"], [["digits_only", "int"]], [["int"]])
    ledger = tools.exploration_ledger()
    assert "join tbl_ord_hdr['cust_ref']" in ledger and "rows 98.1%" in ledger and "describe CUST_MASTER: 200 rows" in ledger
    assert "CUST_MASTER: cust_id, full_nm" in tools.schema_digest()
    tools.adapter.close()


def _independent_sample_workspace(project):
    ws = project / "workspaces" / "indep"
    (ws / "data").mkdir(parents=True)
    (ws / "workspace.toml").write_text(
        '[sources.s]\ntype = "sqlite"\npath = "data/s.sqlite"\nindependent_samples = true\n\n[sampling]\nallowlist = []\n',
        encoding="utf-8",
    )
    shutil.copyfile(project / "workspaces" / "retail" / "topology.md", ws / "topology.md")
    conn = sqlite3.connect(ws / "data" / "s.sqlite")
    conn.execute("CREATE TABLE customers (cust_id INTEGER, full_name TEXT)")
    conn.execute("CREATE TABLE orders (order_no TEXT, cust_ref TEXT, total REAL)")
    conn.executemany("INSERT INTO customers VALUES (?, ?)", [(i, f"n{i}") for i in range(1, 101)])
    # Independently sampled: orders reference customers that are not in the customer sample.
    conn.executemany("INSERT INTO orders VALUES (?, ?, ?)", [(f"ORD-{i:05d}", f"C-{500 + i % 150:05d}", 1.0) for i in range(300)])
    conn.commit()
    conn.close()
    return load_settings(project, "indep")


def test_exploratory_heuristic_proposes_low_tier_edges_on_independent_samples(project):
    settings = _independent_sample_workspace(project)
    store = Store(settings)
    conservative = _run(settings, store, mode="conservative", source="s")
    edges = [c for c in store.list_candidates() if c["kind"] == "edge_mapping"]
    assert conservative["status"] == "completed" and edges == []

    _run(settings, store, mode="exploratory", source="s")
    edges = [c for c in store.list_candidates() if c["kind"] == "edge_mapping"]
    placed = next(c for c in edges if c["mapping"]["edge_type"] == "PLACED")
    assert placed["confidence_tier"] == "low" and placed["discovery_mode"] == "exploratory"
    assert placed["mapping"]["from_key_columns"] == ["cust_ref"] and placed["mapping"]["from_key_transforms"] == [["digits_only", "int"]]
    assert any("independent samples" in r for r in placed["confidence_reasons"])
    assert any(e["tool"] == "check_compatibility" for e in placed["evidence"])


def test_foundry_prompt_helpers(settings, store):
    tools = _tools(settings, store, mode="exploratory")
    assert "check_compatibility" in {f.__name__ for f in build_agent_tools(tools)}
    _run(settings, store, max_iterations=3)
    ctx = IterationContext(1, "retail", load_topology(settings.topology_file), store.review_snapshot())
    checklist = _coverage_checklist(ctx)
    assert "- [x] Node Customer: pending, high via CUST_MASTER" in checklist
    assert "- [ ] Edge PLACED: nothing proposed yet" in checklist
    tools.adapter.close()


# ---------- reset ----------

def test_reset_archives_selected_scopes(settings, store):
    _run(settings, store)
    pending = store.list_candidates("pending")
    store.decide(pending[0]["id"], "approve", "sme", "", "", 1)
    store.decide(pending[1]["id"], "reject", "sme", "wrong", "", 1)
    before = store.reset_preview()
    assert before == {"runs": 1, "pending": 5, "rejected": 1, "approved": 1, "metadata": before["metadata"]} and before["metadata"] >= 1

    with pytest.raises(ValueError):
        store.reset([], "ana")
    with pytest.raises(ValueError):
        store.reset(["everything"], "ana")
    manifest = store.reset(["runs", "pending", "metadata"], "ana")
    assert manifest["moved"]["runs"] == 1 and manifest["moved"]["pending"] == 5
    assert store.list_runs() == [] and store.list_candidates("pending") == []
    assert len(store.list_candidates("rejected")) == 1 and len(store.list_facts()) == 1
    from pathlib import Path

    archive = Path(manifest["path"])
    assert (archive / "reset.json").exists() and any((archive / "runs").iterdir())

    rerun = _run(settings, store)
    assert rerun["status"] == "completed"
    # Approved and rejected interpretations survive the reset and are still respected (no re-proposals).
    from ddg.store import candidate_fingerprint

    kept = {candidate_fingerprint(c) for c in store.list_candidates() if c["status"] in ("approved", "rejected")}
    fresh = store.list_candidates("pending")
    assert fresh and not {candidate_fingerprint(c) for c in fresh} & kept

    manifest = store.reset(["approved", "rejected"], "ana")
    assert manifest["moved"] == {"approved": 1, "facts": 1, "rejected": 1}
    assert store.list_facts() == []


def test_reset_refuses_while_runs_are_active(settings, store):
    runner = DiscoveryRunner(settings, store)
    run = runner.create_run("retail", "heuristic")
    with pytest.raises(ConflictError, match="Cancel active runs"):
        store.reset(["runs"], "ana")
    run["status"] = "completed"
    store.save_run(run)
    assert store.reset(["runs"], "ana")["moved"] == {"runs": 1}


def test_reset_api_and_tier_filter(project):
    client = TestClient(create_app(project))
    run = client.post("/api/w/retail/runs", json={"source": "retail", "planner": "heuristic", "mode": "exploratory",
                                                    "limits": {"max_iterations": 30}}).json()
    assert run["mode"] == "exploratory"
    import time

    for _ in range(300):
        if client.get(f"/api/w/retail/runs/{run['id']}").json()["status"] not in ("queued", "running", "stopping"):
            break
        time.sleep(0.1)
    assert client.post("/api/w/retail/runs", json={"source": "retail", "mode": "wild"}).status_code == 422
    high = client.get("/api/w/retail/candidates", params={"tier": "high"}).json()
    assert high and all(c["confidence_tier"] == "high" for c in high)
    assert client.get("/api/w/retail/runs").json()[0]["mode"] == "exploratory"

    preview = client.get("/api/w/retail/reset").json()
    assert preview["counts"]["pending"] == 7 and "approved" in preview["scopes"]
    bad = client.post("/api/w/retail/reset", json={"scopes": ["runs"], "actor": "ana", "confirm": "nope"})
    assert bad.status_code == 400
    ok = client.post("/api/w/retail/reset", json={"scopes": ["runs", "pending"], "actor": "ana", "confirm": "retail"})
    assert ok.status_code == 200 and ok.json()["moved"] == {"runs": 1, "pending": 7}
    assert client.get("/api/w/retail/candidates").json() == []


def test_cli_reset_and_candidates_tier(project, settings, store, monkeypatch, capsys):
    _run(settings, store)
    monkeypatch.chdir(project)
    assert main(["candidates", "--tier", "high"]) == 0
    out = capsys.readouterr().out
    assert "high" in out and "pending" in out
    assert main(["reset"]) == 2
    assert main(["reset", "--fresh", "--yes", "--actor", "ana"]) == 0
    assert "Archived" in capsys.readouterr().out
    assert Store(settings).list_candidates() == [] and Store(settings).list_runs() == []
