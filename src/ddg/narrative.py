"""Plain-language storytelling for runs and iterations.

Agents produce long, technical notes. Humans need a short narrative: what was looked at,
what was found, what was proposed, and why the run stopped. Facts here come from recorded
activity (evidence, proposals, refusals), so the story is accurate even when a model's own
summary is missing or rambling.
"""

from __future__ import annotations

import re
from collections import Counter

MAX_STORY_CHARS = 700
STOP_REASONS = {
    "no_remaining_work": "it had nothing left to investigate",
    "iteration_limit": "it reached the step limit for this run",
    "stalled": "it stopped finding anything new",
    "time_limit": "it ran out of its time budget",
    "cost_limit": "it reached its cost budget",
    "tool_call_limit": "it reached its tool-call budget",
    "model_call_limit": "it reached its model-call budget",
    "cancelled": "someone cancelled it",
    "invalid_topology": "the topology could not be read",
    "interrupted": "the process stopped unexpectedly (it can be resumed)",
    "error": "it hit an error",
}
CHECK_TOOLS = {"check_join": "relationship", "check_compatibility": "format", "check_uniqueness": "key"}


def short_table(name: str) -> str:
    """edl_prod_vw_rawstd__ela_crm_table_contact -> ela_crm_table_contact (the meaningful tail)."""
    tail = name.split("__")[-1] if "__" in name else name
    return tail if len(tail) <= 40 else tail[:37] + "..."


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _join_words(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def candidate_label(candidate: dict) -> str:
    m = candidate.get("mapping") or {}
    if candidate.get("kind") == "node_mapping":
        return f"{m.get('node_type')} from {short_table(str(m.get('source_table', '')))}"
    if candidate.get("kind") == "edge_mapping":
        return f"{m.get('edge_type')} relationship via {short_table(str(m.get('source_table', '')))}"
    if candidate.get("kind") == "annotation":
        target = short_table(str(m.get("table", "")))
        return f"a dictionary note on {target}{'.' + m['column'] if m.get('column') else ''}"
    return "an observation"


def _refusal_kind(message: str) -> str:
    text = message.lower()
    if "conservative mode" in text:
        return "weak"
    if "duplicate" in text:
        return "duplicate"
    if "rejected" in text:
        return "rejected"
    return "invalid"


def iteration_activity(evidence: list[dict], proposed: list[dict], refusals: list[str]) -> dict:
    tools = Counter(e.get("tool") for e in evidence)
    tables: list[str] = []
    for e in evidence:
        args = e.get("args", {})
        for key in ("table", "left_table", "right_table"):
            if args.get(key) and args[key] not in tables:
                tables.append(args[key])
    return {
        "tables": tables,
        "checks": {label: tools.get(tool, 0) for tool, label in CHECK_TOOLS.items() if tools.get(tool)},
        "profiles": tools.get("profile_column", 0),
        "proposed": [{"id": c["id"], "label": candidate_label(c), "tier": c.get("confidence_tier")} for c in proposed],
        "refused": dict(Counter(_refusal_kind(r) for r in refusals)),
    }


def facts_story(activity: dict) -> str:
    sentences = []
    tables = activity.get("tables", [])
    checks = activity.get("checks", {})
    if tables:
        shown = [short_table(t) for t in tables[:3]]
        names = ", ".join(shown) + f" and {len(tables) - 3} more" if len(tables) > 3 else _join_words(shown)
        what = f"Looked at {names}"
        parts = [_plural(n, f"{label} check") for label, n in checks.items()]
        if activity.get("profiles"):
            parts.append(_plural(activity["profiles"], "column profile"))
        sentences.append(what + (f", running {_join_words(parts)}" if parts else "") + ".")
    proposed = activity.get("proposed", [])
    if proposed:
        items = [f"{p['label']} ({p['tier']} confidence)" if p.get("tier") else p["label"] for p in proposed]
        sentences.append(f"Proposed {_join_words(items)} for review.")
    elif tables:
        sentences.append("Nothing was strong enough to propose yet.")
    refused = activity.get("refused", {})
    reasons = {
        "weak": "because the evidence was too weak for conservative mode",
        "duplicate": "as already proposed",
        "rejected": "as previously rejected by a reviewer",
        "invalid": "as not fitting the topology or data",
    }
    for kind, n in refused.items():
        sentences.append(f"{_plural(n, 'idea')} {'was' if n == 1 else 'were'} held back {reasons[kind]}.")
    return " ".join(sentences) or "Reviewed what is already known; no new checks or proposals in this step."


_ID = re.compile(r"\b(?:ev|cand|run|call|fact)-[0-9a-f]{6,}\b")
_MD = re.compile(r"(\*\*|`|^#+\s*|^\s*[-*]\s+|^\s*\d+\.\s+)", re.MULTILINE)
_BOLD_UNDERSCORE = re.compile(r"(?<!\w)__(\S(?:.*?\S)?)__(?!\w)")  # __bold__, but not table__names


def clean_story(text: str) -> str:
    """Make model prose readable: drop markdown markers, evidence ids, and runaway length."""
    text = _ID.sub("", text or "")
    text = _BOLD_UNDERSCORE.sub(r"\1", text)
    text = _MD.sub("", text)
    text = re.sub(r"\(\s*[,;]?\s*\)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_STORY_CHARS:
        cut = text[:MAX_STORY_CHARS]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = (cut[: end + 1] if end > 200 else cut.rstrip() + "…")
    return text


def split_model_reply(reply: str) -> tuple[str | None, str]:
    """Split 'STORY: ... DETAILS: ...' replies. Returns (story or None, details)."""
    match = re.match(r"\s*\**STORY\**\s*:\s*(.*?)(?:\n\s*\**DETAILS\**\s*:\s*(.*))?\s*$", reply or "", re.DOTALL | re.IGNORECASE)
    if not match:
        return None, (reply or "").strip()
    story = clean_story(match.group(1))
    return (story or None), (match.group(2) or "").strip()


_BLOCK_START = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|#{1,6}\s|-{3,}\s*$|```)")
_ITEM = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$")


def fallback_story(summary: str) -> str:
    """Story for older iterations recorded before stories existed.

    Uses the opening paragraph only. When it introduces a list ("...explored two hypotheses:"), the
    headings of the first few top-level items complete the sentence instead of their full bodies.
    """
    lines = (summary or "").replace("\r\n", "\n").split("\n")
    opening: list[str] = []
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    while i < len(lines) and lines[i].strip() and not _BLOCK_START.match(lines[i]):
        opening.append(lines[i].strip())
        i += 1
    text = clean_story(" ".join(opening))
    if not text or text.endswith(":"):
        heads = []
        for line in lines[i:]:
            if not line.strip():
                continue
            if heads and (re.match(r"^\s*#{1,6}\s", line) or not (_ITEM.match(line) or line.startswith((" ", "\t")))):
                break  # only the first list: stop at a heading or a plain paragraph
            item = _ITEM.match(line)
            if item and not item.group(1):
                head = clean_story(re.split(r"\s[-–—]\s|:\s|\.\s", item.group(2), maxsplit=1)[0])
                if head:
                    heads.append(head.rstrip(".:"))
            if len(heads) == 3:
                break
        if heads:
            text = (text + " " if text else "") + "; ".join(heads) + "."
    sentences = re.split(r"(?<=[.!?])\s+", text)
    story = " ".join(sentences[:2])
    return story if len(story) <= 320 else story[:317].rstrip() + "…"


def run_story(run: dict) -> str:
    iterations = run.get("iterations", [])
    tables: set[str] = set()
    checks: Counter = Counter()
    proposed: list[dict] = []
    for it in iterations:
        activity = it.get("activity") or {}
        tables.update(activity.get("tables", []))
        checks.update(activity.get("checks", {}))
        proposed.extend(activity.get("proposed", []))
    steps = _plural(len(iterations), "step")
    planner = {"foundry": "The Foundry agent", "heuristic": "The offline planner", "profile": "The profiler"}.get(
        str(run.get("planner")), "The agent")
    mode = f" in {run['mode']} mode" if run.get("mode") and run.get("planner") != "profile" else ""
    first = f"{planner} worked on source '{run.get('source')}'{mode} for {steps}"
    if tables:
        first += f", examining {_plural(len(tables), 'table')}"
        if checks:
            first += f" and running {_join_words([_plural(n, f'{k} check') for k, n in checks.items()])}"
    sentences = [first + "."]
    if proposed:
        tiers = Counter(p.get("tier") or "unrated" for p in proposed)
        breakdown = ", ".join(f"{tiers[t]} {t}" for t in ("high", "medium", "low", "unrated") if tiers.get(t))
        sentences.append(f"It proposed {_plural(len(proposed), 'candidate')} for review ({breakdown} confidence).")
    elif run.get("planner") == "profile":
        sentences.append("It collected column statistics for the data dictionary.")
    elif iterations:
        sentences.append("It did not find anything strong enough to propose.")
    reason = run.get("termination_reason")
    if reason:
        sentences.append(f"It stopped because {STOP_REASONS.get(reason, reason.replace('_', ' '))}.")
    if run.get("status") in ("queued", "running"):
        sentences.append("It is still running.")
    return " ".join(sentences)
