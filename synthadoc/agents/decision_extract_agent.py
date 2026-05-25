# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""DecisionExtractAgent — explicit decision records (spec §4.9).

A decision is a deliberate commitment recorded in the source. It must
include a verbatim quote, a decision_date, and an authority assessment.
Optional rationale + consequences + reversal conditions are pulled in
when the source contains them.

Reuses the same substring-quote guard as FactExtractAgent — paraphrased
quotes are rejected without retry; JSON parse failures get one retry.
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
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_AUTHORITY_VALUES = ("formal", "informal", "unknown")

_SYSTEM = (
    "You are an information-extraction assistant. You extract explicit "
    "DECISIONS from a single source. A decision is a deliberate commitment "
    "stated in the source — not an opinion, suggestion, or possibility. "
    "Every decision must include a verbatim quote copied character-for-character "
    "from the source. Return ONLY valid JSON — no fences, no commentary."
)


def _prompt(*, title: str, source_type: str, valid_on: str,
            body: str, retry_hint: str = "") -> str:
    hint = (f"\nIMPORTANT: previous attempt failed validation:\n{retry_hint}\n"
            if retry_hint else "")
    return f"""\
Extract explicit decisions from the source below.{hint}

Return JSON with this exact shape:
{{
  "decisions": [
    {{
      "title":          "Short imperative title for the decision",
      "entity_name":    "Subject of the decision, or empty string if cross-cutting",
      "entity_type":    "project|person|topic|requirement   (empty string if cross-cutting)",
      "decision_date":  "YYYY-MM-DD (default to {valid_on} if unstated)",
      "authority":      "formal|informal|unknown",
      "rationale":      "1-2 sentences justifying the decision (or empty string)",
      "consequences":   "1-2 sentences describing impact (or empty string)",
      "reversal":       "Conditions under which the decision should be reversed (or empty string)",
      "source_quote":   "EXACT verbatim substring of the source body proving the decision was made"
    }}
  ]
}}

Rules:
- ONLY include decisions the source EXPLICITLY makes. Not opinions, options, or hypotheticals.
- source_quote MUST be copied verbatim — paraphrased quotes are rejected.
- entity_type, if non-empty, must be one of: project, person, topic, requirement.
- authority describes the source's tone: 'formal' (signed off / minuted), 'informal' (chat / casual), 'unknown' otherwise.
- If the source contains NO explicit decisions, return {{"decisions": []}}.

Source title: {title}
Source type: {source_type}
Source date: {valid_on}
Source body:
---
{body}
---
"""


@dataclass
class ExtractedDecision:
    title: str
    entity_name: str
    entity_type: str         # may be empty for cross-cutting decisions
    decision_date: str
    authority: str
    rationale: str
    consequences: str
    reversal: str
    source_quote: str


@dataclass
class PersistedDecision:
    decision_id: str
    decision_date: str
    entity_id: Optional[str]
    path: str


