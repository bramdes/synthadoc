# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""End-to-end CLI test for `synthadoc kb revert-source`."""

from __future__ import annotations

import asyncio

from typer.testing import CliRunner

from synthadoc.cli.main import app
from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.storage.log import AuditDB

EID = ids.entity_id("project", "doc-intel")


def _make_wiki(tmp_path):
    (tmp_path / "wiki").mkdir()
    (tmp_path / ".synthadoc").mkdir()
    (tmp_path / ".synthadoc" / "config.toml").write_text(
        "[server]\nport = 7099\n", encoding="utf-8"
    )
    return tmp_path


async def _seed(wiki):
    """Scaffold the kb tier and seed one source + fact + audit ingest row."""
    layout = KBLayout(wiki)
    layout.ensure_layout()
    (wiki / "kb_config.yaml").write_text(
        "resolution_rules:\n"
        "  project.status:\n"
        "    strategy: latest_valid_at_wins\n"
        "    minimum_confidence: medium\n",
        encoding="utf-8",
    )
    db = KBDB(layout.db_path)
    await db.init()
    src_id = ids.source_id("meeting_transcript", "2026-05-01", "standup")
    await db.insert_source(
        id=src_id, source_type="meeting_transcript", title="standup",
        authority="informal", raw_path="r", parsed_path="p",
        created_at="2026-05-01T10:00:00Z", ingested_at="2026-05-01T11:00:00Z",
        sha256="deadbeef",
    )
    await db.upsert_entity(
        id=EID, entity_type="project", name="Doc Intel", slug="doc-intel",
        path="kb/entities/projects/doc-intel/index.md",
    )
    rel = "kb/facts/projects/doc-intel/project.status/2026-05-01-in_progress.md"
    (wiki / rel).parent.mkdir(parents=True, exist_ok=True)
    (wiki / rel).write_text("# in_progress\n", encoding="utf-8")
    await db.insert_fact(
        id=ids.fact_id(EID, "project.status", "2026-05-01"),
        entity_id=EID, fact_type="project.status", value="in_progress",
        valid_at="2026-05-01", observed_at="2026-05-01T09:00:00Z",
        source_id=src_id, source_path="p", source_quote="q",
        confidence="high", path=rel,
    )
    audit = AuditDB(wiki / ".synthadoc" / "audit.db")
    await audit.init()
    await audit.record_ingest(
        source_hash="deadbeef", source_size=123, source_path="staging/standup.md",
        wiki_page="standup", tokens=10, cost_usd=0.0,
    )
    return src_id


async def _source_exists(wiki, src_id) -> bool:
    db = KBDB(KBLayout(wiki).db_path)
    await db.init()
    return (await db.get_source(src_id)) is not None


async def _dedup_exists(wiki, sha, size) -> bool:
    audit = AuditDB(wiki / ".synthadoc" / "audit.db")
    await audit.init()
    return (await audit.find_by_hash(sha, size)) is not None


def test_revert_source_by_hash_clears_kb_and_dedup(tmp_path):
    wiki = _make_wiki(tmp_path)
    src_id = asyncio.run(_seed(wiki))

    result = CliRunner().invoke(
        app, ["kb", "revert-source", "--source-hash", "deadbeef", "--wiki", str(wiki)]
    )
    assert result.exit_code == 0, result.output

    assert asyncio.run(_source_exists(wiki, src_id)) is False
    assert asyncio.run(_dedup_exists(wiki, "deadbeef", 123)) is False


def test_revert_source_dry_run_changes_nothing(tmp_path):
    wiki = _make_wiki(tmp_path)
    src_id = asyncio.run(_seed(wiki))

    result = CliRunner().invoke(
        app, ["kb", "revert-source", "--source-id", src_id, "--dry-run", "--wiki", str(wiki)]
    )
    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
    assert asyncio.run(_source_exists(wiki, src_id)) is True  # untouched


def test_revert_source_requires_one_selector(tmp_path):
    wiki = _make_wiki(tmp_path)
    asyncio.run(_seed(wiki))
    result = CliRunner().invoke(app, ["kb", "revert-source", "--wiki", str(wiki)])
    assert result.exit_code != 0
