"""Data dictionary: what we know about each table and column, assembled from discovery and facts.

Knowledge sources, by precedence:

1. SME annotations edited directly in the dictionary (``dictionary/annotations.json``)
2. Approved ``annotation`` facts (agent-proposed, SME-approved)
3. Source metadata comments (for example Databricks column comments)

Graph roles (node keys, properties, edge keys) come from approved facts and pending
candidates; column statistics come from ``profile_column`` evidence collected by runs;
rejected interpretations are kept as history so nobody repeats them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .store import ConflictError, Store, atomic_write_json, file_lock, read_json, utcnow

TABLE_FIELDS = ("description", "notes", "status")
COLUMN_FIELDS = ("description", "notes", "status", "values")
STATUSES = ("provisional", "verified", "deprecated")
MAX_TEXT = 5000
MAX_VALUES = 200


class DictionaryError(ValueError):
    pass


class DictionaryStore:
    """SME-curated annotations for one workspace, with optimistic per-entry revisions and an audit log."""

    def __init__(self, settings: Settings):
        self.dir = settings.workspace_dir / "dictionary"
        self.path = self.dir / "annotations.json"
        self.history_path = self.dir / "history.jsonl"
        self._lock = self.dir / ".lock"

    def load(self) -> dict:
        return read_json(self.path, default=None) or {"sources": {}}

    def tables(self, source: str) -> dict:
        return self.load().get("sources", {}).get(source, {}).get("tables", {})

    def update(
        self,
        source: str,
        table: str,
        column: str | None,
        changes: dict[str, Any],
        author: str,
        expected_revision: int | None = None,
    ) -> dict:
        if not author or not author.strip():
            raise DictionaryError("author is required")
        allowed = COLUMN_FIELDS if column else TABLE_FIELDS
        unknown = set(changes) - set(allowed)
        if unknown:
            raise DictionaryError(f"Unknown fields {sorted(unknown)}; allowed: {', '.join(allowed)}")
        clean = _validate_changes(changes)
        self.dir.mkdir(parents=True, exist_ok=True)
        with file_lock(self._lock):
            data = self.load()
            tables = data.setdefault("sources", {}).setdefault(source, {}).setdefault("tables", {})
            table_entry = tables.setdefault(table, {"columns": {}})
            target = table_entry if column is None else table_entry.setdefault("columns", {}).setdefault(column, {})
            revision = int(target.get("revision", 0))
            if expected_revision is not None and expected_revision != revision:
                raise ConflictError(
                    f"{table}{'.' + column if column else ''} is at revision {revision}, expected {expected_revision}. Reload and retry."
                )
            target.update(clean)
            target.update({"revision": revision + 1, "updated_by": author.strip(), "updated_at": utcnow()})
            atomic_write_json(self.path, data)
            with self.history_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "at": target["updated_at"], "author": target["updated_by"], "source": source,
                    "table": table, "column": column, "changes": clean, "revision": target["revision"],
                }) + "\n")
            return {k: v for k, v in target.items() if k != "columns"}


def _validate_changes(changes: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in changes.items():
        if key in ("description", "notes"):
            if not isinstance(value, str) or len(value) > MAX_TEXT:
                raise DictionaryError(f"{key} must be text up to {MAX_TEXT} characters")
            clean[key] = value.strip()
        elif key == "status":
            if value not in STATUSES:
                raise DictionaryError(f"status must be one of {', '.join(STATUSES)}")
            clean[key] = value
        elif key == "values":
            if not isinstance(value, dict) or len(value) > MAX_VALUES:
                raise DictionaryError(f"values must be an object of up to {MAX_VALUES} code -> meaning pairs")
            clean[key] = {str(k).strip(): str(v).strip() for k, v in value.items() if str(k).strip()}
    return clean


def collect_profiles(settings: Settings, source: str) -> dict[tuple[str, str], dict]:
    """Latest ``profile_column`` evidence per (table, column) across all runs of this workspace."""
    latest: dict[tuple[str, str], dict] = {}
    if not settings.runs_dir.exists():
        return latest
    for path in settings.runs_dir.glob("run-*/evidence/ev-*.json"):
        evidence = read_json(path)
        if not evidence or evidence.get("tool") != "profile_column" or evidence.get("source") != source:
            continue
        key = (evidence["args"]["table"], evidence["args"]["column"])
        if key not in latest or evidence["observed_at"] > latest[key]["observed_at"]:
            latest[key] = {**evidence["result"], "observed_at": evidence["observed_at"], "evidence_id": evidence["id"],
                           "run_id": evidence["run_id"]}
    return latest


def _decision_notes(candidate: dict) -> list[str]:
    return [d.get("notes") for d in candidate.get("decisions", []) if d.get("notes")]


def build_dictionary(settings: Settings, store: Store, source: str) -> dict:
    inventory = read_json(settings.metadata_dir / source / "inventory.json", default=None) or {}
    inv_tables: dict[str, dict] = inventory.get("tables", {})
    profiles = collect_profiles(settings, source)
    annotations = DictionaryStore(settings).tables(source)
    facts = store.list_facts(source)
    candidates = [c for c in store.list_candidates() if c.get("scope", {}).get("source") == source]

    tables: dict[str, dict] = {}

    def table(name: str) -> dict:
        if name not in tables:
            info = inv_tables.get(name, {})
            tables[name] = {
                "name": name,
                "type": info.get("type"),
                "row_count": info.get("row_count"),
                "source_comment": info.get("comment"),
                "schema_fingerprint": info.get("schema_fingerprint"),
                "in_inventory": name in inv_tables,
                "roles": [],
                "history": [],
                "suggestions": [],
                "_fact_annotation": None,
                "columns": {},
            }
            for col in info.get("columns", []):
                column(name, col["name"], col)
        return tables[name]

    def column(table_name: str, name: str, info: dict | None = None) -> dict:
        t = tables[table_name] if table_name in tables else table(table_name)
        if name not in t["columns"]:
            info = info or {}
            t["columns"][name] = {
                "name": name,
                "type": info.get("type"),
                "nullable": info.get("nullable"),
                "primary_key": info.get("primary_key", False),
                "source_comment": info.get("comment"),
                "in_inventory": bool(info),
                "roles": [],
                "suggestions": [],
                "_fact_annotation": None,
            }
        return t["columns"][name]

    for name in inv_tables:
        table(name)

    items = [(f, "approved") for f in facts] + [(c, c["status"]) for c in candidates if c["status"] != "approved"]
    for item, status in items:
        kind, mapping, ref = item["kind"], item.get("mapping") or {}, item["id"]
        if status == "rejected":
            target_table = mapping.get("source_table") or mapping.get("table")
            if target_table:
                table(target_table)["history"].append({
                    "candidate_id": ref, "kind": kind, "hypothesis": item["hypothesis"], "sme_notes": _decision_notes(item),
                })
            continue
        base = {"status": status, "ref": ref}
        if kind == "node_mapping":
            t = mapping["source_table"]
            table(t)["roles"].append({**base, "kind": "node", "node_type": mapping["node_type"],
                                      "label": f"{mapping['node_type']} node source"})
            uniq = next((e["result"] for e in item.get("evidence", []) if e["tool"] == "check_uniqueness"), None)
            for i, col in enumerate(mapping.get("key_columns", [])):
                column(t, col)["roles"].append({
                    **base, "kind": "node_key", "node_type": mapping["node_type"],
                    "transforms": (mapping.get("key_transforms") or [[]])[i] if i < len(mapping.get("key_transforms") or []) else [],
                    "label": f"Key of {mapping['node_type']}",
                    **({"unique": uniq.get("unique"), "distinct_keys": uniq.get("distinct_keys")} if uniq else {}),
                })
            for prop, col in (mapping.get("properties") or {}).items():
                column(t, col)["roles"].append({**base, "kind": "node_property", "node_type": mapping["node_type"],
                                                "property": prop, "label": f"{mapping['node_type']}.{prop}"})
        elif kind == "edge_mapping":
            t = mapping["source_table"]
            edge = mapping["edge_type"]
            table(t)["roles"].append({**base, "kind": "edge", "edge_type": edge,
                                      "label": f"{mapping.get('from_node_type')} -[{edge}]-> {mapping.get('to_node_type')} source"})
            joins = [e["result"] for e in item.get("evidence", []) if e["tool"] == "check_join"]
            for side in ("from", "to"):
                node_type = mapping.get(f"{side}_node_type")
                for col in mapping.get(f"{side}_key_columns", []):
                    join = next((j for j in joins if j["left"]["table"] == t and j["left"]["columns"] == [col]), None)
                    role = {**base, "kind": "edge_key", "edge_type": edge, "side": side, "node_type": node_type,
                            "label": f"{edge} {side} key -> {node_type}"}
                    if join:
                        role.update({
                            "references": f"{join['right']['table']}.{', '.join(join['right']['columns'])}",
                            "row_coverage": join["row_coverage"], "unmatched_keys": join["unmatched_left_keys"],
                            "null_key_rows": join["left_null_key_rows"], "cardinality": join["observed_cardinality_left_to_right"],
                        })
                    column(t, col)["roles"].append(role)
            for prop, col in (mapping.get("properties") or {}).items():
                column(t, col)["roles"].append({**base, "kind": "edge_property", "edge_type": edge, "property": prop,
                                                "label": f"{edge}.{prop}"})
        elif kind == "annotation":
            t = mapping.get("table")
            if not t:
                continue
            target = column(t, mapping["column"]) if mapping.get("column") else table(t)
            entry = {"ref": ref, "status": status, "description": mapping.get("description", ""),
                     "values": mapping.get("values") or {}, "rules": mapping.get("rules") or [],
                     "hypothesis": item["hypothesis"]}
            if status == "approved":
                target["_fact_annotation"] = entry
            else:
                target["suggestions"].append(entry)

    for (t, c), profile in profiles.items():
        if t in tables or t in inv_tables:
            column(t, c)["profile"] = _profile_summary(profile)

    for t_name, t_ann in annotations.items():
        table(t_name)
        for c_name in t_ann.get("columns", {}):
            column(t_name, c_name)

    out_tables = []
    for t_name in sorted(tables, key=str.lower):
        t = tables[t_name]
        t_ann = annotations.get(t_name, {})
        _resolve_knowledge(t, {k: v for k, v in t_ann.items() if k != "columns"})
        columns = []
        for c in t["columns"].values():
            _resolve_knowledge(c, t_ann.get("columns", {}).get(c["name"], {}))
            columns.append(c)
        t["columns"] = columns
        t["coverage"] = {
            "columns": len(columns),
            "described": sum(1 for c in columns if c["description"]),
            "profiled": sum(1 for c in columns if c.get("profile")),
            "mapped": sum(1 for c in columns if c["roles"]),
        }
        out_tables.append(t)

    summary = {
        "tables": len(out_tables),
        "tables_described": sum(1 for t in out_tables if t["description"]),
        "tables_mapped": sum(1 for t in out_tables if any(r["status"] == "approved" for r in t["roles"])),
        "columns": sum(t["coverage"]["columns"] for t in out_tables),
        "columns_described": sum(t["coverage"]["described"] for t in out_tables),
        "columns_profiled": sum(t["coverage"]["profiled"] for t in out_tables),
        "pending_suggestions": sum(len(t["suggestions"]) + sum(len(c["suggestions"]) for c in t["columns"]) for t in out_tables),
    }
    return {
        "workspace": settings.workspace,
        "source": source,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "inventory_observed_at": inventory.get("observed_at"),
        "dialect": inventory.get("dialect"),
        "summary": summary,
        "tables": out_tables,
    }


def _profile_summary(profile: dict) -> dict:
    rows = profile.get("row_count") or 0
    nulls = profile.get("null_count") or 0
    top = profile.get("top_values")
    return {
        "row_count": rows,
        "null_count": nulls,
        "null_pct": round(100 * nulls / rows, 2) if rows else None,
        "distinct_count": profile.get("distinct_count"),
        "unique": profile.get("unique"),
        "min_length": profile.get("min_length"),
        "max_length": profile.get("max_length"),
        "value_shapes": [s["shape"] for s in profile.get("value_shapes", [])][:5],
        "top_values": top if isinstance(top, list) else None,
        "observed_at": profile.get("observed_at"),
        "evidence_id": profile.get("evidence_id"),
        "run_id": profile.get("run_id"),
    }


def _resolve_knowledge(entry: dict, sme: dict) -> None:
    """Pick description/values by precedence and record where each came from."""
    fact = entry.pop("_fact_annotation", None)
    description, description_source = "", None
    if sme.get("description"):
        description, description_source = sme["description"], "sme"
    elif fact and fact["description"]:
        description, description_source = fact["description"], f"fact {fact['ref']}"
    elif entry.get("source_comment"):
        description, description_source = entry["source_comment"], "source comment"
    values = {**(fact["values"] if fact else {}), **sme.get("values", {})}
    entry.update({
        "description": description,
        "description_source": description_source,
        "status": sme.get("status") or ("verified" if fact else ("provisional" if description else None)),
        "notes": sme.get("notes", ""),
        "rules": fact["rules"] if fact else [],
        "revision": sme.get("revision", 0),
        "updated_by": sme.get("updated_by"),
        "updated_at": sme.get("updated_at"),
    })
    if "history" not in entry:  # columns carry code -> meaning values; tables do not
        entry["values"] = values


def to_markdown(dictionary: dict) -> str:
    s = dictionary["summary"]
    lines = [
        f"# Data Dictionary: {dictionary['workspace']} / {dictionary['source']}",
        "",
        f"Generated {dictionary['generated_at']} from discovery evidence, approved facts, and SME annotations.",
        f"Inventory observed {dictionary.get('inventory_observed_at') or 'never'}.",
        "",
        f"{s['tables']} tables ({s['tables_described']} described, {s['tables_mapped']} mapped to the graph); "
        f"{s['columns']} columns ({s['columns_described']} described, {s['columns_profiled']} profiled).",
        "",
    ]
    for t in dictionary["tables"]:
        lines += [f"## {t['name']}", ""]
        meta = [f"Rows: {t['row_count']}" if t.get("row_count") is not None else None,
                f"Type: {t['type']}" if t.get("type") else None,
                f"Status: {t['status']}" if t.get("status") else None,
                None if t["in_inventory"] else "Not in the latest inventory"]
        lines += [" · ".join(m for m in meta if m), ""]
        if t["description"]:
            lines += [t["description"] + (f" *(source: {t['description_source']})*" if t["description_source"] else ""), ""]
        if t["roles"]:
            lines += ["Graph roles:", ""] + [f"* {r['label']} ({r['status']})" for r in t["roles"]] + [""]
        if t["notes"]:
            lines += [f"Notes: {t['notes']}", ""]
        for rule in t["rules"]:
            lines.append(f"* Rule: {rule}")
        if t["rules"]:
            lines.append("")
        if t["columns"]:
            lines += ["| Column | Type | Description | Graph role | Nulls | Distinct | Status |",
                      "|--------|------|-------------|------------|-------|----------|--------|"]
            for c in t["columns"]:
                p = c.get("profile") or {}
                role = "; ".join(f"{r['label']}" + (" (pending)" if r["status"] != "approved" else "") for r in c["roles"])
                nulls = f"{p['null_pct']}%" if p.get("null_pct") is not None else ""
                lines.append("| " + " | ".join(_cell(v) for v in (
                    c["name"] + (" (PK)" if c.get("primary_key") else ""), c.get("type") or "", c["description"], role,
                    nulls, p.get("distinct_count", ""), c.get("status") or "",
                )) + " |")
            lines.append("")
            for c in t["columns"]:
                if c.get("values"):
                    lines += [f"Values of `{c['name']}`:", ""] + [f"* `{k}`: {v}" for k, v in c["values"].items()] + [""]
                if c.get("notes"):
                    lines += [f"Notes on `{c['name']}`: {c['notes']}", ""]
        if t["history"]:
            lines += ["Rejected interpretations:", ""]
            lines += [f"* {h['hypothesis']}" + (f" (SME: {'; '.join(h['sme_notes'])})" if h["sme_notes"] else "") for h in t["history"]]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def export(settings: Settings, store: Store, source: str, out_dir: Path | None = None) -> dict[str, Path]:
    dictionary = build_dictionary(settings, store, source)
    out_dir = out_dir or (settings.workspace_dir / "dictionary" / "exports")
    md_path = out_dir / f"{source}.md"
    json_path = out_dir / f"{source}.json"
    from .store import atomic_write_text

    atomic_write_text(md_path, to_markdown(dictionary))
    atomic_write_json(json_path, dictionary)
    return {"markdown": md_path, "json": json_path}
