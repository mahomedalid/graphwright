import json

import pytest

from ddg.adapters import create_adapter
from ddg.agent.base import IterationContext
from ddg.agent.foundry_planner import FoundryPlanner, build_agent_tools
from ddg.agent.runner import choose_planner
from ddg.control import RunControl, StopRun
from ddg.tools import DiscoveryTools
from ddg.topology import load_topology


def _tools(settings, store, control):
    adapter = create_adapter(settings, settings.source("retail"))
    return DiscoveryTools(settings, store, adapter, load_topology(settings.topology_file), control, "run-foundry")


def test_auto_planner_falls_back_without_foundry(settings):
    control = RunControl(settings.limits, settings.model, cancel_requested=lambda: False)
    assert choose_planner(settings, "auto", control).name == "heuristic"
    with pytest.raises(RuntimeError, match="FOUNDRY_PROJECT_ENDPOINT"):
        choose_planner(settings, "foundry", control)


def test_env_placeholders_are_treated_as_unset(project, monkeypatch):
    from ddg.config import load_settings

    (project / ".env").write_text(
        "FOUNDRY_PROJECT_ENDPOINT=https://<your-resource>.services.ai.azure.com/api/projects/<your-project>\n"
        "FOUNDRY_MODEL=<model-deployment-name>\n",
        encoding="utf-8",
    )
    settings = load_settings(project)
    assert not settings.model.configured
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://real.services.ai.azure.com/api/projects/p")
    monkeypatch.setenv("FOUNDRY_MODEL", "gpt-4.1")
    assert load_settings(project).model.configured


def test_agent_tool_wrappers_return_json(settings, store):
    control = RunControl(settings.limits, settings.model, cancel_requested=lambda: False)
    tools = _tools(settings, store, control)
    funcs = {f.__name__: f for f in build_agent_tools(tools)}
    assert set(funcs) >= {"list_tables", "check_join", "propose_candidate", "get_review_feedback"}
    tables = json.loads(funcs["list_tables"]())
    assert any(t["table"] == "CUST_MASTER" for t in tables["tables"])
    bad = json.loads(funcs["propose_candidate"]("node_mapping", "x", "y", ["ev-missing"], None))
    assert "Unknown evidence id" in bad["error"]


def test_foundry_planner_enforces_budget_before_model_call(settings, store):
    settings.model.project_endpoint = "https://example.services.ai.azure.com/api/projects/demo"
    settings.model.model = "gpt-test"
    control = RunControl(settings.limits, settings.model, cancel_requested=lambda: True)
    tools = _tools(settings, store, control)
    planner = FoundryPlanner(settings, control)
    ctx = IterationContext(1, "retail", load_topology(settings.topology_file), store.review_snapshot(), {})
    with pytest.raises(StopRun) as stop:
        planner.run_iteration(ctx, tools)
    assert stop.value.reason == "cancelled"
    assert control.usage.model_calls == 0
