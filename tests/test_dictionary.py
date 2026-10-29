import pytest
from fastapi.testclient import TestClient

from ddg.adapters import create_adapter
from ddg.agent.runner import DiscoveryRunner
from ddg.api import create_app
from ddg.cli import main
from ddg.control import RunControl
from ddg.dictionary import DictionaryError, DictionaryStore, build_dictionary, to_markdown
from ddg.store import ConflictError
from ddg.tools import DiscoveryTools
from ddg.topology import load_topology


def _run(settings, store, planner, **limits):
    runner = DiscoveryRunner(settings, store)
    return runner.execute(runner.create_run("retail", planner, {"max_iterations": 30, **limits})["id"])


def _table(dictionary, name):
    return next(t for t in dictionary["tables"] if t["name"] == name)


def _column(table, name):
    return next(c for c in table["columns"] if c["name"] == name)


def test_inventory_only_dictionary_lists_every_table(settings, store):
    _run(settings, store, "heuristic", max_iterations=1)
    d = build_dictionary(settings, store, "retail")
    assert d["summary"]["tables"] == 8 and d["summary"]["columns_described"] == 0
    cust = _table(d, "CUST_MASTER")
    assert cust["row_count"] == 200 and [c["name"] for c in cust["columns"]][:2] == ["cust_id", "full_nm"]
    assert cust["roles"] == [] and cust["description"] == ""


def test_profile_planner_fills_statistics_and_respects_privacy(settings, store):
    run = _run(settings, store, "profile")
    assert run["status"] == "completed" and run["termination_reason"] == "no_remaining_work"
    assert store.list_candidates() == []
    d = build_dictionary(settings, store, "retail")
    assert d["summary"]["columns_profiled"] == d["summary"]["columns"]
    email = _column(_table(d, "CUST_MASTER"), "email_addr")["profile"]
    assert email["distinct_count"] == 200 and email["null_pct"] == 0 and email["top_values"] is None
    category = _column(_table(d, "prod_catalog"), "category")["profile"]
    assert category["top_values"] and category["top_values"][0]["value"]  # allowlisted
    store_cd = _column(_table(d, "tbl_ord_hdr"), "store_cd")["profile"]
    assert store_cd["null_count"] > 0
    second = _run(settings, store, "profile")
    assert second["usage"]["queries"] < 20  # only inventory again; nothing left to profile


def test_profile_runs_without_a_valid_topology(settings, store):
    settings.topology_file.write_text("# Graph Topology: TBD\n\n## Node Types\n", encoding="utf-8")
    run = _run(settings, store, "profile")
    assert run["status"] == "completed", run.get("error")
    assert build_dictionary(settings, store, "retail")["summary"]["columns_profiled"] > 0


def test_facts_candidates_and_rejections_become_roles_and_history(settings, store):
    _run(settings, store, "heuristic")
    pending = {c["mapping"].get("node_type") or c["mapping"].get("edge_type"): c for c in store.list_candidates("pending")}
    for name in ("Customer", "Order", "PLACED"):
        store.decide(pending[name]["id"], "approve", "sme", "", "", 1)
    store.decide(pending["Store"]["id"], "reject", "sme", "stores_ref is a stale extract", "", 1)

    d = build_dictionary(settings, store, "retail")
    cust = _table(d, "CUST_MASTER")
    assert cust["roles"][0]["node_type"] == "Customer" and cust["roles"][0]["status"] == "approved"
    key = _column(cust, "cust_id")["roles"][0]
    assert key["kind"] == "node_key" and key["unique"] is True
    email = _column(cust, "email_addr")["roles"][0]
    assert email["label"] == "Customer.email"

    cust_ref = _column(_table(d, "tbl_ord_hdr"), "cust_ref")
    placed = next(r for r in cust_ref["roles"] if r["kind"] == "edge_key")
    assert placed["references"] == "CUST_MASTER.cust_id" and placed["row_coverage"] > 0.95 and placed["unmatched_keys"] > 0

    lines = _table(d, "ord_lines_v2")
    assert lines["roles"][0]["status"] == "pending"  # CONTAINS still pending
    stores = _table(d, "stores_ref")
    assert stores["roles"] == [] and stores["history"][0]["sme_notes"] == ["stores_ref is a stale extract"]
    assert d["summary"]["tables_mapped"] == 2


def test_sme_annotations_take_precedence_and_are_versioned(settings, store):
    _run(settings, store, "heuristic", max_iterations=1)
    ds = DictionaryStore(settings)
    ds.update("retail", "tbl_ord_hdr", None, {"description": "Order headers", "status": "verified"}, "ana", 0)
    ds.update("retail", "tbl_ord_hdr", "status_cd", {
        "description": "Order lifecycle state", "values": {"NEW": "Created", "SHIP": "Shipped", "DONE": "Delivered"},
        "notes": "DONE also covers returns (unverified)",
    }, "ana", 0)
    with pytest.raises(ConflictError):
        ds.update("retail", "tbl_ord_hdr", None, {"description": "x"}, "bob", 0)
    with pytest.raises(DictionaryError):
        ds.update("retail", "tbl_ord_hdr", None, {"status": "maybe"}, "bob", 1)
    with pytest.raises(DictionaryError):
        ds.update("retail", "tbl_ord_hdr", None, {"description": "x"}, " ", 1)

    d = build_dictionary(settings, store, "retail")
    t = _table(d, "tbl_ord_hdr")
    assert (t["description"], t["description_source"], t["status"], t["revision"]) == ("Order headers", "sme", "verified", 1)
    status = _column(t, "status_cd")
    assert status["values"]["SHIP"] == "Shipped" and status["status"] == "provisional" and status["updated_by"] == "ana"
    assert d["summary"]["tables_described"] == 1 and d["summary"]["columns_described"] == 1
    assert (settings.workspace_dir / "dictionary" / "history.jsonl").read_text(encoding="utf-8").count("\n") == 2

    md = to_markdown(d)
    assert "## tbl_ord_hdr" in md and "Order headers" in md and "* `SHIP`: Shipped" in md and "| status_cd |" in md


