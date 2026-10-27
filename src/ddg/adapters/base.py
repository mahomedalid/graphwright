"""Source adapter contract shared by SQLite and Databricks (FR-002, NFR-001).

Agents never send raw SQL. Adapters build every statement from identifiers that
are validated against the discovered inventory, and every statement passes the
read-only guard before execution.
"""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from typing import Any

FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|MERGE|TRUNCATE|ATTACH|DETACH|PRAGMA|VACUUM|GRANT|REVOKE|COPY|OPTIMIZE|INTO|CALL|EXEC|EXECUTE|SET|USE|LOAD|REFRESH|MSCK|CACHE|UNCACHE)\b",
    re.IGNORECASE,
)


class ReadOnlyViolation(Exception):
    pass


class UnknownObject(Exception):
    pass


def ensure_read_only(sql: str) -> str:
    """Reject anything other than a single SELECT/WITH statement."""
    without_literals = re.sub(r"'(?:''|[^'])*'", "''", sql)
    without_literals = re.sub(r'"(?:""|[^"])*"', '""', without_literals)
    without_literals = re.sub(r"`(?:``|[^`])*`", "``", without_literals)
    without_comments = re.sub(r"--[^\n]*|/\*.*?\*/", " ", without_literals, flags=re.S)
    body = without_comments.strip().rstrip(";").strip()
    if ";" in body:
        raise ReadOnlyViolation("Multiple statements are not allowed")
    first = body.split(None, 1)[0].upper() if body else ""
    if first not in ("SELECT", "WITH"):
        raise ReadOnlyViolation(f"Only SELECT/WITH statements are allowed, got '{first or 'empty'}'")
    match = FORBIDDEN.search(body)
    if match:
        raise ReadOnlyViolation(f"Forbidden keyword '{match.group(1)}' in read-only query")
    return sql


def value_shape(value: Any) -> str:
    """Summarize a value's format without revealing it (letters->A, digits->9)."""
    if value is None:
        return "<null>"
    text = str(value)
    shape = re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "9", text))
    shape = re.sub(r"A{4,}", "A+", shape)
    shape = re.sub(r"9{6,}", "9+", shape)
    return shape[:24]


