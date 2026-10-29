"""Free-form topology extraction, caching, and application integration."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ddg.agent.heuristic import HeuristicPlanner
from ddg.agent.runner import DiscoveryRunner
from ddg.agent.topology_parser import TopologyDocument, parse_with_foundry
from ddg.api import create_app
from ddg.cli import main
from ddg.codegen import generate
from ddg.control import RunControl, StopRun
from ddg.topology import TopologyError, load_workspace_topology

from conftest import approve_all


@pytest.fixture
def freeform(settings):
    canonical = settings.topology_file.read_text(encoding="utf-8")
    settings.topology_file.write_text(
        "Retail: customers place orders. Orders contain products and are sold at stores.",
        encoding="utf-8",
    )
    return canonical


@pytest.fixture
def configured(settings, monkeypatch):
    endpoint = "https://example.services.ai.azure.com/api/projects/demo"
    settings.model.project_endpoint = endpoint
    settings.model.model = "gpt-test"
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", endpoint)
    monkeypatch.setenv("FOUNDRY_MODEL", "gpt-test")
    return settings


def test_given_freeform_without_foundry_when_loading_then_both_remedies_are_reported(settings, freeform):
    with pytest.raises(TopologyError) as error:
        load_workspace_topology(settings, resolve=True)

    assert "Foundry is not configured" in str(error.value)
    assert "FOUNDRY_PROJECT_ENDPOINT" in str(error.value)
    assert "## Node Types" in str(error.value)


def test_given_structured_topology_when_loading_then_foundry_is_not_called(configured, monkeypatch):
    def unexpected_call(*args):
        pytest.fail("Structured topology must not call Foundry")

    monkeypatch.setattr("ddg.agent.topology_parser.parse_with_foundry", unexpected_call)

    assert load_workspace_topology(configured, resolve=True).parsing_mode == "structured"


def test_given_freeform_when_viewing_ui_then_extraction_is_pending_without_model_calls(
    project, configured, freeform, monkeypatch
):
    def unexpected_call(*args):
        pytest.fail("GET requests must not call Foundry")

    monkeypatch.setattr("ddg.agent.topology_parser.parse_with_foundry", unexpected_call)
    client = TestClient(create_app(project))

    topology = client.get("/api/w/retail/topology").json()
    workspace = client.get("/api/workspaces").json()["workspaces"][0]

    assert topology["parsing_mode"] == "pending_foundry"
    assert not topology["is_valid"]
    assert workspace["topology"]["parsing_mode"] == "pending_foundry"


def test_given_freeform_when_resolving_then_validated_cache_is_reused_and_source_is_preserved(
    configured, freeform, monkeypatch
):
    calls = []

    def extract(text, settings, control):
        calls.append(text)
        return freeform

    monkeypatch.setattr("ddg.agent.topology_parser.parse_with_foundry", extract)
    original = configured.topology_file.read_text(encoding="utf-8")

    resolved = load_workspace_topology(configured, resolve=True)
    cached = load_workspace_topology(configured, resolve=True)

    assert resolved.is_valid and resolved.parsing_mode == "foundry"
    assert len(calls) == 1
    assert cached.to_dict() == resolved.to_dict()
    assert configured.topology_file.read_text(encoding="utf-8") == original
    assert (configured.metadata_dir / "topology.json").is_file()

    configured.topology_file.write_text(original + "\nAdditional business requirement.", encoding="utf-8")
    assert load_workspace_topology(configured, strict=False).parsing_mode == "pending_foundry"
    refreshed = load_workspace_topology(configured, resolve=True)
    assert len(calls) == 2 and refreshed.version != resolved.version


@pytest.mark.parametrize("markdown", ["", "## Node Types\n### A\n## Edge Types\n### LINKS\n- From: A\n- To: Missing"])
def test_given_invalid_extraction_when_resolving_then_no_cache_is_written(
    configured, freeform, monkeypatch, markdown
):
    monkeypatch.setattr("ddg.agent.topology_parser.parse_with_foundry", lambda *args: markdown)

    with pytest.raises(TopologyError):
        load_workspace_topology(configured, resolve=True)

    assert not (configured.metadata_dir / "topology.json").exists()


def test_given_foundry_failure_when_resolving_then_error_is_not_hidden(configured, freeform, monkeypatch):
    def denied(*args):
        raise PermissionError("403: model invocation denied")

    monkeypatch.setattr("ddg.agent.topology_parser.parse_with_foundry", denied)

    with pytest.raises(PermissionError, match="403"):
        load_workspace_topology(configured, resolve=True)

    assert not (configured.metadata_dir / "topology.json").exists()


def test_given_missing_topology_when_resolving_then_foundry_does_not_invent_one(configured, monkeypatch):
    configured.topology_file.unlink()

    def unexpected_call(*args):
        pytest.fail("Missing files must not be sent to Foundry")

    monkeypatch.setattr("ddg.agent.topology_parser.parse_with_foundry", unexpected_call)

    with pytest.raises(TopologyError, match="file not found"):
        load_workspace_topology(configured, resolve=True)


def test_given_explicit_offline_run_when_topology_needs_parsing_then_no_model_is_called(
    configured, freeform, store, monkeypatch
):
    def unexpected_call(*args):
        pytest.fail("Explicit heuristic runs must not invoke Foundry")

    monkeypatch.setattr("ddg.agent.topology_parser.parse_with_foundry", unexpected_call)
    runner = DiscoveryRunner(configured, store)

    run = runner.execute(runner.create_run("retail", "heuristic")["id"])

    assert run["termination_reason"] == "invalid_topology"
    assert "needs Foundry parsing" in run["error"]
    assert run["usage"]["model_calls"] == 0


def test_given_extracted_topology_when_discovering_then_ui_revalidation_and_codegen_use_same_version(
    project, configured, freeform, store, monkeypatch
):
    def extract(text, settings, control):
        control.before_model_call()
        control.record_tokens(100, 50)
        return freeform

    monkeypatch.setattr("ddg.agent.topology_parser.parse_with_foundry", extract)
    monkeypatch.setattr("ddg.agent.runner.choose_planner", lambda *args: HeuristicPlanner())
    runner = DiscoveryRunner(configured, store)

    run = runner.execute(runner.create_run("retail", "auto", {"max_iterations": 20})["id"])

    assert run["status"] == "completed", run["error"]
    assert run["usage"]["model_calls"] == 1
    assert run["usage"]["input_tokens"] == 100
    assert run["usage"]["output_tokens"] == 50
    assert len(store.list_candidates()) == 7
    approve_all(store)
    client = TestClient(create_app(project))
    topology = client.get("/api/w/retail/topology").json()
    assert topology["parsing_mode"] == "foundry"
    assert topology["version"] == run["topology_version"]
    assert client.get("/api/w/retail/facts/revalidation?source=retail").json()["issues"] == {}
    generated = generate(configured, "retail", store=store)
    assert generated["topology_version"] == run["topology_version"]

    configured.model.project_endpoint = None
    configured.model.model = None
    assert load_workspace_topology(configured).version == run["topology_version"]


def test_given_freeform_when_cli_checks_topology_then_extraction_is_cached(
    project, configured, freeform, monkeypatch, capsys
):
    monkeypatch.chdir(project)
    monkeypatch.setattr("ddg.agent.topology_parser.parse_with_foundry", lambda *args: freeform)

    result = main(["-w", "retail", "topology", "--json"])

    assert result == 0
    assert json.loads(capsys.readouterr().out)["parsing_mode"] == "foundry"
    assert (configured.metadata_dir / "topology.json").exists()


@pytest.mark.parametrize("cancelled,max_calls,reason", [(True, 5, "cancelled"), (False, 0, "model_call_limit")])
def test_given_exhausted_budget_when_parsing_then_no_request_is_dispatched(configured, cancelled, max_calls, reason):
    configured.limits.max_model_calls = max_calls
    control = RunControl(configured.limits, configured.model, cancel_requested=lambda: cancelled)

    with pytest.raises(StopRun) as error:
        parse_with_foundry("Customers place orders.", configured, control)

    assert error.value.reason == reason
    assert control.usage.model_calls == 0


def test_given_foundry_response_when_parsing_then_typed_output_and_usage_are_checked(configured, monkeypatch):
    import agent_framework
    import agent_framework.foundry
    import azure.identity.aio

    requests = []
    control = RunControl(configured.limits, configured.model, cancel_requested=lambda: False)

    class Credential:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Agent:
        def __init__(self, client, instructions, **kwargs):
            assert "untrusted data" in instructions
            assert "tools" not in kwargs

        async def run(self, prompt, *, options, client_kwargs):
            requests.append(prompt)
            assert options["response_format"] is TopologyDocument
            assert 0 < client_kwargs["timeout"] <= configured.limits.max_minutes * 60
            return SimpleNamespace(
                value=TopologyDocument(markdown="## Node Types\n### Customer\n- Key: id"),
                usage_details={"input_token_count": 123, "output_token_count": 45},
            )

    monkeypatch.setattr(azure.identity.aio, "DefaultAzureCredential", Credential)
    monkeypatch.setattr(agent_framework.foundry, "FoundryChatClient", lambda **kwargs: object())
    monkeypatch.setattr(agent_framework, "Agent", Agent)

    markdown = parse_with_foundry("Customers have IDs.", configured, control)

    assert "### Customer" in markdown
    assert "Customers have IDs." in requests[0]
    assert control.usage.model_calls == 1
    assert control.usage.input_tokens == 123 and control.usage.output_tokens == 45


def test_given_pending_topology_when_generating_via_api_then_error_is_actionable(project, configured, freeform):
    client = TestClient(create_app(project))

    response = client.post("/api/w/retail/codegen", json={"source": "retail"})

    assert response.status_code == 422
    assert "needs Foundry parsing" in response.json()["errors"][0]


def test_given_concurrent_runs_when_extracting_then_only_one_interpretation_is_created(
    configured, freeform, monkeypatch
):
    calls = []

    def extract(*args):
        calls.append(True)
        time.sleep(0.1)
        return freeform

    monkeypatch.setattr("ddg.agent.topology_parser.parse_with_foundry", extract)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: load_workspace_topology(configured, resolve=True), range(2)))

    assert len(calls) == 1
    assert results[0].to_dict() == results[1].to_dict()


def test_given_source_edit_during_extraction_when_resolving_then_stale_result_is_not_cached(
    configured, freeform, monkeypatch
):
    def extract(*args):
        configured.topology_file.write_text("A different graph definition.", encoding="utf-8")
        return freeform

    monkeypatch.setattr("ddg.agent.topology_parser.parse_with_foundry", extract)

    with pytest.raises(ValueError, match="Topology changed during"):
        load_workspace_topology(configured, resolve=True)

    assert not (configured.metadata_dir / "topology.json").exists()
