import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from ddg.agent.runner import DiscoveryRunner
from ddg.api import create_app
from ddg.cli import main
from ddg.config import WorkspaceError, active_workspace, list_workspaces, load_settings, set_active_workspace
from ddg.store import Store
from ddg.topology import load_topology
from ddg.workspaces import create_workspace, describe_workspace


def test_template_is_hidden_and_blank_workspace_is_actionable(project):
    assert list_workspaces(project) == ["retail"]
    path = create_workspace(project, "sales", description='Sales "pilot"')
    assert sorted(p.name for p in path.iterdir()) == ["context", "workspace.toml"]
    assert (path / "context" / "topology.md").is_file()
    settings = load_settings(project, "sales")
    assert settings.description == 'Sales "pilot"' and settings.sources == {} and settings.sampling.allowlist == []
    topology = load_topology(settings.topology_file, strict=False)
    assert not topology.is_valid
    assert "No node types defined" in topology.errors[0].message
    assert not topology.node_types  # the format reference inside the code fence is not parsed


@pytest.mark.parametrize("name", ["_hidden", "-x", "a/b", "..", "has space", ""])
def test_invalid_workspace_names(project, name):
    with pytest.raises(WorkspaceError):
        create_workspace(project, name)


def test_duplicate_and_unknown_workspaces(project):
    with pytest.raises(WorkspaceError, match="already exists"):
        create_workspace(project, "retail")
    with pytest.raises(WorkspaceError, match="not found"):
        load_settings(project, "nope")
    with pytest.raises(WorkspaceError, match="does not exist"):
        set_active_workspace(project, "nope")


def test_copy_from_existing_workspace_copies_config_not_artifacts(project, settings, store):
    DiscoveryRunner(settings, store).execute(DiscoveryRunner(settings, store).create_run("retail", "heuristic")["id"])
    copy = create_workspace(project, "retail-v2", source="retail")
    assert not (copy / "candidates").exists() and not (copy / "runs").exists()
    copied = load_settings(project, "retail-v2")
    assert list(copied.sources) == ["retail"] and copied.sampling.allowlist == settings.sampling.allowlist


def test_workspaces_are_isolated(project, settings, store):
    runner = DiscoveryRunner(settings, store)
    runner.execute(runner.create_run("retail", "heuristic", {"max_iterations": 20})["id"])
    assert len(store.list_candidates()) == 7

    create_workspace(project, "retail-b", source="retail")
    other = load_settings(project, "retail-b")
    (other.workspace_dir / "data").mkdir()
    (other.workspace_dir / "data" / "retail.sqlite").write_bytes((settings.workspace_dir / "data" / "retail.sqlite").read_bytes())
    other_store = Store(other)
    assert other_store.list_candidates() == []  # no leakage, and rejection history is per workspace
    assert other.candidates_dir != settings.candidates_dir and other.runs_dir.parent == other.workspace_dir


def test_active_workspace_resolution(project, monkeypatch):
    create_workspace(project, "alpha")
    assert active_workspace(project) == "retail"  # ddg.toml default
    set_active_workspace(project, "alpha")
    assert active_workspace(project) == "alpha" and load_settings(project).workspace == "alpha"
    monkeypatch.setenv("DDG_WORKSPACE", "retail")
    assert active_workspace(project) == "retail"
    assert load_settings(project, "alpha").workspace == "alpha"  # explicit argument wins


def test_workspace_overrides_global_defaults(project):
    path = create_workspace(project, "tight")
    with (path / "workspace.toml").open("a", encoding="utf-8") as handle:
        handle.write("\n[run]\nmax_minutes = 1.5\n\n[agent]\nplanner = \"heuristic\"\n")
    settings = load_settings(project, "tight")
    assert settings.limits.max_minutes == 1.5 and settings.limits.max_iterations == 3
    assert settings.planner == "heuristic"


def test_cli_workspace_commands(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    assert main(["workspace", "create", "beta", "--description", "B", "--use"]) == 0
    assert active_workspace(project) == "beta"
    assert main(["workspace", "list"]) == 0
    out = capsys.readouterr().out
    assert "* beta" in out and "  retail" in out and "_template" not in out
    assert main(["topology"]) == 1  # active blank workspace is invalid
    assert main(["-w", "retail", "topology"]) == 0
    assert main(["topology", "-w", "retail"]) == 0
    assert main(["workspace", "use", "retail"]) == 0
    assert main(["run", "--max-iterations", "1"]) == 0  # single source is picked automatically
    assert main(["-w", "beta", "run"]) == 2
    assert "has sources []" in capsys.readouterr().err
    assert main(["workspace", "use", "missing"]) == 2


def test_api_workspace_management(project):
    client = TestClient(create_app(project))
    data = client.get("/api/workspaces").json()
    assert data["active"] == "retail" and [w["name"] for w in data["workspaces"]] == ["retail"]

    created = client.post("/api/workspaces", json={"name": "sales", "description": "Sales"})
    assert created.status_code == 201 and created.json()["topology"]["is_valid"] is False
    assert client.post("/api/workspaces", json={"name": "sales"}).status_code == 409
    assert client.post("/api/workspaces", json={"name": "_x"}).status_code == 400
    assert client.post("/api/workspaces", json={"name": "y", "copy_from": "nope"}).status_code == 400

    status = client.get("/api/w/sales/status").json()
    assert status["sources"] == [] and status["workspace"] == "sales"
    assert client.get("/api/w/sales/candidates").json() == []
    assert client.get("/api/w/sales/graphs").json() == []
    assert client.get("/api/w/_template/status").status_code == 404
    assert client.get("/api/w/nope/runs").status_code == 404

    assert client.post("/api/workspaces/sales/activate").json() == {"active": "sales"}
    assert client.get("/api/status").json()["active_workspace"] == "sales"
    assert client.post("/api/workspaces/nope/activate").status_code == 404


def test_api_picks_up_workspace_config_changes(project):
    client = TestClient(create_app(project))
    client.post("/api/workspaces", json={"name": "late"})
    assert client.get("/api/w/late/status").json()["sources"] == []
    path = project / "workspaces" / "late" / "workspace.toml"
    path.write_text(path.read_text(encoding="utf-8") + '\n[sources.db]\ntype = "sqlite"\npath = "data/db.sqlite"\n', encoding="utf-8")
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert client.get("/api/w/late/status").json()["sources"] == [{"name": "db", "type": "sqlite"}]


def test_describe_workspace_counts(project, settings, store):
    runner = DiscoveryRunner(settings, store)
    runner.execute(runner.create_run("retail", "heuristic", {"max_iterations": 20})["id"])
    info = describe_workspace(project, "retail")
    assert info["counts"]["candidates_pending"] == 7 and info["counts"]["runs"] == 1
    assert json.dumps(info)
