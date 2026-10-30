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


@pytest.mark.parametrize("validate_topology", [False, True])
@pytest.mark.parametrize("discovery_scope", ["all", "edges"])
def test_given_references_when_foundry_runs_then_prompt_contains_full_text_and_paths(
    settings, store, monkeypatch, validate_topology, discovery_scope, expected,
):
    import agent_framework
    import agent_framework.foundry
    import azure.identity.aio
    from types import SimpleNamespace

    settings.model.project_endpoint = "https://example.services.ai.azure.com/api/projects/demo"
    settings.model.model = "gpt-test"
    control = RunControl(settings.limits, settings.model, cancel_requested=lambda: False)
    tools = _tools(settings, store, control)
    tools.validate_topology = validate_topology
    tools.discovery_scope = discovery_scope
    for node, mapping in expected["nodes"].items():
        store.add_candidate({
            "kind": "node_mapping", "hypothesis": f"Existing {node} mapping",
            "mapping": {"node_type": node, "identity_namespace": f"{node}_ID", **mapping},
            "scope": {"source": "retail"}, "evidence": [],
        })
    alternate = store.add_candidate({
        "kind": "node_mapping", "hypothesis": "Alternative Customer source",
        "mapping": {"node_type": "Customer", "source_table": "alternate_endpoint_source",
                    "key_columns": ["source_id", "local_id"], "identity_namespace": "ALTERNATE_CUSTOMER_ID"},
        "scope": {"source": "retail"}, "evidence": [],
    })
    prompts = []
    instructions = []

    class Credential:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Agent:
        def __init__(self, client, instruction_text, **kwargs):
            assert client.function_invocation_configuration["allow_concurrent_invocation"] is False
            instructions.append(instruction_text)

        async def run(self, prompt, *, client_kwargs):
            assert client_kwargs["timeout"] > 0
            prompts.append(prompt)
            return SimpleNamespace(text="DONE")

    monkeypatch.setattr(azure.identity.aio, "DefaultAzureCredential", Credential)
    monkeypatch.setattr(agent_framework.foundry, "FoundryChatClient", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(agent_framework, "Agent", Agent)
    documents = [{"path": "context/prior/notes.md", "content": "Previous findings\n" + "x" * 13_000}]
    ctx = IterationContext(
        1, "retail", load_topology(settings.topology_file), store.review_snapshot(), context_documents=documents
    )

    result = FoundryPlanner(settings, control).run_iteration(ctx, tools)

    assert result.done is (not validate_topology and discovery_scope == "all")
    assert json.dumps(documents, ensure_ascii=False) in prompts[0]
    assert "untrusted background data" in instructions[0]
    assert "Existing endpoint mappings for this source" in prompts[0]
    assert alternate["id"] in prompts[0] and "alternate_endpoint_source" in prompts[0]
    assert '"identity_namespace"' in prompts[0] and '"mapping_id"' in prompts[0]
    assert ("TOPOLOGY VALIDATION" in instructions[0]) is validate_topology
    assert ("Discovery scope: EDGES ONLY." in instructions[0]) is (discovery_scope == "edges")
    if discovery_scope == "edges":
        assert "Build the data dictionary" not in instructions[0]
        assert "Existing endpoint mappings for this source" in prompts[0]
        assert '"Customer"' in prompts[0]
    assert ("record_topology_finding" in {function.__name__ for function in build_agent_tools(tools)}) is validate_topology


@pytest.mark.parametrize("outcome", ["success", "error", "timeout", "middleware_error"])
def test_given_discovery_model_call_when_middleware_runs_then_progress_and_failures_are_reported(
    settings, store, monkeypatch, outcome
):
    import asyncio
    from types import SimpleNamespace

    import agent_framework
    import agent_framework.foundry
    import azure.identity.aio

    settings.model.project_endpoint = "https://example.services.ai.azure.com/api/projects/demo"
    settings.model.model = "gpt-test"
    if outcome == "timeout":
        settings.limits.max_minutes = 0.001
    events = []
    control = RunControl(settings.limits, settings.model, lambda: False, on_event=events.append)
    tools = _tools(settings, store, control)

    class Credential:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Agent:
        def __init__(self, client, instructions, **kwargs):
            self.client = client

        async def run(self, prompt, *, client_kwargs):
            context = SimpleNamespace(result=None)

            async def next_call():
                if outcome == "error":
                    raise RuntimeError("model invocation failed")
                if outcome == "middleware_error":
                    raise agent_framework.MiddlewareFailure("unexpected middleware failure")
                if outcome == "timeout":
                    await asyncio.sleep(10)
                context.result = SimpleNamespace(usage_details={"input_token_count": 123, "output_token_count": 45})

            await self.client.middleware[0].process(context, next_call)
            return SimpleNamespace(text="DONE")

    monkeypatch.setattr(azure.identity.aio, "DefaultAzureCredential", Credential)
    monkeypatch.setattr(agent_framework.foundry, "FoundryChatClient", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(agent_framework, "Agent", Agent)
    ctx = IterationContext(1, "retail", load_topology(settings.topology_file), store.review_snapshot(), {})
    planner = FoundryPlanner(settings, control)

    if outcome == "success":
        assert planner.run_iteration(ctx, tools).done
        assert events[-1]["type"] == "model_call_completed"
        assert events[-1]["usage"]["input_tokens"] == 123
    elif outcome == "error":
        with pytest.raises(RuntimeError, match="model invocation failed"):
            planner.run_iteration(ctx, tools)
        assert events[-1]["error_type"] == "RuntimeError"
    elif outcome == "middleware_error":
        with pytest.raises(agent_framework.MiddlewareFailure, match="unexpected"):
            planner.run_iteration(ctx, tools)
    else:
        with pytest.raises(StopRun) as error:
            planner.run_iteration(ctx, tools)
        assert error.value.reason == "time_limit"
        assert events[-1]["reason"] == "time_limit"
    assert events[0]["type"] == "model_call_started"
    assert control.usage.model_calls == 1
