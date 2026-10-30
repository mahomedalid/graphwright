"""Self-contained Spark runtime embedded into exported notebooks.

Spark imports are lazy so exporting packages requires neither Spark nor Java.
Key-normalization functions are embedded immediately before this module.
"""

import hashlib
import json
import math
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from .transforms import apply_transforms, graph_node_id

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession


def contract_fingerprint(contract: dict) -> str:
    """Hash the complete frozen export contract."""
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _table_name(value: str) -> str:
    parts = value.split(".")
    if len(parts) != 3 or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", part) for part in parts):
        raise ValueError(f"Expected fully qualified unquoted catalog.schema.table, got {value!r}")
    return ".".join("`" + part + "`" for part in parts)


def _column(value: str):
    from pyspark.sql import functions as F

    return F.col("`" + value.replace("`", "``") + "`")


def _worker_utc() -> bool:
    return datetime.fromtimestamp(0) == datetime(1970, 1, 1)


def _json_scalar(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite graph properties are unsupported; review the mapping")
    if isinstance(value, datetime) and not _worker_utc():
        raise ValueError("Timestamp property parity requires UTC on Python workers, not only the Spark session")
    if isinstance(value, (Decimal, date, datetime, bytes, bytearray)):
        value = str(bytes(value)) if isinstance(value, bytearray) else str(value)
    if not isinstance(value, (str, bool, int, float)):
        raise ValueError(f"Unsupported property value type: {type(value).__name__}")
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _properties_json(entries) -> str:
    return "{" + ",".join(
        json.dumps(entry["key"], ensure_ascii=True) + ":" + entry["value"]
        for entry in sorted(entries or [], key=lambda entry: entry["key"])
    ) + "}"


def _versions(spark, name: str) -> dict:
    quoted = _table_name(name)
    if not spark.catalog.tableExists(quoted):
        return {"version": None, "metadata": None}
    detail = spark.sql(f"DESCRIBE DETAIL {quoted}").first().asDict()
    if detail["format"].lower() != "delta":
        raise ValueError(f"Output {name} is not Delta")
    table_info = spark.catalog.getTable(quoted)
    if table_info.tableType != "MANAGED":
        raise ValueError(f"Output {name} must be a dedicated managed Delta table, not a path alias")
    row = spark.sql(f"DESCRIBE HISTORY {quoted} LIMIT 1").first().asDict()
    return {"version": row["version"], "metadata": row.get("userMetadata")}


def _runtime_signature(contract, bindings, versions, policy) -> str:
    return contract_fingerprint({
        "contract": contract, "bindings": bindings, "versions": versions, "policy": policy,
    })


def _required_columns(mapping: dict, node: bool) -> list[str]:
    keys = mapping["key_columns"] if node else mapping["from_key_columns"] + mapping["to_key_columns"]
    return sorted(set(keys + list(mapping["properties"].values())))


def preflight(
    spark: "SparkSession", contract: dict, bindings: dict[str, str], versions: dict[str, int],
    expected_fingerprint: str, acknowledge_non_versioned: bool,
) -> tuple[dict, dict]:
    """Read only: verify rules, GraphFrames JVM support, schemas and pinned inputs."""
    from graphframes import GraphFrame
    from pyspark.sql import types as T

    if contract_fingerprint(contract) != expected_fingerprint:
        raise ValueError("Embedded rules fingerprint changed; regenerate the export")
    if tuple(int(part) for part in spark.version.split(".")[:2]) < (3, 5):
        raise ValueError("Notebook export requires Spark 3.5 or later")
    if spark.conf.get("spark.sql.session.timeZone") != "UTC":
        raise ValueError("Set spark.sql.session.timeZone to UTC before running preflight")
    # An action involving JVM GraphFrames catches mismatched Python/JAR installations.
    tiny = GraphFrame(spark.createDataFrame([("a",), ("b",)], ["id"]),
                      spark.createDataFrame([("a", "b")], ["src", "dst"]))
    if tiny.find("(a)-[e]->(b)").count() != 1:
        raise ValueError("GraphFrames Python/JVM smoke test failed; ask your compute administrator")
    required = {}
    for node, mappings in ((True, contract["nodes"]), (False, contract["edges"])):
        for mapping in mappings:
            required.setdefault(mapping["source_table"], set()).update(_required_columns(mapping, node))
    if set(bindings) != set(required) or set(versions) - set(required):
        raise ValueError("Bindings/version overrides must match the selected source aliases")
    frames, observations = {}, {}
    for alias, columns in sorted(required.items()):
        quoted = _table_name(bindings[alias])
        info = spark.catalog.getTable(quoted)
        version = versions.get(alias)
        if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 0):
            raise ValueError(f"{alias}: Delta version must be a nonnegative integer")
        # Do not catch permission or missing-object errors and mislabel them as non-Delta.
        is_view = info.tableType in ("VIEW", "TEMPORARY")
        detail = {} if is_view else spark.sql(f"DESCRIBE DETAIL {quoted}").first().asDict()
        if not is_view and detail.get("format", "").lower() == "delta":
            if version is None:
                version = spark.sql(f"DESCRIBE HISTORY {quoted} LIMIT 1").first()["version"]
            frame = spark.read.option("versionAsOf", version).table(quoted)
            observations[alias] = {"table": bindings[alias], "version": version}
        else:
            if version is not None:
                raise ValueError(f"{alias}: version override requires a Delta table")
            if not acknowledge_non_versioned:
                raise ValueError(f"{alias}: acknowledge non-versioned/view source consistency limits explicitly")
            frame = spark.table(quoted)
            observations[alias] = {"table": bindings[alias], "version": None, "consistent_snapshot": False}
        missing = set(columns) - set(frame.columns)
        if missing:
            raise ValueError(f"{alias}: missing columns {sorted(missing)}; no substitutions are inferred")
        frames[alias] = frame.select(*[_column(c) for c in sorted(columns)])
    key_types = (T.StringType, T.ByteType, T.ShortType, T.IntegerType, T.LongType, T.BooleanType)
    prop_types = key_types + (T.FloatType, T.DoubleType, T.DecimalType, T.DateType, T.TimestampType, T.BinaryType)
    for node, mappings in ((True, contract["nodes"]), (False, contract["edges"])):
        for mapping in mappings:
            fields = {field.name: field.dataType for field in frames[mapping["source_table"]].schema.fields}
            keys = mapping["key_columns"] if node else mapping["from_key_columns"] + mapping["to_key_columns"]
            for col in keys:
                if not isinstance(fields[col], key_types):
                    raise ValueError(f"{mapping['fact_id']}: key {col} type {fields[col]} has no proven identity parity")
            for col in mapping["properties"].values():
                if not isinstance(fields[col], prop_types):
                    raise ValueError(f"{mapping['fact_id']}: property {col} type {fields[col]} is unsupported")
    return {
        "spark": spark.version, "python": __import__("sys").version.split()[0],
        "access_mode": spark.conf.get("spark.databricks.clusterUsageTags.dataSecurityMode", "unreported"),
        "sources": observations, "rules_fingerprint": expected_fingerprint,
    }, {"frames": frames, "observations": observations}


