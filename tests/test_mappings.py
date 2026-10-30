"""Namespaced source-record identities and explicit relationship endpoint contracts."""

import copy
import json
import runpy
import sqlite3
import subprocess
import sys

import pytest

from ddg.adapters import create_adapter
from ddg.codegen import render
from ddg.control import RunControl
from ddg.graph import GraphIndex, GraphRepository
from ddg.graph_exports import write_sqlite
from ddg.mappings import build_spec, node_mapping_catalog, validate_node_mapping
from ddg.tools import DiscoveryTools
from ddg.topology import load_topology
from ddg.transforms import canonical_key, graph_node_id


def _fact(name, kind, mapping):
    return {
        "id": f"fact-{name}", "candidate_id": f"cand-{name}", "candidate_version": 1,
        "kind": kind, "source": "retail", "mapping": mapping,
    }


def _node(name, table, keys, node_type="Customer", **extra):
    return _fact(name, "node_mapping", {
        "node_type": node_type, "source_table": table, "key_columns": keys,
        "identity_namespace": f"{table}_ID", **extra,
    })


def _edge(**extra):
    return _fact("edge", "edge_mapping", {
        "edge_type": "PLACED", "source_table": "orders",
        "from_key_columns": ["customer"], "to_key_columns": ["id"], **extra,
    })


@pytest.mark.parametrize("left,right", [
    (["a|b", "c"], ["a", "b|c"]),
    (['a","b', "c"], ["a", 'b","c']),
    (["a\\b", "c"], ["a", "\\bc"]),
])
def test_given_delimiter_values_when_serializing_composite_keys_then_components_cannot_collide(left, right):
    assert canonical_key(left, [[], []]) != canonical_key(right, [[], []])
    assert json.loads(canonical_key(left, [[], []])) == left


def test_given_missing_transform_chain_when_serializing_then_raises_instead_of_dropping_key_parts():
    with pytest.raises(ValueError, match="Each key component"):
        canonical_key(["account", "line"], [[]])


@pytest.mark.parametrize("node_type,namespace,values,chains,expected", [
    ("Device", "IMEI", ["012312389"], [[]], "Device:IMEI:012312389"),
    ("Device", "VendorA_SERIALNO", ["12324234"], [[]], "Device:VendorA_SERIALNO:12324234"),
    ("Device", "MODEL_COLOR_ID", ["Max", "Blue", "02"], [[], [], []], "Device:MODEL_COLOR_ID:Max:Blue:02"),
    ("Account", "V21_BAN", [" 00012 "], [["trim"]], "Account:V21_BAN:00012"),
    ("Device", "a:b", ["x:y", "%2F", "a/b\\c?#"], [[], [], []],
     "Device:a%3Ab:x%3Ay:%252F:a%2Fb%5Cc%3F%23"),
    ("Device", "SERIAL", ["café"], [[]], "Device:SERIAL:caf%C3%A9"),
])
def test_given_semantic_identity_when_serializing_then_id_is_readable_escaped_and_preserves_values(
    node_type, namespace, values, chains, expected,
):
    assert graph_node_id(node_type, namespace, values, chains) == expected


@pytest.mark.parametrize("values", [[None], [""], ["   "]])
def test_given_missing_key_component_when_serializing_identity_then_null_policy_can_apply(values):
    assert graph_node_id("Account", "V21_BAN", values, [["trim"]]) is None


def test_given_separators_in_components_when_serializing_identity_then_different_tuples_cannot_collide():
    assert graph_node_id("Device", "SERIAL", ["a:b", "c"], [[], []]) != graph_node_id(
        "Device", "SERIAL", ["a", "b:c"], [[], []],
    )
    assert graph_node_id("Device", "a:b", ["c"], [[]]) != graph_node_id("Device", "a", ["b:c"], [[]])
    assert graph_node_id("Device", "SERIAL", ["%3A"], [[]]) != graph_node_id("Device", "SERIAL", [":"], [[]])


