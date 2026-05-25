# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Contradiction detection (plan §4.10.7, spec §8.3.4).

A contradiction is two (or more) **active** facts of the same
``(entity_id, fact_type, valid_at)`` that disagree on ``value``. Active
means ``superseded_by IS NULL`` — the resolver hasn't already picked a
winner. The output is purely informational; this job never modifies
facts. The resolver is the only writer that can mark supersession.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout


REPORT_FILENAME = "conflicts.md"


@dataclass(frozen=True)
class Conflict:
    """One contradiction group."""

    entity_id: str
    fact_type: str
    valid_at: str
    values: tuple[str, ...]            # distinct values asserted
    fact_ids: tuple[str, ...]          # fact ids participating in the conflict
    highest_confidence: str            # 'high' | 'medium' | 'low'


@dataclass
class ContradictionsResult:
    conflicts: list[Conflict] = field(default_factory=list)
    report_path: Path | None = None

    @property
    def count(self) -> int:
        return len(self.conflicts)


async def run(db: KBDB, layout: KBLayout) -> ContradictionsResult:
    """Detect contradictions and write ``kb/maintenance/conflicts.md``.

    The report is overwrite-style: every run rewrites the file from
    scratch so it always reflects current DB state.
    """
    rows = await db.fetchall(
        "SELECT entity_id, fact_type, valid_at, "
        "       GROUP_CONCAT(id, '|||') AS fact_id_list, "
        "       GROUP_CONCAT(value, '|||') AS value_list, "
        "       GROUP_CONCAT(confidence, '|||') AS conf_list, "
        "       COUNT(DISTINCT value) AS distinct_values "
        "FROM facts "
        "WHERE superseded_by IS NULL "
        "GROUP BY entity_id, fact_type, valid_at "
        "HAVING distinct_values > 1 "
        "ORDER BY entity_id, fact_type, valid_at"
    )

    conflicts: list[Conflict] = []
    for r in rows:
        ids = tuple(r["fact_id_list"].split("|||"))
        values = tuple(r["value_list"].split("|||"))
        confs = r["conf_list"].split("|||")
        # Deduplicate values while preserving order
        seen_values = []
        for v in values:
            if v not in seen_values:
                seen_values.append(v)
        conflicts.append(Conflict(
            entity_id=r["entity_id"],
            fact_type=r["fact_type"],
            valid_at=r["valid_at"],
            values=tuple(seen_values),
            fact_ids=ids,
            highest_confidence=_highest_confidence(confs),
        ))

    report_path = layout.maintenance_dir / REPORT_FILENAME
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        _render_report(conflicts), encoding="utf-8", newline="\n",
    )

    return ContradictionsResult(conflicts=conflicts, report_path=report_path)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}


def _highest_confidence(values: list[str]) -> str:
    return max(values, key=lambda v: _CONFIDENCE_RANK.get(v, -1))


def _render_report(conflicts: list[Conflict]) -> str:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# Conflicts",
        "",
        f"_Generated {ts} · {len(conflicts)} conflict(s) found_",
        "",
    ]
    if not conflicts:
        lines.append("_No conflicts detected._")
        lines.append("")
        return "\n".join(lines)

    lines.append(
        "Two or more **active** facts disagree on the same "
        "`(entity, fact_type, valid_at)`. Resolve by reviewing the source "
        "evidence and marking the rejected fact's `review_status: rejected` "
        "(the resolver will then pick the surviving fact)."
    )
    lines.append("")
    for c in conflicts:
        lines.append(f"## {c.entity_id} — {c.fact_type} @ {c.valid_at}")
        lines.append("")
        lines.append(f"_Highest confidence among conflicting facts: {c.highest_confidence}_")
        lines.append("")
        lines.append(f"Distinct values asserted: {', '.join(repr(v) for v in c.values)}")
        lines.append("")
        lines.append("Participating facts:")
        for fid in c.fact_ids:
            lines.append(f"- `{fid}`")
        lines.append("")
    return "\n".join(lines)
