"""Privacy enforcement applied to every tool result before it can reach a model (NFR-002)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import SamplingPolicy


@dataclass
class FilteredSample:
    columns: list[str]
    rows: list[list[Any]]
    blocked_columns: list[str]
    requested_rows: int
    returned_rows: int
    row_limit: int

    def to_dict(self) -> dict:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "blocked_columns": self.blocked_columns,
            "requested_rows": self.requested_rows,
            "returned_rows": self.returned_rows,
            "row_limit": self.row_limit,
            "note": (
                "Columns not on the sampling allowlist were blocked; use profile_column for aggregate metadata."
                if self.blocked_columns
                else ""
            ),
        }


class PrivacyGuard:
    def __init__(self, policy: SamplingPolicy):
        self.policy = policy

    def split_columns(self, table: str, columns: list[str]) -> tuple[list[str], list[str]]:
        allowed = [c for c in columns if self.policy.is_allowed(table, c)]
        blocked = [c for c in columns if not self.policy.is_allowed(table, c)]
        return allowed, blocked

    def clamp_rows(self, requested: int) -> int:
        return max(0, min(int(requested), self.policy.max_rows))

    def filter_sample(self, table: str, columns: list[str], rows: list[tuple], requested_rows: int) -> FilteredSample:
        allowed_idx = [i for i, c in enumerate(columns) if self.policy.is_allowed(table, c)]
        blocked = [c for c in columns if not self.policy.is_allowed(table, c)]
        limit = self.clamp_rows(requested_rows)
        kept = [[_jsonable(row[i]) for i in allowed_idx] for row in rows[:limit]]
        return FilteredSample(
            columns=[columns[i] for i in allowed_idx],
            rows=kept,
            blocked_columns=blocked,
            requested_rows=int(requested_rows),
            returned_rows=len(kept),
            row_limit=self.policy.max_rows,
        )

    def value_or_redacted(self, table: str, column: str, value: Any) -> Any:
        return _jsonable(value) if self.policy.is_allowed(table, column) else "<redacted>"


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return str(value)
