"""FastAPI backend: workspaces, run management, SME review, code generation, and graph visualization.

Global routes live under ``/api`` and workspace-scoped routes under ``/api/w/{workspace}``.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .adapters import create_adapter
from .adapters.base import UnknownObject
from .agent.runner import ACTIVE_STATES, DiscoveryRunner
from .codegen import CodegenError, generate, list_generated
from .confidence import ensure_confidence
from .narrative import candidate_label, facts_story, fallback_story, run_story
from .dictionary import DictionaryError, DictionaryStore, build_dictionary, to_markdown
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
from .graph import Direction, GraphFormatError, GraphRepository, GraphSort
from .mappings import build_spec, revalidation_issues
from .store import ConflictError, Store, read_json, short_hash
from .topology import load_workspace_topology
from .workspaces import create_workspace, describe_all, describe_workspace

STATIC_DIR = Path(__file__).parent / "ui" / "static"
logger = logging.getLogger(__name__)


class RunRequest(BaseModel):
    source: str
    planner: str | None = None
    mode: str | None = Field(default=None, pattern="^(conservative|exploratory)$")
    limits: dict[str, Any] = Field(default_factory=dict)


class ResumeRequest(BaseModel):
    planner: str | None = None
    mode: str | None = Field(default=None, pattern="^(conservative|exploratory)$")
    limits: dict[str, Any] = Field(default_factory=dict)


class ResetRequest(BaseModel):
    scopes: list[str] = Field(min_length=1)
    actor: str = Field(min_length=1, max_length=200)
    confirm: str = Field(description="Must equal the workspace name")


class DecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    reviewer: str = Field(min_length=1, max_length=200)
    notes: str = Field(default="", max_length=5000)
    observations: str = Field(default="", max_length=5000)
    expected_version: int


class CodegenRequest(BaseModel):
    source: str
    allow_stale: bool = False
    exclude_fact_ids: list[str] = Field(default_factory=list)


class FactRevalidationRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=200)
    notes: str = Field(min_length=1, max_length=5000)
    expected_token: str = Field(min_length=1)
    expected_topology_version: str = Field(min_length=1)
    confirm_meaning: bool = False


class WorkspaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    copy_from: str = TEMPLATE_NAME


class DictionaryEdit(BaseModel):
    author: str = Field(min_length=1, max_length=200)
    expected_revision: int
    description: str | None = Field(default=None, max_length=5000)
    notes: str | None = Field(default=None, max_length=5000)
    status: str | None = Field(default=None, pattern="^(provisional|verified|deprecated)$")
    values: dict[str, str] | None = None


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
            "default_mode": s.mode,
            "sources": [{"name": src.name, "type": src.type} for src in s.sources.values()],
            "limits": s.limits.__dict__,
            "sampling": {"max_rows": s.sampling.max_rows, "allowlist": s.sampling.allowlist},
        }

    @ws.get("/topology")
    def topology(ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        return load_workspace_topology(ctx.settings, strict=False).to_dict()

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
            run = ctx.runner.create_run(body.source, body.planner, body.limits, mode=body.mode)
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc
        start_thread(ctx, run["id"])
        return run

    @ws.get("/runs/{run_id}")
    def get_run(run_id: str, events: int = Query(200, ge=0, le=2000), ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        run = _with_stories(ctx.store, _get_run(ctx.store, run_id))
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
            run = ctx.runner.create_run(previous["source"], body.planner or previous.get("planner"), body.limits,
                                        resume_from=run_id, mode=body.mode or previous.get("mode"))
        except (ValueError, KeyError) as exc:
            raise HTTPException(409, str(exc)) from exc
        start_thread(ctx, run["id"])
        return run

    def _with_tier(ctx: WorkspaceContext, candidate: dict, topo=None) -> dict:
        if candidate.get("confidence_tier"):
            return candidate
        topo = topo or load_workspace_topology(ctx.settings, strict=False)
        edge = topo.edge_types.get((candidate.get("mapping") or {}).get("edge_type", ""))
        return ensure_confidence(candidate, edge.cardinality if edge else None)

    @ws.get("/candidates")
    def candidates(status: str | None = None, source: str | None = None, tier: str | None = None,
                   ctx: WorkspaceContext = Depends(workspace_ctx)) -> list[dict]:
        raw = ctx.store.list_candidates(status)
        topo = load_workspace_topology(ctx.settings, strict=False) if any(not c.get("confidence_tier") for c in raw) else None
        items = [_with_tier(ctx, c, topo) for c in raw]
        if source:
            items = [c for c in items if c.get("scope", {}).get("source") == source]
        if tier:
            items = [c for c in items if c.get("confidence_tier") == tier]
        return items

    @ws.get("/candidates/{candidate_id}")
    def candidate(candidate_id: str, ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        try:
            item = ctx.store.get_candidate(candidate_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _with_tier(ctx, item) if item else _not_found(candidate_id)

    @ws.get("/reset")
    def reset_preview(ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        return {"counts": ctx.store.reset_preview(), "scopes": list(Store.RESET_SCOPES)}

    @ws.post("/reset")
    def reset(body: ResetRequest, ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        if body.confirm != ctx.settings.workspace:
            raise HTTPException(400, "Type the workspace name to confirm the reset")
        live = [rid for rid, t in threads.items() if t.is_alive()]
        try:
            return ctx.store.reset(body.scopes, body.actor, active_run_ids=[r for r in live if ctx.store.get_run(r)])
        except ConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

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
        topo = load_workspace_topology(s, strict=False)
        facts = ctx.store.list_facts(source)
        adapter = None
        try:
            adapter = create_adapter(s, s.source(source))
        except Exception:
            logger.exception("Cannot reach source %s for fact revalidation", source)
        try:
            issues = {f["id"]: revalidation_issues(f, topo, adapter) for f in facts}
            spec = build_spec(facts, topo, source, adapter, allow_stale=True)
            validations = {}
            for fact in facts:
                supported = fact["kind"] in ("node_mapping", "edge_mapping")
                errors = [*spec.fact_errors.get(fact["id"], []), *(issue.message for issue in topo.errors)]
                if supported and adapter is not None:
                    for table in fact.get("schema_fingerprints", {}):
                        try:
                            adapter.schema_fingerprint(table)
                        except UnknownObject as exc:
                            errors.append(str(exc))
                validations[fact["id"]] = {
                    "token": short_hash(fact),
                    "errors": errors,
                    "needs_revalidation": supported and bool(issues[fact["id"]]),
                    "can_revalidate": adapter is not None and topo.is_valid and supported and not errors,
                }
        finally:
            if adapter:
                adapter.close()
        return {
            "source_reachable": adapter is not None,
            "topology_version": topo.version,
            "issues": {k: v for k, v in issues.items() if v},
            "validations": validations,
            "facts": facts,
        }

    @ws.post("/facts/{fact_id}/revalidate")
    def revalidate_fact(fact_id: str, body: FactRevalidationRequest, ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        if not body.confirm_meaning:
            raise HTTPException(400, "Confirm that you reviewed the mapping's meaning against the current topology")
        if not body.reviewer.strip() or not body.notes.strip():
            raise HTTPException(400, "Reviewer and review notes are required")
        try:
            fact = ctx.store.get_fact(fact_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if fact is None:
            _not_found(fact_id)
        if fact["kind"] not in ("node_mapping", "edge_mapping"):
            raise HTTPException(422, "Only node and relationship mappings require code-generation revalidation")
        topo = load_workspace_topology(ctx.settings, strict=False)
        if body.expected_token != short_hash(fact) or body.expected_topology_version != topo.version:
            raise HTTPException(409, "The fact or topology changed during review. Reload and review again.")
        if not topo.is_valid:
            raise HTTPException(422, "Fix the invalid topology before revalidating approved mappings")
        source = fact["source"]
        if source not in ctx.settings.sources:
            raise HTTPException(400, f"Unknown source '{source}'")
        facts = ctx.store.list_facts(source)
        source_token = short_hash(facts)
        adapter = None
        try:
            adapter = create_adapter(ctx.settings, ctx.settings.source(source))
            spec = build_spec(facts, topo, source, adapter, allow_stale=True)
            errors = spec.fact_errors.get(fact_id, [])
            if errors:
                return JSONResponse(status_code=422, content={"errors": errors})
            tables = set(fact.get("schema_fingerprints", {}))
            tables.add(fact["mapping"]["source_table"])
            fingerprints = {table: adapter.schema_fingerprint(table) for table in sorted(tables)}
        except UnknownObject as exc:
            return JSONResponse(status_code=422, content={"errors": [str(exc)]})
        except Exception as exc:
            logger.exception("Cannot validate source %s for fact %s", source, fact_id)
            raise HTTPException(503, "Source validation failed. Check source access and retry; no approval was changed.") from exc
        finally:
            if adapter is not None:
                adapter.close()
        if load_workspace_topology(ctx.settings, strict=False).version != topo.version:
            raise HTTPException(409, "The topology changed during validation. Reload and review again.")
        try:
            return ctx.store.revalidate_fact(
                fact_id, topology_version=topo.version, schema_fingerprints=fingerprints,
                reviewer=body.reviewer, notes=body.notes, expected_token=body.expected_token,
                expected_source_token=source_token,
            )
        except ConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError:
            _not_found(fact_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @ws.post("/codegen")
    def codegen(body: CodegenRequest, ctx: WorkspaceContext = Depends(workspace_ctx)):
        if body.source not in ctx.settings.sources:
            raise HTTPException(400, f"Unknown source '{body.source}'")
        try:
            return generate(
                ctx.settings, body.source, allow_stale=body.allow_stale, store=ctx.store,
                exclude_fact_ids=body.exclude_fact_ids,
            )
        except CodegenError as exc:
            return JSONResponse(status_code=422, content={"errors": exc.errors, "warnings": exc.warnings})

    @ws.get("/codegen")
    def generated(ctx: WorkspaceContext = Depends(workspace_ctx)) -> list[dict]:
        return list_generated(ctx.settings)

    # ---- data dictionary ------------------------------------------------------------
    def _dictionary_source(ctx: WorkspaceContext, source: str) -> None:
        if source not in ctx.settings.sources:
            raise HTTPException(404, f"Unknown source '{source}'")

    @ws.get("/dictionary/{source}")
    def dictionary(source: str, ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        _dictionary_source(ctx, source)
        return build_dictionary(ctx.settings, ctx.store, source)

    @ws.get("/dictionary/{source}/markdown")
    def dictionary_markdown(source: str, ctx: WorkspaceContext = Depends(workspace_ctx)) -> PlainTextResponse:
        _dictionary_source(ctx, source)
        text = to_markdown(build_dictionary(ctx.settings, ctx.store, source))
        filename = f"{ctx.settings.workspace}-{source}-dictionary.md"
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    def _edit_dictionary(ctx: WorkspaceContext, source: str, table: str, column: str | None, body: "DictionaryEdit") -> dict:
        _dictionary_source(ctx, source)
        known = {t["name"]: t for t in build_dictionary(ctx.settings, ctx.store, source)["tables"]}
        if table not in known:
            raise HTTPException(404, f"Unknown table '{table}' in source '{source}'. Run discovery or profiling first.")
        if column is not None and column not in {c["name"] for c in known[table]["columns"]}:
            raise HTTPException(404, f"Unknown column '{column}' in table '{table}'")
        changes = body.model_dump(exclude_unset=True, exclude={"author", "expected_revision"})
        if column is None and "values" in changes:
            raise HTTPException(400, "values (code meanings) apply to columns only")
        try:
            return DictionaryStore(ctx.settings).update(source, table, column, changes, body.author, body.expected_revision)
        except ConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except DictionaryError as exc:
            raise HTTPException(400, str(exc)) from exc

    @ws.put("/dictionary/{source}/tables/{table}")
    def edit_table(source: str, table: str, body: DictionaryEdit, ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        return _edit_dictionary(ctx, source, table, None, body)

    @ws.put("/dictionary/{source}/tables/{table}/columns/{column}")
    def edit_column(source: str, table: str, column: str, body: DictionaryEdit,
                    ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        return _edit_dictionary(ctx, source, table, column, body)

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
                     edge_type: str | None = None, direction: Direction = "both", neighbor_type: str | None = None,
                     min_degree: int = Query(0, ge=0), min_neighbors: int = Query(0, ge=0),
                     min_edge_types: int = Query(0, ge=0), sort: GraphSort = "source",
                     ctx: WorkspaceContext = Depends(workspace_ctx)) -> list[dict]:
        return _graph(ctx, name).search(
            q, type, limit, edge_type=edge_type, direction=direction, neighbor_type=neighbor_type,
            min_degree=min_degree, min_neighbors=min_neighbors, min_edge_types=min_edge_types, sort=sort,
        )

    @ws.get("/graphs/{name}/neighborhood")
    def graph_neighborhood(name: str, node_id: str, depth: int = Query(1, ge=1, le=3), limit: int = Query(150, ge=1, le=1000),
                           edge_type: str | None = None, direction: Direction = "both", neighbor_type: str | None = None,
                           ctx: WorkspaceContext = Depends(workspace_ctx)) -> dict:
        try:
            return _graph(ctx, name).neighborhood(
                node_id, depth, limit, [edge_type] if edge_type else None,
                direction=direction, neighbor_type=neighbor_type,
            )
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


def _with_stories(store: Store, run: dict) -> dict:
    """Backfill human stories for runs recorded before stories existed (display only; not persisted)."""
    iterations = []
    for it in run.get("iterations", []):
        if not it.get("activity"):
            proposed = []
            for cid in it.get("new_candidates", []):
                c = store.get_candidate(cid)
                if c:
                    c = ensure_confidence(c)
                    proposed.append({"id": cid, "label": candidate_label(c), "tier": c.get("confidence_tier")})
            it = {**it, "activity": {"proposed": proposed}}
        if not it.get("story"):
            it = {**it, "story": fallback_story(it.get("summary") or "") or facts_story(it["activity"])}
        iterations.append(it)
    run = {**run, "iterations": iterations}
    if not run.get("story") or run.get("status") in ("queued", "running", "stopping"):
        run["story"] = run_story(run)
    return run


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
        for k in ("id", "created_at", "status", "source", "planner", "mode", "resumed_from", "termination_reason", "error", "usage", "ended_at", "elapsed_seconds", "topology_version")
    } | {"iterations": len(run.get("iterations", [])), "candidates": sum(len(i.get("new_candidates", [])) for i in run.get("iterations", []))}