@pytest.mark.parametrize("namespace", [None, "", " ", " IMEI", "IMEI ", ["old", "table", "namespace"]])
def test_given_legacy_or_invalid_namespace_when_validating_then_requires_explicit_review(settings, namespace):
    mapping = _node("a", "crm", ["id"], identity_namespace=namespace)["mapping"]

    errors = validate_node_mapping(mapping, load_topology(settings.topology_file))

    assert "identity_namespace must be an explicit" in " ".join(errors)


@pytest.mark.parametrize("parts", [[], ["one", "two"], ["id", "id"], [None], "id", [["id"]]])
def test_given_invalid_semantic_key_parts_when_validating_then_reports_contract_error(settings, parts):
    mapping = _node("a", "crm", ["id"], identity_key_parts=parts)["mapping"]

    errors = validate_node_mapping(mapping, load_topology(settings.topology_file))

    assert "identity_key_parts" in " ".join(errors)


@pytest.mark.parametrize("changes", [
    {"key_columns": ["id", "sequence"], "identity_key_parts": ["id", "sequence"]},
    {"identity_key_parts": ["different_meaning"]},
    {"key_transforms": [["upper"]]},
])
def test_given_shared_namespace_with_conflicting_contract_when_building_then_flags_both_facts(settings, changes):
    facts = [
        _node("a", "crm", ["id"], identity_namespace="CUSTOMER_ID", identity_key_parts=["id"]),
        _node("b", "billing", ["id"], identity_namespace="CUSTOMER_ID", identity_key_parts=["id"]),
    ]
    facts[1]["mapping"].update(changes)
    original = copy.deepcopy(facts)

    spec = build_spec(facts, load_topology(settings.topology_file), "retail")

    assert not spec.ok
    assert set(spec.fact_errors) == {"fact-a", "fact-b"}
    assert all("incompatible contracts" in " ".join(errors) for errors in spec.fact_errors.values())
    assert facts == original


def test_given_missing_namespace_in_approval_when_building_then_flags_without_assigning_one(settings):
    fact = _node("legacy", "crm", ["id"])
    del fact["mapping"]["identity_namespace"]
    original = copy.deepcopy(fact)

    spec = build_spec([fact], load_topology(settings.topology_file), "retail", allow_stale=True)

    assert not spec.ok
    assert "identity_namespace" in " ".join(spec.fact_errors["fact-legacy"])
    assert fact == original


def test_given_different_source_key_shapes_when_building_then_each_contract_is_retained(settings):
    topology = load_topology(settings.topology_file)
    facts = [_node("a", "crm", ["id"]), _node("b", "billing", ["ban", "line", "sequence"])]
    original = copy.deepcopy(facts)

    spec = build_spec(facts, topology, "retail")

    assert spec.ok, spec.errors
    assert len(spec.nodes) == 2
    assert spec.nodes[0]["identity_namespace"] != spec.nodes[1]["identity_namespace"]
    assert facts == original


@pytest.mark.parametrize("explicit", [False, True])
def test_given_multiple_contracts_when_binding_edge_then_requires_explicit_target(settings, explicit):
    topology = load_topology(settings.topology_file)
    facts = [
        _node("a", "crm", ["id"]), _node("b", "billing", ["ban", "line"]),
        _node("order", "orders", ["id"], "Order"),
        _edge(**({"from_mapping_id": "cand-a", "to_mapping_id": "fact-order"} if explicit else {})),
    ]

    spec = build_spec(facts, topology, "retail")

    assert spec.ok is explicit
    if explicit:
        assert spec.edges[0]["from_identity_namespace"] == spec.nodes[0]["identity_namespace"]
        assert spec.edges[0]["to_mapping_id"] == "cand-order"
    else:
        assert "ambiguous Customer endpoint" in " ".join(spec.fact_errors["fact-edge"])
        assert "from_mapping_id" in " ".join(spec.fact_errors["fact-edge"])


def test_given_same_arity_different_namespaces_when_binding_legacy_edge_then_does_not_guess(settings):
    topology = load_topology(settings.topology_file)
    facts = [
        _node("a", "crm", ["id"]), _node("b", "billing", ["ban"]),
        _node("order", "orders", ["id"], "Order"), _edge(),
    ]

    spec = build_spec(facts, topology, "retail")

    assert not spec.ok
    assert "ambiguous" in " ".join(spec.fact_errors["fact-edge"])


