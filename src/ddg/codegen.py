"""Generate inspectable graph-building code from approved mappings (FR-009).

The product only writes the code. A human reviews and runs it separately; the
resulting JSON (``ddg-graph/v1``) is what the UI visualizer loads.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, transforms
from .adapters import create_adapter
from .config import Settings
from .mappings import BuildSpec, build_spec
from .store import Store, atomic_write_json, atomic_write_text
from .topology import TopologyError, load_workspace_topology

GRAPH_FORMAT = "ddg-graph/v1"


class CodegenError(Exception):
    def __init__(self, errors: list[str], warnings: list[str] | None = None):
        self.errors = errors
        self.warnings = warnings or []
        super().__init__("Cannot generate graph builder:\n- " + "\n- ".join(errors))


TEMPLATE = r'''#!/usr/bin/env python3
"""Generated graph builder. Review before running; regenerate instead of editing.

__HEADER__

The Data Discovery Graph product never executes this file. Run it yourself:

    python __FILENAME__ --sqlite path/to/source.sqlite --out graph_output/graph.json
    python __FILENAME__ --databricks --out graph_output/graph.json

Databricks mode needs `pip install databricks-sql-connector` and DATABRICKS_HOST
(or DATABRICKS_SERVER_HOSTNAME). DATABRICKS_HTTP_PATH defaults to the discovery
warehouse. With DATABRICKS_TOKEN set it uses that token; otherwise it opens a
browser OAuth sign-in. Use read-only credentials. Output format: __FORMAT__ (JSON), optional GraphML.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from xml.sax.saxutils import escape

SPEC = json.loads(r"""__SPEC_JSON__""")

# ---- key transforms (copied from ddg.transforms so discovery and build agree) ----
__TRANSFORMS__
# ---- end transforms ----


def _quote(dialect, identifier):
    q = "`" if dialect == "databricks" else '"'
    return q + identifier.replace(q, q * 2) + q


def _table_ref(dialect, table):
    if dialect == "databricks":
        schema, name = table.split(".", 1) if "." in table else (SPEC["schema"], table)
        return ".".join(_quote(dialect, part) for part in (SPEC["catalog"], schema, name))
    return _quote(dialect, table)


def read_rows(conn, dialect, table, columns):
    cols = ", ".join(_quote(dialect, c) for c in columns)
    sql = "SELECT %s FROM %s ORDER BY %s" % (cols, _table_ref(dialect, table), cols)
    cursor = conn.cursor()
    cursor.execute(sql)
    while True:
        batch = cursor.fetchmany(5000)
        if not batch:
            break
        for row in batch:
            yield dict(zip(columns, row))


def _json_value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _unique(items):
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _add_provenance(element, mapping):
    for prov in element["provenance"]:
        if prov["fact_id"] == mapping["fact_id"]:
            prov["rows"] += 1
            return
    element["provenance"].append(
        {"fact_id": mapping["fact_id"], "source": SPEC["source"], "table": mapping["source_table"], "rows": 1}
    )


def build_graph(conn, dialect="sqlite"):
    nodes, edges = {}, {}
    stats = {"skipped_null_keys": 0, "dangling_edges": 0, "rows_read": 0}
    warnings = list(SPEC.get("warnings", []))

    for m in SPEC["nodes"]:
        columns = _unique(m["key_columns"] + list(m["properties"].values()))
        for row in read_rows(conn, dialect, m["source_table"], columns):
            stats["rows_read"] += 1
            key = canonical_key([row[c] for c in m["key_columns"]], m["key_transforms"])
            if key is None:
                if m["null_key_policy"] == "error":
                    raise ValueError("Null key in %s for %s" % (m["source_table"], m["node_type"]))
                stats["skipped_null_keys"] += 1
                continue
            node_id = "%s:%s" % (m["node_type"], key)
            node = nodes.setdefault(node_id, {"id": node_id, "type": m["node_type"], "key": key, "properties": {}, "provenance": []})
            for prop, col in m["properties"].items():
                if node["properties"].get(prop) is None and row[col] is not None:
                    node["properties"][prop] = _json_value(row[col])
            _add_provenance(node, m)

    for m in SPEC["edges"]:
        columns = _unique(m["from_key_columns"] + m["to_key_columns"] + list(m["properties"].values()))
        for index, row in enumerate(read_rows(conn, dialect, m["source_table"], columns)):
            stats["rows_read"] += 1
            from_key = canonical_key([row[c] for c in m["from_key_columns"]], m["from_key_transforms"])
            to_key = canonical_key([row[c] for c in m["to_key_columns"]], m["to_key_transforms"])
            if from_key is None or to_key is None:
                if m["null_key_policy"] == "error":
                    raise ValueError("Null edge key in %s for %s" % (m["source_table"], m["edge_type"]))
                stats["skipped_null_keys"] += 1
                continue
            source_id = "%s:%s" % (m["from_node_type"], from_key)
            target_id = "%s:%s" % (m["to_node_type"], to_key)
            if source_id not in nodes or target_id not in nodes:
                stats["dangling_edges"] += 1
                continue
            edge_id = "%s:%s->%s" % (m["edge_type"], source_id, target_id)
            if not m["dedupe"]:
                edge_id += "#%d" % index
            edge = edges.setdefault(
                edge_id,
                {"id": edge_id, "type": m["edge_type"], "source": source_id, "target": target_id, "properties": {}, "provenance": []},
            )
            for prop, col in m["properties"].items():
                if edge["properties"].get(prop) is None and row[col] is not None:
                    edge["properties"][prop] = _json_value(row[col])
            _add_provenance(edge, m)

    if stats["dangling_edges"]:
        warnings.append("%d edges skipped because an endpoint node was not found" % stats["dangling_edges"])
    counts = {}
    for item in list(nodes.values()) + list(edges.values()):
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    stats.update({"nodes": len(nodes), "edges": len(edges), "by_type": dict(sorted(counts.items()))})
    return {
        "format": SPEC["format"],
        "inputs": SPEC["inputs"],
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": stats,
        "warnings": warnings,
        "nodes": [nodes[k] for k in sorted(nodes)],
        "edges": [edges[k] for k in sorted(edges)],
    }


