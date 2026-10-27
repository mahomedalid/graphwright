import json

import pytest

from ddg.adapters import DatabricksAdapter, ReadOnlyViolation, SQLiteAdapter, UnknownObject, create_adapter
from ddg.adapters import databricks_adapter
from ddg.adapters.databricks_adapter import parse_table_scope
from ddg.cli import main

CATALOG_TABLES = [
    ["sales", "orders", "MANAGED", None],
    ["sales", "order_lines", "MANAGED", None],
    ["sales", "orders_bak", "MANAGED", None],
    ["crm", "accounts", "MANAGED", "CRM accounts"],
    ["crm", "contacts", "VIEW", None],
]


class FakeDatabricks:
    """Simulates `databricks api` calls for the SQL Statement Execution API."""

    def __init__(self, pending_polls=0, chunked=False):
        self.calls = []
        self.pending_polls = pending_polls
        self.chunked = chunked
        self.cancelled = False

    def response(self, rows, columns=("c",)):
        return {
            "statement_id": "st-1",
            "status": {"state": "SUCCEEDED"},
            "manifest": {"schema": {"columns": [{"name": c} for c in columns]}},
            "result": {"data_array": rows},
        }

    def __call__(self, args, timeout):
        self.calls.append(args)
        method, path = args[1], args[2]
        if method == "post" and path.endswith("/cancel"):
            self.cancelled = True
            return ""
        if method == "get" and path.startswith("/api/2.0/sql/statements/st-1/result/chunks"):
            return json.dumps({"data_array": [["c3"]]})
        if method == "get":
            self.pending_polls -= 1
            if self.pending_polls > 0:
                return json.dumps({"statement_id": "st-1", "status": {"state": "RUNNING"}})
            return json.dumps(self.response([["42"]]))
        payload = json.loads(args[args.index("--json") + 1])
        sql = payload["statement"]
        if self.pending_polls:
            return json.dumps({"statement_id": "st-1", "status": {"state": "PENDING"}})
        if "information_schema.tables" in sql:
            schemas = [s.strip(" '") for s in sql.split("IN (")[1].split(")")[0].split(",")]
            return json.dumps(self.response([r for r in CATALOG_TABLES if r[0] in schemas]))
        if "information_schema.columns" in sql:
            return json.dumps(self.response([["id", "string", "NO", None], ["name", "string", "YES", None]]))
        if "table_constraints" in sql or "referential_constraints" in sql:
            return json.dumps(self.response([]))
        if self.chunked:
            data = self.response([["c1"], ["c2"]])
            data["result"]["next_chunk_internal_link"] = "/api/2.0/sql/statements/st-1/result/chunks/1"
            return json.dumps(data)
        return json.dumps(self.response([["42"]]))

    def statements(self):
        return [json.loads(a[a.index("--json") + 1]) for a in self.calls if "--json" in a]


def test_scope_parsing():
    assert parse_table_scope(None, "sales") == {"sales": ["*"]}
    assert parse_table_scope(["orders", "crm.acc*"], "sales") == {"sales": ["orders"], "crm": ["acc*"]}
    with pytest.raises(ValueError, match="no schema"):
        parse_table_scope(["orders"], None)
    with pytest.raises(ValueError):
        parse_table_scope(["a.b.c"], "sales")
    with pytest.raises(ValueError):
        parse_table_scope(["sales.orders; DROP"], "sales")
    with pytest.raises(ValueError, match="needs 'schema'"):
        parse_table_scope(None, None)


def test_databricks_scoped_tables_across_schemas():
    fake = FakeDatabricks()
    adapter = DatabricksAdapter(
        "prod", "wh123", "main", schema="sales", tables=["orders", "order_*", "crm.accounts", "crm.missing"],
        profile="customer", runner=fake,
    )
    assert sorted(adapter.tables()) == ["crm.accounts", "order_lines", "orders"]
    assert adapter.missing_scope_entries() == ["crm.missing"]
    assert adapter.qualified("orders") == "`main`.`sales`.`orders`"
    assert adapter.qualified("crm.accounts") == "`main`.`crm`.`accounts`"
    assert adapter.row_count("crm.accounts") == 42
    assert "`main`.`crm`.`accounts`" in fake.statements()[-1]["statement"]
    with pytest.raises(UnknownObject):
        adapter.describe_table("orders_bak")  # exists but outside the scope
    with pytest.raises(UnknownObject):
        adapter.describe_table("crm.contacts")
    first = fake.statements()[0]
    assert first["warehouse_id"] == "wh123" and first["on_wait_timeout"] == "CONTINUE" and first["catalog"] == "main"
    assert all(a[-2:] == ["--profile", "customer"] for a in fake.calls)
    with pytest.raises(ReadOnlyViolation):
        adapter.run("DROP TABLE main.sales.orders")


