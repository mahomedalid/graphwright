import json
import time

import pytest
from fastapi.testclient import TestClient

from ddg.api import create_app


@pytest.fixture
def client(project):
    return TestClient(create_app(project))


def _wait(client, run_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/w/retail/runs/{run_id}").json()
        if run["status"] not in ("queued", "running", "stopping"):
            return run
        time.sleep(0.2)
    raise AssertionError("run did not finish")


def test_full_review_codegen_and_graph_flow(client, settings):
    assert client.get("/").status_code == 200
    status = client.get("/api/status").json()
    assert status["foundry_configured"] is False and status["active_workspace"] == "retail"
    assert client.get("/api/w/retail/status").json()["sources"][0]["name"] == "retail"
    assert client.get("/api/w/retail/topology").json()["is_valid"]

    response = client.post("/api/w/retail/runs", json={"source": "retail", "planner": "heuristic", "limits": {"max_iterations": 20}})
    assert response.status_code == 201
    run = _wait(client, response.json()["id"])
    assert run["status"] == "completed" and run["events"]
    assert client.get("/api/w/retail/metadata/retail").json()["tables"]

    pending = client.get("/api/w/retail/candidates", params={"status": "pending"}).json()
    assert len(pending) == 7
    first = pending[0]
    conflict = client.post(f"/api/w/retail/candidates/{first['id']}/decision", json={"decision": "approve", "reviewer": "sme", "expected_version": 5})
    assert conflict.status_code == 409
    invalid = client.post(f"/api/w/retail/candidates/{first['id']}/decision", json={"decision": "maybe", "reviewer": "sme", "expected_version": 1})
    assert invalid.status_code == 422
    for c in pending:
        r = client.post(f"/api/w/retail/candidates/{c['id']}/decision", json={"decision": "approve", "reviewer": "sme", "notes": "ok", "expected_version": c["version"]})
        assert r.status_code == 200 and r.json()["status"] == "approved"
    assert len(client.get("/api/w/retail/facts", params={"source": "retail"}).json()) == 7
    assert client.get("/api/w/retail/facts/revalidation", params={"source": "retail"}).json()["issues"] == {}

    gen = client.post("/api/w/retail/codegen", json={"source": "retail"})
    assert gen.status_code == 200 and gen.json()["path"].endswith(".py")
    assert client.get("/api/w/retail/codegen").json()

    graph = {
        "format": "ddg-graph/v1", "inputs": {}, "built_at": "now", "stats": {}, "warnings": [],
        "nodes": [{"id": "A:1", "type": "A", "key": "1", "properties": {"name": "x"}, "provenance": []},
                  {"id": "B:2", "type": "B", "key": "2", "properties": {}, "provenance": []}],
        "edges": [{"id": "R:A:1->B:2", "type": "R", "source": "A:1", "target": "B:2", "properties": {}, "provenance": []}],
    }
    (settings.graph_output_dir / "g.json").write_text(json.dumps(graph), encoding="utf-8")
    assert client.get("/api/w/retail/graphs").json()[0]["name"] == "g.json"
    assert client.get("/api/w/retail/graphs/g.json/summary").json()["edge_count"] == 1
    assert client.get("/api/w/retail/graphs/g.json/search", params={"q": "x"}).json()[0]["id"] == "A:1"
    hood = client.get("/api/w/retail/graphs/g.json/neighborhood", params={"node_id": "A:1"}).json()
    assert len(hood["nodes"]) == 2 and hood["edges"][0]["type"] == "R"
    assert client.get("/api/w/retail/graphs/..%2Fddg.toml/summary").status_code == 404
    assert client.get("/api/w/retail/graphs/g.json/neighborhood", params={"node_id": "nope"}).status_code == 404


def test_cancel_and_resume_endpoints(client):
    run_id = client.post("/api/w/retail/runs", json={"source": "retail", "planner": "heuristic"}).json()["id"]
    client.post(f"/api/w/retail/runs/{run_id}/cancel")
    run = _wait(client, run_id)
    assert run["status"] in ("cancelled", "completed")
    resumed = client.post(f"/api/w/retail/runs/{run_id}/resume", json={})
    assert resumed.status_code == 201 and resumed.json()["resumed_from"] == run_id
    _wait(client, resumed.json()["id"])
    assert client.post(f"/api/w/retail/runs/{run_id}/cancel").status_code == 409


def test_unknown_source_and_codegen_errors(client):
    assert client.post("/api/w/retail/runs", json={"source": "nope"}).status_code == 400
    blocked = client.post("/api/w/retail/codegen", json={"source": "retail"})
    assert blocked.status_code == 422 and blocked.json()["errors"]


def test_api_token_is_enforced(project, monkeypatch):
    monkeypatch.setenv("DDG_API_TOKEN", "s3cret")
    client = TestClient(create_app(project))
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/w/retail/candidates", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/api/w/retail/runs", json={"source": "retail"}).status_code == 401
    assert client.get("/api/status", headers={"Authorization": "Bearer s3cret"}).status_code == 200
