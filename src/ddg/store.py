"""JSON persistence for candidates, facts, runs, and evidence (FR-004, FR-005, NFR-003).

Every write is atomic (temp file + ``os.replace``) and mutating operations hold a
cross-process lock so concurrent reviewers or runs cannot interleave partial updates.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .config import Settings


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def short_hash(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()[:16]


def _replace_with_retry(src: str, dst: Path, attempts: int = 40) -> None:
    # Windows refuses to replace a file another handle has open; readers hold it only briefly.
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.025 * (attempt + 1))


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=False, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def read_json(path: Path, default: Any = None) -> Any:
    for attempt in range(40):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.025 * (attempt + 1))
    return default


@contextlib.contextmanager
def file_lock(lock_path: Path, timeout: float = 15.0, stale_after: float = 60.0) -> Iterator[None]:
    """Single-writer lock using exclusive file creation; stale locks are broken."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            with contextlib.suppress(FileNotFoundError):
                if time.time() - lock_path.stat().st_mtime > stale_after:
                    os.unlink(lock_path)
                    continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"Could not acquire lock {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(lock_path)


class ConflictError(Exception):
    """Raised when an optimistic version check fails."""


class ProposalRejected(Exception):
    """Raised when a candidate proposal violates feedback or validation rules."""


def mapping_fingerprint(kind: str, mapping: dict | None, hypothesis: str) -> str:
    if mapping:
        relevant = {k: v for k, v in mapping.items() if k not in ("notes",)}
        return short_hash({"kind": kind, "mapping": relevant})
    normalized = " ".join(hypothesis.lower().split())
    return short_hash({"kind": kind, "hypothesis": normalized})


def evidence_fingerprint(evidence: list[dict]) -> str:
    return short_hash(sorted(short_hash(e.get("result")) for e in evidence))


