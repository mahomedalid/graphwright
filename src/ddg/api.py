"""FastAPI backend: workspaces, run management, SME review, code generation, and graph visualization.

Global routes live under ``/api`` and workspace-scoped routes under ``/api/w/{workspace}``.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .adapters import create_adapter
from .agent.runner import ACTIVE_STATES, DiscoveryRunner
from .codegen import CodegenError, generate, list_generated
from .config import (
    CONFIG_FILENAME,
    TEMPLATE_NAME,
    WORKSPACE_FILENAME,
    Settings,
    WorkspaceError,
    _env,
    active_workspace,
    find_root,
    list_workspaces,
    load_settings,
    set_active_workspace,
    workspaces_dir,
)
from .graph import GraphFormatError, GraphRepository
from .mappings import revalidation_issues
from .store import ConflictError, Store, read_json
from .topology import load_topology
from .workspaces import create_workspace, describe_all, describe_workspace

STATIC_DIR = Path(__file__).parent / "ui" / "static"


class RunRequest(BaseModel):
    source: str
    planner: str | None = None
    limits: dict[str, Any] = Field(default_factory=dict)


class ResumeRequest(BaseModel):
    planner: str | None = None
    limits: dict[str, Any] = Field(default_factory=dict)


class DecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    reviewer: str = Field(min_length=1, max_length=200)
    notes: str = Field(default="", max_length=5000)
    observations: str = Field(default="", max_length=5000)
    expected_version: int


class CodegenRequest(BaseModel):
    source: str
    allow_stale: bool = False


class WorkspaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    copy_from: str = TEMPLATE_NAME


@dataclass
class WorkspaceContext:
    settings: Settings
    store: Store
    runner: DiscoveryRunner
    graphs: GraphRepository
    stamp: tuple


class WorkspaceRegistry:
    """Caches one context per workspace; reloads when ddg.toml or workspace.toml changes."""

    def __init__(self, root: Path):
        self.root = root
        self._contexts: dict[str, WorkspaceContext] = {}
        self._lock = threading.Lock()

    def _stamp(self, name: str) -> tuple:
        files = [self.root / CONFIG_FILENAME, workspaces_dir(self.root) / name / WORKSPACE_FILENAME]
        return tuple(f.stat().st_mtime if f.exists() else 0 for f in files)

    def get(self, name: str) -> WorkspaceContext:
        if name not in list_workspaces(self.root):
            raise HTTPException(404, f"Workspace '{name}' not found")
        with self._lock:
            stamp = self._stamp(name)
            ctx = self._contexts.get(name)
            if ctx and ctx.stamp == stamp:
                return ctx
            try:
                settings = load_settings(self.root, name)
            except (WorkspaceError, ValueError) as exc:
                raise HTTPException(422, f"Invalid workspace configuration: {exc}") from exc
            store = Store(settings)
            runner = DiscoveryRunner(settings, store)
            if ctx is None:
                runner.recover_stale_runs()
                store.reconcile()
            ctx = WorkspaceContext(settings, store, runner, GraphRepository(settings.graph_output_dir), stamp)
            self._contexts[name] = ctx
            return ctx


def create_app(root: Path | None = None) -> FastAPI:
    root = (root or find_root()).resolve()
    load_dotenv(root / ".env", override=False)
    api_token = _env("DDG_API_TOKEN")
    registry = WorkspaceRegistry(root)
    threads: dict[str, threading.Thread] = {}

    app = FastAPI(title="Data Discovery Graph", version=__version__)

    def require_token(request: Request) -> None:
        if not api_token:
            return
        token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(token, api_token):
            raise HTTPException(status_code=401, detail="Missing or invalid API token")

    auth = [Depends(require_token)]

    def workspace_ctx(workspace: str) -> WorkspaceContext:
        return registry.get(workspace)

    def start_thread(ctx: WorkspaceContext, run_id: str) -> None:
        thread = threading.Thread(target=ctx.runner.execute, args=(run_id,), name=run_id, daemon=True)
        threads[run_id] = thread
        thread.start()

    # ---- global -------------------------------------------------------------------
    @app.get("/api/status", dependencies=auth)
    def status() -> dict:
        probe = _env("FOUNDRY_PROJECT_ENDPOINT") and _env("FOUNDRY_MODEL")
        return {
            "version": __version__,
            "root": str(root),
            "foundry_configured": bool(probe),
            "auth_required": bool(api_token),
            "active_workspace": active_workspace(root),
        }

    @app.get("/api/workspaces", dependencies=auth)
    def workspaces() -> dict:
        return {"active": active_workspace(root), "workspaces": describe_all(root)}

    @app.post("/api/workspaces", dependencies=auth, status_code=201)
    def new_workspace(body: WorkspaceRequest) -> dict:
        if body.copy_from != TEMPLATE_NAME and body.copy_from not in list_workspaces(root):
            raise HTTPException(400, f"Unknown workspace to copy from: '{body.copy_from}'")
        try:
            create_workspace(root, body.name, body.copy_from, body.description)
        except WorkspaceError as exc:
            raise HTTPException(409 if "already exists" in str(exc) else 400, str(exc)) from exc
        return describe_workspace(root, body.name)

    @app.post("/api/workspaces/{workspace}/activate", dependencies=auth)
    def activate(workspace: str) -> dict:
        try:
            set_active_workspace(root, workspace)
        except WorkspaceError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"active": workspace}

    # ---- workspace-scoped -------------------------------------------------------------
    ws = APIRouter(prefix="/api/w/{workspace}", dependencies=auth)

    @ws.get("/status")
    def ws_status(ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        s = ctx.settings
        return {
            "workspace": s.workspace,
            "description": s.description,
            "path": str(s.workspace_dir),
            "foundry_configured": s.model.configured,
            "default_planner": s.planner,
            "sources": [{"name": src.name, "type": src.type} for src in s.sources.values()],
            "limits": s.limits.__dict__,
            "sampling": {"max_rows": s.sampling.max_rows, "allowlist": s.sampling.allowlist},
        }

    @ws.get("/topology")
    def topology(ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        return load_topology(ctx.settings.topology_file, strict=False).to_dict()

    @ws.get("/metadata/{source}")
    def metadata(source: str, ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        if source not in ctx.settings.sources:
            _not_found(f"source {source}")
        return read_json(ctx.settings.metadata_dir / source / "inventory.json", default={"source": source, "tables": {}})

    @ws.get("/runs")
    def list_runs(ctx: WorkspaceContext = Depends(workspace_ctx)) -> list[dict]:
        return [_run_brief(r) for r in ctx.store.list_runs()]

    @ws.post("/runs", status_code=201)
    def start_run(body: RunRequest, ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        if body.source not in ctx.settings.sources:
            raise HTTPException(400, f"Unknown source '{body.source}' in workspace '{ctx.settings.workspace}'")
        try:
            run = ctx.runner.create_run(body.source, body.planner, body.limits)
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc
        start_thread(ctx, run["id"])
        return run

    @ws.get("/runs/{run_id}")
    def get_run(run_id: str, events: int = Query(200, ge=0, le=2000), ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        run = _get_run(ctx.store, run_id)
        return {**run, "events": ctx.store.read_events(run_id, events)}

    @ws.post("/runs/{run_id}/cancel")
    def cancel_run(run_id: str, ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        run = _get_run(ctx.store, run_id)
        if run["status"] not in ACTIVE_STATES:
            raise HTTPException(409, f"Run is already {run['status']}")
        ctx.store.request_cancel(run_id)
        return {**run, "status": "stopping"}

    @ws.post("/runs/{run_id}/resume", status_code=201)
    def resume_run(run_id: str, body: ResumeRequest, ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        previous = _get_run(ctx.store, run_id)
        try:
            run = ctx.runner.create_run(previous["source"], body.planner or previous.get("planner"), body.limits, resume_from=run_id)
        except (ValueError, KeyError) as exc:
            raise HTTPException(409, str(exc)) from exc
        start_thread(ctx, run["id"])
        return run

    @ws.get("/candidates")
    def candidates(status: str | None = None, source: str | None = None, ctx: WorkspaceContext = Depends(workspace_ctx)) -> list[dict]:
        items = ctx.store.list_candidates(status)
        if source:
            items = [c for c in items if c.get("scope", {}).get("source") == source]
        return items

    @ws.get("/candidates/{candidate_id}")
    def candidate(candidate_id: str, ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        try:
            item = ctx.store.get_candidate(candidate_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return item or _not_found(candidate_id)

    @ws.post("/candidates/{candidate_id}/decision")
    def decide(candidate_id: str, body: DecisionRequest, ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        try:
            return ctx.store.decide(candidate_id, body.decision, body.reviewer, body.notes, body.observations, body.expected_version)
        except KeyError:
            _not_found(candidate_id)
        except ConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @ws.get("/facts")
    def facts(source: str | None = None, ctx: WorkspaceContext = Depends(workspace_ctx)) -> list[dict]:
        return ctx.store.list_facts(source)

    @ws.get("/facts/revalidation")
    def revalidation(source: str, ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        s = ctx.settings
        if source not in s.sources:
            raise HTTPException(400, f"Unknown source '{source}'")
        topo = load_topology(s.topology_file, strict=False)
        adapter = None
        try:
            adapter = create_adapter(s, s.source(source))
        except Exception:  # noqa: BLE001
            adapter = None
        try:
            issues = {f["id"]: revalidation_issues(f, topo, adapter) for f in ctx.store.list_facts(source)}
        finally:
            if adapter:
                adapter.close()
        return {"source_reachable": adapter is not None, "issues": {k: v for k, v in issues.items() if v}}

    @ws.post("/codegen")
    def codegen(body: CodegenRequest, ctx: WorkspaceContext = Depends(workspace_ctx)):
        if body.source not in ctx.settings.sources:
            raise HTTPException(400, f"Unknown source '{body.source}'")
        try:
            return generate(ctx.settings, body.source, allow_stale=body.allow_stale, store=ctx.store)
        except CodegenError as exc:
            return JSONResponse(status_code=422, content={"errors": exc.errors, "warnings": exc.warnings})

    @ws.get("/codegen")
    def generated(ctx: WorkspaceContext = Depends(workspace_ctx)) -> list[dict]:
        return list_generated(ctx.settings)

    def _graph(ctx: WorkspaceContext, name: str):
        try:
            return ctx.graphs.get(name)
        except FileNotFoundError:
            _not_found(name)
        except (GraphFormatError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @ws.get("/graphs")
    def list_graphs(ctx: WorkspaceContext = Depends(workspace_ctx)) -> list[dict]:
        return ctx.graphs.list() if ctx.settings.graph_output_dir.exists() else []

    @ws.get("/graphs/{name}/summary")
    def graph_summary(name: str, ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        return _graph(ctx, name).summary()

    @ws.get("/graphs/{name}/search")
    def graph_search(name: str, q: str = "", type: str | None = None, limit: int = Query(50, ge=1, le=500),
                     ctx: WorkspaceContext = Depends(workspace_ctx)) -> list[dict]:
        return _graph(ctx, name).search(q, type, limit)

    @ws.get("/graphs/{name}/neighborhood")
    def graph_neighborhood(name: str, node_id: str, depth: int = Query(1, ge=1, le=3), limit: int = Query(150, ge=1, le=1000),
                           ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        try:
            return _graph(ctx, name).neighborhood(node_id, depth, limit)
        except KeyError:
            _not_found(node_id)

    app.include_router(ws)

    # ---- UI -------------------------------------------------------------------------
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def revalidate_ui_assets(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    def index() -> HTMLResponse:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__ASSET_VERSION__", _asset_version()))

    app.state.root = root
    app.state.registry = registry
    app.state.threads = threads
    return app


def _asset_version() -> str:
    digest = hashlib.sha256()
    for name in ("app.js", "style.css"):
        digest.update((STATIC_DIR / name).read_bytes())
    return digest.hexdigest()[:10]


def _not_found(what: str):
    raise HTTPException(404, f"Not found: {what}")


def _get_run(store: Store, run_id: str) -> dict:
    try:
        run = store.get_run(run_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return run or _not_found(run_id)


def _run_brief(run: dict) -> dict:
    return {
        k: run.get(k)
        for k in ("id", "created_at", "status", "source", "planner", "resumed_from", "termination_reason", "error", "usage", "ended_at", "elapsed_seconds", "topology_version")
    } | {"iterations": len(run.get("iterations", [])), "candidates": sum(len(i.get("new_candidates", [])) for i in run.get("iterations", []))}
