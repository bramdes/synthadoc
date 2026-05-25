# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for the truncated-fence-tolerant `_strip_fences` helper.

Gemini and similar models sometimes wrap JSON responses in ```json``` fences
and then run up against the max_tokens cap, leaving the closing fence (and
sometimes the trailing JSON) cut off. The fence-strip logic must handle:

  * matched pair (the common case)
  * opening-only (truncated before closer)
  * no fence at all
"""

from __future__ import annotations

import pytest

from synthadoc.agents import (
    decision_extract_agent as DA,
    fact_extract_agent as FA,
    source_summary_agent as SA,
    unknown_extract_agent as UA,
)


_AGENTS = pytest.mark.parametrize(
    "strip", [FA._strip_fences, SA._strip_fences,
              DA._strip_fences, UA._strip_fences],
)


@_AGENTS
def test_no_fence_returns_unchanged(strip):
    raw = '{"a": 1}'
    assert strip(raw).strip() == raw


@_AGENTS
def test_matched_pair_returns_inner(strip):
    raw = '```json\n{"a": 1}\n```'
    assert strip(raw).strip() == '{"a": 1}'


@_AGENTS
def test_matched_pair_without_lang_marker(strip):
    raw = '```\n{"a": 1}\n```'
    assert strip(raw).strip() == '{"a": 1}'


@_AGENTS
def test_opening_only_strips_opener(strip):
    """Truncated mid-content: opener present, no closer."""
    raw = '```json\n{"a": 1, "b": "trunc'
    out = strip(raw)
    assert out.startswith('{')
    assert "```" not in out
    assert "trunc" in out


@_AGENTS
def test_opening_only_uppercase_lang_marker(strip):
    raw = '```JSON\n{"a": 1'
    out = strip(raw)
    assert out.startswith('{')


@_AGENTS
def test_leading_whitespace_handled(strip):
    raw = '   ```json\n{"a": 1}\n```   '
    assert strip(raw).strip() == '{"a": 1}'
