# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for synthadoc.kb.maintenance.broken_links + the kb relink CLI."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from synthadoc.cli.main import app
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.links import emit_links
from synthadoc.kb.maintenance import broken_links as BL


runner = CliRunner()


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


@pytest.fixture
async def wired(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()
    return layout, db


async def test_no_links_returns_zero(wired):
    layout, db = wired
    result = await BL.run(db, layout)
    assert result.count == 0
    text = result.report_path.read_text(encoding="utf-8")
    assert "_No broken links._" in text


async def test_link_to_existing_kb_file_is_not_broken(wired):
    layout, db = wired
    (layout.kb / "facts" / "projects").mkdir(parents=True, exist_ok=True)
    (layout.kb / "facts" / "projects" / "target.md").write_text("body", encoding="utf-8")
    await emit_links(db, from_path="kb/entities/x.md", body="[[target]]")
    result = await BL.run(db, layout)
    assert result.count == 0


async def test_link_to_existing_wiki_file_is_not_broken(wired):
    """The detector also resolves slugs against the legacy wiki/ tree."""
    layout, db = wired
    (layout.root / "wiki").mkdir(parents=True, exist_ok=True)
    (layout.root / "wiki" / "page.md").write_text("body", encoding="utf-8")
    await emit_links(db, from_path="kb/entities/x.md", body="[[page]]")
    result = await BL.run(db, layout)
    assert result.count == 0


async def test_link_to_missing_slug_is_broken(wired):
    layout, db = wired
    await emit_links(db, from_path="kb/entities/x.md", body="[[ghost]]")
    result = await BL.run(db, layout)
    assert result.count == 1
    assert result.broken[0].from_path == "kb/entities/x.md"
    assert result.broken[0].to_path == "ghost"


async def test_report_lists_each_broken_link(wired):
    layout, db = wired
    await emit_links(db, from_path="kb/entities/x.md", body="[[a]] [[b]]")
    (layout.kb / "facts" / "projects").mkdir(parents=True, exist_ok=True)
    (layout.kb / "facts" / "projects" / "a.md").write_text("body", encoding="utf-8")
    result = await BL.run(db, layout)
    assert result.count == 1
    text = result.report_path.read_text(encoding="utf-8")
    assert "[[b]]" in text


# ---------------------------------------------------------------------------
# CLI — `synthadoc kb relink`
# ---------------------------------------------------------------------------


@pytest.fixture
def installed_wiki(tmp_wiki, monkeypatch):
    fake = tmp_wiki / "wikis.json"
    fake.write_text(
        json.dumps({"test-wiki": {"path": str(tmp_wiki), "demo": None,
                                  "installed": "2026-05-24"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("synthadoc.cli.install._REGISTRY", fake)
    cfg = tmp_wiki / ".synthadoc" / "config.toml"
    cfg.write_text("[wiki]\ndomain = \"Test\"\n", encoding="utf-8")
    return tmp_wiki


def test_cli_relink_requires_kb_init(installed_wiki):
    r = runner.invoke(app, ["kb", "relink", "-w", "test-wiki"])
    assert r.exit_code != 0
    assert "kb init" in r.output


def test_cli_relink_walks_kb_tree(installed_wiki):
    runner.invoke(app, ["kb", "init", "-w", "test-wiki"])
    # Write a fact file with two wikilinks
    facts_dir = installed_wiki / "kb" / "facts" / "projects"
    facts_dir.mkdir(parents=True, exist_ok=True)
    (facts_dir / "a.md").write_text(
        "body [[target-1]] [[target-2]]", encoding="utf-8"
    )

    r = runner.invoke(app, ["kb", "relink", "-w", "test-wiki"])
    assert r.exit_code == 0, r.output
    # kb init writes several maintenance stub files; just assert the two
    # wikilinks made it into the table.
    assert "Relink complete: 2 link(s) from" in r.output


# ---------------------------------------------------------------------------
# Integration: render agents emit links
# ---------------------------------------------------------------------------


async def test_entity_render_emits_links_to_facts(tmp_path):
    """After EntityRenderAgent writes a page, the links table must reflect it."""
    from synthadoc.agents.entity_render_agent import EntityRenderAgent
    from synthadoc.kb import ids
    from synthadoc.kb.rules import FactRule, Rules

    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()

    eid = ids.entity_id("project", "x")
    await db.upsert_entity(
        id=eid, entity_type="project", name="X", slug="x",
        path="kb/entities/projects/x/index.md",
    )
    src_id = ids.source_id("meeting_transcript", "2026-05-22", "s")
    await db.insert_source(
        id=src_id, source_type="meeting_transcript", title="s",
        authority="informal", raw_path="r", parsed_path="p",
        created_at="2026-05-22T10:00:00Z",
        ingested_at="2026-05-23T09:00:00Z",
        sha256="h-1",
    )
    fid = ids.fact_id(eid, "project.status", "2026-05-22")
    await db.insert_fact(
        id=fid, entity_id=eid, fact_type="project.status",
        value="completed", valid_at="2026-05-22",
        observed_at="2026-05-23T09:00:00Z",
        source_id=src_id, source_path="p",
        source_quote="status: completed",
        confidence="high",
        path="kb/facts/projects/x/project.status/2026-05-22-completed.md",
    )

    rules = Rules(by_fact_type={
        "project.status": FactRule(
            fact_type="project.status",
            strategy="latest_valid_at_wins",
            minimum_confidence="medium",
        ),
    })
    agent = EntityRenderAgent(db=db, layout=layout, rules=rules)
    await agent.render(eid)

    rows = await db.fetchall("SELECT * FROM links")
    assert rows  # the rendered index links to the fact's stem at least
    # The fact's stem is part of the evidence wikilink
    targets = {r["to_path"] for r in rows}
    assert "2026-05-22-completed" in targets