def write_graphml(graph, path):
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
        '<key id="type" for="all" attr.name="type" attr.type="string"/>',
        '<key id="properties" for="all" attr.name="properties" attr.type="string"/>',
        '<graph id="G" edgedefault="directed">',
    ]
    for n in graph["nodes"]:
        lines.append('<node id="%s"><data key="type">%s</data><data key="properties">%s</data></node>'
                     % (escape(n["id"], {'"': "&quot;"}), escape(n["type"]), escape(json.dumps(n["properties"]))))
    for e in graph["edges"]:
        lines.append('<edge id="%s" source="%s" target="%s"><data key="type">%s</data><data key="properties">%s</data></edge>'
                     % (escape(e["id"], {'"': "&quot;"}), escape(e["source"], {'"': "&quot;"}), escape(e["target"], {'"': "&quot;"}),
                        escape(e["type"]), escape(json.dumps(e["properties"]))))
    lines += ["</graph>", "</graphml>"]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def connect(args):
    if args.databricks:
        from databricks import sql  # type: ignore

        host = os.environ.get("DATABRICKS_SERVER_HOSTNAME") or os.environ.get("DATABRICKS_HOST", "")
        host = host.replace("https://", "").rstrip("/")
        if not host:
            sys.exit("Set DATABRICKS_HOST (for example https://adb-123.azuredatabricks.net)")
        options = {
            "server_hostname": host,
            "http_path": os.environ.get("DATABRICKS_HTTP_PATH") or "/sql/1.0/warehouses/%s" % SPEC["warehouse_id"],
        }
        if os.environ.get("DATABRICKS_TOKEN"):
            options["access_token"] = os.environ["DATABRICKS_TOKEN"]
        else:
            options["auth_type"] = "databricks-oauth"
        return sql.connect(**options), "databricks"
    if not args.sqlite:
        sys.exit("Provide --sqlite PATH or --databricks")
    path = os.path.abspath(args.sqlite)
    uri = "file:" + path.replace("\\", "/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True), "sqlite"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the business graph from source tables.")
    parser.add_argument("--sqlite", help="Path to the SQLite source database")
    parser.add_argument("--databricks", action="store_true", help="Read from Databricks SQL")
    parser.add_argument("--out", required=True, help="Output JSON path (load it in the UI visualizer)")
    parser.add_argument("--graphml", help="Optional GraphML output path")
    args = parser.parse_args(argv)
    conn, dialect = connect(args)
    try:
        graph = build_graph(conn, dialect)
    finally:
        conn.close()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(graph, handle, indent=2)
    if args.graphml:
        write_graphml(graph, args.graphml)
    print("Wrote %d nodes and %d edges to %s" % (graph["stats"]["nodes"], graph["stats"]["edges"], args.out))


if __name__ == "__main__":
    main()
