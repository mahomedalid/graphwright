"""Confirmed deletion retires active facts without discarding audit history."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ddg.api import create_app
from ddg.dictionary import build_dictionary
from ddg.store import ConflictError, ProposalRejected, read_json, short_hash


def _approve(store, *, kind="node_mapping", mapping=None):
    candidate = store.add_candidate({
        "kind": kind, "hypothesis": "Approved interpretation",
        "mapping": mapping or {
            "node_type": "Customer", "source_table": "CUST_MASTER", "key_columns": ["cust_id"],
            "identity_namespace": "CUSTOMER_ID",
        },
        "scope": {"source": "retail"}, "evidence": [{"id": "ev-original", "result": {"unique": True}}],
    })
    store.decide(candidate["id"], "approve", "original-reviewer", "approved", expected_version=1)
    return store.get_fact("fact-" + candidate["id"].removeprefix("cand-"))


def _payload(fact):
    return {"reviewer": "reviewer", "notes": "Meaning is wrong and cannot be revalidated.",
            "expected_token": short_hash(fact), "confirm": fact["id"]}


def test_given_confirmed_deletion_when_deleting_then_retires_fact_and_revokes_approval(settings, store):
    fact = _approve(store)
    before = store.review_snapshot()["version"]

    result = store.delete_fact(fact["id"], **_payload(fact))

    assert result["deleted"]
    assert store.list_facts() == []
    assert store.get_fact(fact["id"]) is None
    assert store.review_snapshot()["version"] != before
    candidate = store.get_candidate(fact["candidate_id"])
    assert candidate["status"] == "rejected"
    assert candidate["decisions"][0]["decision"] == "approve"
    assert candidate["decisions"][-1]["deleted_fact_id"] == fact["id"]
    audit = read_json(settings.approved_dir / f"{fact['id']}.json")
    assert audit["evidence"] == fact["evidence"]
    assert audit["approved_by"] == fact["approved_by"]
    assert audit["deletion"]["reviewer"] == "reviewer"
    assert store.reconcile() == []
    assert store.get_fact(fact["id"]) is None
    with pytest.raises(ProposalRejected, match="Fact deleted"):
        store.add_candidate({
            "kind": fact["kind"], "hypothesis": fact["hypothesis"], "mapping": fact["mapping"],
            "scope": fact["scope"], "evidence": fact["evidence"],
        })


@pytest.mark.parametrize("changes", [{"confirm": "wrong"}, {"reviewer": " "}, {"notes": " "}, {"expected_token": "old"}])
def test_given_missing_confirmation_or_stale_token_when_deleting_then_leaves_fact_unchanged(store, changes):
    fact = _approve(store)

    with pytest.raises((ValueError, ConflictError)):
        store.delete_fact(fact["id"], **{**_payload(fact), **changes})

    assert store.get_fact(fact["id"]) == fact
    assert store.get_candidate(fact["candidate_id"])["status"] == "approved"


def test_given_dependent_relationship_when_deleting_node_then_warns_without_cascading(store):
    node = _approve(store)
    edge = _approve(store, kind="edge_mapping", mapping={
        "edge_type": "PLACED", "source_table": "tbl_ord_hdr", "from_node_type": "Customer",
        "to_node_type": "Order", "from_key_columns": ["cust_ref"], "to_key_columns": ["order_no"],
    })

    result = store.delete_fact(node["id"], **_payload(node))

    assert result["dependent_fact_ids"] == [edge["id"]]
    assert store.get_fact(edge["id"]) == edge
    assert store.get_candidate(edge["candidate_id"])["status"] == "approved"


def test_given_interrupted_candidate_revocation_when_reconciling_then_deletion_stays_committed(settings, store, monkeypatch):
    fact = _approve(store)
    original = store._revoke_fact_candidate

    def fail(_fact):
        raise OSError("simulated write failure")

    monkeypatch.setattr(store, "_revoke_fact_candidate", fail)
    with pytest.raises(OSError, match="simulated"):
        store.delete_fact(fact["id"], **_payload(fact))
    assert store.get_fact(fact["id"]) is None
    assert store.get_candidate(fact["candidate_id"])["status"] == "approved"
    monkeypatch.setattr(store, "_revoke_fact_candidate", original)

    repaired = store.reconcile()

    assert repaired == [fact["candidate_id"]]
    assert store.get_candidate(fact["candidate_id"])["status"] == "rejected"
    assert store.get_fact(fact["id"]) is None


def test_given_delete_request_when_confirmed_then_fact_is_removed_from_api_and_revalidation(project, store):
    fact = _approve(store)
    client = TestClient(create_app(project))

    response = client.request("DELETE", f"/api/w/retail/facts/{fact['id']}", json=_payload(fact))

    assert response.status_code == 200
    assert client.get("/api/w/retail/facts").json() == []
    assert client.get("/api/w/retail/facts/revalidation?source=retail").json()["facts"] == []
    assert client.request("DELETE", f"/api/w/retail/facts/{fact['id']}", json=_payload(fact)).status_code == 404


@pytest.mark.parametrize(("changes", "status"), [
    ({"confirm": "wrong"}, 400), ({"expected_token": "stale"}, 409), ({"reviewer": " "}, 400),
])
def test_given_invalid_delete_request_when_submitted_then_api_keeps_fact(project, store, changes, status):
    fact = _approve(store)
    client = TestClient(create_app(project))

    response = client.request("DELETE", f"/api/w/retail/facts/{fact['id']}", json={**_payload(fact), **changes})

    assert response.status_code == status
    assert store.get_fact(fact["id"]) == fact


def test_given_deleted_fact_when_generating_then_future_builders_exclude_it_and_previous_builder_is_unchanged(project, store):
    fact = _approve(store)
    client = TestClient(create_app(project))
    generated = client.post("/api/w/retail/codegen", json={"source": "retail"})
    assert generated.status_code == 200
    builder = Path(generated.json()["path"])
    previous_code = builder.read_text(encoding="utf-8")

    client.request("DELETE", f"/api/w/retail/facts/{fact['id']}", json=_payload(fact))
    result = client.post("/api/w/retail/codegen", json={"source": "retail"})

    assert result.status_code == 422
    assert builder.read_text(encoding="utf-8") == previous_code


def test_given_deleted_annotation_when_building_dictionary_then_removes_active_knowledge_but_retains_rejection(settings, store):
    fact = _approve(store, kind="annotation", mapping={"table": "CUST_MASTER", "description": "Unreliable description"})
    assert build_dictionary(settings, store, "retail")["tables"][0]["description"] == "Unreliable description"

    store.delete_fact(fact["id"], **_payload(fact))
    table = build_dictionary(settings, store, "retail")["tables"][0]

    assert table["description"] == ""
    assert any("Fact deleted" in note for note in table["history"][0]["sme_notes"])
