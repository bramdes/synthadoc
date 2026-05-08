# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
import pytest
from unittest.mock import AsyncMock

from synthadoc.agents.consolidate_agent import ConsolidateAgent, ConsolidateResult
from synthadoc.providers.base import CompletionResponse
from synthadoc.storage.wiki import WikiStorage, WikiPage
from synthadoc.storage.search import HybridSearch


def _make_agent(tmp_wiki, llm_text: str):
    store = WikiStorage(tmp_wiki / "wiki")
    search = HybridSearch(store, tmp_wiki / ".synthadoc" / "embeddings.db")
    provider = AsyncMock()
    provider.complete = AsyncMock(return_value=CompletionResponse(
        text=llm_text, input_tokens=500, output_tokens=300,
    ))
    agent = ConsolidateAgent(provider=provider, store=store, search=search,
                             wiki_root=tmp_wiki, min_chars=200)
    return agent, store


@pytest.fixture
def long_page(tmp_wiki):
    """A page with multiple appended sections + provenance footers."""
    body = (
        "# EGP\n\n"
        "Initial overview paragraph about EGP.\n\n"
        "## API Key Governance\n\n"
        "First mention of API key governance work.\n\n"
        "_— Source: 2026-05-04 13-01-15 EGP Infra Cost · 2026-05-04_\n\n"
        "## API Key Governance\n\n"
        "Second mention with overlapping detail.\n\n"
        "_— Source: 2026-05-05 16-12-45 EGP Compute · 2026-05-05_\n\n"
        "## Cost Review\n\n"
        "Discussion of compute cost trade-offs with [[bedrock]].\n\n"
        "_— Source: 2026-05-05 16-12-45 EGP Compute · 2026-05-05_\n"
    )
    page = WikiPage(title="EGP", tags=["platform"], content=body,
                    status="active", confidence="medium", sources=[],
                    created="2026-05-04")
    return page


@pytest.mark.asyncio
async def test_consolidate_writes_curated_body(tmp_wiki, long_page):
    new_body = (
        "# EGP\n\n"
        "EGP overview.\n\n"
        "## API Key Governance\n\n"
        "Consolidated discussion across two meetings.\n\n"
        "_— Source: 2026-05-04 13-01-15 EGP Infra Cost · 2026-05-04_\n"
        "_— Source: 2026-05-05 16-12-45 EGP Compute · 2026-05-05_\n\n"
        "## Cost Review\n\n"
        "Compute cost discussion with [[bedrock]].\n\n"
        "_— Source: 2026-05-05 16-12-45 EGP Compute · 2026-05-05_\n"
    )
    agent, store = _make_agent(tmp_wiki, new_body)
    store.write_page("egp", long_page)

    result = await agent.consolidate("egp")

    assert isinstance(result, ConsolidateResult)
    assert result.skipped is False
    assert result.before_chars > result.after_chars  # consolidated should shrink
    written = store.read_page("egp")
    assert "Consolidated discussion" in written.content
    # Provenance and wikilinks survive
    assert "_— Source:" in written.content
    assert "[[bedrock]]" in written.content


@pytest.mark.asyncio
async def test_consolidate_skips_short_pages(tmp_wiki):
    short = WikiPage(title="Tiny", tags=[], content="# Tiny\n\nOne line.",
                     status="active", confidence="medium", sources=[],
                     created="2026-05-04")
    agent, store = _make_agent(tmp_wiki, "irrelevant")
    store.write_page("tiny", short)

    result = await agent.consolidate("tiny")

    assert result.skipped is True
    assert "threshold" in result.skip_reason
    # Page must be untouched
    assert store.read_page("tiny").content == "# Tiny\n\nOne line."


