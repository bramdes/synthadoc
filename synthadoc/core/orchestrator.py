# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import logging

from synthadoc.config import Config, load_config
from synthadoc.core.cache import CacheManager
from synthadoc.core.cost_guard import CostGuard
from synthadoc.core.hooks import HookExecutor
from synthadoc.core.queue import JobQueue
from synthadoc.observability.telemetry import get_tracer, setup_telemetry
from synthadoc.providers import make_provider
from synthadoc.providers.ollama import OllamaProvider
from synthadoc.providers.pricing import estimate_cost
from synthadoc.storage.log import AuditDB, LogWriter
from synthadoc.storage.search import HybridSearch
from synthadoc.storage.wiki import WikiStorage
from synthadoc.storage import wiki_aliases as _wiki_aliases

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, wiki_root: Path, config: Optional[Config] = None) -> None:
        self._root = wiki_root
        self._cfg = config or load_config(
            project_config=wiki_root / ".synthadoc" / "config.toml")
        sd = wiki_root / ".synthadoc"
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "logs").mkdir(exist_ok=True)

        self._queue  = JobQueue(sd / "jobs.db", max_retries=self._cfg.queue.max_retries)
        self.queue   = self._queue
        self._audit  = AuditDB(sd / "audit.db")
        self._cache  = CacheManager(sd / "cache.db")
        self._store  = WikiStorage(wiki_root / "wiki")
        self._search = HybridSearch(
            self._store, sd / "embeddings.db",
            search_cfg=self._cfg.search,
        )
        self._log    = LogWriter(wiki_root / "log.md")
        self._cost   = CostGuard(self._cfg.cost)
        self._hooks  = HookExecutor(self._cfg.hooks)
        setup_telemetry(sd / "logs" / "traces.jsonl")

    async def init(self) -> None:
        await self._queue.init()
        await self._audit.init()
        await self._cache.init()
        self._log_agent_config()
        if self._cfg.search.vector:
            logger.info("Vector search: enabled (model: BAAI/bge-small-en-v1.5) — initialising…")
            try:
                await self._search.init_vector()
                asyncio.create_task(self._run_vector_migration())
            except ImportError:
                logger.warning(
                    "Vector search requires 'fastembed' which is not installed. "
                    "Run: pip install fastembed  then restart the server. "
                    "Falling back to BM25 search."
                )

    async def _run_vector_migration(self) -> None:
        """Embed all existing wiki pages not yet in embeddings.db (background task)."""
        import time
        if not self._cfg.search.vector or self._search._vector_store is None:
            return
        slugs = self._store.list_pages()
        embedded = set(await self._search._vector_store.list_slugs())
        to_embed = [s for s in slugs if s not in embedded]
        if not to_embed:
            logger.info("Vector search: all %d page(s) already embedded — ready", len(embedded))
            return
        logger.info(
            "Vector search: %d page(s) to embed (%d already done) — running background migration, BM25 active meanwhile",
            len(to_embed), len(embedded),
        )
        start = time.monotonic()
        for i, slug in enumerate(to_embed, 1):
            page = self._store.read_page(slug)
            if page:
                text = f"{page.title} {' '.join(page.tags)} {page.content}"
                await self._search.embed_page(slug, text)
            if i % 50 == 0:
                logger.info("Vector migration: %d/%d pages embedded…", i, len(to_embed))
            await asyncio.sleep(0)
        logger.info(
            "Vector migration complete — %d pages, %.0fs",
            len(to_embed), time.monotonic() - start,
        )

    def _log_agent_config(self) -> None:
        """Log the effective provider/model for each named agent slot at startup."""
        slots = ["default", "ingest", "query", "lint", "skill"]
        parts = []
        seen: dict[str, str] = {}
        for slot in slots:
            cfg = self._cfg.agents.resolve(slot)
            label = f"{cfg.provider}/{cfg.model}"
            raw = getattr(self._cfg.agents, slot, None)
            if slot == "default" or raw is not None:
                parts.append(f"{slot}={label}")
                seen[slot] = label
        logger.info("LLM agents — %s", " | ".join(parts))

    async def ingest(self, source: str, force: bool = False) -> str:
        """Enqueue an ingest job. The server worker loop executes it."""
        return await self._queue.enqueue("ingest", {"source": source, "force": force})

    async def resume(self) -> int:
        """Re-enqueue all pending and failed jobs."""
        from synthadoc.core.queue import JobStatus
        jobs = await self._queue.list_jobs(status=JobStatus.PENDING)
        jobs += await self._queue.list_jobs(status=JobStatus.FAILED)
        for job in jobs:
            await self._queue.retry(job.id)
        return len(jobs)

    async def _run_ingest(self, job_id: str, source: str, auto_confirm: bool,
                          force: bool = False, max_results: int | None = None) -> None:
        # auto_confirm is reserved for when user-facing confirmation prompts are added.
        # Cost tracking is live in v0.2 but cost_guard.check() is not yet enforced here.
        from synthadoc.agents.ingest_agent import IngestAgent
        from synthadoc.skills.web_search.scripts.main import _INTENT_RE as _WEB_SEARCH_RE
        try:
            _is_web_search = bool(_WEB_SEARCH_RE.match(source))
            if _is_web_search:
                await self._queue.update_progress(job_id, {"phase": "searching"})
            _agent_cfg = self._cfg.agents.resolve("ingest")
            agent = IngestAgent(
                provider=make_provider("ingest", self._cfg),
                store=self._store, search=self._search,
                log_writer=self._log, audit_db=self._audit,
                cache=self._cache, max_pages=self._cfg.ingest.max_pages_per_ingest,
                cache_version=self._cfg.cache.version,
                fetch_timeout=self._cfg.ingest.fetch_timeout_seconds,
                decision_max_tokens=self._cfg.ingest.decision_max_tokens,
                model=_agent_cfg.model,
                is_local=(_agent_cfg.provider == "ollama"),
                wiki_aliases=_wiki_aliases.load(self._root / "wiki_aliases.yaml"),
            )
            # The agent finalizes result.cost_usd before it writes the audit
            # ledger + activity log, so both reflect the real cost (not $0).
            result = await agent.ingest(source, force=force, bust_cache=force)
            # Recompute for the job-result path (same value the agent used) so
            # the queue result stays authoritative even if the agent was given
            # no pricing info.
            result.cost_usd = estimate_cost(
                _agent_cfg.model,
                result.input_tokens,
                result.output_tokens,
                is_local=(_agent_cfg.provider == "ollama"),
            )
            if max_results is not None and result.child_sources:
                result.child_sources = result.child_sources[:max_results]
            if _is_web_search and result.child_sources:
                await self._queue.update_progress(job_id, {
                    "phase": "found_urls",
                    "total": len(result.child_sources),
                })
            # Fan out web search child sources — batch insert in one transaction
            if result.child_sources:
                child_ids = await self._queue.enqueue_many(
                    "ingest",
                    [{"source": s, "force": False} for s in result.child_sources],
                )
            else:
                child_ids = []

            await self._queue.complete(job_id, result={
                "pages_created": result.pages_created,
                "pages_updated": result.pages_updated,
                "pages_flagged": result.pages_flagged,
                "child_sources_enqueued": len(result.child_sources),
                "child_job_ids": child_ids,
                "tokens_used": result.tokens_used,
                "cost_usd": result.cost_usd,
            })
            # Embed newly written pages for vector search
            if self._cfg.search.vector:
                for slug in result.pages_created + result.pages_updated:
                    page = self._store.read_page(slug)
                    if page:
                        text = f"{page.title} {' '.join(page.tags)} {page.content}"
                        await self._search.embed_page(slug, text)
            self._hooks.fire("on_ingest_complete", {
                "event": "on_ingest_complete", "wiki": str(self._root),
                "source": source,
                "pages_created": result.pages_created,
                "pages_updated": result.pages_updated,
                "pages_flagged": result.pages_flagged,
                "tokens_used": result.tokens_used,
                "cost_usd": result.cost_usd,
            })
            # Best-effort parallel fact-tier pipeline (plan §9 Layer 2).
            # Failures here never affect the page-tier ingest result.
            await self._enqueue_kb_pipeline(source)
        except (NotImplementedError, FileNotFoundError) as e:
            # Permanent failures — source is invalid, retry can never help
            await self._queue.fail_permanent(job_id, str(e))
        except EnvironmentError as e:
            # Provider binary not installed (ERR-PROV-003) — log cleanly, no traceback
            logging.getLogger(__name__).warning("%s", e)
            await self._queue.fail_permanent(job_id, str(e))
        except Exception as e:
            import httpx
            import logging
            from synthadoc.errors import (
                DomainBlockedException, DailyQuotaExhaustedException,
                CodingToolQuotaExhaustedException,
            )
            # Check for LLM rate limits (openai SDK used by Groq/Gemini, and Anthropic SDK)
            _status = getattr(e, "status_code", None) or getattr(
                getattr(e, "response", None), "status_code", None)
            if isinstance(e, DailyQuotaExhaustedException):
                # Daily quota exhaustion is permanent for the rest of the day —
                # permanently fail the job so the worker does NOT sleep-and-retry.
                logging.getLogger(__name__).error(
                    "Daily quota exhausted — permanently failing job %s "
                    "(quota resets at midnight UTC)", job_id
                )
                await self._queue.fail_permanent(job_id, str(e))
            elif isinstance(e, CodingToolQuotaExhaustedException):
                logging.getLogger(__name__).error(
                    "Coding tool quota exhausted — permanently failing job %s", job_id
                )
                await self._queue.fail_permanent(job_id, str(e))
                # Do NOT raise: letting the worker continue drains the pending
                # queue quickly rather than looping every 60 s.
            elif _status == 429:
                # Per-minute rate limit — requeue without burning a retry; worker will sleep
                logging.getLogger(__name__).warning(
                    "LLM rate limit for job %s — requeued without retry penalty", job_id
                )
                await self._queue.requeue(job_id, f"rate_limit: {e}")
                raise
            elif isinstance(e, DomainBlockedException):
                await self._auto_block_domain(e)
                await self._queue.skip(job_id, str(e))
            elif isinstance(e, (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout,
                                   httpx.ConnectError, httpx.ReadError)):
                # Transient network error (timeout, connection refused, dropped) — retry with backoff.
                logging.getLogger(__name__).warning(
                    "URL fetch failed for job %s (%s: %s) — will retry", job_id, source,
                    type(e).__name__
                )
                await self._queue.fail(job_id, f"{type(e).__name__}: {source}")
            elif isinstance(e, httpx.HTTPStatusError):
                status = e.response.status_code
                if 400 <= status < 500:
                    # Permanent client error (404, 410, 451, etc.) — skip, no retry, no traceback.
                    logging.getLogger(__name__).warning(
                        "HTTP %s fetching %s — skipping job %s", status, source, job_id
                    )
                    await self._queue.skip(job_id, f"HTTP {status}: {source}")
                else:
                    # 5xx server error — transient, retry with backoff, no traceback.
                    logging.getLogger(__name__).warning(
                        "HTTP %s fetching %s — will retry job %s", status, source, job_id
                    )
                    await self._queue.fail(job_id, f"HTTP {status}: {source}")
            else:
                await self._queue.fail(job_id, str(e))
                raise

    async def _auto_block_domain(self, exc: "DomainBlockedException") -> None:
        """Persist a newly discovered blocked domain and record an audit event."""
        import json
        import logging
        from datetime import datetime, timezone

        blocked_path = self._root / ".synthadoc" / "blocked_domains.json"
        try:
            existing: list = json.loads(blocked_path.read_text(encoding="utf-8")) \
                if blocked_path.exists() else []
            if exc.domain not in existing:
                existing.append(exc.domain)
                blocked_path.write_text(
                    json.dumps(existing, indent=2), encoding="utf-8"
                )
        except Exception as write_err:
            logging.getLogger(__name__).warning(
                "Could not persist blocked domain %s: %s", exc.domain, write_err
            )

        try:
            await self._audit.record_audit_event(
                job_id="system",
                event="domain_auto_blocked",
                metadata={
                    "domain": exc.domain,
                    "url": exc.url,
                    "status_code": exc.status_code,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            pass

    async def query(self, question: str):
        from synthadoc.agents.query_agent import QueryAgent
        _provider = make_provider("query", self._cfg)
        result = await QueryAgent(
            provider=_provider,
            store=self._store, search=self._search,
            gap_score_threshold=self._cfg.query.gap_score_threshold,
            wiki_root=self._root,
        ).query(question)
        _model = self._cfg.agents.resolve("query").model
        cost_usd = estimate_cost(
            _model,
            result.input_tokens,
            result.output_tokens,
            is_local=isinstance(_provider, OllamaProvider),
        )
        await self._audit.record_query(
            question=question,
            sub_questions_count=result.sub_questions_count or 1,
            tokens=result.tokens_used,
            cost_usd=cost_usd,
        )
        self._log.log_query(
            question=question,
            sub_questions=len(result.citations) or 1,
            citations=result.citations,
            tokens=result.tokens_used,
            cost_usd=cost_usd,
        )
        return result

    async def lint(self, scope: str = "all", auto_resolve: bool = False) -> str:
        """Enqueue a lint job. The server worker loop executes it."""
        return await self._queue.enqueue("lint", {"scope": scope, "auto_resolve": auto_resolve})

    async def consolidate(self, slug: str, force: bool = False,
                          dry_run: bool = False):
        """Rewrite an accumulated wiki page into a curated form."""
        from synthadoc.agents.consolidate_agent import ConsolidateAgent
        _provider = make_provider("ingest", self._cfg)
        result = await ConsolidateAgent(
            provider=_provider, store=self._store, search=self._search,
            wiki_root=self._root,
            max_tokens=self._cfg.ingest.decision_max_tokens,
        ).consolidate(slug, force=force, dry_run=dry_run)
        _model = self._cfg.agents.resolve("ingest").model
        cost_usd = estimate_cost(
            _model,
            result.input_tokens,
            result.output_tokens,
            is_local=isinstance(_provider, OllamaProvider),
        )
        return result, cost_usd

    async def _run_scaffold(self, job_id: str, domain: str) -> None:
        from synthadoc.agents.scaffold_agent import ScaffoldAgent
        try:
            protected_slugs = self._store.list_pages()
            result = await ScaffoldAgent(
                provider=make_provider("ingest", self._cfg)
            ).scaffold(domain=domain, protected_slugs=protected_slugs or None)
            (self._root / "wiki" / "index.md").write_text(
                result.index_md, encoding="utf-8", newline="\n")
            (self._root / "AGENTS.md").write_text(
                result.agents_md, encoding="utf-8", newline="\n")
            (self._root / "wiki" / "purpose.md").write_text(
                result.purpose_md, encoding="utf-8", newline="\n")
            await self._queue.complete(job_id, result={
                "domain": domain,
                "categories": len(result.index_md.splitlines()),
            })
        except Exception as e:
            await self._queue.fail(job_id, str(e))
            raise

    # -----------------------------------------------------------------------
    # Temporal KB (Layer 2) — best-effort fact-tier pipeline
    # -----------------------------------------------------------------------

    async def _enqueue_kb_pipeline(self, source: str) -> Optional[str]:
        """Import a local-file source into kb.db and enqueue a kb_pipeline job.

        Never raises — best-effort, non-fatal per plan §9 Layer 2. Returns the
        new job id, or ``None`` if the import was skipped (URL/web-search
        source, missing file, kb tier not initialised, or anything else).
        """
        from synthadoc.kb.layout import KBLayout
        from synthadoc.kb.db import KBDB
        from synthadoc.kb.import_source import guess_source_type, import_source

        try:
            src_path = Path(source)
            if not src_path.is_file():
                return None  # URL, web-search, or otherwise non-local — skip
            layout = KBLayout(self._root)
            if not layout.db_path.exists():
                logger.debug("kb tier not initialised for %s — skipping fact-tier pipeline",
                             self._root)
                return None
            db = KBDB(layout.db_path)
            await db.init()
            src_id, _ = await import_source(
                src_path=src_path,
                source_type=guess_source_type(src_path),
                db=db, layout=layout,
            )
            return await self._queue.enqueue(
                "kb_pipeline", {"source_id": src_id},
            )
        except Exception as exc:
            logger.warning(
                "kb pipeline enqueue failed for %s: %s", source, exc,
            )
            return None

    async def _run_kb_pipeline(self, job_id: str, source_id: str) -> None:
        """Worker entry point for the ``kb_pipeline`` operation.

        Loads the kb-side dependencies, runs the pipeline, and marks the job
        complete. The pipeline itself never raises — failures land in
        ``result.errors`` and are surfaced via the job result payload.
        """
        from synthadoc.kb.db import KBDB
        from synthadoc.kb.layout import KBLayout
        from synthadoc.kb.pipeline import run_pipeline
        from synthadoc.kb.rules import load as load_rules

        try:
            layout = KBLayout(self._root)
            if not layout.db_path.exists():
                await self._queue.fail_permanent(
                    job_id, "kb tier not initialised (run `synthadoc kb init`)"
                )
                return
            db = KBDB(layout.db_path)
            await db.init()
            rules = load_rules(layout.config_path)
            summary_provider = make_provider("summary", self._cfg)
            facts_provider = make_provider("facts", self._cfg)
            result = await run_pipeline(
                source_id=source_id,
                db=db, layout=layout, rules=rules,
                summary_provider=summary_provider,
                facts_provider=facts_provider,
                max_tokens_per_fact_extract=self._cfg.ingest.max_tokens_per_fact_extract,
            )
            # Price each token bucket against the model that produced it
            # (summary and facts roles can resolve to different models), then
            # record the fact-tier cost so `audit cost` sees it. Best-effort.
            try:
                _sum_cfg = self._cfg.agents.resolve("summary")
                _fct_cfg = self._cfg.agents.resolve("facts")
                kb_cost = (
                    estimate_cost(_sum_cfg.model,
                                  result.summary_input_tokens,
                                  result.summary_output_tokens,
                                  is_local=(_sum_cfg.provider == "ollama"))
                    + estimate_cost(_fct_cfg.model,
                                    result.extract_input_tokens,
                                    result.extract_output_tokens,
                                    is_local=(_fct_cfg.provider == "ollama"))
                )
                await self._audit.record_kb_pipeline_cost(
                    source_id=source_id,
                    input_tokens=result.summary_input_tokens + result.extract_input_tokens,
                    output_tokens=result.summary_output_tokens + result.extract_output_tokens,
                    cost_usd=kb_cost,
                    model=_fct_cfg.model,
                )
            except Exception as cost_exc:
                logger.warning("kb pipeline cost not recorded for %s: %s",
                               source_id, cost_exc)
            await self._queue.complete(job_id, result={
                "source_id": source_id,
                "summary_written": result.summary_written,
                "facts_persisted": result.facts_persisted,
                "facts_rejected": result.facts_rejected,
                "entities_rendered": result.entities_rendered,
                "entities_queued_for_review": result.entities_queued_for_review,
                "tokens_used": result.total_tokens,
                "errors": result.errors,
                "ok": result.ok,
            })
        except Exception as exc:
            # Defensive — run_pipeline shouldn't raise, but if anything in the
            # surrounding setup does, we fail the job cleanly.
            await self._queue.fail(job_id, f"{type(exc).__name__}: {exc}")
            raise

    async def _run_lint(self, job_id: str, scope: str = "all", auto_resolve: bool = False) -> None:
        from synthadoc.agents.lint_agent import LintAgent
        try:
            report = await LintAgent(
                provider=make_provider("lint", self._cfg),
                store=self._store, log_writer=self._log,
                confidence_threshold=self._cfg.cost.auto_resolve_confidence_threshold,
            ).lint(scope=scope, auto_resolve=auto_resolve)
            await self._queue.complete(job_id)
            self._hooks.fire("on_lint_complete", {
                "event": "on_lint_complete", "wiki": str(self._root),
                "contradictions_found": report.contradictions_found,
                "orphans": report.orphan_slugs,
            })
        except Exception as e:
            from synthadoc.errors import DailyQuotaExhaustedException, CodingToolQuotaExhaustedException
            if isinstance(e, (DailyQuotaExhaustedException, CodingToolQuotaExhaustedException)):
                await self._queue.fail_permanent(job_id, str(e))
            else:
                await self._queue.fail(job_id, str(e))
                raise
