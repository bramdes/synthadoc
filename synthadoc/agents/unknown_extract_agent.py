# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""UnknownExtractAgent — first-class explicit unknowns (spec §4.8).

Spec §3.6 makes unknowns mandatory: "The system should explicitly track
what is unknown, unclear, or not evidenced." This agent reads one source
and emits zero-or-more ``unknown`` records covering ambiguities the
source surfaces but does not resolve.

Examples the LLM should catch:

* "Drop 1 is complete." → unknown: is the *full* project complete?
* "Pravin will own the rollout." → unknown if no later source confirms.
* "We will decide next quarter." → unknown: decision deferred.

The agent reuses the FactExtractAgent / DecisionExtractAgent retry +
validation pattern. Unlike facts, an unknown's ``source_quote`` may be
empty when the unknown is about *missing* evidence (the absence of a
statement). The validator handles both cases.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from synthadoc.agents.fact_extract_agent import _quote_in_body
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

_SYSTEM = (
    "You are an information-extraction assistant. You surface UNKNOWNS — "
    "the questions a source leaves unanswered. An unknown is a gap, "
    "ambiguity, or open question. Do NOT speculate. Do NOT invent facts. "
    "Return ONLY valid JSON — no fences, no commentary."
)


def _prompt(*, title: str, source_type: str, valid_on: str,
            body: str, retry_hint: str = "") -> str:
    entity_types = ", ".join(sorted(ids.ENTITY_TYPES))
    hint = (f"\nIMPORTANT: previous attempt failed validation:\n{retry_hint}\n"
            if retry_hint else "")
    return f"""\
List the unknowns this source raises but does not resolve.{hint}

Return JSON with this exact shape:
{{
  "unknowns": [
    {{
      "question":     "One-line question that captures the unknown",
      "entity_name":  "Subject this unknown is about (or empty string if cross-cutting)",
      "entity_type":  "project|person|topic|requirement (or empty string if cross-cutting)",
      "source_quote": "EXACT verbatim substring backing the ambiguity (or empty string if the unknown is about MISSING information)",
      "reasoning":    "1-2 sentences explaining why this is unknown"
    }}
  ]
}}

Rules:
- Each unknown must be SOMETHING THE SOURCE LEAVES OPEN. Not background trivia.
- If source_quote is non-empty, it must be copied verbatim from the source body.
- Empty source_quote means "the source is silent on this" — only use this when truly the gap is the absence.
- entity_type, if non-empty, must be one of: {entity_types}.
- If the source raises NO open questions, return {{"unknowns": []}}.
- Quality over quantity. Don't pad with trivial unknowns.

Source title: {title}
Source type: {source_type}
Source date: {valid_on}
Source body:
---
{body}
---
"""


@dataclass
class ExtractedUnknown:
    question: str
    entity_name: str
    entity_type: str
    source_quote: str        # may be empty (unknown about missing evidence)
    reasoning: str


@dataclass
class PersistedUnknown:
    unknown_id: str
    entity_id: Optional[str]
    path: str


