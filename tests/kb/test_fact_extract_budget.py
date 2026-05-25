# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for the per-source token budget guard on FactExtractAgent."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

import pytest

from synthadoc.agents.fact_extract_agent import (
    FactExtractAgent, FactExtractBudgetExceeded,
)
from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.entity_linker import EntityLinker
from synthadoc.kb.layout import KBLayout
from synthadoc.providers.base import CompletionResponse, LLMProvider, Message


_BODY = "Drop 1 is now complete.\n"

_FACT_PAYLOAD = json.dumps({"facts": [{
    "entity_name": "Document Intelligence",
    "entity_type": "project",
    "fact_type": "project.status",
    "value": "completed",
    "value_raw": "now complete",
    "valid_at": "2026-05-22",
    "source_quote": "Drop 1 is now complete.",
    "source_span": "p1",
    "confidence": "high",
}]})


class _TokenAwareProvider(LLMProvider):
    """A canned provider that can report arbitrary token counts per response."""

    supports_vision = False

    def __init__(self) -> None:
        self._queue: list[tuple[str, int, int]] = []
        self.call_count = 0

    def enqueue(self, text: str, input_tokens: int, output_tokens: int) -> None:
        self._queue.append((text, input_tokens, output_tokens))

    async def complete(
        self, messages: list[Message], system: Optional[str] = None,
        temperature: float = 0.0, max_tokens: int = 4096,
    ) -> CompletionResponse:
        self.call_count += 1
        if not self._queue:
            raise RuntimeError("test provider: queue empty")
        text, it, ot = self._queue.pop(0)
        return CompletionResponse(
            text=text, input_tokens=it, output_tokens=ot,
        )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Within-budget cases
# ---------------------------------------------------------------------------


async def test_no_budget_runs_normally(wired):
    """max_tokens_per_source=0 (default) → no budget check at all."""
    layout, db, linker, src = wired
    provider = _TokenAwareProvider()
    provider.enqueue(_FACT_PAYLOAD, input_tokens=100_000, output_tokens=100_000)
    agent = FactExtractAgent(
        provider=provider, db=db, layout=layout, linker=linker,
        max_tokens_per_source=0,
    )
    result = await agent.extract(src)
    assert len(result.persisted) == 1
    assert result.input_tokens + result.output_tokens == 200_000


async def test_budget_with_headroom_runs(wired):
    layout, db, linker, src = wired
    provider = _TokenAwareProvider()
    provider.enqueue(_FACT_PAYLOAD, input_tokens=300, output_tokens=200)
    agent = FactExtractAgent(
        provider=provider, db=db, layout=layout, linker=linker,
        max_tokens_per_source=10_000,
    )
    result = await agent.extract(src)
    assert len(result.persisted) == 1


# ---------------------------------------------------------------------------
# Budget tripped
# ---------------------------------------------------------------------------


async def test_budget_exceeded_raises_on_first_call(wired):
    layout, db, linker, src = wired
    provider = _TokenAwareProvider()
    # First (and only) response already exceeds the cap
    provider.enqueue(_FACT_PAYLOAD, input_tokens=600, output_tokens=500)
    agent = FactExtractAgent(
        provider=provider, db=db, layout=layout, linker=linker,
        max_tokens_per_source=1_000,
    )
    with pytest.raises(FactExtractBudgetExceeded, match="1100 > 1000"):
        await agent.extract(src)
    # No facts persisted — abort happened after the LLM call but before _persist
    facts = await db.list_facts()
    assert facts == []


async def test_budget_exceeded_includes_source_id(wired):
    layout, db, linker, src = wired
    provider = _TokenAwareProvider()
    provider.enqueue(_FACT_PAYLOAD, input_tokens=2000, output_tokens=0)
    agent = FactExtractAgent(
        provider=provider, db=db, layout=layout, linker=linker,
        max_tokens_per_source=1_000,
    )
    with pytest.raises(FactExtractBudgetExceeded, match=src):
        await agent.extract(src)


async def test_budget_exceeded_on_retry_aborts(wired):
    layout, db, linker, src = wired
    provider = _TokenAwareProvider()
    # First call: parse failure under budget
    provider.enqueue("not json", input_tokens=400, output_tokens=200)
    # Retry: small payload but cumulative now exceeds 1000
    provider.enqueue(_FACT_PAYLOAD, input_tokens=300, output_tokens=200)
    agent = FactExtractAgent(
        provider=provider, db=db, layout=layout, linker=linker,
        max_tokens_per_source=1_000,
    )
    with pytest.raises(FactExtractBudgetExceeded):
        await agent.extract(src)
    # Both calls were made
    assert provider.call_count == 2


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------


async def test_pipeline_records_budget_error_non_fatally(wired):
    """The pipeline must catch FactExtractBudgetExceeded and continue."""
    from synthadoc.kb.pipeline import run_pipeline
    from synthadoc.kb.rules import FactRule, Rules
    from tests.kb._fake_provider import FakeProvider

    layout, db, linker, src = wired

    rules = Rules(by_fact_type={
        "project.status": FactRule(
            fact_type="project.status",
            strategy="latest_valid_at_wins",
            minimum_confidence="medium",
        ),
    })

    summary_provider = FakeProvider()
    summary_provider.enqueue(json.dumps({
        "summary": "x", "key_points": [], "mentioned_entities": [],
        "decisions_mentioned": [], "open_questions": [], "action_items": [],
        "source_reliability": "high",
    }))

    facts_provider = _TokenAwareProvider()
    # Fact call: oversized
    facts_provider.enqueue(_FACT_PAYLOAD, input_tokens=2000, output_tokens=0)

    result = await run_pipeline(
        source_id=src, db=db, layout=layout, rules=rules,
        summary_provider=summary_provider,
        facts_provider=facts_provider,
        max_tokens_per_fact_extract=1_000,
    )
    assert not result.ok
    assert any("FactExtractBudgetExceeded" in e for e in result.errors)
    assert result.facts_persisted == 0
    # Summary still ran fine
    assert result.summary_written is True


# ---------------------------------------------------------------------------
# Config wiring
# ---------------------------------------------------------------------------


def test_ingest_config_default_is_unbounded(tmp_path):
    """The new field defaults to 0 (unbounded) to stay backwards-compatible."""
    from synthadoc.config import load_config
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[agents]\ndefault = { provider = "anthropic", model = "claude-opus-4-6" }\n',
        encoding="utf-8",
    )
    cfg = load_config(project_config=cfg_file)
    assert cfg.ingest.max_tokens_per_fact_extract == 0


def test_ingest_config_reads_max_tokens_field(tmp_path):
    from synthadoc.config import load_config
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[agents]\ndefault = { provider = "anthropic", model = "claude-opus-4-6" }\n'
        '[ingest]\nmax_tokens_per_fact_extract = 20000\n',
        encoding="utf-8",
    )
    cfg = load_config(project_config=cfg_file)
    assert cfg.ingest.max_tokens_per_fact_extract == 20000
