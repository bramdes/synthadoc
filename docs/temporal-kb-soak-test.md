# Temporal KB Soak Test — `syntha_meetings_v2`

A manual playbook for soak-testing the temporal KB tier on a **fresh,
side-by-side wiki**. Designed to be safe (your existing wikis are
never touched) and inspectable (stop at any checkpoint and look at
what landed).

**Target wiki:** `syntha_meetings_v2` at `C:\Proj\syntha_meetings_v2` (new)
**Existing wiki (untouched):** `syntha_meetings` at `C:\Proj\syntha_meetings`
**Engine:** `C:\Proj\synthadoc` (running on Gemini 3.1 Pro Preview per your `[agents]` config)

> **Why a new wiki?** The temporal KB tier auto-runs on every
> `synthadoc ingest`. Adopting it on the existing `syntha_meetings`
> wiki would mean every future meeting also gets a kb-pipeline pass —
> useful eventually, but you want to feel out prompt quality first.
> A second wiki lets you ingest the same source files into both and
> compare side-by-side without committing.

> **What this is going to spend.** ~5-50K Gemini tokens per ingested
> meeting (summary + fact extract + decision extract + unknown extract).
> Cap it with `[ingest] max_tokens_per_fact_extract` in the v2 wiki's
> config if you want a hard ceiling per source.

---

## 0. Install the new wiki

Both terminals start from `C:\Proj\synthadoc`. Activate the venv first:

```powershell
cd C:\Proj\synthadoc
.\.venv\Scripts\Activate.ps1
```

