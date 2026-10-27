"""Workspace Markdown loading and discovery integration tests."""

import pytest

from ddg.agent.base import IterationResult
from ddg.agent.runner import DiscoveryRunner
from ddg.config import load_settings
from ddg.context import load_context_documents
from ddg.workspaces import create_workspace


def test_given_nested_context_when_loading_then_only_markdown_references_are_read(settings):
    directory = settings.workspace_dir / "context"
    (directory / "prior").mkdir(parents=True)
    topology = directory / "topology.md"
    topology.write_text("graph definition", encoding="utf-8")
    (directory / "notes.md").write_text("# Notes\nCustomer means buyer.", encoding="utf-8")
    (directory / "prior" / "analysis.MD").write_text("Previous findings", encoding="utf-8")
    (directory / "ignored.csv").write_text("not reference context", encoding="utf-8")
    (settings.workspace_dir / "outside.md").write_text("not loaded", encoding="utf-8")

    documents = load_context_documents(settings.workspace_dir, topology)

    assert documents == [
        {"path": "context/notes.md", "content": "# Notes\nCustomer means buyer."},
        {"path": "context/prior/analysis.MD", "content": "Previous findings"},
    ]


def test_given_legacy_workspace_when_loading_then_context_is_optional(settings):
    assert load_context_documents(settings.workspace_dir, settings.topology_file) == []


def test_given_invalid_utf8_when_loading_then_error_is_explicit(settings):
    directory = settings.workspace_dir / "context"
    directory.mkdir()
    (directory / "notes.md").write_bytes(b"\xff")

    with pytest.raises(UnicodeDecodeError):
        load_context_documents(settings.workspace_dir, settings.topology_file)


def test_given_context_topology_when_resolving_then_explicit_config_wins(project, settings):
    directory = settings.workspace_dir / "context"
    directory.mkdir()
    (directory / "topology.md").write_text("new topology", encoding="utf-8")

    assert load_settings(project, "retail").topology_file == directory / "topology.md"
    with (settings.workspace_dir / "workspace.toml").open("a", encoding="utf-8") as handle:
        handle.write('\n[topology]\nfile = "topology.md"\n')
    assert load_settings(project, "retail").topology_file == settings.topology_file


def test_given_context_when_copying_workspace_then_nested_markdown_is_preserved(project, settings):
    directory = settings.workspace_dir / "context" / "prior"
    directory.mkdir(parents=True)
    (directory / "notes.md").write_text("Previous findings", encoding="utf-8")
    (directory / "ignored.csv").write_text("ignored", encoding="utf-8")

    target = create_workspace(project, "copy", source="retail")

    assert (target / "context" / "prior" / "notes.md").read_text(encoding="utf-8") == "Previous findings"
    assert not (target / "context" / "prior" / "ignored.csv").exists()
    assert load_settings(project, "copy").topology_file == target / "topology.md"


def test_given_references_when_running_then_planner_receives_snapshot(settings, store, monkeypatch):
    directory = settings.workspace_dir / "context"
    directory.mkdir()
    note = directory / "notes.md"
    note.write_text("Customer means buyer.", encoding="utf-8")
    contexts = []

    class Planner:
        name = "test"

        def run_iteration(self, ctx, tools):
            contexts.append(ctx)
            note.write_text("Edited after run started", encoding="utf-8")
            return IterationResult("done", {}, done=True)

    monkeypatch.setattr("ddg.agent.runner.choose_planner", lambda *args: Planner())
    runner = DiscoveryRunner(settings, store)

    result = runner.execute(runner.create_run("retail")["id"])

    assert result["status"] == "completed"
    assert result["context_files"] == ["context/notes.md"]
    assert contexts[0].context_documents == [
        {"path": "context/notes.md", "content": "Customer means buyer."}
    ]
    assert store.get_run(result["id"])["context_files"] == ["context/notes.md"]


def test_given_invalid_reference_when_running_then_run_fails(settings, store):
    directory = settings.workspace_dir / "context"
    directory.mkdir()
    (directory / "notes.md").write_bytes(b"\xff")
    runner = DiscoveryRunner(settings, store)

    result = runner.execute(runner.create_run("retail", "heuristic")["id"])

    assert result["status"] == "failed"
    assert "UnicodeDecodeError" in result["error"]
