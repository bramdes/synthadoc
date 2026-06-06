# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Broken-link detection (plan §4.10.13).

Reads every wikilink row from the ``links`` table and checks whether the
target slug matches a markdown file anywhere under ``kb/``. A broken
link is a target with **no** matching ``.md`` file.

The check is filesystem-walk-once + per-link hash lookup, so it is O(L)
in the number of links after an O(F) walk where F = file count.

Writes ``kb/maintenance/broken_links.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.links import LINK_TYPE_WIKILINK


REPORT_FILENAME = "broken_links.md"


@dataclass(frozen=True)
class BrokenLink:
    from_path: str
    to_path: str


@dataclass
class BrokenLinksResult:
    broken: list[BrokenLink] = field(default_factory=list)
    report_path: Path | None = None

    @property
    def count(self) -> int:
        return len(self.broken)


async def run(db: KBDB, layout: KBLayout) -> BrokenLinksResult:
    """Detect wikilinks whose target slug has no on-disk markdown file."""
    # Build the slug → path index in one walk. We also index full
    # project-root-relative paths (with and without the .md suffix) so that
    # path-style wikilinks — e.g. `[[kb/entities/projects/doc-intel/index]]`,
    # the only form that resolves to an entity page (all named index.md) — are
    # recognised instead of flagged as broken.
    slug_paths: dict[str, list[str]] = {}
    rel_paths: set[str] = set()

    def _index(path: Path) -> None:
        slug = path.stem
        rel = str(path.relative_to(layout.root)).replace("\\", "/")
        slug_paths.setdefault(slug, []).append(rel)
        rel_paths.add(rel)
        if rel.endswith(".md"):
            rel_paths.add(rel[:-3])

    if layout.kb.exists():
        for path in layout.kb.rglob("*.md"):
            _index(path)
    # The wiki/ tree also resolves slugs — readers care about either tree.
    wiki_dir = layout.root / "wiki"
    if wiki_dir.exists():
        for path in wiki_dir.rglob("*.md"):
            _index(path)

    rows = await db.fetchall(
        "SELECT from_path, to_path FROM links WHERE link_type = ? "
        "ORDER BY from_path, to_path",
        (LINK_TYPE_WIKILINK,),
    )
    broken = [
        BrokenLink(from_path=r["from_path"], to_path=r["to_path"])
        for r in rows
        if r["to_path"] not in slug_paths and r["to_path"] not in rel_paths
    ]

    path = layout.maintenance_dir / REPORT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render(broken), encoding="utf-8", newline="\n")
    return BrokenLinksResult(broken=broken, report_path=path)


def _render(broken: list[BrokenLink]) -> str:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# Broken Links",
        "",
        f"_Generated {ts} · {len(broken)} broken link(s)_",
        "",
    ]
    if not broken:
        lines.append("_No broken links._")
        lines.append("")
        return "\n".join(lines)
    lines.append(
        "These wikilinks point at slugs that do not match any markdown file "
        "under `kb/` or `wiki/`. Either fix the link, create the target page, "
        "or run `synthadoc kb relink` if you expect the links table to be stale."
    )
    lines.append("")
    lines.append("| From | Broken target |")
    lines.append("|---|---|")
    for b in broken:
        lines.append(f"| `{b.from_path}` | `[[{b.to_path}]]` |")
    lines.append("")
    return "\n".join(lines)
