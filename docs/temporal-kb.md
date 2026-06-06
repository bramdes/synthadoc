# Temporal Knowledge Base

The temporal KB is an opt-in **fact tier** that runs alongside Synthadoc's
existing page-tier wiki. Pages stay where they are — under `wiki/`. The
fact tier lives under `kb/` and stores timestamped, source-backed
*facts*, *decisions*, *unknowns*, and rendered *entity* views on top of
them.

If you've read [`temporal_markdown_knowledge_base_spec.md`](../temporal_markdown_knowledge_base_spec.md)
and [`temporal_kb_implementation_plan.md`](../temporal_kb_implementation_plan.md),
this guide is the operator-facing layer over them.

---

## What it gives you

For each ingested source, Synthadoc now produces (in addition to the
page-tier writes it always did):

- **One source summary** — `kb/source_summaries/...` — bounded to that
  source's own content; no cross-source blending.
- **Zero-or-more atomic facts** — `kb/facts/<entity-type>/<slug>/<fact-type>/`
  — each tied to one entity, one fact type, one date, with a verbatim
  source quote.
- **Zero-or-more decisions** — `kb/decisions/` — explicit commitments
  the source records.
- **Zero-or-more unknowns** — `kb/unknowns/` — questions the source
  raises but does not resolve.
- **Rendered entity pages** — `kb/entities/<entity-type>/<slug>/index.md`
  — current-state view computed from the resolver. Re-renders on every
  pipeline run unless the entity is marked `reviewed`, in which case
  proposed changes accumulate in `kb/maintenance/review_queue.md`.
- **Rendered history pages** — `kb/entities/<...>/history.md` — full
  fact log per entity, chronological, with superseded markers.

Maintenance jobs (`synthadoc kb maintenance run`) produce reports under
`kb/maintenance/`:

| Report | What it surfaces |
|---|---|
| `conflicts.md` | Two active facts disagreeing on the same `(entity, fact_type, valid_at)` |
| `stale_pages.md` | Entity pages whose last render predates their newest fact |
| `orphan_facts.md` | Facts whose entity_id has no entity row |
| `facts_without_evidence.md` | Facts with empty quotes or missing source rows |
| `conclusions_without_facts.md` | Derived conclusions with no supporting facts |
| `duplicate_entities.md` | Suspect entity pairs (slug similarity heuristics) |
| `broken_links.md` | `[[wikilinks]]` pointing at non-existent files |
| `people_pages_flags.md` | People entity pages with spec §3.7 violations |
| `kb_health.md` | Aggregated snapshot of every metric vs. its threshold |
| `reports/<timestamp>.md` | Archived copy of `kb_health.md` per run |

---

## Opting in

The KB tier is **strictly opt-in**. A wiki without `synthadoc kb init`
behaves exactly like Synthadoc v0.2 — no `kb/` folder, no `kb.db`, no
parallel pipeline. You can adopt the KB tier on an existing wiki at any
time; existing pages and `audit.db` are untouched.

```bash
# In any wiki directory (or with -w <name>):
synthadoc kb init
```

This creates:

- `kb/` with the folder tree (sources, source_summaries, entities,
  facts, conclusions, decisions, unknowns, maintenance/reports).
- `.synthadoc/kb.db` — SQLite index over the markdown.
- `kb_config.yaml` — resolution rules (which fact types use
  latest-wins vs. append-only vs. requires-review).

It is idempotent — re-running on an initialised wiki only writes
missing pieces; your `kb_config.yaml` edits survive.

---

## Lifecycle

