# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from synthadoc.agents._utils import load_user_context
from synthadoc.agents.search_decompose_agent import SearchDecomposeAgent
from synthadoc.agents.skill_agent import SkillAgent
from synthadoc.core.cache import CACHE_VERSION, CacheManager, make_cache_key
from synthadoc.providers.base import LLMProvider, Message
from synthadoc.storage.log import AuditDB, LogWriter
from synthadoc.storage.search import HybridSearch
from synthadoc.storage.wiki import WikiPage, WikiStorage

logger = logging.getLogger(__name__)

from synthadoc.skills.web_search.scripts.main import _INTENT_RE as _WEB_INTENT_RE
from synthadoc.agents.lint_agent import LINT_SKIP_SLUGS


@dataclass
class IngestResult:
    source: str
    pages_created: list[str] = field(default_factory=list)
    pages_updated: list[str] = field(default_factory=list)
    pages_flagged: list[str] = field(default_factory=list)
    child_sources: list[str] = field(default_factory=list)
    tokens_used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cache_hits: int = 0
    skipped: bool = False
    skip_reason: str = ""


_ANALYSIS_PROMPT = (
    "Extract key entities and tags from the source text below. These will seed a BM25\n"
    "search to find related wiki pages, so prefer specific named entities (people,\n"
    "projects, organisations) over generic words.\n"
    "Return ONLY valid JSON with no markdown fences:\n"
    '{"entities": [...], "tags": [...]}\n'
    "Keep each list under 10 items.\n\n"
)

_DECISION_PROMPT = (
    "You maintain a knowledge wiki. A new source document has arrived. Identify EVERY\n"
    "distinct subject discussed and produce one action per subject. The wiki is keyed\n"
    "by long-lived topics, so a single meeting transcript typically fans out into\n"
    "multiple actions targeting different project / person / issue pages.\n\n"
    "Return ONLY valid JSON — no markdown fences, no explanation outside the JSON.\n\n"
    "SLUG RULES (CRITICAL):\n"
    "- A slug names a long-lived subject: a project/program, a person/stakeholder,\n"
    "  or a recurring issue/topic. Examples: 'egp', 'dong-tao', 'api-key-governance'.\n"
    "- NEVER use a meeting filename, date, time, or 'meeting-with-X' as a slug.\n"
    "- Slugs must NOT begin with a date pattern (YYYY-MM-DD) — those are rejected.\n"
    "- Slugs must NOT include 'meeting', 'sync', 'standup', 'review-with-' as the\n"
    "  primary topic — those describe the event, not the subject.\n\n"
    "WIKILINKS: When writing update_content or page_content, cross-reference related\n"
    "topics using [[slug]] notation. Only link to slugs that exist in the wiki list\n"
    "below or that you are creating in this same response.\n\n"
    "Per-action rules:\n\n"
    "FLAG — if the source DISPUTES a factual claim in an existing page:\n"
    "  {{\"action\":\"flag\", \"target\":\"existing-slug\"}}\n\n"
    "UPDATE — if the source adds info about a subject ALREADY covered by an existing page:\n"
    "  {{\"action\":\"update\", \"target\":\"existing-slug\",\n"
    "    \"update_content\":\"## <descriptive section heading> (YYYY-MM-DD)\\n\\n<detailed body>\"}}\n\n"
    "CREATE — only if the subject is NOT in any existing page:\n"
    "  {{\"action\":\"create\", \"new_slug\":\"topic-slug\",\n"
    "    \"page_content\":\"# <Title>\\n\\n<full body with [[slug]] links>\"}}\n\n"
    "SKIP — if a subject is out of scope per the wiki guidelines, omit it (do not emit\n"
    "an action). If NO subject is in scope, return actions: [] with reasoning.\n\n"
    "DETAIL REQUIREMENT: each update_content / page_content must be COMPREHENSIVE for\n"
    "its subject — preserve specific entities, decisions, numbers, dates, action items,\n"
    "and notable quotes RELATED TO THAT SUBJECT. Do not produce a one-paragraph stub\n"
    "when the source contains substantially more detail about that subject.\n\n"
    "Return shape:\n"
    '  {{"reasoning":"...","actions":[ {{...}}, {{...}} ]}}\n\n'
    "Existing wiki pages (top matches):\n{pages}\n\n"
    "New source:\n{source}\n\n"
    "Detected entities: {entities}"
)

