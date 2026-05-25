# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for UnknownExtractAgent."""

from __future__ import annotations

import json

import pytest

from synthadoc.agents.unknown_extract_agent import (
    PROMPT_VERSION, UnknownExtractAgent,
)
from synthadoc.kb import frontmatter as fm
from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.entity_linker import EntityLinker
from synthadoc.kb.layout import KBLayout
from tests.kb._fake_provider import FakeProvider


_BODY = "Drop 1 is now complete.\nProduction rollout has not been confirmed.\n"


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
        id=src_id, source_type="meeting_transcript", title="Update",
        authority="informal", raw_path=rel, parsed_path=rel,
        created_at="2026-05-22T10:00:00Z",
        ingested_at="2026-05-23T09:00:00Z",
        sha256="h-1",
    )
    return layout, db, linker, src_id


def _u(**kw):
    base = dict(
        question="Is production rollout planned?",
        entity_name="Document Intelligence",
        entity_type="project",
        source_quote="Production rollout has not been confirmed.",
        reasoning="Source states rollout unconfirmed.",
    )
    base.update(kw)
    return base


def _payload(items):
    return json.dumps({"unknowns": items})


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_extract_persists_unknown(wired):
    layout, db, linker, src = wired
    provider = FakeProvider(); provider.enqueue(_payload([_u()]))
    agent = UnknownExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src)
    assert len(result.persisted) == 1
    p = result.persisted[0]
    eid = ids.entity_id("project", "document-intelligence")
    assert p.entity_id == eid
    md = layout.root / p.path
    assert md.exists()
    parsed = fm.read(md)
    assert parsed.type == "unknown"
    assert parsed.data["prompt_version"] == PROMPT_VERSION
    assert parsed.data["source_id"] == src
    assert "Production rollout has not been confirmed." in parsed.body


async def test_empty_quote_allowed_for_missing_evidence_unknowns(wired):
    layout, db, linker, src = wired
    provider = FakeProvider()
    provider.enqueue(_payload([
        _u(question="Who owns final sign-off?",
           source_quote="",  # the source is silent on this — that's the unknown
           reasoning="No owner mentioned in this source."),
    ]))
    agent = UnknownExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src)
    assert len(result.persisted) == 1
    assert len(result.rejected) == 0


async def test_non_empty_quote_must_be_substring(wired):
    layout, db, linker, src = wired
    provider = FakeProvider()
    provider.enqueue(_payload([
        _u(source_quote="Production has been confirmed."),  # paraphrase
    ]))
    agent = UnknownExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src)
    assert len(result.persisted) == 0
    assert "source_quote" in result.rejected[0][0]


async def test_cross_cutting_unknown_has_no_entity(wired):
    layout, db, linker, src = wired
    provider = FakeProvider()
    provider.enqueue(_payload([_u(entity_name="", entity_type="")]))
    agent = UnknownExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src)
    assert result.persisted[0].entity_id is None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_unknown_entity_type_rejected(wired):
    layout, db, linker, src = wired
    provider = FakeProvider()
    provider.enqueue(_payload([_u(entity_type="device")]))
    agent = UnknownExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src)
    assert len(result.persisted) == 0


async def test_entity_name_without_type_rejected(wired):
    layout, db, linker, src = wired
    provider = FakeProvider()
    provider.enqueue(_payload([_u(entity_type="")]))  # name set, type empty
    agent = UnknownExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src)
    assert len(result.persisted) == 0


async def test_missing_question_rejected(wired):
    layout, db, linker, src = wired
    bad = _u(); del bad["question"]
    provider = FakeProvider(); provider.enqueue(_payload([bad]))
    agent = UnknownExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src)
    assert len(result.persisted) == 0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_re_running_skips_llm(wired):
    layout, db, linker, src = wired
    provider = FakeProvider(); provider.enqueue(_payload([_u()]))
    agent = UnknownExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    await agent.extract(src)
    second = await agent.extract(src)
    assert second.cached is True
    assert len(provider.calls) == 1


async def test_empty_extraction_still_caches(wired):
    """A source that produced zero unknowns must still be cached so re-runs don't re-LLM."""
    layout, db, linker, src = wired
    provider = FakeProvider(); provider.enqueue(_payload([]))
    agent = UnknownExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    first = await agent.extract(src)
    assert len(first.persisted) == 0
    second = await agent.extract(src)
    assert second.cached is True
    assert len(provider.calls) == 1


async def test_collision_suffix_when_same_question_repeated(wired):
    layout, db, linker, src = wired
    provider = FakeProvider()
    provider.enqueue(_payload([
        _u(),
        _u(source_quote="Drop 1 is now complete."),  # different quote, same question
    ]))
    agent = UnknownExtractAgent(provider=provider, db=db, layout=layout, linker=linker)
    result = await agent.extract(src)
    assert len(result.persisted) == 2
    suffixes = {p.unknown_id for p in result.persisted}
    assert any(u.endswith("-2") for u in suffixes)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


async def test_unknown_source_raises(wired):
    layout, db, linker, _ = wired
    agent = UnknownExtractAgent(provider=FakeProvider(), db=db, layout=layout, linker=linker)
    with pytest.raises(KeyError):
        await agent.extract("source.meeting.2026-05-22.ghost")
