"""Read-only SQLite adapter for local development."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .base import SourceAdapter


class SQLiteAdapter(SourceAdapter):
    dialect = "sqlite"
    quote_char = '"'

    def __init__(self, name: str, path: str | Path, query_timeout_seconds: int = 30, table_patterns: list[str] | None = None):
        super().__init__(name, query_timeout_seconds, table_patterns)
        self.path = Path(path).resolve()
        if not self.path.exists():
            raise FileNotFoundError(f"SQLite database not found: {self.path}")
        self._connection_lock = threading.Lock()
        # mode=ro opens the file read-only at the OS level; query_only blocks writes in SQLite itself.
        self._conn = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True, check_same_thread=False)
        self._conn.execute("PRAGMA query_only = ON")

    def _execute(self, sql: str, max_rows: int) -> tuple[list[str], list[tuple]]:
        # Enter SQLite only after acquiring this lock: progress callbacks need the GIL.
        with self._connection_lock:
            deadline = time.monotonic() + self.query_timeout_seconds
            self._conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
            try:
                cursor = self._conn.execute(sql)
                try:
                    rows = cursor.fetchmany(max_rows)
                    columns = [d[0] for d in cursor.description or []]
                    return columns, rows
                finally:
                    cursor.close()
            except sqlite3.OperationalError as exc:
                if "interrupted" in str(exc).lower():
                    raise TimeoutError(f"Query exceeded {self.query_timeout_seconds}s timeout") from exc
                raise
            finally:
                self._conn.set_progress_handler(None, 0)

    def _load_tables(self) -> list[dict]:
        with self._connection_lock:
            cursor = self._conn.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            try:
                return [{"name": name, "type": kind, "comment": None} for name, kind in cursor.fetchall()]
            finally:
                cursor.close()

    def _load_columns(self, table: str) -> list[dict]:
        with self._connection_lock:
            cursor = self._conn.execute(f"PRAGMA table_info({self.quote(table)})")
            try:
                return [
                    {"name": row[1], "type": row[2] or "", "nullable": not row[3], "primary_key": bool(row[5]), "comment": None}
                    for row in cursor.fetchall()
                ]
            finally:
                cursor.close()

    def _load_foreign_keys(self, table: str) -> list[dict]:
        with self._connection_lock:
            cursor = self._conn.execute(f"PRAGMA foreign_key_list({self.quote(table)})")
            try:
                return [{"column": row[3], "references_table": row[2], "references_column": row[4]} for row in cursor.fetchall()]
            finally:
                cursor.close()

    def close(self) -> None:
        with self._connection_lock:
            self._conn.close()
