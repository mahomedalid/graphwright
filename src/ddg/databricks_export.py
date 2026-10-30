"""Export approved rules as a portable, manually executed Databricks notebook ZIP."""

from __future__ import annotations

import inspect
import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from . import __version__, notebook_runtime, transforms
from .adapters import create_adapter
from .codegen import CodegenError
from .config import Settings
from .mappings import build_spec
from .store import Store, atomic_write_json
from .topology import TopologyError, load_workspace_topology

EXPORT_FORMAT = "ddg-databricks-notebooks/v1"
_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_NODE_FIELDS = {
    "node_type", "source_table", "key_columns", "key_transforms", "properties",
    "null_key_policy", "identity_namespace", "identity_key_parts",
}
_EDGE_FIELDS = {
    "edge_type", "from_node_type", "to_node_type", "source_table", "from_key_columns",
    "to_key_columns", "from_key_transforms", "to_key_transforms", "properties",
    "null_key_policy", "dedupe", "from_mapping_id", "to_mapping_id",
}


def _validate_table(name: str) -> None:
    if not isinstance(name, str) or len(name.split(".")) != 3 or not all(
        _PART.fullmatch(part) for part in name.split(".")
    ):
        raise CodegenError([f"Databricks table '{name}' must be an unquoted catalog.schema.table identifier"])


def _bindings(settings: Settings, source: str, tables: set[str], overrides: dict[str, str]) -> dict[str, str]:
    cfg = settings.source(source)
    unknown = overrides.keys() - tables
    if unknown:
        raise CodegenError([f"Table binding overrides reference unused tables: {', '.join(sorted(unknown))}"])
    found: dict[str, list[str]] = {table: [] for table in tables}
    if cfg.type == "sqlite":
        path = (settings.workspace_dir / cfg.options["path"]).resolve()
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_sample_manifest'"
                ).fetchone()
                if exists:
                    for local, original in conn.execute("SELECT sqlite_table, source_table FROM _sample_manifest"):
                        if local in found:
                            found[local].append(original)
        except sqlite3.Error as exc:
            raise CodegenError([f"Cannot read source manifest for '{source}': {exc}"]) from exc
    elif cfg.type == "databricks":
        for table in tables:
            found[table] = [
                table if len(table.split(".")) == 3 else
                ".".join([cfg.options["catalog"], table]) if "." in table else
                ".".join([cfg.options["catalog"], cfg.options["schema"], table])
            ]
    else:
        raise CodegenError([f"Notebook export does not support discovery source type '{cfg.type}'"])
    result, errors = {}, []
    for table in sorted(tables):
        if table in overrides:
            result[table] = overrides[table]
        elif len(found[table]) == 1:
            result[table] = found[table][0]
        else:
            errors.append(
                f"Table '{table}' has {len(found[table])} manifest bindings; supply an explicit table_bindings override"
            )
    if errors:
        raise CodegenError(errors)
    for name in result.values():
        _validate_table(name)
    return result


def _notebook(*cells: str) -> str:
    return "# Databricks notebook source\n" + "\n\n# COMMAND ----------\n\n".join(cells) + "\n"


def _run(name: str) -> str:
    return f'# MAGIC %run "./{name}"'


