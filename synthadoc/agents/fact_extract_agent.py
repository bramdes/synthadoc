# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""FactExtractAgent — atomic, timestamped, source-bound facts.

Plan §9 Layer 2 / spec §4.3. For each source, the LLM is asked to emit a
list of atomic facts. Every candidate then runs through a deterministic
validator pipeline:

1. JSON parses to ``{"facts": [...]}`` (one retry on parse failure).
2. Every required field is present and structurally valid.
3. ``entity_type`` is in :data:`ids.ENTITY_TYPES` (closed vocab).
4. ``fact_type`` is in :data:`ids.FACT_TYPES` (closed vocab — plan §14 Q3).
5. ``confidence`` ∈ ``{high, medium, low}``.
6. ``valid_at`` is ``YYYY-MM-DD``.
7. ``source_quote`` **must be a verbatim substring of the parsed source body.**

(7) is the most important rule. It kills the most-common class of LLM
hallucination — "the model invented a quote that sounds right". Plan §11
calls this guard out specifically; it lands here.

Failures at steps 3-7 are *not* retries — the offending fact is dropped
with a warning log and the rest are persisted. Hard failure on (1) gets
one retry with the parser error fed back into the prompt.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from synthadoc.kb import frontmatter as fm
from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.entity_linker import EntityLinker
from synthadoc.kb.layout import KBLayout
from synthadoc.providers.base import LLMProvider, Message

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"

_FENCE_PAIR_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_FENCE_OPEN_RE = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WS_RE = re.compile(r"\s+")

_SYSTEM = (
    "You are a careful information-extraction assistant. "
    "You extract ATOMIC, source-backed facts. "
    "Every fact you emit must include a verbatim quote copied character-for-character "
    "from the source body — paraphrased quotes will be rejected automatically. "
    "Return ONLY valid JSON — no markdown fences, no commentary."
)


def _prompt(*, title: str, source_type: str, valid_on: str,
            body: str, retry_hint: str = "") -> str:
    fact_types_list = ", ".join(sorted(ids.FACT_TYPES))
    entity_types_list = ", ".join(sorted(ids.ENTITY_TYPES))
    hint = f"\nIMPORTANT: previous attempt failed validation:\n{retry_hint}\n" if retry_hint else ""
    return f"""\
Extract atomic facts from the source below.{hint}

Return JSON with this exact shape:
{{
  "facts": [
    {{
      "entity_name":  "Exact subject name as it appears, e.g. 'Document Intelligence'",
      "entity_type":  "one of: {entity_types_list}",
      "fact_type":    "one of: {fact_types_list}",
      "value":        "normalised value, e.g. 'completed', 'in_progress', 'pravin'",
      "value_raw":    "the raw phrase from the source, e.g. 'now complete'",
      "valid_at":     "YYYY-MM-DD (the date the fact is true; NOT today)",
      "source_quote": "EXACT verbatim substring of the source body — copy/paste it",
      "source_span":  "optional locator, e.g. 'paragraph 3' (use empty string if unsure)",
      "confidence":   "high | medium | low"
    }}
  ]
}}

Rules:
- ONLY include facts the source EXPLICITLY states.
- Derived conclusions (things implied but not stated) belong in a separate pass — omit them here.
- source_quote MUST be copied verbatim from the source body — paraphrased quotes are rejected.
- fact_type must be EXACTLY one of the listed values.
- entity_type must be EXACTLY one of: {entity_types_list}.
- valid_at is the date the fact is true. If the source says "as of {valid_on}, status is X",
  use {valid_on}. If unsure, fall back to {valid_on}.
- value should be normalised lower-case identifier (e.g. completed, in_progress, blocked).
- If the source contains NO extractable atomic facts, return {{"facts": []}}.

Source title: {title}
Source type: {source_type}
Source date: {valid_on}
Source body:
---
{body}
---
"""


@dataclass
class ExtractedFact:
    """One candidate fact returned by the LLM, after structural validation."""

    entity_name: str
    entity_type: str
    fact_type: str
    value: str
    value_raw: str
    valid_at: str
    source_quote: str
    source_span: str
    confidence: str


