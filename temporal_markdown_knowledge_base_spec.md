# Temporal Markdown Knowledge Base for Project, Topic, Person, and Document Intelligence

## 1. Vision

The goal is to build an AI-assisted knowledge base that behaves less like a passive wiki and more like a disciplined project knowledge manager.

The system should help manage complex, evolving information across documents, meetings, transcripts, project artifacts, BRDs, FSDs, architecture notes, people, topics, decisions, and open issues. It should support both human browsing and LLM-based reasoning.

The core design principle is:

> Pages are not the source of truth. Pages are synthesized views over timestamped, source-backed facts.

This means the knowledge base should not simply summarize documents into static markdown. It should maintain a structured, time-aware memory of facts, claims, decisions, assumptions, and conclusions.

The system should be usable with tools such as Obsidian, VS Code, GitHub, local coding agents, local LLMs, and markdown-compatible static site tools. It should avoid requiring a heavy proprietary platform.

The intended end state is a knowledge base where a user or LLM can reliably ask:

- What is the current status of this project?
- What did we believe three weeks ago?
- What changed yesterday?
- Which facts are source-backed?
- Which conclusions are derived from facts?
- Which facts are outdated or superseded?
- What are the open unknowns?
- Which decisions have been made?
- Which project pages are stale or messy?
- Which facts were extracted from which document, meeting, or transcript?
- Which people, projects, topics, and decisions are connected?
- Which generated summaries should not yet be trusted?

The system should preserve history, but not pollute current pages with every old fact. It should be able to explain both the current state and the evolution of that state.

---

## 2. Problem Statement

Local LLMs with context windows around 128K tokens are not sufficient for serious multi-document reasoning if the model is expected to hold all documents, summaries, and prior conclusions in prompt context.

Naive compaction and summarization lose important details. This is especially dangerous for requirements, project status, stakeholder responsibility, BRD/FSD traceability, architecture reviews, decisions, risks, assumptions, and meeting-derived updates.

The actual problem is not only context length. The harder problem is knowledge lifecycle:

- Facts change over time.
- Meeting transcripts may update or contradict earlier documents.
- Some sources are formal; others are informal.
- Some claims are directly observed; others are inferred.
- Some pages become stale.
- AI-generated summaries can quietly drift away from the source evidence.
- Topic pages, person pages, and project pages need ongoing maintenance.
- The system needs cleanup and consolidation, not just ingestion.
- A human needs to know what is trusted, unreviewed, outdated, or contradicted.

A simple OpenKB-style generated markdown wiki is useful as a bootstrap pattern, but it is not sufficient as the operating model. The knowledge base must be managed like a library with governance, timestamps, source evidence, review state, and maintenance jobs.

---

## 3. Design Principles

### 3.1 Sources Are Immutable

Original inputs should never be edited by the AI once imported. Raw source files are evidence.

Examples:

- BRD documents
- FSD documents
- meeting transcripts
- email exports
- architecture decks
- test case documents
- notes
- PDFs
- DOCX files
- PPTX files

The system may create parsed versions, markdown conversions, summaries, facts, and topic pages, but the raw source should remain append-only.

### 3.2 Facts Are Timestamped

Every important fact should carry:

- source reference
- source timestamp
- observation timestamp
- valid/effective timestamp where known
- confidence
- review status
- entity links
- fact type
- supersession relationship where applicable

The system should avoid timeless statements such as:

> The project is complete.

Instead, it should prefer:

> The project status is recorded as completed as of 2026-05-22, based on the 2026-05-22 project update meeting. Earlier sources from 2026-05-01 described it as in progress.

### 3.3 Entity Pages Are Synthesized Views

Project, person, and topic pages are not primary truth stores. They are compiled or curated summaries based on timestamped facts and reviewed conclusions.

A project page should show the current state. A history page should show how the state changed. Atomic fact records should show why the system believes each thing.

### 3.4 Observed Facts and Derived Conclusions Must Be Separate

The system must not blur source-backed observations with LLM-generated interpretations.

Example:

Observed fact:

> The meeting transcript says "Drop 1 is complete."

Derived conclusion:

> Document Intelligence is ready for production.

The first can be extracted directly from the source. The second may require additional evidence and should be marked as a conclusion, not a fact.

### 3.5 Confidence and Review Status Are Mandatory

AI-extracted knowledge should not automatically become trusted knowledge.

Every extracted fact or important conclusion should have confidence and review metadata.

Example statuses:

- `unreviewed`
- `reviewed`
- `rejected`
- `needs_clarification`

Example confidence values:

- `high`
- `medium`
- `low`

### 3.6 Unknowns Must Be First-Class Objects

The system should explicitly track what is unknown, unclear, or not evidenced.

For example:

- Is the full project completed or only Drop 1?
- Has production rollout happened?
- Who owns the final sign-off?
- Is this requirement still in scope?
- Is this person still the owner?
- Is the FSD updated after the BRD change?

This prevents the system from becoming falsely confident.

### 3.7 People Pages Must Be Professional and Bounded

People pages should be limited to project-relevant information.

Allowed:

- role
- project involvement
- responsibilities
- decisions made
- open actions
- professional preferences relevant to collaboration
- known ownership or accountability

Avoid:

- personality speculation
- private or sensitive personal details
- gossip
- emotional judgments
- irrelevant personal attributes

### 3.8 Decisions Need Their Own Pages

Important decisions should not be buried in summaries. They should have explicit decision records.

A decision page should capture:

- decision
- date
- source
- rationale
- impact
- related projects/topics
- reversal conditions
- whether it is formal or informal
- whether it has been reviewed

### 3.9 Stable IDs Are Required

Names, titles, and file paths can change. Important objects should have stable IDs.

Examples:

- `project.document-intelligence`
- `topic.document-parsing`
- `person.pravin`
- `decision.2026-05-22.drop1-complete`
- `fact.project.document-intelligence.status.2026-05-22`
- `source.meeting.2026-05-22.doc-intel-update`

Obsidian-style links and relative markdown links are useful for humans, but stable IDs are required for automation.

### 3.10 Maintenance and Garbage Collection Are Core Features

The system needs a recurring maintenance process that cleans, organizes, deduplicates, and validates the knowledge base.

This is not optional. Without maintenance, the knowledge base will rot.

Maintenance should detect:

- orphan pages
- duplicate topics
- stale summaries
- facts without sources
- conclusions without evidence
- unresolved conflicts
- broken links
- low-confidence facts never reviewed
- pages that mix unrelated topics
- resolved issues still marked open
- old status facts not marked superseded
- current pages that contradict newer facts

---

## 4. Functional Requirements

### 4.1 Source Ingestion

The system shall ingest raw source documents and preserve them immutably.

Supported input types should eventually include:

- Markdown
- TXT
- PDF
- DOCX
- PPTX
- XLSX/CSV
- meeting transcripts
- email exports
- copied notes

Each source shall receive a stable source ID.

Each source shall have metadata including:

```yaml
id: source.meeting.2026-05-22.doc-intel-update
type: source
source_type: meeting_transcript
title: "2026-05-22 Document Intelligence Update"
created_at: 2026-05-22T10:00:00+08:00
ingested_at: 2026-05-23T09:00:00+08:00
author: unknown
authority: informal
raw_path: sources/raw/meetings/2026-05-22-doc-intel-update.md
parsed_path: sources/parsed/meetings/2026-05-22-doc-intel-update.md
```

### 4.2 Source Summary Generation

Each source should have a source summary.

A source summary should only summarize that source. It should not merge in knowledge from other sources unless clearly marked.

A source summary should include:

- short summary
- key points
- mentioned entities
- extracted facts
- decisions mentioned
- open questions
- action items
- source reliability/authority
- links to raw/parsed source
- extraction status

Example:

```markdown
---
id: summary.source.meeting.2026-05-22.doc-intel-update
type: source_summary
source_id: source.meeting.2026-05-22.doc-intel-update
review_status: unreviewed
confidence: medium
---

# Source Summary: 2026-05-22 Document Intelligence Update

## Summary

This meeting reports that Drop 1 is complete, while production rollout and test evidence cleanup remain open.

## Key Points

- Drop 1 was reported as complete.
- Production rollout was not confirmed.
- Test evidence cleanup remains open.

## Mentioned Entities

- [[entities/projects/document-intelligence/index.md]]
- [[entities/topics/test-evidence/index.md]]

## Extracted Facts

- [[facts/projects/document-intelligence/status/2026-05-22-completed.md]]

## Unknowns

- Does "complete" mean Drop 1 only or full production readiness?
```

### 4.3 Atomic Fact Extraction

The system shall extract atomic facts from sources.

A fact should be narrow, source-backed, timestamped, and linked to one or more entities.

Example:

