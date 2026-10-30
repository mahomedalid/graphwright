"""Optional real Spark tests; install pyspark 3.5.8 and Java 17 (or jdk4py)."""

import json
import os
import sys

import pytest

pyspark = pytest.importorskip("pyspark", reason="Install pyspark 3.5.8 and Java 17 to run distributed parity tests")

from ddg.databricks_export import render_package
from ddg.notebook_runtime import contract_fingerprint
from ddg.transforms import graph_node_id


@pytest.fixture(scope="module")
def spark():
    if not os.environ.get("JAVA_HOME"):
        jdk4py = pytest.importorskip("jdk4py")
        os.environ["JAVA_HOME"] = str(jdk4py.JAVA_HOME)
    os.environ["PYSPARK_PYTHON"] = sys.executable
    os.environ["PYSPARK_DRIVER_PYTHON"] = sys.executable
    os.environ["SPARK_LOCAL_IP"] = "127.0.0.1"
    session = (pyspark.sql.SparkSession.builder.master("local[2]").appName("graphwright-notebook-parity")
               .config("spark.ui.enabled", "false").config("spark.sql.shuffle.partitions", "2")
               .config("spark.sql.session.timeZone", "UTC").getOrCreate())
    session.sparkContext.setLogLevel("ERROR")
    try:
        yield session
    finally:
        session.stop()


@pytest.fixture
def worker():
    namespace = {"__name__": "__main__"}
    contract = {
        "source_bindings": {"records": "a.b.records", "links": "a.b.links"},
        "nodes": [{
            "fact_id": "node", "mapping_id": "n", "node_type": "Device",
            "identity_namespace": "IMEI", "identity_key_parts": ["identifier"],
            "source_table": "records", "key_columns": ["key"], "key_transforms": [["trim"]],
            "properties": {"label": "label", "count": "count"}, "null_key_policy": "skip",
        }],
        "edges": [{
            "fact_id": "edge", "edge_type": "LINKS", "source_table": "links", "dedupe": True,
            "from_node_type": "Device", "to_node_type": "Device",
            "from_identity_namespace": "IMEI", "to_identity_namespace": "IMEI",
            "from_key_columns": ["from"], "to_key_columns": ["to"],
            "from_key_transforms": [["trim"]], "to_key_transforms": [["trim"]],
            "properties": {}, "null_key_policy": "error",
        }],
        "facts": [{"fact_id": "node", "fact_revision": 1}, {"fact_id": "edge", "fact_revision": 1}],
        "source": "local", "warnings": [], "excluded_fact_ids": [],
    }
    cells = render_package(contract)["00_configure_preflight.py"].split("# COMMAND ----------")
    exec(cells[0], namespace)
    exec(cells[1], namespace)
    return namespace, contract


def test_given_distributed_inputs_when_building_then_preserves_ids_merges_and_reports_conflicts(spark, worker):
    runtime, contract = worker
    records = spark.createDataFrame([
        (" 0001 ", "first", 1), ("0001", "first", 1),
        ("a:b%/\u03a9", "second", 2), ("isolated", "alone", 3),
        (None, "invalid", 4), ("conflict", "x", 5), ("conflict", "y", 5),
    ], "key string, label string, count long").repartition(2)
    links = spark.createDataFrame([
        ("0001", "a:b%/\u03a9"), ("0001", "a:b%/\u03a9"), ("0001", "missing"),
    ], "from string, to string").repartition(2)
    bindings = contract["source_bindings"]
    sources = {"frames": {"records": records, "links": links},
               "observations": {name: {"table": table, "version": None} for name, table in bindings.items()}}
    policy = {"max_dangling_edges": 0, "allow_empty_types": False, "allow_empty_graph": False}

    build = runtime["build_graph_frames"](spark, contract, sources, bindings, {}, policy, contract_fingerprint(contract))
    try:
        nodes = {row["id"]: row.asDict() for row in build["vertices"].collect()}
        edges = [row.asDict() for row in build["edges"].collect()]
        expected = graph_node_id("Device", "IMEI", ["0001"], [["trim"]])
        assert len(nodes) == 4 and len(edges) == 1
        assert expected in nodes
        assert json.loads(nodes[expected]["properties_json"]) == {"label": "first", "count": 1}
        assert json.loads(nodes[expected]["provenance_json"])[0]["rows"] == 2
        assert edges[0]["src"] == expected
        assert edges[0]["dst"] == graph_node_id("Device", "IMEI", ["a:b%/\u03a9"], [["trim"]])
        assert build["report"]["node_property_conflicts"] == 1
        assert build["report"]["dangling_edges"] == 1
        assert build["report"]["isolated_vertices"] == 2
        assert build["report"]["mappings"][0]["null_keys"] == 1
        assert not build["report"]["publishable"]
        assert runtime["validate_frames"](build["vertices"].repartition(3), build["edges"].repartition(3))[
            "node_content_digest"
        ] == build["report"]["node_content_digest"]
    finally:
        runtime["release_build"](build)
    assert build["_cached"] == []


def test_given_composite_unicode_keys_when_using_spark_worker_then_matches_python(spark, worker):
    from pyspark.sql import functions as F, types as T

    runtime, _ = worker
    values = [
        ("001", "x:y"), ("00%", "/"), ("\u00a0\u03a9 ", "\u0661\u0662-A"),
        ("a:b", "c"), ("a", "b:c"), (None, "x"), ("", "y"),
    ]
    chains = [["trim", "upper"], ["digits_only"]]
    key = F.udf(lambda a, b: runtime["graph_node_id"]("Device", "model.color.id", [a, b], chains), T.StringType())

    actual = [r["id"] for r in spark.createDataFrame(values, "a string, b string").select(
        key("a", "b").alias("id")).collect()]

    assert actual == [graph_node_id("Device", "model.color.id", list(row), chains) for row in values]