@pytest.mark.asyncio
async def test_consolidate_rejects_provenance_loss(tmp_wiki, long_page):
    # LLM strips provenance — agent must refuse to write
    bad_body = "# EGP\n\nEverything condensed without sources.\n\n## Topic\n\n" + ("body. " * 80)
    agent, store = _make_agent(tmp_wiki, bad_body)
    store.write_page("egp", long_page)

    with pytest.raises(RuntimeError, match="provenance"):
        await agent.consolidate("egp")
    # Original content preserved
    assert "_— Source:" in store.read_page("egp").content


@pytest.mark.asyncio
async def test_consolidate_skips_unchanged_page_on_rerun(tmp_wiki, long_page):
    """Second consolidate on an unchanged page is a no-op via consolidated_hash."""
    new_body = (
        "# EGP\n\nCurated body.\n\n## Sec\n\nDetail. " + ("more text. " * 80) + "\n\n"
        "_— Source: 2026-05-04 13-01-15 EGP Infra Cost · 2026-05-04_\n"
    )
    agent, store = _make_agent(tmp_wiki, new_body)
    store.write_page("egp", long_page)

    first = await agent.consolidate("egp")
    assert first.skipped is False
    assert agent._provider.complete.await_count == 1

    second = await agent.consolidate("egp")
    assert second.skipped is True
    assert "no changes" in second.skip_reason
    # LLM not called a second time
    assert agent._provider.complete.await_count == 1


@pytest.mark.asyncio
async def test_consolidate_force_bypasses_unchanged_skip(tmp_wiki, long_page):
    """force=True re-runs even when the page hasn't changed since last consolidation."""
    new_body = (
        "# EGP\n\nCurated body.\n\n## Sec\n\nDetail. " + ("more text. " * 80) + "\n\n"
        "_— Source: 2026-05-04 13-01-15 EGP Infra Cost · 2026-05-04_\n"
    )
    agent, store = _make_agent(tmp_wiki, new_body)
    store.write_page("egp", long_page)

    await agent.consolidate("egp")
    assert agent._provider.complete.await_count == 1

    forced = await agent.consolidate("egp", force=True)
    assert forced.skipped is False
    assert agent._provider.complete.await_count == 2


@pytest.mark.asyncio
async def test_consolidate_reruns_after_content_change(tmp_wiki, long_page):
    """Appending new content (e.g. by ingest) breaks the hash and consolidate runs again."""
    new_body = (
        "# EGP\n\nCurated body.\n\n## Sec\n\nDetail. " + ("more text. " * 80) + "\n\n"
        "_— Source: 2026-05-04 13-01-15 EGP Infra Cost · 2026-05-04_\n"
    )
    agent, store = _make_agent(tmp_wiki, new_body)
    store.write_page("egp", long_page)

    await agent.consolidate("egp")
    assert agent._provider.complete.await_count == 1

    # Simulate ingest appending a fresh section
    page = store.read_page("egp")
    page.content = page.content.rstrip() + (
        "\n\n## New Topic\n\nFreshly ingested. " + ("more. " * 80) + "\n\n"
        "_— Source: 2026-05-08 09-00-00 New Sync · 2026-05-08_\n"
    )
    store.write_page("egp", page)

    again = await agent.consolidate("egp")
    assert again.skipped is False
    assert agent._provider.complete.await_count == 2


@pytest.mark.asyncio
async def test_consolidate_strips_code_fences(tmp_wiki, long_page):
    # LLMs sometimes wrap output in ```markdown fences; agent must strip them
    fenced = (
        "```markdown\n"
        "# EGP\n\nClean body.\n\n## Sec\n\nDetail here. " + ("more text. " * 50) + "\n\n"
        "_— Source: 2026-05-04 13-01-15 EGP Infra Cost · 2026-05-04_\n"
        "```"
    )
    agent, store = _make_agent(tmp_wiki, fenced)
    store.write_page("egp", long_page)

    result = await agent.consolidate("egp")
    written = store.read_page("egp").content
    assert not written.startswith("```")
    assert "```" not in written.splitlines()[-1]
    assert result.skipped is False
