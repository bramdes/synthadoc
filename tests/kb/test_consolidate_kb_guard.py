# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for the ConsolidateAgent fact-tier guard (plan §14 Q4)."""

from __future__ import annotations

import pytest

from synthadoc.agents.consolidate_agent import ConsolidateAgent
from synthadoc.storage.search import HybridSearch
from synthadoc.storage.wiki import WikiStorage
from tests.kb._fake_provider import FakeProvider


@pytest.fixture
def env(tmp_path):
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    store = WikiStorage(wiki_dir)
    search = HybridSearch(store, tmp_path / ".synthadoc" / "embeddings.db")
    agent = ConsolidateAgent(
        provider=FakeProvider(),
        store=store,
        search=search,
        wiki_root=tmp_path,
        min_chars=10,  # tiny floor so we'd otherwise consolidate
    )
    return tmp_path, wiki_dir, agent


async def _write_page(wiki_dir, slug: str, type_value: str | None):
    """Write a markdown page with optional `type:` frontmatter."""
    fm_lines = ["title: " + slug]
    if type_value:
        fm_lines.append(f"type: {type_value}")
    fm_lines += ["tags: []", "status: active", "confidence: high", "sources: []"]
    fm = "\n".join(fm_lines)
    body = f"# {slug}\n\nbody with [[other]] and some content here.\n\n_— Source: x · 2026-05-22_\n"
    (wiki_dir / f"{slug}.md").write_text(
        f"---\n{fm}\n---\n\n{body}", encoding="utf-8",
    )


# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fact_type", [
    "entity", "fact", "decision", "unknown", "history",
    "source", "source_summary", "derived_conclusion",
])
async def test_refuses_fact_tier_pages(env, fact_type):
    tmp_path, wiki_dir, agent = env
    slug = f"sample-{fact_type}"
    await _write_page(wiki_dir, slug, type_value=fact_type)

    result = await agent.consolidate(slug)
    assert result.skipped is True
    assert "fact-tier" in result.skip_reason
    assert fact_type in result.skip_reason


async def test_allows_ordinary_wiki_pages(env):
    tmp_path, wiki_dir, agent = env
    slug = "regular-page"
    await _write_page(wiki_dir, slug, type_value=None)
    # Pad so we comfortably clear min_chars / length_floor checks
    text = (wiki_dir / f"{slug}.md").read_text(encoding="utf-8")
    (wiki_dir / f"{slug}.md").write_text(text + "x" * 400, encoding="utf-8")
    agent._provider.enqueue(
        "# regular-page\n\nbody with [[other]] preserved.\n\n"
        "_— Source: x · 2026-05-22_\n" + "x" * 400
    )
    # The fact-tier guard MUST NOT trigger on a plain wiki page.
    result = await agent.consolidate(slug)
    if result.skipped:
        assert "fact-tier" not in result.skip_reason


async def test_unknown_type_value_is_not_a_fact_tier_block(env):
    """A page with `type: notes` (unrelated value) must NOT be refused."""
    tmp_path, wiki_dir, agent = env
    slug = "notes-page"
    await _write_page(wiki_dir, slug, type_value="notes")
    text = (wiki_dir / f"{slug}.md").read_text(encoding="utf-8")
    (wiki_dir / f"{slug}.md").write_text(text + "x" * 400, encoding="utf-8")
    agent._provider.enqueue(
        "# notes-page\n\nbody with [[other]] preserved.\n\n"
        "_— Source: x · 2026-05-22_\n" + "x" * 400
    )
    result = await agent.consolidate(slug)
    if result.skipped:
        assert "fact-tier" not in result.skip_reason