def _projection(spark, frame, mapping, node, source, facts, binding):
    from pyspark.sql import functions as F, types as T

    def normalized(values, chains):
        return [apply_transforms(value, chain) for value, chain in zip(values, chains)]

    key_schema = T.StructType([
        T.StructField("id", T.StringType()), T.StructField("key_values", T.ArrayType(T.StringType())),
    ])

    def node_key(*values):
        parts = normalized(values, mapping["key_transforms"])
        return graph_node_id(mapping["node_type"], mapping["identity_namespace"],
                             list(values), mapping["key_transforms"]), parts

    def edge_key(*values):
        split = len(mapping["from_key_columns"])
        return (
            graph_node_id(mapping["from_node_type"], mapping["from_identity_namespace"],
                          list(values[:split]), mapping["from_key_transforms"]),
            graph_node_id(mapping["to_node_type"], mapping["to_identity_namespace"],
                          list(values[split:]), mapping["to_key_transforms"]),
        )

    def props(*values):
        return {key: _json_scalar(value) for key, value in zip(sorted(mapping["properties"]), values)
                if value is not None}

    property_udf = F.udf(props, T.MapType(T.StringType(), T.StringType()))
    prop_cols = [_column(mapping["properties"][key]) for key in sorted(mapping["properties"])]
    properties = property_udf(*prop_cols) if prop_cols else F.from_json(F.lit("{}"), "map<string,string>")
    provenance = json.dumps({
        "fact": facts[mapping["fact_id"]], "source": source,
        "source_table": mapping["source_table"], "execution_table": binding,
    }, sort_keys=True, separators=(",", ":"))
    if node:
        key = F.udf(node_key, key_schema)(*[_column(c) for c in mapping["key_columns"]])
        result = frame.select(
            key.alias("_key"), properties.alias("_properties"),
        ).select(
            F.col("_key.id").alias("id"), F.lit(mapping["node_type"]).alias("type"),
            F.lit(mapping["identity_namespace"]).alias("identity_namespace"),
            F.array(*[F.lit(p) for p in mapping["identity_key_parts"]]).alias("identity_key_parts"),
            F.col("_key.key_values").alias("key_values"), "_properties",
        )
    else:
        endpoint_schema = T.StructType([T.StructField("src", T.StringType()), T.StructField("dst", T.StringType())])
        key = F.udf(edge_key, endpoint_schema)(
            *[_column(c) for c in mapping["from_key_columns"] + mapping["to_key_columns"]]
        )
        result = frame.select(key.alias("_key"), properties.alias("_properties")).select(
            F.concat(F.lit(mapping["edge_type"] + ":"), F.col("_key.src"), F.lit("->"),
                     F.col("_key.dst")).alias("id"),
            F.col("_key.src").alias("src"), F.col("_key.dst").alias("dst"),
            F.lit(mapping["edge_type"]).alias("type"),
            F.lit(mapping["from_node_type"]).alias("from_type"),
            F.lit(mapping["to_node_type"]).alias("to_type"), "_properties",
        )
    return result.withColumn("_provenance", F.lit(provenance)).withColumn("_fact_id", F.lit(mapping["fact_id"]))


