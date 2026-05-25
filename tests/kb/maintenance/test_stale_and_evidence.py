# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for stale.py + evidence.py — the SQL-only maintenance jobs."""

from __future__ import annotations

import pytest

from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.maintenance import evidence as EV
from synthadoc.kb.maintenance import stale as ST


# ---------------------------------------------------------------------------
# Common fixture
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
    return layout, db, src_id


async def _add_entity(db, slug):
    eid = ids.entity_id("project", slug)
    await db.upsert_entity(
        id=eid, entity_type="project", name=slug.title(),
        slug=slug, path=f"kb/entities/projects/{slug}/index.md",
    )
    return eid


async def _add_fact(db, *, eid, src_id, valid_at, value, fact_type="project.status",
                    observed_at="2026-05-24T09:00:00Z"):
    fid = ids.fact_id(eid, fact_type, valid_at)
    await db.insert_fact(
        id=fid, entity_id=eid, fact_type=fact_type,
        value=value, valid_at=valid_at, observed_at=observed_at,
        source_id=src_id, source_path="p",
        source_quote=f"value: {value}",
        confidence="high",
        path=f"kb/facts/projects/{eid.split('.')[-1]}/{fact_type}/{valid_at}-{value}.md",
    )
    return fid


# ---------------------------------------------------------------------------
# stale.py
# ---------------------------------------------------------------------------


async def test_stale_empty_when_no_facts(wired):
    layout, db, _ = wired
    result = await ST.run(db, layout)
    assert result.count == 0
    assert "_No stale entity pages._" in result.report_path.read_text(encoding="utf-8")


async def test_entity_with_never_rebuilt_and_facts_is_stale(wired):
    layout, db, src = wired
    eid = await _add_entity(db, "fresh")
    await _add_fact(db, eid=eid, src_id=src, valid_at="2026-05-22", value="completed")
    result = await ST.run(db, layout)
    assert result.count == 1
    assert result.stale[0].entity_id == eid
    assert result.stale[0].last_rebuilt is None


async def test_entity_rebuilt_before_newest_fact_is_stale(wired):
    layout, db, src = wired
    eid = await _add_entity(db, "drifting")
    # Stamp last_rebuilt earlier than the fact's observed_at
    await db.execute(
        "UPDATE entities SET last_rebuilt = ? WHERE id = ?",
        ("2026-05-23T00:00:00Z", eid),
    )
    await _add_fact(db, eid=eid, src_id=src, valid_at="2026-05-22",
                    value="completed", observed_at="2026-05-24T09:00:00Z")
    result = await ST.run(db, layout)
    assert result.count == 1


async def test_entity_rebuilt_after_newest_fact_is_fresh(wired):
    layout, db, src = wired
    eid = await _add_entity(db, "fresh")
    await _add_fact(db, eid=eid, src_id=src, valid_at="2026-05-22",
                    value="completed", observed_at="2026-05-24T09:00:00Z")
    await db.execute(
        "UPDATE entities SET last_rebuilt = ? WHERE id = ?",
        ("2026-05-25T00:00:00Z", eid),
    )
    result = await ST.run(db, layout)
    assert result.count == 0


# ---------------------------------------------------------------------------
# evidence.py — orphan facts
# ---------------------------------------------------------------------------


async def test_orphan_facts_empty_in_healthy_db(wired):
    layout, db, src = wired
    eid = await _add_entity(db, "x")
    await _add_fact(db, eid=eid, src_id=src, valid_at="2026-05-22", value="ok")
    result = await EV.run_orphan_facts(db, layout)
    assert result.count == 0


# (No direct test for the FK-violation case — SQLite's PRAGMA foreign_keys=ON
# makes inserting one impossible. The check is defence-in-depth for corrupted
# or migrated DBs.)


# ---------------------------------------------------------------------------
# evidence.py — facts without evidence
# ---------------------------------------------------------------------------


async def test_facts_without_evidence_empty_in_healthy_db(wired):
    layout, db, src = wired
    eid = await _add_entity(db, "x")
    await _add_fact(db, eid=eid, src_id=src, valid_at="2026-05-22", value="ok")
    result = await EV.run_facts_without_evidence(db, layout)
    assert result.count == 0


async def test_facts_with_empty_quote_flagged(wired):
    layout, db, src = wired
    eid = await _add_entity(db, "x")
    # Bypass the agent and insert a fact with an empty quote directly
    await _add_fact(db, eid=eid, src_id=src, valid_at="2026-05-22", value="ok")
    await db.execute(
        "UPDATE facts SET source_quote = '' WHERE entity_id = ?", (eid,),
    )
    result = await EV.run_facts_without_evidence(db, layout)
    assert result.count == 1


async def test_facts_with_missing_source_flagged(wired):
    """A fact pointing at a non-existent source_id."""
    layout, db, _ = wired
    eid = await _add_entity(db, "x")
    # Need to bypass the FK to simulate corruption — use a raw connection with FK off
    import aiosqlite
    async with aiosqlite.connect(layout.db_path) as raw:
        await raw.execute("PRAGMA foreign_keys = OFF")
        fid = ids.fact_id(eid, "project.status", "2026-05-22")
        await raw.execute(
            "INSERT INTO facts (id, entity_id, fact_type, value, valid_at, "
            "observed_at, source_id, source_path, source_quote, confidence, path)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (fid, eid, "project.status", "ok", "2026-05-22",
             "2026-05-23T09:00:00Z", "source.meeting.2026-05-22.ghost",
             "p", "non-empty quote", "high", "kb/facts/x.md"),
        )
        await raw.commit()
    result = await EV.run_facts_without_evidence(db, layout)
    assert result.count == 1


# ---------------------------------------------------------------------------
# evidence.py — conclusions without supporting facts
# ---------------------------------------------------------------------------


async def test_conclusions_without_facts_empty_when_db_empty(wired):
    layout, db, _ = wired
    result = await EV.run_conclusions_without_facts(db, layout)
    assert result.count == 0


async def test_conclusions_without_basis_flagged(wired):
    layout, db, _ = wired
    eid = await _add_entity(db, "x")
    # Insert a conclusion with no basis rows
    await db.execute(
        "INSERT INTO conclusions (id, entity_id, conclusion_type, value, "
        "confidence, path, generated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("conclusion.project.x.foo.2026-05-23", eid,
         "foo", "bar", "high", "kb/conclusions/x.md",
         "2026-05-23T09:00:00Z"),
    )
    result = await EV.run_conclusions_without_facts(db, layout)
    assert result.count == 1
    assert result.items[0]["id"] == "conclusion.project.x.foo.2026-05-23"
