# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for synthadoc.kb.delete_source — per-source revert of the fact tier."""

from __future__ import annotations

import pytest

from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.delete_source import delete_source
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.rules import FactRule, Rules


_RULES = Rules(by_fact_type={
    "project.status": FactRule(
        fact_type="project.status",
        strategy="latest_valid_at_wins",
        minimum_confidence="medium",
        exclude_review_status=("rejected",),
    ),
})

EID = ids.entity_id("project", "doc-intel")


async def _setup(tmp_path) -> tuple[KBDB, KBLayout]:
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()
    await db.upsert_entity(
        id=EID, entity_type="project", name="Doc Intel", slug="doc-intel",
        path="kb/entities/projects/doc-intel/index.md",
    )
    return db, layout


async def _add_source(db, layout, *, date: str, slug: str, sha: str) -> str:
    src_id = ids.source_id("meeting_transcript", date, slug)
    await db.insert_source(
        id=src_id, source_type="meeting_transcript", title=slug,
        authority="informal",
        raw_path=f"kb/sources/raw/meetings/{date}-{slug}.md",
        parsed_path=f"kb/sources/parsed/meetings/{date}-{slug}.md",
        created_at=f"{date}T10:00:00Z", ingested_at=f"{date}T11:00:00Z",
        sha256=sha,
    )
    return src_id


async def _add_fact(db, layout, *, src_id: str, valid_at: str, value: str) -> str:
    fid = ids.fact_id(EID, "project.status", valid_at)
    rel = f"kb/facts/projects/doc-intel/project.status/{valid_at}-{value}.md"
    p = layout.root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# {value}\n", encoding="utf-8")
    await db.insert_fact(
        id=fid, entity_id=EID, fact_type="project.status",
        value=value, valid_at=valid_at, observed_at=f"{valid_at}T09:00:00Z",
        source_id=src_id, source_path="p", source_quote="q",
        confidence="high", path=rel,
    )
    return fid


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------


async def test_deletes_only_the_targeted_sources_facts(tmp_path):
    db, layout = await _setup(tmp_path)
    a = await _add_source(db, layout, date="2026-05-01", slug="a", sha="ha")
    b = await _add_source(db, layout, date="2026-05-22", slug="b", sha="hb")
    fa = await _add_fact(db, layout, src_id=a, valid_at="2026-05-01", value="in_progress")
    fb = await _add_fact(db, layout, src_id=b, valid_at="2026-05-22", value="completed")

    res = await delete_source(db, layout, _RULES, a)

    assert res.facts_deleted == 1
    assert res.sha256 == "ha"
    assert await db.get_fact(fa) is None          # A's fact gone
    assert await db.get_fact(fb) is not None       # B's fact survives
    assert await db.get_source(a) is None          # source row gone
    assert await db.get_source(b) is not None
    # A's fact markdown removed, B's remains
    assert res.files_deleted >= 1


async def test_unsupersedes_surviving_fact(tmp_path):
    """Deleting the newer source must clear the older fact's superseded flag."""
    db, layout = await _setup(tmp_path)
    a = await _add_source(db, layout, date="2026-05-01", slug="a", sha="ha")
    b = await _add_source(db, layout, date="2026-05-22", slug="b", sha="hb")
    fa = await _add_fact(db, layout, src_id=a, valid_at="2026-05-01", value="in_progress")
    fb = await _add_fact(db, layout, src_id=b, valid_at="2026-05-22", value="completed")

    # Resolve so the older fact (fa) is marked superseded by the newer (fb).
    from synthadoc.kb.resolver import resolve_db
    await resolve_db(db, _RULES)
    assert (await db.get_fact(fa))["superseded_by"] == fb

    # Now revert the NEWER source. fa is the only fact left and must be active.
    await delete_source(db, layout, _RULES, b)

    survivor = await db.get_fact(fa)
    assert survivor is not None
    assert survivor["superseded_by"] is None
    rows = await db.fetchall("SELECT * FROM fact_supersession")
    assert rows == []


async def test_orphan_entity_is_removed(tmp_path):
    """An entity with no facts left after the revert is deleted, page and all."""
    db, layout = await _setup(tmp_path)
    a = await _add_source(db, layout, date="2026-05-01", slug="a", sha="ha")
    await _add_fact(db, layout, src_id=a, valid_at="2026-05-01", value="in_progress")

    # Render the entity page first so there is a file to clean up.
    from synthadoc.agents.entity_render_agent import EntityRenderAgent
    await EntityRenderAgent(db=db, layout=layout, rules=_RULES).render(EID)
    idx = layout.entity_index_path("project", "doc-intel")
    assert idx.exists()

    res = await delete_source(db, layout, _RULES, a)

    assert res.entities_deleted == 1
    assert await db.get_entity(EID) is None
    assert not idx.exists()


async def test_surviving_entity_is_rerendered_not_deleted(tmp_path):
    db, layout = await _setup(tmp_path)
    a = await _add_source(db, layout, date="2026-05-01", slug="a", sha="ha")
    b = await _add_source(db, layout, date="2026-05-22", slug="b", sha="hb")
    await _add_fact(db, layout, src_id=a, valid_at="2026-05-01", value="in_progress")
    await _add_fact(db, layout, src_id=b, valid_at="2026-05-22", value="completed")

    res = await delete_source(db, layout, _RULES, a)

    assert res.entities_deleted == 0
    assert res.entities_rerendered == 1
    assert await db.get_entity(EID) is not None
    idx = layout.entity_index_path("project", "doc-intel")
    assert idx.exists()
    # Current state should now reflect the surviving fact only.
    assert "completed" in idx.read_text(encoding="utf-8")


async def test_unknown_source_raises(tmp_path):
    db, layout = await _setup(tmp_path)
    with pytest.raises(KeyError):
        await delete_source(db, layout, _RULES, "source.meeting.2026-01-01.nope")


async def test_reopens_unknown_resolved_by_deleted_fact(tmp_path):
    db, layout = await _setup(tmp_path)
    a = await _add_source(db, layout, date="2026-05-01", slug="a", sha="ha")
    fa = await _add_fact(db, layout, src_id=a, valid_at="2026-05-01", value="in_progress")
    # Keep the entity alive with a second fact from another source.
    b = await _add_source(db, layout, date="2026-05-22", slug="b", sha="hb")
    await _add_fact(db, layout, src_id=b, valid_at="2026-05-22", value="completed")

    uid = ids.unknown_id(EID, "will-it-ship")
    await db.insert_unknown(
        id=uid, created_at="2026-05-01T09:00:00Z",
        path="kb/unknowns/projects/doc-intel/will-it-ship.md", entity_id=EID,
    )
    await db.execute(
        "UPDATE unknowns SET status='resolved', resolved_by_fact_id=? WHERE id=?",
        (fa, uid),
    )

    res = await delete_source(db, layout, _RULES, a)

    assert res.unknowns_reopened == 1
    row = await db.get_unknown(uid)
    assert row["status"] == "open"
    assert row["resolved_by_fact_id"] is None
