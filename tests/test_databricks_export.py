"""Portable notebook contract, worker semantics, and publication guard tests."""

import ast
import copy
import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock
from zipfile import ZipFile

import pytest

from ddg import notebook_runtime as runtime
from ddg.codegen import CodegenError, list_generated
from ddg.config import SourceConfig
from ddg.databricks_export import _bindings, generate, render_package
from ddg.mappings import build_spec
from ddg.topology import load_workspace_topology
from ddg.transforms import apply_transforms, graph_node_id

from conftest import retail_db
from test_codegen import _discover_and_approve


@pytest.fixture
def approved(settings, store):
    _discover_and_approve(settings, store)
    return {f["mapping"]["source_table"]: "catalog.schema.table_" + str(i)
            for i, f in enumerate(store.list_facts("retail"))}


@pytest.fixture
def contract(settings, store, approved):
    spec = build_spec(store.list_facts("retail"), load_workspace_topology(settings), "retail")
    return {
        "format": "ddg-databricks-notebooks/v1", "source": "retail", "workspace": "retail",
        "nodes": spec.nodes, "edges": spec.edges, "facts": spec.fact_versions,
        "source_bindings": approved, "excluded_fact_ids": [], "warnings": [],
    }


def test_given_approvals_when_exporting_then_freezes_rules_without_mutating_sources(settings, store, approved):
    before = {f["id"]: copy.deepcopy(f) for f in store.list_facts("retail")}
    database = retail_db(settings).read_bytes()

    result = generate(settings, "retail", store=store, table_bindings=approved)
    with ZipFile(result["path"]) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("export_manifest.json"))
        notebooks = {name: archive.read(name).decode() for name in names if name.endswith(".py")}

    assert len(names) == 6 and len(notebooks) == 4
    assert manifest["source_bindings"] == approved
    fingerprint = manifest.pop("rules_fingerprint")
    assert runtime.contract_fingerprint(manifest) == fingerprint == result["rules_fingerprint"]
    assert before == {f["id"]: f for f in store.list_facts("retail")}
    assert retail_db(settings).read_bytes() == database
    assert result["builder"] in [entry["builder"] for entry in list_generated(settings)]
    for text in notebooks.values():
        assert text.startswith("# Databricks notebook source\n")
        # Databricks %run cells are comments in source notebook format.
        ast.parse(text)
        assert "from ddg" not in text and "from .transforms" not in text
        assert "pandas" not in text and ".toPandas(" not in text
    assert "ENABLE_PUBLICATION = False" in notebooks["00_configure_preflight.py"]
    assert notebooks["20_publish.py"].index('%run "./10_build_validate"') < notebooks["20_publish.py"].index("CONFIRM_REPLACEMENT")
    assert "read_published_pair" in notebooks["30_explore.py"]


def test_given_manifest_when_exporting_then_uses_original_names_without_guessing(settings, approved):
    with sqlite3.connect(retail_db(settings)) as conn:
        conn.execute("CREATE TABLE _sample_manifest (sqlite_table TEXT, source_table TEXT)")
        conn.executemany("INSERT INTO _sample_manifest VALUES (?, ?)", approved.items())

    assert _bindings(settings, "retail", set(approved), {}) == approved


@pytest.mark.parametrize("entries", [[], [("CUST_MASTER", "a.b.one"), ("CUST_MASTER", "a.b.two")]])
def test_given_missing_or_ambiguous_binding_when_resolving_then_requires_override(settings, entries):
    with sqlite3.connect(retail_db(settings)) as conn:
        conn.execute("CREATE TABLE _sample_manifest (sqlite_table TEXT, source_table TEXT)")
        conn.executemany("INSERT INTO _sample_manifest VALUES (?, ?)", entries)

    with pytest.raises(CodegenError, match="explicit table_bindings"):
        _bindings(settings, "retail", {"CUST_MASTER"}, {})
    assert _bindings(settings, "retail", {"CUST_MASTER"}, {"CUST_MASTER": "a.b.reviewed"}) == {
        "CUST_MASTER": "a.b.reviewed",
    }