Install the wiki (parent directory `C:\Proj\`, name `syntha_meetings_v2`,
matching the same domain wording you used on the original):

```powershell
synthadoc install syntha_meetings_v2 --target C:\Proj --domain "Meetings, projects, decisions, people, and open issues"
```

This creates:

- `C:\Proj\syntha_meetings_v2\wiki\` — empty wiki + scaffolded `index.md`,
  `purpose.md`, `dashboard.md`.
- `C:\Proj\syntha_meetings_v2\AGENTS.md` — domain-tailored guidelines.
- `C:\Proj\syntha_meetings_v2\.synthadoc\config.toml` — auto-allocates
  a free port (probably 7071 since 7070 is taken by the existing wiki).
- `C:\Proj\syntha_meetings_v2\raw_sources\` — empty.

Mirror the LLM config from the original wiki so the comparison is fair:

```powershell
# Open the new config
notepad C:\Proj\syntha_meetings_v2\.synthadoc\config.toml
```

Edit the `[agents]` block to match your existing setup, e.g.:

```toml
[agents]
default = { provider = "gemini", model = "gemini-3.1-pro-preview" }

# Optional — cap runaway fact-extraction cost per source.
# Leave commented out to start; uncomment once you see real numbers.
# [ingest]
# max_tokens_per_fact_extract = 30000
```

Save & close.

---

## 1. Initialise the kb tier

```powershell
synthadoc kb init -w syntha_meetings_v2
```

You should see:

```
KB tier initialised.
  kb/          C:\Proj\syntha_meetings_v2\kb
  kb.db        C:\Proj\syntha_meetings_v2\.synthadoc\kb.db
  config       C:\Proj\syntha_meetings_v2\kb_config.yaml
```

**Skim the resolution rules** — they're sensible defaults for a
meetings-heavy wiki, but worth a glance:

```powershell
notepad C:\Proj\syntha_meetings_v2\kb_config.yaml
```

The defaults relevant for meetings:
- `project.status` / `project.owner` → `latest_valid_at_wins`
- `project.scope` → `requires_review` (won't auto-overwrite)
- `decision.made` → `append_only`

Save & close.

---

## 2. Start the v2 server

In a fresh terminal:

```powershell
cd C:\Proj\synthadoc
.\.venv\Scripts\Activate.ps1
synthadoc serve -w syntha_meetings_v2
```

Banner should show port `7071` (or whatever was auto-allocated). The
original `syntha_meetings` server (port 7070) can keep running in its
own terminal — they don't interfere.

Leave this terminal open.

---

## 3. Test on ONE source first

Pick a recent meeting transcript to start with — *one* file, not the
whole month. The goal is to read every produced artifact by hand
before scaling up.

In your first terminal (where you ran `kb init`):

```powershell
synthadoc ingest `
   "C:\Proj\obsidian_notes_2026\Meetings\2026\05\<file>.md" `
   -w syntha_meetings_v2
```

You'll get a job id. Two jobs will land in the queue:

- **`ingest`** — runs the page-tier work (writes `wiki/<slug>.md` etc.).
- **`kb_pipeline`** — runs the fact-tier work (summary + facts +
  decisions + unknowns + entity render).

Watch them complete:

```powershell
synthadoc jobs list -w syntha_meetings_v2
# Re-run until both show "completed". The kb_pipeline job typically
# takes 10-60 seconds depending on transcript length.
```

If either fails, drill in:

```powershell
synthadoc jobs status <job-id> -w syntha_meetings_v2
```

---

## 4. Inspect the produced markdown by hand

After both jobs complete, four families of files should exist for the
test source. Open each one in your editor.

### Where to look

```powershell
# Source summary
ls C:\Proj\syntha_meetings_v2\kb\source_summaries\

# Facts emitted from this source (grouped by entity)
ls C:\Proj\syntha_meetings_v2\kb\facts\projects\
ls C:\Proj\syntha_meetings_v2\kb\facts\people\

# Decisions (if any explicit ones were in the source)
ls C:\Proj\syntha_meetings_v2\kb\decisions\

# Unknowns (if the source raised any)
ls C:\Proj\syntha_meetings_v2\kb\unknowns\projects\

# Synthesized entity views
ls C:\Proj\syntha_meetings_v2\kb\entities\projects\
ls C:\Proj\syntha_meetings_v2\kb\entities\people\

# For comparison: page-tier output (the existing flow)
ls C:\Proj\syntha_meetings_v2\wiki\
```

### Inspection checklist

Tick off as you read every produced file:

**Source summary** (`kb/source_summaries/.../<slug>.md`):
- [ ] Summary reflects what the source actually said (no invented detail).
- [ ] `mentioned_entities` lists real subjects from the transcript, not props.
- [ ] No content "blended in" from prior meetings — strictly bounded to this source.

**Facts** (`kb/facts/<type>/<slug>/<fact-type>/<date>-<value>.md`):
- [ ] **`source_quote` is verbatim** from the parsed source body. Open
  the source side-by-side — every character must match. (The agent's
  substring guard rejects paraphrases, but eyeball it.)
- [ ] `fact_type` matches what the source actually states (e.g.
  `project.status` for "X is now complete", not `project.scope`).
- [ ] `value` is normalised (lowercase, kebab-case) and matches the
  source's phrasing.
- [ ] `valid_at` is the date the fact is true (often the meeting date),
  not today's date.
- [ ] `confidence` looks reasonable for how clearly the source stated it.

**Decisions** (`kb/decisions/<date>-<slug>.md`):
- [ ] Each decision corresponds to an **explicit commitment** in the
  source — not an opinion, option, or hypothetical.
- [ ] `decision_date` matches the source date (or whatever the source
  explicitly says).
- [ ] `authority` reflects the source's formality (`informal` for chat,
  `formal` for minuted decisions).

**Unknowns** (`kb/unknowns/<type>/<slug>/<slug>.md`):
- [ ] Each unknown is a **real ambiguity** the source raises but
  doesn't resolve.
- [ ] No "trivia unknowns" — only what genuinely matters to act on.

**Entity index pages** (`kb/entities/<type>/<slug>/index.md`):
- [ ] `## Current State` table shows the right resolved value for each
  `fact_type`.
- [ ] `## History` section lists the chronology in date order.
- [ ] `## Recent Changes` makes sense.
- [ ] No personality speculation / gossip / private detail (especially
  on people pages — the maintenance step in §6 surfaces these).

**Page tier** (`wiki/<slug>.md`):
- [ ] Compare to the kb output for the same source — anything in the
  page tier that's missing from the kb tier? (Common gap: things the
  LLM mentioned but didn't extract as a structured fact.)

### Failure modes to flag

If any of these happen, drop a one-liner under §14 (Open questions)
of `temporal_kb_implementation_plan.md` so the next session sees them:

| Symptom | Likely cause | Quick check |
|---|---|---|
| `result.errors` mentions `unparseable JSON` after retry | LLM not following the JSON shape; prompt may need tightening | `synthadoc jobs status <id>` |
| Facts emitted with wrong `fact_type` | Closed-vocab confusion in the prompt | Open the fact `.md`; compare against FACT_TYPES in `synthadoc/kb/ids.py` |
| Two entities for the same subject ("DocIntel" and "Document Intelligence") | Entity linker is slug-exact only; no fuzzy match yet | `sqlite3 .synthadoc\kb.db "SELECT id, name FROM entities WHERE entity_type='project'"` |
| Empty `source_quote` on facts | Should not happen — validator rejects | If you see one, file an Open question; data corruption |
| Resolver picked wrong "current" value | Rules not matching your situation | Edit `kb_config.yaml`, change `strategy` or `minimum_confidence`, re-run maintenance |
| LLM call timed out / hit rate limit | Gemini quota; per-source budget tripping | Lower `[ingest] max_tokens_per_fact_extract`; agent aborts cleanly |
| Substring-guard rejecting most facts | LLM paraphrasing instead of quoting verbatim | Switch the `facts` role to a stronger model (see §7B) |

---

## 5. Ingest a few more meetings

Once the single-source inspection looks reasonable, batch-ingest a
small window — say a week — and watch the queue:

```powershell
synthadoc ingest --batch C:\Proj\obsidian_notes_2026\Meetings\2026\05    -w syntha_meetings_v2
```

(`--batch` walks the directory; each file becomes its own ingest job,
which in turn enqueues its own kb_pipeline job. The queue worker
processes up to `[queue] max_parallel_ingest` at a time.)

Watch progress:

```powershell
synthadoc jobs list -w syntha_meetings_v2
# Filter to pending / failed:
synthadoc jobs list --status pending -w syntha_meetings_v2
synthadoc jobs list --status failed -w syntha_meetings_v2
```

---

## 6. Run the maintenance pass

After at least one source has flowed through the pipeline (and ideally
the batch from §5 has finished), run the deterministic maintenance:

```powershell
synthadoc kb maintenance run -w syntha_meetings_v2
```

You'll see counts per category. Open `kb_health.md`:

```powershell
notepad C:\Proj\syntha_meetings_v2\kb\maintenance\kb_health.md
```

**Expected after a clean batch:**

| Metric | Expected | What FAIL means here |
|---|---|---|
| `conflicts` | 0 | Two active facts disagree — likely the LLM extracted contradictory facts from different meetings about the same project/date |
| `stale_pages` | 0-5 | History older than newest fact; usually OK |
| `orphan_facts` | 0 | Facts pointing at entities that no longer exist |
| `facts_without_evidence` | 0 | Empty quotes — should be impossible |
| `conclusions_without_facts` | 0 | (Conclusions are Layer 4) |
| `duplicate_entity_candidates` | 0-N | Heuristic matches; some real, some aliasing — review by hand |
| `broken_links` | 0 | Wikilinks to slugs not on disk |
| `people_pages_with_policy_flags` | 0 | Spec §3.7 violations — review each flagged line |

**Open the per-category reports under `kb/maintenance/`** and skim
them. The reports are overwrite-style; every `kb maintenance run`
rewrites the snapshot.

---

## 7. Decide what to do next

After inspecting the test source(s) and the maintenance report you'll
have a feel for prompt quality. Three paths:

### A. Prompts work well → promote the v2 tier

Either:

- **Adopt the temporal tier on the original `syntha_meetings` wiki** —
  `synthadoc kb init -w syntha_meetings && synthadoc kb backfill -w syntha_meetings`,
  then re-feed (or wait for the next standup to trigger the auto-pipeline).
- **Switch your daily flow to `syntha_meetings_v2`** — point your
  feed script at the new wiki and retire the old one.

Either way, schedule nightly maintenance:

```powershell
synthadoc schedule add `
   --op "kb maintenance run" --cron "0 3 * * *" -w syntha_meetings_v2
```

### B. Prompts need tuning → iterate on a stronger model

Edit `C:\Proj\syntha_meetings_v2\.synthadoc\config.toml`:

```toml
[agents]
default = { provider = "gemini", model = "gemini-3.1-pro-preview" }
ingest  = { provider = "gemini", model = "gemini-3.1-pro-preview" }

# Point ONLY fact extraction at a stronger model. Summary + decisions +
# unknowns can stay on the cheaper one.
facts   = { provider = "anthropic", model = "claude-opus-4-7" }

[ingest]
# Cap runaway cost — abort fact extraction if one source burns more than this.
max_tokens_per_fact_extract = 30000
```

Bust the cached extraction and re-ingest the test source:

```powershell
synthadoc cache clear -w syntha_meetings_v2
# Re-ingest one source with --force so the kb_pipeline runs again on it:
synthadoc ingest --force "<file>.md" -w syntha_meetings_v2
```

Iterate on §4's inspection checklist until extraction yield is
acceptable, then return to path A.

### C. Bail out and try later

```powershell
# Stop the v2 server (Ctrl-C in its terminal, or kill the PID printed in the banner)
synthadoc uninstall syntha_meetings_v2
```

The original `syntha_meetings` wiki is completely unaffected.

---

## 8. What to file when something looks off

Drop a one-liner under §14 (Open questions) of
`temporal_kb_implementation_plan.md`. Examples worth recording:

- "Gemini 3.1 keeps paraphrasing the source_quote — extraction yield is 60%; bump `facts` role to Opus."
- "Pravin and Asha are real people but the linker created `entity.person.pravin-and-asha` from a co-occurrence; needs split."
- "Decision authority defaulted to `informal` even on minuted decisions; prompt clarification needed."
- "Want to sample-pipeline an existing source without re-feeding → need `synthadoc kb pipeline <source-id>` CLI."

These guide the next session's prompt-tuning + small-feature work.

---

## Quick reference — commands cheat sheet

```powershell
# One-time setup
synthadoc install syntha_meetings_v2 --target C:\Proj --domain "..."
synthadoc kb init -w syntha_meetings_v2

# Daily flow (in two terminals)
synthadoc serve -w syntha_meetings_v2                           # T1
synthadoc ingest <file> -w syntha_meetings_v2                   # T2
synthadoc ingest --batch <dir> -w syntha_meetings_v2            # T2

# Inspection
sqlite3 .synthadoc\kb.db "SELECT * FROM sources LIMIT 5"
sqlite3 .synthadoc\kb.db "SELECT * FROM facts ORDER BY observed_at DESC LIMIT 5"
sqlite3 .synthadoc\kb.db "SELECT * FROM entities WHERE entity_type='project'"

# Periodic maintenance
synthadoc kb maintenance run -w syntha_meetings_v2
synthadoc kb relink -w syntha_meetings_v2                       # after manual edits

# Watch jobs
synthadoc jobs list -w syntha_meetings_v2
synthadoc jobs status <id> -w syntha_meetings_v2

# Iterate on a model change
synthadoc cache clear -w syntha_meetings_v2
synthadoc ingest --force <file> -w syntha_meetings_v2

# Bail out
synthadoc uninstall syntha_meetings_v2
```

For the full feature reference see [`docs/temporal-kb.md`](./temporal-kb.md).
