"""Command-line entry point: ``ddg [-w WORKSPACE] <command>``."""

from __future__ import annotations

import argparse
import json
import runpy
import sys

from .config import TEMPLATE_NAME, WorkspaceError, find_root, list_workspaces, load_settings, set_active_workspace, workspaces_dir


def _settings(args):
    return load_settings(workspace=args.workspace)


def cmd_init_sample(args) -> int:
    root = find_root()
    builder = workspaces_dir(root) / "retail" / "data" / "build_fixture.py"
    if not builder.exists():
        print(f"Sample fixture builder not found at {builder}", file=sys.stderr)
        return 1
    db_path = runpy.run_path(str(builder))["build"](builder.parent / "retail.sqlite")
    print(f"Created {db_path}")
    return 0


def cmd_workspace(args) -> int:
    from .workspaces import create_workspace, describe_all, describe_workspace

    root = find_root()
    action = args.action
    if action == "list":
        for ws in describe_all(root):
            marker = "*" if ws["active"] else " "
            n = ws["topology"]["errors"]
            topo = ("awaiting Foundry parsing" if ws["topology"]["parsing_mode"] == "pending_foundry"
                    else "valid" if ws["topology"]["is_valid"] else f"invalid ({n} error{'' if n == 1 else 's'})")
            c = ws["counts"]
            print(f"{marker} {ws['name']:<20} sources={len(ws['sources'])} topology={topo} "
                  f"pending={c['candidates_pending']} facts={c['facts']} runs={c['runs']}  {ws['description']}")
        if not list_workspaces(root):
            print("No workspaces. Create one with: ddg workspace create <name>")
        return 0
    if action == "create":
        path = create_workspace(root, args.name, args.copy_from, args.description or "")
        print(f"Created workspace '{args.name}' at {path}")
        print(f"Next: edit {load_settings(root, args.name).topology_file} and add a source to {path / 'workspace.toml'}")
        if args.use:
            set_active_workspace(root, args.name)
            print(f"Active workspace: {args.name}")
        return 0
    if action == "use":
        set_active_workspace(root, args.name)
        print(f"Active workspace: {args.name}")
        return 0
    if action == "show":
        import json

        name = args.name or args.workspace or load_settings().workspace
        print(json.dumps(describe_workspace(root, name), indent=2))
        return 0
    return 1


def cmd_sources(args) -> int:
    from .adapters import create_adapter

    settings = _settings(args)
    names = [args.source] if args.source else list(settings.sources)
    if not names:
        print(f"Workspace '{settings.workspace}' has no sources. Add one to {settings.workspace_dir / 'workspace.toml'}")
        return 1
    failures = 0
    for name in names:
        cfg = settings.source(name)
        print(f"[{name}] type={cfg.type}")
        adapter = None
        try:
            adapter = create_adapter(settings, cfg)
            tables = adapter.tables()
            print(f"  connected; {len(tables)} table(s) in scope")
            for table in tables:
                columns = adapter.columns(table)
                print(f"  - {table} ({len(columns)} columns)")
            missing = adapter.missing_scope_entries() if hasattr(adapter, "missing_scope_entries") else []
            for entry in missing:
                print(f"  ! not found or no access: {entry}")
            if not tables or missing:
                failures += 1
        except Exception as exc:  # noqa: BLE001 - report every source
            print(f"  FAILED: {exc}")
            failures += 1
        finally:
            if adapter is not None:
                adapter.close()
    return 1 if failures else 0


def cmd_dictionary(args) -> int:
    from .dictionary import export
    from .store import Store

    settings = _settings(args)
    names = [args.source] if args.source else list(settings.sources)
    if not names:
        print(f"Workspace '{settings.workspace}' has no sources.", file=sys.stderr)
        return 1
    store = Store(settings)
    for name in names:
        settings.source(name)
        paths = export(settings, store, name)
        print(f"[{name}] {paths['markdown']}")
        print(f"[{name}] {paths['json']}")
    return 0