@pytest.mark.parametrize("binding", ["a.b", "a.b.c;DROP TABLE x", "a..b", "`a`.b.c", "a.b.c.d"])
def test_given_invalid_binding_when_resolving_then_rejects(settings, binding):
    with pytest.raises(CodegenError, match="catalog.schema.table"):
        _bindings(settings, "retail", {"CUST_MASTER"}, {"CUST_MASTER": binding})


def test_given_unknown_binding_when_resolving_then_rejects_unused_override(settings):
    with pytest.raises(CodegenError, match="unused tables"):
        _bindings(settings, "retail", {"CUST_MASTER"}, {"unused": "a.b.c"})


def test_given_non_deduplicated_edge_when_exporting_then_blocks_with_fact_error(settings, store, approved):
    edge = next(f for f in store.list_facts("retail") if f["mapping"].get("edge_type") == "PLACED")
    edge["mapping"]["dedupe"] = False
    (settings.facts_dir / (edge["id"] + ".json")).write_text(json.dumps(edge), encoding="utf-8")

    with pytest.raises(CodegenError, match=edge["id"] + ": dedupe=false"):
        generate(settings, "retail", store=store, table_bindings=approved)
    result = generate(settings, "retail", store=store, table_bindings=approved, exclude_fact_ids=[edge["id"]])
    assert result["excluded_fact_ids"] == [edge["id"]]


def test_given_unsupported_derivation_when_exporting_then_does_not_silently_discard_it(settings, store, approved):
    fact = store.list_facts("retail")[0]
    fact["mapping"]["filter"] = "active = true"
    (settings.facts_dir / (fact["id"] + ".json")).write_text(json.dumps(fact), encoding="utf-8")

    with pytest.raises(CodegenError, match="unsupported mapping fields: filter"):
        generate(settings, "retail", store=store, table_bindings=approved)


def test_given_changed_topology_when_exporting_then_rejects_stale_approval(settings, store, approved):
    settings.topology_file.write_text(settings.topology_file.read_text(encoding="utf-8") + "\nChanged\n", encoding="utf-8")

    with pytest.raises(CodegenError, match="revalidate"):
        generate(settings, "retail", store=store, table_bindings=approved)


def test_given_remote_source_when_exporting_then_needs_no_remote_credentials(settings, store, approved, monkeypatch):
    settings.sources["retail"] = SourceConfig("retail", "databricks", {
        "catalog": "a", "schema": "b", "warehouse_id": "not-used",
    })

    def forbidden(*args):
        raise AssertionError("Export must not connect to Databricks")

    monkeypatch.setattr("ddg.databricks_export.create_adapter", forbidden)

    result = generate(settings, "retail", store=store, table_bindings=approved)
    assert result["builder"].endswith(".zip")
    assert any("Remote schema drift" in warning for warning in result["warnings"])


@pytest.mark.parametrize(("values", "chains"), [
    (["0001"], [[]]), ([" a:b%/ "], [["trim"]]), (["\u00e9\u00a0\u03a9"], [["upper", "collapse_spaces"]]),
    (["a:b", "c"], [[], []]), (["a", "b:c"], [[], []]), (["\u0661\u0662-A"], [["digits_only"]]),
    (["9007199254740993"], [["int"]]), ([None], [[]]), ([""], [[]]),
    (["000001"], [["strip_leading_zeros"]]), ([" ABC "], [["trim", "lower"]]),
])
def test_given_embedded_worker_when_generating_id_then_matches_reference(contract, values, chains):
    code = render_package(contract)["00_configure_preflight.py"]
    cells = code.split("# COMMAND ----------")
    namespace = {"__name__": "__main__"}
    exec(cells[0], namespace)
    exec(cells[1], namespace)

    assert namespace["graph_node_id"]("Device", "ID", values, chains) == graph_node_id("Device", "ID", values, chains)
    for value, chain in zip(values, chains):
        assert namespace["apply_transforms"](value, chain) == apply_transforms(value, chain)


