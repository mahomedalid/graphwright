import json
import sqlite3
import subprocess
import sys

import pytest

from ddg.agent.runner import DiscoveryRunner
from ddg.codegen import CodegenError, generate
from ddg.graph import GraphIndex, GraphRepository

from conftest import approve_all, retail_db


def _discover_and_approve(settings, store, only=None):
    runner = DiscoveryRunner(settings, store)
    runner.execute(runner.create_run("retail", "heuristic", {"max_iterations": 20})["id"])
    for c in store.list_candidates("pending"):
        name = (c.get("mapping") or {}).get("node_type") or (c.get("mapping") or {}).get("edge_type")
        if only is None or name in only:
            store.decide(c["id"], "approve", "sme", "", "", c["version"])


def _build(settings, script, out):
    db = retail_db(settings)
    completed = subprocess.run(
        [sys.executable, script, "--sqlite", str(db), "--out", str(out),
         "--graphml", str(out.with_suffix(".graphml")), "--sqlite-out", str(out.with_suffix(".sqlite")),
         "--cypher", str(out.with_suffix(".cypher"))],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def test_generated_builder_produces_reproducible_graph(settings, store):
    _discover_and_approve(settings, store)
    result = generate(settings, "retail", store=store)
    code = result["code"]
    assert "Topology version" in code and "def build_graph" in code and "canonical_key" in code
    assert len(result["facts"]) == 7 and "--sqlite workspaces/retail/data/retail.sqlite" in result["suggested_command"]

    out_dir = settings.graph_output_dir
    first = _build(settings, result["path"], out_dir / "a.json")
    second = _build(settings, result["path"], out_dir / "b.json")
    assert first["nodes"] == second["nodes"] and first["edges"] == second["edges"]

    stats = first["stats"]["by_type"]
    conn = sqlite3.connect(retail_db(settings))
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    assert stats["Customer"] == 200 and stats["Order"] == 1000 and stats["Product"] == 60 and stats["Store"] == 8
    orphan_orders = q("SELECT COUNT(*) FROM tbl_ord_hdr WHERE CAST(SUBSTR(cust_ref, 3) AS INTEGER) > 200")
    assert stats["PLACED"] == 1000 - orphan_orders
    assert stats["SOLD_AT"] == q("SELECT COUNT(*) FROM tbl_ord_hdr WHERE store_cd IS NOT NULL")
    assert stats["CONTAINS"] == q("SELECT COUNT(DISTINCT order_no || '|' || UPPER(sku)) FROM ord_lines_v2")
    assert first["stats"]["dangling_edges"] == orphan_orders and first["warnings"]
    assert len({n["id"] for n in first["nodes"]}) == len(first["nodes"])

    customer = next(n for n in first["nodes"] if n["id"] == "Customer:12")
    assert customer["properties"]["email"].endswith("@example.com")
    assert customer["provenance"][0]["table"] == "CUST_MASTER"
    edge = next(e for e in first["edges"] if e["type"] == "CONTAINS")
    assert set(edge["properties"]) == {"quantity", "unit price"}
    assert first["inputs"]["topology_version"] == result["topology_version"]
    assert (out_dir / "a.graphml").read_text(encoding="utf-8").startswith("<?xml")

    index = GraphIndex(first, out_dir / "a.json")
    hood = index.neighborhood("Customer:12", depth=2, limit=40)
    assert any(e["type"] == "PLACED" for e in hood["edges"]) and len(hood["nodes"]) <= 40
    assert index.search("example.com", "Customer", limit=5)
    sql_index = GraphRepository(out_dir).get("a.sqlite")
    assert sql_index.search("example.com", "Customer", limit=5) == index.search("example.com", "Customer", limit=5)
    assert sql_index.neighborhood("Customer:12", depth=2, limit=40) == hood
    cypher = (out_dir / "a.cypher").read_text(encoding="utf-8")
    assert cypher.count("MERGE (n:DDGNode") == len(first["nodes"])
    assert cypher.count("MERGE (s)-[r:") == len(first["edges"])
    assert "--sqlite-out " in result["suggested_commands"]["sqlite"]
    assert "--cypher " in result["suggested_commands"]["cypher"]


@pytest.mark.parametrize("output_flag", ["--sqlite-out", "--cypher", "--graphml"])
def test_given_export_only_command_when_running_builder_then_does_not_require_json(settings, store, tmp_path, output_flag):
    _discover_and_approve(settings, store)
    result = generate(settings, "retail", store=store)
    output = tmp_path / "nested" / "graph.export"

    completed = subprocess.run(
        [sys.executable, "-I", "-S", result["path"], "--sqlite", str(retail_db(settings)), output_flag, str(output)],
        capture_output=True, text=True, check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert output.stat().st_size > 0


def test_given_conflicting_output_paths_when_running_builder_then_rejects_before_writing(settings, store, tmp_path):
    _discover_and_approve(settings, store)
    script = generate(settings, "retail", store=store)["path"]
    db = retail_db(settings)
    original = db.read_bytes()
    output = tmp_path / "graph"
    commands = [
        ["--out", str(db)], ["--sqlite-out", str(db)], [],
        ["--out", str(output), "--cypher", str(output)],
    ]

    for flags in commands:
        completed = subprocess.run(
            [sys.executable, script, "--sqlite", str(db), *flags], capture_output=True, text=True, check=False,
        )
        assert completed.returncode == 2
    assert db.read_bytes() == original
    assert not output.exists()


def test_codegen_blocks_edges_without_node_mappings(settings, store):
    _discover_and_approve(settings, store, only={"Order", "PLACED"})
    with pytest.raises(CodegenError) as err:
        generate(settings, "retail", store=store)
    assert any("approved node mapping for 'Customer'" in e for e in err.value.errors)


def test_codegen_requires_approved_facts(settings, store):
    with pytest.raises(CodegenError, match="No approved node mappings"):
        generate(settings, "retail", store=store)


def test_topology_change_flags_facts_for_revalidation(settings, store):
    _discover_and_approve(settings, store)
    path = settings.topology_file
    path.write_text(path.read_text(encoding="utf-8").replace("A person or organization", "Any party"), encoding="utf-8")
    with pytest.raises(CodegenError, match="revalidate"):
        generate(settings, "retail", store=store)
    result = generate(settings, "retail", allow_stale=True, store=store)
    assert any("revalidate" in w for w in result["warnings"])


def test_schema_drift_flags_facts(settings, store):
    _discover_and_approve(settings, store)
    db = retail_db(settings)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE stores_ref ADD COLUMN opened_dt TEXT")
    conn.commit()
    conn.close()
    with pytest.raises(CodegenError, match="schema of 'stores_ref' changed"):
        generate(settings, "retail", store=store)


def test_approve_all_helper_is_idempotent(settings, store):
    _discover_and_approve(settings, store, only=set())
    approve_all(store)
    assert not store.list_candidates("pending")
