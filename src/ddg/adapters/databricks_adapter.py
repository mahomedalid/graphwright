"""Databricks adapter that executes guarded statements through the Databricks CLI.

Uses the SQL Statement Execution API via ``databricks api post /api/2.0/sql/statements``
so authentication is delegated to the user's configured CLI profile. Configure a
principal with read-only (``SELECT`` / ``USE``) grants; the adapter additionally
refuses any non-SELECT statement before dispatch.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any, Callable

from .base import SourceAdapter

IDENT_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


class DatabricksError(RuntimeError):
    pass


def _cli_runner(cli_path: str) -> Callable[[list[str], int], str]:
    def run(args: list[str], timeout: int) -> str:
        executable = shutil.which(cli_path) or cli_path
        completed = subprocess.run(
            [executable, *args], capture_output=True, text=True, timeout=timeout, check=False
        )
        if completed.returncode != 0:
            raise DatabricksError(f"databricks CLI failed: {completed.stderr.strip()[:500]}")
        return completed.stdout

    return run


class DatabricksAdapter(SourceAdapter):
    dialect = "databricks"
    quote_char = "`"

    def __init__(
        self,
        name: str,
        warehouse_id: str,
        catalog: str,
        schema: str,
        profile: str | None = None,
        cli_path: str = "databricks",
        query_timeout_seconds: int = 30,
        runner: Callable[[list[str], int], str] | None = None,
    ):
        super().__init__(name, query_timeout_seconds)
        for label, value in (("warehouse_id", warehouse_id), ("catalog", catalog), ("schema", schema)):
            if not value or not IDENT_RE.match(value):
                raise ValueError(f"Databricks {label} must be a simple identifier, got {value!r}")
        self.warehouse_id = warehouse_id
        self.catalog = catalog
        self.schema = schema
        self.profile = profile
        self._runner = runner or _cli_runner(cli_path)

    def qualified(self, table: str) -> str:
        return f"{self.quote(self.catalog)}.{self.quote(self.schema)}.{self.quote(table)}"

    def _execute(self, sql: str, max_rows: int) -> tuple[list[str], list[tuple]]:
        wait = max(5, min(50, int(self.query_timeout_seconds)))
        payload = {
            "warehouse_id": self.warehouse_id,
            "statement": sql,
            "catalog": self.catalog,
            "schema": self.schema,
            "wait_timeout": f"{wait}s",
            "on_wait_timeout": "CANCEL",
            "disposition": "INLINE",
            "format": "JSON_ARRAY",
            "row_limit": int(max_rows),
        }
        args = ["api", "post", "/api/2.0/sql/statements", "--json", json.dumps(payload)]
        if self.profile:
            args += ["--profile", self.profile]
        output = self._runner(args, wait + 15)
        response: dict[str, Any] = json.loads(output)
        state = response.get("status", {}).get("state")
        if state != "SUCCEEDED":
            error = response.get("status", {}).get("error", {}).get("message", "")
            if state in ("CANCELED", "PENDING", "RUNNING"):
                raise TimeoutError(f"Databricks statement did not finish within {wait}s ({state})")
            raise DatabricksError(f"Statement {state}: {error}")
        columns = [c["name"] for c in response.get("manifest", {}).get("schema", {}).get("columns", [])]
        rows = [tuple(r) for r in response.get("result", {}).get("data_array", []) or []]
        return columns, rows[:max_rows]

    def _load_tables(self) -> list[dict]:
        _, rows = self.run(
            f"SELECT table_name, table_type, comment FROM {self.quote(self.catalog)}.information_schema.tables "
            f"WHERE table_schema = '{self.schema}' ORDER BY table_name",
            10_000,
        )
        return [{"name": r[0], "type": (r[1] or "TABLE").lower(), "comment": r[2]} for r in rows]

    def _load_columns(self, table: str) -> list[dict]:
        safe_table = table.replace("'", "''")
        _, rows = self.run(
            f"SELECT column_name, data_type, is_nullable, comment FROM {self.quote(self.catalog)}.information_schema.columns "
            f"WHERE table_schema = '{self.schema}' AND table_name = '{safe_table}' ORDER BY ordinal_position",
            10_000,
        )
        pks = self._primary_keys(table)
        return [
            {
                "name": r[0],
                "type": r[1],
                "nullable": str(r[2]).upper() == "YES",
                "primary_key": r[0] in pks,
                "comment": r[3],
            }
            for r in rows
        ]

    def _primary_keys(self, table: str) -> set[str]:
        safe_table = table.replace("'", "''")
        try:
            _, rows = self.run(
                f"SELECT k.column_name FROM {self.quote(self.catalog)}.information_schema.table_constraints c "
                f"JOIN {self.quote(self.catalog)}.information_schema.key_column_usage k "
                f"ON c.constraint_name = k.constraint_name AND c.table_schema = k.table_schema "
                f"WHERE c.constraint_type = 'PRIMARY KEY' AND c.table_schema = '{self.schema}' AND c.table_name = '{safe_table}'",
                1000,
            )
            return {r[0] for r in rows}
        except DatabricksError:
            return set()

    def _load_foreign_keys(self, table: str) -> list[dict] | None:
        safe_table = table.replace("'", "''")
        try:
            _, rows = self.run(
                f"SELECT k.column_name, u.table_name, u.column_name "
                f"FROM {self.quote(self.catalog)}.information_schema.referential_constraints r "
                f"JOIN {self.quote(self.catalog)}.information_schema.key_column_usage k ON r.constraint_name = k.constraint_name "
                f"JOIN {self.quote(self.catalog)}.information_schema.key_column_usage u ON r.unique_constraint_name = u.constraint_name "
                f"WHERE k.table_schema = '{self.schema}' AND k.table_name = '{safe_table}'",
                1000,
            )
            return [{"column": r[0], "references_table": r[1], "references_column": r[2]} for r in rows]
        except DatabricksError:
            return None
