"""Planner interface: one planner call performs one discovery iteration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..tools import DiscoveryTools
from ..topology import Topology


@dataclass
class IterationContext:
    iteration: int
    source: str
    topology: Topology
    snapshot: dict
    memory: dict = field(default_factory=dict)
    context_documents: list[dict[str, str]] = field(default_factory=list)


@dataclass
class IterationResult:
    summary: str
    memory: dict
    done: bool = False
    story: str | None = None  # short plain-language narrative for humans; summary keeps the full detail


class Planner(Protocol):
    name: str

    def run_iteration(self, ctx: IterationContext, tools: DiscoveryTools) -> IterationResult: ...
