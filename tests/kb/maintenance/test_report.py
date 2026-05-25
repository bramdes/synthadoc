# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for synthadoc.kb.maintenance.report.run_all and the CLI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from synthadoc.cli.main import app
from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.maintenance.report import run_all


runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def small_wiki(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()
    src_id = ids.source_id("meeting_transcript", "2026-05-22", "x")
    await db.insert_source(
        id=src_id, source_type="meeting_transcript", title="x",
        authority="informal", raw_path="r", parsed_path="p",
        created_at="2026-05-22T10:00:00Z",
        ingested_at="2026-05-23T09:00:00Z",
        sha256="h-1",
    )
    eid = ids.entity_id("project", "doc-intel")
    await db.upsert_entity(
        id=eid, entity_type="project", name="Doc Intel",
        slug="doc-intel",
        path="kb/entities/projects/doc-intel/index.md",
    )
    fid = ids.fact_id(eid, "project.status", "2026-05-22")
    await db.insert_fact(
        id=fid, entity_id=eid, fact_type="project.status",
        value="completed", valid_at="2026-05-22",
        observed_at="2026-05-23T09:00:00Z",
        source_id=src_id, source_path="p",
        source_quote="status: completed",
        confidence="high",
        path="kb/facts/projects/doc-intel/project.status/2026-05-22-completed.md",
    )
    return layout, db, eid


# ---------------------------------------------------------------------------
# run_all
# ---------------------------------------------------------------------------


async def test_run_all_writes_health_and_archive(small_wiki):
    layout, db, _ = small_wiki
    report = await run_all(db, layout)
    assert report.ok
    assert report.health_path.exists()
    assert report.report_path.exists()
    # Health snapshot identical to the archived copy
    assert report.health_path.read_text(encoding="utf-8") == \
           report.report_path.read_text(encoding="utf-8")


async def test_run_all_counts_each_job(small_wiki):
    layout, db, _ = small_wiki
    report = await run_all(db, layout)
    assert set(report.counts) == {
        "conflicts",
        "stale_pages",
        "orphan_facts",
        "facts_without_evidence",
        "conclusions_without_facts",
        "duplicate_entity_candidates",
        "broken_links",
        "people_pages_with_policy_flags",
    }
    assert report.counts["conflicts"] == 0
    assert report.counts["orphan_facts"] == 0
    assert report.counts["facts_without_evidence"] == 0
    assert report.counts["duplicate_entity_candidates"] == 0
    assert report.counts["broken_links"] == 0
    # Entity was never rendered → it's stale
    assert report.counts["stale_pages"] == 1


async def test_run_all_renders_histories_by_default(small_wiki):
    layout, db, eid = small_wiki
    report = await run_all(db, layout)
    assert report.histories_rendered == 1
    history_path = layout.entity_history_path("project", "doc-intel")
    assert history_path.exists()


async def test_run_all_skip_histories(small_wiki):
    layout, db, _ = small_wiki
    report = await run_all(db, layout, render_histories=False)
    assert report.histories_rendered == 0


async def test_run_all_individual_job_failure_is_non_fatal(small_wiki, monkeypatch):
    """One job exploding must not stop the others."""
    layout, db, _ = small_wiki

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated")

    monkeypatch.setattr(
        "synthadoc.kb.maintenance.contradictions.run",
        AsyncMock(side_effect=_boom),
    )
    report = await run_all(db, layout)
    assert not report.ok
    assert any("contradictions" in e for e in report.errors)
    # Other counts still populated
    assert "stale_pages" in report.counts


async def test_run_all_threshold_marks_pass_or_fail(small_wiki):
    layout, db, _ = small_wiki
    report = await run_all(db, layout)
    text = report.health_path.read_text(encoding="utf-8")
    # stale_pages threshold is 5, value is 1 → PASS
    assert "PASS" in text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def installed_wiki(tmp_wiki, monkeypatch):
    """A tmp_wiki registered under a name so resolve_wiki_path() finds it."""
    fake_registry = tmp_wiki / "wikis.json"
    fake_registry.write_text(
        json.dumps({"test-wiki": {"path": str(tmp_wiki), "demo": None,
                                  "installed": "2026-05-24"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("synthadoc.cli.install._REGISTRY", fake_registry)
    cfg = tmp_wiki / ".synthadoc" / "config.toml"
    cfg.write_text("[wiki]\ndomain = \"Test\"\n", encoding="utf-8")
    return tmp_wiki


def test_cli_maintenance_run_requires_kb_init(installed_wiki):
    r = runner.invoke(app, ["kb", "maintenance", "run", "-w", "test-wiki"])
    assert r.exit_code != 0
    assert "kb init" in r.output


def test_cli_maintenance_run_outputs_counts(installed_wiki):
    runner.invoke(app, ["kb", "init", "-w", "test-wiki"])
    r = runner.invoke(app, ["kb", "maintenance", "run", "-w", "test-wiki"])
    assert r.exit_code == 0, r.output
    assert "Maintenance complete" in r.output
    assert "conflicts" in r.output
    assert "stale_pages" in r.output
    # Reports landed on disk
    assert (installed_wiki / "kb" / "maintenance" / "kb_health.md").exists()
    reports = list((installed_wiki / "kb" / "maintenance" / "reports").iterdir())
    assert len(reports) == 1


def test_cli_skip_histories(installed_wiki):
    runner.invoke(app, ["kb", "init", "-w", "test-wiki"])
    r = runner.invoke(app, ["kb", "maintenance", "run",
                            "--skip-histories", "-w", "test-wiki"])
    assert r.exit_code == 0
    assert "histories_rendered               0" in r.output
