"""JSON persistence for candidates, facts, runs, and evidence (FR-004, FR-005, NFR-003).

Every write is atomic (temp file + ``os.replace``) and mutating operations hold a
cross-process lock so concurrent reviewers or runs cannot interleave partial updates.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .config import Settings

logger = logging.getLogger(__name__)


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


def mapping_fingerprint(kind: str, mapping: dict | None, hypothesis: str, source: str | None = None) -> str:
    """Identity of an interpretation. Includes the source so the same mapping on a new source is not a duplicate."""
    if mapping:
        relevant = {k: v for k, v in mapping.items() if k not in ("notes",)}
        return short_hash({"kind": kind, "mapping": relevant, "source": source})
    normalized = " ".join(hypothesis.lower().split())
    return short_hash({"kind": kind, "hypothesis": normalized, "source": source})


def candidate_fingerprint(candidate: dict) -> str:
    return mapping_fingerprint(
        candidate["kind"], candidate.get("mapping"), candidate.get("hypothesis", ""), candidate.get("scope", {}).get("source")
    )


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
            fingerprint = candidate_fingerprint(candidate)
            ev_fp = evidence_fingerprint(candidate.get("evidence", []))
            for existing in self.list_candidates():
                # Recompute instead of trusting the stored value so older records (pre source-aware) compare correctly.
                if candidate_fingerprint(existing) != fingerprint:
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
            "revision": 1,
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
            if data and not data.get("deletion") and (source is None or data.get("source") == source):
                facts.append(data)
        facts.sort(key=lambda f: f.get("approved_at", ""))
        return facts

    def get_fact(self, fact_id: str) -> dict | None:
        """Read an approved fact without allowing paths outside the fact directory."""
        if not fact_id.startswith("fact-") or not fact_id.replace("-", "").isalnum():
            raise ValueError("Invalid fact id")
        fact = read_json(self.settings.approved_dir / f"{fact_id}.json")
        return fact if fact and not fact.get("deletion") else None

    def delete_fact(
        self, fact_id: str, *, reviewer: str, notes: str, expected_token: str, confirm: str,
    ) -> dict:
        """Remove a fact from active knowledge, retaining its data and revocation audit."""
        if confirm != fact_id:
            raise ValueError("Type the fact id to confirm deletion")
        if not reviewer.strip() or not notes.strip():
            raise ValueError("Reviewer and deletion reason are required")
        with file_lock(self._lock_path):
            fact = self.get_fact(fact_id)
            if fact is None:
                raise KeyError(fact_id)
            if short_hash(fact) != expected_token:
                raise ConflictError("The fact changed during review. Reload the facts and review deletion again.")
            node_type = (fact.get("mapping") or {}).get("node_type") if fact["kind"] == "node_mapping" else None
            dependents = [
                other["id"] for other in self.list_facts(fact["source"])
                if node_type and other["kind"] == "edge_mapping"
                and node_type in (
                    (other.get("mapping") or {}).get("from_node_type"),
                    (other.get("mapping") or {}).get("to_node_type"),
                )
            ]
            fact["deletion"] = {
                "at": utcnow(), "reviewer": reviewer.strip(), "notes": notes.strip(),
                "previous_token": expected_token, "dependent_fact_ids": dependents,
            }
            # The tombstone commits deletion; reconcile finishes candidate revocation after a crash.
            atomic_write_json(self.settings.approved_dir / f"{fact_id}.json", fact)
            self._revoke_fact_candidate(fact)
            return {"id": fact_id, "deleted": True, "deletion": fact["deletion"],
                    "dependent_fact_ids": dependents}

    def _revoke_fact_candidate(self, fact: dict) -> bool:
        candidate = self.get_candidate(fact["candidate_id"])
        if candidate is None or any(
            decision.get("deleted_fact_id") == fact["id"] for decision in candidate.get("decisions", [])
        ):
            return False
        deletion = fact["deletion"]
        candidate["decisions"].append({
            "decision": "reject", "reviewer": deletion["reviewer"],
            "notes": f"Fact deleted: {deletion['notes']}", "observations": "",
            "at": deletion["at"], "candidate_version": candidate["version"], "deleted_fact_id": fact["id"],
        })
        candidate["status"] = "rejected"
        candidate["version"] += 1
        candidate["updated_at"] = deletion["at"]
        atomic_write_json(self._candidate_path(candidate["id"]), candidate)
        return True

    def revalidate_fact(
        self, fact_id: str, *, topology_version: str, schema_fingerprints: dict[str, str],
        reviewer: str, notes: str, expected_token: str, expected_source_token: str,
    ) -> dict:
        """Persist a confirmed structural revalidation without overwriting approval or evidence history."""
        if not reviewer.strip() or not notes.strip():
            raise ValueError("Reviewer and review notes are required")
        with file_lock(self._lock_path):
            fact = self.get_fact(fact_id)
            if fact is None:
                raise KeyError(fact_id)
            if short_hash(fact) != expected_token or short_hash(self.list_facts(fact["source"])) != expected_source_token:
                raise ConflictError("Approved facts changed during review. Reload the facts and check again.")
            record = {
                "reviewer": reviewer.strip(), "notes": notes.strip(), "at": utcnow(),
                "previous_topology_version": fact.get("topology_version"),
                "previous_schema_fingerprints": fact.get("schema_fingerprints", {}),
                "topology_version": topology_version, "schema_fingerprints": schema_fingerprints,
                "scope": "mapping, source schema, and endpoint structure; data evidence not rerun",
            }
            fact["revalidations"] = [*fact.get("revalidations", []), record]
            fact["revision"] = fact.get("revision", 1) + 1
            fact["topology_version"] = topology_version
            fact["schema_fingerprints"] = schema_fingerprints
            atomic_write_json(self.settings.approved_dir / f"{fact_id}.json", fact)
            return fact

    def reconcile(self) -> list[str]:
        """Repair candidates whose fact was written but whose status update was interrupted."""
        repaired = []
        with file_lock(self._lock_path):
            for path in sorted(self.settings.approved_dir.glob("fact-*.json")):
                fact = read_json(path)
                if fact and fact.get("deletion") and self._revoke_fact_candidate(fact):
                    repaired.append(fact["candidate_id"])
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
            [(c["id"], c["version"]) for c in candidates]
            + [(f["id"], f["candidate_version"], f.get("revision", 1)) for f in facts]
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
        record = (json.dumps({"at": utcnow(), "run_id": run_id, **event}, default=str) + "\n").encode("utf-8")
        with file_lock(path.with_suffix(".lock")):
            with path.open("a+b") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell():
                    handle.seek(-1, os.SEEK_END)
                    if handle.read(1) != b"\n":
                        handle.write(b"\n")
                handle.write(record)

    def read_events(self, run_id: str, limit: int = 500) -> list[dict]:
        path = self.run_dir(run_id) / "events.jsonl"
        if limit <= 0 or not path.exists():
            return []
        # Split bytes first: an in-flight append can end halfway through a UTF-8 character.
        lines = path.read_bytes().split(b"\n")[:-1]
        events: list[dict] = []
        for index in range(len(lines) - 1, -1, -1):
            line = lines[index]
            if not line.strip():
                continue
            try:
                event = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.warning("Skipping malformed event record: run_id=%s line=%s", run_id, index + 1)
                continue
            if not isinstance(event, dict):
                logger.warning("Skipping non-object event record: run_id=%s line=%s", run_id, index + 1)
                continue
            events.append(event)
            if len(events) == limit:
                break
        return events[::-1]

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

    # ----- reset ------------------------------------------------------
    RESET_SCOPES = ("runs", "pending", "rejected", "approved", "metadata")

    def reset_preview(self) -> dict:
        candidates = self.list_candidates()
        return {
            "runs": len(list(self.settings.runs_dir.glob("run-*"))),
            "pending": sum(1 for c in candidates if c["status"] == "pending"),
            "rejected": sum(1 for c in candidates if c["status"] == "rejected"),
            "approved": sum(1 for c in candidates if c["status"] == "approved"),
            "metadata": sum(1 for p in self.settings.metadata_dir.iterdir()) if self.settings.metadata_dir.exists() else 0,
        }

    def reset(self, scopes: list[str], actor: str, active_run_ids: list[str] | None = None) -> dict:
        """Archive discovery state so the next run starts fresh. Nothing is deleted.

        runs: run history, checkpoints, evidence (also clears dictionary column profiles)
        pending / rejected / approved: candidates by status (approved also archives their facts)
        metadata: source inventories and the cached topology parse
        Dictionary annotations, topology, context documents, and configuration are never touched.
        """
        import shutil

        unknown = set(scopes) - set(self.RESET_SCOPES)
        if unknown or not scopes:
            raise ValueError(f"Choose one or more scopes from {', '.join(self.RESET_SCOPES)}")
        if not actor or not actor.strip():
            raise ValueError("actor is required")
        active = [r["id"] for r in self.list_runs() if r.get("status") in ("queued", "running", "stopping")]
        active += [r for r in (active_run_ids or []) if r not in active]
        if active:
            raise ConflictError(f"Cancel active runs before resetting: {', '.join(active)}")

        stamp = utcnow().replace(":", "").replace("-", "").replace("+0000", "Z")
        archive = self.settings.workspace_dir / "archive" / f"reset-{stamp}-{uuid.uuid4().hex[:4]}"
        moved: dict[str, int] = {}

        def move(path: Path, group: str) -> None:
            target = archive / path.relative_to(self.settings.workspace_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
            moved[group] = moved.get(group, 0) + 1

        with file_lock(self._lock_path):
            if "runs" in scopes:
                for path in sorted(self.settings.runs_dir.glob("run-*")):
                    move(path, "runs")
            statuses = [s for s in ("pending", "rejected", "approved") if s in scopes]
            for candidate in self.list_candidates():
                if candidate["status"] not in statuses:
                    continue
                move(self._candidate_path(candidate["id"]), candidate["status"])
                if candidate["status"] == "approved":
                    fact = self.settings.approved_dir / ("fact-" + candidate["id"].removeprefix("cand-") + ".json")
                    if fact.exists():
                        move(fact, "facts")
            if "metadata" in scopes and self.settings.metadata_dir.exists():
                for path in sorted(self.settings.metadata_dir.iterdir()):
                    move(path, "metadata")
            manifest = {"archived_at": utcnow(), "actor": actor.strip(), "scopes": sorted(scopes), "moved": moved,
                        "path": str(archive)}
            if moved:
                atomic_write_json(archive / "reset.json", manifest)
            return manifest
