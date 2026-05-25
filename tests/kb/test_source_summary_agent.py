# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for SourceSummaryAgent."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from synthadoc.agents.source_summary_agent import (
    PROMPT_VERSION,
    SourceSummary,
    SourceSummaryAgent,
)
from synthadoc.kb import frontmatter as fm
from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from tests.kb._fake_provider import FakeProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_CANNED_GOOD = json.dumps({
    "summary": "Drop 1 is complete; production rollout is not yet confirmed.",
    "key_points": [
        "Drop 1 was reported as complete.",
        "Production rollout was not confirmed.",
    ],
    "mentioned_entities": [
        {"name": "Document Intelligence", "entity_type": "project"},
        {"name": "Test Evidence", "entity_type": "topic"},
    ],
    "decisions_mentioned": ["Drop 1 marked complete"],
    "open_questions": ["Does 'complete' include production rollout?"],
    "action_items": ["Confirm production sign-off owner."],
    "source_reliability": "medium",
})


@pytest.fixture
async def wired(tmp_path):
    """A KBDB + KBLayout + one inserted source ready to be summarised."""
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()

    # Create a parsed source file
    src_id = ids.source_id("meeting_transcript", "2026-05-22", "doc-intel-update")
    parsed_path = layout.source_parsed_path(
        "meeting_transcript", "2026-05-22-doc-intel-update.md"
    )
    parsed_path.parent.mkdir(parents=True, exist_ok=True)
    parsed_path.write_text(
        "# Doc Intel Update\n\nDrop 1 is now complete.\n",
        encoding="utf-8",
    )

    rel_parsed = str(parsed_path.relative_to(tmp_path)).replace("\\", "/")
    await db.insert_source(
        id=src_id,
        source_type="meeting_transcript",
        title="2026-05-22 Document Intelligence Update",
        authority="informal",
        raw_path=rel_parsed,
        parsed_path=rel_parsed,
        created_at="2026-05-22T10:00:00+08:00",
        ingested_at="2026-05-23T09:00:00+08:00",
        sha256="hash-1",
    )
    return layout, db, src_id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_summarise_writes_markdown_and_db_row(wired):
    layout, db, src_id = wired
    provider = FakeProvider()
    provider.enqueue(_CANNED_GOOD)
    agent = SourceSummaryAgent(provider=provider, db=db, layout=layout)

    result = await agent.summarise(src_id)

    assert result.summary_id == ids.summary_source_id(src_id)
    assert result.summary_path.exists()
    assert result.cached is False

    # Frontmatter is well-formed and discriminated as source_summary
    parsed = fm.read(result.summary_path)
    assert parsed.type == "source_summary"
    assert parsed.data["source_id"] == src_id
    assert parsed.data["prompt_version"] == PROMPT_VERSION
    assert parsed.data["confidence"] == "medium"

    # Body renders the expected sections
    assert "## Summary" in parsed.body
    assert "Drop 1 was reported as complete." in parsed.body
    assert "Document Intelligence" in parsed.body

    # DB row exists
    rows = await db.fetchall(
        "SELECT * FROM source_summaries WHERE id = ?", (result.summary_id,)
    )
    assert len(rows) == 1
    assert rows[0]["source_id"] == src_id


async def test_summarise_includes_body_in_prompt(wired):
    layout, db, src_id = wired
    provider = FakeProvider()
    provider.enqueue(_CANNED_GOOD)
    agent = SourceSummaryAgent(provider=provider, db=db, layout=layout)
    await agent.summarise(src_id)

    call = provider.calls[0]
    prompt = call.messages[0].content
    assert "Drop 1 is now complete." in prompt
    assert "Source title: 2026-05-22 Document Intelligence Update" in prompt
    # System prompt enforces source-bounded summarisation
    assert "merge in knowledge from other sources" in call.system.lower()


async def test_summarise_truncates_long_body(wired):
    layout, db, src_id = wired
    # Overwrite parsed file with a huge body
    parsed = layout.source_parsed_path("meeting_transcript", "2026-05-22-doc-intel-update.md")
    parsed.write_text("X" * 100_000, encoding="utf-8")
    provider = FakeProvider()
    provider.enqueue(_CANNED_GOOD)
    agent = SourceSummaryAgent(provider=provider, db=db, layout=layout, max_body_chars=5_000)
    await agent.summarise(src_id)
    # Prompt body section should not exceed the cap (plus some prompt scaffolding)
    prompt = provider.calls[0].messages[0].content
    assert prompt.count("X") <= 5_000


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_re_running_skips_llm(wired):
    layout, db, src_id = wired
    provider = FakeProvider()
    provider.enqueue(_CANNED_GOOD)
    agent = SourceSummaryAgent(provider=provider, db=db, layout=layout)
    first = await agent.summarise(src_id)
    second = await agent.summarise(src_id)

    assert second.cached is True
    assert second.summary_path == first.summary_path
    # Only the first call hit the LLM
    assert len(provider.calls) == 1


