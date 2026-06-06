# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""EntityRenderAgent — deterministic render of entity index pages from facts.

Plan §9 Layer 2, spec §4.6. Given an ``entity_id``, this agent:

1. Loads the entity row and every fact for that entity.
2. Runs them through the pure :func:`synthadoc.kb.resolver.resolve_facts`
   to derive the current state.
3. Renders ``kb/entities/<type>/<slug>/index.md`` from a template that
   matches the spec §4.6 example.
4. If the entity's ``current_state_review_status == "reviewed"``, writes
   a *proposal* section to ``kb/maintenance/review_queue.md`` instead
   of overwriting the index page (spec §7.9, plan §11).

The agent makes **no LLM calls**. Pure render. That is why it lives in
``agents/`` only by convention — it is a render, but treating it as an
agent keeps the job-type taxonomy consistent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from synthadoc.kb import frontmatter as fm
from synthadoc.kb import ids
from synthadoc.kb import relations as _relations
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.links import emit_links
from synthadoc.kb.resolver import ResolvedState, resolve_facts
from synthadoc.kb.rules import Rules

logger = logging.getLogger(__name__)

REVIEW_QUEUE_FILENAME = "review_queue.md"


@dataclass
class RenderResult:
    entity_id: str
    path: Path
    written: bool                # False if no facts → nothing to render
    queued_for_review: bool      # True if entity is reviewed and a proposal was queued


