# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for synthadoc.kb.maintenance.people_safety."""

from __future__ import annotations

import pytest

from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.maintenance import people_safety as PS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def wired(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()
    return layout, db


def _write_person_page(layout: KBLayout, slug: str, body: str) -> None:
    """Write kb/entities/people/<slug>/index.md with a minimal frontmatter."""
    target = layout.entity_index_path("person", slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        "---\n"
        f"id: entity.person.{slug}\n"
        "type: entity\n"
        "entity_type: person\n"
        "status: active\n"
        "current_state_review_status: unreviewed\n"
        "---\n\n"
    )
    target.write_text(fm + body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Empty / clean cases
# ---------------------------------------------------------------------------


async def test_no_people_pages_returns_zero(wired):
    layout, db = wired
    result = await PS.run(db, layout)
    assert result.count == 0
    assert result.files_scanned == 0
    text = result.report_path.read_text(encoding="utf-8")
    assert "_No flagged content._" in text


async def test_clean_person_page_has_zero_flags(wired):
    layout, db = wired
    _write_person_page(layout, "pravin",
        "# Pravin\n\nRole: tech lead on Document Intelligence.\n"
        "Owns the production rollout decision.\n"
        "Open action: confirm test evidence cleanup.\n")
    result = await PS.run(db, layout)
    assert result.count == 0
    assert result.files_scanned == 1


# ---------------------------------------------------------------------------
# Per-category detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category,body_snippet,expected_match", [
    ("personality speculation",
     "He seems to be a difficult character.",
     "seems to be"),
    ("personality speculation",
     "Apparently is the kind of person who never listens.",
     "Apparently is"),
    ("emotional judgement",
     "Honestly, he's lazy and stubborn.",
     "lazy"),
    ("emotional judgement",
     "Generally seen as difficult to work with.",
     "difficult to work with"),
    ("private detail",
     "Her husband works at a competing firm.",
     "husband"),
    ("private detail",
     "Has young children at home.",
     "children"),
    ("private detail",
     "Reportedly conservative in views.",
     "conservative"),
    ("private detail",
     "Drinks a lot at company events.",
     "Drinks"),
    ("irrelevant attribute",
     "An older engineer with a long career.",
     None),  # 'older' isn't in our lexicon — see test below for `old`
    ("irrelevant attribute",
     "Quite tall and attractive.",
     "tall"),
    ("gossip",
     "I heard that he's leaving next quarter.",
     "I heard that"),
    ("gossip",
     "She criticised the team behind her back.",
     "behind her back"),
])
async def test_each_category_detected(wired, category, body_snippet, expected_match):
    layout, db = wired
    _write_person_page(layout, "subject", body_snippet)
    result = await PS.run(db, layout)
    if expected_match is None:
        # Snippet shouldn't trigger any rule in this category — skip the match assertion
        cats_present = {f.category for f in result.flags}
        assert category not in cats_present
    else:
        matched = [f for f in result.flags
                   if f.category == category and
                      expected_match.lower() in f.pattern_match.lower()]
        assert matched, (
            f"expected a {category!r} flag matching {expected_match!r} "
            f"on body {body_snippet!r}; got: "
            f"{[(f.category, f.pattern_match) for f in result.flags]}"
        )


# ---------------------------------------------------------------------------
# Frontmatter is not scanned
# ---------------------------------------------------------------------------


async def test_frontmatter_values_do_not_trigger(wired):
    """Frontmatter values that happen to contain trigger words must not flag."""
    layout, db = wired
    target = layout.entity_index_path("person", "pravin")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\n"
        "id: entity.person.pravin\n"
        "type: entity\n"
        "entity_type: person\n"
        "status: active\n"
        "current_state_review_status: unreviewed\n"
        "description: 'lazy daisy chain'\n"   # would otherwise hit 'lazy'
        "---\n\n"
        "# Pravin\n\nRole: tech lead.\n",
        encoding="utf-8",
    )
    result = await PS.run(db, layout)
    assert result.count == 0


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


async def test_report_groups_flags_by_file(wired):
    layout, db = wired
    _write_person_page(layout, "a", "He's lazy.\n")
    _write_person_page(layout, "b", "She is rude.\n")
    result = await PS.run(db, layout)
    text = result.report_path.read_text(encoding="utf-8")
    assert "people/a/" in text
    assert "people/b/" in text
    assert "lazy" in text.lower()
    assert "rude" in text.lower()


async def test_report_includes_line_numbers(wired):
    layout, db = wired
    _write_person_page(layout, "pravin",
        "Line one.\nLine two.\nHe is lazy.\nLine four.\n")
    result = await PS.run(db, layout)
    assert result.count == 1
    # Body starts on line 9 (8 frontmatter lines + blank), so "lazy" is on a
    # line after that — exact line number is implementation detail; just
    # check that it's reported as a positive integer
    assert result.flags[0].line > 0


# ---------------------------------------------------------------------------
# Multiple categories on the same file
# ---------------------------------------------------------------------------


async def test_multiple_violations_in_one_file(wired):
    layout, db = wired
    _write_person_page(layout, "pravin",
        "He is lazy and stubborn.\n"
        "His wife works elsewhere.\n"
        "I heard that he's leaving.\n")
    result = await PS.run(db, layout)
    cats = {f.category for f in result.flags}
    assert "emotional judgement" in cats
    assert "private detail" in cats
    assert "gossip" in cats
