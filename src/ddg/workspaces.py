"""Workspace management: create from template, describe, and switch."""

from __future__ import annotations

import shutil
from pathlib import Path

from .config import (
    TEMPLATE_NAME,
    WORKSPACE_FILENAME,
    WorkspaceError,
    active_workspace,
    list_workspaces,
    load_settings,
    validate_workspace_name,
    workspaces_dir,
)
from .context import markdown_files
from .topology import load_topology

# Only configuration is copied; runtime artifacts (runs, candidates, facts, ...) never are.
COPY_FILES = (WORKSPACE_FILENAME, "topology.md")


def create_workspace(root: Path, name: str, source: str = TEMPLATE_NAME, description: str = "") -> Path:
    validate_workspace_name(name)
    base = workspaces_dir(root)
    target = base / name
    if target.exists():
        raise WorkspaceError(f"Workspace '{name}' already exists")
    origin = base / source
    if not (origin / WORKSPACE_FILENAME).exists():
        raise WorkspaceError(f"Cannot copy from '{source}': {origin / WORKSPACE_FILENAME} not found")
    target.mkdir(parents=True)
    try:
        for filename in COPY_FILES:
            if (origin / filename).exists():
                shutil.copyfile(origin / filename, target / filename)
        for path in markdown_files(origin):
            destination = target / path.relative_to(origin)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        if description:
            _set_description(target / WORKSPACE_FILENAME, description)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def _set_description(path: Path, description: str) -> None:
    safe = description.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("description") and "=" in line:
            lines[i] = f'description = "{safe}"'
            break
    else:
        lines = ["[workspace]", f'description = "{safe}"', "", *lines]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def describe_workspace(root: Path, name: str) -> dict:
    settings = load_settings(root, name)
    topology = load_topology(settings.topology_file, strict=False)

    def count(directory: Path, pattern: str) -> int:
        return sum(1 for _ in directory.glob(pattern)) if directory.exists() else 0

    return {
        "name": name,
        "description": settings.description,
        "path": str(settings.workspace_dir),
        "active": name == active_workspace(root),
        "sources": [{"name": s.name, "type": s.type} for s in settings.sources.values()],
        "topology": {"title": topology.title, "version": topology.version, "is_valid": topology.is_valid,
                     "errors": len(topology.errors)},
        "counts": {
            "candidates_pending": sum(
                1 for p in settings.candidates_dir.glob("cand-*.json") if '"status": "pending"' in p.read_text(encoding="utf-8")
            ) if settings.candidates_dir.exists() else 0,
            "facts": count(settings.facts_dir, "fact-*.json"),
            "runs": count(settings.runs_dir, "run-*"),
            "graphs": count(settings.graph_output_dir, "*.json"),
        },
    }


def describe_all(root: Path) -> list[dict]:
    return [describe_workspace(root, name) for name in list_workspaces(root)]