def _merge(rows, node):
    from pyspark.sql import functions as F, types as T

    group = (["id", "type", "identity_namespace", "identity_key_parts", "key_values"] if node else
             ["id", "src", "dst", "type", "from_type", "to_type"])
    def provenance_json(entries):
        return json.dumps([
            {**json.loads(entry["_provenance"]), "rows": entry["count"]}
            for entry in sorted(entries, key=lambda entry: entry["_provenance"])
        ], sort_keys=True, ensure_ascii=True, separators=(",", ":"))

    provenance = rows.groupBy(*group, "_provenance").count().groupBy(*group).agg(
        F.collect_list(F.struct("_provenance", "count")).alias("_provenance_entries")
    ).withColumn(
        "provenance_json", F.udf(provenance_json, T.StringType())("_provenance_entries")
    ).drop("_provenance_entries")
    entries = rows.select("id", F.explode("_properties").alias("key", "value"))
    values = entries.groupBy("id", "key").agg(F.sort_array(F.collect_set("value")).alias("values"))
    conflicts = values.filter(F.size("values") > 1)
    merged = values.filter(F.size("values") == 1).select(
        "id", "key", F.element_at("values", 1).alias("value")
    ).groupBy("id").agg(
        F.collect_list(F.struct("key", "value")).alias("_entries")
    )
    serialize = F.udf(_properties_json, T.StringType())
    return provenance.join(merged, "id", "left").withColumn(
        "properties_json", serialize("_entries")
    ).drop("_entries"), conflicts


