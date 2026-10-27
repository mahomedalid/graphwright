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


@dataclass
class IterationResult:
    summary: str
    memory: dict
    done: bool = False


class Planner(Protocol):
    name: str

    def run_iteration(self, ctx: IterationContext, tools: DiscoveryTools) -> IterationResult: ...
