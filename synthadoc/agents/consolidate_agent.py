# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
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


_WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:[\|#][^\]]*)?\]\]")
_SOURCE_LINE_RE = re.compile(
    r"_—\s*Source:\s*(?P<label>.+?)\s*·\s*(?P<date>\d{4}-\d{2}-\d{2})\s*_"
)
# Fact-tier page types — consolidate refuses to rewrite these because they
# are deterministically rendered by the resolver/agent and any LLM edit
# would corrupt the source-of-truth in kb.db. Plan §14 Q4.
_FACT_TIER_TYPES = frozenset({
    "entity", "fact", "decision", "unknown", "history",
    "source", "source_summary", "derived_conclusion",
    "maintenance_report",
})
_TYPE_LINE_RE = re.compile(r"^type:\s*(\S+)\s*$", re.MULTILINE)


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
    backup_path: Optional[str] = None
    proposed_body: Optional[str] = None  # populated only on dry_run


class ConsolidateAgent:
    """Rewrites an accumulated wiki page into a curated, deduplicated form."""

    def __init__(self, provider: LLMProvider, store: WikiStorage,
                 search: HybridSearch,
                 wiki_root: Optional[Path] = None,
                 min_chars: int = 2000,
                 length_floor: float = 0.5) -> None:
        self._provider = provider
        self._store = store
        self._search = search
        self._wiki_root = Path(wiki_root) if wiki_root is not None else None
        self._min_chars = min_chars
        self._length_floor = length_floor

    async def consolidate(self, slug: str, force: bool = False,
                          dry_run: bool = False) -> ConsolidateResult:
        if not self._store.page_exists(slug):
            raise ValueError(f"Page not found: {slug}")

        page = self._store.read_page(slug)
        if page is None:
            raise ValueError(f"Page not readable: {slug}")

        fact_tier_type = self._fact_tier_type(slug)
        if fact_tier_type:
            return ConsolidateResult(
                slug=slug, skipped=True,
                skip_reason=(
                    f"refusing to consolidate fact-tier page (type: {fact_tier_type}) "
                    f"— these pages are deterministically rendered; re-run the kb "
                    f"pipeline or maintenance to refresh them"
                ),
                before_chars=len(page.content), after_chars=len(page.content),
            )

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

        new_body = self._strip_code_fences(resp.text.strip())

        if not new_body or len(new_body) < 200:
            raise RuntimeError(
                f"Consolidate produced empty/short output for {slug} "
                f"({len(new_body)} chars)"
            )

        self._verify_preservation(slug, before, new_body)

        if dry_run:
            return ConsolidateResult(
                slug=slug,
                before_chars=len(before),
                after_chars=len(new_body),
                tokens_used=resp.total_tokens,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                proposed_body=new_body,
            )

        backup_path = self._write_backup(slug, before)

        with self._store.page_lock(slug):
            page.content = new_body
            page.consolidated_hash = hashlib.sha256(
                new_body.encode("utf-8")
            ).hexdigest()
            self._store.write_page(slug, page)
            self._search.invalidate_index()

        logger.info(
            "consolidated %s: %d → %d chars (%d tokens, backup: %s)",
            slug, len(before), len(new_body), resp.total_tokens, backup_path,
        )
        return ConsolidateResult(
            slug=slug,
            before_chars=len(before),
            after_chars=len(new_body),
            tokens_used=resp.total_tokens,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            backup_path=str(backup_path) if backup_path else None,
        )

    def _fact_tier_type(self, slug: str) -> str | None:
        """Return the page's ``type:`` field if it identifies a fact-tier page.

        Reads the raw frontmatter block (first ``---``…``---`` fence) so this
        check works even if WikiStorage's parsed dataclass loses the field.
        Returns ``None`` for ordinary wiki pages (no `type:` key or a value
        outside :data:`_FACT_TIER_TYPES`).
        """
        path = self._store._find_existing_path(slug)
        if path is None:
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        if not text.startswith("---"):
            return None
        parts = text.split("---", 2)
        if len(parts) < 3:
            return None
        match = _TYPE_LINE_RE.search(parts[1])
        if not match:
            return None
        value = match.group(1).strip().strip("'\"")
        return value if value in _FACT_TIER_TYPES else None

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        """LLMs occasionally wrap output in ```markdown ... ``` fences; strip."""
        if not text.startswith("```"):
            return text
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()

    def _verify_preservation(self, slug: str, before: str, after: str) -> None:
        """Reject the rewrite if it dropped wikilinks, sources, or shrank too much."""
        old_links = set(_WIKILINK_RE.findall(before))
        new_links = set(_WIKILINK_RE.findall(after))
        missing_links = old_links - new_links
        if missing_links:
            raise RuntimeError(
                f"Consolidate dropped wikilinks for {slug}: "
                f"{sorted(missing_links)}"
            )

        old_sources = {(m.group("label"), m.group("date"))
                       for m in _SOURCE_LINE_RE.finditer(before)}
        new_sources = {(m.group("label"), m.group("date"))
                       for m in _SOURCE_LINE_RE.finditer(after)}
        missing_sources = old_sources - new_sources
        if missing_sources:
            raise RuntimeError(
                f"Consolidate dropped provenance source(s) for {slug}: "
                f"{sorted(missing_sources)}"
            )

        if len(before) > 0 and len(after) < len(before) * self._length_floor:
            raise RuntimeError(
                f"Consolidate over-shrank {slug}: "
                f"{len(after)} chars < {len(before)} × {self._length_floor:.2f} floor "
                f"({int(len(before) * self._length_floor)} min). The LLM likely "
                f"summarised instead of curating."
            )

    def _write_backup(self, slug: str, before: str) -> Optional[Path]:
        """Save the pre-consolidation body so a bad rewrite can be rolled back."""
        if self._wiki_root is None:
            return None
        backup_dir = self._wiki_root / ".synthadoc" / "consolidate-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = backup_dir / f"{slug}-{ts}.md"
        path.write_text(before, encoding="utf-8")
        return path
