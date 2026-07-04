# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for synthadoc.kb.ids."""

from __future__ import annotations

from datetime import date

import pytest

from synthadoc.kb import ids


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_basic(self):
        assert ids.slugify("Document Intelligence") == "document-intelligence"

    def test_collapses_separators(self):
        assert ids.slugify("foo / bar  -- baz") == "foo-bar-baz"

    def test_strips_edges(self):
        assert ids.slugify("--hello-world--") == "hello-world"

    def test_accents(self):
        assert ids.slugify("Café Münster") == "cafe-munster"

    def test_caps_length(self):
        out = ids.slugify("x" * 200)
        assert len(out) <= 64

    def test_fallback_when_no_slug_chars(self):
        out = ids.slugify("!!!")
        assert out.startswith("x-")
        # Same input must yield the same fallback (deterministic).
        assert out == ids.slugify("!!!")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            ids.slugify("")

    def test_idempotent_on_valid_slugs(self):
        for s in ["foo", "foo-bar", "drop-1-complete"]:
            assert ids.slugify(s) == s


# ---------------------------------------------------------------------------
# source_id
# ---------------------------------------------------------------------------


class TestSourceID:
    def test_basic(self):
        assert ids.source_id(
            "meeting_transcript", "2026-05-22", "doc-intel-update"
        ) == "source.meeting.2026-05-22.doc-intel-update"

    def test_accepts_date_object(self):
        assert ids.source_id(
            "document", date(2026, 5, 1), "brd-v1"
        ) == "source.document.2026-05-01.brd-v1"

    def test_rejects_unknown_source_type(self):
        with pytest.raises(ValueError, match="source_type"):
            ids.source_id("video", "2026-05-22", "x")

    def test_rejects_bad_date(self):
        with pytest.raises(ValueError, match="valid_on"):
            ids.source_id("meeting_transcript", "2026/05/22", "x")
        with pytest.raises(ValueError, match="valid_on"):
            ids.source_id("meeting_transcript", "26-05-22", "x")

    def test_rejects_unnormalised_slug(self):
        with pytest.raises(ValueError, match="slug"):
            ids.source_id("meeting_transcript", "2026-05-22", "Doc Intel")

    def test_split_round_trip(self):
        sid = ids.source_id("email", "2026-05-10", "scope-clarification")
        kind, iso, slug = ids.split_source_id(sid)
        assert (kind, iso, slug) == ("email", "2026-05-10", "scope-clarification")


# ---------------------------------------------------------------------------
# summary_source_id
# ---------------------------------------------------------------------------


def test_summary_source_id_prefixes_summary():
    sid = ids.source_id("meeting_transcript", "2026-05-22", "x")
    assert ids.summary_source_id(sid) == f"summary.{sid}"


def test_summary_source_id_requires_source_prefix():
    with pytest.raises(ValueError):
        ids.summary_source_id("not-a-source")


# ---------------------------------------------------------------------------
# entity_id
# ---------------------------------------------------------------------------


class TestEntityID:
    def test_basic(self):
        assert ids.entity_id("project", "document-intelligence") == \
            "entity.project.document-intelligence"

    def test_rejects_unknown_entity_type(self):
        with pytest.raises(ValueError, match="entity_type"):
            ids.entity_id("device", "x")

    def test_rejects_unnormalised_slug(self):
        with pytest.raises(ValueError, match="slug"):
            ids.entity_id("project", "Document Intelligence")


# ---------------------------------------------------------------------------
# fact_id
# ---------------------------------------------------------------------------


class TestFactID:
    def test_basic(self):
        eid = ids.entity_id("project", "document-intelligence")
        out = ids.fact_id(eid, "project.status", "2026-05-22")
        assert out == "fact.project.document-intelligence.project.status.2026-05-22"

    def test_rejects_unknown_fact_type(self):
        eid = ids.entity_id("project", "x")
        with pytest.raises(ValueError, match="fact_type"):
            ids.fact_id(eid, "project.colour", "2026-05-22")

    def test_rejects_bad_entity_id(self):
        with pytest.raises(ValueError):
            ids.fact_id("not-an-entity", "project.status", "2026-05-22")


