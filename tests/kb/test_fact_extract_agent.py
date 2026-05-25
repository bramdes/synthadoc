# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for FactExtractAgent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from synthadoc.agents.fact_extract_agent import (
    FactExtractAgent,
    PROMPT_VERSION,
    _quote_in_body,
)
from synthadoc.kb import frontmatter as fm
from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.entity_linker import EntityLinker
from synthadoc.kb.layout import KBLayout
from tests.kb._fake_provider import FakeProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_SOURCE_BODY = (
    "# Doc Intel Update\n\n"
    "Drop 1 is now complete.\n"
    "Production rollout has not been confirmed.\n"
)


@pytest.fixture
async def wired(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()
    linker = EntityLinker(db, layout)

    src_id = ids.source_id("meeting_transcript", "2026-05-22", "doc-intel-update")
    parsed = layout.source_parsed_path(
        "meeting_transcript", "2026-05-22-doc-intel-update.md"
    )
    parsed.parent.mkdir(parents=True, exist_ok=True)
    parsed.write_text(_SOURCE_BODY, encoding="utf-8")
    rel = str(parsed.relative_to(tmp_path)).replace("\\", "/")
    await db.insert_source(
        id=src_id, source_type="meeting_transcript",
        title="2026-05-22 Doc Intel Update",
        authority="informal",
        raw_path=rel, parsed_path=rel,
        created_at="2026-05-22T10:00:00+08:00",
        ingested_at="2026-05-23T09:00:00+08:00",
        sha256="h-1",
    )
    return layout, db, linker, src_id


def _fact_obj(**kw):
    base = dict(
        entity_name="Document Intelligence",
        entity_type="project",
        fact_type="project.status",
        value="completed",
        value_raw="now complete",
        valid_at="2026-05-22",
        source_quote="Drop 1 is now complete.",
        source_span="paragraph 2",
        confidence="high",
    )
    base.update(kw)
    return base


def _payload(facts):
    return json.dumps({"facts": facts})


# ---------------------------------------------------------------------------
# Substring guard (pure)
# ---------------------------------------------------------------------------


class TestQuoteInBody:
    def test_exact_match(self):
        assert _quote_in_body("Drop 1 is now complete.", _SOURCE_BODY)

    def test_paraphrase_rejected(self):
        assert not _quote_in_body("Drop 1 has been completed.", _SOURCE_BODY)

    def test_whitespace_tolerant(self):
        body = "First line.\n\nSecond  line\there."
        assert _quote_in_body("Second line here.", body)

    def test_empty_quote_rejected(self):
        assert not _quote_in_body("", _SOURCE_BODY)

    def test_empty_body_rejected(self):
        assert not _quote_in_body("anything", "")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_extract_persists_valid_fact(wired):
    layout, db, linker, src_id = wired
    provider = FakeProvider()
    provider.enqueue(_payload([_fact_obj()]))
    agent = FactExtractAgent(provider=provider, db=db, layout=layout, linker=linker)

    result = await agent.extract(src_id)

    assert len(result.persisted) == 1
    assert len(result.rejected) == 0
    persisted = result.persisted[0]
    eid = ids.entity_id("project", "document-intelligence")
    expected_fid = ids.fact_id(eid, "project.status", "2026-05-22")
    assert persisted.fact_id == expected_fid

    # Markdown written with discriminated frontmatter
    md_path = layout.root / persisted.path
    assert md_path.exists()
    parsed = fm.read(md_path)
    assert parsed.type == "fact"
    assert parsed.data["source_quote"] == "Drop 1 is now complete."
    assert parsed.data["prompt_version"] == PROMPT_VERSION

    # DB row matches
    row = await db.get_fact(expected_fid)
    assert row["value"] == "completed"
    assert row["confidence"] == "high"

    # Entity auto-created via linker
    ent = await db.get_entity(eid)
    assert ent is not None
    assert ent["name"] == "Document Intelligence"


async def test_extract_handles_multiple_facts(wired):
    layout, db, linker, src_id = wired
    provider = FakeProvider()
    provider.enqueue(_payload([
        _fact_obj(),
        _fact_obj(
            fact_type="open_issue.created",
            value="production-rollout-unconfirmed",
            value_raw="not been confirmed",
            source_quote="Production rollout has not been confirmed.",
        ),
    ]))
    agent = FactExtractAgent(provider=provider, db=db, layout=layout, linker=linker)

    result = await agent.extract(src_id)
    assert len(result.persisted) == 2
    types = {p.fact_type for p in result.persisted}
    assert types == {"project.status", "open_issue.created"}


# ---------------------------------------------------------------------------
# Validation: substring guard
# ---------------------------------------------------------------------------


async def test_paraphrased_quote_rejected(wired):
    layout, db, linker, src_id = wired
    provider = FakeProvider()
    provider.enqueue(_payload([
        _fact_obj(source_quote="Drop 1 has been completed."),  # paraphrase
    ]))
    agent = FactExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src_id)
    assert len(result.persisted) == 0
    assert len(result.rejected) == 1
    assert "source_quote" in result.rejected[0][0]


async def test_mix_of_good_and_paraphrased(wired):
    layout, db, linker, src_id = wired
    provider = FakeProvider()
    provider.enqueue(_payload([
        _fact_obj(),                                          # OK
        _fact_obj(source_quote="Production is live."),        # hallucination
    ]))
    agent = FactExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src_id)
    assert len(result.persisted) == 1
    assert len(result.rejected) == 1


