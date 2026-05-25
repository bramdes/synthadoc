# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for the orchestrator's kb_pipeline wire-up (plan §9 Layer 2 final item)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from synthadoc.config import load_config
from synthadoc.core.orchestrator import Orchestrator
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from tests.kb._fake_provider import FakeProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_orchestrator_with_kb(tmp_wiki):
    """Init an orchestrator AND its kb tier so kb_pipeline can run."""
    orch = Orchestrator(wiki_root=tmp_wiki, config=load_config())
    await orch.init()
    layout = KBLayout(tmp_wiki)
    layout.ensure_layout()
    await KBDB(layout.db_path).init()
    return orch, layout


# ---------------------------------------------------------------------------
# _enqueue_kb_pipeline
# ---------------------------------------------------------------------------


async def test_enqueue_kb_pipeline_skips_url(tmp_wiki):
    """URL / web-search sources have no local file → quietly skip."""
    orch, _ = await _make_orchestrator_with_kb(tmp_wiki)
    job_id = await orch._enqueue_kb_pipeline("https://example.com/article")
    assert job_id is None


async def test_enqueue_kb_pipeline_skips_when_kb_uninitialised(tmp_wiki):
    """If the wiki has no kb.db, the fact-tier hook is silently skipped."""
    orch = Orchestrator(wiki_root=tmp_wiki, config=load_config())
    await orch.init()
    # KB tier deliberately NOT initialised
    f = tmp_wiki / "raw_sources" / "x.md"
    f.write_text("body", encoding="utf-8")
    job_id = await orch._enqueue_kb_pipeline(str(f))
    assert job_id is None


async def test_enqueue_kb_pipeline_imports_and_enqueues(tmp_wiki):
    orch, layout = await _make_orchestrator_with_kb(tmp_wiki)
    src = tmp_wiki / "raw_sources" / "2026-05-22-x.md"
    src.write_text("Drop 1 is now complete.\n", encoding="utf-8")

    job_id = await orch._enqueue_kb_pipeline(str(src))

    assert job_id is not None
    from synthadoc.core.queue import JobStatus
    jobs = await orch._queue.list_jobs(status=JobStatus.PENDING)
    pipeline_jobs = [j for j in jobs if j.operation == "kb_pipeline"]
    assert len(pipeline_jobs) == 1
    assert "source_id" in pipeline_jobs[0].payload
    assert pipeline_jobs[0].payload["source_id"].startswith("source.")

    # The source row landed in kb.db
    db = KBDB(layout.db_path)
    sources = await db.list_sources()
    assert len(sources) == 1
    assert sources[0]["id"] == pipeline_jobs[0].payload["source_id"]


async def test_enqueue_kb_pipeline_is_idempotent_by_sha(tmp_wiki):
    """Re-enqueuing the same file enqueues a second job but does not re-import."""
    orch, layout = await _make_orchestrator_with_kb(tmp_wiki)
    src = tmp_wiki / "raw_sources" / "2026-05-22-x.md"
    src.write_text("Drop 1 is now complete.\n", encoding="utf-8")

    j1 = await orch._enqueue_kb_pipeline(str(src))
    j2 = await orch._enqueue_kb_pipeline(str(src))
    assert j1 is not None and j2 is not None and j1 != j2
    # Only one source row exists despite two enqueues
    db = KBDB(layout.db_path)
    assert len(await db.list_sources()) == 1


async def test_enqueue_kb_pipeline_never_raises(tmp_wiki, monkeypatch):
    """Any failure inside the hook must be swallowed and return None."""
    orch, _ = await _make_orchestrator_with_kb(tmp_wiki)

    def _explode(*_a, **_kw):
        raise RuntimeError("simulated kb failure")

    monkeypatch.setattr(
        "synthadoc.kb.import_source.import_source",
        AsyncMock(side_effect=_explode),
    )
    src = tmp_wiki / "raw_sources" / "x.md"
    src.write_text("body", encoding="utf-8")
    job_id = await orch._enqueue_kb_pipeline(str(src))
    assert job_id is None


# ---------------------------------------------------------------------------
# _run_kb_pipeline
# ---------------------------------------------------------------------------


