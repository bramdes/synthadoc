# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
import pytest
from pathlib import Path
from unittest.mock import AsyncMock

from synthadoc.agents.consolidate_agent import ConsolidateAgent, ConsolidateResult
from synthadoc.providers.base import CompletionResponse
from synthadoc.storage.wiki import WikiStorage, WikiPage
from synthadoc.storage.search import HybridSearch


# A consolidated body that preserves every source line and wikilink from `long_page`
# and stays above the default length floor.
_GOOD_NEW_BODY = (
    "# EGP\n\n"
    "EGP overview paragraph that ties the topics together.\n\n"
    "## API Key Governance\n\n"
    "Consolidated discussion across two meetings. Detail kept here. "
    + ("more detail. " * 30) + "\n\n"
    "_— Source: 2026-05-04 13-01-15 EGP Infra Cost · 2026-05-04_\n"
    "_— Source: 2026-05-05 16-12-45 EGP Compute · 2026-05-05_\n\n"
    "## Cost Review\n\n"
    "Compute cost discussion with [[bedrock]]. " + ("more cost notes. " * 20) + "\n\n"
    "_— Source: 2026-05-05 16-12-45 EGP Compute · 2026-05-05_\n"
)


def _make_agent(tmp_wiki, llm_text: str, **kwargs):
    store = WikiStorage(tmp_wiki / "wiki")
    search = HybridSearch(store, tmp_wiki / ".synthadoc" / "embeddings.db")
    provider = AsyncMock()
    provider.complete = AsyncMock(return_value=CompletionResponse(
        text=llm_text, input_tokens=500, output_tokens=300,
    ))
    agent = ConsolidateAgent(
        provider=provider, store=store, search=search,
        wiki_root=tmp_wiki, min_chars=200,
        **kwargs,
    )
    return agent, store


@pytest.fixture
def long_page(tmp_wiki):
    """A page with multiple appended sections + provenance footers."""
    body = (
        "# EGP\n\n"
        "Initial overview paragraph about EGP. " + ("more overview. " * 20) + "\n\n"
        "## API Key Governance\n\n"
        "First mention of API key governance work. " + ("more detail. " * 15) + "\n\n"
        "_— Source: 2026-05-04 13-01-15 EGP Infra Cost · 2026-05-04_\n\n"
        "## API Key Governance\n\n"
        "Second mention with overlapping detail. " + ("more detail. " * 15) + "\n\n"
        "_— Source: 2026-05-05 16-12-45 EGP Compute · 2026-05-05_\n\n"
        "## Cost Review\n\n"
        "Discussion of compute cost trade-offs with [[bedrock]]. "
        + ("more cost detail. " * 15) + "\n\n"
        "_— Source: 2026-05-05 16-12-45 EGP Compute · 2026-05-05_\n"
    )
    page = WikiPage(title="EGP", tags=["platform"], content=body,
                    status="active", confidence="medium", sources=[],
                    created="2026-05-04")
    return page


@pytest.mark.asyncio
async def test_consolidate_writes_curated_body(tmp_wiki, long_page):
    agent, store = _make_agent(tmp_wiki, _GOOD_NEW_BODY)
    store.write_page("egp", long_page)

    result = await agent.consolidate("egp")

    assert isinstance(result, ConsolidateResult)
    assert result.skipped is False
    written = store.read_page("egp")
    assert "Consolidated discussion" in written.content
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
    assert store.read_page("tiny").content == "# Tiny\n\nOne line."


@pytest.mark.asyncio
async def test_consolidate_rejects_dropped_provenance_source(tmp_wiki, long_page):
    """LLM that drops a source entirely from the page must be rejected."""
    # _GOOD_NEW_BODY has two lines for the 2026-05-05 source; drop both so the
    # source set shrinks compared to long_page's set.
    bad = _GOOD_NEW_BODY.replace(
        "_— Source: 2026-05-05 16-12-45 EGP Compute · 2026-05-05_\n", "",
    )
    agent, store = _make_agent(tmp_wiki, bad)
    store.write_page("egp", long_page)

    with pytest.raises(RuntimeError, match="provenance"):
        await agent.consolidate("egp")
    assert "2026-05-05" in store.read_page("egp").content


@pytest.mark.asyncio
async def test_consolidate_rejects_dropped_wikilink(tmp_wiki, long_page):
    """LLM that drops a [[wikilink]] from the source must be rejected."""
    bad = _GOOD_NEW_BODY.replace("[[bedrock]]", "bedrock")
    agent, store = _make_agent(tmp_wiki, bad)
    store.write_page("egp", long_page)

    with pytest.raises(RuntimeError, match="wikilinks"):
        await agent.consolidate("egp")
    assert "[[bedrock]]" in store.read_page("egp").content