def render_package(contract: dict) -> dict[str, str]:
    """Render source notebooks with embedded rules and no Graphwright dependency."""
    fingerprint = notebook_runtime.contract_fingerprint(contract)
    rules_literal = repr(json.dumps(contract, sort_keys=True, ensure_ascii=True))
    runtime = inspect.getsource(notebook_runtime).replace(
        "from .transforms import apply_transforms, graph_node_id\n", ""
    )
    transform_source = inspect.getsource(transforms)
    config = _notebook(
        "# Review every binding and policy before running. No persistent writes occur here.\n"
        "import json\n"
        f"CONTRACT = json.loads({rules_literal})\n"
        f"EXPECTED_RULES_FINGERPRINT = {fingerprint!r}\n"
        f"SOURCE_TABLES = {contract['source_bindings']!r}\n"
        "SOURCE_VERSIONS = {}  # Optional local alias -> reviewed Delta version.\n"
        'OUTPUT_NODES_TABLE = "<catalog>.<schema>.<nodes_table>"\n'
        'OUTPUT_EDGES_TABLE = "<catalog>.<schema>.<edges_table>"\n'
        'SCRATCH_ROOT = ""  # Dedicated protected dbfs:/ or /Volumes/ directory.\n'
        "ENABLE_PUBLICATION = False\n"
        "ACKNOWLEDGE_NON_VERSIONED_SOURCES = False\n"
        "ACKNOWLEDGE_REPLACEMENT_OWNERSHIP = False\n"
        "SINGLE_WRITER_COORDINATION = \"\"  # External exclusive lease/job lock reference.\n"
        "VALIDATION_POLICY = {\n"
        "    'max_dangling_edges': 0, 'allow_empty_types': False, 'allow_empty_graph': False,\n"
        "}\n",
        transform_source + "\n\n" + runtime,
        "preflight_report, source_frames = preflight(spark, CONTRACT, SOURCE_TABLES, SOURCE_VERSIONS,\n"
        "    EXPECTED_RULES_FINGERPRINT, ACKNOWLEDGE_NON_VERSIONED_SOURCES)\n"
        "display(spark.createDataFrame([{'runtime': json.dumps(preflight_report, sort_keys=True)}]))",
    )
    build = _notebook(
        _run("00_configure_preflight"),
        "# Materializes distributed caches, but does not publish or write scratch data.\n"
        "build = build_graph_frames(spark, CONTRACT, source_frames, SOURCE_TABLES,\n"
        "    SOURCE_VERSIONS, VALIDATION_POLICY, EXPECTED_RULES_FINGERPRINT)\n"
        "vertices_df, edges_df = build['vertices'], build['edges']\n"
        "from graphframes import GraphFrame\n"
        "g = GraphFrame(vertices_df, edges_df)\n"
        "display(spark.createDataFrame([{'validation': json.dumps(build['report'], sort_keys=True)}]))\n"
        "# Invalid rows are in build['diagnostics']; they are not displayed or sent externally.",
        "# Run after exploration, not before publishing this build.\n"
        "# release_build(build)",
    )
    publish = _notebook(
        "# Rebuilds and validates in THIS notebook's session. This can be expensive.",
        _run("10_build_validate"),
        "# Inspect build['report'] before running the separate confirmation cell.\n"
        "publication_request = publication_plan(spark, build, SOURCE_TABLES,\n"
        "    OUTPUT_NODES_TABLE, OUTPUT_EDGES_TABLE)\n"
        "print(json.dumps(publication_request, sort_keys=True, indent=2))",
        "# Set these only after reviewing this build, both targets, and all exclusions.\n"
        "# Copy publication_request['confirmation'] exactly; reruns get a new build ID.\n"
        'CONFIRM_REPLACEMENT = ""\n'
        "publication_receipt = publish_graph(spark, build, CONTRACT, SOURCE_TABLES,\n"
        "    SOURCE_VERSIONS, VALIDATION_POLICY, EXPECTED_RULES_FINGERPRINT,\n"
        "    OUTPUT_NODES_TABLE, OUTPUT_EDGES_TABLE, SCRATCH_ROOT, publication_request,\n"
        "    enabled=ENABLE_PUBLICATION, confirmation=CONFIRM_REPLACEMENT,\n"
        "    ownership=ACKNOWLEDGE_REPLACEMENT_OWNERSHIP, coordination=SINGLE_WRITER_COORDINATION)\n"
        "print(json.dumps(publication_receipt, sort_keys=True, indent=2))",
        "# Recovery is operator-run only. On failure inspect the printed recovery receipt.\n"
        "# Paste the failure receipt, inspect current target histories, reacquire the lease,\n"
        "# then explicitly confirm retry of the SAME staged build.\n"
        "RECOVERY_RECEIPT = None\n"
        "RECOVERY_EXPECTED_VERSIONS = []  # Current inspected nodes/edges versions, not guesses.\n"
        'CONFIRM_RECOVERY = ""  # RETRY <nodes> AND <edges> BUILD <receipt build_id>\n'
        "if RECOVERY_RECEIPT is not None:\n"
        "    recovery_receipt = retry_staged_publication(spark, RECOVERY_RECEIPT, SOURCE_TABLES,\n"
        "        RECOVERY_EXPECTED_VERSIONS, confirmation=CONFIRM_RECOVERY,\n"
        "        ownership=ACKNOWLEDGE_REPLACEMENT_OWNERSHIP, coordination=SINGLE_WRITER_COORDINATION)\n"
        "    print(json.dumps(recovery_receipt, sort_keys=True, indent=2))\n"
        "# Restore existing targets, after checking no other writer committed:\n"
        "# For each RESTORE, set spark.databricks.delta.commitInfo.userMetadata to\n"
        "# that target's captured prior metadata; reset the session setting afterward.\n"
        "# spark.sql('RESTORE TABLE `catalog`.`schema`.`nodes` TO VERSION AS OF <prior_version>')\n"
        "# spark.sql('RESTORE TABLE `catalog`.`schema`.`edges` TO VERSION AS OF <prior_version>')\n"
        "# If a target did not exist, do not automatically DROP it; resolve manually.\n"
        "# release_build(build)",
    )
    explore = _notebook(
        _run("00_configure_preflight"),
        "EXPLORE_PUBLISHED = False\n"
        "if EXPLORE_PUBLISHED:\n"
        "    vertices_df, edges_df, accepted_versions = read_published_pair(\n"
        "        spark, OUTPUT_NODES_TABLE, OUTPUT_EDGES_TABLE)\n"
        "else:\n"
        "    build = build_graph_frames(spark, CONTRACT, source_frames, SOURCE_TABLES,\n"
        "        SOURCE_VERSIONS, VALIDATION_POLICY, EXPECTED_RULES_FINGERPRINT)\n"
        "    vertices_df, edges_df = build['vertices'], build['edges']\n"
        "from graphframes import GraphFrame\n"
        "from pyspark.sql import functions as F\n"
        "g = GraphFrame(vertices_df, edges_df)\n"
        "display(vertices_df.groupBy('type').count())\n"
        "display(edges_df.groupBy('type').count())\n"
        "# Rows may contain personal identifiers. Enable raw display only intentionally.",
        "# Bounded graph and typed-property examples (uncomment deliberately).\n"
        "# display(g.degrees.limit(20))\n"
        "# display(g.find('(a)-[e]->(b)').limit(20))\n"
        "# display(vertices_df.select('type', F.get_json_object('properties_json', '$.name').alias('name')).limit(20))\n"
        "# Parse a reviewed type with F.from_json('properties_json', '<reviewed Spark schema>').\n"
        "# if not EXPLORE_PUBLISHED:\n"
        "#     release_build(build)",
    )
    return {
        "00_configure_preflight.py": config, "10_build_validate.py": build,
        "20_publish.py": publish, "30_explore.py": explore,
        "export_manifest.json": json.dumps({**contract, "rules_fingerprint": fingerprint}, indent=2, sort_keys=True),
        "README.md": _PACKAGE_README,
    }