class SourceAdapter(ABC):
    """Common discovery operations; subclasses implement dialect-specific execution."""

    dialect: str = "generic"
    quote_char: str = '"'

    def __init__(self, name: str, query_timeout_seconds: int = 30):
        self.name = name
        self.query_timeout_seconds = query_timeout_seconds
        self._tables: dict[str, dict] | None = None
        self._columns: dict[str, list[dict]] = {}
        self.query_count = 0

    # ---- dialect hooks ------------------------------------------------
    @abstractmethod
    def _execute(self, sql: str, max_rows: int) -> tuple[list[str], list[tuple]]:
        """Execute an already-guarded read-only statement."""

    @abstractmethod
    def _load_tables(self) -> list[dict]:
        """Return [{name, type, comment}] for the configured scope."""

    @abstractmethod
    def _load_columns(self, table: str) -> list[dict]:
        """Return [{name, type, nullable, primary_key, comment}]."""

    def _load_foreign_keys(self, table: str) -> list[dict] | None:
        """Declared foreign keys, or None when the source cannot report them."""
        return None

    def qualified(self, table: str) -> str:
        return self.quote(table)

    def close(self) -> None:  # pragma: no cover - optional
        pass

    # ---- helpers -------------------------------------------------------
    def quote(self, identifier: str) -> str:
        q = self.quote_char
        return f"{q}{identifier.replace(q, q * 2)}{q}"

    def run(self, sql: str, max_rows: int = 1000) -> tuple[list[str], list[tuple]]:
        ensure_read_only(sql)
        self.query_count += 1
        return self._execute(sql, max_rows)

    def resolve_table(self, table: str) -> str:
        tables = self.tables()
        if table in tables:
            return table
        lowered = {t.lower(): t for t in tables}
        if table.lower() in lowered:
            return lowered[table.lower()]
        raise UnknownObject(f"Unknown table '{table}'. Use list_tables to see available tables.")

    def resolve_column(self, table: str, column: str) -> str:
        names = [c["name"] for c in self.columns(table)]
        if column in names:
            return column
        lowered = {n.lower(): n for n in names}
        if column.lower() in lowered:
            return lowered[column.lower()]
        raise UnknownObject(f"Unknown column '{column}' in table '{table}'. Columns: {', '.join(names)}")

    # ---- discovery operations -------------------------------------------
    def tables(self) -> dict[str, dict]:
        if self._tables is None:
            self._tables = {t["name"]: t for t in self._load_tables()}
        return self._tables

    def columns(self, table: str) -> list[dict]:
        table = self.resolve_table(table)
        if table not in self._columns:
            self._columns[table] = self._load_columns(table)
        return self._columns[table]

    def row_count(self, table: str) -> int:
        table = self.resolve_table(table)
        _, rows = self.run(f"SELECT COUNT(*) FROM {self.qualified(table)}", 1)
        return int(rows[0][0])

    def describe_table(self, table: str) -> dict:
        table = self.resolve_table(table)
        columns = self.columns(table)
        fks = self._load_foreign_keys(table)
        return {
            "table": table,
            "type": self.tables()[table].get("type", "table"),
            "comment": self.tables()[table].get("comment"),
            "row_count": self.row_count(table),
            "columns": columns,
            "foreign_keys": fks if fks is not None else "unsupported by source",
            "schema_fingerprint": self.schema_fingerprint(table),
        }

    def schema_fingerprint(self, table: str) -> str:
        cols = [(c["name"], str(c.get("type"))) for c in self.columns(table)]
        return hashlib.sha256(repr(cols).encode()).hexdigest()[:12]

    def column_stats(self, table: str, column: str, shape_sample: int = 200) -> dict:
        table = self.resolve_table(table)
        column = self.resolve_column(table, column)
        t, c = self.qualified(table), self.quote(column)
        _, rows = self.run(
            f"SELECT COUNT(*), SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END), COUNT(DISTINCT {c}), "
            f"MIN(LENGTH(CAST({c} AS STRING))), MAX(LENGTH(CAST({c} AS STRING))) FROM {t}"
            if self.dialect == "databricks"
            else f"SELECT COUNT(*), SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END), COUNT(DISTINCT {c}), "
            f"MIN(LENGTH(CAST({c} AS TEXT))), MAX(LENGTH(CAST({c} AS TEXT))) FROM {t}",
            1,
        )
        total, nulls, distinct, min_len, max_len = rows[0]
        _, sample = self.run(f"SELECT {c} FROM {t} WHERE {c} IS NOT NULL LIMIT {int(shape_sample)}", shape_sample)
        shapes: dict[str, int] = {}
        for (value,) in sample:
            shape = value_shape(value)
            shapes[shape] = shapes.get(shape, 0) + 1
        top_shapes = sorted(shapes.items(), key=lambda kv: -kv[1])[:5]
        total = int(total or 0)
        distinct = int(distinct or 0)
        return {
            "table": table,
            "column": column,
            "row_count": total,
            "null_count": int(nulls or 0),
            "distinct_count": distinct,
            "unique": total > 0 and int(nulls or 0) == 0 and distinct == total,
            "min_length": min_len,
            "max_length": max_len,
            "value_shapes": [{"shape": s, "count": n} for s, n in top_shapes],
            "shape_sample_size": len(sample),
        }

    def top_values(self, table: str, column: str, limit: int) -> list[tuple[Any, int]]:
        table = self.resolve_table(table)
        column = self.resolve_column(table, column)
        c = self.quote(column)
        _, rows = self.run(
            f"SELECT {c}, COUNT(*) AS n FROM {self.qualified(table)} GROUP BY {c} ORDER BY n DESC LIMIT {int(limit)}",
            limit,
        )
        return [(r[0], int(r[1])) for r in rows]

    def sample_rows(self, table: str, columns: list[str], limit: int) -> tuple[list[str], list[tuple]]:
        table = self.resolve_table(table)
        resolved = [self.resolve_column(table, c) for c in columns]
        cols = ", ".join(self.quote(c) for c in resolved)
        return self.run(f"SELECT {cols} FROM {self.qualified(table)} LIMIT {int(limit)}", limit)

    def key_counts(self, table: str, columns: list[str], limit: int) -> tuple[list[tuple], bool]:
        """Distinct key tuples with row counts; ``complete`` is False when truncated."""
        table = self.resolve_table(table)
        resolved = [self.resolve_column(table, c) for c in columns]
        cols = ", ".join(self.quote(c) for c in resolved)
        _, rows = self.run(
            f"SELECT {cols}, COUNT(*) FROM {self.qualified(table)} GROUP BY {cols} LIMIT {int(limit) + 1}",
            limit + 1,
        )
        complete = len(rows) <= limit
        return [tuple(r) for r in rows[:limit]], complete