_SUMMARY_OK = json.dumps({
    "summary": "x", "key_points": [], "mentioned_entities": [],
    "decisions_mentioned": [], "open_questions": [], "action_items": [],
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

_DECISIONS_EMPTY = json.dumps({"decisions": []})
_UNKNOWNS_EMPTY = json.dumps({"unknowns": []})


async def test_run_kb_pipeline_marks_complete_with_result(tmp_wiki):
    orch, layout = await _make_orchestrator_with_kb(tmp_wiki)
    src = tmp_wiki / "raw_sources" / "2026-05-22-x.md"
    src.write_text("Drop 1 is now complete.\n", encoding="utf-8")

    # Import the source first to get a real source_id
    job_id = await orch._enqueue_kb_pipeline(str(src))
    from synthadoc.core.queue import JobStatus
    pending = [j for j in await orch._queue.list_jobs(status=JobStatus.PENDING)
               if j.operation == "kb_pipeline"]
    source_id = pending[0].payload["source_id"]

    sp = FakeProvider(); sp.enqueue(_SUMMARY_OK)
    fp = FakeProvider()
    fp.enqueue(_FACTS_OK)
    fp.enqueue(_DECISIONS_EMPTY)
    fp.enqueue(_UNKNOWNS_EMPTY)

    def _fake_make_provider(role, _cfg):
        return sp if role == "summary" else fp

    with patch("synthadoc.core.orchestrator.make_provider", side_effect=_fake_make_provider):
        await orch._run_kb_pipeline(job_id, source_id=source_id)

    # Job is completed and result payload carries the summary
    completed = await orch._queue.list_jobs(status=JobStatus.COMPLETED)
    me = [j for j in completed if j.id == job_id]
    assert len(me) == 1
    payload = me[0].result
    assert payload["source_id"] == source_id
    assert payload["facts_persisted"] == 1
    assert payload["entities_rendered"] == 1
    assert payload["ok"] is True


async def test_run_kb_pipeline_fails_when_kb_not_initialised(tmp_wiki):
    """The handler must fail permanently if kb.db doesn't exist."""
    orch = Orchestrator(wiki_root=tmp_wiki, config=load_config())
    await orch.init()
    # No kb tier
    job_id = await orch._queue.enqueue("kb_pipeline", {"source_id": "source.x"})
    await orch._run_kb_pipeline(job_id, source_id="source.x")
    from synthadoc.core.queue import JobStatus
    failed = await orch._queue.list_jobs(status=JobStatus.FAILED)
    assert any(j.id == job_id for j in failed)


# ---------------------------------------------------------------------------
# _run_ingest tail-hook
# ---------------------------------------------------------------------------


async def test_run_ingest_enqueues_kb_pipeline_on_success(tmp_wiki):
    """After a successful page-tier ingest, a kb_pipeline job must be enqueued."""
    orch, _ = await _make_orchestrator_with_kb(tmp_wiki)
    src = tmp_wiki / "raw_sources" / "2026-05-22-x.md"
    src.write_text("Drop 1 is now complete.\n", encoding="utf-8")
    job_id = await orch._queue.enqueue("ingest",
                                        {"source": str(src), "force": False})

    # Mock the IngestAgent so we don't depend on a real provider
    mock_result = MagicMock()
    mock_result.pages_created = ["doc-intel"]
    mock_result.pages_updated = []
    mock_result.pages_flagged = []
    mock_result.child_sources = []
    mock_result.tokens_used = 0
    mock_result.input_tokens = 0
    mock_result.output_tokens = 0
    mock_result.cost_usd = 0.0
    mock_agent = MagicMock()
    mock_agent.ingest = AsyncMock(return_value=mock_result)

    with patch("synthadoc.core.orchestrator.make_provider", return_value=MagicMock()), \
         patch("synthadoc.agents.ingest_agent.IngestAgent", return_value=mock_agent):
        await orch._run_ingest(job_id, str(src), auto_confirm=True)

    from synthadoc.core.queue import JobStatus
    pending = await orch._queue.list_jobs(status=JobStatus.PENDING)
    pipeline = [j for j in pending if j.operation == "kb_pipeline"]
    assert len(pipeline) == 1


async def test_run_ingest_does_not_enqueue_for_url_source(tmp_wiki):
    """URL sources skip the fact-tier hook."""
    orch, _ = await _make_orchestrator_with_kb(tmp_wiki)
    url = "https://example.com/article"
    job_id = await orch._queue.enqueue("ingest", {"source": url, "force": False})

    mock_result = MagicMock()
    mock_result.pages_created = []
    mock_result.pages_updated = []
    mock_result.pages_flagged = []
    mock_result.child_sources = []
    mock_result.tokens_used = 0
    mock_result.input_tokens = 0
    mock_result.output_tokens = 0
    mock_result.cost_usd = 0.0
    mock_agent = MagicMock()
    mock_agent.ingest = AsyncMock(return_value=mock_result)

    with patch("synthadoc.core.orchestrator.make_provider", return_value=MagicMock()), \
         patch("synthadoc.agents.ingest_agent.IngestAgent", return_value=mock_agent):
        await orch._run_ingest(job_id, url, auto_confirm=True)

    from synthadoc.core.queue import JobStatus
    pending = await orch._queue.list_jobs(status=JobStatus.PENDING)
    assert not any(j.operation == "kb_pipeline" for j in pending)
