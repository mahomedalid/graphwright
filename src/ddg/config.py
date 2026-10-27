"""Configuration: global defaults in ``ddg.toml`` plus one folder per workspace.

Layout::

    ddg.toml                    global defaults ([agent], [run], [sampling], [model], [workspaces])
    workspaces/
      _template/                blank starter copied by ``ddg workspace create``
      <name>/
        workspace.toml          [sources.*], [sampling] allowlist, optional [run]/[agent] overrides
        context/topology.md     user-authored graph topology (legacy: topology.md)
        context/**/*.md         additional unverified reference material
        data/                   optional local data (e.g. SQLite files)
        candidates/ facts/ metadata/ runs/ sandbox/ graph_output/   runtime artifacts
"""

from __future__ import annotations

import fnmatch
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

CONFIG_FILENAME = "ddg.toml"
WORKSPACE_FILENAME = "workspace.toml"
TEMPLATE_NAME = "_template"
ACTIVE_FILENAME = ".active"
WORKSPACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class WorkspaceError(ValueError):
    pass


@dataclass
class RunLimits:
    """Budgets that bound one autonomous run (FR-007, NFR-004)."""

    max_minutes: float = 10.0
    max_iterations: int = 20
    max_model_calls: int = 60
    max_tool_calls: int = 400
    max_cost_usd: float = 2.0
    stall_iterations: int = 2
    query_timeout_seconds: int = 30
    max_join_keys: int = 200_000

    def merged(self, overrides: dict[str, Any] | None) -> "RunLimits":
        data = dict(self.__dict__)
        for key, value in (overrides or {}).items():
            if key in data and value is not None:
                data[key] = type(data[key])(value)
        return RunLimits(**data)


@dataclass
class SamplingPolicy:
    """Raw-value privacy policy (NFR-002).

    Only columns matching an allowlist pattern (``table.column`` globs, case-insensitive)
    may expose raw values; everything else is reduced to aggregate metadata.
    """

    max_rows: int = 10
    max_top_values: int = 5
    allowlist: list[str] = field(default_factory=list)

    def is_allowed(self, table: str, column: str) -> bool:
        target = f"{table}.{column}".lower()
        return any(fnmatch.fnmatchcase(target, pattern.lower()) for pattern in self.allowlist)


@dataclass
class ModelSettings:
    """Foundry model settings; secrets and endpoints come from the environment."""

    project_endpoint: str | None = None
    model: str | None = None
    input_cost_per_1k: float = 0.0025
    output_cost_per_1k: float = 0.01
    max_tool_calls_per_iteration: int = 25

    @property
    def configured(self) -> bool:
        return bool(self.project_endpoint and self.model)


@dataclass
class SourceConfig:
    name: str
    type: str
    options: dict[str, Any]


@dataclass
class Settings:
    root: Path
    workspace: str
    workspace_dir: Path
    limits: RunLimits
    sampling: SamplingPolicy
    model: ModelSettings
    sources: dict[str, SourceConfig]
    topology_file: Path
    planner: str = "auto"
    api_token: str | None = None
    description: str = ""

    @property
    def facts_dir(self) -> Path:
        return self.workspace_dir / "facts"

    @property
    def approved_dir(self) -> Path:
        return self.facts_dir

    @property
    def candidates_dir(self) -> Path:
        return self.workspace_dir / "candidates"

    @property
    def metadata_dir(self) -> Path:
        return self.workspace_dir / "metadata"

    @property
    def sandbox_dir(self) -> Path:
        return self.workspace_dir / "sandbox"

    @property
    def runs_dir(self) -> Path:
        return self.workspace_dir / "runs"

    @property
    def graph_output_dir(self) -> Path:
        return self.workspace_dir / "graph_output"

    def source(self, name: str) -> SourceConfig:
        if name not in self.sources:
            raise KeyError(f"Unknown source '{name}' in workspace '{self.workspace}'. Configured sources: {sorted(self.sources)}")
        return self.sources[name]


def find_root(start: Path | None = None) -> Path:
    env_root = os.environ.get("DDG_ROOT")
    if env_root:
        return Path(env_root).resolve()
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / CONFIG_FILENAME).exists():
            return candidate
    return current


def _env(name: str) -> str | None:
    """Read an env var, treating empty values and unfilled <placeholders> as unset."""
    value = (os.environ.get(name) or "").strip()
    return None if not value or ("<" in value and ">" in value) else value


