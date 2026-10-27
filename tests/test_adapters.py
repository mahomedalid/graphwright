import json
import sqlite3

import pytest

from ddg.adapters import DatabricksAdapter, ReadOnlyViolation, SQLiteAdapter, UnknownObject, ensure_read_only


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (1)",
        "DROP TABLE t",
        "SELECT 1; DELETE FROM t",
        "PRAGMA writable_schema = 1",
        "WITH x AS (SELECT 1) DELETE FROM t",
        "ATTACH DATABASE 'x' AS y",
        "SELECT * INTO new_t FROM t",
        "",
    ],
)
def test_guard_rejects_non_select(sql):
    with pytest.raises(ReadOnlyViolation):
        ensure_read_only(sql)


def test_guard_allows_select_with_keyword_literals():
    ensure_read_only("SELECT \"delete\", 'drop table' FROM t WHERE x = 'insert'")
    ensure_read_only("WITH a AS (SELECT 1) SELECT * FROM a;")


def test_sqlite_adapter_is_read_only_even_without_guard(settings):
    adapter = SQLiteAdapter("retail", settings.workspace_dir / "data/retail.sqlite")
    with pytest.raises(ReadOnlyViolation):
        adapter.run("DELETE FROM CUST_MASTER")
    with pytest.raises(sqlite3.OperationalError):
        adapter._execute("DELETE FROM CUST_MASTER", 1)  # bypass guard: connection itself refuses writes
    assert adapter.row_count("CUST_MASTER") == 200
    adapter.close()


def test_sqlite_adapter_metadata_and_unknown_objects(settings):
    adapter = SQLiteAdapter("retail", settings.workspace_dir / "data/retail.sqlite")
    assert "tbl_ord_hdr" in adapter.tables()
    info = adapter.describe_table("TBL_ORD_HDR")
    assert info["table"] == "tbl_ord_hdr" and info["foreign_keys"] == []
    assert [c["name"] for c in info["columns"]][:2] == ["order_no", "cust_ref"]
    with pytest.raises(UnknownObject):
        adapter.describe_table("users; DROP TABLE x")
    with pytest.raises(UnknownObject):
        adapter.column_stats("tbl_ord_hdr", "nope")
    adapter.close()


def test_databricks_adapter_uses_cli_statement_api():
    calls = []

    def fake_runner(args, timeout):
        payload = json.loads(args[args.index("--json") + 1])
        calls.append(payload)
        sql = payload["statement"]
        if "information_schema.tables" in sql:
            rows = [["orders", "MANAGED", None]]
        elif "information_schema.columns" in sql:
            rows = [["order_id", "string", "NO", None]]
        elif "table_constraints" in sql:
            rows = []
        else:
            rows = [["42"]]
        return json.dumps({"status": {"state": "SUCCEEDED"}, "manifest": {"schema": {"columns": [{"name": "c"}]}}, "result": {"data_array": rows}})

    adapter = DatabricksAdapter("prod", "wh123", "main", "sales", profile="DEV", runner=fake_runner)
    assert list(adapter.tables()) == ["orders"]
    assert adapter.row_count("orders") == 42
    assert "`main`.`sales`.`orders`" in calls[-1]["statement"]
    assert calls[-1]["warehouse_id"] == "wh123" and calls[-1]["on_wait_timeout"] == "CANCEL"
    with pytest.raises(ReadOnlyViolation):
        adapter.run("DROP TABLE main.sales.orders")
    with pytest.raises(ValueError):
        DatabricksAdapter("prod", "wh", "main", "sales'; DROP", runner=fake_runner)


def test_databricks_timeout_is_reported():
    def runner(args, timeout):
        return json.dumps({"status": {"state": "CANCELED"}})

    adapter = DatabricksAdapter("prod", "wh", "main", "sales", runner=runner)
    with pytest.raises(TimeoutError):
        adapter.run("SELECT 1")
