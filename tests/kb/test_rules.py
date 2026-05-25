# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for synthadoc.kb.rules."""

from __future__ import annotations

import pytest

from synthadoc.kb import rules as r


_GOOD_YAML = """\
resolution_rules:
  project.status:
    strategy: latest_valid_at_wins
    minimum_confidence: medium
    exclude_review_status: [rejected]
  project.scope:
    strategy: requires_review
  decision.made:
    strategy: append_only
    reversal_required: true
"""


def test_parse_basic():
    rules = r.parse(_GOOD_YAML)
    assert "project.status" in rules.fact_types
    rule = rules.require("project.status")
    assert rule.strategy == "latest_valid_at_wins"
    assert rule.minimum_confidence == "medium"
    assert "rejected" in rule.exclude_review_status
    assert rule.reversal_required is False


def test_parse_appends_reversal_flag():
    rules = r.parse(_GOOD_YAML)
    assert rules.require("decision.made").reversal_required is True


def test_parse_empty_yields_empty_rules():
    rules = r.parse("")
    assert list(rules.fact_types) == []
    assert rules.get("project.status") is None


def test_parse_rejects_unknown_strategy():
    bad = "resolution_rules:\n  project.status:\n    strategy: magic\n"
    with pytest.raises(r.RulesError, match="strategy"):
        r.parse(bad)


def test_parse_rejects_unknown_fact_type():
    bad = "resolution_rules:\n  project.colour:\n    strategy: append_only\n"
    with pytest.raises(r.RulesError, match="fact_type"):
        r.parse(bad)


def test_parse_rejects_bad_min_confidence():
    bad = ("resolution_rules:\n"
           "  project.status:\n"
           "    strategy: latest_valid_at_wins\n"
           "    minimum_confidence: maybe\n")
    with pytest.raises(r.RulesError, match="minimum_confidence"):
        r.parse(bad)


def test_parse_rejects_bad_exclude_list():
    bad = ("resolution_rules:\n"
           "  project.status:\n"
           "    strategy: latest_valid_at_wins\n"
           "    exclude_review_status: rejected\n")  # str not list
    with pytest.raises(r.RulesError, match="exclude_review_status"):
        r.parse(bad)


def test_parse_rejects_missing_strategy():
    bad = "resolution_rules:\n  project.status:\n    minimum_confidence: high\n"
    with pytest.raises(r.RulesError, match="strategy"):
        r.parse(bad)


def test_load_missing_file_yields_empty(tmp_path):
    rules = r.load(tmp_path / "missing.yaml")
    assert list(rules.fact_types) == []


def test_require_unknown_raises():
    rules = r.parse(_GOOD_YAML)
    with pytest.raises(r.RulesError, match="no resolution rule"):
        rules.require("project.milestone")


def test_confidence_ok_ordering():
    rule = r.FactRule(fact_type="project.status",
                      strategy="latest_valid_at_wins",
                      minimum_confidence="medium")
    assert rule.confidence_ok("high")
    assert rule.confidence_ok("medium")
    assert not rule.confidence_ok("low")


def test_review_status_ok():
    rule = r.FactRule(fact_type="project.status",
                      strategy="latest_valid_at_wins",
                      exclude_review_status=("rejected",))
    assert rule.review_status_ok("unreviewed")
    assert rule.review_status_ok("reviewed")
    assert not rule.review_status_ok("rejected")
