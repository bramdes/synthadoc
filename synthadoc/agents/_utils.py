# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 William Johnason / axoviq.com
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def load_user_context(wiki_root: Optional[Path]) -> str:
    """Combine AGENTS.md and wiki/purpose.md into a system-prompt string.

    Used by ingest and query agents so the LLM sees the wiki's audience,
    behavior guidelines, and scope on every personalized call. Re-read on
    each call so edits take effect without restarting the server.
    """
    if wiki_root is None:
        return ""
    parts = []
    agents = Path(wiki_root) / "AGENTS.md"
    if agents.exists():
        parts.append(agents.read_text(encoding="utf-8").strip())
    purpose = Path(wiki_root) / "wiki" / "purpose.md"
    if purpose.exists():
        parts.append(purpose.read_text(encoding="utf-8").strip())
    return "\n\n---\n\n".join(p for p in parts if p)


def parse_json_string_array(text: str, max_items: int) -> list[str] | None:
    """Strip code fences, parse a JSON array, return non-empty strings up to max_items.

    Returns None (never raises) when the text cannot be parsed as a non-empty
    JSON array of strings, so callers can fall back without a try/except.
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(l for l in lines if not l.strip().startswith("```")).strip()
    try:
        parts = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parts, list) or not parts:
        return None
    filtered = [str(q) for q in parts[:max_items] if str(q).strip()]
    return filtered or None
