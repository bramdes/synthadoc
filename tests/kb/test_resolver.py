# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for synthadoc.kb.resolver."""

from __future__ import annotations

import pytest

from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb import resolver as R
from synthadoc.kb.rules import FactRule, Rules


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fact(
    *,
    eid: str,
    fact_type: str,
    valid_at: str,
    value: str,
    observed_at: str = "2026-05-23T09:00:00Z",
    confidence: str = "high",
    review_status: str = "unreviewed",
    superseded_by: str | None = None,
) -> dict:
    fid = ids.fact_id(eid, fact_type, valid_at)
    return dict(
        id=fid, entity_id=eid, fact_type=fact_type,
        value=value, valid_at=valid_at, observed_at=observed_at,
        confidence=confidence, review_status=review_status,
        superseded_by=superseded_by,
    )


_RULES = R.Rules(by_fact_type={
    "project.status": FactRule(
        fact_type="project.status",
        strategy="latest_valid_at_wins",
        minimum_confidence="medium",
        exclude_review_status=("rejected",),
    ),
    "project.milestone": FactRule(
        fact_type="project.milestone",
        strategy="append_only",
    ),
    "project.scope": FactRule(
        fact_type="project.scope",
        strategy="requires_review",
    ),
    "open_issue.created": FactRule(
        fact_type="open_issue.created",
        strategy="open_until_closure_fact",
    ),
})


EID = ids.entity_id("project", "doc-intel")


# ---------------------------------------------------------------------------
# latest_valid_at_wins
# ---------------------------------------------------------------------------


def test_latest_wins_picks_newest_valid_at():
    facts = [
        _fact(eid=EID, fact_type="project.status",
              valid_at="2026-05-01", value="in_progress"),
        _fact(eid=EID, fact_type="project.status",
              valid_at="2026-05-22", value="completed"),
    ]
    states, writes = R.resolve_facts(facts, _RULES)
    state = states[EID]
    assert state.current["project.status"].value == "completed"
    assert state.current["project.status"].valid_at == "2026-05-22"
    # The older fact must be queued for supersession
    old_id = ids.fact_id(EID, "project.status", "2026-05-01")
    new_id = ids.fact_id(EID, "project.status", "2026-05-22")
    assert (old_id, new_id) in writes


def test_latest_wins_ignores_already_superseded_writes():
    new_id = ids.fact_id(EID, "project.status", "2026-05-22")
    facts = [
        # Pre-marked: superseded_by already set to the new id
        _fact(eid=EID, fact_type="project.status",
              valid_at="2026-05-01", value="in_progress",
              superseded_by=new_id),
        _fact(eid=EID, fact_type="project.status",
              valid_at="2026-05-22", value="completed"),
    ]
    states, writes = R.resolve_facts(facts, _RULES)
    # Winner picked, but no new supersession write emitted
    assert states[EID].current["project.status"].value == "completed"
    assert writes == []


def test_latest_wins_skips_low_confidence_when_minimum_medium():
    facts = [
        _fact(eid=EID, fact_type="project.status",
              valid_at="2026-05-22", value="completed", confidence="low"),
        _fact(eid=EID, fact_type="project.status",
              valid_at="2026-05-01", value="in_progress", confidence="high"),
    ]
    states, _ = R.resolve_facts(facts, _RULES)
    # Newer fact filtered out by minimum_confidence; older one wins instead.
    assert states[EID].current["project.status"].value == "in_progress"


def test_latest_wins_skips_rejected():
    facts = [
        _fact(eid=EID, fact_type="project.status",
              valid_at="2026-05-22", value="completed",
              review_status="rejected"),
        _fact(eid=EID, fact_type="project.status",
              valid_at="2026-05-01", value="in_progress"),
    ]
    states, _ = R.resolve_facts(facts, _RULES)
    assert states[EID].current["project.status"].value == "in_progress"


def test_latest_wins_no_eligible_facts_yields_no_current():
    facts = [
        _fact(eid=EID, fact_type="project.status",
              valid_at="2026-05-22", value="completed", confidence="low"),
    ]
    states, _ = R.resolve_facts(facts, _RULES)
    # state created but project.status not in current
    assert "project.status" not in states[EID].current


