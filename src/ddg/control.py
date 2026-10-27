"""Run budgets and cancellation checks enforced before every tool or model dispatch (NFR-004)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .config import ModelSettings, RunLimits


class StopRun(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass
class Usage:
    tool_calls: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    queries: int = 0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class RunControl:
    limits: RunLimits
    model: ModelSettings
    cancel_requested: Callable[[], bool]
    usage: Usage = field(default_factory=Usage)
    started: float = field(default_factory=time.monotonic)

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started

    def check(self) -> None:
        if self.cancel_requested():
            raise StopRun("cancelled")
        if self.elapsed_seconds >= self.limits.max_minutes * 60:
            raise StopRun("time_limit")
        if self.usage.cost_usd >= self.limits.max_cost_usd:
            raise StopRun("cost_limit")
        if self.usage.tool_calls >= self.limits.max_tool_calls:
            raise StopRun("tool_call_limit")
        if self.usage.model_calls >= self.limits.max_model_calls:
            raise StopRun("model_call_limit")

    def before_tool(self) -> None:
        self.check()
        self.usage.tool_calls += 1

    def before_model_call(self) -> None:
        self.check()
        self.usage.model_calls += 1

    def record_tokens(self, input_tokens: int, output_tokens: int) -> None:
        self.usage.input_tokens += int(input_tokens or 0)
        self.usage.output_tokens += int(output_tokens or 0)
        self.usage.cost_usd = round(
            self.usage.input_tokens / 1000 * self.model.input_cost_per_1k
            + self.usage.output_tokens / 1000 * self.model.output_cost_per_1k,
            6,
        )

    def remaining_seconds(self) -> float:
        return max(0.0, self.limits.max_minutes * 60 - self.elapsed_seconds)
