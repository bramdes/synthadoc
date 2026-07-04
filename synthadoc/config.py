# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Configuration system for synthadoc.

Loads TOML configuration from global and project-level files,
merges them (project wins), and validates the result.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Known providers
# ---------------------------------------------------------------------------

KNOWN_PROVIDERS = {"anthropic", "openai", "ollama", "gemini", "groq", "minimax", "deepseek",
                   "claude-code", "opencode"}


# ---------------------------------------------------------------------------
# Leaf dataclasses
# ---------------------------------------------------------------------------


@dataclass
class AgentConfig:
    provider: str
    model: str
    base_url: str = ""


@dataclass
class AgentsConfig:
    default: AgentConfig
    ingest: Optional[AgentConfig] = None
    query: Optional[AgentConfig] = None
    lint: Optional[AgentConfig] = None
    skill: Optional[AgentConfig] = None
    # Temporal-KB roles (Layer 2). Default = inherit from `ingest` / `default`.
    facts: Optional[AgentConfig] = None
    summary: Optional[AgentConfig] = None
    llm_timeout_seconds: int = 0  # 0 = no limit (provider default)

    def resolve(self, agent_name: str) -> AgentConfig:
        """Return the effective AgentConfig for *agent_name*.

        Overrides are already merged with the default's values at parse time,
        so for the classic roles we simply return them. The temporal-KB roles
        (``facts``, ``summary``) fall back to ``ingest`` before ``default`` so
        a user who only points ``ingest`` at a stronger model gets that
        benefit for free.
        """
        override = getattr(self, agent_name, None)
        if override is None:
            if agent_name in ("facts", "summary") and self.ingest is not None:
                src = self.ingest
            else:
                src = self.default
            return AgentConfig(
                provider=src.provider, model=src.model, base_url=src.base_url
            )
        return AgentConfig(
            provider=override.provider,
            model=override.model,
            base_url=override.base_url,
        )


@dataclass
class CostConfig:
    soft_warn_usd: float = 0.50
    hard_gate_usd: float = 2.00
    auto_resolve_confidence_threshold: float = 0.85


@dataclass
class CacheConfig:
    version: str = "4"   # bump to invalidate all cached LLM responses


@dataclass
class IngestConfig:
    max_pages_per_ingest: int = 15
    chunk_size: int = 1500
    chunk_overlap: int = 150
    fetch_timeout_seconds: int = 30
    # Per-source token budget for fact extraction (0 = unbounded). A runaway
    # LLM response that exceeds this raises FactExtractBudgetExceeded inside
    # the kb pipeline; the page-tier ingest is unaffected.
    max_tokens_per_fact_extract: int = 0
    # Output budget for the page-tier decision step. It emits several full page
    # bodies in one JSON object, so the 4096 default truncates it (and on
    # thinking models the budget is shared with reasoning). Too small → empty
    # decisions → pages never get written.
    decision_max_tokens: int = 40000


@dataclass
class QueryConfig:
    gap_score_threshold: float = 2.0   # BM25 score below which gap is detected
    follow_links: bool = False         # read pages that retrieved pages [[link]] to
    max_linked_pages: int = 12         # cap on linked pages pulled into context
    page_char_budget: int = 1500       # chars of each page fed to the answering LLM
    link_hops: int = 1                 # how many [[link]] hops to follow
    # Source-layer retrieval: also search the verbatim fact-tier source text
    # (kb.db sources → parsed files), not just the consolidated wiki pages.
    # Pages give relationships/current-state; sources give dated verbatim detail
    # (figures, quotes) that consolidation drops. See
    # docs/plans/source-retrieval-query-design-v0.3.md.
    source_retrieval: bool = False     # blend verbatim source excerpts into context
    source_top_n: int = 6              # max source passages pulled in
    source_char_budget: int = 4000     # total chars of source excerpts fed to the LLM
    source_chunk_chars: int = 800      # target size of each source passage


@dataclass
class QueueConfig:
    max_parallel_ingest: int = 4
    max_retries: int = 3
    backoff_base_seconds: int = 5


@dataclass
class LogsConfig:
    level: str = "INFO"          # console log level: DEBUG | INFO | WARNING | ERROR
    max_file_mb: int = 5         # max size of synthadoc.log before rotation (MB)
    backup_count: int = 5        # number of rotated files to keep (total ≈ max_file_mb × backup_count)


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 7070
    reload: bool = False


@dataclass
class ScheduleJob:
    op: str
    cron: str


@dataclass
class ScheduleConfig:
    jobs: list[ScheduleJob] = field(default_factory=list)


@dataclass
class WebSearchConfig:
    provider: str = "tavily"
    max_results: int = 20


@dataclass
class WikiConfig:
    domain: str = "General"


@dataclass
class SearchConfig:
    vector: bool = False
    vector_top_candidates: int = 20


# ---------------------------------------------------------------------------
# Root config
# ---------------------------------------------------------------------------


