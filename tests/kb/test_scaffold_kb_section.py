# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for the ScaffoldAgent kb-tier extension (plan §14 Q5)."""

from __future__ import annotations

import json

import pytest

from synthadoc.agents.scaffold_agent import ScaffoldAgent
from tests.kb._fake_provider import FakeProvider


_CANNED = json.dumps({
    "categories": [
        {"heading": "Topics", "description": "Main topics", "slugs": []},
    ],
    "agents_guidelines": "Cite sources. Be terse.",
    "purpose_include": "Things about X",
    "purpose_exclude": "Things about Y",
    "dashboard_intro": "A wiki for X.",
})


async def test_scaffold_default_omits_temporal_section():
    provider = FakeProvider(); provider.enqueue(_CANNED)
    agent = ScaffoldAgent(provider=provider)
    result = await agent.scaffold(domain="X")
    assert "Temporal KB Rules" not in result.agents_md
    assert "kb/sources/raw/" not in result.agents_md


async def test_scaffold_kb_initialized_emits_temporal_section():
    provider = FakeProvider(); provider.enqueue(_CANNED)
    agent = ScaffoldAgent(provider=provider)
    result = await agent.scaffold(domain="X", kb_initialized=True)
    assert "Temporal KB Rules" in result.agents_md
    # The section must mention key kb paths so navigating agents can find them
    assert "kb/sources/raw/" in result.agents_md
    assert "kb/entities/" in result.agents_md
    assert "kb/maintenance/" in result.agents_md
    # And reference key rules from spec §3
    assert "superseded_by" in result.agents_md
    assert "review_queue.md" in result.agents_md


async def test_scaffold_kb_section_appears_after_query_guidelines():
    provider = FakeProvider(); provider.enqueue(_CANNED)
    agent = ScaffoldAgent(provider=provider)
    result = await agent.scaffold(domain="X", kb_initialized=True)
    qg_pos = result.agents_md.index("## Query Guidelines")
    tk_pos = result.agents_md.index("## Temporal KB Rules")
    assert tk_pos > qg_pos


async def test_scaffold_other_outputs_unchanged_by_kb_flag():
    """index_md / purpose_md / dashboard_intro must NOT change based on the kb flag."""
    p1 = FakeProvider(); p1.enqueue(_CANNED)
    p2 = FakeProvider(); p2.enqueue(_CANNED)
    a = ScaffoldAgent(provider=p1)
    b = ScaffoldAgent(provider=p2)
    no_kb = await a.scaffold(domain="X")
    with_kb = await b.scaffold(domain="X", kb_initialized=True)
    assert no_kb.index_md == with_kb.index_md
    assert no_kb.purpose_md == with_kb.purpose_md
    assert no_kb.dashboard_intro == with_kb.dashboard_intro
