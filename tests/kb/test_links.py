# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for synthadoc.kb.links."""

from __future__ import annotations

import pytest

from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.links import (
    LINK_TYPE_WIKILINK,
    emit_links,
    extract_wikilinks,
    relink_all,
)


# ---------------------------------------------------------------------------
# Pure extractor
# ---------------------------------------------------------------------------


class TestExtractWikilinks:
    def test_basic(self):
        assert extract_wikilinks("see [[doc-intel]] please") == ["doc-intel"]

    def test_multiple(self):
        out = extract_wikilinks("[[a]] and [[b]] and [[a]]")
        assert out == ["a", "b", "a"]

    def test_with_alias(self):
        assert extract_wikilinks("[[doc-intel|Document Intelligence]]") == ["doc-intel"]

    def test_strips_whitespace(self):
        assert extract_wikilinks("[[ pravin ]]") == ["pravin"]

    def test_ignores_anchor_links(self):
        # `#` is excluded from the slug class so these won't match
        assert extract_wikilinks("[[#anchor]]") == []

    def test_empty_body(self):
        assert extract_wikilinks("") == []
        assert extract_wikilinks(None) == []

    def test_does_not_match_single_brackets(self):
        assert extract_wikilinks("a [link](url) and [thing]") == []


# ---------------------------------------------------------------------------
# emit_links
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    d = KBDB(tmp_path / "kb.db")
    await d.init()
    return d


async def test_emit_inserts_unique_targets(db):
    inserted = await emit_links(
        db, from_path="kb/entities/projects/x/index.md",
        body="[[a]] [[b]] [[a]] [[c]]",
    )
    # Three unique targets — duplicates collapsed
    assert inserted == 3
    rows = await db.fetchall("SELECT * FROM links")
    assert {r["to_path"] for r in rows} == {"a", "b", "c"}
    assert all(r["link_type"] == LINK_TYPE_WIKILINK for r in rows)


async def test_emit_replaces_prior_links_for_same_path(db):
    path = "kb/entities/projects/x/index.md"
    await emit_links(db, from_path=path, body="[[a]] [[b]]")
    await emit_links(db, from_path=path, body="[[c]]")
    rows = await db.fetchall("SELECT * FROM links WHERE from_path = ?", (path,))
    assert {r["to_path"] for r in rows} == {"c"}


async def test_emit_with_empty_body_clears_links(db):
    path = "kb/entities/projects/x/index.md"
    await emit_links(db, from_path=path, body="[[a]]")
    await emit_links(db, from_path=path, body="")
    rows = await db.fetchall("SELECT * FROM links WHERE from_path = ?", (path,))
    assert rows == []


async def test_emit_does_not_touch_other_files_links(db):
    await emit_links(db, from_path="page-a.md", body="[[x]]")
    await emit_links(db, from_path="page-b.md", body="[[y]]")
    # Re-emit just page-a
    await emit_links(db, from_path="page-a.md", body="")
    rows = await db.fetchall("SELECT * FROM links WHERE from_path = ?", ("page-b.md",))
    assert {r["to_path"] for r in rows} == {"y"}


# ---------------------------------------------------------------------------
# relink_all
# ---------------------------------------------------------------------------


async def test_relink_all_walks_kb_tree(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()
    # Write a couple of fake fact pages
    (layout.kb / "facts" / "projects").mkdir(parents=True, exist_ok=True)
    (layout.kb / "facts" / "projects" / "a.md").write_text(
        "---\nid: fact.a\n---\n\nbody [[target-1]] [[target-2]]\n",
        encoding="utf-8",
    )
    (layout.kb / "facts" / "projects" / "b.md").write_text(
        "no frontmatter [[target-3]]\n",
        encoding="utf-8",
    )

    counts = await relink_all(db, layout)
    assert counts["files"] == 2
    assert counts["links_emitted"] == 3
    rows = await db.fetchall("SELECT * FROM links")
    assert {r["to_path"] for r in rows} == {"target-1", "target-2", "target-3"}


async def test_relink_all_skips_raw_sources(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()
    # Put a markdown file under kb/sources/raw — must be skipped
    raw = layout.source_raw_path("meeting_transcript", "x.md")
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("body [[should-not-be-scanned]]", encoding="utf-8")

    counts = await relink_all(db, layout)
    assert counts["files"] == 0
    assert counts["links_emitted"] == 0


async def test_relink_all_strips_frontmatter_before_scanning(tmp_path):
    """A `[[slug]]` inside the YAML block should not become a link."""
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()
    (layout.kb / "facts" / "people").mkdir(parents=True, exist_ok=True)
    (layout.kb / "facts" / "people" / "x.md").write_text(
        "---\nid: fact.x\ndescription: see [[wrong]]\n---\n\nbody [[right]]\n",
        encoding="utf-8",
    )
    await relink_all(db, layout)
    rows = await db.fetchall("SELECT * FROM links")
    assert {r["to_path"] for r in rows} == {"right"}


async def test_relink_all_wipes_existing_rows(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()
    # Seed a stale row that won't be re-emitted by the walk
    await emit_links(db, from_path="kb/old.md", body="[[stale]]")
    counts = await relink_all(db, layout)
    rows = await db.fetchall("SELECT * FROM links")
    assert rows == []
    assert counts["files"] == 0


async def test_relink_all_is_idempotent(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()
    (layout.kb / "facts" / "projects").mkdir(parents=True, exist_ok=True)
    (layout.kb / "facts" / "projects" / "a.md").write_text(
        "body [[x]]", encoding="utf-8")
    first = await relink_all(db, layout)
    second = await relink_all(db, layout)
    assert first == second
    rows = await db.fetchall("SELECT * FROM links")
    assert len(rows) == 1
