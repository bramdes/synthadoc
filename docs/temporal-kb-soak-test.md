# Temporal KB Soak Test — `syntha_meetings`

A manual playbook for adopting the temporal KB tier on a real wiki and
verifying the prompts work on real ambiguity. Designed to be safe
(every step is non-destructive to your existing `wiki/` pages) and
inspectable (stop at any checkpoint and look at what landed).

**Target wiki:** `syntha_meetings` at `C:\Proj\syntha_meetings`
**Engine:** `C:\Proj\synthadoc` (running on Gemini 3.1 Pro Preview per your `[agents]` config)

> **What this is going to do.** Create a `kb/` folder + `.synthadoc/kb.db`
> inside the wiki. Populate `kb.db` with source rows for files you've
> already ingested. Run the LLM-driven fact / decision / unknown
> extractors on at least one source. Spend ~5-50K tokens per source
> on Gemini. Your existing `wiki/*.md` pages are **never** modified by
> any step here.

---

## 0. Pre-flight

```powershell
# In the engine repo
cd C:\Proj\synthadoc

# Sanity-check which wiki is active
.\.venv\Scripts\synthadoc.exe use
# Expect: "Active wiki: 'syntha_meetings'" (or similar)
```

**Stop the running server.** The kb tier is opt-in; running `kb init`
while the server is up is harmless (the server only watches `jobs.db`),
but later steps benefit from the server being up to drain the kb_pipeline
queue jobs. So:

```powershell
# If the server is running in another terminal (you mentioned PID 47412):
taskkill /PID 47412 /F
# OR: just hit Ctrl-C in the terminal where you ran `synthadoc serve`
```

**Backup `.synthadoc/`** (the SQLite databases — small and easy to
restore if you want a clean slate):

```powershell
Copy-Item -Recurse -Force `
   "C:\Proj\syntha_meetings\.synthadoc" `
   "C:\Proj\syntha_meetings\.synthadoc.backup-pre-kb"
```

If anything goes sideways you can `rm -r .synthadoc` and rename the
backup back into place. Your `wiki/` is unaffected at any step.

---

## 1. Initialise the kb tier

```powershell
.\.venv\Scripts\synthadoc.exe kb init -w syntha_meetings
```

You should see:

```
KB tier initialised.
  kb/          C:\Proj\syntha_meetings\kb
  kb.db        C:\Proj\syntha_meetings\.synthadoc\kb.db
  config       C:\Proj\syntha_meetings\kb_config.yaml
```

**Inspect what landed:**

```powershell
# Folder tree (should be empty except for the maintenance stub files)
Get-ChildItem -Recurse "C:\Proj\syntha_meetings\kb" | Select-Object FullName

# Resolution rules — read and adjust if any fact_type needs a different strategy
notepad "C:\Proj\syntha_meetings\kb_config.yaml"
```