```markdown
---
id: fact.project.document-intelligence.status.2026-05-22
type: fact
entity_id: project.document-intelligence
entity_path: entities/projects/document-intelligence/index.md
fact_type: project.status
value: completed
valid_at: 2026-05-22
observed_at: 2026-05-23T09:00:00+08:00
source_id: source.meeting.2026-05-22.doc-intel-update
source_path: sources/parsed/meetings/2026-05-22-doc-intel-update.md
source_span: paragraph-18
source_quote: "Drop 1 is now complete."
confidence: high
review_status: unreviewed
supersedes:
  - fact.project.document-intelligence.status.2026-05-01
---

# Fact: Document Intelligence Drop 1 status completed as of 2026-05-22

The source states that Drop 1 is now complete.
```

The system should distinguish between fact types.

Examples:

- `project.status`
- `project.owner`
- `project.scope`
- `project.milestone`
- `person.role_on_project`
- `topic.definition`
- `requirement.coverage`
- `decision.made`
- `open_issue.created`
- `open_issue.closed`
- `assumption.created`
- `assumption.invalidated`

### 4.4 Observed Fact vs Derived Conclusion

The system shall classify extracted knowledge as one of:

- `observed_fact`
- `derived_conclusion`
- `assumption`
- `decision`
- `open_issue`
- `conflict`
- `unknown`

Observed facts require direct source evidence.

Derived conclusions require reasoning over one or more facts and should include a reasoning note.

Example conclusion:

```markdown
---
id: conclusion.project.document-intelligence.production-readiness.2026-05-23
type: derived_conclusion
entity_id: project.document-intelligence
conclusion_type: project.readiness
value: not_confirmed
based_on:
  - fact.project.document-intelligence.status.2026-05-22
  - fact.project.document-intelligence.issue.test-evidence.2026-05-22
confidence: medium
review_status: unreviewed
---

# Conclusion: Production readiness not confirmed

Drop 1 is reported as complete, but production rollout is not explicitly confirmed and test evidence cleanup remains open.
```

### 4.5 Project, Person, and Topic Pages

Every important project, person, and topic should have an entity folder.

Example:

```text
entities/
  projects/
    document-intelligence/
      index.md
      history.md
      decisions/
      open-issues.md
  people/
    pravin/
      index.md
      history.md
  topics/
    document-parsing/
      index.md
      history.md
```

The `index.md` page should represent the current synthesized view.

The `history.md` page should represent the meaningful historical evolution.

### 4.6 Entity Page Template

Project/entity pages should follow a consistent structure.

Example:

```markdown
---
id: project.document-intelligence
type: project
status: active
current_state_review_status: reviewed
last_reviewed: 2026-05-23
---

# Document Intelligence

## Current Summary

Drop 1 is reported as complete as of 2026-05-22. Production rollout is not yet confirmed. Test evidence cleanup remains open.

## Current State

| Field | Value | As Of | Confidence | Evidence |
|---|---|---:|---|---|
| Status | completed | 2026-05-22 | high | [[facts/projects/document-intelligence/status/2026-05-22-completed.md]] |
| Production readiness | not confirmed | 2026-05-23 | medium | [[conclusions/projects/document-intelligence/production-readiness-2026-05-23.md]] |

## Current Open Issues

- Test evidence cleanup remains open.
- Production rollout is not confirmed.

## Unknowns

- Does "complete" mean Drop 1 only or full end-to-end completion?
- Who owns final production rollout sign-off?

## Recent Important Changes

- 2026-05-22: Status changed from `in_progress` to `completed`.

## Decisions

- [[decisions/projects/document-intelligence/2026-05-22-drop1-complete.md]]

## History

- [[history.md]]
```

### 4.7 History Pages

Every important project/person/topic may have a `history.md` page.

The history page should not be the raw fact store. It should be a human-readable historical synthesis based on facts.

It should include:

- status changes
- ownership changes
- scope changes
- decision evolution
- major open issue lifecycle
- assumptions invalidated
- contradictions resolved

Example:

```markdown
---
id: history.project.document-intelligence
type: history
entity_id: project.document-intelligence
last_rebuilt: 2026-05-23T09:30:00+08:00
---

# Document Intelligence History

## Status Changes

### 2026-05-22

Status changed to `completed` for Drop 1.

Evidence:
- [[facts/projects/document-intelligence/status/2026-05-22-completed.md]]

Supersedes:
- [[facts/projects/document-intelligence/status/2026-05-01-in-progress.md]]

### 2026-05-01

Status was recorded as `in_progress`.

Evidence:
- [[facts/projects/document-intelligence/status/2026-05-01-in-progress.md]]

## Important Open Issue Changes

### 2026-05-22

Test evidence cleanup remains open.
```

