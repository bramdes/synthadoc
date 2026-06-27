# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""End-to-end CLI tests for `synthadoc wiki merge` / `wiki dedup`."""

from __future__ import annotations

from typer.testing import CliRunner

from synthadoc.cli.main import app
from synthadoc.storage import wiki_aliases as wa
from synthadoc.storage.wiki import WikiStorage, WikiPage

runner = CliRunner()


def _make_wiki(tmp_path):
    (tmp_path / "wiki").mkdir()
    (tmp_path / ".synthadoc").mkdir()
    (tmp_path / ".synthadoc" / "config.toml").write_text(
        "[server]\nport = 7099\n", encoding="utf-8")
    return tmp_path


def _page(content, title="T", tags=None):
    return WikiPage(title=title, tags=tags or [], content=content,
                    status="active", confidence="medium", sources=[])


def test_wiki_merge_cli(tmp_path):
    wiki = _make_wiki(tmp_path)
    store = WikiStorage(wiki / "wiki")
    store.write_page("lyzr", _page("# Lyzr\n\nKeeper.", title="Lyzr"), folder="projects")
    store.write_page("liz", _page("# Liz\n\nVariant. [[liz]]", title="Liz"), folder="projects")
    (wiki / "wiki" / "index.md").write_text(
        "## Recently Added\n- [[lyzr]] — Lyzr\n- [[liz]] — Liz\n", encoding="utf-8")

    result = runner.invoke(app, ["wiki", "merge", "liz", "--into", "lyzr",
                                 "-w", str(wiki)])
    assert result.exit_code == 0, result.output
    assert "Merged 'liz' → 'lyzr'" in result.output
    assert not store.page_exists("liz")
    assert "Variant." in store.read_page("lyzr").content
    assert wa.load(wiki / "wiki_aliases.yaml").resolve("liz") == "lyzr"


def test_wiki_merge_dry_run_writes_nothing(tmp_path):
    wiki = _make_wiki(tmp_path)
    store = WikiStorage(wiki / "wiki")
    store.write_page("lyzr", _page("# Lyzr\n\nKeeper."), folder="projects")
    store.write_page("liz", _page("# Liz\n\nVariant."), folder="projects")

    result = runner.invoke(app, ["wiki", "merge", "liz", "--into", "lyzr",
                                 "-w", str(wiki), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output.lower()
    assert store.page_exists("liz")   # untouched
    assert not (wiki / "wiki_aliases.yaml").exists()


def test_wiki_merge_unknown_slug_errors(tmp_path):
    wiki = _make_wiki(tmp_path)
    store = WikiStorage(wiki / "wiki")
    store.write_page("lyzr", _page("# Lyzr\n\nKeeper."), folder="projects")
    result = runner.invoke(app, ["wiki", "merge", "ghost", "--into", "lyzr",
                                 "-w", str(wiki)])
    assert result.exit_code != 0


def test_wiki_dedup_cli(tmp_path):
    wiki = _make_wiki(tmp_path)
    store = WikiStorage(wiki / "wiki")
    for slug, title in [("liz", "Liz"), ("lizer", "Lizer"), ("byob", "BYOB")]:
        store.write_page(slug, _page(f"# {title}\n\nx", title=title), folder="projects")

    result = runner.invoke(app, ["wiki", "dedup", "-w", str(wiki)])
    assert result.exit_code == 0, result.output
    assert "lizer" in result.output and "liz" in result.output
    assert "wiki merge" in result.output   # suggests the command


def test_wiki_dedup_none_found(tmp_path):
    wiki = _make_wiki(tmp_path)
    store = WikiStorage(wiki / "wiki")
    for slug in ("byob", "egp", "aura"):
        store.write_page(slug, _page(f"# {slug}\n\nx", title=slug), folder="projects")
    result = runner.invoke(app, ["wiki", "dedup", "-w", str(wiki)])
    assert result.exit_code == 0, result.output
    assert "No likely-duplicate pages" in result.output
