# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Delete one source and everything the fact tier derived from it.

Re-ingesting a *changed* file produces a new SHA-256, so the dedup check no
longer skips it — but the previous version's facts, decisions, summary and
source row are left behind, and re-ingest layers the new facts on top. Over
time that drifts. The only clean remedy used to be a full wipe + re-import of
the whole corpus, which re-runs LLM extraction for *every* source.

This module makes the cheap alternative possible: surgically remove a single
source's fact-tier artifacts, then re-resolve and re-render the entities it
touched. Both of those last two steps are **deterministic, no-LLM** (see
:func:`synthadoc.kb.resolver.resolve_db` and
:class:`synthadoc.agents.entity_render_agent.EntityRenderAgent`), so the only
LLM cost of a revert+re-ingest is re-extracting the *one* changed source.

Scope: this is the **fact tier** (``kb/`` + ``kb.db``) only. The page tier
(``wiki/``) merges content from many sources into shared pages and is not
cleanly reversible per source — periodic ``synthadoc consolidate`` curates it.
Audit-table (dedup) cleanup lives in the CLI wrapper, not here, because it is a
different database and concern.

Mirrors the patterns in :mod:`synthadoc.kb.review`: raw ``db.execute`` /
``db.fetchall``, markdown kept in sync with the DB, entities re-rendered after
the state change. Like the review actions, the cascade is a sequence of
short-lived transactions rather than one atomic write — that matches the rest
of the KB tier, and ``kb.db`` is rebuildable from the markdown if interrupted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from synthadoc.agents.entity_render_agent import EntityRenderAgent
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.resolver import resolve_db
from synthadoc.kb.rules import Rules

logger = logging.getLogger(__name__)


@dataclass
class DeleteResult:
    """What a :func:`delete_source` call removed/changed. Inspect after the call."""

    source_id: str
    sha256: Optional[str] = None
    facts_deleted: int = 0
    decisions_deleted: int = 0
    summaries_deleted: int = 0
    conclusions_deleted: int = 0
    unknowns_reopened: int = 0
    entities_rerendered: int = 0
    entities_deleted: int = 0
    files_deleted: int = 0
    warnings: list[str] = field(default_factory=list)


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


def _unlink(root: Path, rel_path: Optional[str], result: DeleteResult) -> None:
    """Best-effort delete of a fact-tier markdown file recorded in the DB.

    *rel_path* is relative to the wiki root (as stored in the ``path`` columns)
    unless already absolute. Missing files are not an error — the DB is
    authoritative and we are tearing it down anyway.
    """
    if not rel_path:
        return
    p = Path(rel_path)
    if not p.is_absolute():
        p = root / p
    try:
        if p.exists():
            p.unlink()
            result.files_deleted += 1
        # Prune now-empty parent dirs up to (but not including) the wiki root.
        parent = p.parent
        root_resolved = root.resolve()
        while parent != root_resolved and parent.is_dir():
            try:
                next(parent.iterdir())
                break  # not empty
            except StopIteration:
                parent.rmdir()
                parent = parent.parent
    except OSError as exc:
        result.warnings.append(f"could not delete {p}: {exc}")


async def _delete_links_for(db: KBDB, rel_paths: list[str]) -> None:
    """Remove links table rows that originate from or point at *rel_paths*."""
    for rp in rel_paths:
        await db.execute(
            "DELETE FROM links WHERE from_path = ? OR to_path = ?", (rp, rp)
        )