def test_agent_annotation_candidates_flow_into_dictionary(settings, store):
    control = RunControl(settings.limits, settings.model, cancel_requested=lambda: False)
    adapter = create_adapter(settings, settings.source("retail"))
    tools = DiscoveryTools(settings, store, adapter, load_topology(settings.topology_file), control, "run-x")
    profile = tools.profile_column("prod_catalog", "category")

    bad = tools.propose_candidate("annotation", "x", "y", [profile["evidence_id"]], {"table": "nope", "description": "d"})
    assert "Unknown table" in bad["error"]
    bad = tools.propose_candidate("annotation", "x", "y", [profile["evidence_id"]], {"table": "prod_catalog", "values": {"a": "b"}})
    assert "require a column" in bad["error"]
    bad = tools.propose_candidate("annotation", "x", "y", [], {"table": "prod_catalog", "description": "d"})
    assert "evidence" in bad["error"]

    ok = tools.propose_candidate(
        "annotation", "category is the merchandising department", "six stable values", [profile["evidence_id"]],
        {"table": "PROD_CATALOG", "column": "CATEGORY", "description": "Merchandising department", "values": {"Toys": "Toys & games"}},
    )
    d = build_dictionary(settings, store, "retail")
    col = _column(_table(d, "prod_catalog"), "category")
    assert col["suggestions"][0]["description"] == "Merchandising department" and col["description"] == ""

    store.decide(ok["candidate_id"], "approve", "sme", "", "", 1)
    d = build_dictionary(settings, store, "retail")
    col = _column(_table(d, "prod_catalog"), "category")
    assert col["description"] == "Merchandising department" and col["description_source"].startswith("fact ")
    assert col["status"] == "verified" and col["values"] == {"Toys": "Toys & games"} and col["suggestions"] == []

    DictionaryStore(settings).update("retail", "prod_catalog", "category", {"description": "Department"}, "ana", 0)
    col = _column(_table(build_dictionary(settings, store, "retail"), "prod_catalog"), "category")
    assert col["description"] == "Department" and col["values"] == {"Toys": "Toys & games"}  # SME wins; fact values kept

    known = tools.get_dictionary("prod_catalog")
    assert known["tables"][0]["columns"][0]["description"] == "Department"
    adapter.close()


def test_dictionary_api(project):
    client = TestClient(create_app(project))
    run = client.post("/api/w/retail/runs", json={"source": "retail", "planner": "profile", "limits": {"max_iterations": 10}}).json()
    import time

    for _ in range(200):
        if client.get(f"/api/w/retail/runs/{run['id']}").json()["status"] not in ("queued", "running", "stopping"):
            break
        time.sleep(0.1)
    d = client.get("/api/w/retail/dictionary/retail").json()
    assert d["summary"]["columns_profiled"] == d["summary"]["columns"] > 0

    url = "/api/w/retail/dictionary/retail/tables/CUST_MASTER"
    r = client.put(url, json={"author": "ana", "expected_revision": 0, "description": "Customer master", "status": "verified"})
    assert r.status_code == 200 and r.json()["revision"] == 1
    assert client.put(url, json={"author": "ana", "expected_revision": 0, "description": "x"}).status_code == 409
    assert client.put(url, json={"author": "ana", "expected_revision": 1, "values": {"a": "b"}}).status_code == 400
    assert client.put(url, json={"author": "ana", "expected_revision": 1, "status": "bogus"}).status_code == 422
    col = client.put(url + "/columns/city_txt", json={"author": "ana", "expected_revision": 0, "values": {"Seattle": "HQ city"}})
    assert col.status_code == 200
    assert client.put(url + "/columns/nope", json={"author": "a", "expected_revision": 0}).status_code == 404
    assert client.put("/api/w/retail/dictionary/retail/tables/nope", json={"author": "a", "expected_revision": 0}).status_code == 404
    assert client.get("/api/w/retail/dictionary/nope").status_code == 404

    md = client.get("/api/w/retail/dictionary/retail/markdown")
    assert md.status_code == 200 and "attachment" in md.headers["content-disposition"]
    assert "Customer master" in md.text and "`Seattle`: HQ city" in md.text


def test_cli_dictionary_export(project, settings, store, monkeypatch, capsys):
    _run(settings, store, "heuristic", max_iterations=1)
    monkeypatch.chdir(project)
    assert main(["dictionary"]) == 0
    out_dir = settings.workspace_dir / "dictionary" / "exports"
    assert (out_dir / "retail.md").read_text(encoding="utf-8").startswith("# Data Dictionary: retail / retail")
    assert (out_dir / "retail.json").exists()
