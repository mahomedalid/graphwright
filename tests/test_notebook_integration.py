"""Notebook target routing and workspace-scoped package downloads."""

import json
import sys
import types
import zipfile
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from ddg import api, cli
from ddg.codegen import CodegenError
from ddg.workspaces import create_workspace


@pytest.fixture
def client(project):
    return TestClient(api.create_app(project))


@pytest.fixture
def exporter(monkeypatch):
    calls = []

    def generate(settings, source, **kwargs):
        calls.append((settings, source, kwargs))
        return {
            "target": "databricks-notebooks", "path": str(settings.sandbox_dir / "generated" / "notebooks.zip"),
            "builder": "notebooks.zip", "warnings": [], "facts": [], "source_bindings": {},
        }

    module = types.ModuleType("ddg.databricks_export")
    module.generate = generate
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module, calls


def test_given_notebook_target_when_posting_then_forwards_bindings_and_exclusions(client, exporter):
    _, calls = exporter
    response = client.post("/api/w/retail/codegen", json={
        "source": "retail", "target": "databricks-notebooks",
        "table_bindings": {"CUST_MASTER": "catalog.schema.customers"}, "exclude_fact_ids": ["fact-left-out"],
    })

    assert response.status_code == 200
    assert response.json()["target"] == "databricks-notebooks"
    settings, source, kwargs = calls[0]
    assert settings.workspace == source == "retail"
    assert kwargs["store"].settings.workspace == "retail"
    assert kwargs["table_bindings"] == {"CUST_MASTER": "catalog.schema.customers"}
    assert kwargs["exclude_fact_ids"] == ["fact-left-out"]
    assert "allow_stale" not in kwargs


def test_given_notebook_error_when_posting_then_preserves_actionable_errors(client, exporter):
    module, _ = exporter

    def fail(*args, **kwargs):
        raise CodegenError(["fact-1: Missing binding for sample_table; supply catalog.schema.table"], ["Review approval"])

    module.generate = fail
    response = client.post("/api/w/retail/codegen", json={"source": "retail", "target": "databricks-notebooks"})

    assert response.status_code == 422
    assert "Missing binding" in response.json()["errors"][0]
    assert response.json()["warnings"] == ["Review approval"]


@pytest.mark.parametrize("body", [
    {"target": "unknown"},
    {"target": "databricks-notebooks", "table_bindings": []},
    {"target": "databricks-notebooks", "table_bindings": {"sample": 12}},
    {"target": "databricks-notebooks", "allow_stale": True},
    {"table_bindings": {}},
])
def test_given_invalid_target_options_when_posting_then_rejects(client, exporter, body):
    response = client.post("/api/w/retail/codegen", json={"source": "retail", **body})
    assert response.status_code == 422
    assert exporter[1] == []


def test_given_default_target_when_posting_then_keeps_standalone_options(client, monkeypatch):
    calls = []
    monkeypatch.setattr(api, "generate", lambda settings, source, **kwargs: calls.append(kwargs) or {"builder": "builder.py"})

    response = client.post("/api/w/retail/codegen", json={
        "source": "retail", "allow_stale": True, "exclude_fact_ids": ["fact-1"],
    })

    assert response.json() == {"builder": "builder.py"}
    assert calls[0]["allow_stale"] is True
    assert calls[0]["exclude_fact_ids"] == ["fact-1"]


