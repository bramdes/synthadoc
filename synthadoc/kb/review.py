# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Human review actions for the temporal KB (plan §11, spec §7.9).

The fact tier extracts everything as ``review_status: unreviewed``. A human
then triages: reject a fact (resolves a conflict), merge duplicate entities,
or mark an entity's current state ``reviewed`` (which locks its page so future
contradicting facts are *queued* as proposals rather than silently applied).

Every action here updates **both** the working store (``kb.db``) **and** the
durable markdown frontmatter, because the markdown is the source of truth — a
``kb.db`` rebuilt from disk must reproduce the same review decisions. After a
state change we re-resolve the affected entity and re-render its page so the
effect is visible immediately.

This module is intentionally free of any CLI / typer concerns; the CLI in
:mod:`synthadoc.cli.kb` is a thin wrapper over these functions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from synthadoc.agents.entity_render_agent import EntityRenderAgent
from synthadoc.kb import frontmatter as fm
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.resolver import resolve_db
from synthadoc.kb.rules import Rules

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result + listing types
# ---------------------------------------------------------------------------


@dataclass
class ReviewResult:
    """Outcome of a single review action."""

    action: str                       # reject | accept | reopen | merge
    target_id: str
    detail: str = ""
    entity_rendered: Optional[str] = None
    queued_for_review: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class PendingReview:
    """Everything currently awaiting a human decision."""

    conflicts: list = field(default_factory=list)          # contradictions.Conflict
    duplicates: list = field(default_factory=list)         # duplicates.DuplicateCandidate
    facts_unreviewed: int = 0
    decisions_unreviewed: int = 0
    entities_unreviewed: int = 0
    review_queue_path: Optional[Path] = None
    review_queue_has_entries: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _patch_frontmatter(root: Path, rel_path: str | Path, updates: dict) -> bool:
    """Patch fields in a fact-tier markdown file's frontmatter, in place.

    *rel_path* is taken relative to the wiki root unless already absolute.
    Returns True on success, False if the file is missing or unparseable
    (the DB stays authoritative; the caller surfaces a warning).
    """
    p = Path(rel_path)
    if not p.is_absolute():
        p = root / p
    if not p.exists():
        return False
    try:
        parsed = fm.read(p)
    except Exception as exc:  # malformed frontmatter — don't crash the action
        logger.warning("could not patch frontmatter at %s: %s", p, exc)
        return False
    parsed.data.update(updates)
    fm.write(p, parsed.data, parsed.body)
    return True


async def _rerender_entity(
    db: KBDB, layout: KBLayout, rules: Rules, entity_id: str
):
    """Re-render one entity page; returns the agent's RenderResult or None."""
    agent = EntityRenderAgent(db=db, layout=layout, rules=rules)
    try:
        return await agent.render(entity_id)
    except KeyError:
        return None


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


async def gather_pending(db: KBDB, layout: KBLayout) -> PendingReview:
    """Collect everything awaiting review. Recomputes the conflict and
    duplicate reports as a side effect (keeps the markdown in sync)."""
    from synthadoc.kb.maintenance import contradictions, duplicates

    conf = await contradictions.run(db, layout)
    dup = await duplicates.run(db, layout)

    rows = await db.fetchall(
        "SELECT 'facts' AS t, COUNT(*) AS n FROM facts WHERE review_status='unreviewed' "
        "UNION ALL SELECT 'decisions', COUNT(*) FROM decisions WHERE review_status='unreviewed' "
        "UNION ALL SELECT 'entities', COUNT(*) FROM entities WHERE current_state_review_status='unreviewed'"
    )
    counts = {r["t"]: r["n"] for r in rows}

    queue_path = layout.maintenance_dir / "review_queue.md"
    has_entries = False
    if queue_path.exists():
        # A bare stub contains only the heading + one explanatory line.
        has_entries = "## Proposed update" in queue_path.read_text(encoding="utf-8")

    return PendingReview(
        conflicts=conf.conflicts,
        duplicates=dup.candidates,
        facts_unreviewed=counts.get("facts", 0),
        decisions_unreviewed=counts.get("decisions", 0),
        entities_unreviewed=counts.get("entities", 0),
        review_queue_path=queue_path if queue_path.exists() else None,
        review_queue_has_entries=has_entries,
    )


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


async def reject_fact(
    db: KBDB, layout: KBLayout, rules: Rules, fact_id: str
) -> ReviewResult:
    """Mark a fact ``rejected`` so the resolver ignores it everywhere, then
    re-resolve and re-render its entity. Raises ``KeyError`` if unknown."""
    fact = await db.get_fact(fact_id)
    if fact is None:
        raise KeyError(f"fact {fact_id!r} not found in kb.db")

    await db.execute(
        "UPDATE facts SET review_status='rejected' WHERE id=?", (fact_id,)
    )
    result = ReviewResult(
        action="reject",
        target_id=fact_id,
        detail=f"{fact['fact_type']} = {fact['value']}",
    )
    if not _patch_frontmatter(layout.root, fact["path"], {"review_status": "rejected"}):
        result.warnings.append(f"fact markdown not found at {fact['path']}")

    await resolve_db(db, rules)
    if fact.get("entity_id"):
        rr = await _rerender_entity(db, layout, rules, fact["entity_id"])
        result.entity_rendered = fact["entity_id"]
        result.queued_for_review = bool(rr and rr.queued_for_review)
    return result