def _policy(policy):
    if set(policy) != {"max_dangling_edges", "allow_empty_types", "allow_empty_graph"}:
        raise ValueError("Validation policy fields must match the exported policy")
    limit = policy["max_dangling_edges"]
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("max_dangling_edges must be an explicit nonnegative integer")
    if any(not isinstance(policy[k], bool) for k in ("allow_empty_types", "allow_empty_graph")):
        raise ValueError("Empty-output overrides must be explicit booleans")


def _union(spark, frames, schema):
    if not frames:
        return spark.createDataFrame([], schema)
    result = frames[0]
    for frame in frames[1:]:
        result = result.unionByName(frame)
    return result


def build_graph_frames(
    spark: "SparkSession", contract: dict, sources: dict, bindings: dict[str, str],
    versions: dict[str, int], policy: dict, expected_fingerprint: str,
) -> dict:
    """Build and diagnose distributed graph DataFrames without persistent writes."""
    from pyspark.sql import functions as F

    _policy(policy)
    if contract_fingerprint(contract) != expected_fingerprint:
        raise ValueError("Rules changed since export; regenerate")
    if set(bindings) != set(sources["observations"]) or any(
        observation["table"] != bindings[alias]
        or (alias in versions and versions[alias] != observation["version"])
        for alias, observation in sources["observations"].items()
    ):
        raise ValueError("Source bindings/versions changed after preflight; run preflight again")
    build_id = uuid.uuid4().hex
    built_at = datetime.now(timezone.utc)
    cached = []
    diagnostics, mapping_reports = {}, []
    facts = {f["fact_id"]: f for f in contract["facts"]}
    try:
        raw = {}
        for node, label in ((True, "nodes"), (False, "edges")):
            frames = []
            for mapping in contract[label]:
                rows = _projection(spark, sources["frames"][mapping["source_table"]],
                                   mapping, node, contract["source"], facts, bindings[mapping["source_table"]]).persist()
                cached.append(rows)
                total = rows.count()
                invalid = rows.filter(F.col("id").isNull())
                nulls = invalid.count()
                diagnostics[mapping["fact_id"] + ":null_keys"] = invalid
                mapping_reports.append({
                    "fact_id": mapping["fact_id"], "type": mapping.get("node_type", mapping.get("edge_type")),
                    "rows": total, "null_keys": nulls, "null_key_policy": mapping["null_key_policy"],
                    "normalized_duplicate_ids": total - nulls - rows.filter(F.col("id").isNotNull()).select("id").distinct().count(),
                })
                frames.append(rows.filter(F.col("id").isNotNull()))
            # Empty edges need a real schema rather than inference from data.
            raw[label] = _union(spark, frames,
                "id string, src string, dst string, type string, from_type string, to_type string, "
                "_properties map<string,string>, _provenance string, _fact_id string")
        vertices, node_conflicts = _merge(raw["nodes"], True)
        edges, edge_conflicts = _merge(raw["edges"], False)
        valid_src = vertices.select(F.col("id").alias("src"), F.col("type").alias("from_type"))
        valid_dst = vertices.select(F.col("id").alias("dst"), F.col("type").alias("to_type"))
        matched = edges.join(valid_src, ["src", "from_type"], "left_semi").join(
            valid_dst, ["dst", "to_type"], "left_semi")
        dangling = edges.join(matched.select("id"), "id", "left_anti").persist()
        cached.append(dangling)
        dangling_count = dangling.count()
        diagnostics.update({
            "node_property_conflicts": node_conflicts, "edge_property_conflicts": edge_conflicts,
            "dangling_edges": dangling,
        })
        edge_candidates = edges.count()
        outputs = []
        for frame in (vertices, matched):
            frame = frame.withColumn("build_id", F.lit(build_id)).withColumn(
                "rules_fingerprint", F.lit(expected_fingerprint)
            ).withColumn("built_at", F.lit(built_at)).persist()
            cached.append(frame)
            outputs.append(frame)
        vertices, edges = outputs
        report = validate_frames(vertices, edges)
        report.update({
            "mappings": mapping_reports, "sources": sources["observations"], "policy": dict(policy),
            "excluded_fact_ids": contract["excluded_fact_ids"], "warnings": contract["warnings"],
            "node_property_conflicts": node_conflicts.count(), "edge_property_conflicts": edge_conflicts.count(),
            "dangling_edges": dangling_count, "edge_candidates": edge_candidates,
            "endpoint_match_ratio": (edge_candidates - dangling_count) / edge_candidates if edge_candidates else None,
            "build_id": build_id, "rules_fingerprint": expected_fingerprint,
        })
        blockers = []
        if any(m["null_keys"] and m["null_key_policy"] == "error" for m in mapping_reports):
            blockers.append("Null keys violate an approved error policy")
        if report["node_property_conflicts"] or report["edge_property_conflicts"]:
            blockers.append("Conflicting non-null properties require reviewed deterministic precedence")
        if dangling_count > policy["max_dangling_edges"]:
            blockers.append("Dangling edge count exceeds the explicit tolerance")
        selected_types = {m["node_type"] for m in contract["nodes"]} | {m["edge_type"] for m in contract["edges"]}
        actual_types = set(report["node_types"]) | set(report["edge_types"])
        if selected_types - actual_types and not policy["allow_empty_types"]:
            blockers.append("Selected types are empty: " + ", ".join(sorted(selected_types - actual_types)))
        if report["nodes"] == 0 and not policy["allow_empty_graph"]:
            blockers.append("The graph is empty")
        if report["invalid_nodes"] or report["invalid_edges"] or report["duplicate_node_ids"] or report["duplicate_edge_ids"]:
            blockers.append("Graph IDs or endpoint contracts are invalid")
        report["blockers"] = blockers
        report["publishable"] = not blockers
        return {
            "vertices": vertices, "edges": edges, "report": report, "diagnostics": diagnostics, "_cached": cached,
            "signature": _runtime_signature(contract, bindings, versions, policy), "build_id": build_id,
            "fingerprint": expected_fingerprint, "built_at": built_at.isoformat(),
        }
    except BaseException:
        for frame in cached:
            frame.unpersist()
        raise


