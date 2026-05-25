# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Import a raw source file into the temporal-KB tier.

Used by both ``synthadoc kb import-source`` (CLI) and the orchestrator's
parallel-pass wire-up. Idempotent — same SHA-256 returns the existing
source id without copying or re-inserting.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from synthadoc.kb import frontmatter as fm
from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout


_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def date_from_filename(path: Path) -> Optional[str]:
    """Pull a ``YYYY-MM-DD`` prefix from *path*'s stem if present."""
    m = _DATE_PREFIX_RE.match(path.stem)
    return m.group(1) if m else None


_EXT_TO_SOURCE_TYPE = {
    ".md": "document", ".txt": "document",
    ".pdf": "document", ".docx": "document",
    ".pptx": "deck", ".xlsx": "document", ".csv": "document",
}


def guess_source_type(path: Path, default: str = "document") -> str:
    """Best-effort source_type from file extension."""
    return _EXT_TO_SOURCE_TYPE.get(path.suffix.lower(), default)


async def import_source(
    *,
    src_path: Path,
    source_type: str,
    db: KBDB,
    layout: KBLayout,
    title: Optional[str] = None,
    valid_on: Optional[str] = None,
    authority: str = "informal",
    author: Optional[str] = None,
) -> tuple[str, bool]:
    """Copy *src_path* into ``kb/sources/raw/`` and record it in kb.db.

    Returns ``(source_id, was_already_imported)``. If ``was_already_imported``
    is True, the existing source id is returned and nothing was copied or
    inserted (SHA-256 match on the existing row).

    Raises ``FileNotFoundError`` if *src_path* doesn't exist, ``ValueError``
    if *source_type* is unknown, and ``sqlite3.IntegrityError`` on race
    conditions (very rare; the disk copies are rolled back before re-raising).
    """
    src_path = Path(src_path)
    if not src_path.is_file():
        raise FileNotFoundError(f"source file not found: {src_path}")
    if source_type not in ids.SOURCE_TYPES:
        raise ValueError(
            f"unknown source_type {source_type!r}; "
            f"valid: {sorted(ids.SOURCE_TYPES)!r}"
        )

    digest = sha256_file(src_path)
    existing = await db.find_source_by_sha256(digest)
    if existing is not None:
        return existing["id"], True

    if valid_on is None:
        valid_on = date_from_filename(src_path) or datetime.now(
            timezone.utc).date().isoformat()
    if title is None:
        title = src_path.stem

    base_slug = ids.slugify(src_path.stem)
    base_id = ids.source_id(source_type, valid_on, base_slug)
    # Async collision check
    suffix_n = 1
    candidate = base_id
    while await db.source_exists(candidate):
        suffix_n += 1
        candidate = f"{base_id}-{suffix_n}"
    src_id = candidate
    final_slug = base_slug if suffix_n == 1 else f"{base_slug}-{suffix_n}"

    raw_target = layout.source_raw_path(
        source_type, f"{valid_on}-{final_slug}{src_path.suffix}"
    )
    layout.assert_not_raw_source(raw_target, allow_import=True)
    raw_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_path, raw_target)

    # v0.3 passthrough parser. A real per-format parser comes with Layer 3
    # (or sooner if a non-markdown source format becomes critical).
    parsed_target = layout.source_parsed_path(
        source_type, f"{valid_on}-{final_slug}{src_path.suffix}"
    )
    parsed_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(raw_target, parsed_target)

    created_at = datetime.fromtimestamp(
        src_path.stat().st_mtime, tz=timezone.utc
    ).isoformat()
    ingested_at = datetime.now(timezone.utc).isoformat()

    sidecar_data = fm.build_source(
        id=src_id,
        source_type=source_type,
        title=title,
        authority=authority,
        raw_path=str(raw_target.relative_to(layout.root)).replace("\\", "/"),
        parsed_path=str(parsed_target.relative_to(layout.root)).replace("\\", "/"),
        created_at=created_at,
        ingested_at=ingested_at,
        sha256=digest,
        author=author,
    )
    sidecar_path = parsed_target.with_suffix(parsed_target.suffix + ".meta.yaml")
    fm.write(sidecar_path, sidecar_data, "")

    try:
        await db.insert_source(
            id=src_id,
            source_type=source_type,
            title=title,
            authority=authority,
            raw_path=sidecar_data["raw_path"],
            parsed_path=sidecar_data["parsed_path"],
            created_at=created_at,
            ingested_at=ingested_at,
            sha256=digest,
            author=author,
        )
    except sqlite3.IntegrityError:
        # Lost a race — clean up our copies and let the caller decide.
        for p in (raw_target, parsed_target, sidecar_path):
            try:
                p.unlink()
            except OSError:
                pass
        raise

    return src_id, False