### 4.8 Unknowns

Unknowns must be captured explicitly.

Example:

```markdown
---
id: unknown.project.document-intelligence.production-rollout
type: unknown
entity_id: project.document-intelligence
created_at: 2026-05-23
status: open
related_sources:
  - source.meeting.2026-05-22.doc-intel-update
---

# Unknown: Production rollout status

The 2026-05-22 update confirms Drop 1 completion but does not confirm production rollout.
```

Unknowns can be closed when later evidence resolves them.

### 4.9 Decision Records

Important decisions must be stored separately.

Example:

```markdown
---
id: decision.2026-05-22.drop1-complete
type: decision
entity_id: project.document-intelligence
decision_date: 2026-05-22
status: active
authority: informal
review_status: unreviewed
source_id: source.meeting.2026-05-22.doc-intel-update
---

# Decision: Drop 1 Completion Accepted

## Decision

Drop 1 is considered complete for current scope.

## Rationale

The project update stated that Drop 1 is now complete.

## Evidence

- [[sources/parsed/meetings/2026-05-22-doc-intel-update.md]]
- [[facts/projects/document-intelligence/status/2026-05-22-completed.md]]

## Consequences

- Project status may move from `in_progress` to `completed` for Drop 1.
- Production rollout still needs separate confirmation.

## Reversal Conditions

- New defects are found.
- Completion was later clarified to mean only partial scope.
- Test evidence is rejected.
```

### 4.10 Maintenance Process

The system shall include recurring maintenance jobs.

Maintenance jobs should include:

1. ingest new sources
2. summarize sources
3. extract facts
4. link facts to entities
5. detect changed facts
6. mark superseded facts
7. detect contradictions
8. update or propose updates to entity pages
9. update history pages
10. update decision pages
11. update unknowns
12. detect stale pages
13. detect broken links
14. detect duplicate entities/topics
15. detect orphan facts
16. detect facts without evidence
17. detect conclusions without observed facts
18. produce a maintenance report
19. produce a review queue

Maintenance should not silently rewrite high-trust pages unless the system is explicitly configured to do so.

---

## 5. Non-Functional Requirements

### 5.1 Human Inspectability

The knowledge base must be readable and editable as normal markdown.

A human should be able to open it in:

- Obsidian
- VS Code
- GitHub
- local filesystem
- static site browser such as MkDocs

### 5.2 LLM Friendliness

The knowledge base must be easy for an LLM or coding agent to navigate.

Requirements:

- stable folder structure
- consistent templates
- YAML frontmatter
- relative markdown links
- stable IDs
- clear page types
- clear source references
- clear distinction between current state, history, facts, decisions, and unknowns

### 5.3 Determinism Where Possible

The system should avoid free-form agentic rewriting where deterministic updates are possible.

Examples:

- stable IDs generated deterministically
- links checked programmatically
- facts indexed into SQLite
- current status resolved by rule where possible
- stale pages detected by metadata
- garbage collection checks run as scripts

### 5.4 Local-First Operation

The system should be able to run locally with local models and local files.

Cloud integration may be optional, but the core should not depend on SaaS services.

### 5.5 Version Control

The KB should work well with Git.

Expected workflow:

- raw sources added
- AI proposes generated files
- review queue created
- human or senior LLM reviews changes
- approved changes committed
- diffs remain inspectable

---

## 6. Proposed Solution Design

### 6.1 Repository Structure

Recommended structure:

```text
project-kb/
  README.md
  AGENTS.md
  kb_config.yaml

  docs/
    index.md

    sources/
      raw/
        meetings/
        documents/
        emails/
        decks/
      parsed/
        meetings/
        documents/
        emails/
        decks/

    source_summaries/
      meetings/
      documents/
      emails/
      decks/

    entities/
      projects/
        document-intelligence/
          index.md
          history.md
          open-issues.md
          decisions/
      people/
        pravin/
          index.md
          history.md
      topics/
        document-parsing/
          index.md
          history.md

    facts/
      projects/
      people/
      topics/
      requirements/

    conclusions/
      projects/
      people/
      topics/

    decisions/
      projects/
      topics/

    unknowns/
      projects/
      people/
      topics/

    maintenance/
      review_queue.md
      stale_pages.md
      conflicts.md
      orphan_facts.md
      duplicate_entities.md
      broken_links.md
      maintenance_reports/

  data/
    kb.sqlite
    embeddings/
    indexes/

  scripts/
    kb_ingest.py
    kb_summarize_source.py
    kb_extract_facts.py
    kb_link_entities.py
    kb_resolve_current_state.py
    kb_update_history.py
    kb_check_links.py
    kb_detect_stale.py
    kb_detect_duplicates.py
    kb_garbage_collect.py
    kb_maintenance.py
```