async def test_force_re_runs_llm(wired):
    layout, db, src_id = wired
    provider = FakeProvider()
    provider.enqueue(_CANNED_GOOD)
    provider.enqueue(_CANNED_GOOD)
    agent = SourceSummaryAgent(provider=provider, db=db, layout=layout)
    await agent.summarise(src_id)
    await agent.summarise(src_id, force=True)
    assert len(provider.calls) == 2


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


async def test_summarise_strips_markdown_fences(wired):
    layout, db, src_id = wired
    provider = FakeProvider()
    provider.enqueue("```json\n" + _CANNED_GOOD + "\n```")
    agent = SourceSummaryAgent(provider=provider, db=db, layout=layout)
    result = await agent.summarise(src_id)
    assert "Drop 1 is complete" in result.summary.summary


async def test_summarise_rejects_invalid_json(wired):
    """If BOTH the initial call and the one retry return bad JSON, raise."""
    layout, db, src_id = wired
    provider = FakeProvider()
    provider.enqueue("definitely not json")
    provider.enqueue("still not json")  # retry response
    agent = SourceSummaryAgent(provider=provider, db=db, layout=layout)
    with pytest.raises(ValueError, match="unparseable JSON"):
        await agent.summarise(src_id)
    assert len(provider.calls) == 2  # initial + one retry


async def test_summarise_retries_on_parse_failure_and_recovers(wired):
    """First response is broken JSON (truncated); retry succeeds."""
    layout, db, src_id = wired
    provider = FakeProvider()
    provider.enqueue('{"summary": "truncated mid-stri')  # truncated
    provider.enqueue(_CANNED_GOOD)                       # retry: full
    agent = SourceSummaryAgent(provider=provider, db=db, layout=layout)
    result = await agent.summarise(src_id)
    assert result.cached is False
    assert result.summary_path.exists()
    assert len(provider.calls) == 2
    # Retry prompt mentions the previous failure
    retry_prompt = provider.calls[1].messages[0].content
    assert "previous attempt failed validation" in retry_prompt


async def test_summarise_strips_truncated_opening_fence(wired):
    """LLM wraps response in ```json``` but the closing fence is truncated."""
    layout, db, src_id = wired
    provider = FakeProvider()
    truncated = "```json\n" + _CANNED_GOOD  # opener present, no closer
    provider.enqueue(truncated)
    agent = SourceSummaryAgent(provider=provider, db=db, layout=layout)
    result = await agent.summarise(src_id)
    assert "Drop 1" in result.summary.summary or "complete" in result.summary.summary.lower()


async def test_summarise_drops_invalid_entity_types(wired):
    layout, db, src_id = wired
    bad = json.dumps({
        "summary": "ok",
        "key_points": [],
        "mentioned_entities": [
            {"name": "Good", "entity_type": "project"},
            {"name": "Bogus", "entity_type": "device"},
            {"name": "", "entity_type": "project"},
            "not even a dict",
        ],
        "decisions_mentioned": [],
        "open_questions": [],
        "action_items": [],
        "source_reliability": "high",
    })
    provider = FakeProvider()
    provider.enqueue(bad)
    agent = SourceSummaryAgent(provider=provider, db=db, layout=layout)
    result = await agent.summarise(src_id)
    assert len(result.summary.mentioned_entities) == 1
    assert result.summary.mentioned_entities[0]["name"] == "Good"


async def test_unknown_reliability_defaults_to_medium(wired):
    layout, db, src_id = wired
    data = json.loads(_CANNED_GOOD)
    data["source_reliability"] = "magical"
    provider = FakeProvider()
    provider.enqueue(json.dumps(data))
    agent = SourceSummaryAgent(provider=provider, db=db, layout=layout)
    result = await agent.summarise(src_id)
    assert result.summary.source_reliability == "medium"


async def test_unknown_source_id_raises(wired):
    layout, db, _ = wired
    agent = SourceSummaryAgent(provider=FakeProvider(), db=db, layout=layout)
    with pytest.raises(KeyError):
        await agent.summarise("source.meeting.2026-05-22.ghost")


async def test_missing_parsed_file_still_calls_llm(wired):
    """A missing parsed source produces an empty body, not a crash."""
    layout, db, src_id = wired
    parsed = layout.source_parsed_path("meeting_transcript", "2026-05-22-doc-intel-update.md")
    parsed.unlink()
    provider = FakeProvider()
    provider.enqueue(_CANNED_GOOD)
    agent = SourceSummaryAgent(provider=provider, db=db, layout=layout)
    result = await agent.summarise(src_id)
    assert result.summary_path.exists()
    prompt = provider.calls[0].messages[0].content
    assert "(empty source body)" in prompt
