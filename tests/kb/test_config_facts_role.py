# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for the `facts` / `summary` agent roles added in Layer 2."""

from __future__ import annotations

from synthadoc.config import load_config


_BASE = """\
[agents]
default = {{ provider = "anthropic", model = "claude-opus-4-6" }}
{extras}
"""


def _cfg(tmp_path, extras: str = ""):
    p = tmp_path / "config.toml"
    p.write_text(_BASE.format(extras=extras), encoding="utf-8")
    return load_config(project_config=p)


def test_facts_role_falls_back_to_default_when_no_overrides(tmp_path):
    cfg = _cfg(tmp_path)
    facts = cfg.agents.resolve("facts")
    assert facts.provider == "anthropic"
    assert facts.model == "claude-opus-4-6"


def test_facts_role_inherits_ingest_when_set(tmp_path):
    cfg = _cfg(tmp_path, 'ingest = { model = "claude-sonnet-4-6" }')
    facts = cfg.agents.resolve("facts")
    # Should follow ingest, not default
    assert facts.model == "claude-sonnet-4-6"
    assert facts.provider == "anthropic"


def test_facts_role_explicit_override_wins(tmp_path):
    cfg = _cfg(
        tmp_path,
        'ingest = { model = "claude-sonnet-4-6" }\n'
        'facts  = { provider = "openai", model = "gpt-5" }',
    )
    facts = cfg.agents.resolve("facts")
    assert facts.provider == "openai"
    assert facts.model == "gpt-5"


def test_summary_role_also_inherits_ingest(tmp_path):
    cfg = _cfg(tmp_path, 'ingest = { model = "claude-sonnet-4-6" }')
    summary = cfg.agents.resolve("summary")
    assert summary.model == "claude-sonnet-4-6"


def test_classic_roles_unchanged_by_kb_additions(tmp_path):
    cfg = _cfg(tmp_path, 'lint = { model = "claude-haiku-4-5" }')
    lint = cfg.agents.resolve("lint")
    assert lint.model == "claude-haiku-4-5"
    # `query` falls back to default (not ingest) — same as before
    query = cfg.agents.resolve("query")
    assert query.model == "claude-opus-4-6"
