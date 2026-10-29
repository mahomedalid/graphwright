"""Profiling planner: inventories every table in scope and profiles columns that lack statistics.

It proposes no candidates; it fills the data dictionary with column profiles (null rates,
distinct counts, value shapes, and top values for allowlisted columns) while respecting
run budgets, cancellation, and checkpoint/resume like any other run.
"""

from __future__ import annotations

from ..dictionary import collect_profiles
from ..tools import DiscoveryTools
from .base import IterationContext, IterationResult

MAX_DESCRIBE_PER_ITERATION = 40
MAX_COLUMNS_PER_ITERATION = 60


class ProfilePlanner:
    name = "profile"

    def run_iteration(self, ctx: IterationContext, tools: DiscoveryTools) -> IterationResult:
        memory = dict(ctx.memory)
        listing = tools.list_tables()
        all_tables = [t["table"] for t in listing.get("tables", [])]
        missing = [t for t in all_tables if t not in tools.inventory]
        if missing:
            batch = missing[:MAX_DESCRIBE_PER_ITERATION]
            for table in batch:
                tools.describe_table(table)
            return IterationResult(f"Inventoried {len(batch)} of {len(all_tables)} tables.", memory)

        profiled = set(collect_profiles(tools.settings, tools.adapter.name))
        failed = {tuple(x) for x in memory.get("failed", [])}
        todo = [
            (table, col["name"])
            for table in sorted(tools.inventory, key=str.lower)
            for col in tools.inventory[table].get("columns", [])
            if (table, col["name"]) not in profiled and (table, col["name"]) not in failed
        ]
        if not todo:
            return IterationResult(f"All columns in {len(all_tables)} tables are profiled.", memory, done=True)

        done = 0
        for table, column in todo[:MAX_COLUMNS_PER_ITERATION]:
            result = tools.profile_column(table, column)
            if "error" in result:
                failed.add((table, column))
            else:
                done += 1
        memory["failed"] = sorted(failed)
        remaining = len(todo) - min(len(todo), MAX_COLUMNS_PER_ITERATION)
        return IterationResult(
            f"Profiled {done} columns ({remaining} remaining{f', {len(failed)} failed' if failed else ''}).",
            memory,
            done=remaining == 0,
        )
