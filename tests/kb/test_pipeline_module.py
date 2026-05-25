# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for synthadoc.kb.pipeline."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from synthadoc.kb import frontmatter as fm
from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.pipeline import run_pipeline
from synthadoc.kb.rules import FactRule, Rules
from tests.kb._fake_provider import FakeProvider


_RULES = Rules(by_fact_type={
    "project.status": FactRule(
        fact_type="project.status",
        strategy="latest_valid_at_wins",
        minimum_confidence="medium",
    ),
})

_SUMMARY_OK = json.dumps({
    "summary": "Status update.",
    "key_points": [],
    "mentioned_entities": [],
    "decisions_mentioned": [],
    "open_questions": [],
    "action_items": [],
    "source_reliability": "high",
})

_FACTS_OK = json.dumps({
    "facts": [{
        "entity_name": "Document Intelligence",
        "entity_type": "project",
        "fact_type": "project.status",
        "value": "completed",
        "value_raw": "now complete",
        "valid_at": "2026-05-22",
        "source_quote": "Drop 1 is now complete.",
        "source_span": "p1",
        "confidence": "high",
    }]
})

# Layer 3 added decision + unknown agents to the pipeline. Most tests don't
# care about them — they queue empty arrays so the pipeline can complete.
_DECISIONS_EMPTY = json.dumps({"decisions": []})
_UNKNOWNS_EMPTY = json.dumps({"unknowns": []})


def _queue_facts_chain(provider, *, facts=_FACTS_OK,
                       decisions=_DECISIONS_EMPTY,
                       unknowns=_UNKNOWNS_EMPTY):
    """Enqueue the three responses pipeline.run will pull from facts_provider."""
    provider.enqueue(facts)
    provider.enqueue(decisions)
    provider.enqueue(unknowns)