def release_build(build: dict) -> None:
    """Release only caches owned by this build after exploration/publication."""
    for frame in build["_cached"]:
        frame.unpersist()
    build["_cached"].clear()


def validate_frames(vertices: "DataFrame", edges: "DataFrame") -> dict:
    """Run distributed integrity gates; collect bounded type/count aggregates only."""
    from pyspark.sql import functions as F

    node_count, edge_count = vertices.count(), edges.count()
    invalid_nodes = vertices.filter(F.col("id").isNull() | (F.col("id") == "") | F.col("type").isNull()).count()
    invalid_edges = edges.filter(
        F.col("id").isNull() | F.col("src").isNull() | F.col("dst").isNull()
    ).count()
    src = vertices.select(F.col("id").alias("src"), F.col("type").alias("from_type"))
    dst = vertices.select(F.col("id").alias("dst"), F.col("type").alias("to_type"))
    valid_edges = edges.join(src, ["src", "from_type"], "left_semi").join(dst, ["dst", "to_type"], "left_semi")
    used = edges.select(F.col("src").alias("id")).union(edges.select(F.col("dst").alias("id"))).distinct()
    return {
        "nodes": node_count, "edges": edge_count, "invalid_nodes": invalid_nodes,
        "invalid_edges": invalid_edges + edge_count - valid_edges.count(),
        "duplicate_node_ids": node_count - vertices.select("id").distinct().count(),
        "duplicate_edge_ids": edge_count - edges.select("id").distinct().count(),
        "isolated_vertices": vertices.join(used, "id", "left_anti").count(),
        "node_types": {r["type"]: r["count"] for r in vertices.groupBy("type").count().collect()},
        "edge_types": {r["type"]: r["count"] for r in edges.groupBy("type").count().collect()},
        "node_content_digest": _content_digest(vertices),
        "edge_content_digest": _content_digest(edges),
    }