# ---------------------------------------------------------------------------
# Validation: closed vocabularies
# ---------------------------------------------------------------------------


async def test_unknown_fact_type_rejected(wired):
    layout, db, linker, src_id = wired
    provider = FakeProvider()
    provider.enqueue(_payload([_fact_obj(fact_type="project.colour")]))
    agent = FactExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src_id)
    assert len(result.persisted) == 0
    assert len(result.rejected) == 1
    assert "fact_type" in result.rejected[0][0]


async def test_unknown_entity_type_rejected(wired):
    layout, db, linker, src_id = wired
    provider = FakeProvider()
    provider.enqueue(_payload([_fact_obj(entity_type="device")]))
    agent = FactExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src_id)
    assert len(result.persisted) == 0
    assert "entity_type" in result.rejected[0][0]


async def test_bad_date_format_rejected(wired):
    layout, db, linker, src_id = wired
    provider = FakeProvider()
    provider.enqueue(_payload([_fact_obj(valid_at="2026/05/22")]))
    agent = FactExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src_id)
    assert len(result.persisted) == 0
    assert "valid_at" in result.rejected[0][0]


async def test_invalid_confidence_rejected(wired):
    layout, db, linker, src_id = wired
    provider = FakeProvider()
    provider.enqueue(_payload([_fact_obj(confidence="meh")]))
    agent = FactExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src_id)
    assert len(result.persisted) == 0
    assert "confidence" in result.rejected[0][0]


async def test_missing_required_field_rejected(wired):
    layout, db, linker, src_id = wired
    bad = _fact_obj()
    del bad["entity_name"]
    provider = FakeProvider()
    provider.enqueue(_payload([bad]))
    agent = FactExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src_id)
    assert len(result.persisted) == 0
    assert "entity_name" in result.rejected[0][0]


# ---------------------------------------------------------------------------
# Parse failure + retry
# ---------------------------------------------------------------------------


async def test_one_retry_on_unparseable_json(wired):
    layout, db, linker, src_id = wired
    provider = FakeProvider()
    provider.enqueue("not json at all")
    provider.enqueue(_payload([_fact_obj()]))
    agent = FactExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src_id)
    assert len(result.persisted) == 1
    assert len(provider.calls) == 2
    # Second call's prompt mentions the parse error
    assert "previous attempt failed validation" in provider.calls[1].messages[0].content


async def test_persistent_parse_failure_raises(wired):
    layout, db, linker, src_id = wired
    provider = FakeProvider()
    provider.enqueue("nope")
    provider.enqueue("still nope")
    agent = FactExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    with pytest.raises(ValueError, match="unparseable JSON"):
        await agent.extract(src_id)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_re_running_skips_llm(wired):
    layout, db, linker, src_id = wired
    provider = FakeProvider()
    provider.enqueue(_payload([_fact_obj()]))
    agent = FactExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    await agent.extract(src_id)
    second = await agent.extract(src_id)
    assert second.cached is True
    assert len(second.persisted) == 1
    assert len(provider.calls) == 1


async def test_force_re_runs_llm(wired):
    layout, db, linker, src_id = wired
    provider = FakeProvider()
    provider.enqueue(_payload([_fact_obj()]))
    provider.enqueue(_payload([]))  # second call: no new facts (avoid id collision noise)
    agent = FactExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    await agent.extract(src_id)
    second = await agent.extract(src_id, force=True)
    assert second.cached is False
    assert len(provider.calls) == 2


# ---------------------------------------------------------------------------
# Collision suffix
# ---------------------------------------------------------------------------


async def test_collision_suffix_when_same_entity_type_date(wired):
    layout, db, linker, src_id = wired
    # Two milestone facts on the same date for the same entity
    provider = FakeProvider()
    provider.enqueue(_payload([
        _fact_obj(
            fact_type="project.milestone",
            value="drop-1",
            source_quote="Drop 1 is now complete.",
        ),
        _fact_obj(
            fact_type="project.milestone",
            value="rollout-pending",
            source_quote="Production rollout has not been confirmed.",
        ),
    ]))
    agent = FactExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src_id)
    assert len(result.persisted) == 2
    ids_set = {p.fact_id for p in result.persisted}
    eid = ids.entity_id("project", "document-intelligence")
    base = ids.fact_id(eid, "project.milestone", "2026-05-22")
    assert base in ids_set
    assert any(fid.endswith("-2") for fid in ids_set)


# ---------------------------------------------------------------------------
# Unknown source / missing body
# ---------------------------------------------------------------------------


async def test_unknown_source_raises(wired):
    layout, db, linker, _ = wired
    agent = FactExtractAgent(provider=FakeProvider(), db=db, layout=layout, linker=linker)
    with pytest.raises(KeyError):
        await agent.extract("source.meeting.2026-05-22.ghost")


async def test_missing_parsed_body_rejects_all_quotes(wired):
    """With no body to substring-match against, every quote is rejected."""
    layout, db, linker, src_id = wired
    parsed = layout.source_parsed_path(
        "meeting_transcript", "2026-05-22-doc-intel-update.md"
    )
    parsed.unlink()
    provider = FakeProvider()
    provider.enqueue(_payload([_fact_obj()]))
    agent = FactExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src_id)
    assert len(result.persisted) == 0
    assert len(result.rejected) == 1