class Store:
    """File-backed store rooted at the project directory."""

    def __init__(self, settings: Settings):
        self.settings = settings
        for directory in (
            settings.candidates_dir,
            settings.approved_dir,
            settings.metadata_dir,
            settings.sandbox_dir,
            settings.runs_dir,
            settings.graph_output_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self._lock_path = settings.candidates_dir / ".lock"

    # ----- candidates -------------------------------------------------
    def _candidate_path(self, candidate_id: str) -> Path:
        if not candidate_id.replace("-", "").isalnum():
            raise ValueError("Invalid candidate id")
        return self.settings.candidates_dir / f"{candidate_id}.json"

    def list_candidates(self, status: str | None = None) -> list[dict]:
        items = []
        for path in sorted(self.settings.candidates_dir.glob("cand-*.json")):
            data = read_json(path)
            if data and (status is None or data.get("status") == status):
                items.append(data)
        items.sort(key=lambda c: c.get("created_at", ""))
        return items

    def get_candidate(self, candidate_id: str) -> dict | None:
        return read_json(self._candidate_path(candidate_id))

    def add_candidate(self, candidate: dict) -> dict:
        """Persist a new candidate after feedback-reuse checks (FR-006)."""
        with file_lock(self._lock_path):
            fingerprint = mapping_fingerprint(candidate["kind"], candidate.get("mapping"), candidate["hypothesis"])
            ev_fp = evidence_fingerprint(candidate.get("evidence", []))
            for existing in self.list_candidates():
                if existing.get("fingerprint") != fingerprint:
                    continue
                status = existing.get("status")
                if status in ("pending", "approved"):
                    raise ProposalRejected(
                        f"Duplicate of {status} candidate {existing['id']}; do not resubmit the same interpretation."
                    )
                if status == "rejected":
                    notes = "; ".join(d.get("notes", "") for d in existing.get("decisions", []) if d.get("decision") == "reject")
                    if existing.get("evidence_fingerprint") == ev_fp or not candidate.get("new_evidence_note"):
                        raise ProposalRejected(
                            f"This interpretation was rejected in {existing['id']} (SME notes: {notes or 'none'}). "
                            "Resubmission requires new evidence and a 'new_evidence_note' explaining what changed."
                        )
            record = {
                "id": new_id("cand"),
                "version": 1,
                "status": "pending",
                "created_at": utcnow(),
                **candidate,
                "fingerprint": fingerprint,
                "evidence_fingerprint": ev_fp,
                "decisions": [],
            }
            atomic_write_json(self._candidate_path(record["id"]), record)
            return record

    def decide(
        self,
        candidate_id: str,
        decision: str,
        reviewer: str,
        notes: str = "",
        observations: str = "",
        expected_version: int | None = None,
    ) -> dict:
        if decision not in ("approve", "reject"):
            raise ValueError("decision must be 'approve' or 'reject'")
        if not reviewer.strip():
            raise ValueError("reviewer is required")
        with file_lock(self._lock_path):
            candidate = self.get_candidate(candidate_id)
            if candidate is None:
                raise KeyError(candidate_id)
            if expected_version is not None and candidate["version"] != expected_version:
                raise ConflictError(
                    f"Candidate {candidate_id} is at version {candidate['version']}, expected {expected_version}. Reload and retry."
                )
            if candidate["status"] != "pending":
                raise ConflictError(f"Candidate {candidate_id} is already {candidate['status']}")
            record = {
                "decision": decision,
                "reviewer": reviewer.strip(),
                "notes": notes,
                "observations": observations,
                "at": utcnow(),
                "candidate_version": candidate["version"],
            }
            if decision == "approve":
                # Fact first: if the process dies before the candidate update, reconcile() repairs it.
                self._write_fact(candidate, record)
            candidate["decisions"].append(record)
            candidate["status"] = "approved" if decision == "approve" else "rejected"
            candidate["version"] += 1
            candidate["updated_at"] = record["at"]
            atomic_write_json(self._candidate_path(candidate_id), candidate)
            return candidate

    # ----- facts ------------------------------------------------------
    def _write_fact(self, candidate: dict, decision: dict) -> dict:
        fact = {
            "id": "fact-" + candidate["id"].removeprefix("cand-"),
            "candidate_id": candidate["id"],
            "candidate_version": candidate["version"],
            "kind": candidate["kind"],
            "hypothesis": candidate["hypothesis"],
            "mapping": candidate.get("mapping"),
            "source": candidate.get("scope", {}).get("source"),
            "scope": candidate.get("scope", {}),
            "topology_version": candidate.get("topology_version"),
            "schema_fingerprints": candidate.get("schema_fingerprints", {}),
            "evidence": candidate.get("evidence", []),
            "limitations": candidate.get("limitations", []),
            "approved_by": decision["reviewer"],
            "approved_at": decision["at"],
            "notes": decision["notes"],
            "observations": decision["observations"],
        }
        atomic_write_json(self.settings.approved_dir / f"{fact['id']}.json", fact)
        return fact

    def list_facts(self, source: str | None = None) -> list[dict]:
        facts = []
        for path in sorted(self.settings.approved_dir.glob("fact-*.json")):
            data = read_json(path)
            if data and (source is None or data.get("source") == source):
                facts.append(data)
        facts.sort(key=lambda f: f.get("approved_at", ""))
        return facts

    def reconcile(self) -> list[str]:
        """Repair candidates whose fact was written but whose status update was interrupted."""
        repaired = []
        with file_lock(self._lock_path):
            for fact in self.list_facts():
                candidate = self.get_candidate(fact["candidate_id"])
                if candidate and candidate["status"] == "pending" and candidate["version"] == fact["candidate_version"]:
                    candidate["decisions"].append(
                        {
                            "decision": "approve",
                            "reviewer": fact["approved_by"],
                            "notes": fact["notes"],
                            "observations": fact["observations"],
                            "at": fact["approved_at"],
                            "candidate_version": candidate["version"],
                            "repaired": True,
                        }
                    )
                    candidate["status"] = "approved"
                    candidate["version"] += 1
                    atomic_write_json(self._candidate_path(candidate["id"]), candidate)
                    repaired.append(candidate["id"])
        return repaired

    def review_snapshot(self) -> dict:
        """Immutable snapshot of review state consumed at an iteration boundary."""
        candidates = self.list_candidates()
        facts = self.list_facts()
        snapshot = {
            "approved": [c for c in candidates if c["status"] == "approved"],
            "rejected": [c for c in candidates if c["status"] == "rejected"],
            "pending": [c for c in candidates if c["status"] == "pending"],
            "facts": facts,
        }
        snapshot["version"] = short_hash(
            [(c["id"], c["version"]) for c in candidates] + [(f["id"], f["candidate_version"]) for f in facts]
        )
        return snapshot

    # ----- runs -------------------------------------------------------
    def run_dir(self, run_id: str) -> Path:
        if not run_id.replace("-", "").isalnum():
            raise ValueError("Invalid run id")
        return self.settings.runs_dir / run_id

    def save_run(self, run: dict) -> None:
        atomic_write_json(self.run_dir(run["id"]) / "run.json", run)

    def get_run(self, run_id: str) -> dict | None:
        return self._overlay_cancel(read_json(self.run_dir(run_id) / "run.json"))

    def list_runs(self) -> list[dict]:
        runs = [self._overlay_cancel(read_json(p)) for p in self.settings.runs_dir.glob("run-*/run.json")]
        return sorted((r for r in runs if r), key=lambda r: r.get("created_at", ""), reverse=True)

    def _overlay_cancel(self, run: dict | None) -> dict | None:
        # The runner is the only writer of run.json; a pending cancel request is shown as "stopping".
        if run and run.get("status") in ("queued", "running") and self.cancel_requested(run["id"]):
            run["status"] = "stopping"
        return run

    def save_checkpoint(self, run_id: str, checkpoint: dict) -> None:
        atomic_write_json(self.run_dir(run_id) / "checkpoint.json", checkpoint)

    def load_checkpoint(self, run_id: str) -> dict | None:
        return read_json(self.run_dir(run_id) / "checkpoint.json")

    def append_event(self, run_id: str, event: dict) -> None:
        path = self.run_dir(run_id) / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": utcnow(), "run_id": run_id, **event}, default=str) + "\n")

    def read_events(self, run_id: str, limit: int = 500) -> list[dict]:
        path = self.run_dir(run_id) / "events.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(line) for line in lines if line.strip()]

    def save_evidence(self, run_id: str, evidence: dict) -> None:
        atomic_write_json(self.run_dir(run_id) / "evidence" / f"{evidence['id']}.json", evidence)

    def get_evidence(self, run_id: str, evidence_id: str) -> dict | None:
        if not evidence_id.replace("-", "").isalnum():
            return None
        return read_json(self.run_dir(run_id) / "evidence" / f"{evidence_id}.json")

    def request_cancel(self, run_id: str) -> None:
        path = self.run_dir(run_id) / "cancel"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(utcnow(), encoding="utf-8")

    def cancel_requested(self, run_id: str) -> bool:
        return (self.run_dir(run_id) / "cancel").exists()
