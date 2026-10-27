import sqlite3

import pytest

from ddg.adapters import ReadOnlyViolation, SQLiteAdapter, UnknownObject, ensure_read_only


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