def cmd_topology(args) -> int:
    import json

    from .control import StopRun
    from .topology import TopologyError, load_workspace_topology

    settings = _settings(args)
    try:
        topology = load_workspace_topology(settings, strict=False, resolve=True)
    except (TopologyError, StopRun) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(topology.to_dict(), indent=2))
    else:
        print(f"Workspace: {settings.workspace}")
        print(topology.summary_for_agent())
        for issue in topology.issues:
            where = f" (line {issue.line})" if issue.line else ""
            print(f"{issue.level.upper()}{where}: {issue.message}")
        print("VALID" if topology.is_valid else "INVALID")
    return 0 if topology.is_valid else 1


def _limits(args) -> dict:
    return {
        k: v
        for k, v in {
            "max_minutes": args.max_minutes,
            "max_iterations": args.max_iterations,
            "max_cost_usd": args.max_cost_usd,
        }.items()
        if v is not None
    }


def cmd_run(args) -> int:
    from .agent.runner import DiscoveryRunner

    settings = _settings(args)
    runner = DiscoveryRunner(settings)
    source = args.source
    if source is None:
        if len(settings.sources) != 1:
            print(f"Specify --source; workspace '{settings.workspace}' has sources {sorted(settings.sources)}", file=sys.stderr)
            return 2
        source = next(iter(settings.sources))
    run = runner.create_run(source, args.planner, _limits(args), resume_from=getattr(args, "run_id", None),
                            mode=args.mode, validate_topology=args.validate_topology, discovery_scope=args.discovery_scope)
    print(f"Mode: {run['mode']}")
    print(f"Discovery scope: {run['discovery_scope']}")
    print(f"Started {run['id']} in workspace '{settings.workspace}' on source '{run['source']}'")
    result = runner.execute(run["id"])
    for item in result["iterations"]:
        print(f"  iteration {item['iteration']}: {item.get('summary', '')} new={item['new_candidates']}")
    print(f"{result['status']} ({result['termination_reason']}) usage={result['usage']}")
    for finding in result.get("topology_findings", []):
        print(f"  [{finding['status']}] {finding['element']} / {finding['aspect']}: {finding['finding']}")
        if finding["recommendation"]:
            print(f"    Recommendation: {finding['recommendation']}")
        print(f"    Evidence: {', '.join(finding['evidence_ids'])}")
        for limitation in finding["limitations"]:
            print(f"    Limitation: {limitation}")
    tested = {finding["element"] for finding in result.get("topology_findings", [])}
    for target in result.get("validation_targets", []):
        if target not in tested:
            print(f"  Not tested: {target}")
    if result.get("error"):
        print(result["error"], file=sys.stderr)
    return 0 if result["status"] != "failed" else 1


def cmd_resume(args) -> int:
    from .store import Store

    previous = Store(_settings(args)).get_run(args.run_id)
    if previous is None:
        print(f"Run {args.run_id} not found", file=sys.stderr)
        return 1
    args.source = previous["source"]
    args.planner = args.planner or previous.get("planner")
    args.mode = args.mode or previous.get("mode")
    return cmd_run(args)


def cmd_cancel(args) -> int:
    from .store import Store

    Store(_settings(args)).request_cancel(args.run_id)
    print(f"Cancellation requested for {args.run_id}")
    return 0


def cmd_runs(args) -> int:
    from .store import Store

    for run in Store(_settings(args)).list_runs():
        print(f"{run['id']}  {run['status']:<10} {run['source']:<12} {run.get('termination_reason') or '':<18} iterations={len(run['iterations'])}")
    return 0


def cmd_candidates(args) -> int:
    from .confidence import ensure_confidence
    from .store import Store

    for c in Store(_settings(args)).list_candidates(args.status):
        c = ensure_confidence(c)
        if args.tier and c["confidence_tier"] != args.tier:
            continue
        print(f"{c['id']}  v{c['version']} {c['status']:<9} {c['confidence_tier']:<6} {c['kind']:<13} {c['hypothesis']}")
    return 0