### 6.2 AGENTS.md

The repository should include an `AGENTS.md` file that instructs LLM agents how to behave.

Example:

```markdown
# AGENTS.md

## Purpose

This repository is a temporal markdown knowledge base. Do not treat current pages as absolute truth. Use source-backed facts and timestamps.

## Rules

1. Never modify files under `docs/sources/raw/`.
2. Do not convert a derived conclusion into an observed fact.
3. Do not state current truth without checking timestamped facts.
4. Every important claim must link to source-backed evidence.
5. Every extracted fact must have confidence and review status.
6. Every project/person/topic entity must use a stable ID.
7. If evidence is ambiguous, create or update an unknown.
8. If a newer fact supersedes an older fact, mark the relationship explicitly.
9. Do not add personal speculation to people pages.
10. Do not silently overwrite reviewed pages. Put proposed changes in `maintenance/review_queue.md`.

## Page Types

Supported page types:
- source
- source_summary
- fact
- derived_conclusion
- project
- person
- topic
- decision
- unknown
- history
- maintenance_report
```

### 6.3 SQLite Index

Markdown should be the durable human-readable layer. SQLite should be the query/index layer.

Suggested tables:

```sql
CREATE TABLE sources (
  id TEXT PRIMARY KEY,
  type TEXT,
  title TEXT,
  authority TEXT,
  raw_path TEXT,
  parsed_path TEXT,
  created_at TEXT,
  ingested_at TEXT
);

CREATE TABLE entities (
  id TEXT PRIMARY KEY,
  entity_type TEXT,
  name TEXT,
  path TEXT,
  status TEXT,
  last_reviewed TEXT
);

CREATE TABLE facts (
  id TEXT PRIMARY KEY,
  entity_id TEXT,
  fact_type TEXT,
  value TEXT,
  valid_at TEXT,
  observed_at TEXT,
  source_id TEXT,
  source_path TEXT,
  source_span TEXT,
  confidence TEXT,
  review_status TEXT,
  supersedes TEXT,
  superseded_by TEXT
);

CREATE TABLE conclusions (
  id TEXT PRIMARY KEY,
  entity_id TEXT,
  conclusion_type TEXT,
  value TEXT,
  based_on TEXT,
  confidence TEXT,
  review_status TEXT,
  path TEXT
);

CREATE TABLE decisions (
  id TEXT PRIMARY KEY,
  entity_id TEXT,
  decision_date TEXT,
  status TEXT,
  authority TEXT,
  review_status TEXT,
  path TEXT
);

CREATE TABLE unknowns (
  id TEXT PRIMARY KEY,
  entity_id TEXT,
  status TEXT,
  created_at TEXT,
  resolved_at TEXT,
  path TEXT
);

CREATE TABLE links (
  from_path TEXT,
  to_path TEXT,
  link_type TEXT
);
```

### 6.4 Current State Resolution

Current state should be generated from facts and conclusions using rules.

Example rules:

```yaml
resolution_rules:
  project.status:
    strategy: latest_valid_at_wins
    minimum_confidence: medium
    exclude_review_status:
      - rejected

  person.role_on_project:
    strategy: latest_valid_at_wins
    minimum_confidence: medium

  decision:
    strategy: append_only
    reversal_required: true

  open_issue:
    strategy: open_until_closure_fact

  unknown:
    strategy: open_until_resolved
```

Important: not every fact type should use latest-wins. Scope, requirements, and decisions may need review rather than automatic overwrite.

### 6.5 Maintenance Reports

A maintenance report should be generated after each maintenance run.

Example:

