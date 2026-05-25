# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Aggregator — runs every Layer 3 maintenance job and emits a snapshot.

``run_all`` is the entry point. It chains every individual job, collects
counts, refreshes ``kb/maintenance/kb_health.md``, and archives a
timestamped run under ``kb/maintenance/reports/``.

Each individual job is independent — a failure in one is logged and
surfaced in the returned :class:`MaintenanceReport` but does not abort
the rest. This matches the page-tier "best-effort" stance from Layer 2.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from synthadoc.agents.history_render_agent import HistoryRenderAgent
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.maintenance import broken_links as _broken_links
from synthadoc.kb.maintenance import contradictions as _contradictions
from synthadoc.kb.maintenance import duplicates as _duplicates
from synthadoc.kb.maintenance import evidence as _evidence
from synthadoc.kb.maintenance import people_safety as _people_safety
from synthadoc.kb.maintenance import stale as _stale
from synthadoc.observability.telemetry import get_tracer

logger = logging.getLogger(__name__)

HEALTH_FILENAME = "kb_health.md"


@dataclass
class MaintenanceReport:
    """Roll-up of one ``run_all`` invocation."""

    generated_at: str
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    histories_rendered: int = 0
    report_path: Path | None = None
    health_path: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


async def run_all(
    db: KBDB,
    layout: KBLayout,
    *,
    render_histories: bool = True,
) -> MaintenanceReport:
    """Run every maintenance job in order. Never raises."""
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = MaintenanceReport(generated_at=ts)
    tracer = get_tracer()

    async def _safely(label: str, coro_factory):
        with tracer.start_as_current_span(f"kb.maintenance.{label}") as span:
            try:
                result = await coro_factory()
                # Surface the headline count when available — every job's result
                # exposes a `count` property; broken_links/orphan etc. are dicts
                # without one. Guard with hasattr.
                if hasattr(result, "count"):
                    span.set_attribute("count", int(result.count))
                return result
            except Exception as exc:
                span.set_attribute("error", True)
                span.set_attribute("error.type", type(exc).__name__)
                logger.warning("maintenance %s failed: %s", label, exc)
                report.errors.append(f"{label}: {type(exc).__name__}: {exc}")
                return None

    # 1. Contradictions
    contradictions = await _safely(
        "contradictions", lambda: _contradictions.run(db, layout)
    )
    report.counts["conflicts"] = contradictions.count if contradictions else 0

    # 2. Stale entity pages
    stale = await _safely("stale", lambda: _stale.run(db, layout))
    report.counts["stale_pages"] = stale.count if stale else 0

    # 3. Evidence gaps
    orphans = await _safely(
        "orphan_facts", lambda: _evidence.run_orphan_facts(db, layout)
    )
    report.counts["orphan_facts"] = orphans.count if orphans else 0

    no_evidence = await _safely(
        "facts_without_evidence",
        lambda: _evidence.run_facts_without_evidence(db, layout),
    )
    report.counts["facts_without_evidence"] = no_evidence.count if no_evidence else 0

    no_basis = await _safely(
        "conclusions_without_facts",
        lambda: _evidence.run_conclusions_without_facts(db, layout),
    )
    report.counts["conclusions_without_facts"] = no_basis.count if no_basis else 0

    # 4. Duplicate entity candidates (deterministic, no LLM)
    duplicates = await _safely("duplicates", lambda: _duplicates.run(db, layout))
    report.counts["duplicate_entity_candidates"] = duplicates.count if duplicates else 0

    # 5. Broken wikilinks (links table → filesystem)
    broken = await _safely("broken_links", lambda: _broken_links.run(db, layout))
    report.counts["broken_links"] = broken.count if broken else 0

    # 6. People-page safety (spec §3.7)
    safety = await _safely("people_safety", lambda: _people_safety.run(db, layout))
    report.counts["people_pages_with_policy_flags"] = (
        len({f.path for f in safety.flags}) if safety else 0
    )

    # 7. Per-entity history pages
    if render_histories:
        with tracer.start_as_current_span("kb.maintenance.history_render") as span:
            agent = HistoryRenderAgent(db=db, layout=layout)
            for entity in await db.list_entities():
                try:
                    result = await agent.render(entity["id"])
                    if result.written:
                        report.histories_rendered += 1
                except Exception as exc:
                    logger.warning(
                        "history render failed for %s: %s", entity["id"], exc,
                    )
                    report.errors.append(
                        f"history({entity['id']}): {type(exc).__name__}: {exc}"
                    )
            span.set_attribute("histories_rendered", report.histories_rendered)

    # 8. Snapshot to kb_health.md
    health_text = _render_health(report)
    health_path = layout.maintenance_dir / HEALTH_FILENAME
    health_path.parent.mkdir(parents=True, exist_ok=True)
    health_path.write_text(health_text, encoding="utf-8", newline="\n")
    report.health_path = health_path

    # 9. Archive a timestamped copy
    stamp = ts.replace(":", "").replace("-", "").split("+")[0]
    archive_path = layout.reports_dir / f"{stamp}.md"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(health_text, encoding="utf-8", newline="\n")
    report.report_path = archive_path

    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


# Per-spec §8.5 monitoring metrics. Threshold = 0 means we'd like none.
_THRESHOLDS = {
    "conflicts": 0,
    "facts_without_evidence": 0,
    "conclusions_without_facts": 0,
    "orphan_facts": 0,
    "stale_pages": 5,
    "duplicate_entity_candidates": 0,
    "broken_links": 0,
    "people_pages_with_policy_flags": 0,
}


def _render_health(report: MaintenanceReport) -> str:
    lines = [
        "# KB Health",
        "",
        f"_Generated {report.generated_at}_",
        "",
        "| Metric | Value | Threshold | Status |",
        "|---|---:|---:|---|",
    ]
    for key, value in sorted(report.counts.items()):
        threshold = _THRESHOLDS.get(key, "—")
        if isinstance(threshold, int):
            status = "PASS" if value <= threshold else "FAIL"
        else:
            status = "—"
        lines.append(f"| {key} | {value} | {threshold} | {status} |")
    lines.append("")
    lines.append(f"Histories rendered: **{report.histories_rendered}**")
    lines.append("")
    if report.errors:
        lines.append("## Errors")
        lines.append("")
        for err in report.errors:
            lines.append(f"- {err}")
        lines.append("")
    return "\n".join(lines)
