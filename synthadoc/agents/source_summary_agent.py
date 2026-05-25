# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""SourceSummaryAgent — one source in, one source-summary markdown out.

Plan §9 Layer 2 / spec §4.2. The summary is **strictly bounded to the
source's own content** — no merging with prior knowledge from other
sources. The agent's job is to produce a faithful precis, not synthesis.

Output is two artefacts:

1. A markdown file under ``kb/source_summaries/<source_type_alias>/<slug>.md``
   with discriminated frontmatter (``type: source_summary``).
2. A row in ``kb.db.source_summaries``.

Both are written atomically per-source — if either fails the agent
re-raises so the queue can retry. Idempotent: re-running on an
already-summarised source skips the LLM call (the cached layer above
handles the actual response cache; this agent's contract is "produce the
expected files in the expected place").
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from synthadoc.kb import frontmatter as fm
from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.providers.base import LLMProvider, Message

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"

_SYSTEM = (
    "You are a careful information-extraction assistant. "
    "Your task is to summarise ONE source document on its own terms. "
    "Do NOT merge in knowledge from other sources. "
    "Do NOT speculate beyond what the source states. "
    "Return ONLY valid JSON — no markdown fences, no commentary."
)


def _prompt(*, title: str, source_type: str, valid_on: str,
            body: str, retry_hint: str = "") -> str:
    hint = (f"\nIMPORTANT: previous attempt failed validation:\n{retry_hint}\n"
            if retry_hint else "")
    return f"""\
Summarise the source below.{hint}

Return JSON with this exact shape:

{{
  "summary":        "2-4 sentence neutral precis of what THIS source says.",
  "key_points":     ["short bullet 1", "short bullet 2", "..."],
  "mentioned_entities": [
    {{"name": "Exact name as it appears", "entity_type": "project|person|topic|requirement"}}
  ],
  "decisions_mentioned": ["short description of each explicit decision, or empty list"],
  "open_questions":     ["short description of each open question, or empty list"],
  "action_items":       ["short description of each action item, or empty list"],
  "source_reliability": "high|medium|low"
}}

Rules:
- ONLY use information present in the source below.
- Do not infer entities from context unless they are explicitly named.
- "entity_type" must be exactly one of: project, person, topic, requirement.
- Keep each list short. Quality over quantity.

Source title: {title}
Source type: {source_type}
Source date: {valid_on}
Source body:
---
{body}
---
"""


@dataclass
class SourceSummary:
    """Structured payload returned by :meth:`SourceSummaryAgent.summarise`."""

    summary: str
    key_points: list[str]
    mentioned_entities: list[dict]
    decisions_mentioned: list[str]
    open_questions: list[str]
    action_items: list[str]
    source_reliability: str
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class SummariseResult:
    """End-to-end result: structured summary + paths it landed at."""

    source_id: str
    summary_id: str
    summary_path: Path
    summary: SourceSummary
    cached: bool = False


class SourceSummaryAgent:
    """Async agent. Stateless once constructed."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        db: KBDB,
        layout: KBLayout,
        max_body_chars: int = 40_000,
        max_retries: int = 1,
    ) -> None:
        self._provider = provider
        self._db = db
        self._layout = layout
        self._max_body_chars = max_body_chars
        self._max_retries = max_retries

    # ------------------------------------------------------------------

    async def summarise(self, source_id: str, *, force: bool = False) -> SummariseResult:
        """Generate (or fetch) the summary for *source_id*.

        Raises ``KeyError`` if the source is unknown to the DB.
        """
        source = await self._db.get_source(source_id)
        if source is None:
            raise KeyError(f"source {source_id!r} not found in kb.db")

        summary_id = ids.summary_source_id(source_id)
        source_type = source["source_type"]
        type_alias = ids.source_type_alias(source_type)
        slug = ids.split_source_id(source_id)[2]
        summary_path = self._layout.source_summary_dir(source_type) / f"{slug}.md"

        # Idempotency — if the summary file already exists and DB row says so,
        # skip the LLM call. Pass force=True to override.
        existing_row = await self._db.fetchall(
            "SELECT * FROM source_summaries WHERE id = ?", (summary_id,)
        )
        if existing_row and summary_path.exists() and not force:
            logger.info("source_summary %s already exists — skipping", summary_id)
            summary = self._parse_existing(summary_path)
            return SummariseResult(
                source_id=source_id,
                summary_id=summary_id,
                summary_path=summary_path,
                summary=summary,
                cached=True,
            )

        body = self._read_parsed_body(source)
        summary = await self._call_llm(source, body)

        self._write_markdown(
            summary_id=summary_id,
            source_id=source_id,
            summary=summary,
            target=summary_path,
            title=source["title"],
            source_type=source_type,
        )

        path_rel = str(summary_path.relative_to(self._layout.root)).replace("\\", "/")
        # Upsert (insert-or-replace by id) — keeps re-runs clean.
        await self._db.execute(
            "INSERT INTO source_summaries (id, source_id, path, review_status, "
            "confidence, generated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  path = excluded.path,"
            "  confidence = excluded.confidence,"
            "  generated_at = excluded.generated_at",
            (summary_id, source_id, path_rel, "unreviewed",
             self._confidence_from_reliability(summary.source_reliability),
             datetime.now(timezone.utc).isoformat()),
        )

        return SummariseResult(
            source_id=source_id,
            summary_id=summary_id,
            summary_path=summary_path,
            summary=summary,
            cached=False,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _read_parsed_body(self, source: dict) -> str:
        parsed = source.get("parsed_path")
        if not parsed:
            return ""
        path = self._layout.root / parsed
        if not path.exists():
            logger.warning("parsed source missing for %s at %s", source["id"], path)
            return ""
        body = path.read_text(encoding="utf-8")
        if len(body) > self._max_body_chars:
            logger.info(
                "truncating source %s body from %d to %d chars",
                source["id"], len(body), self._max_body_chars,
            )
            body = body[: self._max_body_chars]
        return body

    async def _call_llm(self, source: dict, body: str) -> SourceSummary:
        """Call the LLM with one retry on parse failure.

        Gemini and similar long-context models sometimes truncate mid-string
        when the response brushes against ``max_tokens``. The retry feeds the
        parse error back in the prompt so the model can shorten its output.
        """
        valid_on = ids.split_source_id(source["id"])[1]
        prompt = _prompt(
            title=source["title"],
            source_type=source["source_type"],
            valid_on=valid_on,
            body=body or "(empty source body)",
        )
        input_tokens = 0
        output_tokens = 0
        last_err: Optional[str] = None
        data: dict[str, Any] | None = None
        for attempt in range(self._max_retries + 1):
            resp = await self._provider.complete(
                messages=[Message(role="user", content=prompt)],
                system=_SYSTEM,
                temperature=0.0,
                max_tokens=4096,
            )
            input_tokens += resp.input_tokens
            output_tokens += resp.output_tokens
            try:
                data = _parse_json(resp.text)
                break
            except ValueError as exc:
                last_err = str(exc)
                if attempt == self._max_retries:
                    raise
                logger.warning(
                    "source summary JSON parse failed (attempt %d): %s",
                    attempt + 1, exc,
                )
                prompt = _prompt(
                    title=source["title"],
                    source_type=source["source_type"],
                    valid_on=valid_on,
                    body=body or "(empty source body)",
                    retry_hint=last_err,
                )
        assert data is not None  # for type-checker; either set or raised
        return SourceSummary(
            summary=str(data.get("summary", "")).strip(),
            key_points=_strip_strs(data.get("key_points", [])),
            mentioned_entities=_clean_entities(data.get("mentioned_entities", [])),
            decisions_mentioned=_strip_strs(data.get("decisions_mentioned", [])),
            open_questions=_strip_strs(data.get("open_questions", [])),
            action_items=_strip_strs(data.get("action_items", [])),
            source_reliability=_clean_reliability(data.get("source_reliability", "medium")),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _write_markdown(
        self,
        *,
        summary_id: str,
        source_id: str,
        summary: SourceSummary,
        target: Path,
        title: str,
        source_type: str,
    ) -> None:
        confidence = self._confidence_from_reliability(summary.source_reliability)
        data = fm.build_source_summary(
            id=summary_id,
            source_id=source_id,
            confidence=confidence,
            extra={"prompt_version": PROMPT_VERSION},
        )
        body = _render_body(title=title, source_type=source_type, summary=summary)
        target.parent.mkdir(parents=True, exist_ok=True)
        fm.write(target, data, body)

    def _parse_existing(self, path: Path) -> SourceSummary:
        """Read a previously-written summary back into a SourceSummary.

        Used by the cached-path branch so the caller always gets the same
        return shape. The JSON-y fields are not recovered (they're not in the
        markdown body); only the summary prose. Good enough for callers that
        only need to know "yes it's there".
        """
        text = path.read_text(encoding="utf-8")
        parts = text.split("---", 2)
        body = parts[2] if len(parts) >= 3 else text
        # Pull the "## Summary" paragraph
        m = re.search(r"## Summary\s+(.+?)(?:\n##|\Z)", body, re.DOTALL)
        prose = m.group(1).strip() if m else body.strip()
        return SourceSummary(
            summary=prose,
            key_points=[], mentioned_entities=[], decisions_mentioned=[],
            open_questions=[], action_items=[], source_reliability="medium",
        )

    @staticmethod
    def _confidence_from_reliability(reliability: str) -> str:
        return {"high": "high", "medium": "medium", "low": "low"}.get(
            reliability.lower(), "medium"
        )


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


_FENCE_PAIR_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_FENCE_OPEN_RE = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)


def _strip_fences(text: str) -> str:
    """Strip a ```json … ``` fence from an LLM response.

    Handles two cases:
      * Matched-pair fence: ```json\\n{ … }\\n``` → return the inner body.
      * Opening-only fence (response truncated before the closing fence):
        ```json\\n{ … (cut off) → strip the opener, keep the rest.
    """
    raw = text.strip()
    m = _FENCE_PAIR_RE.search(raw)
    if m:
        return m.group(1)
    # Opening-fence-only fallback (truncated response)
    m = _FENCE_OPEN_RE.match(raw)
    if m:
        return raw[m.end():]
    return raw


def _parse_json(text: str) -> dict[str, Any]:
    """Parse a JSON blob that the LLM may have wrapped in ```json``` fences."""
    raw = _strip_fences(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"SourceSummaryAgent: LLM returned unparseable JSON: {exc}\n"
            f"---\n{text[:400]}\n---"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"SourceSummaryAgent: expected JSON object, got {type(data).__name__}"
        )
    return data


def _strip_strs(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for it in items:
        if isinstance(it, str) and it.strip():
            out.append(it.strip())
    return out


def _clean_entities(items: Any) -> list[dict]:
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name", "")).strip()
        et = str(it.get("entity_type", "")).strip().lower()
        if not name or et not in ids.ENTITY_TYPES:
            continue
        out.append({"name": name, "entity_type": et})
    return out


def _clean_reliability(value: Any) -> str:
    v = str(value).strip().lower()
    return v if v in ("high", "medium", "low") else "medium"


def _render_body(*, title: str, source_type: str, summary: SourceSummary) -> str:
    lines: list[str] = [f"# Source Summary: {title}", ""]
    lines.append("## Summary")
    lines.append("")
    lines.append(summary.summary or "_(no summary returned)_")
    lines.append("")

    def _section(heading: str, items: list[str]) -> None:
        lines.append(f"## {heading}")
        lines.append("")
        if items:
            for it in items:
                lines.append(f"- {it}")
        else:
            lines.append("_None._")
        lines.append("")

    _section("Key Points", summary.key_points)

    lines.append("## Mentioned Entities")
    lines.append("")
    if summary.mentioned_entities:
        for ent in summary.mentioned_entities:
            lines.append(f"- **{ent['name']}** _(type: {ent['entity_type']})_")
    else:
        lines.append("_None._")
    lines.append("")

    _section("Decisions Mentioned", summary.decisions_mentioned)
    _section("Open Questions", summary.open_questions)
    _section("Action Items", summary.action_items)

    lines.append(f"_Source reliability: {summary.source_reliability}_")
    lines.append("")
    return "\n".join(lines)