```markdown
---
id: maintenance_report.2026-05-23
type: maintenance_report
generated_at: 2026-05-23T10:00:00+08:00
---

# Maintenance Report - 2026-05-23

## Sources Processed

- 3 meeting transcripts
- 1 FSD document

## Important Changes

- `project.document-intelligence` status changed from `in_progress` to `completed`.
- Unknown created: production rollout status not confirmed.
- Test evidence cleanup remains open.

## Review Required

- Confirm whether "completed" means Drop 1 only or full production readiness.
- Review proposed update to Document Intelligence project page.

## Garbage Collection Findings

- 2 orphan facts
- 1 duplicate topic candidate
- 3 broken links
- 4 facts without reviewed status after 30 days
```

---

## 7. Example Workflow

### 7.1 New Source Arrives

A new meeting transcript is added:

```text
docs/sources/raw/meetings/2026-05-22-doc-intel-update.md
```

### 7.2 Ingest

The system creates metadata and parsed source:

```text
docs/sources/parsed/meetings/2026-05-22-doc-intel-update.md
```

### 7.3 Summarize Source

The system creates:

```text
docs/source_summaries/meetings/2026-05-22-doc-intel-update.md
```

### 7.4 Extract Facts

The system creates facts:

```text
docs/facts/projects/document-intelligence/status/2026-05-22-completed.md
docs/facts/projects/document-intelligence/issues/2026-05-22-test-evidence-open.md
```

### 7.5 Link Entities

The system links facts to:

```text
docs/entities/projects/document-intelligence/index.md
docs/entities/topics/test-evidence/index.md
```

### 7.6 Resolve Current State

The system sees an older fact:

```text
2026-05-01: status = in_progress
```

And a newer fact:

```text
2026-05-22: status = completed
```

Using `latest_valid_at_wins` for `project.status`, it proposes:

```text
Current status = completed as of 2026-05-22
```

The older fact is marked as superseded.

### 7.7 Create Unknown

Because "completed" may mean only Drop 1, the system creates:

```text
docs/unknowns/projects/document-intelligence/production-rollout-status.md
```

### 7.8 Update History

The system updates:

```text
docs/entities/projects/document-intelligence/history.md
```

### 7.9 Add to Review Queue

If the project page is reviewed/trusted, the system should not silently overwrite it. It should create a proposed update in:

```text
docs/maintenance/review_queue.md
```

---

## 8. Testing and Monitoring Strategy

This section is critical. The system should be tested like an information extraction and knowledge maintenance system, not just a summarization system.

The goal of testing is to verify that the KB remains useful, grounded, time-aware, and maintainable as documents accumulate.

### 8.1 Test Dataset

Create a small but adversarial test corpus.

Minimum dataset:

```text
test_corpus/
  sources/
    2026-05-01-project-status.md
    2026-05-08-project-status.md
    2026-05-15-ownership-change.md
    2026-05-22-project-completion.md
    2026-05-23-conflicting-update.md
    BRD_v1.md
    FSD_v1.md
```

The test corpus should include:

1. a project status that changes over time
2. an outdated fact that should be superseded
3. an ambiguous update
4. a contradiction
5. a person/project responsibility change
6. a decision
7. an unknown that later gets resolved
8. a fact with low confidence
9. a source that mentions a person but does not assign responsibility
10. a source that says "done" but only for one phase, not the full project

### 8.2 Golden Expected Outputs

For each test case, define expected outputs.

Example:

```yaml
test_id: project_status_supersession
sources:
  - 2026-05-01-project-status.md
  - 2026-05-22-project-completion.md
expected:
  current_status:
    value: completed
    as_of: 2026-05-22
  superseded_facts:
    - fact.project.document-intelligence.status.2026-05-01
  history_contains:
    - "2026-05-01: in_progress"
    - "2026-05-22: completed"
  unknowns_created:
    - production_rollout_status
```

### 8.3 Evaluation Dimensions

The test LLM or evaluation process should measure the following.

#### 8.3.1 Source Grounding

Question:

> Does every important fact or conclusion link to a source?

Metrics:

- percentage of facts with source ID
- percentage of facts with source span
- percentage of conclusions with supporting facts
- number of unsupported claims on entity pages

Target:

```text
>= 95% of facts have source_id and source_span.
100% of derived conclusions have based_on references.
0 high-confidence conclusions without evidence.
```

#### 8.3.2 Fact/Conclusion Separation

Question:

> Does the system distinguish observed facts from derived conclusions?

Tests:

- source says "Drop 1 complete"
- system must not automatically state "production ready" as an observed fact
- production readiness should be a derived conclusion or unknown

Target:

```text
0 derived conclusions stored as observed facts.
```

#### 8.3.3 Temporal Accuracy

Question:

> Does the system correctly resolve current state from timestamped facts?