_OVERVIEW_PROMPT = (
    "Write a 2-paragraph overview of a knowledge wiki based on the page titles and "
    "excerpts below.\n"
    "First paragraph: what topics this wiki covers.\n"
    "Second paragraph: key themes and concepts found.\n"
    "Keep it under 200 words. Plain text only — no markdown headings.\n\n"
    "Pages:\n{pages}"
)

_VISION_PROMPT = (
    "Extract all text and key information from this image. "
    "Return plain text only, preserving the structure and content faithfully."
)


_SLUG_BLACKLIST = frozenset({
    "wikilinks", "wikilink", "wiki", "obsidian", "dataview",
    # URL path segments that are never meaningful topic names
    "watch", "embed", "video", "index", "page", "post", "article", "content",
})

# Reject slugs that look like meeting events rather than long-lived subjects:
# date-prefixed (2026-05-04-...) or built around event nouns (meeting, sync, etc.).
_MEETING_SLUG_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}|"           # date prefix: 2026-05-04...
    r"(^|-)(meeting|sync|standup|review-with|catchup|catch-up|"
    r"check-in|checkin|1-?on-?1|one-on-one)(-|$)",
    re.IGNORECASE,
)


def _is_meeting_slug(slug: str) -> bool:
    return bool(_MEETING_SLUG_RE.search(slug))


def _coerce_str_list(lst: object) -> list[str]:
    """Ensure every item in an LLM-returned list is a plain string.

    Some models return entities as dicts ({"name": "Canada", "type": "location"})
    instead of strings.  Extract the most useful field or fall back to str().
    """
    if not isinstance(lst, list):
        return []
    result = []
    for item in lst:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            value = item.get("name") or item.get("value") or item.get("label") or item.get("text") or ""
            if value:
                result.append(str(value))
        else:
            result.append(str(item))
    return [s for s in result if s.strip()]


def _parse_json_response(text: str) -> dict:
    """Parse a JSON object from an LLM response, handling markdown code fences."""
    text = text.strip()
    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Strip markdown code block: ```json ... ``` or ``` ... ```
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Find first {...} in the response
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}


def _slugify(title: str) -> str:
    # Decompose accented characters (é → e + combining accent) so they map to ASCII
    normalized = unicodedata.normalize("NFKD", title)
    # Keep ASCII alphanumeric and CJK character blocks (valid Obsidian filename chars)
    slug = re.sub(
        r"[^a-z0-9\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]+",
        "-",
        normalized.lower(),
    ).strip("-")
    # Fallback: if title was entirely symbols with no slug-able chars, use a content hash
    return slug or "page-" + hashlib.md5(title.encode()).hexdigest()[:8]