def generate(
    settings: Settings, source: str, *, store: Store | None = None,
    exclude_fact_ids: list[str] | None = None, table_bindings: dict[str, str] | None = None,
) -> dict:
    """Validate selected approvals and write a generation-only notebook package."""
    store = store or Store(settings)
    facts = store.list_facts(source)
    excluded = set(exclude_fact_ids or [])
    unknown = excluded - {fact["id"] for fact in facts}
    if unknown:
        raise CodegenError([f"Cannot exclude unknown facts: {', '.join(sorted(unknown))}"])
    selected = [fact for fact in facts if fact["id"] not in excluded]
    try:
        topology = load_workspace_topology(settings)
    except TopologyError as exc:
        raise CodegenError([str(exc)]) from exc
    # Remote export must not require credentials or issue discovery queries.
    adapter = create_adapter(settings, settings.source(source)) if settings.source(source).type == "sqlite" else None
    try:
        spec = build_spec(selected, topology, source, adapter)
    finally:
        if adapter is not None:
            adapter.close()
    if adapter is None:
        spec.warnings.append("Remote schema drift is not checked during export; Databricks preflight must validate it")
    errors = list(spec.errors)
    for fact in selected:
        if fact["kind"] not in ("node_mapping", "edge_mapping"):
            continue
        allowed = _NODE_FIELDS if fact["kind"] == "node_mapping" else _EDGE_FIELDS
        unsupported = fact["mapping"].keys() - allowed
        if unsupported:
            errors.append(f"{fact['id']}: unsupported mapping fields: {', '.join(sorted(unsupported))}")
    for edge in spec.edges:
        if not edge["dedupe"]:
            errors.append(
                f"{edge['fact_id']}: dedupe=false has no stable distributed edge identity. "
                "Review a relationship key contract or exclude this fact."
            )
    if errors or not spec.ok:
        raise CodegenError(errors, spec.warnings)
    bindings = _bindings(settings, source, {m["source_table"] for m in spec.nodes + spec.edges}, table_bindings or {})
    contract = {
        "format": EXPORT_FORMAT, "exporter_version": __version__, "workspace": settings.workspace,
        "source": source, "topology_version": spec.topology_version,
        "topology_content": settings.topology_file.read_text(encoding="utf-8"),
        "facts": sorted(spec.fact_versions, key=lambda fact: fact["fact_id"]),
        "source_fingerprints": {f["id"]: f.get("schema_fingerprints", {}) for f in selected},
        "excluded_fact_ids": sorted(excluded), "source_bindings": bindings,
        "nodes": sorted(spec.nodes, key=lambda m: m["fact_id"]),
        "edges": sorted(spec.edges, key=lambda m: m["fact_id"]),
        "warnings": spec.warnings, "identity_format": "semantic/v1",
        "validation_defaults": {
            "max_dangling_edges": 0, "allow_empty_types": False, "allow_empty_graph": False,
        },
    }
    files = render_package(contract)
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    filename = f"databricks_notebooks_{source}_{uuid.uuid4().hex[:12]}.zip"
    out = settings.sandbox_dir / "generated"
    out.mkdir(parents=True, exist_ok=True)
    path = out / filename
    temporary = path.with_suffix(".tmp")
    try:
        with ZipFile(temporary, "w", ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    manifest = {
        "target": "databricks-notebooks", "builder": filename, "generated_at": generated_at,
        "workspace": settings.workspace, "source": source, "topology_version": spec.topology_version,
        "facts": spec.fact_versions, "excluded_fact_ids": sorted(excluded),
        "warnings": spec.warnings, "rules_fingerprint": notebook_runtime.contract_fingerprint(contract),
        "source_bindings": bindings, "files": sorted(files),
    }
    atomic_write_json(path.with_suffix(".manifest.json"), manifest)
    return {"path": str(path), **manifest}


_PACKAGE_README = """---
title: Databricks graph notebook package
description: Manual import, validation, publication, and recovery.
---

## Import and build

Import all four Python source notebooks into the same Databricks workspace folder.
Review export_manifest.json and the embedded CONTRACT. Rules are frozen; regenerate
after changing approvals. No source rows or credentials are bundled.

Use Spark 3.5+ with Python 3.10+ and a matching GraphFrames Python AND JVM
installation. Your administrator must select compatible Runtime/access mode/JARs.
No automatic pip installation is performed. Preflight runs a synthetic graph
operation to verify both sides. A target Databricks Runtime has not been certified
by local generation tests; validate on your compute before production use.

Edit 00: SOURCE_TABLES (aliases are the original local table names), optional
SOURCE_VERSIONS, output names and policies. Sources use fully qualified unquoted
catalog.schema.table names. Delta sources are pinned to observed or explicit
versions. Views/non-Delta sources require an explicit consistency acknowledgment.
UTC Spark session timezone is required. Key columns support strings, integral
numbers and booleans. Properties support scalar strings, booleans and numbers;
decimal/date/timestamp/binary values use the local builder's string representation.
Timestamp properties also require UTC Python worker timezones and fail otherwise.
Complex properties and floating/date/decimal keys fail preflight for review.
Python normalization UDFs run on Spark workers to preserve exact semantic/v1
escaping, Unicode, and approved legacy int-transform behavior (including its
lossy float conversion). Do not use int for large business identifiers without
review. These UDFs are not driver-side graph construction.

Run 10 to build vertices_df, edges_df and g without persistent writes. Inspect
aggregate build['report']; restricted invalid rows are in build['diagnostics'].
Property disagreements block publication; no arbitrary first row wins. Agreeing
properties and provenance merge deterministically. Null policy skip is counted;
error blocks. Dangling edges block by default. Override only with reviewed limits.
Empty selected types block by default. Nothing changes local approvals/confidence.
Run release_build(build) when finished to release cached DataFrames.

## Publish only after review

20 runs 10 again in its own session. This can scan full sources again. Review
THAT build, then copy its confirmation text into the separate confirmation cell.
Enable publication, acknowledge authoritative replacement ownership, configure a
dedicated protected SCRATCH_ROOT, and acquire an EXTERNAL exclusive lease/job lock.
Set SINGLE_WRITER_COORDINATION to its reference. This field is an attestation,
not a distributed locking implementation; if exclusive coordination is unavailable,
leave publication disabled. All other writers must honor the same coordination.
Targets must be dedicated managed Delta tables, not source aliases or shared paths.

Publication replaces the complete selected graph. Exclusions and absent records
disappear. Exactly two permanent graph tables are used. Build metadata is also
stored in Delta commit userMetadata so empty tables remain pair-verifiable.
Confirmed publication stages only under scratch/build_id, validates those exact
snapshots, checks prior versions, then writes nodes and edges sequentially.

**The two writes are NOT atomic.** Cooperating readers use read_published_pair,
which pins versions and rejects mixed builds; other SQL readers can see a mixed
pair. Keep this availability window acceptable or leave publication disabled.
Multi-table transactional publication is not implemented or advertised.

## Failure and recovery

Failures print a recovery receipt with stage paths and prior/observed versions.
Do not delete stages until recovery is complete. Reacquire the external lease,
check neither target was changed by another writer, and manually restore prior
Delta versions with RESTORE TABLE ... TO VERSION AS OF ... or use the separate
operator-confirmed retry cell for the inspected stages. For RESTORE commits,
preserve the prior pair userMetadata through the session's
spark.databricks.delta.commitInfo.userMetadata setting, then reset that setting;
otherwise guarded readers reject the restored pair. Never blindly drop newly
created targets. Delta retention limits recovery.
No automatic rollback is performed. Successful stages are retained for operator
cleanup under your retention policy; delete only the two exact build-owned paths
from the receipt, never a scratch root or workspace directory.

30 chooses an in-memory build or a version-pinned published pair. GraphFrames
uses src/dst, corresponding to source/target locally and from_id/to_id in reference
prose. Isolated vertices are retained. Raw IDs may contain personal information:
keep notebooks, scratch data and outputs within your authorized security boundary.
"""