@dataclass
class DecisionExtractResult:
    persisted: list[PersistedDecision] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    cached: bool = False
    input_tokens: int = 0
    output_tokens: int = 0


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class DecisionExtractAgent:
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

    # ------------------------------------------------------------------

    async def extract(self, source_id: str, *, force: bool = False) -> DecisionExtractResult:
        source = await self._db.get_source(source_id)
        if source is None:
            raise KeyError(f"source {source_id!r} not found in kb.db")

        if not force:
            cached_row = await self._db.fetchall(
                "SELECT value FROM kb_meta WHERE key = ?",
                (f"decisions_done_for:{source_id}",),
            )
            if cached_row:
                existing = await self._db.list_decisions(source_id=source_id)
                return DecisionExtractResult(
                    persisted=[
                        PersistedDecision(
                            decision_id=r["id"],
                            decision_date=r["decision_date"],
                            entity_id=r["entity_id"],
                            path=r["path"],
                        )
                        for r in existing
                    ],
                    cached=True,
                )

        body = self._read_body(source)
        candidates, in_tok, out_tok = await self._call_with_retry(source, body)

        result = DecisionExtractResult(input_tokens=in_tok, output_tokens=out_tok)
        for raw in candidates:
            try:
                decision = self._validate(raw, body)
            except _ValidationError as exc:
                logger.warning("rejected decision from %s: %s", source_id, exc)
                result.rejected.append((str(exc), json.dumps(raw, ensure_ascii=False)))
                continue
            persisted = await self._persist(decision, source=source)
            if persisted:
                result.persisted.append(persisted)
        # Cache even when 0 decisions were emitted so re-runs don't re-LLM.
        await self._db.execute(
            "INSERT INTO kb_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"decisions_done_for:{source_id}",
             datetime.now(timezone.utc).isoformat()),
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
            items = data.get("decisions", [])
            if not isinstance(items, list):
                if attempt == self._max_retries:
                    raise ValueError(
                        f"'decisions' must be a list, got {type(items).__name__}"
                    )
                last_err = f"'decisions' was {type(items).__name__}, must be a list"
                prompt = _prompt(
                    title=source["title"], source_type=source["source_type"],
                    valid_on=valid_on, body=body or "(empty source body)",
                    retry_hint=last_err,
                )
                continue
            return items, in_tok, out_tok
        return [], in_tok, out_tok

    def _validate(self, raw: Any, body: str) -> ExtractedDecision:
        if not isinstance(raw, dict):
            raise _ValidationError(f"decision must be an object, got {type(raw).__name__}")

        def _req(field_: str) -> str:
            v = raw.get(field_)
            if not isinstance(v, str) or not v.strip():
                raise _ValidationError(f"missing or empty '{field_}'")
            return v.strip()

        title = _req("title")
        decision_date = _req("decision_date")
        authority = _req("authority").lower()
        source_quote = _req("source_quote")

        if authority not in _AUTHORITY_VALUES:
            raise _ValidationError(
                f"authority={authority!r} not in {_AUTHORITY_VALUES!r}"
            )
        if not _DATE_RE.match(decision_date):
            raise _ValidationError(
                f"decision_date={decision_date!r} is not YYYY-MM-DD"
            )

        entity_name = str(raw.get("entity_name", "")).strip()
        entity_type = str(raw.get("entity_type", "")).strip().lower()
        # entity_type may be empty (cross-cutting decision). If non-empty,
        # it must be in the closed vocab.
        if entity_type and entity_type not in ids.ENTITY_TYPES:
            raise _ValidationError(
                f"entity_type={entity_type!r} not in {sorted(ids.ENTITY_TYPES)!r}"
            )
        # entity_name and entity_type must be both filled or both empty
        if bool(entity_name) != bool(entity_type):
            raise _ValidationError(
                "entity_name and entity_type must be both set or both empty"
            )

        if not _quote_in_body(source_quote, body):
            raise _ValidationError(
                f"source_quote not found verbatim in source body "
                f"(quote={source_quote[:60]!r})"
            )

        return ExtractedDecision(
            title=title,
            entity_name=entity_name,
            entity_type=entity_type,
            decision_date=decision_date,
            authority=authority,
            rationale=str(raw.get("rationale", "")).strip(),
            consequences=str(raw.get("consequences", "")).strip(),
            reversal=str(raw.get("reversal", "")).strip(),
            source_quote=source_quote,
        )

    async def _persist(
        self, decision: ExtractedDecision, *, source: dict,
    ) -> Optional[PersistedDecision]:
        # Optional entity link
        entity_id: Optional[str] = None
        if decision.entity_name and decision.entity_type:
            link = await self._linker.resolve(
                name=decision.entity_name, entity_type=decision.entity_type,
            )
            if link is not None:
                entity_id = link.entity_id

        # Stable id with collision suffix on same date
        base_slug = ids.slugify(decision.title)
        base_id = ids.decision_id(decision.decision_date, base_slug)
        n = 1
        candidate = base_id
        path = self._layout.decision_path(decision.decision_date, base_slug)
        while await self._db.get_decision(candidate) is not None:
            n += 1
            candidate = f"{base_id}-{n}"
            path = self._layout.decision_path(
                decision.decision_date, f"{base_slug}-{n}"
            )
        decision_id = candidate

        rel_path = str(path.relative_to(self._layout.root)).replace("\\", "/")

        data = fm.build_decision(
            id=decision_id,
            decision_date=decision.decision_date,
            status="active",
            authority=decision.authority,
            source_id=source["id"],
            entity_id=entity_id,
            extra={"prompt_version": PROMPT_VERSION},
        )
        body_md = _render_body(decision, source=source)
        fm.write(path, data, body_md)

        await self._db.insert_decision(
            id=decision_id,
            decision_date=decision.decision_date,
            status="active",
            authority=decision.authority,
            source_id=source["id"],
            path=rel_path,
            entity_id=entity_id,
        )

        return PersistedDecision(
            decision_id=decision_id,
            decision_date=decision.decision_date,
            entity_id=entity_id,
            path=rel_path,
        )

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
    """Internal — per-candidate rejection."""


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
            f"DecisionExtractAgent: unparseable JSON: {exc}\n---\n{text[:400]}\n---"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"DecisionExtractAgent: expected JSON object, got {type(data).__name__}"
        )
    return data


def _render_body(d: ExtractedDecision, *, source: dict) -> str:
    lines = [
        f"# Decision: {d.title}",
        "",
        f"_Decision date: {d.decision_date} · authority: {d.authority}_",
        "",
        f"> {d.source_quote}",
        "",
    ]
    if d.rationale:
        lines.append("## Rationale")
        lines.append("")
        lines.append(d.rationale)
        lines.append("")
    if d.consequences:
        lines.append("## Consequences")
        lines.append("")
        lines.append(d.consequences)
        lines.append("")
    if d.reversal:
        lines.append("## Reversal Conditions")
        lines.append("")
        lines.append(d.reversal)
        lines.append("")
    lines.append(f"_Source: {source['id']}_")
    lines.append("")
    return "\n".join(lines)