@pytest.mark.asyncio
async def test_consolidate_rejects_over_shrink(tmp_wiki, long_page):
    """LLM output below length-floor × before is treated as over-summarisation."""
    # Above the 200-char hard-min but well below 0.9 × len(long_page).
    tight = (
        "# EGP\n\nTerse summary that still preserves both sources and the [[bedrock]] "
        "wikilink but drops most of the underlying detail from the original page.\n"
        "_— Source: 2026-05-04 13-01-15 EGP Infra Cost · 2026-05-04_\n"
        "_— Source: 2026-05-05 16-12-45 EGP Compute · 2026-05-05_\n"
    )
    assert len(tight) > 200  # passes the empty/short guard
    agent, store = _make_agent(tmp_wiki, tight, length_floor=0.9)
    store.write_page("egp", long_page)

    with pytest.raises(RuntimeError, match="over-shrank"):
        await agent.consolidate("egp")


@pytest.mark.asyncio
async def test_consolidate_writes_backup_before_overwriting(tmp_wiki, long_page):
    agent, store = _make_agent(tmp_wiki, _GOOD_NEW_BODY)
    store.write_page("egp", long_page)
    original_content = long_page.content

    result = await agent.consolidate("egp")

    assert result.backup_path is not None
    backup = Path(result.backup_path)
    assert backup.exists()
    assert backup.parent.name == "consolidate-backups"
    assert backup.read_text(encoding="utf-8") == original_content


@pytest.mark.asyncio
async def test_consolidate_dry_run_does_not_write(tmp_wiki, long_page):
    agent, store = _make_agent(tmp_wiki, _GOOD_NEW_BODY)
    store.write_page("egp", long_page)
    before = store.read_page("egp").content

    result = await agent.consolidate("egp", dry_run=True)

    assert result.skipped is False
    assert result.proposed_body is not None
    assert "Consolidated discussion" in result.proposed_body
    assert result.backup_path is None
    # Page must be untouched and consolidated_hash must NOT be set
    after = store.read_page("egp")
    assert after.content == before
    assert after.consolidated_hash is None
    # No backup file written
    backup_dir = tmp_wiki / ".synthadoc" / "consolidate-backups"
    assert not backup_dir.exists() or not list(backup_dir.iterdir())


@pytest.mark.asyncio
async def test_consolidate_skips_unchanged_page_on_rerun(tmp_wiki, long_page):
    agent, store = _make_agent(tmp_wiki, _GOOD_NEW_BODY)
    store.write_page("egp", long_page)

    first = await agent.consolidate("egp")
    assert first.skipped is False
    assert agent._provider.complete.await_count == 1

    second = await agent.consolidate("egp")
    assert second.skipped is True
    assert "no changes" in second.skip_reason
    assert agent._provider.complete.await_count == 1


@pytest.mark.asyncio
async def test_consolidate_force_bypasses_unchanged_skip(tmp_wiki, long_page):
    agent, store = _make_agent(tmp_wiki, _GOOD_NEW_BODY)
    store.write_page("egp", long_page)

    await agent.consolidate("egp")
    assert agent._provider.complete.await_count == 1

    forced = await agent.consolidate("egp", force=True)
    assert forced.skipped is False
    assert agent._provider.complete.await_count == 2


@pytest.mark.asyncio
async def test_consolidate_reruns_after_content_change(tmp_wiki, long_page):
    agent, store = _make_agent(tmp_wiki, _GOOD_NEW_BODY)
    store.write_page("egp", long_page)

    await agent.consolidate("egp")
    assert agent._provider.complete.await_count == 1

    page = store.read_page("egp")
    page.content = page.content.rstrip() + (
        "\n\n## New Topic\n\nFreshly ingested. " + ("more. " * 80) + "\n\n"
        "_— Source: 2026-05-08 09-00-00 New Sync · 2026-05-08_\n"
    )
    store.write_page("egp", page)

    # The mocked LLM still returns _GOOD_NEW_BODY which won't preserve the new
    # 2026-05-08 source — that's fine, we only need to confirm the agent
    # *attempted* a re-run (not that it succeeded).
    with pytest.raises(RuntimeError, match="provenance"):
        await agent.consolidate("egp")
    assert agent._provider.complete.await_count == 2


@pytest.mark.asyncio
async def test_consolidate_strips_code_fences(tmp_wiki, long_page):
    fenced = "```markdown\n" + _GOOD_NEW_BODY + "\n```"
    agent, store = _make_agent(tmp_wiki, fenced)
    store.write_page("egp", long_page)

    result = await agent.consolidate("egp")
    written = store.read_page("egp").content
    assert not written.startswith("```")
    assert "```" not in written.splitlines()[-1]
    assert result.skipped is False