```
                                 ┌────────────────┐
   user → synthadoc ingest <src> │   IngestAgent  │
                                 │  (page tier)   │
                                 └────────┬───────┘
                                          │ on success
                                          ▼
                                ┌──────────────────┐
                                │ _enqueue_kb_pipe │  best-effort, non-fatal
                                │   (orchestrator) │
                                └────────┬─────────┘
                                          │
                              ┌───────────▼────────────┐
                              │   kb_pipeline job      │
                              ├────────────────────────┤
                              │ 1. import_source       │  → kb/sources/raw + kb.db row
                              │ 2. SourceSummaryAgent  │  → kb/source_summaries/...
                              │ 3. FactExtractAgent    │  → kb/facts/... + facts table
                              │ 4. DecisionExtractAgent│  → kb/decisions/... + decisions table
                              │ 5. UnknownExtractAgent │  → kb/unknowns/... + unknowns table
                              │ 6. resolve_db          │  → mark superseded facts
                              │ 7. EntityRenderAgent   │  → kb/entities/.../index.md
                              └────────────────────────┘
```

Failures inside any step are caught and surfaced on the job result —
the page-tier ingest never aborts because of a fact-tier hiccup. URL /
web-search ingests skip the fact tier entirely (no local file to
preserve immutably).

A second, periodic loop is the **maintenance pass**:

```
synthadoc kb maintenance run   # invoked manually or by cron
   ├── contradiction detection
   ├── stale-page detection
   ├── evidence gap checks
   ├── duplicate-entity heuristics
   ├── broken-link scan (against the links table)
   ├── people-page §3.7 safety scan
   ├── per-entity history.md re-render
   └── kb_health.md + reports/<TS>.md
```

Maintenance is read-mostly. It writes reports and history pages but
**never deletes facts** — supersession is the only state transition,
and it's a column update, not a row delete.

---

## CLI reference

All KB commands take the standard `-w / --wiki <name>` flag (or
default to the wiki set by `synthadoc use`).

```bash
# One-time setup
synthadoc kb init                            # scaffold kb/ + kb.db

# Importing sources manually (the ingest hook does this automatically)
synthadoc kb import-source path/to/file.md \
   --type meeting_transcript               # or document, email, deck, note
   --title "2026-05-22 Drop 1 Update"      # optional, defaults to filename
   --date 2026-05-22                       # optional, parsed from filename
   --authority informal                    # formal | informal | unknown

# Backfill from the existing audit.db (one-shot migration helper)
synthadoc kb backfill [--dry-run]

# Rebuild the links table from on-disk markdown (after manual edits)
synthadoc kb relink

# Run every maintenance job + emit kb_health.md
synthadoc kb maintenance run [--skip-histories]

# Triage the review queue (see "Reviewing the KB" below)
synthadoc kb review list                     # what's awaiting a decision
synthadoc kb review reject <fact-id>         # drop a wrong fact, re-render
synthadoc kb review accept-fact <fact-id>    # confirm a single fact
synthadoc kb review accept <entity-id>       # lock an entity's page as reviewed
synthadoc kb review reopen <entity-id>       # unlock it again
synthadoc kb review merge <dup-id> --into <keeper-id>   # merge duplicates
```

`--skip-histories` is a perf knob — `history.md` re-render is the
slowest step in maintenance, and for large wikis you may want to run
the deterministic checks more often than the history regeneration.

---

## Common workflows

### Adopting the KB tier on an existing wiki

```bash
synthadoc kb init -w my-wiki
synthadoc kb backfill -w my-wiki      # seed sources from audit history
# From here on, every `synthadoc ingest` auto-populates the fact tier
synthadoc ingest path/to/source.md -w my-wiki
synthadoc kb maintenance run -w my-wiki
```

### Reviewing what a source produced

```bash
synthadoc kb import-source ./meeting.md --type meeting_transcript
# Then read:
#   kb/source_summaries/meetings/<date>-<slug>.md  — what was said
#   kb/facts/projects/<...>/<...>.md               — what was extracted
#   kb/entities/projects/<slug>/index.md           — synthesised view
#   kb/entities/projects/<slug>/history.md         — full chronology
```

---

## Reviewing the KB

