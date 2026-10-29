"""Explicit fact revalidation and non-destructive build selection."""

import pytest
from fastapi.testclient import TestClient

from ddg.api import create_app
from ddg.store import ConflictError, short_hash


@pytest.fixture
def client(project):
    return TestClient(create_app(project))


def _approve(store, kind="node_mapping", mapping=None, **extra):
    candidate = store.add_candidate({
        "kind": kind, "hypothesis": "Mapping for review",
        "mapping": mapping or {
            "node_type": "Customer", "source_table": "CUST_MASTER", "key_columns": ["cust_id"],
        },
        "scope": {"source": "retail"}, "topology_version": "old-topology",
        "evidence": [{"id": "ev-original"}], **extra,
    })
    store.decide(candidate["id"], "approve", "original-reviewer", "original approval", expected_version=1)
    return store.get_fact("fact-" + candidate["id"].removeprefix("cand-"))


def _payload(client, fact):
    report = client.get("/api/w/retail/facts/revalidation", params={"source": "retail"}).json()
    return {
        "reviewer": "new-reviewer", "notes": "Reviewed identity and business meaning against the current graph.",
        "confirm_meaning": True, "expected_token": report["validations"][fact["id"]]["token"],
        "expected_topology_version": report["topology_version"],
    }


def test_given_stale_fact_when_explicitly_revalidated_then_codegen_is_unblocked(client, store):
    fact = _approve(store)
    assert client.post("/api/w/retail/codegen", json={"source": "retail"}).status_code == 422
    before_snapshot = store.review_snapshot()["version"]

    response = client.post(f"/api/w/retail/facts/{fact['id']}/revalidate", json=_payload(client, fact))
    result = client.post("/api/w/retail/codegen", json={"source": "retail"})

    assert response.status_code == 200
    updated = response.json()
    assert updated["revision"] == 2
    assert updated["approved_by"] == fact["approved_by"]
    assert updated["approved_at"] == fact["approved_at"]
    assert updated["evidence"] == fact["evidence"]
    assert updated["mapping"] == fact["mapping"]
    assert updated["revalidations"][0]["previous_topology_version"] == "old-topology"
    assert updated["revalidations"][0]["reviewer"] == "new-reviewer"
    assert "data evidence not rerun" in updated["revalidations"][0]["scope"]
    assert store.review_snapshot()["version"] != before_snapshot
    assert result.status_code == 200
    assert result.json()["facts"][0]["fact_revision"] == 2
    assert client.get("/api/w/retail/facts/revalidation", params={"source": "retail"}).json()["issues"] == {}


@pytest.mark.parametrize("changes", [
    {"confirm_meaning": False}, {"reviewer": "  "}, {"notes": "  "},
    {"expected_token": "outdated"}, {"expected_topology_version": "outdated"},
])
def test_given_missing_confirmation_or_stale_review_when_revalidating_then_fact_is_unchanged(client, store, changes):
    fact = _approve(store)
    payload = {**_payload(client, fact), **changes}

    response = client.post(f"/api/w/retail/facts/{fact['id']}/revalidate", json=payload)

    assert response.status_code in (400, 409)
    assert store.get_fact(fact["id"]) == fact


def test_given_missing_source_column_when_revalidating_then_approval_is_not_renewed(client, store):
    fact = _approve(store, mapping={
        "node_type": "Customer", "source_table": "CUST_MASTER", "key_columns": ["missing_column"],
    })
    report = client.get("/api/w/retail/facts/revalidation", params={"source": "retail"}).json()

    response = client.post(f"/api/w/retail/facts/{fact['id']}/revalidate", json=_payload(client, fact))

    assert report["validations"][fact["id"]]["can_revalidate"] is False
    assert response.status_code == 422
    assert store.get_fact(fact["id"]) == fact


def test_given_changed_schema_when_reviewed_then_updates_fingerprints_and_keeps_history(client, store):
    fact = _approve(store, schema_fingerprints={"CUST_MASTER": "old-schema"})

    response = client.post(f"/api/w/retail/facts/{fact['id']}/revalidate", json=_payload(client, fact))

    assert response.status_code == 200
    updated = response.json()
    assert updated["schema_fingerprints"]["CUST_MASTER"] != "old-schema"
    assert updated["revalidations"][0]["previous_schema_fingerprints"] == {"CUST_MASTER": "old-schema"}