'''


def _transforms_source() -> str:
    source = inspect.getsource(transforms)
    return source.split('"""', 2)[2].strip() if source.startswith('"""') else source


def render(spec: BuildSpec, source_cfg_options: dict, filename: str, generated_at: str, workspace: str = "") -> str:
    payload = {
        "format": GRAPH_FORMAT,
        "source": spec.source,
        "catalog": source_cfg_options.get("catalog"),
        "schema": source_cfg_options.get("schema"),
        "warehouse_id": source_cfg_options.get("warehouse_id"),
        "inputs": {
            "workspace": workspace,
            "source": spec.source,
            "topology_version": spec.topology_version,
            "facts": spec.fact_versions,
            "generator": f"ddg {__version__}",
            "generated_at": generated_at,
            "builder": filename,
        },
        "warnings": spec.warnings,
        "nodes": spec.nodes,
        "edges": spec.edges,
    }
    spec_json = json.dumps(payload, indent=2, sort_keys=True)
    if '"""' in spec_json:
        raise CodegenError(["Mapping specification contains a triple quote and cannot be embedded safely"])
    header = "\n".join(
        [
            f"Generated at: {generated_at} by ddg {__version__}",
            f"Workspace: {workspace}",
            f"Source: {spec.source}",
            f"Topology version: {spec.topology_version}",
            "Approved facts: " + ", ".join(f"{f['fact_id']} (candidate {f['candidate_id']} v{f['candidate_version']})" for f in spec.fact_versions),
        ]
    )
    return (
        TEMPLATE.replace("__HEADER__", header)
        .replace("__FILENAME__", filename)
        .replace("__FORMAT__", GRAPH_FORMAT)
        .replace("__TRANSFORMS__", _transforms_source())
        .replace("__SPEC_JSON__", spec_json)
    )


def generate(
    settings: Settings, source: str, allow_stale: bool = False, store: Store | None = None, *,
    exclude_fact_ids: list[str] | None = None,
) -> dict:
    store = store or Store(settings)
    facts = store.list_facts(source)
    excluded = set(exclude_fact_ids or [])
    unknown = excluded - {fact["id"] for fact in facts}
    if unknown:
        raise CodegenError([f"Cannot exclude unknown facts for source '{source}': {', '.join(sorted(unknown))}"])
    facts = [fact for fact in facts if fact["id"] not in excluded]
    try:
        topology = load_workspace_topology(settings)
    except TopologyError as exc:
        raise CodegenError([str(exc)]) from exc
    source_cfg = settings.source(source)
    adapter = None
    try:
        adapter = create_adapter(settings, source_cfg)
    except Exception:  # noqa: BLE001 - source unreachable: generate without live schema checks
        adapter = None
    try:
        spec = build_spec(facts, topology, source, adapter, allow_stale=allow_stale)
    finally:
        if adapter is not None:
            adapter.close()
    if adapter is None:
        spec.warnings.append("Source was unreachable; column existence and schema drift were not verified")
    if excluded:
        spec.warnings.append(
            "Explicitly left out of this build (approved facts unchanged): " + ", ".join(sorted(excluded))
        )
    if not spec.ok:
        raise CodegenError(spec.errors, spec.warnings)
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    stamp = generated_at.replace(":", "").replace("-", "").replace("+0000", "")[:15]
    filename = f"build_graph_{source}_{stamp}.py"
    out_dir = settings.sandbox_dir / "generated"
    path = out_dir / filename
    code = render(spec, source_cfg.options, filename, generated_at, settings.workspace)
    atomic_write_text(path, code)
    manifest = {
        "builder": filename,
        "generated_at": generated_at,
        "workspace": settings.workspace,
        "source": source,
        "topology_version": spec.topology_version,
        "facts": spec.fact_versions,
        "excluded_fact_ids": sorted(excluded),
        "warnings": spec.warnings,
        "suggested_command": _suggested_command(settings, source_cfg, path),
    }
    atomic_write_json(path.with_suffix(".manifest.json"), manifest)
    return {"path": str(path), "code": code, **manifest}


def _suggested_command(settings: Settings, source_cfg, path: Path) -> str:
    """Command a human runs from the repository root; paths use forward slashes for portability."""

    def rel(p: Path) -> str:
        return p.resolve().relative_to(settings.root).as_posix()

    out = settings.graph_output_dir / f"{source_cfg.name}.graph.json"
    if source_cfg.type == "sqlite":
        db = settings.workspace_dir / source_cfg.options["path"]
        return f"python {rel(path)} --sqlite {rel(db)} --out {rel(out)}"
    return f"python {rel(path)} --databricks --out {rel(out)}"


def list_generated(settings: Settings) -> list[dict]:
    out = []
    for manifest in sorted((settings.sandbox_dir / "generated").glob("*.manifest.json"), reverse=True):
        out.append(json.loads(manifest.read_text(encoding="utf-8")))
    return out
