# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for EntityRenderAgent."""

from __future__ import annotations

import pytest

from synthadoc.agents.entity_render_agent import EntityRenderAgent, REVIEW_QUEUE_FILENAME
from synthadoc.kb import frontmatter as fm
from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.rules import FactRule, Rules


_RULES = Rules(by_fact_type={
    "project.status": FactRule(
        fact_type="project.status",
        strategy="latest_valid_at_wins",
        minimum_confidence="medium",
    ),
    "project.milestone": FactRule(
        fact_type="project.milestone", strategy="append_only",
    ),
})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def wired(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()

    eid = ids.entity_id("project", "doc-intel")
    await db.upsert_entity(
        id=eid, entity_type="project", name="Document Intelligence",
        slug="doc-intel",
        path="kb/entities/projects/doc-intel/index.md",
    )

    src_id = ids.source_id("meeting_transcript", "2026-05-22", "x")
    await db.insert_source(
        id=src_id, source_type="meeting_transcript", title="x",
        authority="informal", raw_path="r", parsed_path="p",
        created_at="2026-05-22T10:00:00Z",
        ingested_at="2026-05-23T09:00:00Z",
        sha256="h-1",
    )

    return layout, db, eid, src_id


async def _add_status_fact(
    db, *, eid, src_id, valid_at, value, observed_at="2026-05-23T09:00:00Z",
    confidence="high",
):
    fid = ids.fact_id(eid, "project.status", valid_at)
    await db.insert_fact(
        id=fid, entity_id=eid, fact_type="project.status",
        value=value, valid_at=valid_at, observed_at=observed_at,
        source_id=src_id, source_path="p",
        source_quote=f"Status is {value}",
        confidence=confidence,
        path=f"kb/facts/projects/doc-intel/project.status/{valid_at}-{value}.md",
    )
    return fid


async def _add_milestone(db, *, eid, src_id, valid_at, value):
    fid = ids.fact_id(eid, "project.milestone", valid_at)
    await db.insert_fact(
        id=fid, entity_id=eid, fact_type="project.milestone",
        value=value, valid_at=valid_at, observed_at="2026-05-23T09:00:00Z",
        source_id=src_id, source_path="p",
        source_quote=f"Milestone {value}",
        confidence="high",
        path=f"kb/facts/projects/doc-intel/project.milestone/{valid_at}-{value}.md",
    )
    return fid


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_render_writes_entity_page(wired):
    layout, db, eid, src_id = wired
    await _add_status_fact(db, eid=eid, src_id=src_id,
                           valid_at="2026-05-22", value="completed")
    agent = EntityRenderAgent(db=db, layout=layout, rules=_RULES)
    result = await agent.render(eid)

    assert result.written is True
    assert result.queued_for_review is False
    assert result.path.exists()

    parsed = fm.read(result.path)
    assert parsed.type == "entity"
    assert parsed.data["entity_type"] == "project"
    assert parsed.data["current_state_review_status"] == "unreviewed"
    assert "# Document Intelligence" in parsed.body
    assert "## Current State" in parsed.body
    assert "completed" in parsed.body
    assert "2026-05-22" in parsed.body


async def test_render_includes_resolved_value_in_summary(wired):
    layout, db, eid, src_id = wired
    await _add_status_fact(db, eid=eid, src_id=src_id,
                           valid_at="2026-05-22", value="completed")
    agent = EntityRenderAgent(db=db, layout=layout, rules=_RULES)
    result = await agent.render(eid)
    body = result.path.read_text(encoding="utf-8")
    assert "project.status = **completed**" in body
    assert "as of 2026-05-22" in body


async def test_render_includes_append_only_history(wired):
    layout, db, eid, src_id = wired
    await _add_milestone(db, eid=eid, src_id=src_id,
                         valid_at="2026-04-01", value="kickoff")
    await _add_milestone(db, eid=eid, src_id=src_id,
                         valid_at="2026-05-22", value="drop-1")
    agent = EntityRenderAgent(db=db, layout=layout, rules=_RULES)
    result = await agent.render(eid)
    body = result.path.read_text(encoding="utf-8")
    assert "## History (append-only series)" in body
    assert "kickoff" in body
    assert "drop-1" in body


async def test_render_updates_last_rebuilt(wired):
    layout, db, eid, src_id = wired
    await _add_status_fact(db, eid=eid, src_id=src_id,
                           valid_at="2026-05-22", value="completed")
    agent = EntityRenderAgent(db=db, layout=layout, rules=_RULES)
    await agent.render(eid)
    row = await db.get_entity(eid)
    assert row["last_rebuilt"]


# ---------------------------------------------------------------------------
# Review queue branch
# ---------------------------------------------------------------------------


async def test_reviewed_entity_writes_proposal_not_index(wired):
    layout, db, eid, src_id = wired
    # Flip the entity to reviewed
    await db.execute(
        "UPDATE entities SET current_state_review_status = 'reviewed' WHERE id = ?",
        (eid,),
    )
    await _add_status_fact(db, eid=eid, src_id=src_id,
                           valid_at="2026-05-22", value="completed")
    agent = EntityRenderAgent(db=db, layout=layout, rules=_RULES)
    result = await agent.render(eid)

    assert result.written is False
    assert result.queued_for_review is True

    # Index page must NOT have been written
    assert not result.path.exists()

    # Review queue must contain the proposal
    queue_path = layout.maintenance_dir / REVIEW_QUEUE_FILENAME
    assert queue_path.exists()
    text = queue_path.read_text(encoding="utf-8")
    assert "Proposed update — Document Intelligence" in text
    assert "project.status" in text


async def test_reviewed_entity_appends_proposals(wired):
    layout, db, eid, src_id = wired
    await db.execute(
        "UPDATE entities SET current_state_review_status = 'reviewed' WHERE id = ?",
        (eid,),
    )
    await _add_status_fact(db, eid=eid, src_id=src_id,
                           valid_at="2026-05-22", value="completed")
    agent = EntityRenderAgent(db=db, layout=layout, rules=_RULES)
    await agent.render(eid)
    await agent.render(eid)
    text = (layout.maintenance_dir / REVIEW_QUEUE_FILENAME).read_text(encoding="utf-8")
    # Two proposals appended
    assert text.count("Proposed update — Document Intelligence") == 2


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_entity_with_no_facts_skips_write(wired):
    layout, db, eid, _ = wired
    agent = EntityRenderAgent(db=db, layout=layout, rules=_RULES)
    result = await agent.render(eid)
    assert result.written is False
    assert result.queued_for_review is False
    assert not result.path.exists()


async def test_unknown_entity_raises(wired):
    layout, db, _, _ = wired
    agent = EntityRenderAgent(db=db, layout=layout, rules=_RULES)
    with pytest.raises(KeyError):
        await agent.render("entity.project.ghost")


async def test_low_confidence_facts_filtered_from_current_state(wired):
    """Below minimum_confidence → fact dropped from current state."""
    layout, db, eid, src_id = wired
    await _add_status_fact(db, eid=eid, src_id=src_id,
                           valid_at="2026-05-22", value="completed",
                           confidence="low")
    agent = EntityRenderAgent(db=db, layout=layout, rules=_RULES)
    result = await agent.render(eid)
    body = result.path.read_text(encoding="utf-8")
    # No resolved current state because the only fact was too low-confidence
    assert "_No resolved current state yet._" in body


async def test_render_includes_recent_changes_section(wired):
    layout, db, eid, src_id = wired
    await _add_status_fact(db, eid=eid, src_id=src_id,
                           valid_at="2026-05-22", value="completed")
    agent = EntityRenderAgent(db=db, layout=layout, rules=_RULES)
    result = await agent.render(eid)
    body = result.path.read_text(encoding="utf-8")
    assert "## Recent Changes" in body
    assert "project.status = completed" in body
