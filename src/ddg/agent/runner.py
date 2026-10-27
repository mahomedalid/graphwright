"""Run orchestration: manual invocation, autonomous iterations, budgets, checkpoint/resume (FR-003, FR-007)."""

from __future__ import annotations

import time
import traceback
from typing import Any

from ..adapters import create_adapter
from ..config import Settings
from ..control import RunControl, StopRun
from ..store import Store, atomic_write_json, new_id, utcnow
from ..tools import DiscoveryTools
from ..topology import TopologyError, load_topology
from .base import IterationContext, Planner
from .heuristic import HeuristicPlanner

ACTIVE_STATES = ("queued", "running", "stopping")
STALE_AFTER_SECONDS = 900


def choose_planner(settings: Settings, requested: str | None, control: RunControl) -> Planner:
    name = (requested or settings.planner or "auto").lower()
    if name == "auto":
        name = "foundry" if settings.model.configured else "heuristic"
    if name == "heuristic":
        return HeuristicPlanner()
    if name == "foundry":
        from .foundry_planner import FoundryPlanner

        return FoundryPlanner(settings, control)
    raise ValueError(f"Unknown planner '{name}' (expected auto, foundry, or heuristic)")


class DiscoveryRunner:
    def __init__(self, settings: Settings, store: Store | None = None):
        self.settings = settings
        self.store = store or Store(settings)

    def create_run(
        self,
        source: str,
        planner: str | None = None,
        limits: dict[str, Any] | None = None,
        resume_from: str | None = None,
    ) -> dict:
        self.settings.source(source)
        if resume_from:
            previous = self.store.get_run(resume_from)
            if previous is None:
                raise KeyError(f"Run {resume_from} not found")
            if previous["status"] in ACTIVE_STATES:
                raise ValueError(f"Run {resume_from} is still {previous['status']}; cancel it before resuming")
            if self.store.load_checkpoint(resume_from) is None:
                raise ValueError(f"Run {resume_from} has no checkpoint to resume from")
            if previous["source"] != source:
                raise ValueError("A resumed run must use the same source")
        merged = self.settings.limits.merged(limits)
        run = {
            "id": new_id("run"),
            "created_at": utcnow(),
            "status": "queued",
            "source": source,
            "planner": planner or self.settings.planner,
            "limits": merged.__dict__,
            "resumed_from": resume_from,
            "iterations": [],
            "usage": {},
            "termination_reason": None,
            "error": None,
        }
        self.store.save_run(run)
        self.store.append_event(run["id"], {"type": "run_created", "source": source, "resumed_from": resume_from})
        return run

    def execute(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        limits = self.settings.limits.merged(run["limits"])
        control = RunControl(limits, self.settings.model, cancel_requested=lambda: self.store.cancel_requested(run_id))
        adapter = None
        checkpoint: dict = {"memory": {}, "evidence_index": {}, "iteration": 0}
        try:
            topology = load_topology(self.settings.topology_file, strict=True)
            run["topology_version"] = topology.version
            self.store.reconcile()
            source_cfg = self.settings.source(run["source"])
            adapter = create_adapter(self.settings, source_cfg)
            if run.get("resumed_from"):
                previous = self.store.load_checkpoint(run["resumed_from"]) or {}
                checkpoint.update({k: previous.get(k, checkpoint[k]) for k in checkpoint})
                if previous.get("topology_version") and previous["topology_version"] != topology.version:
                    self.store.append_event(run_id, {"type": "warning", "message": "Topology changed since checkpoint; earlier notes may be stale"})
            planner = choose_planner(self.settings, run.get("planner"), control)
            run["planner"] = planner.name
            tools = DiscoveryTools(
                self.settings, self.store, adapter, topology, control, run_id, checkpoint["evidence_index"]
            )
            run.update({"status": "running", "started_at": utcnow()})
            self.store.save_run(run)
            self.store.append_event(run_id, {"type": "run_started", "planner": planner.name, "topology_version": topology.version})

            memory = checkpoint["memory"]
            iteration = int(checkpoint["iteration"])
            self._checkpoint(run, topology.version, memory, tools, iteration)
            stalled = 0
            reason = None
            while reason is None:
                control.check()
                iteration += 1
                tools.iteration = iteration
                snapshot = self.store.review_snapshot()
                proposed_before = len(tools.proposed)
                evidence_before = len(tools.evidence_index)
                started = time.monotonic()
                self.store.append_event(run_id, {"type": "iteration_started", "iteration": iteration, "review_snapshot_version": snapshot["version"]})
                ctx = IterationContext(iteration, run["source"], topology, snapshot, memory)
                try:
                    result = planner.run_iteration(ctx, tools)
                finally:
                    new_ids = [c["id"] for c in tools.proposed[proposed_before:]]
                    record = {
                        "iteration": iteration,
                        "review_snapshot_version": snapshot["version"],
                        "new_candidates": new_ids,
                        "proposal_errors": tools.proposal_errors[-5:],
                        "duration_seconds": round(time.monotonic() - started, 2),
                        "usage": control.usage.to_dict(),
                    }
                    tools.proposal_errors.clear()
                    run["iterations"].append(record)
                    run["usage"] = control.usage.to_dict()
                    self._checkpoint(run, topology.version, memory, tools, iteration)
                memory = result.memory
                record["summary"] = result.summary
                self._checkpoint(run, topology.version, memory, tools, iteration)
                self.store.append_event(run_id, {"type": "iteration_completed", "iteration": iteration, "summary": result.summary[:500], "new_candidates": new_ids})

                # Stalled = no new candidates and no new evidence (PRD section 12).
                progressed = bool(new_ids) or len(tools.evidence_index) > evidence_before
                stalled = 0 if progressed else stalled + 1
                if result.done:
                    reason = "no_remaining_work"
                elif iteration - int(checkpoint["iteration"]) >= limits.max_iterations:
                    reason = "iteration_limit"
                elif stalled >= limits.stall_iterations:
                    reason = "stalled"
            run["status"] = "completed"
            run["termination_reason"] = reason
        except StopRun as stop:
            run["status"] = "cancelled" if stop.reason == "cancelled" else "completed"
            run["termination_reason"] = stop.reason
        except TopologyError as exc:
            run["status"] = "failed"
            run["termination_reason"] = "invalid_topology"
            run["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - recorded for operators
            run["status"] = "failed"
            run["termination_reason"] = "error"
            run["error"] = f"{type(exc).__name__}: {exc}"
            self.store.append_event(run_id, {"type": "error", "error": run["error"], "trace": traceback.format_exc()[-2000:]})
        finally:
            if adapter is not None:
                adapter.close()
        run["usage"] = control.usage.to_dict()
        run["ended_at"] = utcnow()
        run["elapsed_seconds"] = round(control.elapsed_seconds, 1)
        self.store.save_run(run)
        self.store.append_event(run_id, {"type": "run_finished", "status": run["status"], "reason": run["termination_reason"]})
        return run

    def _checkpoint(self, run: dict, topology_version: str, memory: dict, tools: DiscoveryTools, iteration: int) -> None:
        self.store.save_checkpoint(
            run["id"],
            {
                "run_id": run["id"],
                "saved_at": utcnow(),
                "iteration": iteration,
                "topology_version": topology_version,
                "memory": memory,
                "evidence_index": tools.evidence_index,
            },
        )
        self._save_inventory(run["source"], tools)
        self.store.save_run(run)

    def _save_inventory(self, source: str, tools: DiscoveryTools) -> None:
        if not tools.inventory:
            return
        path = self.settings.metadata_dir / source / "inventory.json"
        atomic_write_json(
            path,
            {
                "source": source,
                "dialect": tools.adapter.dialect,
                "observed_at": utcnow(),
                "tables": {name: {k: v for k, v in info.items()} for name, info in sorted(tools.inventory.items())},
            },
        )

    def recover_stale_runs(self) -> list[str]:
        """Mark runs whose process died as failed so they can be resumed from their checkpoint."""
        recovered = []
        now = time.time()
        for run in self.store.list_runs():
            if run["status"] not in ("running", "stopping"):
                continue
            run_dir = self.store.run_dir(run["id"])
            paths = [run_dir / "run.json", run_dir / "events.jsonl"]
            last = max((p.stat().st_mtime for p in paths if p.exists()), default=0)
            if now - last > STALE_AFTER_SECONDS:
                run["status"] = "failed"
                run["termination_reason"] = "interrupted"
                run["error"] = "Run stopped without finishing; resume from its checkpoint."
                self.store.save_run(run)
                recovered.append(run["id"])
        return recovered