Tests:

- old status: in_progress
- new status: completed
- expected current status: completed
- old status should remain in history
- old status should not appear as current

Target:

```text
>= 95% correct current-state resolution for simple latest-wins fact types.
100% preservation of historical facts.
```

#### 8.3.4 Supersession Correctness

Question:

> Are old facts marked as superseded when a newer fact replaces them?

Metrics:

- correct supersedes links
- no deletion of old facts
- history page shows both states

Target:

```text
>= 90% correct supersession links in test cases.
0 deletion of superseded facts.
```

#### 8.3.5 Unknown Detection

Question:

> Does the system create unknowns when evidence is incomplete?

Example:

Source says:

> Drop 1 is complete.

Expected:

- status may become completed for Drop 1
- unknown should be created for production rollout if not explicitly stated

Target:

```text
>= 85% recall for intentionally ambiguous test cases.
```

#### 8.3.6 People Page Safety

Question:

> Are people pages professional and bounded?

Checks:

- no personality speculation
- no sensitive personal details
- no gossip
- only project-relevant responsibilities, roles, decisions, and actions

Target:

```text
0 unsafe or irrelevant people-page claims in test corpus.
```

#### 8.3.7 Decision Record Quality

Question:

> Are important decisions captured as decision records?

Metrics:

- percentage of explicit decisions extracted
- decision date present
- source present
- rationale present where available
- consequences captured where available
- reversal conditions present where inferable or templated

Target:

```text
>= 90% of explicit decisions captured.
100% of decisions have source_id and decision_date.
```

#### 8.3.8 Stable ID Consistency

Question:

> Are IDs stable across reruns?

Test:

- run ingestion twice on the same corpus
- compare generated IDs

Target:

```text
>= 99% ID stability across reruns.
0 duplicate entity IDs for the same project/person/topic.
```

#### 8.3.9 Garbage Collection Effectiveness

Question:

> Does maintenance detect KB rot?

Synthetic issues to inject:

- broken links
- orphan facts
- duplicate topic pages
- facts without source
- stale entity page
- reviewed page contradicted by newer fact

Target:

```text
>= 95% detection of injected broken links.
>= 90% detection of orphan facts.
>= 80% detection of duplicate topics.
>= 90% detection of stale pages.
```

#### 8.3.10 Answer Quality Over KB

Question:

Ask the system questions using only the KB:

- What is the current status of Document Intelligence?
- What changed since 2026-05-01?
- What is unknown about production rollout?
- Which facts support the current status?
- What decisions were made?
- What old belief was superseded?

Expected answer requirements:

- correct current state
- source-backed evidence
- historical context
- no unsupported claims
- explicit unknowns where needed

Target:

```text
>= 90% answer correctness on golden questions.
100% answers cite fact/source links for important claims.
```

### 8.4 Regression Tests

Every change to prompts, schemas, parsing, or maintenance jobs should run regression tests.

Minimum regression test commands:

```bash
kb test --corpus test_corpus/basic_temporal
kb test --corpus test_corpus/conflicts
kb test --corpus test_corpus/people_pages
kb test --corpus test_corpus/decisions
kb test --corpus test_corpus/garbage_collection
```

Expected outputs:

```text
PASS source grounding
PASS fact/conclusion separation
PASS temporal resolution
PASS supersession
PASS unknown detection
PASS people safety
PASS decision extraction
PASS stable IDs
PASS garbage collection
PASS KB answer quality
```

### 8.5 Monitoring Metrics

Track these metrics over time:

```yaml
kb_health:
  total_sources: 0
  total_source_summaries: 0
  total_entities: 0
  total_facts: 0
  facts_without_source: 0
  facts_without_source_span: 0
  unreviewed_high_impact_facts: 0
  stale_entity_pages: 0
  broken_links: 0
  orphan_facts: 0
  duplicate_entity_candidates: 0
  unresolved_unknowns: 0
  unresolved_conflicts: 0
  conclusions_without_supporting_facts: 0
  people_pages_with_policy_flags: 0
```

Create a dashboard page:

```text
docs/maintenance/kb_health.md
```

Example:

```markdown
# KB Health

Generated: 2026-05-23T10:00:00+08:00

| Metric | Value | Threshold | Status |
|---|---:|---:|---|
| Facts without source | 2 | 0 | FAIL |
| Broken links | 1 | 0 | FAIL |
| Stale entity pages | 4 | 5 | PASS |
| Conclusions without facts | 0 | 0 | PASS |
| Unreviewed high-impact facts | 7 | 10 | PASS |
```

