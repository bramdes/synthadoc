# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Resolution rule loader for ``kb_config.yaml`` (plan §8, spec §6.4).

Rules say *how* the resolver collapses a list of timestamped facts of a given
type into a single current-state value. They are intentionally declarative —
the resolver itself stays pure and switches on ``strategy``.

Strategies (v0.3):

``latest_valid_at_wins``
    Pick the newest fact by ``(valid_at, observed_at)``, ignoring those
    below ``minimum_confidence`` or with ``review_status`` in
    ``exclude_review_status``. Older facts of the same type are marked
    superseded.

``append_only``
    Every fact is kept side-by-side. No supersession, no "current" value
    other than the full list. Used for milestones, decisions, closure
    events.

``requires_review``
    Never auto-resolve. Pending facts are surfaced in the review queue;
    no current-state value emitted by the resolver alone.

``open_until_closure_fact``
    A fact stays "open" until another fact (typically the matching
    ``*.closed`` fact_type) supersedes it. v0.3 treats this as
    append_only with a closure flag — proper pairing logic lands in
    Layer 3.

``open_until_resolved``
    Like ``open_until_closure_fact`` but for assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

import yaml

from synthadoc.kb.ids import FACT_TYPES


STRATEGIES = frozenset({
    "latest_valid_at_wins",
    "append_only",
    "requires_review",
    "open_until_closure_fact",
    "open_until_resolved",
})

# Ordering used to apply the minimum_confidence filter.
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


class RulesError(ValueError):
    """Raised when kb_config.yaml is malformed or references unknown types."""


@dataclass(frozen=True)
class FactRule:
    """One row from ``resolution_rules:``."""

    fact_type: str
    strategy: str
    minimum_confidence: str = "low"
    exclude_review_status: tuple[str, ...] = ()
    reversal_required: bool = False

    def confidence_ok(self, confidence: str) -> bool:
        return _CONFIDENCE_RANK.get(confidence, -1) >= _CONFIDENCE_RANK.get(
            self.minimum_confidence, 0
        )

    def review_status_ok(self, review_status: str) -> bool:
        return review_status not in self.exclude_review_status


@dataclass(frozen=True)
class Rules:
    """All resolution rules for one wiki."""

    by_fact_type: Mapping[str, FactRule] = field(default_factory=dict)

    def get(self, fact_type: str) -> Optional[FactRule]:
        return self.by_fact_type.get(fact_type)

    def require(self, fact_type: str) -> FactRule:
        rule = self.get(fact_type)
        if rule is None:
            raise RulesError(
                f"no resolution rule defined for fact_type={fact_type!r}; "
                f"add it to kb_config.yaml or skip the type"
            )
        return rule

    @property
    def fact_types(self) -> Iterable[str]:
        return self.by_fact_type.keys()


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def parse(text: str) -> Rules:
    """Parse a kb_config.yaml document into typed :class:`Rules`."""
    if not text.strip():
        return Rules(by_fact_type={})
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise RulesError(f"kb_config.yaml parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise RulesError("kb_config.yaml top-level must be a mapping")
    rr = data.get("resolution_rules", {})
    if not isinstance(rr, dict):
        raise RulesError("resolution_rules must be a mapping")

    rules: dict[str, FactRule] = {}
    for fact_type, raw in rr.items():
        if not isinstance(raw, dict):
            raise RulesError(
                f"resolution_rules[{fact_type!r}] must be a mapping, got {type(raw).__name__}"
            )
        rules[fact_type] = _build_rule(fact_type, raw)
    return Rules(by_fact_type=rules)


def load(path: Path) -> Rules:
    """Load and parse ``kb_config.yaml`` at *path*. Missing file → empty rules."""
    p = Path(path)
    if not p.exists():
        return Rules(by_fact_type={})
    return parse(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _build_rule(fact_type: str, raw: dict) -> FactRule:
    if fact_type not in FACT_TYPES:
        raise RulesError(
            f"unknown fact_type {fact_type!r} in resolution_rules; "
            f"valid: {sorted(FACT_TYPES)!r}"
        )
    strategy = raw.get("strategy")
    if strategy is None:
        raise RulesError(
            f"resolution_rules[{fact_type!r}] missing required field 'strategy'"
        )
    if strategy not in STRATEGIES:
        raise RulesError(
            f"resolution_rules[{fact_type!r}].strategy={strategy!r} not in {sorted(STRATEGIES)!r}"
        )
    min_conf = raw.get("minimum_confidence", "low")
    if min_conf not in _CONFIDENCE_RANK:
        raise RulesError(
            f"resolution_rules[{fact_type!r}].minimum_confidence={min_conf!r} "
            f"must be one of {sorted(_CONFIDENCE_RANK)!r}"
        )
    excluded = raw.get("exclude_review_status", []) or []
    if not isinstance(excluded, list) or not all(isinstance(x, str) for x in excluded):
        raise RulesError(
            f"resolution_rules[{fact_type!r}].exclude_review_status must be a list of strings"
        )
    return FactRule(
        fact_type=fact_type,
        strategy=strategy,
        minimum_confidence=min_conf,
        exclude_review_status=tuple(excluded),
        reversal_required=bool(raw.get("reversal_required", False)),
    )
