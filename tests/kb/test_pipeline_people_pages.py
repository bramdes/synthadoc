# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""people_pages corpus (spec §8.1, plan §10).

Creates a small set of synthetic people pages — one clean, one bristling
with every spec §3.7 violation category — and asserts the maintenance
aggregator's people-safety detector catches every violation while
leaving the clean page alone.

The kb_health.md snapshot must mark the file count in
``people_pages_with_policy_flags`` as FAIL when violations are present.
"""

from __future__ import annotations

import pytest

from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.maintenance.report import run_all


# A profile that should pass — only spec §3.7 ALLOWED content
_CLEAN = (
    "# Pravin\n\n"
    "Role: tech lead on Document Intelligence project.\n"
    "Responsibilities: production readiness sign-off, sprint planning.\n"
    "Open actions: confirm test evidence cleanup; review Drop 1 acceptance.\n"
    "Decisions made: 2026-05-22 — accepted Drop 1 completion.\n"
)

# A profile that violates every category in §3.7
_DIRTY = (
    "# Subject\n\n"
    "He seems to be a difficult character and is honestly lazy.\n"  # personality + emotional
    "His wife works at a competing firm, and they have young children at home.\n"  # private
    "An attractive and tall person — quite young for the role.\n"  # irrelevant
    "I heard that he's leaving next quarter — apparently behind her back.\n"  # gossip
)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def corpus(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()

    def write(slug: str, body: str) -> None:
        path = layout.entity_index_path("person", slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            f"id: entity.person.{slug}\n"
            "type: entity\n"
            "entity_type: person\n"
            "status: active\n"
            "current_state_review_status: unreviewed\n"
            "---\n\n"
            + body,
            encoding="utf-8",
        )

    write("pravin", _CLEAN)
    write("subject", _DIRTY)
    return layout, db


# ---------------------------------------------------------------------------
# End-to-end via run_all
# ---------------------------------------------------------------------------


async def test_run_all_flags_dirty_people_page(corpus):
    layout, db = corpus
    report = await run_all(db, layout)
    assert report.counts["people_pages_with_policy_flags"] >= 1


async def test_run_all_does_not_flag_clean_people_page(corpus):
    """The flagged-file count is unique paths, not unique flags. Clean → no entry."""
    layout, db = corpus
    report = await run_all(db, layout)
    # Read the people_pages_flags.md report directly to confirm `pravin` is absent
    text = (layout.maintenance_dir / "people_pages_flags.md").read_text(encoding="utf-8")
    assert "people/subject/" in text
    assert "people/pravin/" not in text


async def test_run_all_health_snapshot_marks_people_flags_as_fail(corpus):
    """Threshold for people_pages_with_policy_flags is 0 → any flag = FAIL."""
    layout, db = corpus
    report = await run_all(db, layout)
    text = report.health_path.read_text(encoding="utf-8")
    fail_lines = [
        line for line in text.splitlines()
        if line.startswith("|") and "people_pages_with_policy_flags" in line
    ]
    assert fail_lines
    assert "FAIL" in fail_lines[0]


async def test_detector_covers_every_spec_3_7_category(corpus):
    """The dirty profile mixes all five categories — every category should appear."""
    from synthadoc.kb.maintenance import people_safety as PS
    layout, db = corpus
    result = await PS.run(db, layout)
    categories = {f.category for f in result.flags}
    assert categories == {
        "personality speculation",
        "emotional judgement",
        "private detail",
        "irrelevant attribute",
        "gossip",
    }