Everything the fact tier extracts starts as `review_status: unreviewed`.
Review is how a human triages it: drop wrong facts, merge duplicate
entities, and lock pages you've curated. The `synthadoc kb review`
command group does this **end to end** — it updates both `kb.db` and the
markdown frontmatter, then re-resolves and re-renders the affected
pages so the effect is immediate.

> **Why a command and not a text edit?** `kb.db` is the working store the
> renderer reads from; the markdown is the durable truth a rebuild
> restores from. Hand-editing a `review_status:` line in a `.md` file
> changes only one of the two and is silently overwritten on the next
> render. Always go through `kb review`, which keeps both in sync.

### See what's pending

```bash
synthadoc kb review list
```

Prints, in one place:

- **Unreviewed counts** — facts / decisions / entities still untriaged.
- **Conflicts** — each with the participating fact ids, ready to paste
  into `kb review reject`.
- **Duplicate candidates** — each pair, ready for `kb review merge`.
- **Queued proposals** — if any reviewed page has pending changes in
  `kb/maintenance/review_queue.md`.

It also refreshes `conflicts.md` and `duplicate_entities.md` as a side
effect. The full reports stay under `kb/maintenance/`.

### Resolve a contradiction

`conflicts.md` lists two-or-more active facts disagreeing at the same
`(entity, fact_type, valid_at)`. Look at each fact's `source_quote`,
decide which is wrong, and reject it by id:

```bash
synthadoc kb review reject fact.project.document-intelligence.project.milestone.2026-05-22-2
```

This marks the fact `rejected` in `kb.db` **and** in its markdown
frontmatter, then re-resolves and re-renders the entity page. A rejected
fact is excluded from **every** resolution strategy — including
`append_only` and `requires_review` — so the value disappears from the
entity page and the conflict clears. The fact stays on disk as evidence
(it still appears on `history.md`); it just no longer counts.

To simply confirm a fact is correct (without changing the page), use
`synthadoc kb review accept-fact <fact-id>`.

### Lock a reviewed entity page

By default the render agent overwrites `index.md` on every pipeline run.
Once a page is correct, lock it:

```bash
synthadoc kb review accept entity.project.document-intelligence
```

This renders the current page, then sets
`current_state_review_status: reviewed` (DB + frontmatter) and stamps
`last_reviewed`. From now on, any new contradicting fact is appended to
`kb/maintenance/review_queue.md` as a *proposed update* instead of
silently overwriting your page. To resume auto-rendering:

```bash
synthadoc kb review reopen entity.project.document-intelligence
```

### Merge duplicate entities

`duplicate_entities.md` flags suspicious pairs by slug similarity (noise
tokens, substrings, Levenshtein ≤ 2). It never auto-merges. Pick the
keeper and merge the duplicate into it:

```bash
synthadoc kb review merge entity.project.doc-intel-enterprise-roadmap \
   --into entity.project.doc-intel
```

This re-points the duplicate's facts, decisions, and unknowns to the
keeper, marks the duplicate `merged` (with `merged_into` set), re-renders
the keeper page so the moved facts show up, and rewrites the duplicate's
page as a tombstone pointing at the keeper. **It also records the merge in
`kb_aliases.yaml`** (see below) so it survives a re-import.

### Curating entities durably: aliases & sub-areas

Two version-controllable files at the wiki root (siblings of
`kb_config.yaml`, **not** under `.synthadoc/`, so they commit with the wiki
and survive a clean re-import) let you teach the system about your entities
once and have it apply on every import:

**`kb_aliases.yaml` — "these names are the SAME entity."** Applied by the
`EntityLinker` *during import*, so declared variants never fragment in the
first place. The merge above writes here automatically; you can also
pre-seed before an import (no existing entity required):

```bash
synthadoc kb review alias "AURA Program" --into "AURA" --type project
synthadoc kb review alias "Cloud Code"  --into "Claude Code" --type project
```
```yaml
# kb_aliases.yaml
aliases:
  project:
    AURA:
      - AURA Program
      - AURA EGP Program
```