def test_given_missing_evidence_table_when_revalidating_then_reports_error_without_changes(client, store):
    fact = _approve(store, schema_fingerprints={"deleted_table": "old-schema"})

    response = client.post(f"/api/w/retail/facts/{fact['id']}/revalidate", json=_payload(client, fact))

    assert response.status_code == 422
    assert "deleted_table" in str(response.json())
    assert store.get_fact(fact["id"]) == fact


def test_given_unreachable_source_when_revalidating_then_preserves_the_old_approval(client, store, monkeypatch):
    fact = _approve(store)
    payload = _payload(client, fact)

    def unavailable(*args, **kwargs):
        raise OSError("test source unavailable")

    monkeypatch.setattr("ddg.api.create_adapter", unavailable)

    response = client.post(f"/api/w/retail/facts/{fact['id']}/revalidate", json=payload)

    assert response.status_code == 503
    assert store.get_fact(fact["id"]) == fact


def test_given_no_selected_node_mappings_when_generating_then_does_not_create_empty_builder(client, store):
    fact = _approve(store)

    response = client.post("/api/w/retail/codegen", json={
        "source": "retail", "exclude_fact_ids": [fact["id"]],
    })

    assert response.status_code == 422
    assert "No approved node mappings" in " ".join(response.json()["errors"])


@pytest.fixture
def mismatched_facts(store):
    customer = _approve(store, mapping={
        "node_type": "Customer", "source_table": "CUST_MASTER", "key_columns": ["cust_id", "email_addr"],
    })
    order = _approve(store, mapping={
        "node_type": "Order", "source_table": "tbl_ord_hdr", "key_columns": ["order_no"],
    })
    edge = _approve(store, "edge_mapping", mapping={
        "edge_type": "PLACED", "source_table": "tbl_ord_hdr",
        "from_key_columns": ["cust_ref"], "to_key_columns": ["order_no"],
    })
    return customer, order, edge


def test_given_endpoint_id_mismatch_when_revalidating_then_explains_required_parts(client, store, mismatched_facts):
    edge = mismatched_facts[2]

    response = client.post(f"/api/w/retail/facts/{edge['id']}/revalidate", json=_payload(client, edge))

    assert response.status_code == 422
    message = " ".join(response.json()["errors"])
    assert "cust_ref" in message and "cust_id + email_addr" in message
    assert "leave this relationship out of this build" in message
    assert store.get_fact(edge["id"]) == edge


def test_given_bad_relationship_when_explicitly_excluded_then_builds_remaining_facts(client, store, mismatched_facts):
    edge = mismatched_facts[2]
    original = store.list_facts()

    response = client.post("/api/w/retail/codegen", json={
        "source": "retail", "allow_stale": True, "exclude_fact_ids": [edge["id"]],
    })

    assert response.status_code == 200
    assert response.json()["excluded_fact_ids"] == [edge["id"]]
    assert edge["id"] not in [fact["fact_id"] for fact in response.json()["facts"]]
    assert any("Explicitly left out" in warning for warning in response.json()["warnings"])
    assert store.list_facts() == original


def test_given_node_excluded_but_relationship_kept_when_generating_then_still_blocks(client, mismatched_facts):
    customer = mismatched_facts[0]

    response = client.post("/api/w/retail/codegen", json={
        "source": "retail", "allow_stale": True, "exclude_fact_ids": [customer["id"]],
    })

    assert response.status_code == 422
    assert "approved node mapping for 'Customer'" in " ".join(response.json()["errors"])


def test_given_unknown_exclusion_when_generating_then_rejects_it(client, store):
    _approve(store)

    response = client.post("/api/w/retail/codegen", json={
        "source": "retail", "exclude_fact_ids": ["fact-unknown"],
    })

    assert response.status_code == 422
    assert "Cannot exclude unknown facts" in " ".join(response.json()["errors"])


@pytest.mark.parametrize("stale_fact", [True, False])
def test_given_concurrent_changes_when_saving_revalidation_then_rejects_stale_snapshot(store, stale_fact):
    fact = _approve(store)
    token = short_hash(fact) if not stale_fact else "old-fact-token"
    source_token = short_hash(store.list_facts("retail")) if stale_fact else "old-source-token"

    with pytest.raises(ConflictError):
        store.revalidate_fact(
            fact["id"], topology_version="current", schema_fingerprints={},
            reviewer="reviewer", notes="reviewed", expected_token=token, expected_source_token=source_token,
        )

    assert store.get_fact(fact["id"]) == fact