For a meetings-heavy wiki the default rules are sensible:
- `project.status` / `project.owner` → `latest_valid_at_wins`
- `project.scope` → `requires_review` (won't auto-overwrite)
- `decision.made` → `append_only`

Save & close.

---

## 2. Backfill source rows from your existing audit history

This populates `kb.db.sources` from the ingest records already in
`.synthadoc/audit.db`. No LLM calls; no fact extraction. Just an index
of what you've ingested.

```powershell
# Dry-run first — shows what would be inserted, writes nothing
.\.venv\Scripts\synthadoc.exe kb backfill -w syntha_meetings --dry-run
```

Expect output like:

```
[wiki: syntha_meetings]
would seed: source.document.2026-05-15.standup-2026-05-15  (file=present)
would seed: source.document.2026-05-16.weekly-review       (file=present)
...
Backfill complete: 42 would seed, 0 already present, 0 skipped (no source_hash).
```

If the numbers look right, run for real:

```powershell
.\.venv\Scripts\synthadoc.exe kb backfill -w syntha_meetings
```

**Verify:**

```powershell
# Count source rows
sqlite3 "C:\Proj\syntha_meetings\.synthadoc\kb.db" "SELECT COUNT(*) FROM sources"

# Eyeball a few
sqlite3 "C:\Proj\syntha_meetings\.synthadoc\kb.db" `
   "SELECT id, source_type, title, raw_path FROM sources LIMIT 5"
```

At this point the kb tier exists as a passive index. Page-tier writes
continue to ignore it; nothing about your `wiki/` has changed.

---

## 3. Test on ONE source first

The safest soak test is to run the full kb pipeline against a single
source end-to-end and inspect every file it produces. Two ways:

### Option A (recommended): ingest a fresh meeting

When your next standup transcript lands under
`C:\Proj\obsidian_notes_2026\Meetings\2026\05\`, ingest it normally.
The orchestrator's `_enqueue_kb_pipeline` hook will fire automatically
after the page-tier ingest succeeds:

```powershell
# Start the server in one terminal:
.\.venv\Scripts\synthadoc.exe serve -w syntha_meetings

# In another terminal, ingest one file:
.\.venv\Scripts\synthadoc.exe ingest `
   "C:\Proj\obsidian_notes_2026\Meetings\2026\05\2026-05-24-standup.md" `
   -w syntha_meetings

# Watch the jobs queue
.\.venv\Scripts\synthadoc.exe jobs list -w syntha_meetings
```

You'll see TWO jobs land:
- One `ingest` job — runs the page-tier (your existing wiki update).
- One `kb_pipeline` job — runs the fact-tier (summary + facts + decisions + unknowns + render).

Wait for both to reach `completed`. The kb_pipeline job typically takes
10-60 seconds depending on transcript length.

**If you'd rather not wait for a new meeting,** you can re-feed one
recent meeting from the existing pile by pointing your feed script at a
narrower window:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
   -File "C:\Proj\syntha_meetings_test\feed_meetings.ps1" `
   -MeetingsRoot "C:\Proj\obsidian_notes_2026\Meetings\2026\05" `
   -Days 1
```

The deduplication check in IngestAgent will skip files whose SHA-256 is
already in `audit.db`, so this only ingests anything *new*. If nothing
is new and you really want to soak-test, you can pass `--force` to
re-ingest a specific file — but that also re-runs the page-tier, so
your `wiki/<slug>.md` will get an additional appended section. Only
use `--force` when you don't mind the wiki page growing.

### Option B: pick one historical source and dry-run the pipeline by hand

If you'd rather sample an existing source without re-feeding, the kb
pipeline doesn't currently have a standalone `synthadoc kb pipeline <id>`
CLI (that's tracked as a small Layer 4 follow-up). Until then, Option A
is the cleanest path.

---

## 4. Inspect the produced markdown by hand

After the kb_pipeline job completes, four families of files should
exist for the test source. Open each one in your editor and verify
against the checklist below.

### Where to look

```powershell
# Source summary
ls "C:\Proj\syntha_meetings\kb\source_summaries\documents\"
# or \meetings\ if your --type guess hit meeting_transcript

# Facts emitted from this source (grouped by entity)
ls "C:\Proj\syntha_meetings\kb\facts\projects\"
ls "C:\Proj\syntha_meetings\kb\facts\people\"

# Decisions (if any explicit ones were in the source)
ls "C:\Proj\syntha_meetings\kb\decisions\"

# Unknowns (if the source raised any)
ls "C:\Proj\syntha_meetings\kb\unknowns\projects\"

# Synthesized entity views
ls "C:\Proj\syntha_meetings\kb\entities\projects\"
ls "C:\Proj\syntha_meetings\kb\entities\people\"
```

### Inspection checklist

Read every produced file and tick off the items:

**Source summary** (`kb/source_summaries/.../<slug>.md`):
- [ ] Summary reflects what the source actually said (no invented detail).
- [ ] `mentioned_entities` lists real subjects from the transcript, not props.
- [ ] No content "blended in" from prior meetings — strictly bounded to this source.

**Facts** (`kb/facts/<type>/<slug>/<fact-type>/<date>-<value>.md`):
- [ ] **`source_quote` is verbatim** from the parsed source body. Open the source side-by-side — every character must match. (The agent's substring guard rejects paraphrases, but it's worth eyeballing.)
- [ ] `fact_type` matches what the source actually states (e.g. `project.status` for "X is now complete", not `project.scope`).
- [ ] `value` is normalised (lowercase, kebab-case) and matches the source's phrasing.
- [ ] `valid_at` is the date the fact is true (often the meeting date), not today's date.
- [ ] `confidence` looks reasonable for how clearly the source stated it.

**Decisions** (`kb/decisions/<date>-<slug>.md`):
- [ ] Each decision corresponds to an **explicit commitment** in the source — not an opinion, option, or hypothetical.
- [ ] `decision_date` matches the source date (or whatever the source explicitly says).
- [ ] `authority` reflects the source's formality (`informal` for chat, `formal` for minuted decisions).

**Unknowns** (`kb/unknowns/<type>/<slug>/<slug>.md`):
- [ ] Each unknown is a **real ambiguity** the source raises but doesn't resolve.
- [ ] No "trivia unknowns" — only what genuinely matters to act on.

**Entity index pages** (`kb/entities/<type>/<slug>/index.md`):
- [ ] `## Current State` table shows the right resolved value for each `fact_type`.
- [ ] `## History` section lists the chronology in date order.
- [ ] `## Recent Changes` makes sense.
- [ ] No personality speculation / gossip / private detail (especially on people pages — see step 5).

### Failure modes to flag

If any of these happen, file a one-line note in
`temporal_kb_implementation_plan.md` under §14 (Open questions) so the
next session sees them:

| Symptom | Likely cause | Quick check |
|---|---|---|
| `result.errors` mentions `unparseable JSON` after retry | LLM not following the JSON shape; prompt may need tightening | `synthadoc jobs status <id>` |
| Facts emitted with wrong `fact_type` | Closed-vocab confusion in the prompt | Look at `kb/facts/.../*.md`; compare to FACT_TYPES in `synthadoc/kb/ids.py` |
| Two entities for the same subject ("DocIntel" and "Document Intelligence") | Entity linker is slug-exact only; doesn't fuzzy-match yet | `sqlite3 kb.db "SELECT id, name FROM entities WHERE entity_type='project'"` |
| Empty `source_quote` on facts | Should not happen — validator rejects | If you see one, the schema column has wrong data; file an Open question |
| Resolver picked wrong "current" value | Rules not matching your situation | Edit `kb_config.yaml`, set `minimum_confidence` or change `strategy`, re-run maintenance |
| LLM call timed out / hit rate limit | Gemini quota; per-source budget tripping | Lower `[ingest] max_tokens_per_fact_extract` and let it abort cleanly |

---

## 5. Run the maintenance pass

After at least one source has flowed through the pipeline, run the
deterministic maintenance:

```powershell
.\.venv\Scripts\synthadoc.exe kb maintenance run -w syntha_meetings
```

You'll see counts per category. Open `kb/maintenance/kb_health.md` —
this is the snapshot you'd watch over time:

```powershell
notepad "C:\Proj\syntha_meetings\kb\maintenance\kb_health.md"
```

**Expected on a freshly-seeded wiki with one piloted source:**

| Metric | Expected | What FAIL means here |
|---|---|---|
| `conflicts` | 0 | Two active facts disagree — probably from a re-ingest |
| `stale_pages` | 0-5 | History older than newest fact; usually OK |
| `orphan_facts` | 0 | Facts pointing at entities that no longer exist |
| `facts_without_evidence` | 0 | Empty quotes — should be impossible with the agent's guard |
| `conclusions_without_facts` | 0 | (Conclusions are Layer 4) |
| `duplicate_entity_candidates` | 0-N | Heuristic matches; some are real, some are aliasing artefacts |
| `broken_links` | 0 | Wikilinks to slugs not on disk |
| `people_pages_with_policy_flags` | 0 | Spec §3.7 violations — review each flagged line |

**Open the per-category reports under `kb/maintenance/`** and skim them.
The reports are overwrite-style; every `kb maintenance run` rewrites
the snapshot.

---

## 6. Decide what to do next

After inspecting the test source and the maintenance report you'll
have a feel for prompt quality. Three paths from here:

**A. Prompts work well → adopt the tier widely.**

Re-feed the meetings backlog and let the kb pipeline run on everything:

```powershell
# Run your existing feed script — only NEW meetings will be ingested
# (the SHA dedup check skips already-ingested files), but each of those
# triggers the kb pipeline now.
powershell -NoProfile -ExecutionPolicy Bypass `
   -File "C:\Proj\syntha_meetings_test\feed_meetings.ps1" `
   -MeetingsRoot "C:\Proj\obsidian_notes_2026\Meetings\2026\05" `
   -Days 30

# Or if you want to backfill the kb tier across the existing audit history
# (re-ingesting with --force so the orchestrator hook fires):
# WARNING: this re-runs the page-tier ingest too — your wiki pages
# will get duplicate sections.
# Only do this if you've thought through the implications.
```

Schedule a nightly maintenance pass:

```powershell
.\.venv\Scripts\synthadoc.exe schedule add `
   --op "kb maintenance run" --cron "0 3 * * *" -w syntha_meetings
```

**B. Prompts need tuning → iterate on a stronger model.**

Edit `.synthadoc/config.toml`:

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

Re-run step 3 (Option A) on the same source after `synthadoc cache clear -w syntha_meetings`
to bypass any cached extraction.

**C. Bail out and try later.**

Stop the server, delete `kb/` + `.synthadoc/kb.db`, restore the backup
from step 0:

```powershell
Remove-Item -Recurse -Force "C:\Proj\syntha_meetings\kb"
Remove-Item -Force "C:\Proj\syntha_meetings\.synthadoc\kb.db"
Remove-Item -Force "C:\Proj\syntha_meetings\kb_config.yaml"
# OR full rollback:
Remove-Item -Recurse -Force "C:\Proj\syntha_meetings\.synthadoc"
Rename-Item "C:\Proj\syntha_meetings\.synthadoc.backup-pre-kb" `
            "C:\Proj\syntha_meetings\.synthadoc"
```

Your `wiki/` and `raw_sources/` are untouched throughout.

---

## 7. What to file when something looks off

When you spot a prompt regression or a UX gap, drop a one-liner under
§14 (Open questions) of `temporal_kb_implementation_plan.md`. Examples
of what's worth recording:

- "Gemini 3.1 keeps paraphrasing the source_quote — extraction yield is 60%; bump to Opus on facts?"
- "Pravin and Asha are real people but the linker created `entity.person.pravin-and-asha` from a co-occurrence; needs split."
- "Decision authority defaulted to `informal` even on minuted decisions; prompt clarification needed."
- "Want to sample-pipeline an existing source without re-feeding → need `synthadoc kb pipeline <source-id>` CLI."

These guide the next session's prompt-tuning + small-feature work.

---

## Quick reference — commands cheat sheet

```powershell
# Setup (one-time)
synthadoc kb init -w syntha_meetings
synthadoc kb backfill -w syntha_meetings

# Each new meeting (automatic via the orchestrator hook on ingest)
synthadoc ingest <file> -w syntha_meetings

# Inspect what got produced
sqlite3 .synthadoc\kb.db "SELECT * FROM sources LIMIT 5"
sqlite3 .synthadoc\kb.db "SELECT * FROM facts ORDER BY observed_at DESC LIMIT 5"

# Periodic maintenance
synthadoc kb maintenance run -w syntha_meetings
synthadoc kb relink -w syntha_meetings           # after manual edits

# Watch jobs
synthadoc jobs list -w syntha_meetings
synthadoc jobs status <id> -w syntha_meetings
```

For the full feature reference see [`docs/temporal-kb.md`](./temporal-kb.md).
