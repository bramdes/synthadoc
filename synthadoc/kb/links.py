# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Wikilink extraction + persistence to the ``links`` table.

Two integration points (per the journal entry that scoped this):

1. **Per-render emission.** Render agents call :func:`emit_links` after
   writing a markdown body — links are replaced (delete-then-insert)
   for the rendered path so the table is always current.

2. **Full rescan.** :func:`relink_all` walks the entire ``kb/`` tree and
   rebuilds the table from scratch. Useful after manual edits, schema
   migrations, or a fresh checkout.

For v0.3 the only link kind we track is ``wikilink`` (``[[slug]]`` or
``[[slug|alias]]``). Source-id references and frontmatter pointers
remain implicit; add them as separate ``link_type`` rows when a
maintenance check needs them.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable

from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout

logger = logging.getLogger(__name__)

# Matches `[[slug]]` and `[[slug|alias]]`. Slugs may not contain `]`, `|`, or `#`.
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]*)?\]\]")

LINK_TYPE_WIKILINK = "wikilink"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract_wikilinks(body: str) -> list[str]:
    """Return the ordered list of slugs referenced via `[[slug]]` in *body*.

    Duplicates are preserved — callers that want unique targets should
    pass through :class:`dict.fromkeys` to deduplicate while preserving
    first-occurrence order.
    """
    if not body:
        return []
    return [m.group(1).strip() for m in _WIKILINK_RE.finditer(body)]


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


async def emit_links(
    db: KBDB,
    *,
    from_path: str,
    body: str,
) -> int:
    """Replace this file's wikilink rows with the links currently in *body*.

    *from_path* is stored verbatim — typically a project-root-relative
    POSIX-style path. Returns the number of rows inserted.
    """
    targets = list(dict.fromkeys(extract_wikilinks(body)))
    # Delete any prior wikilink rows for this source — keeps the table in sync
    # with the markdown body after every render.
    await db.execute(
        "DELETE FROM links WHERE from_path = ? AND link_type = ?",
        (from_path, LINK_TYPE_WIKILINK),
    )
    if not targets:
        return 0
    for target in targets:
        await db.execute(
            "INSERT OR IGNORE INTO links (from_path, to_path, link_type) "
            "VALUES (?, ?, ?)",
            (from_path, target, LINK_TYPE_WIKILINK),
        )
    return len(targets)


# ---------------------------------------------------------------------------
# Full rescan
# ---------------------------------------------------------------------------


async def relink_all(db: KBDB, layout: KBLayout) -> dict[str, int]:
    """Rebuild every wikilink row in the ``links`` table from on-disk markdown.

    Walks the ``kb/`` tree (skipping ``kb/sources/raw/`` and the
    ``maintenance/reports/`` archive), strips any frontmatter block, and
    re-emits links for each file. Returns a counts dict the CLI can print.

    Skipped paths:
      * ``kb/sources/raw/**`` — immutable; should not back-reference.
      * ``kb/maintenance/reports/**`` — historic snapshots; we never want
        broken-link reports there to count as "live" KB links.
    """
    scanned = 0
    files = 0
    emitted = 0
    # Wipe and rebuild wikilink rows under a single transaction-ish dance.
    await db.execute(
        "DELETE FROM links WHERE link_type = ?", (LINK_TYPE_WIKILINK,)
    )
    root = layout.root
    kb_root = layout.kb
    if not kb_root.exists():
        return {"files": 0, "links_emitted": 0, "skipped": 0}

    skip_prefixes = (
        (root / "kb" / "sources" / "raw").resolve(),
        (root / "kb" / "maintenance" / "reports").resolve(),
    )

    for path in sorted(kb_root.rglob("*.md")):
        resolved = path.resolve()
        scanned += 1
        if any(_is_under(resolved, p) for p in skip_prefixes):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("relink_all: could not read %s: %s", path, exc)
            continue
        body = _strip_frontmatter(text)
        rel = str(path.relative_to(root)).replace("\\", "/")
        emitted += await emit_links(db, from_path=rel, body=body)
        files += 1
    return {"files": files, "links_emitted": emitted,
            "skipped": scanned - files}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return text
    return parts[2].lstrip("\n")


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
