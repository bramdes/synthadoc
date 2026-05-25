# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for HistoryRenderAgent."""

from __future__ import annotations

import pytest

from synthadoc.agents.history_render_agent import HistoryRenderAgent
from synthadoc.kb import frontmatter as fm
from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout


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


async def _add(db, *, eid, src_id, valid_at, value, fact_type="project.status",
               confidence="high"):
    fid = ids.fact_id(eid, fact_type, valid_at)
    await db.insert_fact(
        id=fid, entity_id=eid, fact_type=fact_type,
        value=value, valid_at=valid_at, observed_at="2026-05-23T09:00:00Z",
        source_id=src_id, source_path="p",
        source_quote=f"value is {value}",
        confidence=confidence,
        path=f"kb/facts/projects/doc-intel/{fact_type}/{valid_at}-{value}.md",
    )
    return fid


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_render_writes_history_page(wired):
    layout, db, eid, src = wired
    await _add(db, eid=eid, src_id=src, valid_at="2026-05-01", value="in_progress")
    await _add(db, eid=eid, src_id=src, valid_at="2026-05-22", value="completed")
    agent = HistoryRenderAgent(db=db, layout=layout)

    result = await agent.render(eid)
    assert result.written is True
    assert result.total_facts == 2
    assert result.fact_types_covered == 1

    parsed = fm.read(result.path)
    assert parsed.type == "history"
    assert parsed.data["entity_id"] == eid
    body = parsed.body
    assert "# Document Intelligence — History" in body
    assert "in_progress" in body
    assert "completed" in body
    # Chronological order: earlier date appears before later date
    assert body.index("2026-05-01") < body.index("2026-05-22")


async def test_render_groups_by_fact_type(wired):
    layout, db, eid, src = wired
    await _add(db, eid=eid, src_id=src, valid_at="2026-05-22",
               value="completed", fact_type="project.status")
    await _add(db, eid=eid, src_id=src, valid_at="2026-05-22",
               value="pravin", fact_type="project.owner")
    agent = HistoryRenderAgent(db=db, layout=layout)
    result = await agent.render(eid)
    body = result.path.read_text(encoding="utf-8")
    assert "## project.status" in body
    assert "## project.owner" in body
    assert result.fact_types_covered == 2


async def test_render_includes_superseded_marker(wired):
    layout, db, eid, src = wired
    old = await _add(db, eid=eid, src_id=src, valid_at="2026-05-01", value="in_progress")
    new = await _add(db, eid=eid, src_id=src, valid_at="2026-05-22", value="completed")
    await db.mark_superseded(old, new)
    agent = HistoryRenderAgent(db=db, layout=layout)
    result = await agent.render(eid)
    body = result.path.read_text(encoding="utf-8")
    assert "superseded by" in body
    assert new in body


async def test_render_includes_source_quote(wired):
    layout, db, eid, src = wired
    await _add(db, eid=eid, src_id=src, valid_at="2026-05-22", value="completed")
    agent = HistoryRenderAgent(db=db, layout=layout)
    result = await agent.render(eid)
    body = result.path.read_text(encoding="utf-8")
    assert "> value is completed" in body


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_entity_without_facts_does_not_write(wired):
    layout, db, eid, _ = wired
    agent = HistoryRenderAgent(db=db, layout=layout)
    result = await agent.render(eid)
    assert result.written is False
    assert not result.path.exists()


async def test_unknown_entity_raises(wired):
    layout, db, _, _ = wired
    agent = HistoryRenderAgent(db=db, layout=layout)
    with pytest.raises(KeyError):
        await agent.render("entity.project.ghost")


async def test_overwrite_on_rerun(wired):
    layout, db, eid, src = wired
    await _add(db, eid=eid, src_id=src, valid_at="2026-05-22", value="completed")
    agent = HistoryRenderAgent(db=db, layout=layout)
    first = await agent.render(eid)
    first_body = first.path.read_text(encoding="utf-8")
    # Add another fact and re-render
    await _add(db, eid=eid, src_id=src, valid_at="2026-06-01", value="archived")
    second = await agent.render(eid)
    second_body = second.path.read_text(encoding="utf-8")
    assert second.total_facts == 2
    assert "archived" in second_body
    assert second_body != first_body  # confirmed it was rewritten
