# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Evidence-gap and orphan detection (plan §4.10.15-17).

Three checks, each pure SQL, each writing its own report:

* **Orphan facts** — facts whose ``entity_id`` no longer matches any row in
  ``entities``. The FK normally prevents this, but the check is cheap and
  guards against a corrupted DB or a Layer 3 entity-merge bug.

* **Facts without evidence** — facts with empty ``source_quote`` or no
  matching ``source_id`` in ``sources``. Important because the
  substring-quote guard is FactExtractAgent-side; this catches drift from
  hand-written facts or older prompt versions.

* **Conclusions without supporting facts** — rows in ``conclusions`` that
  have zero entries in ``conclusion_basis``. Spec §3.4: derived
  conclusions must cite the observed facts they rest on.

All three writers are overwrite-style; every run reflects current DB state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout


ORPHAN_FACTS_FILENAME = "orphan_facts.md"
EVIDENCE_FILENAME = "facts_without_evidence.md"
CONCLUSIONS_FILENAME = "conclusions_without_facts.md"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class GapResult:
    """Generic result for any of the three checks."""

    items: list[dict] = field(default_factory=list)
    report_path: Path | None = None
    label: str = ""

    @property
    def count(self) -> int:
        return len(self.items)


# ---------------------------------------------------------------------------
# Orphan facts
# ---------------------------------------------------------------------------


async def run_orphan_facts(db: KBDB, layout: KBLayout) -> GapResult:
    rows = await db.fetchall(
        "SELECT f.id, f.entity_id, f.fact_type, f.valid_at "
        "FROM facts f "
        "LEFT JOIN entities e ON e.id = f.entity_id "
        "WHERE e.id IS NULL "
        "ORDER BY f.id"
    )
    path = layout.maintenance_dir / ORPHAN_FACTS_FILENAME
    _write(path, "Orphan Facts", rows, [
        ("Fact ID", "id"), ("Missing entity_id", "entity_id"),
        ("Fact type", "fact_type"), ("Valid at", "valid_at"),
    ], hint=(
        "These facts reference entities that no longer exist. The schema's "
        "foreign key normally prevents this — investigate any rows here."
    ))
    return GapResult(items=rows, report_path=path, label="orphan_facts")


# ---------------------------------------------------------------------------
# Facts without evidence
# ---------------------------------------------------------------------------


async def run_facts_without_evidence(db: KBDB, layout: KBLayout) -> GapResult:
    rows = await db.fetchall(
        "SELECT f.id, f.entity_id, f.fact_type, f.source_id, "
        "       (f.source_quote IS NULL OR LENGTH(TRIM(f.source_quote)) = 0) "
        "         AS missing_quote, "
        "       (s.id IS NULL) AS missing_source "
        "FROM facts f "
        "LEFT JOIN sources s ON s.id = f.source_id "
        "WHERE (f.source_quote IS NULL OR LENGTH(TRIM(f.source_quote)) = 0) "
        "   OR s.id IS NULL "
        "ORDER BY f.id"
    )
    path = layout.maintenance_dir / EVIDENCE_FILENAME
    _write(path, "Facts Without Evidence", rows, [
        ("Fact ID", "id"), ("Entity", "entity_id"),
        ("Fact type", "fact_type"), ("Source ID", "source_id"),
        ("Missing quote?", "missing_quote"),
        ("Missing source?", "missing_source"),
    ], hint=(
        "Every fact must have a non-empty `source_quote` AND a `source_id` "
        "that resolves to a row in `sources`. Facts here violate the spec §3.4 "
        "requirement that observed facts carry direct source evidence."
    ))
    return GapResult(items=rows, report_path=path, label="facts_without_evidence")


# ---------------------------------------------------------------------------
# Conclusions without supporting facts
# ---------------------------------------------------------------------------


async def run_conclusions_without_facts(db: KBDB, layout: KBLayout) -> GapResult:
    rows = await db.fetchall(
        "SELECT c.id, c.entity_id, c.conclusion_type, c.confidence "
        "FROM conclusions c "
        "LEFT JOIN conclusion_basis cb ON cb.conclusion_id = c.id "
        "GROUP BY c.id "
        "HAVING COUNT(cb.fact_id) = 0 "
        "ORDER BY c.id"
    )
    path = layout.maintenance_dir / CONCLUSIONS_FILENAME
    _write(path, "Conclusions Without Supporting Facts", rows, [
        ("Conclusion ID", "id"), ("Entity", "entity_id"),
        ("Type", "conclusion_type"), ("Confidence", "confidence"),
    ], hint=(
        "Conclusions must cite the observed facts they derive from. "
        "Conclusions here have zero entries in `conclusion_basis` and violate "
        "spec §3.4."
    ))
    return GapResult(items=rows, report_path=path, label="conclusions_without_facts")


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def _write(
    path: Path, title: str, rows: list[dict],
    columns: list[tuple[str, str]], *, hint: str,
) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"# {title}",
        "",
        f"_Generated {ts} · {len(rows)} row(s)_",
        "",
    ]
    if not rows:
        lines.append("_None._")
        lines.append("")
    else:
        lines.append(hint)
        lines.append("")
        headers = " | ".join(h for h, _ in columns)
        sep = " | ".join("---" for _ in columns)
        lines.append(f"| {headers} |")
        lines.append(f"| {sep} |")
        for r in rows:
            cells = []
            for _, key in columns:
                v = r.get(key)
                cells.append("" if v is None else str(v))
            lines.append(f"| {' | '.join(cells)} |")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
