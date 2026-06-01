# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Current-state resolver — pure function over the facts table.

The resolver answers: "given everything we currently know, what is the
current value of fact_type ``T`` for entity ``E``?" It is **deterministic**
— same facts + same rules → same answer. No LLM calls. Plan §8, spec §6.4.

Two entry points:

* :func:`resolve_facts` — pure, takes a list of fact dicts + Rules,
  returns a :class:`ResolvedState` per entity. Easy to unit-test.

* :func:`resolve_db` — convenience wrapper that loads facts from a
  :class:`KBDB` and writes back the supersession links derived from the
  resolution. Calling it twice on the same DB is a no-op.

The resolver does **not** delete facts. Older facts of the same type are
marked via :meth:`KBDB.mark_superseded`; they remain in the DB and on disk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from synthadoc.kb.rules import FactRule, Rules


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedValue:
    """The winning value for one (entity_id, fact_type) pair."""

    entity_id: str
    fact_type: str
    value: str
    valid_at: str
    confidence: str
    fact_id: str            # the fact that won
    superseded: tuple[str, ...] = ()   # IDs of facts the winner supersedes


@dataclass(frozen=True)
class AppendedSeries:
    """Append-only series (no winner) — every fact is kept."""

    entity_id: str
    fact_type: str
    fact_ids: tuple[str, ...]


@dataclass(frozen=True)
class PendingReview:
    """A type that requires review — surfaced but not auto-resolved."""

    entity_id: str
    fact_type: str
    fact_ids: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedState:
    """One entity's full resolved state."""

    entity_id: str
    current: Mapping[str, ResolvedValue] = field(default_factory=dict)
    appended: Mapping[str, AppendedSeries] = field(default_factory=dict)
    pending: Mapping[str, PendingReview] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure resolver
# ---------------------------------------------------------------------------


def resolve_facts(
    facts: Iterable[Mapping[str, Any]],
    rules: Rules,
    *,
    skip_unknown_types: bool = True,
) -> tuple[dict[str, ResolvedState], list[tuple[str, str]]]:
    """Resolve a flat list of fact rows into per-entity current state.

    Parameters
    ----------
    facts:
        Iterable of fact-row dicts. Each must carry at least
        ``id``, ``entity_id``, ``fact_type``, ``value``, ``valid_at``,
        ``observed_at``, ``confidence``, ``review_status``,
        ``superseded_by``. Extra fields are ignored.
    rules:
        Resolution rules loaded from ``kb_config.yaml``.
    skip_unknown_types:
        If True (default), facts whose ``fact_type`` is not in *rules* are
        silently ignored. If False, raises :class:`KeyError`.

    Returns
    -------
    (states, supersession_writes)
        ``states`` maps ``entity_id`` → :class:`ResolvedState`.
        ``supersession_writes`` is a list of
        ``(superseded_id, superseder_id)`` pairs the caller should persist
        via :meth:`KBDB.mark_superseded`. Pairs already present in the
        input (i.e. ``superseded_by`` already set) are excluded — the list
        is the *delta*.
    """
    # Group by (entity_id, fact_type)
    grouped: dict[tuple[str, str], list[dict]] = {}
    for f in facts:
        if f.get("review_status") == "rejected":
            # A human-rejected fact is evidence only. It must never participate
            # in resolution under ANY strategy — including append_only and
            # requires_review, which otherwise ignore review_status. This is
            # what makes `synthadoc kb review reject` effective everywhere.
            continue
        if not _is_active_input(f):
            # Facts already marked superseded in the DB are evidence, not
            # candidates. We still keep them in the grouped pool so a
            # subsequent run is idempotent.
            pass
        rule = rules.get(f["fact_type"])
        if rule is None and skip_unknown_types:
            continue
        grouped.setdefault((f["entity_id"], f["fact_type"]), []).append(dict(f))

    states: dict[str, ResolvedState] = {}
    writes: list[tuple[str, str]] = []

    for (entity_id, fact_type), group in grouped.items():
        rule = rules.require(fact_type)
        state = states.setdefault(entity_id, _empty_state(entity_id))
        if rule.strategy == "latest_valid_at_wins":
            _apply_latest_wins(state, fact_type, group, rule, writes)
        elif rule.strategy == "append_only":
            _apply_append_only(state, fact_type, group)
        elif rule.strategy == "requires_review":
            _apply_requires_review(state, fact_type, group)
        elif rule.strategy in ("open_until_closure_fact", "open_until_resolved"):
            # v0.3: treat as append_only — proper pairing arrives in Layer 3
            _apply_append_only(state, fact_type, group)
        else:  # pragma: no cover — guarded by Rules parser
            raise RuntimeError(f"unknown strategy {rule.strategy!r}")

    return states, writes


