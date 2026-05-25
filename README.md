# Synthadoc

```
      .-+###############+-.
    .##                   ##.
   ##    .----.   .----.    ##
  ##    /######\ /######\    ##
  ##    |######| |######|    ##
  ##    | [SD] | | wiki |    ##
  ##    |######| |######|    ##
  ##    \######/ \######/    ##
   ##    '----'   '----'    ##
    '##                   ##'
      '-+###############+-'

       S Y N T H A D O C
  ────────────────────────────────
  Domain-agnostic LLM wiki engine
   with a temporal fact tier on top
```

An LLM-driven engine that turns raw documents and meeting transcripts
into a living, cross-referenced knowledge base. Local-first. Plain
Markdown on disk. Two cooperating tiers:

- **Page tier** (`wiki/`) — the user-facing wiki. Every ingest pass
  decomposes a source into per-subject actions (create / update / flag),
  with per-section provenance footers and contradiction surfacing. This
  is the original Synthadoc and the artifact most users browse in
  Obsidian.
- **Temporal KB tier** (`kb/`, opt-in) — a parallel store of
  timestamped, source-backed *facts*, *decisions*, and *unknowns*.
  Pages here are **synthesized views** rendered from those facts, not
  hand-edited. Asks like "what is the current status?" or "what
  changed since 2026-05-01?" are answerable structurally, not via
  context-window guessing.

Stop the server and the folder is still a readable, editable knowledge
base in any Markdown tool.

---

## What's in this fork