@pytest.fixture
def package(settings):
    directory = settings.sandbox_dir / "generated"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "notebooks.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("00_config.py", "# Databricks notebook source\n")
    manifest = {
        "target": "databricks-notebooks", "workspace": "retail", "builder": path.name,
        "source": "retail", "facts": [], "warnings": [],
    }
    path.with_suffix(".manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path, manifest


def test_given_generated_package_when_listing_and_downloading_then_returns_scoped_zip(client, package):
    path, manifest = package
    assert manifest in client.get("/api/w/retail/codegen").json()
    response = client.get(f"/api/w/retail/codegen/{path.name}/download")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="notebooks.zip"' in response.headers["content-disposition"]
    assert response.content == path.read_bytes()


@pytest.mark.parametrize("change", ["missing", "corrupt", "workspace", "target", "builder", "list"])
def test_given_unregistered_or_mismatched_package_when_downloading_then_not_found(client, package, change):
    path, manifest = package
    sidecar = path.with_suffix(".manifest.json")
    if change == "missing":
        sidecar.unlink()
    elif change == "corrupt":
        sidecar.write_text("{", encoding="utf-8")
    elif change == "list":
        sidecar.write_text("[]", encoding="utf-8")
    else:
        manifest[change] = "not-the-generated-value"
        sidecar.write_text(json.dumps(manifest), encoding="utf-8")
    assert client.get("/api/w/retail/codegen/notebooks.zip/download").status_code == 404


@pytest.mark.parametrize("name", [
    "missing.zip", "builder.py", "..%2Fnotebooks.zip", "..%5Cnotebooks.zip",
    "C%3Anotebooks.zip", "%2E%2E%5C%2E%2E%5Cddg.toml", "notebooks.zip%00",
])
def test_given_unsafe_or_unknown_filename_when_downloading_then_not_found(client, package, name):
    assert client.get(f"/api/w/retail/codegen/{name}/download").status_code == 404


def test_given_other_workspace_when_downloading_then_cannot_access_original_package(client, project, package):
    create_workspace(project, "other")
    assert client.get("/api/w/other/codegen/notebooks.zip/download").status_code == 404


def test_given_token_auth_when_downloading_then_requires_authorization(project, package, monkeypatch):
    monkeypatch.setenv("DDG_API_TOKEN", "test-token")
    with TestClient(api.create_app(project)) as client:
        url = "/api/w/retail/codegen/notebooks.zip/download"
        assert client.get(url).status_code == 401
        assert client.get(url, headers={"Authorization": "Bearer test-token"}).status_code == 200


def test_given_notebook_cli_when_generating_then_forwards_json_and_exclusions(settings, exporter, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_settings", lambda args: settings)
    result = cli.main([
        "generate", "--source", "retail", "--target", "databricks-notebooks",
        "--table-bindings", '{"CUST_MASTER":"catalog.schema.customers"}',
        "--exclude-fact", "fact-1", "--exclude-fact", "fact-2",
    ])
    assert result == 0
    assert exporter[1][0][2] == {
        "table_bindings": {"CUST_MASTER": "catalog.schema.customers"},
        "exclude_fact_ids": ["fact-1", "fact-2"],
    }
    output = capsys.readouterr().out
    assert "Extract the ZIP" in output
    assert "four Python source notebooks into the same Databricks folder" in output
    assert "Resolved source bindings" in output
    assert "Nothing was executed" in output


def test_given_documented_codegen_alias_when_parsing_then_preserves_workspace_and_target(settings, exporter, monkeypatch, capsys):
    workspaces = []
    monkeypatch.setattr(cli, "_settings", lambda args: workspaces.append(args.workspace) or settings)
    exporter[0].generate = lambda *args, **kwargs: {
        "target": "databricks-notebooks", "path": "notebooks.zip",
        "warnings": ["Review full-data key uniqueness"],
        "source_bindings": {"sample": "catalog.schema.original"},
    }

    assert cli.main([
        "--workspace", "mydata", "codegen", "--source", "local", "--target", "databricks-notebooks",
    ]) == 0
    assert workspaces == ["mydata"]
    output = capsys.readouterr().out
    assert "catalog.schema.original" in output
    assert "WARNING: Review full-data key uniqueness" in output
    assert "Extract the ZIP" in output


@pytest.mark.parametrize("options", [
    ["--target", "databricks-notebooks", "--table-bindings", "broken"],
    ["--target", "databricks-notebooks", "--table-bindings", "[]"],
    ["--target", "databricks-notebooks", "--table-bindings", '{"sample":1}'],
    ["--target", "databricks-notebooks", "--allow-stale"],
    ["--table-bindings", "{}"],
])
def test_given_invalid_cli_options_when_generating_then_actionable_error(exporter, capsys, options):
    assert cli.main(["generate", "--source", "retail", *options]) == 2
    assert exporter[1] == []
    assert capsys.readouterr().err


def test_given_cli_export_error_when_generating_then_reports_without_traceback(exporter, capsys):
    def fail(*args, **kwargs):
        raise CodegenError(["Missing binding for sample; supply catalog.schema.table"])

    exporter[0].generate = fail
    assert cli.main(["generate", "--source", "retail", "--target", "databricks-notebooks"]) == 2
    assert "Missing binding" in capsys.readouterr().err


def test_given_default_cli_target_when_generating_then_preserves_standalone_ux(settings, monkeypatch, capsys):
    import ddg.codegen

    monkeypatch.setattr(cli, "_settings", lambda args: settings)
    calls = []
    monkeypatch.setattr(ddg.codegen, "generate", lambda *args, **kwargs: calls.append(kwargs) or {
        "path": "builder.py", "warnings": [], "suggested_command": "python builder.py --out graph.json",
    })
    assert cli.main(["generate", "--source", "retail", "--allow-stale", "--exclude-fact", "fact-1"]) == 0
    assert calls == [{"allow_stale": True, "exclude_fact_ids": ["fact-1"]}]
    assert "python builder.py --out graph.json" in capsys.readouterr().out


def test_given_approved_mapping_when_exporting_via_api_then_lists_and_downloads_real_notebooks(client, store):
    candidate = store.add_candidate({
        "kind": "node_mapping", "hypothesis": "Customers from the sample",
        "mapping": {
            "node_type": "Customer", "source_table": "CUST_MASTER", "key_columns": ["cust_id"],
            "identity_namespace": "CUSTOMER_ID",
        },
        "scope": {"source": "retail"}, "evidence": [{"id": "sample-evidence"}],
    })
    store.decide(candidate["id"], "approve", "reviewer", "Reviewed mapping", expected_version=1)

    response = client.post("/api/w/retail/codegen", json={
        "source": "retail", "target": "databricks-notebooks",
        "table_bindings": {"CUST_MASTER": "catalog.schema.customers"},
    })

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["target"] == "databricks-notebooks"
    assert result["builder"].endswith(".zip")
    assert any(item["builder"] == result["builder"] for item in client.get("/api/w/retail/codegen").json())
    download = client.get(f"/api/w/retail/codegen/{result['builder']}/download")
    assert download.status_code == 200
    with zipfile.ZipFile(BytesIO(download.content)) as archive:
        assert len([name for name in archive.namelist() if name.endswith(".py")]) >= 4
        assert any(name.endswith(".json") for name in archive.namelist())