@pytest.mark.parametrize(("value", "expected"), [
    (None, None), (123, "123"), (True, "true"), ("001", '"001"'),
    (Decimal("1.20"), '"1.20"'), (date(2026, 1, 2), '"2026-01-02"'),
    (datetime(2026, 1, 2, 3, 4), '"2026-01-02 03:04:00"'), (b"x", '"b\'x\'"'),
])
def test_given_scalar_property_when_serializing_then_preserves_local_typed_semantics(value, expected, monkeypatch):
    monkeypatch.setattr(runtime, "_worker_utc", lambda: True)
    assert runtime._json_scalar(value) == expected


def test_given_non_utc_worker_when_serializing_timestamp_then_rejects_timezone_drift(monkeypatch):
    monkeypatch.setattr(runtime, "_worker_utc", lambda: False)
    with pytest.raises(ValueError, match="UTC on Python workers"):
        runtime._json_scalar(datetime(2026, 1, 2, 3, 4))


@pytest.mark.parametrize("value", [float("inf"), float("nan"), ["x"], {"nested": "x"}])
def test_given_unsupported_property_when_serializing_then_fails_explicitly(value):
    with pytest.raises(ValueError):
        runtime._json_scalar(value)


def test_given_properties_when_merging_then_sorts_keys_and_keeps_types():
    assert json.loads(runtime._properties_json([
        {"key": "z", "value": "3"}, {"key": "a", "value": '"001"'},
    ])) == {"a": "001", "z": 3}
    assert runtime._properties_json([]) == "{}"


def test_given_different_contract_order_when_fingerprinting_then_stays_stable():
    assert runtime.contract_fingerprint({"a": 1, "b": 2}) == runtime.contract_fingerprint({"b": 2, "a": 1})
    assert runtime.contract_fingerprint({"a": 1}) != runtime.contract_fingerprint({"a": 2})


@pytest.mark.parametrize("states", [
    [{"version": 1, "metadata": None}] * 2,
    [{"version": 1, "metadata": "{}"}] * 2,
    [{"version": None, "metadata": None}, {"version": 1, "metadata": '{"format":"ddg-graph-pair/v1"}'}],
    [{"version": 1, "metadata": '{"format":"ddg-graph-pair/v1","build_id":"a"}'},
     {"version": 2, "metadata": '{"format":"ddg-graph-pair/v1","build_id":"b"}'}],
])
def test_given_mixed_or_unmanaged_pair_when_reading_metadata_then_fails_closed(states):
    with pytest.raises(ValueError):
        runtime._pair_metadata(states)


def test_given_empty_published_tables_when_reading_metadata_then_uses_commit_identity():
    metadata = {"format": "ddg-graph-pair/v1", "build_id": "a", "rules_fingerprint": "b", "counts": [0, 0]}
    assert runtime._pair_metadata([
        {"version": 2, "metadata": json.dumps(metadata)}, {"version": 6, "metadata": json.dumps(metadata)},
    ]) == metadata


@pytest.mark.parametrize("targets", [("a.b.x", "a.b.x"), ("a.b.source", "a.b.edges"), ("a.b.x;drop", "a.b.edges")])
def test_given_unsafe_targets_when_planning_then_never_touches_spark(targets):
    spark = MagicMock()
    with pytest.raises(ValueError):
        runtime.publication_plan(spark, {}, {"alias": "a.b.source"}, *targets)
    assert spark.mock_calls == []


@pytest.mark.parametrize("options", [
    {"enabled": False, "ownership": True, "coordination": "lease"},
    {"enabled": True, "ownership": False, "coordination": "lease"},
    {"enabled": True, "ownership": True, "coordination": ""},
])
def test_given_missing_publication_authorization_when_publishing_then_never_touches_spark(options):
    spark = MagicMock()
    with pytest.raises(ValueError, match="Publication requires"):
        runtime.publish_graph(spark, {}, {}, {}, {}, {}, "", "a.b.nodes", "a.b.edges", "", {},
                              confirmation="", **options)
    assert spark.mock_calls == []