Downstream of [paulmchen/synthadoc](https://github.com/paulmchen/synthadoc).
The original engine — multi-action ingest with source decomposition,
provenance footers, subfolder layout, `consolidate`, `scaffold`,
contradiction detection — is intact. The fork adds the **Temporal KB
tier** described in
[`temporal_markdown_knowledge_base_spec.md`](temporal_markdown_knowledge_base_spec.md)
and built per
[`temporal_kb_implementation_plan.md`](temporal_kb_implementation_plan.md).

| Area | What's new |
|---|---|
| Storage | `kb/sources`, `kb/source_summaries`, `kb/entities`, `kb/facts`, `kb/decisions`, `kb/unknowns`, `kb/maintenance`. SQLite index at `.synthadoc/kb.db`. |
| IDs | Stable IDs like `source.meeting.2026-05-22.<slug>`, `fact.project.<slug>.project.status.2026-05-22`. Generators + validators in `synthadoc/kb/ids.py`. |
| Pipeline | After every successful `synthadoc ingest`, a `kb_pipeline` job runs: SourceSummary → FactExtract → DecisionExtract → UnknownExtract → resolve → EntityRender. Best-effort, non-fatal — page-tier ingest is never blocked. |
| Maintenance | `synthadoc kb maintenance run` → conflicts, stale pages, orphan facts, facts-without-evidence, conclusions-without-basis, duplicate entities, broken links, people-page §3.7 safety. Reports under `kb/maintenance/`. |
| Determinism | Substring-quote guard on every extracted fact (paraphrased quotes are rejected without retry). Closed vocabularies for `entity_type` / `fact_type` / `authority` / `confidence` / `review_status`. Pure resolver with `latest_valid_at_wins` / `append_only` / `requires_review` strategies. |
| Cost guard | `[ingest] max_tokens_per_fact_extract = N` caps runaway extraction per source. |
| Telemetry | Per-maintenance-job OTel spans (`kb.maintenance.<name>`) land in `.synthadoc/logs/traces.jsonl`. |
| CLI | `synthadoc kb {init, import-source, backfill, relink, maintenance run}`. |

A previous README is preserved at [`README.old.md`](README.old.md) for
historical context.

---

## Mental model

```
                       ┌──────────────────────┐
                       │   synthadoc ingest   │
                       │       <source>       │
                       └──────────┬───────────┘
                                  │
                ┌─────────────────┼─────────────────┐
                ▼                                   ▼
   ┌──────────────────────┐         ┌──────────────────────────┐
   │   PAGE TIER (always) │         │  KB TIER (if initialised)│
   ├──────────────────────┤         ├──────────────────────────┤
   │ IngestAgent          │         │ kb_pipeline job          │
   │  fan-out by subject  │         │  ├── SourceSummaryAgent  │
   │  → wiki/<slug>.md    │         │  ├── FactExtractAgent    │
   │  + provenance footer │         │  ├── DecisionExtractAgent│
   │  + contradictions    │         │  ├── UnknownExtractAgent │
   │                      │         │  ├── resolve_db          │
   └──────────────────────┘         │  └── EntityRenderAgent   │
                                    │     → kb/entities/…/.md  │
                                    └──────────────────────────┘

           Periodic: `synthadoc kb maintenance run`
           ↳ conflict / stale / evidence / dup / link / safety reports
           ↳ history.md re-render per entity
           ↳ kb_health.md snapshot + archived copy
```

The fact tier is **strictly opt-in.** A wiki without
`synthadoc kb init` behaves exactly like the v0.2 page-tier engine —
no `kb/` folder, no `kb.db`, no parallel pipeline.

---

## Quick start (page tier only)

```bash
# Install
git clone https://github.com/axoviq-ai/synthadoc.git
cd synthadoc
pip3 install -e ".[dev]"

# Set at least one LLM API key (Gemini Flash is free-tier and the default)
export GEMINI_API_KEY=AIza…      # macOS/Linux
$env:GEMINI_API_KEY = "AIza…"    # PowerShell

# Install a demo wiki
synthadoc install history-of-computing --target ~/wikis --demo
synthadoc serve -w history-of-computing
# → http://127.0.0.1:7070
```

The demo ships with pre-built pages and sample sources. Walk through
[`docs/user-quick-start-guide.md`](docs/user-quick-start-guide.md)
for a full tour — installing the Obsidian plugin, running queries,
batch ingesting, resolving contradictions, fixing orphans.

To start a wiki from scratch:

```bash
synthadoc install my-research --target ~/wikis \
   --domain "Market conditions and trends in Canada"
synthadoc use my-research
synthadoc serve
```

---

## Adopting the Temporal KB tier

On any existing wiki:

```bash
synthadoc kb init -w my-wiki              # creates kb/ + kb.db + kb_config.yaml
synthadoc kb backfill -w my-wiki          # seeds sources from existing audit history

# From here on every `synthadoc ingest` also runs the kb pipeline.
synthadoc ingest path/to/source.md -w my-wiki

# Periodic maintenance
synthadoc kb maintenance run -w my-wiki
```

That's it. The kb pipeline runs as a queue job on the existing
worker, retries on transient failure via the same machinery as
ingest/lint, and surfaces telemetry in the same traces.jsonl.

For a step-by-step soak-test playbook on a real wiki (with
inspection checklists, failure-mode triage, rollback) see
[`docs/temporal-kb-soak-test.md`](docs/temporal-kb-soak-test.md).

For the full user guide — workflows, frontmatter reference, resolution
rules, per-role provider config, troubleshooting, architecture pointers
— see [`docs/temporal-kb.md`](docs/temporal-kb.md).

---

## Command reference

### Page tier (unchanged from upstream)

```bash
synthadoc install <name> --target <dir> [--demo | --domain "..."]
synthadoc use [<name> | --clear]
synthadoc serve -w <name> [--background] [--port N]
synthadoc ingest <file-or-url> -w <name> [--force] [--batch <dir>] [--file <manifest>]
synthadoc query "..." -w <name> [--save]
synthadoc lint run [--scope contradictions] [--auto-resolve] -w <name>
synthadoc lint report -w <name>
synthadoc consolidate <slug> [--dry-run] [--force] -w <name>
synthadoc scaffold -w <name>                  # re-generates index / AGENTS.md / purpose.md
synthadoc jobs {list, status, retry, cancel, purge} -w <name>
synthadoc audit {history, cost, events} -w <name>
synthadoc schedule {add, list, remove, apply} -w <name>
synthadoc cache clear -w <name>
synthadoc status -w <name>
```

### Temporal KB tier (this fork)

```bash
synthadoc kb init -w <name>
synthadoc kb import-source <file> --type meeting_transcript|document|email|deck|note -w <name>
synthadoc kb backfill [--dry-run] -w <name>
synthadoc kb relink -w <name>                 # rebuild links table after manual edits
synthadoc kb maintenance run [--skip-histories] -w <name>
```

---

## What the temporal tier gives you

Concretely, for each ingested source you get:

- **One source summary**, bounded to that source's own content.
- **Zero-or-more atomic facts** — each tied to one entity, one
  `fact_type`, one `valid_at` date, with a verbatim source quote.
  Closed vocab for `fact_type` (e.g. `project.status`, `project.owner`,
  `decision.made`); paraphrased quotes are rejected automatically.
- **Zero-or-more decisions** — explicit commitments only, with date /
  authority / source-quote.
- **Zero-or-more unknowns** — explicit gaps the source raises but
  doesn't resolve.
- **Rendered entity index pages** — current-state view computed from
  the resolver. Re-renders on every pipeline run unless the entity is
  marked `current_state_review_status: reviewed`, in which case
  proposed changes accumulate in `kb/maintenance/review_queue.md`.
- **Rendered history pages** — chronological fact log per entity,
  including superseded facts as evidence of historical state.

Plus the maintenance reports under `kb/maintenance/` (conflicts,
stale pages, orphan facts, facts without evidence, conclusions without
basis, duplicate entity candidates, broken links, people-page safety
flags, `kb_health.md` snapshot, timestamped archives).

---

## Configuration

Defaults are sensible enough that the demo works out of the box. Real
deployments tune `<wiki-root>/.synthadoc/config.toml`:

```toml
[wiki]
domain = "My Domain"

[server]
port = 7070

[agents]
default = { provider = "gemini",    model = "gemini-2.5-flash-lite" }
ingest  = { provider = "anthropic", model = "claude-sonnet-4-6" }

# Optional KB roles — both fall back to `ingest` then `default` if absent.
# Point `facts` at a stronger model if extraction yield is low.
summary = { provider = "gemini",    model = "gemini-2.5-flash" }
facts   = { provider = "anthropic", model = "claude-opus-4-7" }

# 0 = unbounded. Cap to abort runaway extraction per source.
[ingest]
max_pages_per_ingest         = 15
max_tokens_per_fact_extract  = 30000

[cost]
soft_warn_usd = 0.50
hard_gate_usd = 2.00

[search]
vector              = false   # set true to opt in to BAAI/bge-small-en-v1.5 re-ranking
vector_top_candidates = 20

[logs]
level        = "INFO"
max_file_mb  = 5
backup_count = 5
```

Full configuration reference (layer precedence, all keys, defaults):
see [`docs/user-quick-start-guide.md`](docs/user-quick-start-guide.md)
Appendix E, plus [`docs/temporal-kb.md`](docs/temporal-kb.md) for the
KB-specific knobs.

---

## Architecture

```
synthadoc/
├── agents/                 # ingest / query / lint / consolidate / scaffold (page tier)
│                          # source_summary / fact_extract / decision_extract / unknown_extract
│                          # entity_render / history_render (KB tier)
├── kb/                     # KB-tier library (no Synthadoc-agent dependencies)
│   ├── ids.py             # stable ID generators + validators (closed vocab)
│   ├── frontmatter.py     # discriminated YAML read/write
│   ├── db.py              # KBDB — sources, entities, facts, decisions, unknowns, links
│   ├── layout.py          # folder constants + raw-source immutability guard
│   ├── rules.py           # kb_config.yaml resolution rule loader
│   ├── resolver.py        # pure facts → current-state per entity
│   ├── entity_linker.py   # deterministic name → entity_id
│   ├── links.py           # wikilink extractor + per-file emission + relink_all
│   ├── import_source.py   # idempotent (SHA) source → kb/sources/ + DB row
│   ├── pipeline.py        # end-to-end orchestrator (one source)
│   └── maintenance/       # contradictions, stale, evidence, duplicates,
│                          # broken_links, people_safety, report aggregator
├── core/                   # orchestrator, queue, cache, scheduler, cost_guard, hooks
├── cli/                    # typer sub-apps (install, use, serve, ingest, query,
│                          # lint, jobs, audit, schedule, scaffold, consolidate,
│                          # cache, status, kb)
├── providers/              # anthropic / openai / gemini / groq / minimax /
│                          # deepseek / ollama / coding-tool (claude-code, opencode)
├── skills/                 # ingest skills: md, txt, pdf, docx, pptx, xlsx, url,
│                          # image, youtube, web_search
├── storage/                # WikiStorage (page tier), HybridSearch (BM25 + optional vector)
├── observability/          # OTel setup; idempotent for tests
└── integration/            # FastAPI HTTP server + MCP server + worker loop
```

The page-tier and KB-tier layers are decoupled — `kb/` has no
dependencies on `agents/` or `storage/`. You can drive the KB
programmatically from a Python script without touching the page-tier
storage at all (useful for evaluation harnesses).

Full design + plugin development guide: [`docs/design.md`](docs/design.md).

---

## Testing

The fork ships **1,072 passing tests** (one pre-existing path-traversal
test fails on `main` too — tracked separately). Two coverage tiers:

| Suite | What it proves |
|---|---|
| `tests/{cli,core,storage,...}` | Page tier — IngestAgent, orchestrator, queue, search, providers (804 tests upstream + smoothed) |
| `tests/kb/` | Temporal tier — IDs, frontmatter, DB, layout, resolver, entity linker, every agent, pipeline, maintenance, end-to-end corpora, spec §9 acceptance threshold runner (~330 tests) |

Run the full suite:

```bash
pytest --ignore=tests/performance -q
```

Run only the KB tier:

```bash
pytest tests/kb -q
```

The spec §9 acceptance-threshold runner (`tests/kb/test_spec9_thresholds.py`)
is a single comprehensive test against a golden corpus that asserts
every numeric threshold the spec sets for prototype acceptance (≥90%
fact extraction, ≥95% link validity, 100% raw-source immutability, 0
deletion of superseded facts, etc.).

---

## Links

- **Temporal KB**
  - User guide: [`docs/temporal-kb.md`](docs/temporal-kb.md)
  - Soak-test playbook: [`docs/temporal-kb-soak-test.md`](docs/temporal-kb-soak-test.md)
  - Spec: [`temporal_markdown_knowledge_base_spec.md`](temporal_markdown_knowledge_base_spec.md)
  - Implementation plan + session journal: [`temporal_kb_implementation_plan.md`](temporal_kb_implementation_plan.md)
- **Page tier**
  - Design: [`docs/design.md`](docs/design.md)
  - Quick-start guide: [`docs/user-quick-start-guide.md`](docs/user-quick-start-guide.md)
  - Pre-fork README: [`README.old.md`](README.old.md)
- **Project**
  - Contributing: [`CONTRIBUTING.md`](CONTRIBUTING.md)
  - License: AGPL-3.0
