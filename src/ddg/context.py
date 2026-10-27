"""Load workspace Markdown references without interpreting or executing them."""

from pathlib import Path


def markdown_files(workspace_dir: Path) -> list[Path]:
    """List context Markdown files recursively, rejecting paths outside the workspace."""
    directory = workspace_dir / "context"
    if not directory.exists():
        return []
    root = workspace_dir.resolve()
    if not directory.resolve().is_relative_to(root):
        raise ValueError(f"Context directory must stay inside the workspace: {directory}")
    if not directory.is_dir():
        raise ValueError(f"Context path must be a directory: {directory}")
    paths = sorted(
        (p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() == ".md"),
        key=lambda p: p.relative_to(directory).as_posix(),
    )
    for path in paths:
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"Context Markdown must stay inside the workspace: {path}")
    return paths


def load_context_documents(workspace_dir: Path, topology_file: Path) -> list[dict[str, str]]:
    """Read UTF-8 references with workspace-relative filenames, excluding the topology."""
    return [
        {"path": path.relative_to(workspace_dir).as_posix(), "content": path.read_text(encoding="utf-8")}
        for path in markdown_files(workspace_dir)
        if path.resolve() != topology_file.resolve()
    ]