def test_databricks_schema_less_source_requires_qualified_tables():
    fake = FakeDatabricks()
    adapter = DatabricksAdapter("prod", "wh", "main", tables=["sales.orders", "crm.accounts"], runner=fake)
    assert sorted(adapter.tables()) == ["crm.accounts", "sales.orders"]
    assert "schema" not in fake.statements()[0]


def test_databricks_validates_identifiers():
    fake = FakeDatabricks()
    with pytest.raises(ValueError):
        DatabricksAdapter("prod", "wh", "main", "sales'; DROP", runner=fake)
    with pytest.raises(ValueError):
        DatabricksAdapter("prod", "wh", "main", "sales", profile="x --host evil", runner=fake)


def test_databricks_polls_long_running_statements(monkeypatch):
    monkeypatch.setattr(databricks_adapter, "POLL_SECONDS", 0)
    fake = FakeDatabricks(pending_polls=3)
    adapter = DatabricksAdapter("prod", "wh", "main", "sales", runner=fake, query_timeout_seconds=30)
    _, rows = adapter.run("SELECT 1", 10)
    assert rows == [("42",)]
    assert [a[1] for a in fake.calls].count("get") >= 2 and not fake.cancelled


def test_databricks_cancels_after_timeout(monkeypatch):
    monkeypatch.setattr(databricks_adapter, "POLL_SECONDS", 0)
    fake = FakeDatabricks(pending_polls=10**9)
    adapter = DatabricksAdapter("prod", "wh", "main", "sales", runner=fake, query_timeout_seconds=0)
    with pytest.raises(TimeoutError, match="cancelled"):
        adapter.run("SELECT 1")
    assert fake.cancelled


def test_databricks_follows_result_chunks():
    fake = FakeDatabricks(chunked=True)
    adapter = DatabricksAdapter("prod", "wh", "main", "sales", runner=fake)
    assert adapter.run("SELECT x FROM t", 10)[1] == [("c1",), ("c2",), ("c3",)]
    assert adapter.run("SELECT x FROM t", 2)[1] == [("c1",), ("c2",)]


def test_databricks_failure_and_missing_cli(monkeypatch):
    def failed(args, timeout):
        return json.dumps({"statement_id": "s", "status": {"state": "FAILED", "error": {"message": "TABLE_OR_VIEW_NOT_FOUND"}}})

    adapter = DatabricksAdapter("prod", "wh", "main", "sales", runner=failed)
    with pytest.raises(databricks_adapter.DatabricksError, match="TABLE_OR_VIEW_NOT_FOUND"):
        adapter.run("SELECT 1")
    monkeypatch.setattr(databricks_adapter.shutil, "which", lambda _: None)
    adapter = DatabricksAdapter("prod", "wh", "main", "sales", cli_path="databricks")
    with pytest.raises(databricks_adapter.DatabricksError, match="not found"):
        adapter.run("SELECT 1")


def test_sqlite_table_scope(settings):
    adapter = SQLiteAdapter("retail", settings.workspace_dir / "data/retail.sqlite", table_patterns=["cust_*", "TBL_ORD_*"])
    assert sorted(adapter.tables()) == ["CUST_MASTER", "cust_master_bak", "tbl_ord_hdr"]  # case-insensitive globs
    adapter.close()
    adapter = SQLiteAdapter("retail", settings.workspace_dir / "data/retail.sqlite", table_patterns=["CUST_MASTER"])
    assert list(adapter.tables()) == ["CUST_MASTER"]
    with pytest.raises(UnknownObject):
        adapter.describe_table("cust_master_bak")
    adapter.close()


def test_factory_validates_source_options(settings):
    from ddg.config import SourceConfig

    with pytest.raises(ValueError, match="need warehouse_id"):
        create_adapter(settings, SourceConfig("p", "databricks", {"catalog": "main", "schema": "s"}))
    with pytest.raises(ValueError, match="list of strings"):
        create_adapter(settings, SourceConfig("p", "sqlite", {"path": "data/retail.sqlite", "tables": "orders"}))


def test_cli_sources_check(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    path = project / "workspaces" / "retail" / "workspace.toml"
    path.write_text(path.read_text(encoding="utf-8").replace(
        'path = "data/retail.sqlite"', 'path = "data/retail.sqlite"\ntables = ["CUST_MASTER", "tbl_ord_hdr"]'
    ), encoding="utf-8")
    assert main(["sources", "check"]) == 0
    out = capsys.readouterr().out
    assert "2 table(s) in scope" in out and "- CUST_MASTER (5 columns)" in out and "@" not in out