def test_latest_wins_ties_broken_by_id():
    """Same valid_at + same observed_at → deterministic by id."""
    facts = [
        _fact(eid=EID, fact_type="project.status",
              valid_at="2026-05-22", value="a", observed_at="2026-05-23T09:00:00Z"),
        # Same date, same observed_at, different value
        dict(
            id="fact.project.doc-intel.project.status.2026-05-22-2",
            entity_id=EID, fact_type="project.status",
            value="b", valid_at="2026-05-22",
            observed_at="2026-05-23T09:00:00Z",
            confidence="high", review_status="unreviewed",
            superseded_by=None,
        ),
    ]
    states, _ = R.resolve_facts(facts, _RULES)
    # The id-sort tiebreak picks the lexicographically larger id (we sort reverse=True)
    assert states[EID].current["project.status"].value == "b"


# ---------------------------------------------------------------------------
# append_only
# ---------------------------------------------------------------------------


def test_append_only_keeps_every_fact():
    facts = [
        _fact(eid=EID, fact_type="project.milestone",
              valid_at="2026-04-01", value="kickoff"),
        _fact(eid=EID, fact_type="project.milestone",
              valid_at="2026-05-22", value="drop-1"),
    ]
    states, writes = R.resolve_facts(facts, _RULES)
    series = states[EID].appended["project.milestone"]
    assert len(series.fact_ids) == 2
    assert writes == []  # never supersedes


# ---------------------------------------------------------------------------
# requires_review
# ---------------------------------------------------------------------------


def test_requires_review_does_not_set_current():
    facts = [
        _fact(eid=EID, fact_type="project.scope",
              valid_at="2026-05-22", value="must include drop-2"),
    ]
    states, _ = R.resolve_facts(facts, _RULES)
    assert "project.scope" not in states[EID].current
    assert "project.scope" in states[EID].pending


# ---------------------------------------------------------------------------
# open_until_*
# ---------------------------------------------------------------------------


def test_open_until_closure_treated_as_append_for_v03():
    facts = [
        _fact(eid=EID, fact_type="open_issue.created",
              valid_at="2026-05-22", value="test-evidence-cleanup"),
    ]
    states, _ = R.resolve_facts(facts, _RULES)
    assert "open_issue.created" in states[EID].appended


# ---------------------------------------------------------------------------
# skip_unknown_types
# ---------------------------------------------------------------------------


def test_unknown_fact_type_skipped_by_default():
    facts = [
        _fact(eid=EID, fact_type="topic.definition",
              valid_at="2026-05-22", value="x"),
    ]
    states, writes = R.resolve_facts(facts, _RULES)
    assert states == {}
    assert writes == []


def test_unknown_fact_type_raises_when_disabled():
    facts = [
        # Skip the rule lookup but force it via require
        _fact(eid=EID, fact_type="topic.definition",
              valid_at="2026-05-22", value="x"),
    ]
    # When skip_unknown_types=False, the resolver still groups everything and
    # then calls rules.require() which raises.
    with pytest.raises(Exception):
        R.resolve_facts(facts, _RULES, skip_unknown_types=False)


# ---------------------------------------------------------------------------
# DB integration
# ---------------------------------------------------------------------------


async def test_resolve_db_writes_supersession(tmp_path):
    db = KBDB(tmp_path / "kb.db")
    await db.init()

    src_id = ids.source_id("meeting_transcript", "2026-05-22", "x")
    await db.insert_source(
        id=src_id, source_type="meeting_transcript", title="x",
        authority="informal", raw_path="r", parsed_path="p",
        created_at="2026-05-22T10:00:00Z",
        ingested_at="2026-05-23T09:00:00Z",
        sha256="h1",
    )
    await db.upsert_entity(
        id=EID, entity_type="project", name="Doc Intel", slug="doc-intel",
        path="kb/entities/projects/doc-intel/index.md",
    )

    async def _add(valid_at, value):
        fid = ids.fact_id(EID, "project.status", valid_at)
        await db.insert_fact(
            id=fid, entity_id=EID, fact_type="project.status",
            value=value, valid_at=valid_at,
            observed_at="2026-05-23T09:00:00Z",
            source_id=src_id, source_path="p", source_quote="q",
            confidence="high",
            path=f"kb/facts/projects/doc-intel/project.status/{valid_at}-{value}.md",
        )

    await _add("2026-05-01", "in_progress")
    await _add("2026-05-22", "completed")

    states = await R.resolve_db(db, _RULES)
    assert states[EID].current["project.status"].value == "completed"

    # Old fact now superseded in the DB
    old_id = ids.fact_id(EID, "project.status", "2026-05-01")
    new_id = ids.fact_id(EID, "project.status", "2026-05-22")
    old_row = await db.get_fact(old_id)
    assert old_row["superseded_by"] == new_id

    # Second run is a no-op
    await R.resolve_db(db, _RULES)
    rows = await db.fetchall("SELECT * FROM fact_supersession")
    assert len(rows) == 1