@dataclass
class Config:
    agents: AgentsConfig
    cache: CacheConfig = field(default_factory=CacheConfig)
    cost: CostConfig = field(default_factory=CostConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    query: QueryConfig = field(default_factory=QueryConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)
    logs: LogsConfig = field(default_factory=LogsConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    web_search: WebSearchConfig = field(default_factory=WebSearchConfig)
    wiki: WikiConfig = field(default_factory=WikiConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    hooks: dict = field(default_factory=dict)
    wikis: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_agent(raw: dict) -> AgentConfig:
    return AgentConfig(
        provider=raw["provider"],
        model=raw.get("model", ""),
        base_url=raw.get("base_url", ""),
    )


def _validate_provider(agent: AgentConfig) -> None:
    if agent.provider not in KNOWN_PROVIDERS:
        raise ValueError(
            f"Unknown provider '{agent.provider}'. "
            f"Must be one of: {', '.join(sorted(KNOWN_PROVIDERS))}"
        )


def _build_default_agents_config() -> AgentsConfig:
    """Return a sentinel AgentsConfig with no real default (used as base before merging)."""
    # We use a placeholder that will be replaced during _merge if the user
    # supplies [agents] sections.  We cannot construct AgentsConfig without a
    # default AgentConfig so we use a special sentinel value.
    return None  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------


def _merge(base_raw: dict, override_raw: dict) -> dict:
    """Deep-merge *override_raw* on top of *base_raw* (override wins).

    For dict values, recurse.  For list values (e.g. schedule.jobs), the
    override completely replaces the base.
    """
    result = dict(base_raw)
    for key, val in override_raw.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _merge(result[key], val)
        else:
            result[key] = val
    return result


def _raw_to_config(raw: dict, source_has_agents: bool) -> Config:
    """Convert a merged raw TOML dict into a ``Config`` instance."""

    # --- agents ---
    a = raw.get("agents", {})

    if source_has_agents and "default" not in a:
        raise ValueError(
            "agents.default is required but missing from the configuration file."
        )

    if "default" not in a:
        # No agents section at all — we cannot build AgentsConfig.
        # Callers that supply a config file must have agents.default.
        # When no config file is given we should NOT raise; we'll use a
        # placeholder.  We handle this by returning a sentinel None and
        # the caller provides a bare Config with no agents.
        raise ValueError(
            "agents.default is required but missing from the configuration."
        )

    default_agent = _parse_agent(a["default"])
    _validate_provider(default_agent)

    agents = AgentsConfig(
        default=default_agent,
        llm_timeout_seconds=int(a.get("llm_timeout_seconds", 0)),
    )

    for name in ("ingest", "query", "lint", "skill"):
        if name in a:
            base_vals = {
                "provider": default_agent.provider,
                "model": default_agent.model,
                "base_url": default_agent.base_url,
            }
            base_vals.update(a[name])
            parsed = _parse_agent(base_vals)
            _validate_provider(parsed)
            setattr(agents, name, parsed)

    # Temporal-KB roles inherit from `ingest` first, then `default`, so a user
    # who only points `ingest` at a stronger model gets that benefit on
    # fact extraction and source summaries for free.
    _ingest_or_default = agents.ingest or default_agent
    for name in ("facts", "summary"):
        if name in a:
            base_vals = {
                "provider": _ingest_or_default.provider,
                "model": _ingest_or_default.model,
                "base_url": _ingest_or_default.base_url,
            }
            base_vals.update(a[name])
            parsed = _parse_agent(base_vals)
            _validate_provider(parsed)
            setattr(agents, name, parsed)

    # --- cost ---
    c = raw.get("cost", {})
    cost = CostConfig(
        soft_warn_usd=c.get("soft_warn_usd", 0.50),
        hard_gate_usd=c.get("hard_gate_usd", 2.00),
        auto_resolve_confidence_threshold=c.get("auto_resolve_confidence_threshold", 0.85),
    )

    # --- ingest ---
    ig = raw.get("ingest", {})
    ingest = IngestConfig(
        max_pages_per_ingest=ig.get("max_pages_per_ingest", 15),
        chunk_size=ig.get("chunk_size", 1500),
        chunk_overlap=ig.get("chunk_overlap", 150),
        fetch_timeout_seconds=ig.get("fetch_timeout_seconds", 30),
        max_tokens_per_fact_extract=int(ig.get("max_tokens_per_fact_extract", 0)),
        decision_max_tokens=int(ig.get("decision_max_tokens", 40000)),
    )

    # --- query ---
    q_section = raw.get("query", {})
    query = QueryConfig(
        gap_score_threshold=q_section.get("gap_score_threshold", 2.0),
        follow_links=q_section.get("follow_links", False),
        max_linked_pages=q_section.get("max_linked_pages", 12),
        page_char_budget=q_section.get("page_char_budget", 1500),
        link_hops=q_section.get("link_hops", 1),
        source_retrieval=q_section.get("source_retrieval", False),
        source_top_n=q_section.get("source_top_n", 6),
        source_char_budget=q_section.get("source_char_budget", 4000),
        source_chunk_chars=q_section.get("source_chunk_chars", 800),
    )

    # --- queue ---
    q = raw.get("queue", {})
    queue = QueueConfig(
        max_parallel_ingest=q.get("max_parallel_ingest", 4),
        max_retries=q.get("max_retries", 3),
        backoff_base_seconds=q.get("backoff_base_seconds", 5),
    )

    # --- logs ---
    lg = raw.get("logs", {})
    logs = LogsConfig(
        level=lg.get("level", "INFO"),
        max_file_mb=lg.get("max_file_mb", 5),
        backup_count=lg.get("backup_count", 5),
    )

    # --- server ---
    sv = raw.get("server", {})
    server = ServerConfig(
        host=sv.get("host", "127.0.0.1"),
        port=sv.get("port", 7070),
        reload=sv.get("reload", False),
    )

    # --- cache ---
    cv = raw.get("cache", {})
    cache = CacheConfig(version=str(cv.get("version", "4")))

    # --- schedule ---
    sched_raw = raw.get("schedule", {})
    jobs_raw = sched_raw.get("jobs", [])
    jobs = [ScheduleJob(op=j["op"], cron=j["cron"]) for j in jobs_raw]
    schedule = ScheduleConfig(jobs=jobs)

    # --- web_search ---
    ws = raw.get("web_search", {})
    web_search = WebSearchConfig(
        provider=ws.get("provider", "tavily"),
        max_results=ws.get("max_results", 20),
    )

    # --- hooks ---
    hooks = raw.get("hooks", {})

    # --- wiki ---
    wk = raw.get("wiki", {})
    wiki = WikiConfig(domain=wk.get("domain", "General"))

    # --- wikis ---
    wikis = raw.get("wikis", {})

    # --- search ---
    sr = raw.get("search", {})
    search = SearchConfig(
        vector=bool(sr.get("vector", False)),
        vector_top_candidates=int(sr.get("vector_top_candidates", 20)),
    )

    return Config(
        agents=agents,
        cache=cache,
        cost=cost,
        ingest=ingest,
        query=query,
        queue=queue,
        logs=logs,
        server=server,
        schedule=schedule,
        web_search=web_search,
        wiki=wiki,
        search=search,
        hooks=hooks,
        wikis=wikis,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(
    global_config: Optional[Path] = None,
    project_config: Optional[Path] = None,
) -> Config:
    """Load and merge configuration.

    Resolution order (later wins):
      1. Built-in defaults
      2. *global_config* file (if provided and exists)
      3. *project_config* file (if provided and exists)

    Parameters
    ----------
    global_config:
        Path to the global TOML config file.
    project_config:
        Path to the project-level TOML config file.

    Raises
    ------
    ValueError
        If a provided config file has an [agents] section without a *default*
        entry, or if any agent specifies an unknown provider.
    """
    raw: dict = {}

    # Track whether any loaded file actually contains an [agents] section,
    # so we know whether to enforce the agents.default requirement.
    any_file_loaded = False

    if global_config is not None and Path(global_config).exists():
        with open(global_config, "rb") as fh:
            global_raw = tomllib.load(fh)
        raw = _merge(raw, global_raw)
        any_file_loaded = True

    if project_config is not None and Path(project_config).exists():
        try:
            with open(project_config, "rb") as fh:
                project_raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            msg = str(exc)
            if "cannot overwrite" in msg.lower():
                raise ValueError(
                    f"[ERR-CFG-003] Duplicate key in {project_config}.\n"
                    f"Only one 'default' line may be active under [agents] at a time.\n"
                    f"Comment out all but one provider and restart the server.\n"
                    f"(TOML error: {exc})"
                ) from exc
            raise ValueError(f"[ERR-CFG-003] Invalid TOML in {project_config}: {exc}") from exc
        raw = _merge(raw, project_raw)
        any_file_loaded = True

    # If no config files were loaded, return a bare Config with defaults only
    # (no agents section required).
    if not any_file_loaded:
        return Config(
            agents=AgentsConfig(default=AgentConfig(provider="gemini", model="gemini-2.5-flash-lite")),
            web_search=WebSearchConfig(),
            search=SearchConfig(),
        )

    # A global_config was provided — it must define agents.default.
    if global_config is not None and Path(global_config).exists():
        a = raw.get("agents", {})
        if "default" not in a:
            raise ValueError(
                "agents.default is required but missing from the configuration."
            )

    # If agents section is absent (e.g. project_config with only [wikis]),
    # inject built-in defaults so the rest of the build succeeds.
    if "agents" not in raw or "default" not in raw.get("agents", {}):
        raw.setdefault("agents", {})["default"] = {
            "provider": "gemini",
            "model": "gemini-2.5-flash-lite",
        }

    return _raw_to_config(raw, source_has_agents=True)
