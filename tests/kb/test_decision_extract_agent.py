# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for DecisionExtractAgent."""

from __future__ import annotations

import json

import pytest

from synthadoc.agents.decision_extract_agent import (
    DecisionExtractAgent, PROMPT_VERSION,
)
from synthadoc.kb import frontmatter as fm
from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.entity_linker import EntityLinker
from synthadoc.kb.layout import KBLayout
from tests.kb._fake_provider import FakeProvider


_BODY = (
    "# Update\n\n"
    "Drop 1 is now complete.\n"
    "Decision: production launch is approved.\n"
)


@pytest.fixture
async def wired(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()
    linker = EntityLinker(db, layout)

    src_id = ids.source_id("meeting_transcript", "2026-05-22", "x")
    parsed = layout.source_parsed_path("meeting_transcript", "2026-05-22-x.md")
    parsed.parent.mkdir(parents=True, exist_ok=True)
    parsed.write_text(_BODY, encoding="utf-8")
    rel = str(parsed.relative_to(tmp_path)).replace("\\", "/")
    await db.insert_source(
        id=src_id, source_type="meeting_transcript",
        title="Update", authority="informal",
        raw_path=rel, parsed_path=rel,
        created_at="2026-05-22T10:00:00Z",
        ingested_at="2026-05-23T09:00:00Z",
        sha256="h-1",
    )
    return layout, db, linker, src_id


def _decision(**kw):
    base = dict(
        title="Production launch approved",
        entity_name="Document Intelligence",
        entity_type="project",
        decision_date="2026-05-22",
        authority="informal",
        rationale="Drop 1 is complete.",
        consequences="Customer announcement scheduled.",
        reversal="If post-launch defects exceed threshold.",
        source_quote="Decision: production launch is approved.",
    )
    base.update(kw)
    return base


def _payload(decisions):
    return json.dumps({"decisions": decisions})


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_extract_persists_decision(wired):
    layout, db, linker, src = wired
    provider = FakeProvider(); provider.enqueue(_payload([_decision()]))
    agent = DecisionExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src)
    assert len(result.persisted) == 1
    p = result.persisted[0]
    assert p.decision_date == "2026-05-22"
    md = layout.root / p.path
    assert md.exists()
    parsed = fm.read(md)
    assert parsed.type == "decision"
    assert parsed.data["prompt_version"] == PROMPT_VERSION
    assert "Decision: Production launch approved" in parsed.body
    assert "Rationale" in parsed.body


async def test_extract_links_to_entity(wired):
    layout, db, linker, src = wired
    provider = FakeProvider(); provider.enqueue(_payload([_decision()]))
    agent = DecisionExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src)
    eid = ids.entity_id("project", "document-intelligence")
    assert result.persisted[0].entity_id == eid
    # Entity was auto-created by the linker
    ent = await db.get_entity(eid)
    assert ent is not None


async def test_extract_cross_cutting_decision_has_no_entity(wired):
    layout, db, linker, src = wired
    provider = FakeProvider()
    provider.enqueue(_payload([
        _decision(entity_name="", entity_type=""),
    ]))
    agent = DecisionExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src)
    assert result.persisted[0].entity_id is None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_paraphrased_quote_rejected(wired):
    layout, db, linker, src = wired
    provider = FakeProvider()
    provider.enqueue(_payload([
        _decision(source_quote="Production has been approved."),
    ]))
    agent = DecisionExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src)
    assert len(result.persisted) == 0
    assert len(result.rejected) == 1
    assert "source_quote" in result.rejected[0][0]


async def test_unknown_authority_rejected(wired):
    layout, db, linker, src = wired
    provider = FakeProvider()
    provider.enqueue(_payload([_decision(authority="executive")]))
    agent = DecisionExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src)
    assert len(result.persisted) == 0
    assert "authority" in result.rejected[0][0]


async def test_bad_date_rejected(wired):
    layout, db, linker, src = wired
    provider = FakeProvider()
    provider.enqueue(_payload([_decision(decision_date="May 22 2026")]))
    agent = DecisionExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src)
    assert len(result.persisted) == 0


async def test_entity_name_without_type_rejected(wired):
    layout, db, linker, src = wired
    provider = FakeProvider()
    provider.enqueue(_payload([
        _decision(entity_type=""),  # name set, type empty
    ]))
    agent = DecisionExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src)
    assert len(result.persisted) == 0


# ---------------------------------------------------------------------------
# Idempotency + collision suffix
# ---------------------------------------------------------------------------


async def test_re_running_skips_llm(wired):
    layout, db, linker, src = wired
    provider = FakeProvider(); provider.enqueue(_payload([_decision()]))
    agent = DecisionExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    first = await agent.extract(src)
    second = await agent.extract(src)
    assert second.cached is True
    assert len(second.persisted) == 1
    assert len(provider.calls) == 1


async def test_empty_extraction_still_caches(wired):
    """A source that produced zero decisions must still cache so re-runs don't re-LLM."""
    layout, db, linker, src = wired
    provider = FakeProvider(); provider.enqueue(_payload([]))
    agent = DecisionExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    first = await agent.extract(src)
    assert len(first.persisted) == 0
    second = await agent.extract(src)
    assert second.cached is True
    assert len(provider.calls) == 1


async def test_collision_suffix_for_same_title_same_date(wired):
    layout, db, linker, src = wired
    provider = FakeProvider()
    provider.enqueue(_payload([
        _decision(),
        # Same title on the same date — second one should get -2 suffix
        _decision(source_quote="Drop 1 is now complete."),
    ]))
    agent = DecisionExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src)
    assert len(result.persisted) == 2
    ids_set = {p.decision_id for p in result.persisted}
    assert any(d.endswith("-2") for d in ids_set)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


async def test_unknown_source_raises(wired):
    layout, db, linker, _ = wired
    agent = DecisionExtractAgent(provider=FakeProvider(), db=db, layout=layout, linker=linker)
    with pytest.raises(KeyError):
        await agent.extract("source.meeting.2026-05-22.ghost")


async def test_one_retry_on_parse_failure(wired):
    layout, db, linker, src = wired
    provider = FakeProvider()
    provider.enqueue("nope")
    provider.enqueue(_payload([_decision()]))
    agent = DecisionExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src)
    assert len(result.persisted) == 1
    assert len(provider.calls) == 2