def _content_digest(frame):
    from pyspark.sql import functions as F

    hashed = frame.select(F.sha2(F.to_json(F.struct(*[
        _column(name) for name in sorted(frame.columns)
    ]), {"ignoreNullFields": "false"}), 256).alias("_hash"))
    # Four commutative 64-bit limb sums detect changed cached/recomputed outputs.
    row = hashed.agg(*[
        F.sum(F.conv(F.substring("_hash", start, 16), 16, 10).cast("decimal(38,0)")).alias(str(start))
        for start in (1, 17, 33, 49)
    ]).first()
    return [str(value) if value is not None else "0" for value in row]


def _pair_metadata(states):
    metadata = []
    for state in states:
        if state["version"] is None:
            metadata.append(None)
        else:
            parsed = json.loads(state["metadata"] or "null")
            if not isinstance(parsed, dict) or parsed.get("format") != "ddg-graph-pair/v1":
                raise ValueError("Existing targets lack Graphwright pair metadata; explicit migration is required")
            metadata.append(parsed)
    if metadata[0] != metadata[1]:
        raise ValueError("Published graph pair is incomplete or mixed; recover before reading/replacing")
    return metadata[0]


def publication_plan(
    spark: "SparkSession", build: dict, bindings: dict[str, str], nodes_table: str, edges_table: str,
) -> dict:
    """Capture optimistic target versions and exact replacement confirmation."""
    for name in [nodes_table, edges_table, *bindings.values()]:
        _table_name(name)
    targets = [nodes_table.lower(), edges_table.lower()]
    if len(set(targets)) != 2 or set(targets) & {name.lower() for name in bindings.values()}:
        raise ValueError("Targets must be distinct and must not overlap any input table")
    states = [_versions(spark, name) for name in (nodes_table, edges_table)]
    _pair_metadata(states)
    return {
        "targets": [nodes_table, edges_table], "prior": states,
        "build_id": build["build_id"], "rules_fingerprint": build["fingerprint"],
        "confirmation": f"REPLACE {nodes_table} AND {edges_table} BUILD {build['build_id']} RULES {build['fingerprint']}",
    }


def _read_pair_at(spark, names, states):
    from pyspark.sql import functions as F

    metadata = _pair_metadata(states)
    if metadata is None:
        raise ValueError("No published graph pair exists")
    frames = [spark.read.option("versionAsOf", state["version"]).table(_table_name(name))
              for name, state in zip(names, states)]
    for frame in frames:
        if frame.filter(
            F.col("build_id").isNull() | F.col("rules_fingerprint").isNull()
            | (F.col("build_id") != metadata["build_id"])
            | (F.col("rules_fingerprint") != metadata["rules_fingerprint"])
        ).limit(1).count():
            raise ValueError("Published rows disagree with Delta pair commit metadata")
    report = validate_frames(*frames)
    if any(report[k] for k in ("invalid_nodes", "invalid_edges", "duplicate_node_ids", "duplicate_edge_ids")):
        raise ValueError("Published pair failed graph integrity checks")
    if [report["nodes"], report["edges"]] != metadata["counts"]:
        raise ValueError("Published row counts disagree with pair metadata")
    return frames[0], frames[1]


def read_published_pair(
    spark: "SparkSession", nodes_table: str, edges_table: str,
) -> tuple["DataFrame", "DataFrame", list[int]]:
    """Pin and validate a coherent pair, including commit metadata for empty tables."""
    names = [nodes_table, edges_table]
    states = [_versions(spark, name) for name in names]
    frames = _read_pair_at(spark, names, states)
    return *frames, [state["version"] for state in states]