@dataclass
class PersistedFact:
    """A fact that survived all validators and was written to disk + DB."""

    fact_id: str
    entity_id: str
    fact_type: str
    value: str
    valid_at: str
    path: str


@dataclass
class FactExtractResult:
    persisted: list[PersistedFact] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (reason, json_blob)
    cached: bool = False
    input_tokens: int = 0
    output_tokens: int = 0


class FactExtractBudgetExceeded(RuntimeError):
    """Raised when the per-source token budget is exhausted mid-extraction.

    The pipeline catches this and records it on ``result.errors`` — the
    page-tier ingest is unaffected. Persisted facts from earlier LLM calls
    in this source remain valid; the agent simply stops *further* work.
    """


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class FactExtractAgent:
    """Async agent. Holds references but no per-call state."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        db: KBDB,
        layout: KBLayout,
        linker: EntityLinker,
        max_body_chars: int = 40_000,
        max_retries: int = 1,
        max_tokens_per_source: int = 0,  # 0 = unbounded
    ) -> None:
        self._provider = provider
        self._db = db
        self._layout = layout
        self._linker = linker
        self._max_body_chars = max_body_chars
        self._max_retries = max_retries
        self._max_tokens_per_source = max(0, int(max_tokens_per_source))

    # ------------------------------------------------------------------

    async def extract(self, source_id: str, *, force: bool = False) -> FactExtractResult:
        """Extract facts from *source_id*. Idempotent unless ``force=True``.

        Raises ``KeyError`` if the source is unknown to the DB.
        """
        source = await self._db.get_source(source_id)
        if source is None:
            raise KeyError(f"source {source_id!r} not found in kb.db")

        if not force:
            existing = await self._db.fetchall(
                "SELECT id FROM facts WHERE source_id = ? LIMIT 1", (source_id,)
            )
            if existing:
                logger.info("facts already extracted for %s — skipping", source_id)
                rows = await self._db.fetchall(
                    "SELECT id, entity_id, fact_type, value, valid_at, path "
                    "FROM facts WHERE source_id = ?", (source_id,)
                )
                return FactExtractResult(
                    persisted=[
                        PersistedFact(
                            fact_id=r["id"], entity_id=r["entity_id"],
                            fact_type=r["fact_type"], value=r["value"],
                            valid_at=r["valid_at"], path=r["path"],
                        )
                        for r in rows
                    ],
                    cached=True,
                )

        body = self._read_parsed_body(source)
        try:
            candidates, in_tok, out_tok = await self._call_with_retry(source, body)
        except FactExtractBudgetExceeded:
            # Budget tripped before any candidates landed — surface a clear
            # result with the flag set rather than swallowing the error.
            logger.warning(
                "fact extraction budget exceeded for %s; aborting", source_id,
            )
            raise

        result = FactExtractResult(input_tokens=in_tok, output_tokens=out_tok)

        for raw in candidates:
            try:
                fact = self._validate(raw, body)
            except _ValidationError as exc:
                logger.warning("rejected fact from %s: %s", source_id, exc)
                result.rejected.append((str(exc), json.dumps(raw, ensure_ascii=False)))
                continue
            persisted = await self._persist(fact, source=source)
            if persisted is not None:
                result.persisted.append(persisted)

        return result

    # ------------------------------------------------------------------
    # LLM + parsing
    # ------------------------------------------------------------------

    async def _call_with_retry(
        self, source: dict, body: str,
    ) -> tuple[list[dict], int, int]:
        valid_on = ids.split_source_id(source["id"])[1]
        prompt = _prompt(
            title=source["title"], source_type=source["source_type"],
            valid_on=valid_on, body=body or "(empty source body)",
        )
        input_tokens = 0
        output_tokens = 0
        last_err: Optional[str] = None
        for attempt in range(self._max_retries + 1):
            resp = await self._provider.complete(
                messages=[Message(role="user", content=prompt)],
                system=_SYSTEM,
                temperature=0.0,
                max_tokens=8192,
            )
            input_tokens += resp.input_tokens
            output_tokens += resp.output_tokens
            if self._max_tokens_per_source and \
                    input_tokens + output_tokens > self._max_tokens_per_source:
                raise FactExtractBudgetExceeded(
                    f"fact extraction exceeded per-source token budget "
                    f"({input_tokens + output_tokens} > "
                    f"{self._max_tokens_per_source}) for source {source['id']!r}"
                )
            try:
                data = _parse_json(resp.text)
            except ValueError as exc:
                last_err = str(exc)
                if attempt == self._max_retries:
                    raise
                logger.warning(
                    "fact extraction JSON parse failed (attempt %d): %s",
                    attempt + 1, exc,
                )
                prompt = _prompt(
                    title=source["title"], source_type=source["source_type"],
                    valid_on=valid_on, body=body or "(empty source body)",
                    retry_hint=last_err,
                )
                continue
            facts = data.get("facts", [])
            if not isinstance(facts, list):
                if attempt == self._max_retries:
                    raise ValueError(
                        f"'facts' must be a list, got {type(facts).__name__}"
                    )
                last_err = f"'facts' was {type(facts).__name__}, must be a list"
                prompt = _prompt(
                    title=source["title"], source_type=source["source_type"],
                    valid_on=valid_on, body=body or "(empty source body)",
                    retry_hint=last_err,
                )
                continue
            return facts, input_tokens, output_tokens
        return [], input_tokens, output_tokens  # unreachable

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(self, raw: Any, body: str) -> ExtractedFact:
        if not isinstance(raw, dict):
            raise _ValidationError(f"fact must be an object, got {type(raw).__name__}")

        def _required(field_: str) -> str:
            v = raw.get(field_)
            if not isinstance(v, str) or not v.strip():
                raise _ValidationError(f"missing or empty '{field_}'")
            return v.strip()

        entity_name = _required("entity_name")
        entity_type = _required("entity_type").lower()
        fact_type = _required("fact_type")
        value = _required("value")
        valid_at = _required("valid_at")
        source_quote = _required("source_quote")
        confidence = _required("confidence").lower()

        if entity_type not in ids.ENTITY_TYPES:
            raise _ValidationError(
                f"entity_type={entity_type!r} not in {sorted(ids.ENTITY_TYPES)!r}"
            )
        if fact_type not in ids.FACT_TYPES:
            raise _ValidationError(
                f"fact_type={fact_type!r} not in closed vocab"
            )
        if confidence not in ("high", "medium", "low"):
            raise _ValidationError(f"confidence={confidence!r} not in (high, medium, low)")
        if not _DATE_RE.match(valid_at):
            raise _ValidationError(f"valid_at={valid_at!r} is not YYYY-MM-DD")

        # The substring guard — the kingpin. Normalise both sides (NFKD +
        # collapse whitespace) so trivial formatting differences don't trip
        # us, but reject anything beyond that.
        if not _quote_in_body(source_quote, body):
            raise _ValidationError(
                f"source_quote not found verbatim in source body "
                f"(quote={source_quote[:60]!r})"
            )

        return ExtractedFact(
            entity_name=entity_name,
            entity_type=entity_type,
            fact_type=fact_type,
            value=value,
            value_raw=str(raw.get("value_raw", "")).strip() or source_quote,
            valid_at=valid_at,
            source_quote=source_quote,
            source_span=str(raw.get("source_span", "")).strip(),
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _persist(
        self, fact: ExtractedFact, *, source: dict,
    ) -> Optional[PersistedFact]:
        link = await self._linker.resolve(
            name=fact.entity_name, entity_type=fact.entity_type
        )
        if link is None:
            # Should be unreachable with auto_create=True, but guard anyway.
            logger.warning("entity link returned None for %r", fact.entity_name)
            return None
        entity_id = link.entity_id

        base_fact_id = ids.fact_id(entity_id, fact.fact_type, fact.valid_at)
        # Collision on (entity_id, fact_type, valid_at) — pick a free suffix.
        async def _taken(candidate: str) -> bool:
            row = await self._db.get_fact(candidate)
            return row is not None
        # The collision helper expects a sync callable; we wrap the async check
        # explicitly here rather than threading an async helper through ids.py.
        fact_id = base_fact_id
        n = 1
        while await _taken(fact_id):
            n += 1
            fact_id = f"{base_fact_id}-{n}"

        # Filesystem path for the fact markdown
        entity_slug = entity_id.split(".", 2)[2]
        value_slug = ids.slugify(fact.value)
        path = self._layout.fact_path(
            entity_type=fact.entity_type,
            entity_slug=entity_slug,
            fact_type=fact.fact_type,
            valid_at=fact.valid_at,
            value_slug=value_slug,
        )
        # If collision-suffix shifted the id, mirror it in the filename
        if n > 1:
            path = path.with_name(f"{fact.valid_at}-{value_slug}-{n}.md")

        rel_path = str(path.relative_to(self._layout.root)).replace("\\", "/")

        data = fm.build_fact(
            id=fact_id,
            entity_id=entity_id,
            fact_type=fact.fact_type,
            value=fact.value,
            valid_at=fact.valid_at,
            observed_at=datetime.now(timezone.utc).isoformat(),
            source_id=source["id"],
            source_path=source["parsed_path"] or "",
            source_quote=fact.source_quote,
            confidence=fact.confidence,
            value_raw=fact.value_raw,
            source_span=fact.source_span or None,
            extra={"prompt_version": PROMPT_VERSION},
        )
        body_md = _render_fact_body(fact)
        fm.write(path, data, body_md)

        await self._db.insert_fact(
            id=fact_id,
            entity_id=entity_id,
            fact_type=fact.fact_type,
            value=fact.value,
            valid_at=fact.valid_at,
            observed_at=data["observed_at"],
            source_id=source["id"],
            source_path=source["parsed_path"] or "",
            source_quote=fact.source_quote,
            confidence=fact.confidence,
            path=rel_path,
            value_raw=fact.value_raw,
            source_span=fact.source_span or None,
        )

        return PersistedFact(
            fact_id=fact_id,
            entity_id=entity_id,
            fact_type=fact.fact_type,
            value=fact.value,
            valid_at=fact.valid_at,
            path=rel_path,
        )

    # ------------------------------------------------------------------
    # Body reader
    # ------------------------------------------------------------------

    def _read_parsed_body(self, source: dict) -> str:
        parsed = source.get("parsed_path")
        if not parsed:
            return ""
        path = self._layout.root / parsed
        if not path.exists():
            return ""
        body = path.read_text(encoding="utf-8")
        if len(body) > self._max_body_chars:
            body = body[: self._max_body_chars]
        return body


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ValidationError(ValueError):
    """Internal — raised per-candidate; the agent logs and continues."""


def _strip_fences(text: str) -> str:
    """Strip a ```json … ``` fence. Tolerates truncated (no-closer) responses."""
    raw = text.strip()
    m = _FENCE_PAIR_RE.search(raw)
    if m:
        return m.group(1)
    m = _FENCE_OPEN_RE.match(raw)
    if m:
        return raw[m.end():]
    return raw


def _parse_json(text: str) -> dict:
    raw = _strip_fences(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"FactExtractAgent: unparseable JSON: {exc}\n---\n{text[:400]}\n---"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"FactExtractAgent: expected JSON object, got {type(data).__name__}"
        )
    return data


def _quote_in_body(quote: str, body: str) -> bool:
    """Substring check with mild normalisation (NFKC + whitespace collapse).

    Empty quote always rejected — `"" in body` is True in Python and we do
    not want that to count as evidence.
    """
    if not body or not quote.strip():
        return False
    if quote in body:
        return True
    nb = _normalise(body)
    nq = _normalise(quote)
    return bool(nq) and nq in nb


def _normalise(text: str) -> str:
    decomposed = unicodedata.normalize("NFKC", text)
    return _WS_RE.sub(" ", decomposed).strip()


def _render_fact_body(fact: ExtractedFact) -> str:
    lines = [
        f"# Fact: {fact.entity_name} — {fact.fact_type} = {fact.value} (as of {fact.valid_at})",
        "",
        f"> {fact.source_quote}",
        "",
        f"_Confidence: {fact.confidence}_",
    ]
    if fact.source_span:
        lines.append(f"_Source span: {fact.source_span}_")
    lines.append("")
    return "\n".join(lines)
