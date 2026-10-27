"""Source adapter factory."""

from __future__ import annotations

from ..config import Settings, SourceConfig
from .base import ReadOnlyViolation, SourceAdapter, UnknownObject, ensure_read_only
from .databricks_adapter import DatabricksAdapter
from .sqlite_adapter import SQLiteAdapter

__all__ = [
    "DatabricksAdapter",
    "ReadOnlyViolation",
    "SQLiteAdapter",
    "SourceAdapter",
    "UnknownObject",
    "create_adapter",
    "ensure_read_only",
]


def create_adapter(settings: Settings, source: SourceConfig) -> SourceAdapter:
    timeout = int(source.options.get("query_timeout_seconds", settings.limits.query_timeout_seconds))
    if source.type == "sqlite":
        path = settings.workspace_dir / source.options["path"]
        return SQLiteAdapter(source.name, path, query_timeout_seconds=timeout)
    if source.type == "databricks":
        return DatabricksAdapter(
            source.name,
            warehouse_id=source.options["warehouse_id"],
            catalog=source.options["catalog"],
            schema=source.options["schema"],
            profile=source.options.get("profile"),
            cli_path=source.options.get("cli_path", "databricks"),
            query_timeout_seconds=timeout,
        )
    raise ValueError(f"Unsupported source type '{source.type}' for source '{source.name}' (expected sqlite or databricks)")
