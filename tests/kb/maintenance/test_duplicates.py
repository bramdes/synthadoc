# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for synthadoc.kb.maintenance.duplicates."""

from __future__ import annotations

import pytest

from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.maintenance import duplicates as D


@pytest.fixture
async def wired(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()
    return layout, db


async def _add(db, *, entity_type, slug, name):
    eid = ids.entity_id(entity_type, slug)
    await db.upsert_entity(
        id=eid, entity_type=entity_type, name=name, slug=slug,
        path=f"kb/entities/{entity_type}s/{slug}/index.md",
    )
    return eid


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


async def test_empty_db_returns_no_candidates(wired):
    layout, db = wired
    result = await D.run(db, layout)
    assert result.count == 0
    text = result.report_path.read_text(encoding="utf-8")
    assert "_No duplicate candidates._" in text


async def test_single_entity_returns_no_candidates(wired):
    layout, db = wired
    await _add(db, entity_type="project", slug="alpha", name="Alpha")
    result = await D.run(db, layout)
    assert result.count == 0


async def test_different_types_are_not_duplicates(wired):
    """Same slug across types is not a conflict."""
    layout, db = wired
    await _add(db, entity_type="project", slug="alpha", name="Alpha")
    await _add(db, entity_type="person", slug="alpha", name="Alpha")
    result = await D.run(db, layout)
    assert result.count == 0


async def test_noise_token_match(wired):
    layout, db = wired
    await _add(db, entity_type="project", slug="alpha", name="Alpha")
    await _add(db, entity_type="project", slug="alpha-project", name="Alpha Project")
    result = await D.run(db, layout)
    assert result.count == 1
    assert "noise tokens" in result.candidates[0].reason


async def test_substring_match(wired):
    layout, db = wired
    await _add(db, entity_type="project", slug="doc-intel", name="Doc Intel")
    await _add(
        db, entity_type="project", slug="doc-intel-pilot", name="Doc Intel Pilot",
    )
    result = await D.run(db, layout)
    # 'doc-intel' is a substring of 'doc-intel-pilot' → substring match
    assert result.count >= 1
    assert any("substring" in c.reason for c in result.candidates)


async def test_levenshtein_typo_match(wired):
    layout, db = wired
    await _add(db, entity_type="topic", slug="parsing", name="Parsing")
    await _add(db, entity_type="topic", slug="parsng", name="Parsng")  # typo
    result = await D.run(db, layout)
    assert any("Levenshtein" in c.reason for c in result.candidates)


async def test_distant_names_are_not_duplicates(wired):
    layout, db = wired
    await _add(db, entity_type="project", slug="alpha", name="Alpha")
    await _add(db, entity_type="project", slug="omega", name="Omega")
    result = await D.run(db, layout)
    assert result.count == 0


async def test_report_lists_pairs_deterministically(wired):
    layout, db = wired
    await _add(db, entity_type="project", slug="alpha", name="Alpha")
    await _add(db, entity_type="project", slug="alpha-project", name="Alpha Project")
    r1 = await D.run(db, layout)
    r2 = await D.run(db, layout)
    assert r1.report_path.read_text(encoding="utf-8") == \
           r2.report_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Levenshtein helper unit tests
# ---------------------------------------------------------------------------


def test_levenshtein_basic():
    assert D._levenshtein("abc", "abc", cap=5) == 0
    assert D._levenshtein("abc", "abd", cap=5) == 1
    assert D._levenshtein("abc", "axyc", cap=5) == 2
    assert D._levenshtein("", "abc", cap=5) == 3


def test_levenshtein_caps_early():
    assert D._levenshtein("a" * 20, "b" * 20, cap=3) == 3