def cmd_reset(args) -> int:
    from .store import Store

    settings = _settings(args)
    store = Store(settings)
    scopes = ["runs", "pending", "metadata"] if args.fresh else [s for s in Store.RESET_SCOPES if getattr(args, s)]
    if not scopes:
        print("Choose what to reset: --fresh, or any of --runs --pending --rejected --approved --metadata", file=sys.stderr)
        return 2
    counts = store.reset_preview()
    print(f"Workspace '{settings.workspace}': will archive " + ", ".join(f"{s} ({counts[s]})" for s in scopes))
    if "approved" in scopes:
        print("WARNING: approved facts will be archived; generated builders will need re-approval.")
    if "rejected" in scopes:
        print("Note: archiving rejected candidates lets the agent propose those interpretations again.")
    if not args.yes:
        answer = input(f"Type the workspace name ({settings.workspace}) to confirm: ").strip()
        if answer != settings.workspace:
            print("Cancelled.")
            return 1
    manifest = store.reset(scopes, args.actor or "cli")
    print(f"Archived {manifest['moved'] or 'nothing'} to {manifest['path']}")
    return 0


def cmd_review(args) -> int:
    from .store import Store

    store = Store(_settings(args))
    candidate = store.get_candidate(args.candidate_id)
    if candidate is None:
        print("Candidate not found", file=sys.stderr)
        return 1
    result = store.decide(args.candidate_id, args.decision, args.reviewer, args.notes, args.observations, candidate["version"])
    print(f"{result['id']} -> {result['status']}")
    return 0


