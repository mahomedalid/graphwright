from __future__ import annotations

import json
import runpy
import shutil
from pathlib import Path

import pytest

from ddg.config import load_settings
from ddg.store import Store

REPO = Path(__file__).resolve().parents[1]
RETAIL = REPO / "workspaces" / "retail"
TEMPLATE = REPO / "workspaces" / "_template"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    for name in ("FOUNDRY_PROJECT_ENDPOINT", "FOUNDRY_MODEL", "DDG_API_TOKEN", "DDG_ROOT", "DDG_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A throwaway repository root with the real ddg.toml, template, and retail workspace configuration."""
    root = tmp_path / "proj"
    retail = root / "workspaces" / "retail"
    (retail / "data").mkdir(parents=True)
    shutil.copyfile(REPO / "ddg.toml", root / "ddg.toml")
    shutil.copytree(TEMPLATE, root / "workspaces" / "_template")
    for name in ("workspace.toml", "topology.md", "expected_mappings.json"):
        shutil.copyfile(RETAIL / name, retail / name)
    shutil.copyfile(RETAIL / "data" / "build_fixture.py", retail / "data" / "build_fixture.py")
    runpy.run_path(str(retail / "data" / "build_fixture.py"))["build"](retail / "data" / "retail.sqlite")
    return root


@pytest.fixture
def settings(project: Path):
    return load_settings(project, "retail")


@pytest.fixture
def store(settings):
    return Store(settings)


@pytest.fixture
def expected() -> dict:
    return json.loads((RETAIL / "expected_mappings.json").read_text(encoding="utf-8"))


def retail_db(settings) -> Path:
    return settings.workspace_dir / "data" / "retail.sqlite"


def approve_all(store: Store, reviewer: str = "sme") -> None:
    for candidate in store.list_candidates("pending"):
        store.decide(candidate["id"], "approve", reviewer, "looks right", "", candidate["version"])
