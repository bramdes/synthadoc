# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Garbage-collection corpus (spec §8.1 #5, plan §10).

A single test that injects synthetic rot into a fresh kb tier and then
runs ``run_all`` to assert every maintenance detector picks up its
expected pathology. Spec §8.3.9 lists target detection rates; this test
proves we hit *every* category, not the rates.

What gets injected:

* a **contradiction** — two active facts for the same
  ``(entity, fact_type, valid_at)`` with different values;
* an **orphan fact** — a fact whose entity_id no longer matches any
  row in ``entities`` (inserted with FKs disabled);
* a **fact without evidence** — a fact whose ``source_quote`` is empty;
* a **conclusion without basis** — a row in ``conclusions`` with zero
  entries in ``conclusion_basis``;
* **duplicate-entity candidates** — two entities whose slugs differ
  only by a noise token (``alpha`` vs ``alpha-project``);
* a **stale page** — an entity with facts but a ``last_rebuilt`` older
  than the newest ``observed_at``;
* a **broken wikilink** — a links-table row pointing at a missing
  target slug.
"""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite
import pytest

from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.links import emit_links
from synthadoc.kb.maintenance.report import run_all


# ---------------------------------------------------------------------------
# Fixture — synthetic rot
# ---------------------------------------------------------------------------


@pytest.fixture
async def rotten(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()

    src_id = ids.source_id("meeting_transcript", "2026-05-22", "x")
    await db.insert_source(
        id=src_id, source_type="meeting_transcript", title="x",
        authority="informal", raw_path="r", parsed_path="p",
        created_at="2026-05-22T10:00:00Z",
        ingested_at="2026-05-23T09:00:00Z",
        sha256="h-1",
    )

    # --- two entities + a duplicate candidate -----------------------------
    a_id = ids.entity_id("project", "alpha")
    ap_id = ids.entity_id("project", "alpha-project")  # noise-token duplicate
    await db.upsert_entity(
        id=a_id, entity_type="project", name="Alpha", slug="alpha",
        path="kb/entities/projects/alpha/index.md",
    )
    await db.upsert_entity(
        id=ap_id, entity_type="project", name="Alpha Project",
        slug="alpha-project",
        path="kb/entities/projects/alpha-project/index.md",
    )

    # --- stale page: entity that was "rendered" before its newest fact ----
    stale_id = ids.entity_id("project", "stale")
    await db.upsert_entity(
        id=stale_id, entity_type="project", name="Stale", slug="stale",
        path="kb/entities/projects/stale/index.md",
    )
    await db.execute(
        "UPDATE entities SET last_rebuilt = ? WHERE id = ?",
        ("2026-04-01T00:00:00Z", stale_id),
    )
    stale_fid = ids.fact_id(stale_id, "project.status", "2026-05-22")
    await db.insert_fact(
        id=stale_fid, entity_id=stale_id, fact_type="project.status",
        value="completed", valid_at="2026-05-22",
        observed_at="2026-05-24T09:00:00Z",
        source_id=src_id, source_path="p",
        source_quote="status: completed",
        confidence="high",
        path="kb/facts/projects/stale/project.status/2026-05-22-completed.md",
    )

    # --- contradiction: two active facts with different values ------------
    fid1 = ids.fact_id(a_id, "project.status", "2026-05-22")
    fid2 = fid1 + "-2"
    await db.insert_fact(
        id=fid1, entity_id=a_id, fact_type="project.status",
        value="completed", valid_at="2026-05-22",
        observed_at="2026-05-23T09:00:00Z",
        source_id=src_id, source_path="p",
        source_quote="status: completed",
        confidence="high",
        path="kb/facts/projects/alpha/project.status/2026-05-22-completed.md",
    )
    await db.insert_fact(
        id=fid2, entity_id=a_id, fact_type="project.status",
        value="in_progress", valid_at="2026-05-22",
        observed_at="2026-05-23T09:00:00Z",
        source_id=src_id, source_path="p",
        source_quote="status: in progress",
        confidence="high",
        path="kb/facts/projects/alpha/project.status/2026-05-22-in_progress.md",
    )

    # --- fact without evidence: empty source_quote ------------------------
    no_ev_fid = ids.fact_id(ap_id, "project.status", "2026-05-22")
    await db.insert_fact(
        id=no_ev_fid, entity_id=ap_id, fact_type="project.status",
        value="completed", valid_at="2026-05-22",
        observed_at="2026-05-23T09:00:00Z",
        source_id=src_id, source_path="p",
        source_quote="placeholder",   # will be blanked below
        confidence="high",
        path="kb/facts/projects/alpha-project/project.status/2026-05-22-completed.md",
    )
    await db.execute(
        "UPDATE facts SET source_quote = '' WHERE id = ?", (no_ev_fid,),
    )

    # --- orphan fact: insert with FKs off so entity_id has no entity row --
    orphan_fid = "fact.project.ghost.project.status.2026-05-22"
    async with aiosqlite.connect(layout.db_path) as raw:
        await raw.execute("PRAGMA foreign_keys = OFF")
        await raw.execute(
            "INSERT INTO facts (id, entity_id, fact_type, value, valid_at, "
            "observed_at, source_id, source_path, source_quote, confidence, path)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (orphan_fid, ids.entity_id("project", "ghost"),
             "project.status", "x", "2026-05-22",
             "2026-05-23T09:00:00Z", src_id, "p", "ok",
             "high", "kb/facts/x.md"),
        )
        await raw.commit()

    # --- conclusion without basis ----------------------------------------
    cid = "conclusion.project.alpha.foo.2026-05-23"
    await db.execute(
        "INSERT INTO conclusions (id, entity_id, conclusion_type, value, "
        "confidence, path, generated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (cid, a_id, "foo", "bar", "high", "kb/conclusions/x.md",
         "2026-05-23T09:00:00Z"),
    )

    # --- broken wikilink: link to a slug with no on-disk file -------------
    await emit_links(
        db,
        from_path="kb/entities/projects/alpha/index.md",
        body="see [[totally-missing-target]]",
    )

    return layout, db


# ---------------------------------------------------------------------------
# Test — one run, assert on every detector
# ---------------------------------------------------------------------------


async def test_run_all_detects_every_synthetic_pathology(rotten):
    layout, db = rotten
    report = await run_all(db, layout)

    # All seven detectors fired and counted ≥1
    assert report.counts["conflicts"] >= 1, "contradiction not detected"
    assert report.counts["stale_pages"] >= 1, "stale page not detected"
    assert report.counts["orphan_facts"] >= 1, "orphan fact not detected"
    assert report.counts["facts_without_evidence"] >= 1, \
        "fact-without-evidence not detected"
    assert report.counts["conclusions_without_facts"] >= 1, \
        "conclusion-without-basis not detected"
    assert report.counts["duplicate_entity_candidates"] >= 1, \
        "duplicate-entity candidate not detected"
    assert report.counts["broken_links"] >= 1, "broken link not detected"


async def test_run_all_health_snapshot_marks_failures(rotten):
    """kb_health.md must mark each over-threshold metric as FAIL."""
    layout, db = rotten
    report = await run_all(db, layout)
    text = report.health_path.read_text(encoding="utf-8")
    # Per _THRESHOLDS, every count > 0 except stale_pages (cap 5) is a FAIL.
    fail_lines = [
        line for line in text.splitlines()
        if line.startswith("|") and "FAIL" in line
    ]
    # Expect at least conflicts, orphan_facts, facts_without_evidence,
    # conclusions_without_facts, duplicate_entity_candidates, broken_links → 6
    assert len(fail_lines) >= 6, (
        f"expected ≥6 FAIL rows in health snapshot, got {len(fail_lines)}:\n{text}"
    )


async def test_run_all_never_deletes_facts(rotten):
    """Spec §8.3.4 / plan §11 invariant: maintenance never reduces fact count."""
    layout, db = rotten
    before = await db.count_facts()
    await run_all(db, layout)
    after = await db.count_facts()
    assert after >= before