def cmd_generate(args) -> int:
    from .codegen import CodegenError, generate

    try:
        if args.target == "databricks-notebooks":
            from .databricks_export import generate as generate_notebooks

            if args.allow_stale:
                raise ValueError("--allow-stale is only supported for the standalone target; review old approvals first")
            bindings = None
            if args.table_bindings is not None:
                try:
                    bindings = json.loads(args.table_bindings)
                except json.JSONDecodeError as exc:
                    raise ValueError("--table-bindings must be a JSON object mapping sample tables to Databricks tables") from exc
                if not isinstance(bindings, dict) or not all(
                    isinstance(key, str) and isinstance(value, str) for key, value in bindings.items()
                ):
                    raise ValueError("--table-bindings must be a JSON object with string table names and values")
            result = generate_notebooks(
                _settings(args), args.source, exclude_fact_ids=args.exclude_fact, table_bindings=bindings,
            )
        else:
            if args.table_bindings is not None:
                raise ValueError("--table-bindings requires --target databricks-notebooks")
            result = generate(
                _settings(args), args.source, allow_stale=args.allow_stale,
                exclude_fact_ids=args.exclude_fact,
            )
    except CodegenError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Generated {result['path']}")
    for warning in result["warnings"]:
        print(f"WARNING: {warning}")
    if args.target == "databricks-notebooks":
        print("Resolved source bindings:")
        print(json.dumps(result["source_bindings"], indent=2))
        print("Extract the ZIP, then import the four Python source notebooks into the same Databricks folder. Review bindings and configuration, then run manually. Nothing was executed.")
    else:
        print(f"Review it, then run from the repository root: {result['suggested_command']}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(find_root()), host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    # -w works before or after the subcommand; SUPPRESS keeps a subparser from overwriting the global value.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-w", "--workspace", default=argparse.SUPPRESS, help="Workspace name (default: active workspace)")

    parser = argparse.ArgumentParser(prog="ddg", description="Data Discovery Graph")
    parser.add_argument("-w", "--workspace", default=None, help="Workspace name (default: active workspace)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func, help_text: str, *, aliases: tuple[str, ...] = ()):
        p = sub.add_parser(name, help=help_text, parents=[common], aliases=aliases)
        p.set_defaults(func=func)
        return p

    add("init-sample", cmd_init_sample, "Build the retail workspace's sample SQLite database")

    p = add("workspace", cmd_workspace, "List, create, switch, or show workspaces")
    ws_sub = p.add_subparsers(dest="action", required=True)
    ws_sub.add_parser("list", help="List workspaces (* marks the active one)")
    c = ws_sub.add_parser("create", help=f"Create a workspace from {TEMPLATE_NAME} or another workspace")
    c.add_argument("name")
    c.add_argument("--from", dest="copy_from", default=TEMPLATE_NAME, help="Copy configuration from this workspace")
    c.add_argument("--description")
    c.add_argument("--use", action="store_true", help="Make it the active workspace")
    u = ws_sub.add_parser("use", help="Set the active workspace")
    u.add_argument("name")
    s = ws_sub.add_parser("show", help="Show workspace details")
    s.add_argument("name", nargs="?")

    p = add("sources", cmd_sources, "Check source connections and list tables in scope (no row values)")
    p.add_argument("action", choices=["check"])
    p.add_argument("--source", help="Check only this source")

    p = add("dictionary", cmd_dictionary, "Export the data dictionary (Markdown + JSON) for each source")
    p.add_argument("--source", help="Export only this source")

    p = add("topology", cmd_topology, "Validate and print the workspace topology")
    p.add_argument("--json", action="store_true")

    for name, func, help_text in (("run", cmd_run, "Start a discovery run"), ("resume", cmd_resume, "Resume from a run checkpoint")):
        p = add(name, func, help_text)
        if name == "run":
            p.add_argument("--source", help="Source name (optional when the workspace has one source)")
        else:
            p.add_argument("run_id")
        p.add_argument("--planner", choices=["auto", "foundry", "heuristic", "profile"],
                       help="profile = only collect column statistics for the data dictionary")
        p.add_argument("--mode", choices=["conservative", "exploratory"],
                       help="exploratory also accepts low-confidence proposals (default from workspace [agent] mode)")
        p.add_argument("--max-minutes", type=float)
        p.add_argument("--max-iterations", type=int)
        p.add_argument("--max-cost-usd", type=float)
        p.add_argument("--validate-topology", action=argparse.BooleanOptionalAction, default=None,
                       help="Test topology assumptions and recommend edits without changing the topology")
        p.add_argument("--discovery-scope", choices=["all", "edges"],
                       help="edges = only discover relationships using existing approved or pending node mappings")

    add("cancel", cmd_cancel, "Request cancellation of an active run").add_argument("run_id")
    add("runs", cmd_runs, "List runs")

    p = add("candidates", cmd_candidates, "List candidates with their confidence tier")
    p.add_argument("--status", choices=["pending", "approved", "rejected"])
    p.add_argument("--tier", choices=["high", "medium", "low"])

    p = add("reset", cmd_reset, "Archive discovery state so the next run starts fresh (nothing is deleted)")
    p.add_argument("--fresh", action="store_true", help="Same as --runs --pending --metadata (keeps approved and rejected)")
    for scope, text in (("runs", "run history, checkpoints, evidence (also clears dictionary column profiles)"),
                        ("pending", "pending candidates"),
                        ("rejected", "rejected candidates (lifts their resubmission block)"),
                        ("approved", "approved candidates and their facts"),
                        ("metadata", "source inventories and cached topology parse")):
        p.add_argument(f"--{scope}", action="store_true", help=text)
    p.add_argument("--actor", help="Name recorded in the archive manifest")
    p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")

    p = add("review", cmd_review, "Approve or reject a candidate")
    p.add_argument("candidate_id")
    p.add_argument("decision", choices=["approve", "reject"])
    p.add_argument("--reviewer", required=True)
    p.add_argument("--notes", default="")
    p.add_argument("--observations", default="")

    p = add("generate", cmd_generate, "Generate graph-building code from approved facts", aliases=("codegen",))
    p.add_argument("--source", required=True)
    p.add_argument("--target", choices=["standalone", "databricks-notebooks"], default="standalone",
                   help="Standalone Python builder (default) or a manually executed Databricks notebook ZIP")
    p.add_argument("--table-bindings", metavar="JSON",
                   help='Notebook-only table overrides, e.g. \'{"sample_table":"catalog.schema.table"}\'')
    p.add_argument("--allow-stale", action="store_true", help="Include facts flagged for revalidation")
    p.add_argument("--exclude-fact", action="append", default=[], metavar="FACT_ID",
                   help="Leave an approved fact out of this build only (repeat for multiple facts)")

    p = add("serve", cmd_serve, "Start the web UI and API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (WorkspaceError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
