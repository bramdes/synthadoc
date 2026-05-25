# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Duplicate-entity detection (plan §4.10.14, deferred from entity_linker).

Pure Python (no LLM) for v0.3. The detector flags pairs of entities of
the same ``entity_type`` whose names look suspiciously alike. Heuristics:

* Same slug after dropping noise tokens (``the``, ``project``, ``team``).
* Levenshtein-distance ≤ 2 between full slugs (catches typos and
  pluralisation).
* Name is a substring/superstring of the other (``Doc Intel`` /
  ``Document Intelligence`` heuristic).

The output is purely advisory — it never merges or modifies entities.
Acting on a proposal is a separate Layer 3 follow-up (or, until then,
a human edit).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout


REPORT_FILENAME = "duplicate_entities.md"

# Tokens stripped before slug comparison. Order matters — longer first.
_NOISE_TOKENS = ("the", "project", "team", "group", "topic", "page")


@dataclass(frozen=True)
class DuplicateCandidate:
    entity_type: str
    a_id: str
    b_id: str
    a_name: str
    b_name: str
    reason: str           # short string explaining why the pair was flagged


@dataclass
class DuplicatesResult:
    candidates: list[DuplicateCandidate] = field(default_factory=list)
    report_path: Path | None = None

    @property
    def count(self) -> int:
        return len(self.candidates)


async def run(db: KBDB, layout: KBLayout, *, max_distance: int = 2) -> DuplicatesResult:
    entities = await db.list_entities()
    # Group by type — duplicates only make sense within the same kind.
    by_type: dict[str, list[dict]] = {}
    for e in entities:
        by_type.setdefault(e["entity_type"], []).append(e)

    candidates: list[DuplicateCandidate] = []
    for et, group in by_type.items():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if a["id"] == b["id"]:
                    continue
                reason = _classify(a, b, max_distance=max_distance)
                if reason:
                    candidates.append(DuplicateCandidate(
                        entity_type=et,
                        a_id=a["id"], b_id=b["id"],
                        a_name=a["name"], b_name=b["name"],
                        reason=reason,
                    ))

    # Stable order
    candidates.sort(key=lambda c: (c.entity_type, c.a_id, c.b_id))

    path = layout.maintenance_dir / REPORT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render(candidates), encoding="utf-8", newline="\n")
    return DuplicatesResult(candidates=candidates, report_path=path)


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------


def _classify(a: dict, b: dict, *, max_distance: int) -> str:
    a_slug = a["slug"]
    b_slug = b["slug"]
    if a_slug == b_slug:
        # Should not happen (the linker prevents same slug + same type), but
        # surface it anyway since it's a corruption signal.
        return "identical slugs"
    a_core = _strip_noise(a_slug)
    b_core = _strip_noise(b_slug)
    if a_core and a_core == b_core:
        return "same slug after dropping noise tokens"
    if a_slug in b_slug or b_slug in a_slug:
        return "one slug is a substring of the other"
    dist = _levenshtein(a_slug, b_slug, cap=max_distance + 1)
    if dist <= max_distance:
        return f"slug Levenshtein distance {dist}"
    return ""


def _strip_noise(slug: str) -> str:
    parts = [p for p in slug.split("-") if p and p not in _NOISE_TOKENS]
    return "-".join(parts)


def _levenshtein(a: str, b: str, *, cap: int) -> int:
    """Bounded Levenshtein distance. Returns ``cap`` if distance >= cap."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) >= cap:
        return cap
    # Two-row DP, bounded.
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * lb
        row_min = cur[0]
        for j, cb in enumerate(b, 1):
            ins = cur[j - 1] + 1
            dele = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            cur[j] = min(ins, dele, sub)
            if cur[j] < row_min:
                row_min = cur[j]
        if row_min >= cap:
            return cap
        prev = cur
    return min(prev[-1], cap)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render(candidates: list[DuplicateCandidate]) -> str:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# Duplicate Entity Candidates",
        "",
        f"_Generated {ts} · {len(candidates)} candidate pair(s)_",
        "",
    ]
    if not candidates:
        lines.append("_No duplicate candidates._")
        lines.append("")
        return "\n".join(lines)
    lines.append(
        "These pairs share suspicious naming. The detector is advisory — "
        "review each pair and (if confirmed) consolidate by setting one "
        "entity's `merged_into` to the other's id."
    )
    lines.append("")
    lines.append("| Type | Entity A | Entity B | Reason |")
    lines.append("|---|---|---|---|")
    for c in candidates:
        lines.append(
            f"| {c.entity_type} | `{c.a_id}` ({c.a_name}) | "
            f"`{c.b_id}` ({c.b_name}) | {c.reason} |"
        )
    lines.append("")
    return "\n".join(lines)