# ---------------------------------------------------------------------------
# conclusion_id
# ---------------------------------------------------------------------------


class TestConclusionID:
    def test_basic(self):
        eid = ids.entity_id("project", "x")
        out = ids.conclusion_id(eid, "production_readiness", "2026-05-23")
        assert out == "conclusion.project.x.production_readiness.2026-05-23"

    def test_rejects_dotted_conclusion_type(self):
        eid = ids.entity_id("project", "x")
        with pytest.raises(ValueError, match="conclusion_type"):
            ids.conclusion_id(eid, "production.readiness", "2026-05-23")


# ---------------------------------------------------------------------------
# decision_id / unknown_id / history_id
# ---------------------------------------------------------------------------


def test_decision_id_basic():
    assert ids.decision_id("2026-05-22", "drop1-complete") == \
        "decision.2026-05-22.drop1-complete"


def test_unknown_id_basic():
    eid = ids.entity_id("project", "document-intelligence")
    assert ids.unknown_id(eid, "production-rollout") == \
        "unknown.project.document-intelligence.production-rollout"


def test_history_id_basic():
    eid = ids.entity_id("project", "document-intelligence")
    assert ids.history_id(eid) == "history.project.document-intelligence"


# ---------------------------------------------------------------------------
# with_collision_suffix
# ---------------------------------------------------------------------------


class TestCollisionSuffix:
    def test_returns_base_when_free(self):
        assert ids.with_collision_suffix("foo", lambda _: False) == "foo"

    def test_appends_2_when_base_taken(self):
        taken = {"foo"}
        assert ids.with_collision_suffix("foo", lambda x: x in taken) == "foo-2"

    def test_walks_until_free(self):
        taken = {"foo", "foo-2", "foo-3"}
        assert ids.with_collision_suffix("foo", lambda x: x in taken) == "foo-4"


class TestSlugWithSuffix:
    def test_short_slug_appends_directly(self):
        assert ids.slug_with_suffix("foo", 2) == "foo-2"

    def test_rejects_n_below_one(self):
        with pytest.raises(ValueError):
            ids.slug_with_suffix("foo", 0)

    def test_at_cap_slug_stays_valid(self):
        # A slug already at the 64-char cap must not overflow when suffixed —
        # the result has to remain idempotent under slugify (regression: the
        # kb-seed collision loop produced 66-char slugs that _check_slug then
        # rejected with "slug must already be a valid slug").
        base = ids.slugify("x" * 80)  # capped to 64
        assert len(base) == 64
        out = ids.slug_with_suffix(base, 2)
        assert len(out) <= 64
        assert out.endswith("-2")
        assert out == ids.slugify(out)

    def test_source_id_round_trips_after_suffix(self):
        base = ids.slugify(
            "2026-05-26-17-05-32-tsd-aml-data-architecture-egp-vs-edac-staging"
        )
        sid = ids.source_id("document", "2026-05-26", ids.slug_with_suffix(base, 2))
        # split_source_id runs _check_slug; must not raise.
        assert ids.split_source_id(sid)[2].endswith("-2")


# ---------------------------------------------------------------------------
# validators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("source.meeting.2026-05-22.x", True),
    ("entity.project.x", True),
    ("fact.project.x.project.status.2026-05-22", True),
    ("conclusion.project.x.foo.2026-05-22", True),
    ("decision.2026-05-22.x", True),
    ("unknown.project.x.foo", True),
    ("history.project.x", True),
    ("summary.source.meeting.2026-05-22.x", True),
    ("page.foo", False),
    ("", False),
    (None, False),
])
def test_is_valid_id(value, expected):
    assert ids.is_valid_id(value) is expected


def test_id_kind_returns_prefix():
    assert ids.id_kind("source.meeting.2026-05-22.x") == "source"
    assert ids.id_kind("entity.project.x") == "entity"
    assert ids.id_kind("summary.source.meeting.2026-05-22.x") == "summary"
    assert ids.id_kind("fact.project.x.project.status.2026-05-22") == "fact"


def test_id_kind_rejects_unknown():
    with pytest.raises(ValueError):
        ids.id_kind("page.foo")
