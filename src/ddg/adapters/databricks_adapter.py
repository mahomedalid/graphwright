"""Databricks adapter that executes guarded statements through the Databricks CLI.

Uses the SQL Statement Execution API via ``databricks api`` so authentication is delegated
to the user's CLI profile (``databricks auth login``). Configure a principal with read-only
(``USE CATALOG``/``USE SCHEMA``/``SELECT``) grants; the adapter additionally refuses any
non-SELECT statement before dispatch.

Scope: ``catalog`` plus either a default ``schema`` and/or a ``tables`` list. Each ``tables``
entry is ``table`` (in the default schema) or ``schema.table``; ``*`` globs are allowed in
the table part. Tables outside the scope cannot be described, sampled, or joined.
"""

from __future__ import annotations

import fnmatch
import json
import re
import shutil
import subprocess
import time
from typing import Any, Callable

from .base import SourceAdapter, UnknownObject

IDENT_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
PATTERN_RE = re.compile(r"^[A-Za-z0-9_\-*?]+$")
POLL_SECONDS = 2.0


class DatabricksError(RuntimeError):
    pass


def _cli_runner(cli_path: str) -> Callable[[list[str], int], str]:
    def run(args: list[str], timeout: int) -> str:
        executable = shutil.which(cli_path)
        if executable is None:
            raise DatabricksError(
                f"Databricks CLI '{cli_path}' not found. Install it (https://docs.databricks.com/dev-tools/cli/install) "
                "or set cli_path in workspace.toml."
            )
        completed = subprocess.run([executable, *args], capture_output=True, text=True, timeout=timeout, check=False)
        if completed.returncode != 0:
            raise DatabricksError(f"databricks CLI failed: {completed.stderr.strip()[:500]}")
        return completed.stdout

    return run


def parse_table_scope(tables: list[str] | None, default_schema: str | None) -> dict[str, list[str]]:
    """Map schema -> table glob patterns. No list means every table in the default schema."""
    if not tables:
        if not default_schema:
            raise ValueError("Databricks source needs 'schema' or a 'tables' list")
        return {default_schema: ["*"]}
    scope: dict[str, list[str]] = {}
    for entry in tables:
        parts = str(entry).strip().split(".")
        if len(parts) == 1:
            if not default_schema:
                raise ValueError(f"Table '{entry}' has no schema; use 'schema.table' or set 'schema'")
            schema, pattern = default_schema, parts[0]
        elif len(parts) == 2:
            schema, pattern = parts
        else:
            raise ValueError(f"Table '{entry}' must be 'table' or 'schema.table' (the catalog is set separately)")
        if not IDENT_RE.match(schema) or not PATTERN_RE.match(pattern):
            raise ValueError(f"Invalid table scope entry '{entry}'")
        scope.setdefault(schema, []).append(pattern)
    return scope


