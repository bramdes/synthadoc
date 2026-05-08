# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from synthadoc.agents._utils import load_user_context
from synthadoc.providers.base import LLMProvider, Message
from synthadoc.storage.search import HybridSearch
from synthadoc.storage.wiki import WikiStorage

logger = logging.getLogger(__name__)


_CONSOLIDATE_PROMPT = (
    "You are consolidating a wiki page that has accumulated multiple appended\n"
    "sections from different sources (meetings, articles). Rewrite the page body\n"
    "into a curated, topically-organized form.\n\n"
    "CRITICAL RULES:\n"
    "- Preserve EVERY `_— Source: ... · YYYY-MM-DD_` provenance line verbatim,\n"
    "  attached to the fact or section it belongs to. When a single fact came\n"
    "  from multiple sources, list all of them.\n"
    "- Preserve EVERY `[[slug]]` wikilink verbatim.\n"
    "- Deduplicate facts that repeat across sections (merge with all source lines).\n"
    "- Organize by topic (## headings), not chronologically.\n"
    "- Distinguish facts from opinions: prefix Bram's stated views with\n"
    "  `**Bram's read:**` or `**Bram thinks:**`. When attributing a view to\n"
    "  someone else, write `<Person> said: …` rather than asserting it as fact.\n"
    "- Do NOT lose any specific decisions, numbers, dates, action items, or quotes.\n"
    "- Do NOT invent facts not present in the source body.\n\n"
    "Return ONLY the new page body in Markdown — no explanation, no code fences.\n"
    "Start with the same `# <Title>` heading.\n\n"
    "Page slug: {slug}\n"
    "Title: {title}\n\n"
    "Current body:\n{content}\n"
)


@dataclass
class ConsolidateResult:
    slug: str
    skipped: bool = False
    skip_reason: str = ""
    before_chars: int = 0
    after_chars: int = 0
    tokens_used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class ConsolidateAgent:
    """Rewrites an accumulated wiki page into a curated, deduplicated form."""

    def __init__(self, provider: LLMProvider, store: WikiStorage,
                 search: HybridSearch,
                 wiki_root: Optional[Path] = None,
                 min_chars: int = 2000) -> None:
        self._provider = provider
        self._store = store
        self._search = search
        self._wiki_root = Path(wiki_root) if wiki_root is not None else None
        self._min_chars = min_chars

    async def consolidate(self, slug: str, force: bool = False) -> ConsolidateResult:
        if not self._store.page_exists(slug):
            raise ValueError(f"Page not found: {slug}")

        page = self._store.read_page(slug)
        if page is None:
            raise ValueError(f"Page not readable: {slug}")

        before = page.content
        before_hash = hashlib.sha256(before.encode("utf-8")).hexdigest()
        if not force and page.consolidated_hash == before_hash:
            return ConsolidateResult(
                slug=slug, skipped=True,
                skip_reason="no changes since last consolidation",
                before_chars=len(before), after_chars=len(before),
            )

        if len(before) < self._min_chars:
            return ConsolidateResult(
                slug=slug, skipped=True,
                skip_reason=f"page below {self._min_chars}-char threshold",
                before_chars=len(before), after_chars=len(before),
            )

        user_context = load_user_context(self._wiki_root)
        prompt = _CONSOLIDATE_PROMPT.format(
            slug=slug, title=page.title, content=before,
        )
        resp = await self._provider.complete(
            messages=[Message(role="user", content=prompt)],
            system=user_context or None,
            temperature=0.0,
        )

        new_body = resp.text.strip()
        # Defend against the LLM wrapping the result in code fences anyway.
        if new_body.startswith("```"):
            lines = new_body.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            new_body = "\n".join(lines).strip()

        if not new_body or len(new_body) < 200:
            raise RuntimeError(
                f"Consolidate produced empty/short output for {slug} "
                f"({len(new_body)} chars)"
            )

        # Provenance guard: refuse to write if every source footer disappeared.
        if "_— Source:" in before and "_— Source:" not in new_body:
            raise RuntimeError(
                f"Consolidate dropped all provenance footers for {slug}; "
                "refusing to write."
            )

        with self._store.page_lock(slug):
            page.content = new_body
            page.consolidated_hash = hashlib.sha256(
                new_body.encode("utf-8")
            ).hexdigest()
            self._store.write_page(slug, page)
            self._search.invalidate_index()

        logger.info(
            "consolidated %s: %d → %d chars (%d tokens)",
            slug, len(before), len(new_body), resp.total_tokens,
        )
        return ConsolidateResult(
            slug=slug,
            before_chars=len(before),
            after_chars=len(new_body),
            tokens_used=resp.total_tokens,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
        )
