# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for synthadoc.kb.frontmatter."""

from __future__ import annotations

import pytest

from synthadoc.kb import frontmatter as fm
from synthadoc.kb import ids


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def test_build_source_round_trip(tmp_path):
    src_id = ids.source_id("meeting_transcript", "2026-05-22", "doc-intel-update")
    data = fm.build_source(
        id=src_id,
        source_type="meeting_transcript",
        title="2026-05-22 Document Intelligence Update",
        authority="informal",
        raw_path="kb/sources/raw/meetings/x.md",
        parsed_path="kb/sources/parsed/meetings/x.md",
        created_at="2026-05-22T10:00:00+08:00",
        ingested_at="2026-05-23T09:00:00+08:00",
        sha256="abc123",
    )
    rendered = fm.dump(data, "Source body here.\n")
    parsed = fm.parse(rendered)
    assert parsed.id == src_id
    assert parsed.type == "source"
    assert parsed.body.strip() == "Source body here."


def test_build_fact_with_supersedes():
    eid = ids.entity_id("project", "doc-intel")
    fid = ids.fact_id(eid, "project.status", "2026-05-22")
    sid = ids.source_id("meeting_transcript", "2026-05-22", "x")
    data = fm.build_fact(
        id=fid,
        entity_id=eid,
        fact_type="project.status",
        value="completed",
        valid_at="2026-05-22",
        observed_at="2026-05-23T09:00:00+08:00",
        source_id=sid,
        source_path="kb/sources/parsed/meetings/x.md",
        source_quote="Drop 1 is now complete.",
        confidence="high",
        supersedes=["fact.project.doc-intel.project.status.2026-05-01"],
    )
    assert data["supersedes"] == ["fact.project.doc-intel.project.status.2026-05-01"]
    rendered = fm.dump(data, "")
    parsed = fm.parse(rendered)
    assert parsed.data["fact_type"] == "project.status"
    assert parsed.data["source_quote"] == "Drop 1 is now complete."


def test_build_entity_minimal():
    eid = ids.entity_id("project", "doc-intel")
    data = fm.build_entity(id=eid, entity_type="project")
    assert data["status"] == "active"
    assert data["current_state_review_status"] == "unreviewed"


def test_build_entity_rejects_bad_type():
    with pytest.raises(fm.FrontmatterError, match="entity_type"):
        fm.build_entity(id="entity.project.x", entity_type="device")


def test_build_decision_round_trip():
    sid = ids.source_id("meeting_transcript", "2026-05-22", "x")
    data = fm.build_decision(
        id=ids.decision_id("2026-05-22", "drop1-complete"),
        decision_date="2026-05-22",
        status="active",
        authority="informal",
        source_id=sid,
    )
    parsed = fm.parse(fm.dump(data, ""))
    assert parsed.type == "decision"


def test_build_unknown_minimal():
    data = fm.build_unknown(
        id=ids.unknown_id(ids.entity_id("project", "x"), "production-rollout"),
        created_at="2026-05-23",
    )
    assert data["status"] == "open"


def test_build_history():
    eid = ids.entity_id("project", "x")
    data = fm.build_history(
        id=ids.history_id(eid),
        entity_id=eid,
        last_rebuilt="2026-05-23T09:30:00+08:00",
    )
    assert data["type"] == "history"


# ---------------------------------------------------------------------------
# Parse / validate
# ---------------------------------------------------------------------------


def test_parse_missing_fence_raises():
    with pytest.raises(fm.FrontmatterError, match="leading"):
        fm.parse("no frontmatter here\n")


def test_parse_missing_closing_fence_raises():
    with pytest.raises(fm.FrontmatterError, match="closing"):
        fm.parse("---\nid: source.meeting.2026-05-22.x\ntype: source\n")


def test_parse_rejects_unknown_type():
    bad = "---\nid: page.foo\ntype: bogus\n---\nbody\n"
    with pytest.raises(fm.FrontmatterError, match="type"):
        fm.parse(bad)


def test_parse_rejects_missing_required_field():
    # type=fact requires many fields; omit them all
    bad = "---\nid: fact.project.x.project.status.2026-05-22\ntype: fact\n---\n"
    with pytest.raises(fm.FrontmatterError, match="missing required"):
        fm.parse(bad)


def test_parse_rejects_invalid_id():
    bad = "---\nid: not-a-kb-id\ntype: entity\nentity_type: project\nstatus: active\ncurrent_state_review_status: unreviewed\n---\n"
    with pytest.raises(fm.FrontmatterError, match="invalid id"):
        fm.parse(bad)


def test_read_and_write_round_trip(tmp_path):
    eid = ids.entity_id("project", "doc-intel")
    data = fm.build_entity(id=eid, entity_type="project")
    target = tmp_path / "entity.md"
    fm.write(target, data, "# Document Intelligence\n\nBody.\n")
    parsed = fm.read(target)
    assert parsed.id == eid
    assert "# Document Intelligence" in parsed.body


def test_source_summary_requires_source_prefixed_source_id():
    sid = ids.source_id("meeting_transcript", "2026-05-22", "x")
    good = fm.build_source_summary(
        id=ids.summary_source_id(sid),
        source_id=sid,
    )
    assert good["source_id"] == sid

    # Manually construct a broken one and try to parse — should fail validation
    raw = (
        "---\n"
        "id: summary.source.meeting.2026-05-22.x\n"
        "type: source_summary\n"
        "source_id: not-a-source-prefix\n"
        "review_status: unreviewed\n"
        "confidence: medium\n"
        "---\n"
    )
    with pytest.raises(fm.FrontmatterError, match="source_id"):
        fm.parse(raw)


def test_extra_fields_round_trip():
    eid = ids.entity_id("project", "x")
    data = fm.build_entity(id=eid, entity_type="project",
                           extra={"custom_field": "custom_value"})
    parsed = fm.parse(fm.dump(data, ""))
    assert parsed.data["custom_field"] == "custom_value"
