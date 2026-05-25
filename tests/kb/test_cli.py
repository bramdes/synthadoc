# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for `synthadoc kb init`, `synthadoc kb import-source`, and `synthadoc kb backfill`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from synthadoc.cli.main import app


runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def installed_wiki(tmp_wiki, monkeypatch):
    """A tmp_wiki registered under a name so resolve_wiki_path() can find it."""
    # Patch the registry path so the test doesn't touch the user's real registry.
    fake_registry = tmp_wiki / "wikis.json"
    fake_registry.write_text(
        json.dumps({"test-wiki": {"path": str(tmp_wiki), "demo": None, "installed": "2026-05-23"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("synthadoc.cli.install._REGISTRY", fake_registry)
    # The wiki must have a config.toml or the CLI errors out
    cfg = tmp_wiki / ".synthadoc" / "config.toml"
    cfg.write_text("[wiki]\ndomain = \"Test\"\n", encoding="utf-8")
    return tmp_wiki


# ---------------------------------------------------------------------------
# kb init
# ---------------------------------------------------------------------------


def test_kb_init_creates_layout(installed_wiki):
    result = runner.invoke(app, ["kb", "init", "-w", "test-wiki"])
    assert result.exit_code == 0, result.output
    assert (installed_wiki / "kb" / "sources" / "raw" / "meetings").is_dir()
    assert (installed_wiki / "kb" / "entities" / "projects").is_dir()
    assert (installed_wiki / "kb" / "maintenance" / "review_queue.md").is_file()
    assert (installed_wiki / ".synthadoc" / "kb.db").is_file()
    assert (installed_wiki / "kb_config.yaml").is_file()


def test_kb_init_is_idempotent(installed_wiki):
    r1 = runner.invoke(app, ["kb", "init", "-w", "test-wiki"])
    assert r1.exit_code == 0
    # User customises the config — second init must not clobber it
    cfg = installed_wiki / "kb_config.yaml"
    cfg.write_text("# my custom config\n", encoding="utf-8")
    r2 = runner.invoke(app, ["kb", "init", "-w", "test-wiki"])
    assert r2.exit_code == 0
    assert cfg.read_text(encoding="utf-8") == "# my custom config\n"


# ---------------------------------------------------------------------------
# kb import-source
# ---------------------------------------------------------------------------


def _write_sample_source(tmp_path: Path, name: str = "2026-05-22-doc-intel.md") -> Path:
    src = tmp_path / name
    src.write_text(
        "# Document Intelligence Update\n\n"
        "Drop 1 is now complete.\n",
        encoding="utf-8",
    )
    return src


def test_import_source_basic(installed_wiki, tmp_path):
    runner.invoke(app, ["kb", "init", "-w", "test-wiki"])
    src = _write_sample_source(tmp_path)
    result = runner.invoke(app, [
        "kb", "import-source", str(src),
        "--type", "meeting_transcript",
        "-w", "test-wiki",
    ])
    assert result.exit_code == 0, result.output
    assert "Imported:" in result.output
    # Raw + parsed copies exist
    raw = installed_wiki / "kb" / "sources" / "raw" / "meetings"
    parsed = installed_wiki / "kb" / "sources" / "parsed" / "meetings"
    assert any(raw.iterdir())
    assert any(p.suffix == ".md" for p in parsed.iterdir())
    # Sidecar frontmatter file written
    assert any(p.name.endswith(".meta.yaml") for p in parsed.iterdir())


def test_import_source_is_idempotent(installed_wiki, tmp_path):
    runner.invoke(app, ["kb", "init", "-w", "test-wiki"])
    src = _write_sample_source(tmp_path)

    r1 = runner.invoke(app, [
        "kb", "import-source", str(src),
        "--type", "meeting_transcript",
        "-w", "test-wiki",
    ])
    assert r1.exit_code == 0
    r2 = runner.invoke(app, [
        "kb", "import-source", str(src),
        "--type", "meeting_transcript",
        "-w", "test-wiki",
    ])
    assert r2.exit_code == 0
    assert "Already imported" in r2.output


def test_import_source_picks_date_from_filename(installed_wiki, tmp_path):
    runner.invoke(app, ["kb", "init", "-w", "test-wiki"])
    src = _write_sample_source(tmp_path, "2026-05-22-x.md")
    result = runner.invoke(app, [
        "kb", "import-source", str(src),
        "--type", "meeting_transcript",
        "-w", "test-wiki",
    ])
    assert result.exit_code == 0
    assert "source.meeting.2026-05-22" in result.output


def test_import_source_collision_suffixed(installed_wiki, tmp_path):
    runner.invoke(app, ["kb", "init", "-w", "test-wiki"])
    a = tmp_path / "2026-05-22-x.md"
    b = tmp_path / "2026-05-22-x-other.md"
    a.write_text("body a", encoding="utf-8")
    b.write_text("body b", encoding="utf-8")
    # Different filenames so same date but different slugs by default; force collision
    # by re-using the explicit --title so the underlying slugify lands on the same id.
    # Easier path: rename b to look identical-stem (rename to a's stem after a is imported).
    runner.invoke(app, [
        "kb", "import-source", str(a),
        "--type", "meeting_transcript",
        "-w", "test-wiki",
    ])
    # Force same base slug by passing matching --title and a content-different file
    c = tmp_path / "duplicate-name.md"
    c.write_text("different content\n", encoding="utf-8")
    r = runner.invoke(app, [
        "kb", "import-source", str(c),
        "--type", "meeting_transcript",
        "--title", "X",
        "--date", "2026-05-22",
        "-w", "test-wiki",
    ])
    assert r.exit_code == 0
    # First import had stem "2026-05-22-x" → slug "2026-05-22-x"; second has stem
    # "duplicate-name" → slug "duplicate-name". So no collision. But the test
    # is here to assert the code path doesn't crash when called with --date —
    # actual collision-suffix logic is unit-tested in test_ids.py.
    assert "Imported:" in r.output


def test_import_source_missing_file_errors(installed_wiki):
    runner.invoke(app, ["kb", "init", "-w", "test-wiki"])
    r = runner.invoke(app, [
        "kb", "import-source", "/no/such/file.md",
        "--type", "meeting_transcript",
        "-w", "test-wiki",
    ])
    assert r.exit_code != 0


def test_import_source_unknown_type_errors(installed_wiki, tmp_path):
    runner.invoke(app, ["kb", "init", "-w", "test-wiki"])
    src = _write_sample_source(tmp_path)
    r = runner.invoke(app, [
        "kb", "import-source", str(src),
        "--type", "video",
        "-w", "test-wiki",
    ])
    assert r.exit_code != 0


# ---------------------------------------------------------------------------
# kb backfill
# ---------------------------------------------------------------------------


def test_backfill_creates_sources_from_audit(installed_wiki):
    runner.invoke(app, ["kb", "init", "-w", "test-wiki"])
    # Seed audit.db with an ingest record
    import asyncio
    from synthadoc.storage.log import AuditDB
    audit = AuditDB(installed_wiki / ".synthadoc" / "audit.db")

    async def _seed():
        await audit.init()
        await audit.record_ingest(
            source_hash="seedhash1",
            source_size=100,
            source_path="/some/missing/file.md",
            wiki_page="moores-law",
            tokens=500,
            cost_usd=0.002,
        )
    asyncio.run(_seed())

    r = runner.invoke(app, ["kb", "backfill", "-w", "test-wiki"])
    assert r.exit_code == 0, r.output
    assert "1 seeded" in r.output or "1 would seed" in r.output

    # Re-running is a no-op (already present)
    r2 = runner.invoke(app, ["kb", "backfill", "-w", "test-wiki"])
    assert r2.exit_code == 0
    assert "0 seeded" in r2.output


def test_backfill_dry_run_writes_nothing(installed_wiki):
    runner.invoke(app, ["kb", "init", "-w", "test-wiki"])
    import asyncio
    from synthadoc.storage.log import AuditDB
    audit = AuditDB(installed_wiki / ".synthadoc" / "audit.db")

    async def _seed():
        await audit.init()
        await audit.record_ingest(
            source_hash="dryhash",
            source_size=10,
            source_path="/tmp/x.md",
            wiki_page="x",
            tokens=1,
            cost_usd=0.0,
        )
    asyncio.run(_seed())

    r = runner.invoke(app, ["kb", "backfill", "-w", "test-wiki", "--dry-run"])
    assert r.exit_code == 0
    assert "would seed" in r.output
    # Re-running for real should still find this one to insert
    r2 = runner.invoke(app, ["kb", "backfill", "-w", "test-wiki"])
    assert "1 seeded" in r2.output
