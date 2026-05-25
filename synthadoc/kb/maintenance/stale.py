# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Stale entity-page detection (plan §4.10.12).

An entity page is *stale* when:

* It has at least one fact, AND
* ``entity.last_rebuilt`` is older than the most recent ``fact.observed_at``
  for that entity, OR ``last_rebuilt`` is NULL.

Pure SQL. Writes ``kb/maintenance/stale_pages.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout


REPORT_FILENAME = "stale_pages.md"


@dataclass(frozen=True)
class StaleEntity:
    entity_id: str
    name: str
    last_rebuilt: str | None
    newest_fact_observed_at: str


@dataclass
class StaleResult:
    stale: list[StaleEntity] = field(default_factory=list)
    report_path: Path | None = None

    @property
    def count(self) -> int:
        return len(self.stale)


async def run(db: KBDB, layout: KBLayout) -> StaleResult:
    rows = await db.fetchall(
        "SELECT e.id, e.name, e.last_rebuilt, MAX(f.observed_at) AS newest "
        "FROM entities e "
        "JOIN facts f ON f.entity_id = e.id "
        "GROUP BY e.id "
        "HAVING e.last_rebuilt IS NULL OR e.last_rebuilt < MAX(f.observed_at) "
        "ORDER BY e.id"
    )
    stale = [
        StaleEntity(
            entity_id=r["id"], name=r["name"],
            last_rebuilt=r["last_rebuilt"],
            newest_fact_observed_at=r["newest"],
        )
        for r in rows
    ]
    path = layout.maintenance_dir / REPORT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render(stale), encoding="utf-8", newline="\n")
    return StaleResult(stale=stale, report_path=path)


def _render(stale: list[StaleEntity]) -> str:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# Stale Entity Pages",
        "",
        f"_Generated {ts} · {len(stale)} stale page(s)_",
        "",
    ]
    if not stale:
        lines.append("_No stale entity pages._")
        lines.append("")
        return "\n".join(lines)
    lines.append(
        "These entity pages have facts newer than their last rebuild. "
        "Re-run the pipeline (or `synthadoc kb maintenance run`) to refresh them."
    )
    lines.append("")
    lines.append("| Entity | Name | Last rebuilt | Newest fact observed |")
    lines.append("|---|---|---|---|")
    for s in stale:
        lines.append(
            f"| `{s.entity_id}` | {s.name} | "
            f"{s.last_rebuilt or '_never_'} | {s.newest_fact_observed_at} |"
        )
    lines.append("")
    return "\n".join(lines)