@pytest.mark.parametrize("policy", [
    {"max_dangling_edges": -1, "allow_empty_types": False, "allow_empty_graph": False},
    {"max_dangling_edges": True, "allow_empty_types": False, "allow_empty_graph": False},
    {"max_dangling_edges": 0, "allow_empty_types": "yes", "allow_empty_graph": False},
    {},
])
def test_given_invalid_validation_override_when_building_then_rejects(policy):
    with pytest.raises(ValueError):
        runtime._policy(policy)


@pytest.fixture
def publication(monkeypatch):
    contract = {"rules": "frozen"}
    fingerprint = runtime.contract_fingerprint(contract)
    bindings = {"records": "a.b.source"}
    policy = {"max_dangling_edges": 0, "allow_empty_types": False, "allow_empty_graph": False}
    report = {"nodes": 1, "edges": 0, "node_content_digest": ["a"], "edge_content_digest": ["0"]}
    build = {
        "build_id": "testbuild", "fingerprint": fingerprint,
        "signature": runtime._runtime_signature(contract, bindings, {}, policy),
        "report": {**report, "publishable": True, "blockers": []},
        "vertices": MagicMock(), "edges": MagicMock(),
    }
    states = {name: {"version": None, "metadata": None} for name in ("a.b.nodes", "a.b.edges")}
    monkeypatch.setattr(runtime, "_versions", lambda spark, name: copy.deepcopy(states[name]))
    monkeypatch.setattr(runtime, "validate_frames", lambda *frames: dict(report))
    monkeypatch.setattr(runtime, "_read_pair_at", lambda *args: ("nodes", "edges"))
    spark = MagicMock()
    staged = [MagicMock(), MagicMock()]
    spark.read.format.return_value.load.side_effect = staged
    for frame, name in zip(staged, states):
        frame.write.format.return_value = frame.write
        frame.write.mode.return_value = frame.write
        frame.write.option.return_value = frame.write

        def commit(quoted, *, frame=frame, name=name):
            metadata = frame.write.option.call_args.args[1]
            states[name] = {"version": 0, "metadata": metadata}

        frame.write.saveAsTable.side_effect = commit
    plan = runtime.publication_plan(spark, build, bindings, "a.b.nodes", "a.b.edges")

    def publish(**overrides):
        options = {
            "enabled": True, "confirmation": plan["confirmation"], "ownership": True, "coordination": "external-lease",
        }
        options.update(overrides)
        return runtime.publish_graph(
            spark, build, contract, bindings, {}, policy, fingerprint, "a.b.nodes", "a.b.edges",
            "dbfs:/protected/graphwright", plan, **options,
        )

    return publish, build, states, staged, plan, spark, report


def test_given_confirmed_build_when_publishing_then_stages_both_and_records_pair(publication):
    publish, build, states, staged, plan, spark, _ = publication

    receipt = publish()

    assert receipt["status"] == "complete" and receipt["versions"] == [0, 0]
    assert json.loads(states["a.b.nodes"]["metadata"]) == json.loads(states["a.b.edges"]["metadata"])
    assert receipt["stage_paths"] == [
        "dbfs:/protected/graphwright/testbuild/nodes", "dbfs:/protected/graphwright/testbuild/edges",
    ]
    for frame in staged:
        assert frame.write.mode.call_args.args == ("overwrite",)
        assert frame.write.saveAsTable.call_count == 1
    assert build["vertices"].write.format.call_count == build["edges"].write.format.call_count == 1


def test_given_failure_between_commits_when_publishing_then_reports_recovery_and_reader_rejects(publication, capsys):
    publish, _, states, staged, _, _, _ = publication
    staged[1].write.saveAsTable.side_effect = RuntimeError("Injected edge commit failure")

    with pytest.raises(RuntimeError, match="Injected edge"):
        publish()

    output = capsys.readouterr().out
    assert "PUBLICATION INCOMPLETE" in output and '"version": null' in output
    assert "testbuild/nodes" in output and "testbuild/edges" in output
    assert states["a.b.nodes"]["version"] == 0 and states["a.b.edges"]["version"] is None
    with pytest.raises(ValueError, match="mixed"):
        runtime._pair_metadata(list(states.values()))
    assert "RESTORE" not in str(staged[0].mock_calls)