@pytest.mark.parametrize("reference", ["cand-deleted", "cand-other-source", "cand-order"])
def test_given_unavailable_or_wrong_type_target_when_binding_then_cannot_fall_back(settings, reference):
    topology = load_topology(settings.topology_file)
    foreign = {**_node("other-source", "crm", ["id"]), "source": "another-source"}
    facts = [
        _node("a", "crm", ["id"]), foreign,
        _node("order", "orders", ["id"], "Order"), _edge(from_mapping_id=reference),
    ]

    spec = build_spec(facts, topology, "retail")

    assert not spec.ok
    assert "does not identify an available node mapping" in " ".join(spec.fact_errors["fact-edge"])


def test_given_wrong_composite_arity_when_binding_explicit_endpoint_then_reports_selected_contract(settings):
    topology = load_topology(settings.topology_file)
    facts = [
        _node("a", "crm", ["ban", "subscriber", "sequence"]),
        _node("order", "orders", ["id"], "Order"),
        _edge(from_mapping_id="cand-a", from_key_columns=["ban", "subscriber"]),
    ]

    spec = build_spec(facts, topology, "retail")

    assert not spec.ok
    assert "ban + subscriber + sequence" in " ".join(spec.fact_errors["fact-edge"])


@pytest.mark.parametrize("keys,transforms", [
    (["sequence", "ban"], [[], []]),
    (["ban", "sequence"], [["int"], []]),
])
def test_given_same_table_endpoint_when_order_or_normalization_differs_then_rejects(settings, keys, transforms):
    topology = load_topology(settings.topology_file)
    facts = [
        _node("a", "orders", ["ban", "sequence"]),
        _node("order", "orders", ["id"], "Order"),
        _edge(from_mapping_id="cand-a", from_key_columns=keys, from_key_transforms=transforms),
    ]

    spec = build_spec(facts, topology, "retail")

    assert not spec.ok
    assert "exact ordered key contract" in " ".join(spec.fact_errors["fact-edge"])


def test_given_pending_mapping_when_approved_then_endpoint_reference_and_namespace_remain_stable(settings, store):
    candidate = store.add_candidate({
        "kind": "node_mapping", "hypothesis": "Customer record identity",
        "scope": {"source": "retail"},
        "mapping": {"node_type": "Customer", "source_table": "CUST_MASTER", "key_columns": ["cust_id"],
                    "identity_namespace": "CUSTOMER_ID"},
    })
    pending = node_mapping_catalog(store.review_snapshot(), "retail")["Customer"][0]

    store.decide(candidate["id"], "approve", "sme")
    approved = node_mapping_catalog(store.review_snapshot(), "retail")["Customer"][0]

    assert approved["mapping_id"] == pending["mapping_id"]
    assert approved["identity_namespace"] == pending["identity_namespace"]
    assert approved["_status"] == "approved"