@dataclass
class UnknownExtractResult:
    persisted: list[PersistedUnknown] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    cached: bool = False
    input_tokens: int = 0
    output_tokens: int = 0


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class UnknownExtractAgent:
    def __init__(
        self,
        *,
        provider: LLMProvider,
        db: KBDB,
        layout: KBLayout,
        linker: EntityLinker,
        max_body_chars: int = 40_000,
        max_retries: int = 1,
    ) -> None:
        self._provider = provider
        self._db = db
        self._layout = layout
        self._linker = linker
        self._max_body_chars = max_body_chars
        self._max_retries = max_retries

    async def extract(self, source_id: str, *, force: bool = False) -> UnknownExtractResult:
        source = await self._db.get_source(source_id)
        if source is None:
            raise KeyError(f"source {source_id!r} not found in kb.db")

        if not force:
            existing = await self._db.fetchall(
                "SELECT u.* FROM unknowns u "
                "JOIN entities e ON e.id = u.entity_id "
                "WHERE EXISTS ("
                "  SELECT 1 FROM facts f WHERE f.entity_id = e.id AND f.source_id = ?"
                ") LIMIT 1",
                (source_id,),
            )
            # We can't perfectly tell "this source already produced unknowns"
            # without a source-link column. For v0.3 the safest cache check is:
            # was extract() called for this source before? Track via a meta row.
            cached = await self._db.fetchall(
                "SELECT value FROM kb_meta WHERE key = ?",
                (f"unknowns_done_for:{source_id}",),
            )
            if cached:
                return UnknownExtractResult(cached=True)

        body = self._read_body(source)
        candidates, in_tok, out_tok = await self._call_with_retry(source, body)

        result = UnknownExtractResult(input_tokens=in_tok, output_tokens=out_tok)
        for raw in candidates:
            try:
                u = self._validate(raw, body)
            except _ValidationError as exc:
                logger.warning("rejected unknown from %s: %s", source_id, exc)
                result.rejected.append((str(exc), json.dumps(raw, ensure_ascii=False)))
                continue
            persisted = await self._persist(u, source=source)
            if persisted:
                result.persisted.append(persisted)

        # Stamp meta so re-runs are cached even when no unknowns were emitted.
        await self._db.execute(
            "INSERT INTO kb_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"unknowns_done_for:{source_id}", datetime.now(timezone.utc).isoformat()),
        )
        return result

    # ------------------------------------------------------------------

    async def _call_with_retry(
        self, source: dict, body: str,
    ) -> tuple[list[dict], int, int]:
        valid_on = ids.split_source_id(source["id"])[1]
        prompt = _prompt(
            title=source["title"], source_type=source["source_type"],
            valid_on=valid_on, body=body or "(empty source body)",
        )
        in_tok = 0
        out_tok = 0
        last_err = ""
        for attempt in range(self._max_retries + 1):
            resp = await self._provider.complete(
                messages=[Message(role="user", content=prompt)],
                system=_SYSTEM, temperature=0.0, max_tokens=8192,
            )
            in_tok += resp.input_tokens
            out_tok += resp.output_tokens
            try:
                data = _parse_json(resp.text)
            except ValueError as exc:
                last_err = str(exc)
                if attempt == self._max_retries:
                    raise
                prompt = _prompt(
                    title=source["title"], source_type=source["source_type"],
                    valid_on=valid_on, body=body or "(empty source body)",
                    retry_hint=last_err,
                )
                continue
            items = data.get("unknowns", [])
            if not isinstance(items, list):
                if attempt == self._max_retries:
                    raise ValueError(
                        f"'unknowns' must be a list, got {type(items).__name__}"
                    )
                last_err = f"'unknowns' was {type(items).__name__}, must be a list"
                prompt = _prompt(
                    title=source["title"], source_type=source["source_type"],
                    valid_on=valid_on, body=body or "(empty source body)",
                    retry_hint=last_err,
                )
                continue
            return items, in_tok, out_tok
        return [], in_tok, out_tok

    def _validate(self, raw: Any, body: str) -> ExtractedUnknown:
        if not isinstance(raw, dict):
            raise _ValidationError(f"unknown must be an object, got {type(raw).__name__}")

        def _req(field_: str) -> str:
            v = raw.get(field_)
            if not isinstance(v, str) or not v.strip():
                raise _ValidationError(f"missing or empty '{field_}'")
            return v.strip()

        question = _req("question")
        reasoning = str(raw.get("reasoning", "")).strip()
        entity_name = str(raw.get("entity_name", "")).strip()
        entity_type = str(raw.get("entity_type", "")).strip().lower()
        source_quote = str(raw.get("source_quote", "")).strip()

        if entity_type and entity_type not in ids.ENTITY_TYPES:
            raise _ValidationError(
                f"entity_type={entity_type!r} not in {sorted(ids.ENTITY_TYPES)!r}"
            )
        if bool(entity_name) != bool(entity_type):
            raise _ValidationError(
                "entity_name and entity_type must be both set or both empty"
            )
        # Non-empty source_quote MUST be substring of body. Empty is allowed —
        # the unknown is about missing evidence.
        if source_quote and not _quote_in_body(source_quote, body):
            raise _ValidationError(
                f"source_quote not found verbatim in source body "
                f"(quote={source_quote[:60]!r})"
            )

        return ExtractedUnknown(
            question=question, entity_name=entity_name, entity_type=entity_type,
            source_quote=source_quote, reasoning=reasoning,
        )

    async def _persist(
        self, u: ExtractedUnknown, *, source: dict,
    ) -> Optional[PersistedUnknown]:
        entity_id: Optional[str] = None
        if u.entity_name and u.entity_type:
            link = await self._linker.resolve(
                name=u.entity_name, entity_type=u.entity_type,
            )
            if link is not None:
                entity_id = link.entity_id

        slug = ids.slugify(u.question)
        # An entity-less unknown gets a synthetic "cross-cutting" key —
        # this keeps the id shape valid and the filesystem path sensible.
        if entity_id is None:
            placeholder_eid = ids.entity_id("topic", "cross-cutting")
            uid_base = ids.unknown_id(placeholder_eid, slug)
            path_dir = (self._layout.root / "kb" / "unknowns" / "_cross-cutting")
        else:
            uid_base = ids.unknown_id(entity_id, slug)
            entity_type = entity_id.split(".")[1]
            entity_slug = entity_id.split(".", 2)[2]
            path_dir = self._layout.unknown_path(
                entity_type, entity_slug, slug
            ).parent

        n = 1
        uid = uid_base
        path = path_dir / f"{slug}.md"
        while await self._db.get_unknown(uid) is not None:
            n += 1
            slug_n = ids.slug_with_suffix(slug, n)
            uid = ids.unknown_id(entity_id or placeholder_eid, slug_n)
            path = path_dir / f"{slug_n}.md"

        rel_path = str(path.relative_to(self._layout.root)).replace("\\", "/")
        created_at = datetime.now(timezone.utc).isoformat()

        data = fm.build_unknown(
            id=uid,
            created_at=created_at,
            entity_id=entity_id,
            extra={"prompt_version": PROMPT_VERSION, "source_id": source["id"]},
        )
        body_md = _render_body(u, source=source)
        fm.write(path, data, body_md)

        await self._db.insert_unknown(
            id=uid, created_at=created_at, path=rel_path,
            entity_id=entity_id, status="open",
        )

        return PersistedUnknown(unknown_id=uid, entity_id=entity_id, path=rel_path)

    def _read_body(self, source: dict) -> str:
        parsed = source.get("parsed_path")
        if not parsed:
            return ""
        path = self._layout.root / parsed
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8")
        if len(text) > self._max_body_chars:
            text = text[: self._max_body_chars]
        return text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ValidationError(ValueError):
    pass


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
            f"UnknownExtractAgent: unparseable JSON: {exc}\n---\n{text[:400]}\n---"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"UnknownExtractAgent: expected JSON object, got {type(data).__name__}"
        )
    return data


def _render_body(u: ExtractedUnknown, *, source: dict) -> str:
    lines = [
        f"# Unknown: {u.question}",
        "",
    ]
    if u.source_quote:
        lines.append(f"> {u.source_quote}")
        lines.append("")
    if u.reasoning:
        lines.append(u.reasoning)
        lines.append("")
    lines.append(f"_Source: {source['id']}_")
    lines.append("")
    return "\n".join(lines)
