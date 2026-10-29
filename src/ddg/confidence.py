"""Confidence tiers for candidates: high, medium, or low.

The tier is computed from the evidence a candidate cites (not from model self-assessment)
and then capped by the tier the proposer states, so a model can lower but never raise it.
Every tier carries human-readable reasons for the SME.
"""

from __future__ import annotations

from typing import Any

TIERS = ("low", "medium", "high")
TIER_SCORE = {"low": 0.3, "medium": 0.6, "high": 0.85}
HIGH_COVERAGE = 0.9
MEDIUM_COVERAGE = 0.5


def _rank(tier: str) -> int:
    return TIERS.index(tier)


def _min_tier(*tiers: str | None) -> str:
    present = [t for t in tiers if t in TIERS]
    return min(present, key=_rank) if present else "low"


def _downgrade(tier: str) -> str:
    return TIERS[max(0, _rank(tier) - 1)]


def _cardinality_conflict(observed: str | None, declared: str | None) -> bool:
    if not observed or not declared or "-to-" not in observed or "-to-" not in declared:
        return False
    obs_from, obs_to = observed.split("-to-")
    dec_from, dec_to = declared.split("-to-")
    # "many" on the declared side tolerates an observed "one" (samples may not show repeats).
    return not ((obs_from == dec_from or dec_from == "many") and (obs_to == dec_to or dec_to == "many"))


def _reverse(cardinality: str) -> str:
    left, right = cardinality.split("-to-")
    return f"{right}-to-{left}"


def _edge_cardinality(join: dict, mapping: dict) -> str | None:
    """Orient a check_join result (referencing -> referenced) to the edge direction (from -> to).

    Only single-join edges can be oriented: if the referencing column holds the to-node key, each source row is a
    from-node and the join cardinality already reads from -> to; if it holds the from-node key, it is reversed.
    """
    observed = join.get("observed_cardinality_left_to_right")
    left_cols = [c.lower() for c in (join.get("left") or {}).get("columns", [])]
    if not observed or "-to-" not in observed or not left_cols:
        return None
    if left_cols == [c.lower() for c in mapping.get("to_key_columns", [])]:
        return observed
    if left_cols == [c.lower() for c in mapping.get("from_key_columns", [])]:
        return _reverse(observed)
    return None


def _results(evidence: list[dict], tool: str) -> list[dict]:
    return [{**e.get("result", {}), "_scope": e.get("scope"), "_args": e.get("args", {})} for e in evidence if e.get("tool") == tool]


def assess(
    kind: str,
    mapping: dict | None,
    evidence: list[dict],
    *,
    declared_cardinality: str | None = None,
    stated_tier: str | None = None,
    independent_samples: bool = False,
) -> dict[str, Any]:
    mapping = mapping or {}
    reasons: list[str] = []
    tier = "low"

    if kind == "node_mapping":
        keys = list(mapping.get("key_columns") or [])
        checks = [r for r in _results(evidence, "check_uniqueness")
                  if r.get("table", "").lower() == str(mapping.get("source_table", "")).lower()
                  and [c.lower() for c in r.get("columns", [])] == [k.lower() for k in keys]]
        if not checks:
            reasons.append("No uniqueness check on the proposed key")
        else:
            best = max(checks, key=lambda r: (bool(r.get("unique")), r.get("_scope") == "full"))
            distinct = best.get("distinct_keys") or 0
            dupes = best.get("duplicate_keys") or 0
            nulls = best.get("rows_with_null_key") or 0
            if best.get("unique") and best.get("_scope") == "full":
                tier = "high"
                reasons.append(f"Key is unique and non-null across {distinct} values (full-data check)")
            elif best.get("unique"):
                tier = "medium"
                reasons.append("Key is unique in a sample, not verified on full data")
            elif distinct and dupes / distinct <= 0.01 and nulls == 0:
                tier = "medium"
                reasons.append(f"Key is nearly unique ({dupes} duplicate values of {distinct})")
            else:
                reasons.append(f"Key is not unique ({dupes} duplicate values, {nulls} null-key rows)")

    elif kind == "edge_mapping":
        joins = _results(evidence, "check_join")
        compat = _results(evidence, "check_compatibility")
        best_join = max(joins, key=lambda r: r.get("row_coverage") or 0, default=None)
        coverage = (best_join or {}).get("row_coverage") or 0.0
        if best_join and coverage >= HIGH_COVERAGE and best_join.get("_scope") == "full":
            tier = "high"
            reasons.append(f"{coverage:.0%} of rows match the referenced key (full-data check)")
        elif best_join and coverage >= MEDIUM_COVERAGE:
            tier = "medium"
            reasons.append(f"{coverage:.0%} of rows match the referenced key")
        elif best_join and coverage > 0:
            reasons.append(f"Only {coverage:.1%} of rows match the referenced key")
        elif best_join:
            reasons.append("No value overlap between the key columns")
        else:
            reasons.append("No join check between the key columns")
        good_compat = [c for c in compat if c.get("verdict") == "compatible"]
        if good_compat:
            reasons.append("Key formats are compatible (value shapes and types agree)")
            if independent_samples and tier == "low":
                reasons.append("Source tables are independent samples, so low overlap is expected; overlap cannot confirm this edge")
        elif compat and all(c.get("verdict") == "incompatible" for c in compat):
            reasons.append("Key formats look incompatible")
        oriented = _edge_cardinality(best_join, mapping) if best_join and len(joins) == 1 else None
        if oriented and _cardinality_conflict(oriented, declared_cardinality):
            if tier != "low":
                tier = _downgrade(tier)
            reasons.append(f"Observed cardinality {oriented} conflicts with topology '{declared_cardinality}'")

    elif kind == "annotation":
        column = str(mapping.get("column") or "").lower()
        table = str(mapping.get("table") or "").lower()
        about_target = [
            e for e in evidence
            if e.get("tool") in ("profile_column", "sample_rows")
            and str(e.get("args", {}).get("table", "")).lower() == table
            and (not column or column in [str(c).lower() for c in (e.get("args", {}).get("columns") or [e.get("args", {}).get("column")])])
        ]
        if about_target:
            tier = "medium"
            reasons.append("Backed by a profile or sample of the annotated column")
        else:
            reasons.append("No profile or sample of the annotated table/column was cited")

    else:
        reasons.append("Observations are hypotheses for SME follow-up")

    evidence_tier = tier
    final = _min_tier(evidence_tier, stated_tier) if stated_tier in TIERS else evidence_tier
    if stated_tier in TIERS and _rank(stated_tier) < _rank(evidence_tier):
        reasons.append(f"Proposer lowered the tier to {stated_tier}")
    return {"tier": final, "evidence_tier": evidence_tier, "stated_tier": stated_tier, "reasons": reasons}


def ensure_confidence(candidate: dict, declared_cardinality: str | None = None) -> dict:
    """Backfill a tier for candidates created before tiers existed (display only; not persisted)."""
    if candidate.get("confidence_tier"):
        return candidate
    result = assess(candidate.get("kind", ""), candidate.get("mapping"), candidate.get("evidence", []),
                    declared_cardinality=declared_cardinality)
    return {**candidate, "confidence_tier": result["tier"], "confidence_reasons": result["reasons"] + ["Tier computed after the fact"]}
