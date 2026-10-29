from concurrent.futures import ThreadPoolExecutor

import pytest

from ddg.store import ConflictError, ProposalRejected, atomic_write_json


def test_given_partially_written_event_when_reading_then_only_complete_lines_are_returned(store):
    store.append_event("run-test", {"type": "model_call_started"})
    path = store.run_dir("run-test") / "events.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "unfinished"')

    events = store.read_events("run-test")

    assert [event["type"] for event in events] == ["model_call_started"]
    assert store.read_events("run-test", limit=0) == []


@pytest.mark.parametrize("damaged", [b"not-json", b'{"type":', b"\xff", b"null", b"[]"])
def test_given_damaged_event_when_reading_then_valid_events_are_preserved(store, damaged):
    store.append_event("run-test", {"type": "first"})
    path = store.run_dir("run-test") / "events.jsonl"
    with path.open("ab") as handle:
        handle.write(damaged + b"\n")
    store.append_event("run-test", {"type": "last"})

    events = store.read_events("run-test", limit=2)

    assert [event["type"] for event in events] == ["first", "last"]


def test_given_incomplete_utf8_tail_when_appending_then_new_event_is_readable(store):
    store.append_event("run-test", {"type": "first"})
    path = store.run_dir("run-test") / "events.jsonl"
    with path.open("ab") as handle:
        handle.write(b'{"type": "\xe2\x82')

    before = store.read_events("run-test")
    store.append_event("run-test", {"type": "last"})
    after = store.read_events("run-test")

    assert [event["type"] for event in before] == ["first"]
    assert [event["type"] for event in after] == ["first", "last"]


def test_given_concurrent_writers_when_appending_then_all_events_are_intact(store):
    def append(index):
        store.append_event("run-test", {"index": index, "payload": "x" * 10000})

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(append, range(16)))

    events = store.read_events("run-test")

    assert sorted(event["index"] for event in events) == list(range(16))
    assert all(event["payload"] == "x" * 10000 for event in events)


def _candidate(**overrides):
    base = {
        "kind": "node_mapping",
        "hypothesis": "Rows of X are Things",
        "mapping": {"node_type": "Customer", "source_table": "CUST_MASTER", "key_columns": ["cust_id"]},
        "evidence": [{"id": "ev-1", "result": {"unique": True}}],
        "scope": {"source": "retail"},
    }
    base.update(overrides)
    return base


def test_decision_requires_current_version_and_pending_status(store):
    c = store.add_candidate(_candidate())
    with pytest.raises(ConflictError):
        store.decide(c["id"], "approve", "sme", expected_version=99)
    approved = store.decide(c["id"], "approve", "sme", "ok", "obs", expected_version=1)
    assert approved["status"] == "approved" and approved["version"] == 2
    with pytest.raises(ConflictError):
        store.decide(c["id"], "reject", "other", expected_version=2)
    fact = store.list_facts()[0]
    assert fact["candidate_id"] == c["id"] and fact["approved_by"] == "sme" and fact["observations"] == "obs"


def test_reviewer_required(store):
    c = store.add_candidate(_candidate())
    with pytest.raises(ValueError):
        store.decide(c["id"], "approve", "  ", expected_version=1)


def test_duplicate_and_rejected_proposals_are_blocked(store):
    c = store.add_candidate(_candidate())
    with pytest.raises(ProposalRejected, match="Duplicate"):
        store.add_candidate(_candidate())
    store.decide(c["id"], "reject", "sme", "wrong table", expected_version=1)
    with pytest.raises(ProposalRejected, match="wrong table"):
        store.add_candidate(_candidate())
    with pytest.raises(ProposalRejected):  # new note but identical evidence
        store.add_candidate(_candidate(new_evidence_note="trust me"))
    revisited = store.add_candidate(
        _candidate(new_evidence_note="re-profiled after fix", evidence=[{"id": "ev-2", "result": {"unique": False}}])
    )
    assert revisited["status"] == "pending"


def test_reconcile_repairs_interrupted_promotion(store):
    c = store.add_candidate(_candidate())
    store._write_fact(c, {"reviewer": "sme", "notes": "", "observations": "", "at": "2026-01-01T00:00:00+00:00"})
    assert store.get_candidate(c["id"])["status"] == "pending"  # simulated crash before status update
    assert store.reconcile() == [c["id"]]
    repaired = store.get_candidate(c["id"])
    assert repaired["status"] == "approved" and repaired["decisions"][0]["repaired"]
    assert store.reconcile() == []


def test_atomic_write_leaves_no_temp_files(tmp_path):
    target = tmp_path / "x.json"
    atomic_write_json(target, {"a": 1})
    with pytest.raises(TypeError):
        atomic_write_json(target, {1j: 1})
    assert target.read_text(encoding="utf-8").strip().startswith("{")
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_review_snapshot_version_changes_after_decision(store):
    c = store.add_candidate(_candidate())
    before = store.review_snapshot()["version"]
    store.decide(c["id"], "reject", "sme", "no", expected_version=1)
    assert store.review_snapshot()["version"] != before
