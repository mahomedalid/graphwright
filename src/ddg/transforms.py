"""Key-normalization transforms shared by discovery checks and generated builders.

This module is intentionally self-contained (standard library only) because its
source is embedded verbatim into generated graph-building code.
"""

import json
import re
from urllib.parse import quote

TRANSFORM_NAMES = ("trim", "upper", "lower", "digits_only", "int", "strip_leading_zeros", "collapse_spaces")


def _apply_one(value, name):
    if value is None:
        return None
    text = str(value)
    if name == "trim":
        return text.strip()
    if name == "upper":
        return text.upper()
    if name == "lower":
        return text.lower()
    if name == "collapse_spaces":
        return re.sub(r"\s+", " ", text).strip()
    if name == "digits_only":
        digits = re.sub(r"\D", "", text)
        return digits or None
    if name == "strip_leading_zeros":
        stripped = text.lstrip("0")
        return stripped or ("0" if text else None)
    if name == "int":
        stripped = text.strip()
        try:
            return str(int(float(stripped))) if stripped else None
        except ValueError:
            return None
    raise ValueError(f"Unknown transform '{name}'. Allowed: {', '.join(TRANSFORM_NAMES)}")


def apply_transforms(value, transforms):
    """Apply an ordered transform chain and return a canonical string, or None for null keys."""
    if value is None:
        return None
    result = value
    for name in transforms or ():
        result = _apply_one(result, name)
        if result is None:
            return None
    text = str(result)
    return text if text != "" else None


def canonical_key(values, transforms_per_column):
    """Build a canonical composite key; returns None if any component is null."""
    if len(values) != len(transforms_per_column):
        raise ValueError("Each key component requires its own transform chain")
    parts = []
    for value, transforms in zip(values, transforms_per_column):
        part = apply_transforms(value, transforms)
        if part is None:
            return None
        parts.append(part)
    return parts[0] if len(parts) == 1 else json.dumps(parts, ensure_ascii=True, separators=(",", ":"))


def graph_node_id(
    node_type: str, namespace: str, values: list, transforms_per_column: list[list[str]],
) -> str | None:
    """Serialize a semantic identity as one readable ID with escaped, ordered components."""
    if not node_type or not namespace or namespace != namespace.strip():
        raise ValueError("Graph identity requires an entity type and an explicit semantic namespace")
    if not values or len(values) != len(transforms_per_column):
        raise ValueError("Graph identity requires key values and one transform chain per component")
    parts = [node_type, namespace]
    for value, chain in zip(values, transforms_per_column):
        normalized = apply_transforms(value, chain)
        if normalized is None:
            return None
        parts.append(normalized)
    return ":".join(quote(part, safe="") for part in parts)