class EntityRenderAgent:
    """Pure-ish render. No LLM calls; just SQL + templating."""

    def __init__(
        self,
        *,
        db: KBDB,
        layout: KBLayout,
        rules: Rules,
    ) -> None:
        self._db = db
        self._layout = layout
        self._rules = rules
        # Declared parent/sub-area relationships (kb_relations.yaml). Reloaded
        # per agent construction so hand-edits take effect on the next render.
        self._relations = _relations.load(layout.relations_path)

    # ------------------------------------------------------------------

    async def render(self, entity_id: str) -> RenderResult:
        """Render (or propose) the entity page for *entity_id*.

        Raises ``KeyError`` if the entity is unknown.
        """
        entity = await self._db.get_entity(entity_id)
        if entity is None:
            raise KeyError(f"entity {entity_id!r} not found in kb.db")

        # Rejected facts are kept in the DB as evidence but must not appear in
        # any rendered section (current state, history, or recent changes).
        facts = [
            f for f in await self._db.list_facts(entity_id=entity_id)
            if f.get("review_status") != "rejected"
        ]
        if not facts:
            return RenderResult(
                entity_id=entity_id,
                path=self._entity_path(entity),
                written=False,
                queued_for_review=False,
            )

        states, _writes = resolve_facts(facts, self._rules)
        state = states.get(entity_id, ResolvedState(entity_id=entity_id))

        target = self._entity_path(entity)
        review_status = entity.get("current_state_review_status", "unreviewed")

        if review_status == "reviewed":
            queued_path = await self._queue_proposal(entity, state, facts)
            logger.info("queued proposal for reviewed entity %s at %s",
                        entity_id, queued_path)
            return RenderResult(
                entity_id=entity_id,
                path=target,
                written=False,
                queued_for_review=True,
            )

        # Free write — entity hasn't been reviewed yet.
        part_of, subareas = await self._relationship_links(entity)
        body = _render_body(
            entity=entity, state=state, facts=facts,
            part_of=part_of, subareas=subareas,
        )
        data = fm.build_entity(
            id=entity_id,
            entity_type=entity["entity_type"],
            status=entity.get("status") or "active",
            current_state_review_status=review_status,
            last_rebuilt=datetime.now(timezone.utc).isoformat(),
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        fm.write(target, data, body)

        # Update the DB to record the rebuild timestamp
        await self._db.execute(
            "UPDATE entities SET last_rebuilt = ? WHERE id = ?",
            (data["last_rebuilt"], entity_id),
        )

        # Refresh the links table so broken-link maintenance stays accurate
        rel_path = str(target.relative_to(self._layout.root)).replace("\\", "/")
        await emit_links(self._db, from_path=rel_path, body=body)

        return RenderResult(
            entity_id=entity_id,
            path=target,
            written=True,
            queued_for_review=False,
        )

    # ------------------------------------------------------------------

    def _entity_path(self, entity: dict) -> Path:
        return self._layout.entity_index_path(entity["entity_type"], entity["slug"])

    def _entity_link(self, entity_type: str, slug: str, name: str) -> str:
        """A path-based wikilink to an entity's index page.

        Entity pages are all named ``index.md``, so a basename ``[[slug]]``
        link is ambiguous and won't resolve. A path link with a display alias
        navigates correctly in Obsidian (and `broken_links` understands the
        path form)."""
        rel = str(
            self._layout.entity_index_path(entity_type, slug)
            .relative_to(self._layout.root)
        ).replace("\\", "/")
        if rel.endswith(".md"):
            rel = rel[:-3]
        return f"[[{rel}|{name}]]"

    async def _display_name(self, entity_type: str, slug: str) -> str:
        """Display name for a (type, slug) — the entity's name if it exists,
        else a title-cased fall-back from the slug."""
        row = await self._db.get_entity(ids.entity_id(entity_type, slug))
        if row and row.get("name"):
            return row["name"]
        return slug.replace("-", " ")

    async def _relationship_links(self, entity: dict) -> tuple[Optional[str], list[str]]:
        """Return (part_of_link, [subarea_links]) for this entity from the
        declared kb_relations.yaml map."""
        etype = entity["entity_type"]
        slug = entity["slug"]
        part_of = None
        parent_slug = self._relations.parent_of(etype, slug)
        if parent_slug:
            part_of = self._entity_link(
                etype, parent_slug, await self._display_name(etype, parent_slug)
            )
        subareas = []
        for child_slug in self._relations.children_of(etype, slug):
            subareas.append(
                self._entity_link(
                    etype, child_slug, await self._display_name(etype, child_slug)
                )
            )
        return part_of, subareas

    async def _queue_proposal(
        self, entity: dict, state: ResolvedState, facts: list[dict],
    ) -> Path:
        target = self._layout.maintenance_dir / REVIEW_QUEUE_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(
                "# Review Queue\n\n"
                "Proposed updates to reviewed entity pages are appended here.\n",
                encoding="utf-8", newline="\n",
            )
        block = _render_proposal_block(entity=entity, state=state, facts=facts)
        with open(target, "a", encoding="utf-8", newline="\n") as f:
            f.write(block)
        return target


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _render_body(
    *, entity: dict, state: ResolvedState, facts: list[dict],
    part_of: Optional[str] = None, subareas: Optional[list[str]] = None,
) -> str:
    name = entity["name"]
    fact_by_id = {f["id"]: f for f in facts}
    lines: list[str] = [f"# {name}", ""]

    # Part of — parent entity, when this entity is a declared sub-area.
    if part_of:
        lines.append(f"_Part of {part_of}_")
        lines.append("")

    # Summary — one-line synthesis from current state
    lines.append("## Current Summary")
    lines.append("")
    summary_line = _summary_line(state)
    lines.append(summary_line or "_No resolved current state yet._")
    lines.append("")

    # Current State table — every fact_type with a resolved value
    lines.append("## Current State")
    lines.append("")
    lines.append("| Field | Value | As Of | Confidence | Evidence |")
    lines.append("|---|---|---|---|---|")
    if state.current:
        for fact_type, resolved in sorted(state.current.items()):
            evidence = fact_by_id.get(resolved.fact_id)
            evidence_link = (
                f"[[{Path(evidence['path']).stem}]]" if evidence else resolved.fact_id
            )
            lines.append(
                f"| {fact_type} | {resolved.value} | {resolved.valid_at} | "
                f"{resolved.confidence} | {evidence_link} |"
            )
    else:
        lines.append("| _none_ | | | | |")
    lines.append("")

    # Sub-areas — child entities declared in kb_relations.yaml.
    if subareas:
        lines.append("## Sub-areas")
        lines.append("")
        for link in subareas:
            lines.append(f"- {link}")
        lines.append("")

    # Append-only series — milestones, decisions made, etc.
    if state.appended:
        lines.append("## History (append-only series)")
        lines.append("")
        for fact_type, series in sorted(state.appended.items()):
            lines.append(f"### {fact_type}")
            lines.append("")
            for fid in series.fact_ids:
                f = fact_by_id.get(fid)
                if not f:
                    continue
                lines.append(
                    f"- **{f['valid_at']}** — {f['value']} "
                    f"(`{Path(f['path']).stem}`)"
                )
            lines.append("")

    # Pending review — requires_review strategy
    if state.pending:
        lines.append("## Pending Review")
        lines.append("")
        for fact_type, pending in sorted(state.pending.items()):
            lines.append(f"### {fact_type}")
            lines.append("")
            for fid in pending.fact_ids:
                f = fact_by_id.get(fid)
                if not f:
                    continue
                lines.append(
                    f"- **{f['valid_at']}** — {f['value']} "
                    f"({f['confidence']} confidence, `{Path(f['path']).stem}`)"
                )
            lines.append("")

    # Recent changes — last 5 active (non-superseded) facts by observed_at
    recent = sorted(
        (f for f in facts if f.get("superseded_by") is None),
        key=lambda f: f.get("observed_at", ""),
        reverse=True,
    )[:5]
    lines.append("## Recent Changes")
    lines.append("")
    if recent:
        for f in recent:
            lines.append(
                f"- {f.get('observed_at', '')[:10]}: "
                f"{f['fact_type']} = {f['value']} "
                f"(valid {f['valid_at']})"
            )
    else:
        lines.append("_No recent changes._")
    lines.append("")
    return "\n".join(lines)


def _summary_line(state: ResolvedState) -> str:
    """Best-effort one-line summary built from the resolved current values."""
    parts = []
    for fact_type, rv in sorted(state.current.items()):
        parts.append(f"{fact_type} = **{rv.value}** (as of {rv.valid_at})")
    return "; ".join(parts)


def _render_proposal_block(
    *, entity: dict, state: ResolvedState, facts: list[dict],
) -> str:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    name = entity["name"]
    eid = entity["id"]
    lines = [
        "",
        "---",
        "",
        f"## Proposed update — {name}",
        "",
        f"_Generated {ts} · entity_id `{eid}` · status: reviewed (proposal not auto-applied)_",
        "",
        _summary_line(state) or "_No resolved current state._",
        "",
        f"Fact count: {len(facts)}",
    ]
    if state.current:
        lines.append("")
        lines.append("Current resolved values:")
        for fact_type, rv in sorted(state.current.items()):
            lines.append(f"- `{fact_type}` = {rv.value} (as of {rv.valid_at}, "
                         f"evidence `{rv.fact_id}`)")
    lines.append("")
    return "\n".join(lines)