@pytest.mark.parametrize("scope", ["all", "edges"])
def test_given_ambiguous_endpoint_when_proposing_then_enforces_contract_before_review(settings, store, scope):
    for table, keys in (("CUST_MASTER", ["cust_id"]), ("tbl_ord_hdr", ["cust_ref", "order_no"])):
        store.add_candidate({
            "kind": "node_mapping", "hypothesis": table, "scope": {"source": "retail"},
            "mapping": {"node_type": "Customer", "source_table": table, "key_columns": keys,
                        "identity_namespace": f"{table}_ID"},
        })
    order = store.add_candidate({
        "kind": "node_mapping", "hypothesis": "Order", "scope": {"source": "retail"},
        "mapping": {"node_type": "Order", "source_table": "tbl_ord_hdr", "key_columns": ["order_no"],
                    "identity_namespace": "ORDER_ID"},
    })
    adapter = create_adapter(settings, settings.source("retail"))
    try:
        tools = DiscoveryTools(
            settings, store, adapter, load_topology(settings.topology_file),
            RunControl(settings.limits, settings.model, lambda: False), "run-contracts",
            mode="exploratory", discovery_scope=scope,
        )
        evidence = tools.describe_table("tbl_ord_hdr")
        mapping = {
            "edge_type": "PLACED", "source_table": "tbl_ord_hdr",
            "from_key_columns": ["cust_ref"], "to_key_columns": ["order_no"],
        }

        rejected = tools.propose_candidate("edge_mapping", "Customer places Order", "test",
                                           [evidence["evidence_id"]], dict(mapping))
        customer = next(node for node in node_mapping_catalog(store.review_snapshot(), "retail")["Customer"]
                        if node["source_table"] == "CUST_MASTER")
        accepted = tools.propose_candidate(
            "edge_mapping", "Customer places Order", "test", [evidence["evidence_id"]],
            {**mapping, "from_mapping_id": customer["mapping_id"], "to_mapping_id": order["id"]},
        )

        assert "from_mapping_id" in rejected["error"]
        assert "candidate_id" in accepted, accepted
        assert store.get_candidate(accepted["candidate_id"])["mapping"]["from_mapping_id"] == customer["mapping_id"]
    finally:
        adapter.close()


@pytest.mark.parametrize("namespace,parts,expected_error", [
    (None, ["customer"], "identity_namespace"),
    ("CUSTOMER_ID", ["different_meaning"], "incompatible contracts"),
])
def test_given_invalid_node_identity_when_proposing_then_blocks_before_persistence(
    settings, store, namespace, parts, expected_error,
):
    store.add_candidate({
        "kind": "node_mapping", "hypothesis": "Established customer contract", "scope": {"source": "retail"},
        "mapping": {
            "node_type": "Customer", "source_table": "CUST_MASTER", "key_columns": ["cust_id"],
            "identity_namespace": "CUSTOMER_ID", "identity_key_parts": ["customer"],
        },
    })
    adapter = create_adapter(settings, settings.source("retail"))
    try:
        tools = DiscoveryTools(
            settings, store, adapter, load_topology(settings.topology_file),
            RunControl(settings.limits, settings.model, lambda: False), "run-contracts", mode="exploratory",
        )
        evidence = tools.describe_table("tbl_ord_hdr")
        before = store.review_snapshot()

        result = tools.propose_candidate(
            "node_mapping", "Customer identity", "test", [evidence["evidence_id"]],
            {"node_type": "Customer", "source_table": "tbl_ord_hdr", "key_columns": ["cust_ref"],
             "identity_namespace": namespace, "identity_key_parts": parts},
        )

        assert expected_error in result["error"]
        assert store.review_snapshot() == before
    finally:
        adapter.close()


