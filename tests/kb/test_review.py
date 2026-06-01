# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for the human-review actions (synthadoc.kb.review)."""

from __future__ import annotations

import pytest

from synthadoc.kb import frontmatter as fm
from synthadoc.kb import ids, review
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.rules import FactRule, Rules


_RULES = Rules(by_fact_type={
    "project.status": FactRule(
        fact_type="project.status", strategy="latest_valid_at_wins",
        minimum_confidence="medium",
    ),
    "project.milestone": FactRule(
        fact_type="project.milestone", strategy="append_only",
    ),
})


@pytest.fixture
async def wired(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()

    eid = ids.entity_id("project", "doc-intel")
    await db.upsert_entity(
        id=eid, entity_type="project", name="Document Intelligence",
        slug="doc-intel", path="kb/entities/projects/doc-intel/index.md",
    )
    src_id = ids.source_id("meeting_transcript", "2026-05-22", "x")
    await db.insert_source(
        id=src_id, source_type="meeting_transcript", title="x",
        authority="informal", raw_path="r", parsed_path="p",
        created_at="2026-05-22T10:00:00Z", ingested_at="2026-05-23T09:00:00Z",
        sha256="h-1",
    )
    return layout, db, eid, src_id


async def _add_milestone(layout, db, *, eid, src_id, valid_at, value, suffix=""):
    """Insert an append_only milestone fact AND write its markdown file."""
    fid = ids.fact_id(eid, "project.milestone", valid_at) + suffix
    rel = f"kb/facts/projects/doc-intel/project.milestone/{valid_at}-{value}.md"
    await db.insert_fact(
        id=fid, entity_id=eid, fact_type="project.milestone", value=value,
        valid_at=valid_at, observed_at="2026-05-23T09:00:00Z",
        source_id=src_id, source_path="p", source_quote=f"Milestone {value}",
        confidence="high", path=rel,
    )
    data = fm.build_fact(
        id=fid, entity_id=eid, fact_type="project.milestone", value=value,
        valid_at=valid_at, observed_at="2026-05-23T09:00:00Z",
        source_id=src_id, source_path="p", source_quote=f"Milestone {value}",
        confidence="high",
    )
    fm.write(layout.root / rel, data, f"# Fact\n\n{value}\n")
    return fid, rel


# ---------------------------------------------------------------------------
# reject
# ---------------------------------------------------------------------------


async def test_reject_removes_fact_from_rendered_page(wired):
    """A rejected append_only fact disappears from the entity page — proving
    the resolver's global reject filter works even for append_only."""
    layout, db, eid, src_id = wired
    await _add_milestone(layout, db, eid=eid, src_id=src_id,
                         valid_at="2026-04-01", value="april")
    fid_july, _ = await _add_milestone(layout, db, eid=eid, src_id=src_id,
                                       valid_at="2026-05-22", value="july")

    result = await review.reject_fact(db, layout, _RULES, fid_july)

    assert result.action == "reject"
    assert result.entity_rendered == eid
    assert not result.warnings  # the fact markdown existed and was patched

    # DB updated
    row = await db.get_fact(fid_july)
    assert row["review_status"] == "rejected"

    # Markdown frontmatter patched (durable truth)
    page = layout.root / "kb/facts/projects/doc-intel/project.milestone/2026-05-22-july.md"
    assert fm.read(page).data["review_status"] == "rejected"

    # Rendered entity page keeps april, drops july
    body = (layout.root / "kb/entities/projects/doc-intel/index.md").read_text(encoding="utf-8")
    assert "april" in body
    assert "july" not in body


async def test_reject_unknown_fact_raises(wired):
    layout, db, _, _ = wired
    with pytest.raises(KeyError):
        await review.reject_fact(db, layout, _RULES, "fact.project.doc-intel.project.status.2026-01-01")


async def test_reject_missing_markdown_warns_but_succeeds(wired):
    """DB-only fact (no file on disk) still rejects; a warning is surfaced."""
    layout, db, eid, src_id = wired
    fid = ids.fact_id(eid, "project.milestone", "2026-05-22")
    await db.insert_fact(
        id=fid, entity_id=eid, fact_type="project.milestone", value="july",
        valid_at="2026-05-22", observed_at="2026-05-23T09:00:00Z",
        source_id=src_id, source_path="p", source_quote="q", confidence="high",
        path="kb/facts/projects/doc-intel/project.milestone/missing.md",
    )
    result = await review.reject_fact(db, layout, _RULES, fid)
    assert (await db.get_fact(fid))["review_status"] == "rejected"
    assert any("not found" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# accept / reopen entity
# ---------------------------------------------------------------------------


async def test_accept_locks_entity_so_changes_queue(wired):
    layout, db, eid, src_id = wired
    await _add_milestone(layout, db, eid=eid, src_id=src_id,
                         valid_at="2026-04-01", value="april")

    res = await review.set_entity_reviewed(db, layout, _RULES, eid, reviewed=True)
    assert res.action == "accept"

    # DB + page frontmatter both reviewed
    assert (await db.get_entity(eid))["current_state_review_status"] == "reviewed"
    page = layout.root / "kb/entities/projects/doc-intel/index.md"
    assert fm.read(page).data["current_state_review_status"] == "reviewed"

    # A new fact now queues a proposal instead of overwriting the page
    await _add_milestone(layout, db, eid=eid, src_id=src_id,
                         valid_at="2026-06-01", value="august")
    res2 = await review.reject_fact(db, layout, _RULES, ids.fact_id(eid, "project.milestone", "2026-06-01"))
    assert res2.queued_for_review is True


async def test_reopen_unlocks_entity(wired):
    layout, db, eid, src_id = wired
    await _add_milestone(layout, db, eid=eid, src_id=src_id,
                         valid_at="2026-04-01", value="april")
    await review.set_entity_reviewed(db, layout, _RULES, eid, reviewed=True)

    res = await review.set_entity_reviewed(db, layout, _RULES, eid, reviewed=False)
    assert res.action == "reopen"
    assert (await db.get_entity(eid))["current_state_review_status"] == "unreviewed"


async def test_accept_unknown_entity_raises(wired):
    layout, db, _, _ = wired
    with pytest.raises(KeyError):
        await review.set_entity_reviewed(db, layout, _RULES, "entity.project.ghost", reviewed=True)


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


async def test_merge_moves_facts_to_keeper_and_tombstones_dup(wired):
    layout, db, eid, src_id = wired  # eid is the keeper
    dup = ids.entity_id("project", "doc-intel-roadmap")
    await db.upsert_entity(
        id=dup, entity_type="project", name="Doc Intel Roadmap",
        slug="doc-intel-roadmap",
        path="kb/entities/projects/doc-intel-roadmap/index.md",
    )
    # A fact on the duplicate
    fid, _ = await _add_milestone(layout, db, eid=dup, src_id=src_id,
                                  valid_at="2026-05-22", value="merged-mlstn",
                                  suffix="")

    res = await review.merge_entity(db, layout, _RULES, dup, eid)
    assert res.action == "merge"
    assert res.entity_rendered == eid

    # Fact re-pointed to keeper
    assert (await db.get_fact(fid))["entity_id"] == eid
    # Dup marked merged
    drow = await db.get_entity(dup)
    assert drow["merged_into"] == eid
    assert drow["status"] == "merged"

    # Keeper page shows the moved fact
    keeper_page = (layout.root / "kb/entities/projects/doc-intel/index.md").read_text(encoding="utf-8")
    assert "merged-mlstn" in keeper_page

    # Dup page is a tombstone pointing at the keeper
    dup_page = (layout.root / "kb/entities/projects/doc-intel-roadmap/index.md").read_text(encoding="utf-8")
    assert "Merged into" in dup_page
    assert fm.read(layout.root / "kb/entities/projects/doc-intel-roadmap/index.md").data["merged_into"] == eid


async def test_merge_into_self_raises(wired):
    layout, db, eid, _ = wired
    with pytest.raises(ValueError):
        await review.merge_entity(db, layout, _RULES, eid, eid)


async def test_merge_unknown_dup_raises(wired):
    layout, db, eid, _ = wired
    with pytest.raises(KeyError):
        await review.merge_entity(db, layout, _RULES, "entity.project.ghost", eid)