def publish_graph(
    spark: "SparkSession", build: dict, contract: dict, bindings: dict[str, str], versions: dict[str, int],
    policy: dict, expected_fingerprint: str, nodes_table: str, edges_table: str, scratch_root: str, plan: dict,
    *, enabled: bool, confirmation: str, ownership: bool, coordination: str,
) -> dict:
    """Stage exact outputs and sequentially replace a coordinated two-table pair."""
    if not enabled or not ownership or not isinstance(coordination, str) or not coordination.strip():
        raise ValueError("Publication requires enablement, replacement ownership and an external exclusive writer lease")
    if (contract_fingerprint(contract) != expected_fingerprint
            or build["signature"] != _runtime_signature(contract, bindings, versions, policy)
            or build["fingerprint"] != expected_fingerprint):
        raise ValueError("Rules/configuration changed after validation; rebuild before confirming")
    if not build["report"]["publishable"]:
        raise ValueError("Publication blocked: " + "; ".join(build["report"]["blockers"]))
    if confirmation != plan["confirmation"]:
        raise ValueError("Copy the exact confirmation for this build and these targets")
    current_plan = publication_plan(spark, build, bindings, nodes_table, edges_table)
    if current_plan != plan:
        raise ValueError("Targets or build changed since confirmation; inspect a new publication plan")
    if not isinstance(scratch_root, str) or not re.fullmatch(r"(?:dbfs:/|/Volumes/)[A-Za-z0-9_./-]+", scratch_root):
        raise ValueError("Configure a dedicated protected dbfs:/ or /Volumes/ scratch directory")
    if ".." in scratch_root.split("/") or len(scratch_root.strip("/").split("/")) < 3:
        raise ValueError("Scratch location must be a dedicated directory, not a storage root")
    paths = [scratch_root.rstrip("/") + "/" + build["build_id"] + "/" + label for label in ("nodes", "edges")]
    receipt = {**plan, "stage_paths": paths, "coordination": coordination, "status": "incomplete", "committed": []}
    metadata = {
        "format": "ddg-graph-pair/v1", "build_id": build["build_id"],
        "rules_fingerprint": build["fingerprint"],
        "counts": [build["report"]["nodes"], build["report"]["edges"]],
    }
    receipt["pair_metadata"] = metadata
    receipt["expected_validation"] = build["report"]
    try:
        for frame, path in zip((build["vertices"], build["edges"]), paths):
            frame.write.format("delta").mode("errorifexists").save(path)
        staged = [spark.read.format("delta").load(path) for path in paths]
        staged_report = validate_frames(*staged)
        if staged_report != {key: build["report"][key] for key in staged_report}:
            raise ValueError("Materialized staged graph differs from validated output")
        if publication_plan(spark, build, bindings, nodes_table, edges_table) != plan:
            raise ValueError("A target changed while staging; no target replacement authorized")
        for frame, name, prior in zip(staged, plan["targets"], plan["prior"]):
            # Schema migrations require separate operator review, never overwriteSchema.
            if prior["version"] is not None:
                current = spark.read.option("versionAsOf", prior["version"]).table(_table_name(name))
                if {(f.name, f.dataType.simpleString()) for f in current.schema} != {
                    (f.name, f.dataType.simpleString()) for f in frame.schema
                }:
                    raise ValueError(f"Target schema changed for {name}; review migration separately")
        for frame, name, prior in zip(staged, plan["targets"], plan["prior"]):
            if _versions(spark, name) != prior:
                raise ValueError(f"{name} changed before its commit; exclusive writer coordination was lost")
            frame.write.format("delta").mode("overwrite").option(
                "userMetadata", json.dumps(metadata, sort_keys=True)
            ).saveAsTable(_table_name(name))
            receipt["committed"].append({"table": name, **_versions(spark, name)})
        states = [_versions(spark, name) for name in plan["targets"]]
        if _pair_metadata(states) != metadata:
            raise ValueError("Target commits do not identify this staged build")
        _read_pair_at(spark, plan["targets"], states)
        receipt.update({"status": "complete", "versions": [s["version"] for s in states], "validation": staged_report})
        return receipt
    except BaseException:
        print("PUBLICATION INCOMPLETE. Retain stages and recover manually:\n" + json.dumps(receipt, sort_keys=True, indent=2))
        raise