_BODY = "Drop 1 is now complete.\n"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def wired(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()

    src_id = ids.source_id("meeting_transcript", "2026-05-22", "x")
    parsed = layout.source_parsed_path("meeting_transcript", "2026-05-22-x.md")
    parsed.parent.mkdir(parents=True, exist_ok=True)
    parsed.write_text(_BODY, encoding="utf-8")
    rel = str(parsed.relative_to(tmp_path)).replace("\\", "/")

    await db.insert_source(
        id=src_id, source_type="meeting_transcript",
        title="2026-05-22 update", authority="informal",
        raw_path=rel, parsed_path=rel,
        created_at="2026-05-22T10:00:00Z",
        ingested_at=datetime.now(timezone.utc).isoformat(),
        sha256=hashlib.sha256(_BODY.encode()).hexdigest(),
    )
    return layout, db, src_id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_run_pipeline_summary_facts_render(wired):
    layout, db, src_id = wired
    sp = FakeProvider(); sp.enqueue(_SUMMARY_OK)
    fp = FakeProvider(); _queue_facts_chain(fp)

    result = await run_pipeline(
        source_id=src_id, db=db, layout=layout, rules=_RULES,
        summary_provider=sp, facts_provider=fp,
    )

    assert result.ok
    assert result.summary_written is True
    assert result.facts_persisted == 1
    assert result.facts_rejected == 0
    assert result.decisions_persisted == 0
    assert result.unknowns_persisted == 0
    assert result.entities_rendered == 1
    assert result.entities_queued_for_review == 0
    eid = ids.entity_id("project", "document-intelligence")
    assert result.entities_touched == (eid,)

    # Entity page on disk
    entity = await db.get_entity(eid)
    page_path = layout.root / entity["path"]
    assert page_path.exists()
    page = fm.read(page_path)
    assert page.type == "entity"
    assert "completed" in page.body


async def test_run_pipeline_uses_two_providers_independently(wired):
    """The summary provider and facts provider must be called separately."""
    layout, db, src_id = wired
    sp = FakeProvider(); sp.enqueue(_SUMMARY_OK)
    fp = FakeProvider(); _queue_facts_chain(fp)

    await run_pipeline(
        source_id=src_id, db=db, layout=layout, rules=_RULES,
        summary_provider=sp, facts_provider=fp,
    )

    assert len(sp.calls) == 1
    # Layer 3 added decision + unknown agents — three LLM calls on the facts provider
    assert len(fp.calls) == 3


# ---------------------------------------------------------------------------
# Non-fatal failure modes
# ---------------------------------------------------------------------------


async def test_summary_failure_does_not_block_fact_extraction(wired):
    layout, db, src_id = wired
    sp = FakeProvider(); sp.enqueue_error(RuntimeError("LLM exploded"))
    fp = FakeProvider(); _queue_facts_chain(fp)

    result = await run_pipeline(
        source_id=src_id, db=db, layout=layout, rules=_RULES,
        summary_provider=sp, facts_provider=fp,
    )

    assert not result.ok
    assert any("summary" in e for e in result.errors)
    # Facts still got extracted and entity still got rendered
    assert result.facts_persisted == 1
    assert result.entities_rendered == 1


async def test_fact_extraction_failure_skips_render(wired):
    layout, db, src_id = wired
    sp = FakeProvider(); sp.enqueue(_SUMMARY_OK)
    fp = FakeProvider()
    fp.enqueue_error(RuntimeError("provider down"))
    # Decisions + unknowns still get called; queue empty payloads
    fp.enqueue(_DECISIONS_EMPTY)
    fp.enqueue(_UNKNOWNS_EMPTY)

    result = await run_pipeline(
        source_id=src_id, db=db, layout=layout, rules=_RULES,
        summary_provider=sp, facts_provider=fp,
    )

    assert not result.ok
    assert result.facts_persisted == 0
    assert result.entities_rendered == 0


async def test_no_facts_means_no_render(wired):
    """A source that produces zero valid facts skips the render step entirely."""
    layout, db, src_id = wired
    sp = FakeProvider(); sp.enqueue(_SUMMARY_OK)
    fp = FakeProvider()
    _queue_facts_chain(fp, facts=json.dumps({"facts": []}))

    result = await run_pipeline(
        source_id=src_id, db=db, layout=layout, rules=_RULES,
        summary_provider=sp, facts_provider=fp,
    )

    assert result.ok  # no errors
    assert result.facts_persisted == 0
    assert result.decisions_persisted == 0
    assert result.unknowns_persisted == 0
    assert result.entities_rendered == 0
    assert result.entities_touched == ()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_second_run_is_no_op_on_cached_path(wired):
    layout, db, src_id = wired
    sp = FakeProvider(); sp.enqueue(_SUMMARY_OK)
    fp = FakeProvider(); _queue_facts_chain(fp)
    await run_pipeline(
        source_id=src_id, db=db, layout=layout, rules=_RULES,
        summary_provider=sp, facts_provider=fp,
    )
    # Second run with no queued responses — every agent must hit its cached path.
    sp2 = FakeProvider()
    fp2 = FakeProvider()
    result = await run_pipeline(
        source_id=src_id, db=db, layout=layout, rules=_RULES,
        summary_provider=sp2, facts_provider=fp2,
    )
    assert result.ok
    assert len(sp2.calls) == 0
    assert len(fp2.calls) == 0
    assert result.summary_written is False        # cached → not "written"
    assert result.facts_persisted == 1            # cached existing facts returned


async def test_unknown_source_surfaces_errors_not_raises(wired):
    layout, db, _ = wired
    sp = FakeProvider()
    fp = FakeProvider()
    result = await run_pipeline(
        source_id="source.meeting.2026-05-22.ghost",
        db=db, layout=layout, rules=_RULES,
        summary_provider=sp, facts_provider=fp,
    )
    # Both summary and facts fail with KeyError; pipeline records, never raises.
    assert not result.ok
    assert result.summary_written is False
    assert result.facts_persisted == 0
    assert any("KeyError" in e for e in result.errors)