# ---------------------------------------------------------------------------
# DB convenience wrapper
# ---------------------------------------------------------------------------


async def resolve_db(db, rules: Rules) -> dict[str, ResolvedState]:
    """Run :func:`resolve_facts` over every fact in *db* and persist supersession.

    Returns the same ``states`` dict :func:`resolve_facts` returns.
    Calling twice is a no-op because supersession links are idempotent.
    """
    facts = await db.list_facts()
    states, writes = resolve_facts(facts, rules)
    for superseded_id, superseder_id in writes:
        await db.mark_superseded(superseded_id, superseder_id)
    return states


# ---------------------------------------------------------------------------
# Strategy implementations
# ---------------------------------------------------------------------------


def _apply_latest_wins(
    state: ResolvedState,
    fact_type: str,
    group: list[dict],
    rule: FactRule,
    writes: list[tuple[str, str]],
) -> None:
    """Pick the newest qualifying fact; mark older ones superseded."""
    # Eligible candidates: pass confidence and review_status gates
    eligible = [
        f for f in group
        if rule.confidence_ok(f.get("confidence", "low"))
        and rule.review_status_ok(f.get("review_status", "unreviewed"))
    ]
    if not eligible:
        return
    # Newest by (valid_at, observed_at); ties broken by id for determinism.
    eligible.sort(
        key=lambda f: (f.get("valid_at", ""), f.get("observed_at", ""), f.get("id", "")),
        reverse=True,
    )
    winner = eligible[0]
    loser_ids = []
    for f in group:
        if f["id"] == winner["id"]:
            continue
        loser_ids.append(f["id"])
        # Only emit a write if the DB doesn't already record the relationship
        existing = f.get("superseded_by")
        if existing != winner["id"]:
            writes.append((f["id"], winner["id"]))

    current = dict(state.current)
    current[fact_type] = ResolvedValue(
        entity_id=state.entity_id,
        fact_type=fact_type,
        value=winner["value"],
        valid_at=winner.get("valid_at", ""),
        confidence=winner.get("confidence", ""),
        fact_id=winner["id"],
        superseded=tuple(loser_ids),
    )
    # ResolvedState is frozen — rebuild it
    object.__setattr__(state, "current", current)


def _apply_append_only(
    state: ResolvedState,
    fact_type: str,
    group: list[dict],
) -> None:
    ids_sorted = tuple(
        f["id"] for f in sorted(group, key=lambda f: (f.get("valid_at", ""), f.get("id", "")))
    )
    appended = dict(state.appended)
    appended[fact_type] = AppendedSeries(
        entity_id=state.entity_id,
        fact_type=fact_type,
        fact_ids=ids_sorted,
    )
    object.__setattr__(state, "appended", appended)


def _apply_requires_review(
    state: ResolvedState,
    fact_type: str,
    group: list[dict],
) -> None:
    ids_sorted = tuple(
        f["id"] for f in sorted(group, key=lambda f: (f.get("valid_at", ""), f.get("id", "")))
    )
    pending = dict(state.pending)
    pending[fact_type] = PendingReview(
        entity_id=state.entity_id,
        fact_type=fact_type,
        fact_ids=ids_sorted,
    )
    object.__setattr__(state, "pending", pending)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_state(entity_id: str) -> ResolvedState:
    # ResolvedState is frozen, but we want to mutate its mappings during
    # accumulation. The dataclass with field default_factory gives us a fresh
    # dict per instance, so we use object.__setattr__ in the apply_* funcs.
    return ResolvedState(entity_id=entity_id, current={}, appended={}, pending={})


def _is_active_input(f: Mapping[str, Any]) -> bool:
    return f.get("superseded_by") is None