class IngestAgent:
    def __init__(self, provider: LLMProvider, store: WikiStorage, search: HybridSearch,
                 log_writer: LogWriter, audit_db: AuditDB, cache: CacheManager,
                 max_pages: int = 15, wiki_root: Optional[Path] = None,
                 cache_version: str = CACHE_VERSION,
                 fetch_timeout: int = 30) -> None:
        self._provider = provider
        self._store = store
        self._search = search
        self._log = log_writer
        self._audit = audit_db
        self._cache = cache
        self._max_pages = max_pages
        self._wiki_root = Path(wiki_root) if wiki_root is not None else None
        self._cache_version = cache_version
        self._skill_agent = SkillAgent(skill_kwargs={
            "url": {"fetch_timeout": fetch_timeout},
            "youtube": {"provider": self._provider},
        })

    async def _analyse(self, text: str, bust_cache: bool = False,
                       user_context: str = "") -> dict:
        """Step 1 — entity/tag extraction for BM25 candidate search. Cached by content hash."""
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        ctx_hash = hashlib.sha256(user_context.encode()).hexdigest() if user_context else ""
        ck = make_cache_key(
            "analyse-v1",
            {"text_hash": text_hash, "ctx_hash": ctx_hash},
            version=self._cache_version,
        )
        if not bust_cache:
            cached = await self._cache.get(ck)
            if cached:
                return cached
        resp = await self._provider.complete(
            messages=[Message(role="user", content=f"{_ANALYSIS_PROMPT}{text[:40000]}")],
            system=user_context or None,
            temperature=0.0,
        )
        data = _parse_json_response(resp.text)
        data["entities"] = _coerce_str_list(data.get("entities", []))
        data["tags"] = _coerce_str_list(data.get("tags", []))
        data["_tokens"] = resp.total_tokens
        await self._cache.set(ck, data)
        return data

    async def _update_overview(self) -> None:
        """Regenerate wiki/overview.md from the 10 most-recently-modified pages."""
        if self._wiki_root is None:
            return
        wiki_dir = self._wiki_root / "wiki"
        pages = sorted(
            [p for p in wiki_dir.glob("*.md")
             if p.stem not in {"overview", "index", "dashboard", "log"}],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:10]
        if not pages:
            return
        page_ctx = []
        for p in pages:
            snippet = p.read_text(encoding="utf-8")[:200].replace("\n", " ")
            page_ctx.append(f"- {p.stem}: {snippet}")
        pages_str = "\n".join(page_ctx)
        resp = await self._provider.complete(
            messages=[Message(role="user",
                              content=_OVERVIEW_PROMPT.format(pages=pages_str))],
            system=load_user_context(self._wiki_root) or None,
            temperature=0.3,
            max_tokens=512,
        )
        from datetime import date as _date
        content = (
            f"---\ntitle: Wiki Overview\nstatus: auto\n"
            f"updated: {_date.today().isoformat()}\n---\n\n"
            f"# Wiki Overview\n\n{resp.text.strip()}\n"
        )
        (wiki_dir / "overview.md").write_text(content, encoding="utf-8", newline="\n")

    def _hash(self, path: str) -> tuple[str, int]:
        data = Path(path).read_bytes()
        return hashlib.sha256(data).hexdigest(), len(data)

    def _needs_file_check(self, source: str) -> bool:
        """Return True when source must exist as a local file before ingestion."""
        return self._skill_agent.needs_path_resolution(source)

    async def _already_ingested(self, src_hash: str, src_size: int) -> bool:
        """Return True only if this source was ingested AND its wiki page still exists."""
        existing = await self._audit.find_by_hash(src_hash, src_size)
        if not existing:
            return False
        wiki_page = existing.get("wiki_page", "")
        return not wiki_page or self._store.page_exists(wiki_page)

    async def ingest(self, source: str, force: bool = False, bust_cache: bool = False) -> IngestResult:
        result = IngestResult(source=source)

        if self._needs_file_check(source):
            p = Path(source).resolve()

            # Security: reject sources outside wiki_root
            if self._wiki_root is not None:
                root_resolved = self._wiki_root.resolve()
                try:
                    p.relative_to(root_resolved)
                except ValueError:
                    raise PermissionError(
                        f"Source {p} is outside wiki root {root_resolved}"
                    )

            if not p.exists():
                raise FileNotFoundError(f"Source not found: {source}")
            if p.stat().st_size == 0:
                raise ValueError(f"Source file is empty: {source}")

            # Dedup: hash + size (file sources only)
            src_hash, src_size = self._hash(str(p))

            # Check for hash collision (same hash, different size)
            if not force:
                existing = await self._audit.find_by_hash_only(src_hash)
                if existing and existing["size"] != src_size:
                    logger.warning(
                        "Hash collision detected: hash=%s matches existing record but size differs "
                        "(existing=%d, current=%d). Treating as new source.",
                        src_hash, existing["size"], src_size
                    )
                elif await self._already_ingested(src_hash, src_size):
                    result.skipped = True
                    result.skip_reason = "already ingested"
                    return result

        # For URL / non-file sources p, src_hash, src_size are not set above.
        # Provide safe defaults so the audit call at the end always succeeds.
        if not self._needs_file_check(source):
            p = Path(source.split("?")[0].rstrip("/").split("/")[-1] or "url-source")
            src_hash = hashlib.sha256(source.encode()).hexdigest()
            src_size = len(source.encode())
            if not force and await self._already_ingested(src_hash, src_size):
                result.skipped = True
                result.skip_reason = "already ingested"
                return result

        # Web search decomposition: detect intent, decompose into keyword sub-queries,
        # fire N parallel Tavily searches, deduplicate URLs across results.
        try:
            _skill_meta = self._skill_agent.detect_skill(source)
            _is_web_search = _skill_meta.name == "web_search"
        except Exception:
            _is_web_search = False

        if _is_web_search:
            _bare_query = _WEB_INTENT_RE.sub("", source).strip() or source
            _sub_queries = await SearchDecomposeAgent(self._provider).decompose(_bare_query)
            _sub_results = await asyncio.gather(*[
                self._skill_agent.extract(f"search for: {q}") for q in _sub_queries
            ])
            _seen: set[str] = set()
            _merged_urls: list[str] = []
            for _r in _sub_results:
                for _url in _r.metadata.get("child_sources", []):
                    if _url not in _seen:
                        _seen.add(_url)
                        _merged_urls.append(_url)
            from synthadoc.skills.base import ExtractedContent as _EC
            extracted = _EC(
                text="", source_path=source,
                metadata={"child_sources": _merged_urls, "query": _bare_query,
                          "results_count": len(_merged_urls)},
            )
        else:
            extracted = await self._skill_agent.extract(source)

        # Web search fan-out: return child sources; orchestrator enqueues them as jobs
        if extracted.metadata.get("child_sources"):
            result.child_sources = extracted.metadata["child_sources"]
            return result

        # Pass 0: Vision extraction for image files
        if extracted.metadata.get("is_image"):
            if not getattr(self._provider, "supports_vision", True):
                raise NotImplementedError(
                    "Image ingest requires a vision-capable model. "
                    f"Current provider/model does not support vision. "
                    "Switch to anthropic (claude-*) or gemini (gemini-*) for image sources."
                )
            b64 = extracted.metadata.get("base64", "")
            media_type = extracted.metadata.get("media_type", "image/png")
            vision_resp = await self._provider.complete(
                messages=[Message(role="user", content=[
                    {"type": "image", "source": {
                        "type": "base64", "media_type": media_type, "data": b64,
                    }},
                    {"type": "text", "text": _VISION_PROMPT},
                ])],
                temperature=0.0,
            )
            result.tokens_used += vision_resp.total_tokens
            result.input_tokens += vision_resp.input_tokens
            result.output_tokens += vision_resp.output_tokens
            text = vision_resp.text[:40000]
        else:
            text = extracted.text[:40000]

        if not text or not text.strip():
            logger.warning("Skipping ingest for %s — no extractable text", source)
            result.skipped = True
            result.skip_reason = "no extractable text"
            return result

        # Pre-summarised sources (e.g. YouTube) bypass analysis/decision: the
        # skill already produced a synthesized body; write it directly.
        if extracted.metadata.get("has_summary"):
            self._write_has_summary_page(extracted, [], result)
            if result.pages_created or result.pages_updated:
                await self._update_overview()
            else:
                result.skipped = True
                result.skip_reason = result.skip_reason or "has_summary source had no usable slug"
            self._log.log_ingest(source=p.name,
                                 pages_created=result.pages_created,
                                 pages_updated=result.pages_updated,
                                 pages_flagged=result.pages_flagged,
                                 tokens=result.tokens_used,
                                 cost_usd=result.cost_usd,
                                 cache_hits=result.cache_hits)
            primary = (result.pages_created + result.pages_updated
                       + result.pages_flagged or [p.stem])[0]
            await self._audit.record_ingest(src_hash, src_size, source,
                                            primary,
                                            result.tokens_used, result.cost_usd)
            return result

        # Persona + scope (AGENTS.md + purpose.md) — passed as system prompt so
        # the LLM personalizes summaries, page writes, and scope decisions.
        user_context = load_user_context(self._wiki_root)

        # Step 1: analysis pass (cached separately from decision)
        analysis = await self._analyse(text, bust_cache=bust_cache, user_context=user_context)
        result.tokens_used += analysis.pop("_tokens", 0)
        # input/output split not available for the analyse call (cached via _analyse)

        entities = _coerce_str_list(analysis.get("entities", []))
        tags = _coerce_str_list(analysis.get("tags", []))

        # Fallback: if LLM entity extraction returned nothing, extract key phrases
        # directly from the source text so BM25 always has meaningful search terms.
        if not entities:
            # English: capitalized noun phrases
            english = re.findall(r'\b[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*\b', text[:2000])
            # CJK: 2–6 consecutive chars — shorter is too granular, longer risks full sentences
            cjk = re.findall(
                r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]{2,6}',
                text[:2000],
            )
            entities = list(dict.fromkeys(english + cjk))[:12]
            logger.debug("Entity extraction returned empty; using text-extracted phrases: %s", entities)

        # Pass 2: hybrid search
        candidates = self._search.bm25_search(entities + tags, top_n=self._max_pages)

        # Build page context: top 5 candidates with content snippets
        pages_ctx = []
        for r in candidates[:5]:
            page = self._store.read_page(r.slug)
            if page:
                snippet = page.content[:600].replace("\n", " ")
                pages_ctx.append(f"[{r.slug}]: {snippet}")
        pages_str = "\n".join(pages_ctx) or "none"

        # Pass 3: decision (cached by source-text hash + candidate slugs + context hash)
        slugs = [r.slug for r in candidates]
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        ctx_hash = hashlib.sha256(user_context.encode()).hexdigest() if user_context else ""
        ck2 = make_cache_key(
            "make-decision",
            {"text_hash": text_hash, "slugs": slugs, "ctx_hash": ctx_hash},
            version=self._cache_version,
        )
        cached2 = None if bust_cache else await self._cache.get(ck2)
        if cached2:
            result.cache_hits += 1
            decisions = cached2
        else:
            decision_prompt = _DECISION_PROMPT
            if user_context:
                decision_prompt = (
                    "Per the wiki scope and guidelines in the system message, "
                    "respond with action=\"skip\" if the source is clearly out of scope.\n\n"
                ) + _DECISION_PROMPT
            resp2 = await self._provider.complete(
                messages=[Message(role="user", content=decision_prompt.format(
                    pages=pages_str,
                    source=text,
                    entities=entities,
                ))],
                system=user_context or None,
                temperature=0.0,
            )
            result.tokens_used += resp2.total_tokens
            result.input_tokens += resp2.input_tokens
            result.output_tokens += resp2.output_tokens
            decisions = _parse_json_response(resp2.text)
            await self._cache.set(ck2, decisions)

        # Pass 4: process each action. Backward-compat: legacy decisions had a
        # single top-level action; wrap them into a one-element list.
        actions = decisions.get("actions")
        if not isinstance(actions, list):
            if decisions.get("action"):
                actions = [decisions]
            else:
                actions = []

        explicit_skip = False
        for act in actions:
            if not isinstance(act, dict):
                continue
            if (act.get("action") or "").lower() == "skip":
                explicit_skip = True
                continue
            self._apply_action(act, result, extracted, tags)

        if not (result.pages_created or result.pages_updated or result.pages_flagged):
            result.skipped = True
            if explicit_skip:
                result.skip_reason = "out of scope"
            else:
                result.skip_reason = result.skip_reason or "no synthesizable content"

        if result.pages_created or result.pages_updated:
            await self._update_overview()

        self._log.log_ingest(source=p.name,
                             pages_created=result.pages_created,
                             pages_updated=result.pages_updated,
                             pages_flagged=result.pages_flagged,
                             tokens=result.tokens_used,
                             cost_usd=result.cost_usd,
                             cache_hits=result.cache_hits)
        primary = (result.pages_created + result.pages_updated
                   + result.pages_flagged or [p.stem])[0]
        await self._audit.record_ingest(src_hash, src_size, source,
                                        primary,
                                        result.tokens_used, result.cost_usd)
        return result

    def _apply_action(self, act: dict, result: IngestResult,
                      extracted, tags: list[str]) -> None:
        """Apply a single LLM-decided action (flag/update/create/skip)."""
        kind = (act.get("action") or "").lower()
        if kind == "skip" or not kind:
            return

        target = act.get("target") or ""
        new_slug = act.get("new_slug") or ""
        update_content = (act.get("update_content") or "").strip()
        page_content = (act.get("page_content") or "").strip()

        if kind == "flag":
            if not target or target in LINT_SKIP_SLUGS or not self._store.page_exists(target):
                logger.warning("Skipping flag — target missing or invalid: %r", target)
                return
            with self._store.page_lock(target):
                page = self._store.read_page(target)
                if page:
                    page.status = "contradicted"
                    self._store.write_page(target, page)
                    self._search.invalidate_index()
            result.pages_flagged.append(target)
            return

        if kind == "update":
            if not target or not self._store.page_exists(target):
                logger.warning("Skipping update — target missing: %r", target)
                return
            if not update_content:
                logger.warning("Skipping update for %s — empty update_content", target)
                return
            with self._store.page_lock(target):
                page = self._store.read_page(target)
                if page:
                    page.content = page.content.rstrip() + f"\n\n{update_content}"
                    self._store.write_page(target, page)
                    self._search.invalidate_index()
            result.pages_updated.append(target)
            return

        if kind == "create":
            if not new_slug:
                logger.warning("Skipping create — no new_slug provided")
                return
            if not page_content:
                logger.warning("Skipping create for slug %r — empty page_content", new_slug)
                return
            slug = _slugify(new_slug)
            if not slug or slug in _SLUG_BLACKLIST or _is_meeting_slug(slug):
                logger.warning("Skipping create — slug rejected: %r → %r", new_slug, slug)
                return

            if self._store.page_exists(slug):
                # Slug already exists — append page_content as a new section instead of overwriting
                with self._store.page_lock(slug):
                    page = self._store.read_page(slug)
                    if page:
                        page.content = page.content.rstrip() + f"\n\n{page_content}"
                        self._store.write_page(slug, page)
                        self._search.invalidate_index()
                result.pages_updated.append(slug)
                return

            page_title = self._title_from_page_content(page_content) or slug.replace("-", " ").title()
            new_page = WikiPage(
                title=page_title, tags=tags,
                content=page_content,
                status="active", confidence="medium", sources=[],
                created=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            )
            with self._store.page_lock(slug):
                self._store.write_page(slug, new_page)
                self._search.invalidate_index()
            result.pages_created.append(slug)
            self._store.append_to_index(slug, new_page.title)
            return

        logger.warning("Unknown action kind: %r", kind)

    @staticmethod
    def _title_from_page_content(body: str) -> str:
        """Pull a title from the first '# Title' line of LLM-supplied page content."""
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped.lstrip("#").strip()
            if stripped:
                break
        return ""

    def _write_has_summary_page(self, extracted, tags: list[str],
                                result: IngestResult) -> None:
        """Write a YouTube-style pre-summarised body directly to a slug derived
        from the source's video_id/url, bypassing the LLM decision flow."""
        meta = extracted.metadata or {}
        slug_seed = meta.get("video_id") or meta.get("url") or extracted.source_path
        slug = _slugify(str(slug_seed))
        if not slug or _is_meeting_slug(slug) or slug in _SLUG_BLACKLIST:
            logger.warning("has_summary source had no usable slug: %r", slug_seed)
            return
        body = extracted.text
        page_title = self._title_from_page_content(body) or slug.replace("-", " ").title()
        if self._store.page_exists(slug):
            with self._store.page_lock(slug):
                page = self._store.read_page(slug)
                if page:
                    page.content = page.content.rstrip() + f"\n\n{body}"
                    self._store.write_page(slug, page)
                    self._search.invalidate_index()
            result.pages_updated.append(slug)
            return
        new_page = WikiPage(
            title=page_title, tags=tags, content=body,
            status="active", confidence="medium", sources=[],
            created=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )
        with self._store.page_lock(slug):
            self._store.write_page(slug, new_page)
            self._search.invalidate_index()
        result.pages_created.append(slug)
        self._store.append_to_index(slug, new_page.title)