**`kb_relations.yaml` — "this entity is a SUB-AREA of that one."** Keeps both
entities but records a parent/child link. The entity renderer shows a
`## Sub-areas` list on the parent and a `_Part of …_` line on each child
(path-style wikilinks, since entity pages are all `index.md`):

```bash
synthadoc kb review relate "Doc Intel intent extraction" --parent "Doc Intel" --type project
```
```yaml
# kb_relations.yaml
relations:
  project:
    Doc Intel:
      - Doc Intel intent extraction
      - Doc Intel ITC / Production Deployment
```

Both files are also created (empty, documented) by `synthadoc kb init`, and
are hand-editable. The linker reads them on each pipeline run, so edits take
effect on the **next import** — restart `synthadoc serve` once after
upgrading so it loads the current code, then re-imports pick them up
automatically. (Aliases apply at link time; relationships surface the next
time the affected entity pages are rendered.)

### Suggested cadence

```bash
synthadoc kb maintenance run      # recompute health + reports
synthadoc kb review list          # see conflicts / duplicates / counts
synthadoc kb review reject …      # resolve each conflict
synthadoc kb review merge …       # collapse each real duplicate
synthadoc kb review accept …      # lock pages you've curated
```

---

## Frontmatter reference

Every fact-tier file carries a discriminated `type` field. The full
schema for each type lives in `synthadoc/kb/frontmatter.py`; the user-
visible fields you'll most often see:

```yaml
# Source (one per imported file)
id: source.meeting.2026-05-22.drop1-update
type: source
source_type: meeting_transcript
title: "2026-05-22 Drop 1 Update"
authority: informal
raw_path: kb/sources/raw/meetings/2026-05-22-drop1-update.md
parsed_path: kb/sources/parsed/meetings/2026-05-22-drop1-update.md
created_at: 2026-05-22T10:00:00+00:00
ingested_at: 2026-05-22T09:30:00+00:00
sha256: <hex>

# Fact (atomic, timestamped, source-backed)
id: fact.project.doc-intel.project.status.2026-05-22
type: fact
entity_id: entity.project.doc-intel
fact_type: project.status
value: completed
valid_at: 2026-05-22                     # when the fact is true
observed_at: 2026-05-22T09:30:00+00:00   # when we extracted it
source_id: source.meeting.2026-05-22.drop1-update
source_quote: "Drop 1 is now complete."  # VERBATIM substring of parsed source
confidence: high                         # high | medium | low
review_status: unreviewed                # unreviewed | reviewed | rejected | needs_clarification
supersedes:                              # optional
  - fact.project.doc-intel.project.status.2026-05-01

# Entity (rendered current-state view)
id: entity.project.doc-intel
type: entity
entity_type: project
status: active
current_state_review_status: unreviewed  # set to 'reviewed' to pin
last_rebuilt: 2026-05-22T09:30:10+00:00
```

---

## Configuring resolution rules

`kb_config.yaml` controls how the resolver collapses a fact stream
into current state per `fact_type`.

```yaml
resolution_rules:
  project.status:
    strategy: latest_valid_at_wins
    minimum_confidence: medium             # 'low' allows everything
    exclude_review_status: [rejected]

  project.scope:
    strategy: requires_review              # never auto-resolve

  decision.made:
    strategy: append_only
    reversal_required: true                # marker only; not yet enforced
```

Strategies (v0.3):

- **`latest_valid_at_wins`** — pick the newest by `(valid_at, observed_at)`;
  mark older facts of the same type superseded.
- **`append_only`** — every fact kept as evidence; no winner picked.
- **`requires_review`** — never auto-resolve; pending facts surface in
  the entity page's "Pending Review" section.
- **`open_until_closure_fact`** / **`open_until_resolved`** — placeholders
  for issue / assumption lifecycles; treated as `append_only` in v0.3,
  with proper pairing logic deferred to Layer 4.