def test_given_colliding_values_and_different_key_shapes_when_building_then_edges_use_selected_namespace(settings, tmp_path):
    topology = load_topology(settings.topology_file)
    facts = [
        _node("a", "crm", ["id"]),
        _node("b", "billing", ["ban", "line"]),
        _node("c", "another_crm", ["id"]),
        _node("order", "orders", ["id"], "Order"),
        _edge(from_key_columns=["ban", "line"], from_mapping_id="cand-b", to_mapping_id="cand-order"),
    ]
    spec = build_spec(facts, topology, "retail")
    assert spec.ok, spec.errors
    script = tmp_path / "build.py"
    script.write_text(render(spec, {}, script.name, "test"), encoding="utf-8")
    database = tmp_path / "source.sqlite"
    with sqlite3.connect(database) as conn:
        conn.executescript("""
            CREATE TABLE crm(id TEXT);
            INSERT INTO crm VALUES ('a|b|c');
            CREATE TABLE another_crm(id TEXT);
            INSERT INTO another_crm VALUES ('a|b|c');
            CREATE TABLE billing(ban TEXT, line TEXT);
            INSERT INTO billing VALUES ('a|b', 'c'), ('a', 'b|c');
            CREATE TABLE orders(id TEXT, ban TEXT, line TEXT);
            INSERT INTO orders VALUES ('order', 'a', 'b|c');
        """)
    output = tmp_path / "graph.json"

    completed = subprocess.run(
        [sys.executable, "-I", "-S", str(script), "--sqlite", str(database), "--out", str(output)],
        capture_output=True, text=True, check=False,
    )

    assert completed.returncode == 0, completed.stderr
    graph = json.loads(output.read_text(encoding="utf-8"))
    assert graph["stats"]["nodes"] == 5
    assert graph["stats"]["edges"] == 1
    assert graph["stats"]["dangling_edges"] == 0
    edge = graph["edges"][0]
    target_customer = next(node for node in graph["nodes"] if node["id"] == edge["source"])
    assert target_customer["key"] == '["a","b|c"]'
    assert target_customer["provenance"][0]["fact_id"] == "fact-b"
    assert graph["inputs"]["identity_format"] == "semantic/v1"
    assert target_customer["id"] == "Customer:billing_ID:a:b%7Cc"
    write_sqlite(graph, tmp_path / "graph.sqlite")
    json_index = GraphIndex(graph, output)
    sql_index = GraphRepository(tmp_path).get("graph.sqlite")
    assert json_index.search("a|b|c", "Customer") == sql_index.search("a|b|c", "Customer")
    assert len(json_index.search("a|b|c", "Customer")) == 2
    with sqlite3.connect(database) as conn:
        rebuilt = runpy.run_path(str(script))["build_graph"](conn)
    assert rebuilt["nodes"] == graph["nodes"]
    assert rebuilt["edges"] == graph["edges"]


def test_given_shared_semantic_identity_across_tables_when_building_then_merges_and_preserves_provenance(settings, tmp_path):
    topology = load_topology(settings.topology_file)
    facts = [
        _node("a", "crm", ["ban"], identity_namespace="V21_BAN", identity_key_parts=["ban"]),
        _node("b", "billing", ["account_no"], identity_namespace="V21_BAN", identity_key_parts=["ban"]),
        _node("order", "orders", ["id"], "Order", identity_namespace="ORDER_ID"),
        _edge(from_mapping_id="cand-b", to_mapping_id="cand-order"),
    ]
    spec = build_spec(facts, topology, "retail")
    assert spec.ok, spec.errors
    script = tmp_path / "build.py"
    script.write_text(render(spec, {}, script.name, "test"), encoding="utf-8")
    builder = runpy.run_path(str(script))["build_graph"]
    with sqlite3.connect(":memory:") as conn:
        conn.executescript("""
            CREATE TABLE crm(ban TEXT);
            INSERT INTO crm VALUES ('00012');
            CREATE TABLE billing(account_no TEXT);
            INSERT INTO billing VALUES ('00012');
            CREATE TABLE orders(id TEXT, customer TEXT);
            INSERT INTO orders VALUES ('order', '00012');
        """)

        graph = builder(conn)
        conn.execute("ALTER TABLE crm RENAME TO renamed")
        renamed_facts = copy.deepcopy(facts)
        renamed_facts[0]["mapping"]["source_table"] = "renamed"
        for fact in renamed_facts:
            fact["source"] = "renamed_connection"
        renamed_spec = build_spec(renamed_facts, topology, "renamed_connection")
        renamed_script = tmp_path / "renamed_build.py"
        renamed_script.write_text(render(renamed_spec, {}, renamed_script.name, "test"), encoding="utf-8")
        renamed_graph = runpy.run_path(str(renamed_script))["build_graph"](conn)

    customer = next(node for node in graph["nodes"] if node["type"] == "Customer")
    assert customer["id"] == "Customer:V21_BAN:00012"
    assert {item["fact_id"] for item in customer["provenance"]} == {"fact-a", "fact-b"}
    assert graph["stats"]["nodes"] == 2 and graph["stats"]["edges"] == 1
    assert graph["edges"][0]["source"] == customer["id"]
    assert [node["id"] for node in renamed_graph["nodes"]] == [node["id"] for node in graph["nodes"]]
    assert renamed_graph["edges"][0]["source"] == graph["edges"][0]["source"]