async def accept_fact(
    db: KBDB, layout: KBLayout, rules: Rules, fact_id: str
) -> ReviewResult:
    """Mark a single fact ``reviewed`` (confirms it; does not lock the page)."""
    fact = await db.get_fact(fact_id)
    if fact is None:
        raise KeyError(f"fact {fact_id!r} not found in kb.db")
    await db.execute(
        "UPDATE facts SET review_status='reviewed' WHERE id=?", (fact_id,)
    )
    result = ReviewResult(
        action="accept-fact",
        target_id=fact_id,
        detail=f"{fact['fact_type']} = {fact['value']}",
    )
    if not _patch_frontmatter(layout.root, fact["path"], {"review_status": "reviewed"}):
        result.warnings.append(f"fact markdown not found at {fact['path']}")
    return result


async def set_entity_reviewed(
    db: KBDB, layout: KBLayout, rules: Rules, entity_id: str, *, reviewed: bool
) -> ReviewResult:
    """Lock (``reviewed``) or unlock (``unreviewed``) an entity's current state.

    Once reviewed, the render agent stops overwriting the page and instead
    appends future changes to the review queue. We render the page *before*
    flipping the flag so it reflects the latest resolved state, then patch the
    on-disk frontmatter to match.
    """
    entity = await db.get_entity(entity_id)
    if entity is None:
        raise KeyError(f"entity {entity_id!r} not found in kb.db")

    status = "reviewed" if reviewed else "unreviewed"
    now = _now() if reviewed else None

    await resolve_db(db, rules)
    if reviewed:
        # Free-write the current page first (entity is still unreviewed here).
        await _rerender_entity(db, layout, rules, entity_id)

    await db.execute(
        "UPDATE entities SET current_state_review_status=?, last_reviewed=? WHERE id=?",
        (status, now, entity_id),
    )
    idx = layout.entity_index_path(entity["entity_type"], entity["slug"])
    patch = {"current_state_review_status": status}
    if now is not None:
        patch["last_reviewed"] = now
    result = ReviewResult(
        action="accept" if reviewed else "reopen", target_id=entity_id,
        detail=entity.get("name", ""),
    )
    if not _patch_frontmatter(layout.root, idx, patch):
        result.warnings.append(f"entity page not found at {idx}")
    if not reviewed:
        # Now unreviewed → re-render so the page is a free write again.
        await _rerender_entity(db, layout, rules, entity_id)
    return result


async def merge_entity(
    db: KBDB, layout: KBLayout, rules: Rules, dup_id: str, into_id: str
) -> ReviewResult:
    """Merge *dup_id* into *into_id*: re-point its facts/decisions/unknowns to
    the keeper, mark the duplicate ``merged``, re-render the keeper, and leave
    a tombstone page behind. Raises ``KeyError``/``ValueError`` on bad input."""
    if dup_id == into_id:
        raise ValueError("cannot merge an entity into itself")
    dup = await db.get_entity(dup_id)
    if dup is None:
        raise KeyError(f"entity {dup_id!r} not found in kb.db")
    keeper = await db.get_entity(into_id)
    if keeper is None:
        raise KeyError(f"keeper entity {into_id!r} not found in kb.db")
    if keeper.get("merged_into") == dup_id:
        raise ValueError("refusing to merge: that would create a merge cycle")

    await db.execute("UPDATE facts SET entity_id=? WHERE entity_id=?", (into_id, dup_id))
    await db.execute("UPDATE decisions SET entity_id=? WHERE entity_id=?", (into_id, dup_id))
    await db.execute("UPDATE unknowns SET entity_id=? WHERE entity_id=?", (into_id, dup_id))
    await db.execute(
        "UPDATE entities SET merged_into=?, status='merged' WHERE id=?",
        (into_id, dup_id),
    )

    await resolve_db(db, rules)
    rr = await _rerender_entity(db, layout, rules, into_id)

    # Record the merge in the version-controlled alias file so it is durable:
    # a re-import (even after a wipe) re-applies it from the start.
    result = ReviewResult(
        action="merge", target_id=dup_id,
        detail=f"into {into_id} ({keeper.get('name', '')})",
        entity_rendered=into_id,
        queued_for_review=bool(rr and rr.queued_for_review),
    )
    try:
        from synthadoc.kb import aliases as _aliases
        _aliases.add_alias(
            layout.aliases_path,
            entity_type=dup["entity_type"],
            canonical_name=keeper.get("name") or keeper["slug"],
            alias_name=dup.get("name") or dup["slug"],
        )
    except Exception as exc:
        result.warnings.append(f"could not record alias: {exc}")
    try:
        idx = layout.entity_index_path(dup["entity_type"], dup["slug"])
        data = fm.build_entity(
            id=dup_id,
            entity_type=dup["entity_type"],
            status="merged",
            extra={"merged_into": into_id},
        )
        body = (
            f"# {dup.get('name', dup['slug'])}\n\n"
            f"> **Merged into {keeper.get('name', into_id)}** "
            f"(`{into_id}`).\n>\n"
            f"> This entity was a duplicate. Its facts now live on the keeper's page.\n"
        )
        fm.write(idx, data, body)
    except Exception as exc:
        result.warnings.append(f"could not write tombstone page: {exc}")
    return result