---

## Per-role provider configuration

The fact tier honours two new role overrides under `[agents]`:

```toml
# .synthadoc/config.toml
[agents]
default  = { provider = "gemini", model = "gemini-2.5-flash-lite" }
ingest   = { provider = "anthropic", model = "claude-sonnet-4-6" }

# Optional: send summaries / fact extraction to a different model than ingest.
# If absent, both inherit from `ingest` (then `default`).
summary  = { provider = "gemini", model = "gemini-2.5-flash" }
facts    = { provider = "anthropic", model = "claude-opus-4-7" }
```

The fallback chain for `facts` and `summary` is `→ ingest → default`,
so a user who only points `ingest` at a stronger model gets that
benefit for free.

Recommended in practice: cheap-and-fast for `summary`, accurate for
`facts`. Fact extraction is where the substring-quote guard and the
closed-vocab validators do the most rejection work; a stronger model
there reduces rework.

---

## Hooking into automation

The KB pipeline runs inside Synthadoc's existing job queue, so the
same `synthadoc.log` JSON lines, `audit.db` rows, and OpenTelemetry
trace spans you already monitor cover the new work. Look for:

- Job operation `kb_pipeline` — one per ingested file.
- OTel span `kb.maintenance.<name>` — one per maintenance job in a run.
- Audit events under the existing `audit_events` table.

To schedule recurring maintenance:

```bash
synthadoc schedule add --op "kb maintenance run" --cron "0 3 * * *" -w my-wiki
```

(Same scheduler that handles `ingest` and `lint` schedules; no fact-tier
specific knobs.)

### Recommended cadences

A workable cron template for a wiki that ingests on weekday business
hours:

```bash
# Every night at 03:00 — full maintenance pass (covers conflicts, stale,
# evidence gaps, duplicates, broken links, people safety, history re-render).
synthadoc schedule add --op "kb maintenance run" --cron "0 3 * * *" -w my-wiki

# Every hour during business hours — relink only, so manual edits don't leave
# the links table stale. Cheap; finishes in milliseconds on most wikis.
synthadoc schedule add --op "kb relink" --cron "0 9-18 * * 1-5" -w my-wiki

# Weekly on Sundays at 04:00 — a maintenance run WITH --skip-histories is the
# usual nightly; the weekly one re-renders every history.md for any pages that
# changed without their entity being touched (rare but possible).
synthadoc schedule add --op "kb maintenance run" --cron "0 4 * * 0" -w my-wiki
```

If your wiki ingests at a steady drip rather than in bursts, run the
full maintenance every six hours instead of nightly — the cost is one
SQL pass + N history renders, and you get conflict + broken-link
detection inside the same business day.

### Controlling fact-extraction cost

A pathological source (a 40 KB transcript with a chatty model) can burn
~30 000 tokens on the fact-extract call alone. Cap it per source:

```toml
# .synthadoc/config.toml
[ingest]
max_tokens_per_fact_extract = 20000    # 0 = unbounded (default)
```

When the cap trips, the agent raises ``FactExtractBudgetExceeded``;
the pipeline catches it into ``result.errors`` and continues — your
page-tier ingest is unaffected, and the source's facts can be
re-extracted later with a higher budget or a different model.

---

## Troubleshooting

### "kb.db not found"

The KB tier hasn't been initialised for this wiki. Run
`synthadoc kb init -w <name>`. Existing wikis are unaffected by the
tier's existence; nothing automatically opts you in.

### Facts I expect aren't extracted

Check `kb_health.md` first. Then inspect the source's row in the
`facts` table:

```bash
sqlite3 .synthadoc/kb.db \
   "SELECT id, fact_type, value, valid_at, confidence FROM facts WHERE source_id = '<id>'"
```

If the LLM emitted a candidate that got rejected, it appears in the
ingest job's log (filtered by `job_id`). Most common rejection causes:

- **Source quote not verbatim** — the LLM paraphrased. Re-extract with
  a stronger model (`[agents] facts = { … }`) or amend the prompt.
- **Wrong `fact_type`** — closed vocab. Either the fact really doesn't
  fit (legitimately rejected) or a new fact_type needs adding to
  `synthadoc/kb/ids.py:FACT_TYPES` + a resolution rule.
- **Bad date format** — non-ISO dates are rejected. Usually the LLM
  needs the source's own date stamped clearly in the body.

### `consolidate` says "refusing to consolidate fact-tier page"

By design — `consolidate` is for free-form wiki pages. Entity pages
are deterministically rendered; rewriting them would corrupt the
source-of-truth in `kb.db`. To refresh an entity page, run the kb
pipeline again or `synthadoc kb maintenance run`.

### Broken-link reports a slug I know exists

The `links` table can drift after manual edits or `git checkout`.
Rebuild it:

```bash
synthadoc kb relink -w my-wiki
```

### A people page got flagged for content I think is fine

The detector is regex-based and deliberately err-on-flagging. False
positives are expected for a small lexicon. The flags are *advisory*
— review the line and either rephrase or accept the flag and ignore
that entry in the next report. The pattern lexicon lives in
`synthadoc/kb/maintenance/people_safety.py` if you want to tune it.

### `kb maintenance run` is slow

Profile via the OTel traces (`.synthadoc/logs/traces.jsonl`). The
common offender is `history.md` re-render across many entities — pass
`--skip-histories` if you don't need the chronology this run.

---

## Architecture pointers

| Concern | Module |
|---|---|
| Stable ID generation + validation | `synthadoc/kb/ids.py` |
| Discriminated frontmatter parse/build | `synthadoc/kb/frontmatter.py` |
| SQLite schema + helpers | `synthadoc/kb/db.py` |
| Folder layout + immutability guard | `synthadoc/kb/layout.py` |
| Resolution rule loader | `synthadoc/kb/rules.py` |
| Current-state resolver (pure) | `synthadoc/kb/resolver.py` |
| Entity linker (deterministic) | `synthadoc/kb/entity_linker.py` |
| Wikilink extraction + emission | `synthadoc/kb/links.py` |
| End-to-end pipeline (one source) | `synthadoc/kb/pipeline.py` |
| Source import helper | `synthadoc/kb/import_source.py` |
| Maintenance jobs | `synthadoc/kb/maintenance/*.py` |
| Maintenance aggregator | `synthadoc/kb/maintenance/report.py` |
| Summary / fact / decision / unknown agents | `synthadoc/agents/*.py` |
| Entity / history render agents | `synthadoc/agents/*_render_agent.py` |
| CLI | `synthadoc/cli/kb.py` |
| Orchestrator wire-up | `synthadoc/core/orchestrator.py:_enqueue_kb_pipeline / _run_kb_pipeline` |

---

## What's *not* yet in the temporal KB

The plan calls these out explicitly so future work doesn't surprise you:

- **Vector / semantic search over facts** — BM25 + exact normalisation
  is enough for v0.3. Adding fact-level vector search is a Layer 4
  candidate.
- **`entities.merged_into` following** — the column exists and the
  duplicate detector flags candidates, but the render and resolver
  don't yet chase the chain. Manual merges work; auto-following is
  future work.
- **LLM-driven escalator on top of regex-based people safety** —
  today's pass catches common patterns; a paraphrase-aware classifier
  is on the followup list.
- **Real-time UI for the review queue** — review = edit
  `review_status` in frontmatter. The queue is a markdown page; that's
  the v0.3 UI.
- **BRD / FSD traceability matrix, ReqIF export** — explicitly deferred
  per spec §11.

When you hit a hard edge, file a follow-up in
[`temporal_kb_implementation_plan.md`](../temporal_kb_implementation_plan.md)
under §14 (Open questions) so it's visible to the next session.
