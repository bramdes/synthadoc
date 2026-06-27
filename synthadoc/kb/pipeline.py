# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""End-to-end fact-tier pipeline for one source.

This is the orchestration glue that runs Layer 2's agents in order:

    SourceSummaryAgent  →  FactExtractAgent  →  resolve_db  →  EntityRenderAgent

Designed to be called from the job queue (one source per ``kb_pipeline``
job) and from tests. Failures are caught and surfaced on the result
object — they do **not** raise. The page tier remains the authoritative
ingest path; the fact tier is best-effort.

Plan §9 Layer 2 final item: "Wire parallel fact pass into IngestAgent
(best-effort, non-fatal)". This module is what the wire-up dispatches
to; the orchestrator only needs to enqueue + invoke.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from synthadoc.agents.decision_extract_agent import DecisionExtractAgent
from synthadoc.agents.entity_render_agent import EntityRenderAgent
from synthadoc.agents.fact_extract_agent import FactExtractAgent
from synthadoc.agents.source_summary_agent import SourceSummaryAgent
from synthadoc.agents.unknown_extract_agent import UnknownExtractAgent
from synthadoc.kb.db import KBDB
from synthadoc.kb.entity_linker import EntityLinker
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.resolver import resolve_db
from synthadoc.kb.rules import Rules
from synthadoc.providers.base import LLMProvider

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    """What happened during one pipeline run. Inspect after `.ok` is True."""

    source_id: str
    summary_written: bool = False
    facts_persisted: int = 0
    facts_rejected: int = 0
    decisions_persisted: int = 0
    decisions_rejected: int = 0
    unknowns_persisted: int = 0
    unknowns_rejected: int = 0
    entities_touched: tuple[str, ...] = ()
    entities_rendered: int = 0
    entities_queued_for_review: int = 0
    errors: list[str] = field(default_factory=list)
    # Token usage, split by model role so the caller can price each bucket
    # against the right model (summary vs facts can run on different models).
    # Cache hits contribute zero — no new LLM call was made.
    summary_input_tokens: int = 0
    summary_output_tokens: int = 0
    extract_input_tokens: int = 0      # facts + decisions + unknowns
    extract_output_tokens: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def total_tokens(self) -> int:
        return (self.summary_input_tokens + self.summary_output_tokens
                + self.extract_input_tokens + self.extract_output_tokens)


async def run_pipeline(
    *,
    source_id: str,
    db: KBDB,
    layout: KBLayout,
    rules: Rules,
    summary_provider: LLMProvider,
    facts_provider: LLMProvider,
    force: bool = False,
    max_tokens_per_fact_extract: int = 0,
) -> PipelineResult:
    """Run summary → extract → resolve → render for a single source.

    The two provider arguments let callers point summary at a cheaper model
    than fact extraction (per plan §11 risk mitigation; the `facts` agent
    role exists for exactly this reason). Pass the same provider twice for
    a unified model.

    Never raises. All failures are caught and surfaced on the result's
    ``errors`` list so the caller can record the partial outcome and let
    the page-tier pipeline succeed regardless.
    """
    result = PipelineResult(source_id=source_id)

    linker = EntityLinker(db, layout)
    summary_agent = SourceSummaryAgent(provider=summary_provider, db=db, layout=layout)
    fact_agent = FactExtractAgent(
        provider=facts_provider, db=db, layout=layout, linker=linker,
        max_tokens_per_source=max_tokens_per_fact_extract,
    )
    decision_agent = DecisionExtractAgent(
        provider=facts_provider, db=db, layout=layout, linker=linker,
    )
    unknown_agent = UnknownExtractAgent(
        provider=facts_provider, db=db, layout=layout, linker=linker,
    )
    render_agent = EntityRenderAgent(db=db, layout=layout, rules=rules)

    # 1. Summarise
    try:
        summary = await summary_agent.summarise(source_id, force=force)
        result.summary_written = not summary.cached
        if not summary.cached:
            result.summary_input_tokens += summary.summary.input_tokens
            result.summary_output_tokens += summary.summary.output_tokens
    except Exception as exc:
        logger.warning("kb pipeline: summary failed for %s: %s", source_id, exc)
        result.errors.append(f"summary: {type(exc).__name__}: {exc}")

    # 2. Extract facts
    touched_entities: set[str] = set()
    try:
        extract = await fact_agent.extract(source_id, force=force)
        result.facts_persisted = len(extract.persisted)
        result.facts_rejected = len(extract.rejected)
        result.extract_input_tokens += extract.input_tokens
        result.extract_output_tokens += extract.output_tokens
        for p in extract.persisted:
            touched_entities.add(p.entity_id)
    except Exception as exc:
        logger.warning("kb pipeline: fact extract failed for %s: %s", source_id, exc)
        result.errors.append(f"facts: {type(exc).__name__}: {exc}")

    # 3. Extract decisions (best-effort, side-channel)
    try:
        decisions = await decision_agent.extract(source_id, force=force)
        result.decisions_persisted = len(decisions.persisted)
        result.decisions_rejected = len(decisions.rejected)
        result.extract_input_tokens += decisions.input_tokens
        result.extract_output_tokens += decisions.output_tokens
        for d in decisions.persisted:
            if d.entity_id:
                touched_entities.add(d.entity_id)
    except Exception as exc:
        logger.warning("kb pipeline: decision extract failed for %s: %s", source_id, exc)
        result.errors.append(f"decisions: {type(exc).__name__}: {exc}")

    # 4. Extract unknowns (best-effort, side-channel)
    try:
        unknowns = await unknown_agent.extract(source_id, force=force)
        result.unknowns_persisted = len(unknowns.persisted)
        result.unknowns_rejected = len(unknowns.rejected)
        result.extract_input_tokens += unknowns.input_tokens
        result.extract_output_tokens += unknowns.output_tokens
        for u in unknowns.persisted:
            if u.entity_id:
                touched_entities.add(u.entity_id)
    except Exception as exc:
        logger.warning("kb pipeline: unknown extract failed for %s: %s", source_id, exc)
        result.errors.append(f"unknowns: {type(exc).__name__}: {exc}")

    result.entities_touched = tuple(sorted(touched_entities))

    # 5. Resolve (DB-wide, idempotent). Only run if at least one fact landed.
    if any(p for p in [result.facts_persisted, result.decisions_persisted,
                       result.unknowns_persisted]):
        try:
            await resolve_db(db, rules)
        except Exception as exc:
            logger.warning("kb pipeline: resolver failed: %s", exc)
            result.errors.append(f"resolve: {type(exc).__name__}: {exc}")

    # 6. Render affected entities
    for entity_id in result.entities_touched:
        try:
            render = await render_agent.render(entity_id)
            if render.written:
                result.entities_rendered += 1
            if render.queued_for_review:
                result.entities_queued_for_review += 1
        except Exception as exc:
            logger.warning(
                "kb pipeline: render failed for %s: %s", entity_id, exc,
            )
            result.errors.append(f"render({entity_id}): {type(exc).__name__}: {exc}")

    return result
