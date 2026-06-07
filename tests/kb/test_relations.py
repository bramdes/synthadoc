# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for kb_relations.yaml parsing, the CLI helper, and render output."""

from __future__ import annotations

import pytest

from synthadoc.agents.entity_render_agent import EntityRenderAgent
from synthadoc.kb import ids, relations
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.rules import FactRule, Rules


_RULES = Rules(by_fact_type={
    "project.status": FactRule(
        fact_type="project.status", strategy="latest_valid_at_wins",
        minimum_confidence="medium",
    ),
})


# ---------------------------------------------------------------------------
# parse / add_relation
# ---------------------------------------------------------------------------


def test_parse_builds_both_directions():
    r = relations.parse(
        "relations:\n"
        "  project:\n"
        "    Doc Intel:\n"
        "      - Doc Intel intent extraction\n"
        "      - doc-intel-itc-production-deployment\n"
    )
    assert r.children_of("project", "doc-intel") == (
        "doc-intel-intent-extraction", "doc-intel-itc-production-deployment",
    )
    assert r.parent_of("project", "doc-intel-intent-extraction") == "doc-intel"
    assert r.parent_of("project", "doc-intel") is None


def test_parse_ignores_self_reference():
    r = relations.parse("relations:\n  project:\n    Doc Intel:\n      - Doc Intel\n")
    assert r.children_of("project", "doc-intel") == ()


def test_parse_rejects_unknown_type():
    with pytest.raises(relations.RelationError):
        relations.parse("relations:\n  widget:\n    A:\n      - b\n")


def test_add_relation_idempotent(tmp_path):
    p = tmp_path / "kb_relations.yaml"
    relations.add_relation(p, entity_type="project", parent_name="Doc Intel",
                           child_name="Doc Intel intent extraction")
    relations.add_relation(p, entity_type="project", parent_name="Doc Intel",
                           child_name="Doc Intel intent extraction")
    r = relations.load(p)
    assert r.parent_of("project", "doc-intel-intent-extraction") == "doc-intel"
    assert p.read_text(encoding="utf-8").count("Doc Intel intent extraction") == 1


def test_add_relation_rejects_self(tmp_path):
    with pytest.raises(relations.RelationError):
        relations.add_relation(tmp_path / "r.yaml", entity_type="project",
                               parent_name="Doc Intel", child_name="doc-intel")


# ---------------------------------------------------------------------------
# render integration
# ---------------------------------------------------------------------------


@pytest.fixture
async def wired(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()
    src = ids.source_id("meeting_transcript", "2026-05-22", "x")
    await db.insert_source(
        id=src, source_type="meeting_transcript", title="x", authority="informal",
        raw_path="r", parsed_path="p", created_at="2026-05-22T10:00:00Z",
        ingested_at="2026-05-23T09:00:00Z", sha256="h1",
    )
    return layout, db, src


async def _entity_with_fact(db, layout, *, slug, name, src):
    eid = ids.entity_id("project", slug)
    await db.upsert_entity(id=eid, entity_type="project", name=name, slug=slug,
                           path=f"kb/entities/projects/{slug}/index.md")
    fid = ids.fact_id(eid, "project.status", "2026-05-22")
    await db.insert_fact(
        id=fid, entity_id=eid, fact_type="project.status", value="active",
        valid_at="2026-05-22", observed_at="2026-05-23T09:00:00Z", source_id=src,
        source_path="p", source_quote="active", confidence="high",
        path=f"kb/facts/projects/{slug}/project.status/2026-05-22-active.md",
    )
    return eid


async def test_parent_page_shows_subareas(wired):
    layout, db, src = wired
    await _entity_with_fact(db, layout, slug="doc-intel", name="Doc Intel", src=src)
    await _entity_with_fact(db, layout, slug="doc-intel-intent-extraction",
                            name="Doc Intel intent extraction", src=src)
    relations.add_relation(layout.relations_path, entity_type="project",
                           parent_name="Doc Intel",
                           child_name="Doc Intel intent extraction")

    agent = EntityRenderAgent(db=db, layout=layout, rules=_RULES)
    res = await agent.render(ids.entity_id("project", "doc-intel"))
    body = res.path.read_text(encoding="utf-8")
    assert "## Sub-areas" in body
    # Path-style link to the child entity's index page, with display alias.
    assert "[[kb/entities/projects/doc-intel-intent-extraction/index|Doc Intel intent extraction]]" in body


async def test_child_page_shows_part_of(wired):
    layout, db, src = wired
    await _entity_with_fact(db, layout, slug="doc-intel", name="Doc Intel", src=src)
    child = await _entity_with_fact(db, layout, slug="doc-intel-intent-extraction",
                                    name="Doc Intel intent extraction", src=src)
    relations.add_relation(layout.relations_path, entity_type="project",
                           parent_name="Doc Intel",
                           child_name="Doc Intel intent extraction")

    agent = EntityRenderAgent(db=db, layout=layout, rules=_RULES)
    res = await agent.render(child)
    body = res.path.read_text(encoding="utf-8")
    assert "_Part of [[kb/entities/projects/doc-intel/index|Doc Intel]]_" in body


async def test_subarea_link_skipped_when_child_absent(wired):
    """A declared sub-area whose child entity wasn't produced this import is
    omitted, so the parent page never carries a dangling link."""
    layout, db, src = wired
    await _entity_with_fact(db, layout, slug="doc-intel", name="Doc Intel", src=src)
    # Declare a child that does NOT exist as an entity this import.
    relations.add_relation(layout.relations_path, entity_type="project",
                           parent_name="Doc Intel",
                           child_name="Doc Intel ghost workstream")
    agent = EntityRenderAgent(db=db, layout=layout, rules=_RULES)
    res = await agent.render(ids.entity_id("project", "doc-intel"))
    body = res.path.read_text(encoding="utf-8")
    # No Sub-areas section at all, since the only declared child has no page.
    assert "## Sub-areas" not in body
    assert "doc-intel-ghost-workstream" not in body


async def test_subarea_link_skipped_when_child_has_no_facts(wired):
    """An entity row with zero facts gets no page, so it isn't linked."""
    layout, db, src = wired
    await _entity_with_fact(db, layout, slug="doc-intel", name="Doc Intel", src=src)
    # Child entity row exists but carries no facts → no page.
    await db.upsert_entity(
        id=ids.entity_id("project", "doc-intel-empty"), entity_type="project",
        name="Doc Intel empty", slug="doc-intel-empty",
        path="kb/entities/projects/doc-intel-empty/index.md",
    )
    relations.add_relation(layout.relations_path, entity_type="project",
                           parent_name="Doc Intel", child_name="Doc Intel empty")
    agent = EntityRenderAgent(db=db, layout=layout, rules=_RULES)
    res = await agent.render(ids.entity_id("project", "doc-intel"))
    assert "## Sub-areas" not in res.path.read_text(encoding="utf-8")


async def test_no_relations_means_no_sections(wired):
    layout, db, src = wired
    await _entity_with_fact(db, layout, slug="doc-intel", name="Doc Intel", src=src)
    agent = EntityRenderAgent(db=db, layout=layout, rules=_RULES)
    res = await agent.render(ids.entity_id("project", "doc-intel"))
    body = res.path.read_text(encoding="utf-8")
    assert "## Sub-areas" not in body
    assert "Part of" not in body