def retry_staged_publication(
    spark: "SparkSession", receipt: dict, bindings: dict[str, str], expected_versions: list[int | None],
    *, confirmation: str, ownership: bool, coordination: str,
) -> dict:
    """Operator-only retry of inspected stages under a newly acquired exclusive lease."""
    names = receipt["targets"]
    if len(names) != 2 or len(expected_versions) != 2:
        raise ValueError("Recovery requires exactly two targets and two inspected current versions")
    for name in [*names, *bindings.values()]:
        _table_name(name)
    if len({name.lower() for name in names}) != 2 or {
        name.lower() for name in names
    } & {name.lower() for name in bindings.values()}:
        raise ValueError("Recovery targets overlap one another or a source")
    required = f"RETRY {names[0]} AND {names[1]} BUILD {receipt['build_id']}"
    if not ownership or not coordination.strip() or confirmation != required:
        raise ValueError(f"Recovery requires exclusive coordination, ownership and confirmation: {required}")
    metadata = receipt["pair_metadata"]
    if (metadata["build_id"] != receipt["build_id"]
            or metadata["rules_fingerprint"] != receipt["rules_fingerprint"]):
        raise ValueError("Recovery receipt build metadata disagrees")
    paths = receipt["stage_paths"]
    if len(paths) != 2 or any(
        not re.fullmatch(r"(?:dbfs:/|/Volumes/)[A-Za-z0-9_./-]+", path)
        or ".." in path.split("/")
        or not path.endswith("/" + receipt["build_id"] + "/" + label)
        for path, label in zip(paths, ("nodes", "edges"))
    ):
        raise ValueError("Recovery requires exact build-owned staged Delta paths")
    states = [_versions(spark, name) for name in names]
    if [state["version"] for state in states] != expected_versions:
        raise ValueError("A target changed since recovery inspection; do not overwrite another writer")
    staged = [spark.read.format("delta").load(path) for path in paths]
    report = validate_frames(*staged)
    if report != {key: receipt["expected_validation"][key] for key in report}:
        raise ValueError("Recovery stages differ from the validated build")
    try:
        for frame, name, prior in zip(staged, names, states):
            if _versions(spark, name) != prior:
                raise ValueError("Writer coordination was lost during recovery")
            if prior["version"] is not None:
                existing = spark.read.option("versionAsOf", prior["version"]).table(_table_name(name))
                if {(f.name, f.dataType.simpleString()) for f in existing.schema} != {
                    (f.name, f.dataType.simpleString()) for f in frame.schema
                }:
                    raise ValueError("Recovery cannot perform a schema migration")
            frame.write.format("delta").mode("overwrite").option(
                "userMetadata", json.dumps(metadata, sort_keys=True)
            ).saveAsTable(_table_name(name))
        accepted = [_versions(spark, name) for name in names]
        if _pair_metadata(accepted) != metadata:
            raise ValueError("Recovery commits do not identify the staged build")
        _read_pair_at(spark, names, accepted)
        return {**receipt, "status": "complete", "versions": [state["version"] for state in accepted],
                "recovery_coordination": coordination}
    except BaseException:
        print("RECOVERY INCOMPLETE. Inspect target histories and retain stages:\n"
              + json.dumps(receipt, sort_keys=True, indent=2))
        raise