def test_given_stale_confirmation_when_publishing_then_never_stages(publication):
    publish, build, _, _, _, _, _ = publication
    with pytest.raises(ValueError, match="exact confirmation"):
        publish(confirmation="REPLACE old-build")
    assert build["vertices"].write.mock_calls == []


def test_given_changed_config_when_publishing_then_requires_rebuild(publication):
    publish, build, _, _, _, _, _ = publication
    build["signature"] = "previous-policy"
    with pytest.raises(ValueError, match="configuration changed"):
        publish()
    assert build["vertices"].write.mock_calls == []


def test_given_failed_validation_when_publishing_then_never_stages(publication):
    publish, build, _, _, _, _, _ = publication
    build["report"].update(publishable=False, blockers=["property conflict"])
    with pytest.raises(ValueError, match="property conflict"):
        publish()
    assert build["vertices"].write.mock_calls == []


def test_given_changed_staged_content_when_publishing_then_never_replaces(publication, monkeypatch):
    publish, _, _, staged, _, _, report = publication
    monkeypatch.setattr(runtime, "validate_frames", lambda *args: {**report, "node_content_digest": ["different"]})
    with pytest.raises(ValueError, match="staged graph differs"):
        publish()
    assert all(frame.write.saveAsTable.call_count == 0 for frame in staged)


def test_given_target_changed_during_staging_when_publishing_then_never_replaces(publication):
    publish, build, states, staged, _, _, _ = publication
    writer = build["vertices"].write.format.return_value.mode.return_value
    metadata = json.dumps({"format": "ddg-graph-pair/v1", "build_id": "other"})

    def another_publisher(path):
        for name in states:
            states[name] = {"version": 3, "metadata": metadata}

    writer.save.side_effect = another_publisher
    with pytest.raises(ValueError, match="target changed while staging"):
        publish()
    assert all(frame.write.saveAsTable.call_count == 0 for frame in staged)


def test_given_inspected_stages_when_retrying_then_republishes_same_build(publication):
    publish, _, states, staged, _, spark, _ = publication
    receipt = publish()
    spark.read.format.return_value.load.side_effect = staged
    existing = MagicMock()
    existing.schema = staged[0].schema
    spark.read.option.return_value.table.return_value = existing

    result = runtime.retry_staged_publication(
        spark, receipt, {"records": "a.b.source"}, [0, 0],
        confirmation="RETRY a.b.nodes AND a.b.edges BUILD testbuild", ownership=True, coordination="new-lease",
    )

    assert result["status"] == "complete" and result["recovery_coordination"] == "new-lease"
    assert runtime._pair_metadata(list(states.values()))["build_id"] == "testbuild"


def test_given_changed_target_when_retrying_then_refuses_overwrite(publication):
    publish, _, _, staged, _, spark, _ = publication
    receipt = publish()
    counts = [frame.write.saveAsTable.call_count for frame in staged]

    with pytest.raises(ValueError, match="target changed since recovery inspection"):
        runtime.retry_staged_publication(
            spark, receipt, {"records": "a.b.source"}, [0, 5],
            confirmation="RETRY a.b.nodes AND a.b.edges BUILD testbuild", ownership=True, coordination="new-lease",
        )

    assert counts == [frame.write.saveAsTable.call_count for frame in staged]


def test_given_unconfirmed_recovery_when_retrying_then_never_reads_stages(publication):
    publish, _, _, _, _, spark, _ = publication
    receipt = publish()
    before = spark.read.format.call_count

    with pytest.raises(ValueError, match="Recovery requires exclusive"):
        runtime.retry_staged_publication(
            spark, receipt, {"records": "a.b.source"}, [0, 0],
            confirmation="", ownership=True, coordination="new-lease",
        )

    assert spark.read.format.call_count == before