class DatabricksAdapter(SourceAdapter):
    dialect = "databricks"
    quote_char = "`"

    def __init__(
        self,
        name: str,
        warehouse_id: str,
        catalog: str,
        schema: str | None = None,
        tables: list[str] | None = None,
        profile: str | None = None,
        cli_path: str = "databricks",
        query_timeout_seconds: int = 30,
        runner: Callable[[list[str], int], str] | None = None,
    ):
        super().__init__(name, query_timeout_seconds)
        for label, value in (("warehouse_id", warehouse_id), ("catalog", catalog)):
            if not value or not IDENT_RE.match(value):
                raise ValueError(f"Databricks {label} must be a simple identifier, got {value!r}")
        if schema is not None and not IDENT_RE.match(schema):
            raise ValueError(f"Databricks schema must be a simple identifier, got {schema!r}")
        if profile is not None and not IDENT_RE.match(profile):
            raise ValueError(f"Databricks profile must be a simple name, got {profile!r}")
        self.warehouse_id = warehouse_id
        self.catalog = catalog
        self.schema = schema
        self.scope = parse_table_scope(tables, schema)
        self.profile = profile
        self._runner = runner or _cli_runner(cli_path)

    # ---- naming: tables in the default schema keep bare names, others are "schema.table" ----
    def _display_name(self, schema: str, table: str) -> str:
        return table if schema == self.schema else f"{schema}.{table}"

    def _split(self, name: str) -> tuple[str, str]:
        if "." in name:
            schema, table = name.split(".", 1)
            return schema, table
        if not self.schema:
            raise UnknownObject(f"Table '{name}' needs a schema prefix")
        return self.schema, name

    def qualified(self, table: str) -> str:
        schema, name = self._split(table)
        return f"{self.quote(self.catalog)}.{self.quote(schema)}.{self.quote(name)}"

    def _info_schema(self, view: str) -> str:
        return f"{self.quote(self.catalog)}.information_schema.{view}"

    @staticmethod
    def _lit(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    # ---- statement execution ---------------------------------------------------------
    def _api(self, method: str, path: str, payload: dict | None = None, timeout: int = 60) -> dict[str, Any]:
        args = ["api", method, path]
        if payload is not None:
            args += ["--json", json.dumps(payload)]
        if self.profile:
            args += ["--profile", self.profile]
        output = self._runner(args, timeout)
        return json.loads(output) if output.strip() else {}

    def _execute(self, sql: str, max_rows: int) -> tuple[list[str], list[tuple]]:
        deadline = time.monotonic() + self.query_timeout_seconds
        wait = max(5, min(50, int(self.query_timeout_seconds)))
        response = self._api(
            "post",
            "/api/2.0/sql/statements",
            {
                "warehouse_id": self.warehouse_id,
                "statement": sql,
                "catalog": self.catalog,
                **({"schema": self.schema} if self.schema else {}),
                "wait_timeout": f"{wait}s",
                "on_wait_timeout": "CONTINUE",
                "disposition": "INLINE",
                "format": "JSON_ARRAY",
                "row_limit": int(max_rows),
            },
            timeout=wait + 30,
        )
        statement_id = response.get("statement_id")
        while response.get("status", {}).get("state") in ("PENDING", "RUNNING"):
            if time.monotonic() >= deadline or not statement_id:
                if statement_id:
                    try:
                        self._api("post", f"/api/2.0/sql/statements/{statement_id}/cancel", timeout=30)
                    except DatabricksError:
                        pass
                raise TimeoutError(f"Databricks statement exceeded {self.query_timeout_seconds}s and was cancelled")
            time.sleep(POLL_SECONDS)
            response = self._api("get", f"/api/2.0/sql/statements/{statement_id}", timeout=60)

        state = response.get("status", {}).get("state")
        if state != "SUCCEEDED":
            error = response.get("status", {}).get("error", {}).get("message", "")
            if state == "CANCELED":
                raise TimeoutError(f"Databricks statement was cancelled: {error}")
            raise DatabricksError(f"Statement {state}: {error}")

        columns = [c["name"] for c in response.get("manifest", {}).get("schema", {}).get("columns", [])]
        result = response.get("result", {}) or {}
        rows: list[tuple] = [tuple(r) for r in result.get("data_array", []) or []]
        next_link = result.get("next_chunk_internal_link")
        while next_link and len(rows) < max_rows:
            chunk = self._api("get", next_link, timeout=60)
            rows.extend(tuple(r) for r in chunk.get("data_array", []) or [])
            next_link = chunk.get("next_chunk_internal_link")
        return columns, rows[:max_rows]

    # ---- metadata ---------------------------------------------------------------------
    def _load_tables(self) -> list[dict]:
        schemas = ", ".join(self._lit(s) for s in sorted(self.scope))
        _, rows = self.run(
            f"SELECT table_schema, table_name, table_type, comment FROM {self._info_schema('tables')} "
            f"WHERE table_schema IN ({schemas}) ORDER BY table_schema, table_name",
            10_000,
        )
        found = []
        for schema, table, kind, comment in rows:
            patterns = self.scope.get(schema, [])
            if any(fnmatch.fnmatchcase(table.lower(), p.lower()) for p in patterns):
                found.append({"name": self._display_name(schema, table), "type": (kind or "TABLE").lower(), "comment": comment})
        return found

    def missing_scope_entries(self) -> list[str]:
        """Explicit (non-glob) scope entries that do not exist or are not visible to this principal."""
        names = {n.lower() for n in self.tables()}
        missing = []
        for schema, patterns in self.scope.items():
            for pattern in patterns:
                if any(ch in pattern for ch in "*?"):
                    continue
                if self._display_name(schema, pattern).lower() not in names:
                    missing.append(f"{schema}.{pattern}")
        return missing

    def _load_columns(self, table: str) -> list[dict]:
        schema, name = self._split(table)
        _, rows = self.run(
            f"SELECT column_name, data_type, is_nullable, comment FROM {self._info_schema('columns')} "
            f"WHERE table_schema = {self._lit(schema)} AND table_name = {self._lit(name)} ORDER BY ordinal_position",
            10_000,
        )
        pks = self._primary_keys(schema, name)
        return [
            {"name": r[0], "type": r[1], "nullable": str(r[2]).upper() == "YES", "primary_key": r[0] in pks, "comment": r[3]}
            for r in rows
        ]

    def _primary_keys(self, schema: str, table: str) -> set[str]:
        try:
            _, rows = self.run(
                f"SELECT k.column_name FROM {self._info_schema('table_constraints')} c "
                f"JOIN {self._info_schema('key_column_usage')} k "
                f"ON c.constraint_name = k.constraint_name AND c.table_schema = k.table_schema "
                f"WHERE c.constraint_type = 'PRIMARY KEY' AND c.table_schema = {self._lit(schema)} AND c.table_name = {self._lit(table)}",
                1000,
            )
            return {r[0] for r in rows}
        except DatabricksError:
            return set()

    def _load_foreign_keys(self, table: str) -> list[dict] | None:
        schema, name = self._split(table)
        try:
            _, rows = self.run(
                f"SELECT k.column_name, u.table_schema, u.table_name, u.column_name "
                f"FROM {self._info_schema('referential_constraints')} r "
                f"JOIN {self._info_schema('key_column_usage')} k ON r.constraint_name = k.constraint_name "
                f"JOIN {self._info_schema('key_column_usage')} u ON r.unique_constraint_name = u.constraint_name "
                f"WHERE k.table_schema = {self._lit(schema)} AND k.table_name = {self._lit(name)}",
                1000,
            )
            return [
                {"column": r[0], "references_table": self._display_name(r[1], r[2]), "references_column": r[3]}
                for r in rows
            ]
        except DatabricksError:
            return None
