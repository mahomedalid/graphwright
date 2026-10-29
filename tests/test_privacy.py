import pytest

from ddg.adapters import create_adapter
from ddg.control import RunControl
from ddg.tools import DiscoveryTools
from ddg.topology import load_topology


@pytest.fixture
def tools(settings, store):
    control = RunControl(settings.limits, settings.model, cancel_requested=lambda: False)
    adapter = create_adapter(settings, settings.source("retail"))
    yield DiscoveryTools(settings, store, adapter, load_topology(settings.topology_file), control, "run-test")
    adapter.close()


def test_sample_blocks_non_allowlisted_columns_and_caps_rows(tools):
    result = tools.sample_rows("CUST_MASTER", ["cust_id", "email_addr", "city_txt"], limit=500)
    assert result["columns"] == ["city_txt"]
    assert set(result["blocked_columns"]) == {"cust_id", "email_addr"}
    assert result["returned_rows"] == tools.settings.sampling.max_rows
    assert all("@" not in str(v) for row in result["rows"] for v in row)


def test_sample_with_only_blocked_columns_returns_no_values(tools):
    result = tools.sample_rows("CUST_MASTER", ["email_addr", "full_nm"], limit=3)
    assert result["rows"] == [] and result["columns"] == []


def test_profile_hides_top_values_unless_allowlisted(tools):
    blocked = tools.profile_column("CUST_MASTER", "email_addr")
    assert blocked["top_values"].startswith("blocked")
    assert blocked["distinct_count"] == 200 and blocked["value_shapes"]
    allowed = tools.profile_column("prod_catalog", "category")
    assert isinstance(allowed["top_values"], list) and allowed["top_values"][0]["value"]


def test_join_reports_shapes_not_values_for_blocked_columns(tools):
    result = tools.check_join("tbl_ord_hdr", ["cust_ref"], "CUST_MASTER", ["cust_id"], [["digits_only", "int"]], [["int"]])
    assert result["row_coverage"] > 0.95 and result["unmatched_left_keys"] > 0
    assert "unmatched_examples" not in result
    assert result["unmatched_value_shapes"] == ["A-99999"]


def test_events_do_not_contain_row_values(tools, store):
    tools.sample_rows("prod_catalog", ["prod_name"], limit=3)
    events = store.read_events("run-test")
    assert events and "Product" not in str(events)


def test_given_unexpected_tool_error_when_recording_then_failure_is_visible_without_raw_error_text(
    tools, store, monkeypatch
):
    def broken():
        raise RuntimeError("sensitive row value")

    monkeypatch.setattr(tools.adapter, "tables", broken)

    import pytest

    with pytest.raises(RuntimeError, match="sensitive"):
        tools.list_tables()
    events = store.read_events("run-test")

    assert [event["type"] for event in events] == ["tool_started", "tool_failed"]
    assert events[-1]["error_type"] == "RuntimeError"
    assert events[0]["call_id"] == events[1]["call_id"]
    assert "sensitive" not in str(events)