async def delete_source(
    db: KBDB, layout: KBLayout, rules: Rules, source_id: str
) -> DeleteResult:
    """Remove *source_id* and every fact-tier artifact derived from it.

    Steps, in foreign-key-safe order:

    1. Collect the source's facts / decisions / summaries and the entities
       they touch.
    2. Clear references *into* the doomed facts: un-supersede surviving facts,
       drop ``fact_supersession`` and ``conclusion_basis`` rows, reopen
       unknowns those facts had resolved.
    3. Delete the facts / decisions / summaries / source rows + their markdown,
       plus the raw/parsed/sidecar source files.
    4. Re-resolve the whole DB (recomputes supersession from what remains) and
       re-render each touched entity — or delete it if nothing references it
       any more.

    Raises ``KeyError`` if *source_id* is unknown.
    """
    source = await db.get_source(source_id)
    if source is None:
        raise KeyError(f"source {source_id!r} not found in kb.db")

    result = DeleteResult(source_id=source_id, sha256=source.get("sha256"))
    root = layout.root

    # 1. Collect what this source owns -------------------------------------
    fact_rows = await db.fetchall(
        "SELECT id, entity_id, path FROM facts WHERE source_id = ?", (source_id,)
    )
    fact_ids = [r["id"] for r in fact_rows]
    decision_rows = await db.fetchall(
        "SELECT id, entity_id, path FROM decisions WHERE source_id = ?", (source_id,)
    )
    summary_rows = await db.fetchall(
        "SELECT id, path FROM source_summaries WHERE source_id = ?", (source_id,)
    )

    touched_entities: set[str] = set()
    for r in fact_rows:
        if r.get("entity_id"):
            touched_entities.add(r["entity_id"])
    for r in decision_rows:
        if r.get("entity_id"):
            touched_entities.add(r["entity_id"])

    deleted_rel_paths: list[str] = []

    # 2. Clear references into the doomed facts ----------------------------
    if fact_ids:
        ph = _placeholders(len(fact_ids))

        # Surviving facts that pointed at a doomed fact must be un-superseded;
        # resolve_db only *adds* supersession links, it never clears stale ones.
        await db.execute(
            f"UPDATE facts SET superseded_by = NULL WHERE superseded_by IN ({ph})",
            tuple(fact_ids),
        )
        await db.execute(
            f"DELETE FROM fact_supersession "
            f"WHERE superseded_id IN ({ph}) OR superseder_id IN ({ph})",
            tuple(fact_ids) + tuple(fact_ids),
        )

        # Conclusions whose evidence included a doomed fact: drop the basis
        # rows; any conclusion left with no basis at all is now unsupported.
        affected_conclusions = await db.fetchall(
            f"SELECT DISTINCT conclusion_id FROM conclusion_basis "
            f"WHERE fact_id IN ({ph})",
            tuple(fact_ids),
        )
        await db.execute(
            f"DELETE FROM conclusion_basis WHERE fact_id IN ({ph})", tuple(fact_ids)
        )
        for c in affected_conclusions:
            cid = c["conclusion_id"]
            remaining = await db.fetchall(
                "SELECT COUNT(*) AS n FROM conclusion_basis WHERE conclusion_id = ?",
                (cid,),
            )
            if remaining and remaining[0]["n"] == 0:
                crow = await db.fetchall(
                    "SELECT path FROM conclusions WHERE id = ?", (cid,)
                )
                if crow and crow[0].get("path"):
                    deleted_rel_paths.append(crow[0]["path"])
                    _unlink(root, crow[0]["path"], result)
                await db.execute("DELETE FROM conclusions WHERE id = ?", (cid,))
                result.conclusions_deleted += 1

        # Unknowns these facts had resolved go back to open.
        reopened = await db.fetchall(
            f"SELECT id FROM unknowns WHERE resolved_by_fact_id IN ({ph})",
            tuple(fact_ids),
        )
        if reopened:
            await db.execute(
                f"UPDATE unknowns SET status = 'open', resolved_at = NULL, "
                f"resolved_by_fact_id = NULL WHERE resolved_by_fact_id IN ({ph})",
                tuple(fact_ids),
            )
            result.unknowns_reopened = len(reopened)

    # 3. Delete the owned rows (children before the source they reference) --
    for r in fact_rows:
        if r.get("path"):
            deleted_rel_paths.append(r["path"])
            _unlink(root, r["path"], result)
    for r in decision_rows:
        if r.get("path"):
            deleted_rel_paths.append(r["path"])
            _unlink(root, r["path"], result)
    for r in summary_rows:
        if r.get("path"):
            deleted_rel_paths.append(r["path"])
            _unlink(root, r["path"], result)

    await db.execute("DELETE FROM facts WHERE source_id = ?", (source_id,))
    await db.execute("DELETE FROM decisions WHERE source_id = ?", (source_id,))
    await db.execute("DELETE FROM source_summaries WHERE source_id = ?", (source_id,))
    result.facts_deleted = len(fact_rows)
    result.decisions_deleted = len(decision_rows)
    result.summaries_deleted = len(summary_rows)

    # Raw / parsed / sidecar source files (immutable-source guard is bypassed
    # here on purpose — this is the one legitimate teardown path).
    for key in ("raw_path", "parsed_path"):
        rel = source.get(key)
        _unlink(root, rel, result)
    if source.get("parsed_path"):
        _unlink(root, source["parsed_path"] + ".meta.yaml", result)

    await db.execute("DELETE FROM sources WHERE id = ?", (source_id,))

    # 4. Re-resolve, then re-render or drop each touched entity -------------
    await resolve_db(db, rules)

    render_agent = EntityRenderAgent(db=db, layout=layout, rules=rules)
    for entity_id in sorted(touched_entities):
        entity = await db.get_entity(entity_id)
        if entity is None:
            continue
        counts = await db.fetchall(
            "SELECT "
            "  (SELECT COUNT(*) FROM facts WHERE entity_id = :e) AS nf, "
            "  (SELECT COUNT(*) FROM decisions WHERE entity_id = :e) AS nd, "
            "  (SELECT COUNT(*) FROM unknowns WHERE entity_id = :e) AS nu",
            {"e": entity_id},
        )
        nf = counts[0]["nf"] if counts else 0
        nd = counts[0]["nd"] if counts else 0
        nu = counts[0]["nu"] if counts else 0

        if nf == 0 and nd == 0 and nu == 0:
            # Orphan — nothing references it any more. Tear it down.
            await _delete_orphan_entity(db, layout, entity, deleted_rel_paths, result)
        else:
            try:
                rr = await render_agent.render(entity_id)
                if rr.written or rr.queued_for_review:
                    result.entities_rerendered += 1
            except KeyError:
                pass

    # 5. Drop links rows for everything we removed -------------------------
    await _delete_links_for(db, deleted_rel_paths)

    return result