def _read_toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def global_config(root: Path) -> dict[str, Any]:
    return _read_toml(root / CONFIG_FILENAME)


def workspaces_dir(root: Path, raw: dict[str, Any] | None = None) -> Path:
    raw = raw if raw is not None else global_config(root)
    return root / raw.get("workspaces", {}).get("dir", "workspaces")


def validate_workspace_name(name: str) -> str:
    if not WORKSPACE_NAME_RE.match(name or ""):
        raise WorkspaceError(
            f"Invalid workspace name '{name}': use letters, digits, '-' or '_' (must not start with '_' or '-')"
        )
    return name


def list_workspaces(root: Path) -> list[str]:
    base = workspaces_dir(root)
    if not base.exists():
        return []
    return sorted(
        p.name for p in base.iterdir()
        if p.is_dir() and not p.name.startswith(("_", ".")) and (p / WORKSPACE_FILENAME).exists()
    )


def active_workspace(root: Path) -> str | None:
    """Resolution order: DDG_WORKSPACE, workspaces/.active, ddg.toml [workspaces].default, first available."""
    available = list_workspaces(root)
    candidates = [
        _env("DDG_WORKSPACE"),
        _read_active_file(root),
        global_config(root).get("workspaces", {}).get("default"),
    ]
    for name in candidates:
        if name and name in available:
            return name
    return available[0] if available else None


def _read_active_file(root: Path) -> str | None:
    path = workspaces_dir(root) / ACTIVE_FILENAME
    return path.read_text(encoding="utf-8").strip() or None if path.exists() else None


def set_active_workspace(root: Path, name: str) -> None:
    if name not in list_workspaces(root):
        raise WorkspaceError(f"Workspace '{name}' does not exist. Available: {list_workspaces(root)}")
    path = workspaces_dir(root) / ACTIVE_FILENAME
    path.write_text(name + "\n", encoding="utf-8")


def load_settings(root: Path | None = None, workspace: str | None = None) -> Settings:
    root = (root or find_root()).resolve()
    load_dotenv(root / ".env", override=False)
    raw = global_config(root)
    name = workspace or active_workspace(root)
    if not name:
        raise WorkspaceError(
            f"No workspaces found in {workspaces_dir(root, raw)}. Create one with 'ddg workspace create <name>'."
        )
    validate_workspace_name(name)
    ws_dir = workspaces_dir(root, raw) / name
    if not (ws_dir / WORKSPACE_FILENAME).exists():
        raise WorkspaceError(f"Workspace '{name}' not found (expected {ws_dir / WORKSPACE_FILENAME})")
    ws = _read_toml(ws_dir / WORKSPACE_FILENAME)

    def section(key: str) -> dict[str, Any]:
        return {**raw.get(key, {}), **ws.get(key, {})}

    limits = RunLimits().merged(section("run"))
    sampling_cfg = section("sampling")
    sampling = SamplingPolicy(
        max_rows=int(sampling_cfg.get("max_rows", 10)),
        max_top_values=int(sampling_cfg.get("max_top_values", 5)),
        allowlist=list(ws.get("sampling", {}).get("allowlist", [])),
    )
    model_cfg = raw.get("model", {})
    model = ModelSettings(
        project_endpoint=_env("FOUNDRY_PROJECT_ENDPOINT"),
        model=_env("FOUNDRY_MODEL"),
        input_cost_per_1k=float(model_cfg.get("input_cost_per_1k", 0.0025)),
        output_cost_per_1k=float(model_cfg.get("output_cost_per_1k", 0.01)),
        max_tool_calls_per_iteration=int(model_cfg.get("max_tool_calls_per_iteration", 25)),
    )
    sources = {
        source_name: SourceConfig(
            name=source_name, type=str(cfg.get("type", "")), options={k: v for k, v in cfg.items() if k != "type"}
        )
        for source_name, cfg in ws.get("sources", {}).items()
    }
    return Settings(
        root=root,
        workspace=name,
        workspace_dir=ws_dir,
        limits=limits,
        sampling=sampling,
        model=model,
        sources=sources,
        topology_file=ws_dir / ws.get("topology", {}).get(
            "file", "context/topology.md" if (ws_dir / "context" / "topology.md").exists() else "topology.md"
        ),
        planner=str(section("agent").get("planner", "auto")),
        api_token=_env("DDG_API_TOKEN"),
        description=str(ws.get("workspace", {}).get("description", "")),
    )