### 8.6 Fine-Tuning / Prompt Tuning Loop

The separate LLM run should be allowed to iterate on:

- extraction prompts
- summary templates
- fact schemas
- decision detection prompts
- unknown detection logic
- maintenance prompts
- conflict detection prompts
- reranking logic
- current-state resolution rules

But it should not change the core vision without explicit approval.

The tuning loop should work as follows:

```text
1. Run pipeline on test corpus.
2. Compare generated KB against golden expected outputs.
3. Identify failures.
4. Adjust prompt/schema/rules.
5. Rerun regression tests.
6. Record results.
7. Promote only if metrics improve or failure trade-offs are accepted.
```

Each test run should produce:

```text
test_results/
  run_YYYYMMDD_HHMM/
    metrics.json
    failures.md
    generated_kb_snapshot/
    diff_against_golden.md
    prompt_versions.yaml
```

### 8.7 Human Review Tests

At least some tests should be judged by a human or higher-quality review model.

Human review checklist:

- Is the current page readable?
- Does it avoid stale clutter?
- Does it preserve useful history?
- Are source-backed facts clear?
- Are conclusions clearly separated from facts?
- Are unknowns useful rather than noisy?
- Are people pages professional?
- Would a project manager trust this page?
- Would an LLM be able to navigate from the page to evidence?

---

## 9. Acceptance Criteria

The first usable version is successful when it can:

1. ingest a small set of documents and transcripts
2. create one source summary per source
3. extract atomic timestamped facts
4. create project/person/topic pages
5. create history pages for important entities
6. distinguish observed facts from derived conclusions
7. maintain confidence and review status
8. create unknowns for ambiguous or missing evidence
9. create decision records for important decisions
10. use stable IDs
11. preserve raw sources immutably
12. mark superseded facts without deleting them
13. generate a maintenance report
14. detect basic garbage such as broken links, orphan facts, duplicate topics, and unsupported conclusions
15. answer current-state and history questions with source-backed evidence

Minimum success threshold for prototype:

```text
- 90%+ correct fact extraction on small golden corpus
- 90%+ correct current-state resolution for simple project status tests
- 100% raw source immutability
- 0 observed/derived category violations in golden tests
- 0 people-page safety violations in golden tests
- 95%+ link validity
```

---

## 10. Implementation Guidance for the Next LLM

The next LLM has freedom to design implementation details, but it should preserve the core model:

```text
Sources are immutable evidence.
Facts are timestamped observations.
Conclusions are derived from facts.
Entity pages are current synthesized views.
History pages explain evolution.
Decision pages capture commitments.
Unknowns prevent false certainty.
Maintenance jobs keep the library healthy.
Garbage collection prevents rot.
Stable IDs keep the system automatable.
Markdown keeps it inspectable.
SQLite keeps it queryable.
```

Recommended first implementation path:

```text
1. Build the folder structure and templates.
2. Implement source ingestion and metadata.
3. Implement source summary generation.
4. Implement fact extraction to markdown + SQLite.
5. Implement entity linking.
6. Implement current-state resolution for project.status.
7. Implement history page generation.
8. Implement unknown creation.
9. Implement decision record extraction.
10. Implement garbage collection checks.
11. Build test corpus and golden outputs.
12. Run prompt/rule tuning loop.
```

Do not begin by building a generic chatbot. Build the knowledge lifecycle first.

---

## 11. Deferred or Optional Capabilities

These are useful later but should not distract the initial build.

Optional:

- vector search
- embeddings
- semantic duplicate detection
- web UI
- MkDocs publishing
- advanced permissions
- integration with ticketing systems
- auto-generated PowerPoint/status reports
- BRD/FSD traceability matrix
- formal ReqIF export
- graph database
- real-time collaboration

The first milestone should prove that the knowledge base can correctly ingest, timestamp, resolve, preserve, and maintain evolving project knowledge.

---

## 12. Summary

This system should be treated as an AI-assisted temporal knowledge management layer.

It is not merely:

```text
documents → summaries → chatbot
```

It is:

```text
immutable sources
→ source summaries
→ atomic timestamped facts
→ reviewed conclusions
→ current entity pages
→ history pages
→ decision records
→ unknowns
→ maintenance and garbage collection
```

The end result should help both humans and LLMs reason over evolving knowledge without relying on giant context windows or fragile summarization chains.