async def _delete_orphan_entity(
    db: KBDB,
    layout: KBLayout,
    entity: dict,
    deleted_rel_paths: list[str],
    result: DeleteResult,
) -> None:
    """Delete an entity that has no facts/decisions/unknowns left.

    Removes any remaining conclusions for it (FK children first), its index
    and history markdown, the entity row, and prunes the now-empty entity dir.
    """
    entity_id = entity["id"]
    try:
        # Conclusions are the only remaining FK children that reference the
        # entity directly; clear their basis rows then the rows themselves.
        conc = await db.fetchall(
            "SELECT id, path FROM conclusions WHERE entity_id = ?", (entity_id,)
        )
        for c in conc:
            await db.execute(
                "DELETE FROM conclusion_basis WHERE conclusion_id = ?", (c["id"],)
            )
            if c.get("path"):
                deleted_rel_paths.append(c["path"])
                _unlink(layout.root, c["path"], result)
            result.conclusions_deleted += 1
        await db.execute("DELETE FROM conclusions WHERE entity_id = ?", (entity_id,))

        et, slug = entity["entity_type"], entity["slug"]
        idx = layout.entity_index_path(et, slug)
        hist = layout.entity_history_path(et, slug)
        for page in (idx, hist):
            rel = str(page.relative_to(layout.root)).replace("\\", "/")
            deleted_rel_paths.append(rel)
            _unlink(layout.root, rel, result)

        await db.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
        result.entities_deleted += 1
    except Exception as exc:  # never let one stubborn orphan abort the revert
        result.warnings.append(f"could not delete orphan entity {entity_id}: {exc}")
