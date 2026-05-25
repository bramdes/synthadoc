# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""HistoryRenderAgent — deterministic per-entity history page (spec §4.7).

Renders ``kb/entities/<type>/<slug>/history.md`` from the full fact log
(including superseded facts — they're still evidence of historical
state). The page is a human-readable synthesis of how the entity evolved
over time.

No LLM calls. Pure SQL + templating. The render is overwrite-style so
every run reflects current DB state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from synthadoc.kb import frontmatter as fm
from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.links import emit_links

logger = logging.getLogger(__name__)


@dataclass
class HistoryRenderResult:
    entity_id: str
    path: Path
    written: bool                # False if entity has zero facts → nothing to render
    fact_types_covered: int = 0
    total_facts: int = 0


class HistoryRenderAgent:
    """Pure render of an entity's history.md."""

    def __init__(self, *, db: KBDB, layout: KBLayout) -> None:
        self._db = db
        self._layout = layout

    async def render(self, entity_id: str) -> HistoryRenderResult:
        entity = await self._db.get_entity(entity_id)
        if entity is None:
            raise KeyError(f"entity {entity_id!r} not found in kb.db")

        facts = await self._db.list_facts(entity_id=entity_id)
        target = self._layout.entity_history_path(
            entity["entity_type"], entity["slug"],
        )
        if not facts:
            return HistoryRenderResult(
                entity_id=entity_id, path=target, written=False,
            )

        # Group by fact_type, sorted within each by valid_at ascending so the
        # earliest known state appears first (matches spec §4.7 example).
        by_type: dict[str, list[dict]] = {}
        for f in facts:
            by_type.setdefault(f["fact_type"], []).append(f)
        for group in by_type.values():
            group.sort(key=lambda f: (f.get("valid_at", ""), f.get("observed_at", "")))

        body = _render_body(entity=entity, by_type=by_type, total=len(facts))
        data = fm.build_history(
            id=ids.history_id(entity_id),
            entity_id=entity_id,
            last_rebuilt=datetime.now(timezone.utc).isoformat(),
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        fm.write(target, data, body)

        rel_path = str(target.relative_to(self._layout.root)).replace("\\", "/")
        await emit_links(self._db, from_path=rel_path, body=body)

        return HistoryRenderResult(
            entity_id=entity_id,
            path=target,
            written=True,
            fact_types_covered=len(by_type),
            total_facts=len(facts),
        )


# ---------------------------------------------------------------------------
# Templating
# ---------------------------------------------------------------------------


def _render_body(*, entity: dict, by_type: dict[str, list[dict]], total: int) -> str:
    name = entity["name"]
    lines: list[str] = [
        f"# {name} — History",
        "",
        f"_Synthesised from {total} fact(s) across "
        f"{len(by_type)} fact type(s)_",
        "",
    ]
    for fact_type in sorted(by_type):
        lines.append(f"## {fact_type}")
        lines.append("")
        for f in by_type[fact_type]:
            stamp = f.get("valid_at", "")
            value = f.get("value", "")
            stem = Path(f.get("path", "")).stem or f.get("id", "")
            extras = []
            if f.get("superseded_by"):
                extras.append(f"superseded by `{f['superseded_by']}`")
            if f.get("review_status") == "rejected":
                extras.append("rejected")
            suffix = f" _({'; '.join(extras)})_" if extras else ""
            lines.append(
                f"### {stamp}"
            )
            lines.append("")
            lines.append(f"Value: **{value}**{suffix}")
            quote = f.get("source_quote")
            if quote:
                lines.append("")
                lines.append(f"> {quote}")
            lines.append("")
            lines.append(
                f"Evidence: [[{stem}]] · source `{f.get('source_id', '')}` · "
                f"confidence {f.get('confidence', '')}"
            )
            lines.append("")
    return "\n".join(lines)
