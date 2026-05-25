# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for synthadoc.kb.maintenance.contradictions."""

from __future__ import annotations

import pytest

from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.maintenance import contradictions as M


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def wired(tmp_path):
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
    eid = ids.entity_id("project", "doc-intel")
    await db.upsert_entity(
        id=eid, entity_type="project", name="Doc Intel",
        slug="doc-intel",
        path="kb/entities/projects/doc-intel/index.md",
    )
    return layout, db, eid, src_id


async def _add_status(
    db, *, eid, src_id, value, valid_at="2026-05-22",
    confidence="high", suffix="",
):
    fid = ids.fact_id(eid, "project.status", valid_at) + suffix
    await db.insert_fact(
        id=fid, entity_id=eid, fact_type="project.status",
        value=value, valid_at=valid_at,
        observed_at="2026-05-23T09:00:00Z",
        source_id=src_id, source_path="p",
        source_quote=f"status: {value}",
        confidence=confidence,
        path=f"kb/facts/projects/doc-intel/project.status/{valid_at}-{value}.md",
    )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


async def test_no_facts_means_no_conflicts(wired):
    layout, db, _, _ = wired
    result = await M.run(db, layout)
    assert result.count == 0
    assert result.report_path.exists()
    assert "_No conflicts detected._" in result.report_path.read_text(encoding="utf-8")


async def test_single_fact_per_group_is_not_a_conflict(wired):
    layout, db, eid, src = wired
    await _add_status(db, eid=eid, src_id=src, value="completed")
    result = await M.run(db, layout)
    assert result.count == 0


async def test_two_distinct_values_at_same_date_is_a_conflict(wired):
    layout, db, eid, src = wired
    await _add_status(db, eid=eid, src_id=src, value="completed")
    await _add_status(db, eid=eid, src_id=src, value="in_progress", suffix="-2")
    result = await M.run(db, layout)
    assert result.count == 1
    c = result.conflicts[0]
    assert c.entity_id == eid
    assert c.fact_type == "project.status"
    assert c.valid_at == "2026-05-22"
    assert set(c.values) == {"completed", "in_progress"}
    assert len(c.fact_ids) == 2


async def test_same_value_twice_is_not_a_conflict(wired):
    """Two facts agreeing is duplication, not contradiction."""
    layout, db, eid, src = wired
    await _add_status(db, eid=eid, src_id=src, value="completed")
    await _add_status(db, eid=eid, src_id=src, value="completed", suffix="-2")
    result = await M.run(db, layout)
    assert result.count == 0


async def test_superseded_facts_excluded(wired):
    """A fact marked superseded is no longer 'active' — it can't conflict."""
    layout, db, eid, src = wired
    await _add_status(db, eid=eid, src_id=src, value="in_progress",
                      valid_at="2026-05-01")
    await _add_status(db, eid=eid, src_id=src, value="completed",
                      valid_at="2026-05-22")
    # Mark the old fact superseded by the new one
    old_id = ids.fact_id(eid, "project.status", "2026-05-01")
    new_id = ids.fact_id(eid, "project.status", "2026-05-22")
    await db.mark_superseded(old_id, new_id)
    # Now add a third fact at 2026-05-22 with a different value → conflict at 05-22 only
    await _add_status(db, eid=eid, src_id=src, value="blocked",
                      valid_at="2026-05-22", suffix="-2")
    result = await M.run(db, layout)
    # 2026-05-01 has only one active fact (superseded); 2026-05-22 has two distinct → 1 conflict
    assert result.count == 1
    assert result.conflicts[0].valid_at == "2026-05-22"


async def test_different_entities_are_separate_groups(wired):
    layout, db, eid, src = wired
    other_eid = ids.entity_id("project", "other")
    await db.upsert_entity(
        id=other_eid, entity_type="project", name="Other", slug="other",
        path="kb/entities/projects/other/index.md",
    )
    await _add_status(db, eid=eid, src_id=src, value="completed")
    await _add_status(db, eid=other_eid, src_id=src, value="completed")
    # Each entity has only one fact → no conflict
    result = await M.run(db, layout)
    assert result.count == 0


async def test_highest_confidence_recorded(wired):
    layout, db, eid, src = wired
    await _add_status(db, eid=eid, src_id=src, value="completed", confidence="medium")
    await _add_status(db, eid=eid, src_id=src, value="in_progress",
                      confidence="high", suffix="-2")
    result = await M.run(db, layout)
    assert result.conflicts[0].highest_confidence == "high"


# ---------------------------------------------------------------------------
# Report content + idempotency
# ---------------------------------------------------------------------------


async def test_report_lists_participating_facts(wired):
    layout, db, eid, src = wired
    await _add_status(db, eid=eid, src_id=src, value="completed")
    await _add_status(db, eid=eid, src_id=src, value="in_progress", suffix="-2")
    result = await M.run(db, layout)
    text = result.report_path.read_text(encoding="utf-8")
    assert "doc-intel" in text
    assert "project.status" in text
    assert "completed" in text
    assert "in_progress" in text


async def test_running_twice_overwrites_report(wired):
    """Second run with cleared state must show '_No conflicts detected._'."""
    layout, db, eid, src = wired
    await _add_status(db, eid=eid, src_id=src, value="completed")
    await _add_status(db, eid=eid, src_id=src, value="in_progress", suffix="-2")
    await M.run(db, layout)
    # Mark one of them rejected by tagging superseded (simplest: mark the second superseded by the first)
    old_id = ids.fact_id(eid, "project.status", "2026-05-22") + "-2"
    new_id = ids.fact_id(eid, "project.status", "2026-05-22")
    await db.mark_superseded(old_id, new_id)

    result2 = await M.run(db, layout)
    assert result2.count == 0
    text = result2.report_path.read_text(encoding="utf-8")
    assert "_No conflicts detected._" in text
