# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""CLI for the temporal KB tier.

Sub-commands:

    synthadoc kb init                Scaffold kb/ folder + kb.db
    synthadoc kb import-source PATH  Import a raw source into kb/sources/raw/
    synthadoc kb backfill            Seed sources from audit.db.ingests

The CLI is intentionally thin — it delegates to :mod:`synthadoc.kb`. New
behaviour belongs in the library, not in this file.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from synthadoc import errors as E
from synthadoc.cli._wiki import resolve_wiki
from synthadoc.cli.install import resolve_wiki_path
from synthadoc.cli.main import app
from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.import_source import import_source as _import_source_impl
from synthadoc.kb.layout import KBLayout

kb_app = typer.Typer(name="kb", help="Temporal KB (fact tier) commands.")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


# Default kb_config.yaml — resolution rules per plan §8.
_DEFAULT_KB_CONFIG = """\
# synthadoc Temporal KB resolution rules — plan §8
#
# Each fact_type names a strategy used by the resolver to derive current state
# from the timestamped facts table. Edit and restart the maintenance jobs;
# the markdown is the durable truth, so changing this file is non-destructive.

resolution_rules:
  project.status:
    strategy: latest_valid_at_wins
    minimum_confidence: medium
    exclude_review_status: [rejected]

  project.owner:
    strategy: latest_valid_at_wins
    minimum_confidence: medium

  project.scope:
    strategy: requires_review

  project.milestone:
    strategy: append_only

  person.role_on_project:
    strategy: latest_valid_at_wins
    minimum_confidence: medium

  topic.definition:
    strategy: latest_valid_at_wins
    minimum_confidence: medium

  requirement.coverage:
    strategy: requires_review

  decision.made:
    strategy: append_only
    reversal_required: true

  open_issue.created:
    strategy: open_until_closure_fact

  open_issue.closed:
    strategy: append_only

  assumption.created:
    strategy: open_until_resolved

  assumption.invalidated:
    strategy: append_only
"""


# Default kb_aliases.yaml — version-controllable entity merge map. Empty by
# default; populated by `synthadoc kb review merge` / `kb review alias`, or by
# hand. Applied by the EntityLinker during import so duplicates never form.
_DEFAULT_KB_ALIASES = """\
# Entity aliases for the temporal KB. Declares that several names are the SAME
# entity; applied during import so duplicates never form. Commit this file with
# your wiki — it survives a clean re-import (unlike merges stored in kb.db).
#
# Canonical display name is the key; aliases may be names or slugs (both are
# slugified on load). Example:
#
# aliases:
#   project:
#     AURA:
#       - AURA Program
#       - AURA EGP Program
#   person:
#     Harsh:
#       - hash
#
# Managed by `synthadoc kb review merge` and `synthadoc kb review alias`.

aliases: {}
"""


# Stub bodies for the maintenance pages — created empty so they show up in
# the wiki immediately even before any job has run.
_MAINTENANCE_STUBS = {
    "review_queue.md": "# Review Queue\n\nProposed updates to reviewed entity pages will be appended here.\n",
    "stale_pages.md": "# Stale Entity Pages\n\n_No entries yet — run `synthadoc kb maintenance run`._\n",
    "conflicts.md": "# Conflicts\n\n_No entries yet — run `synthadoc kb maintenance run`._\n",
    "orphan_facts.md": "# Orphan Facts\n\n_No entries yet — run `synthadoc kb maintenance run`._\n",
    "duplicate_entities.md": "# Duplicate Entity Candidates\n\n_No entries yet — run `synthadoc kb maintenance run`._\n",
    "broken_links.md": "# Broken Links\n\n_No entries yet — run `synthadoc kb maintenance run`._\n",
    "kb_health.md": "# KB Health\n\n_No entries yet — run `synthadoc kb maintenance run`._\n",
}


def _resolve_wiki_root(wiki: Optional[str]) -> Path:
    """Look up the wiki root path or exit with a friendly error."""
    name = resolve_wiki(wiki)
    root = resolve_wiki_path(name)
    if not root.exists():
        E.cli_error(
            E.WIKI_NOT_FOUND,
            f"Wiki directory not found: {root}",
            "Check the wiki name or path.",
        )
    cfg = root / ".synthadoc" / "config.toml"
    if not cfg.exists():
        E.cli_error(
            E.WIKI_INVALID,
            f"No .synthadoc/config.toml at {root}",
            "Is this a valid synthadoc wiki directory?",
        )
    return root


# ---------------------------------------------------------------------------
# kb init
# ---------------------------------------------------------------------------


@kb_app.command("init")
def init_cmd(
    wiki: Optional[str] = typer.Option(None, "--wiki", "-w", help="Wiki name or path"),
):
    """Scaffold the kb/ folder and create kb.db for the temporal tier.

    Idempotent — safe to run on a wiki that has been partially initialised.
    Never touches files under wiki/.
    """
    root = _resolve_wiki_root(wiki)
    layout = KBLayout(root)
    layout.ensure_layout()

    # Maintenance stubs
    for name, body in _MAINTENANCE_STUBS.items():
        target = layout.maintenance_dir / name
        if not target.exists():
            target.write_text(body, encoding="utf-8", newline="\n")

    # kb_config.yaml — only write if missing so user edits survive re-init
    if not layout.config_path.exists():
        layout.config_path.write_text(_DEFAULT_KB_CONFIG, encoding="utf-8", newline="\n")

    # kb_aliases.yaml — version-controllable entity merge map (see _DEFAULT_KB_ALIASES)
    if not layout.aliases_path.exists():
        layout.aliases_path.write_text(_DEFAULT_KB_ALIASES, encoding="utf-8", newline="\n")

    # Create kb.db
    async def _init_db():
        db = KBDB(layout.db_path)
        await db.init()

    asyncio.run(_init_db())

    typer.echo("KB tier initialised.")
    typer.echo(f"  kb/          {layout.kb}")
    typer.echo(f"  kb.db        {layout.db_path}")
    typer.echo(f"  config       {layout.config_path}")


# ---------------------------------------------------------------------------
# kb import-source
# ---------------------------------------------------------------------------


@kb_app.command("import-source")
def import_source_cmd(
    path: str = typer.Argument(..., help="Path to the source file"),
    source_type: str = typer.Option(
        ..., "--type", "-t",
        help="One of: meeting_transcript, document, email, deck, note",
    ),
    title: Optional[str] = typer.Option(
        None, "--title",
        help="Human-readable title (default: filename stem)",
    ),
    valid_on: Optional[str] = typer.Option(
        None, "--date",
        help="Source date YYYY-MM-DD (default: parsed from filename, else today UTC)",
    ),
    authority: str = typer.Option(
        "informal", "--authority",
        help="formal | informal | unknown",
    ),
    author: Optional[str] = typer.Option(None, "--author"),
    wiki: Optional[str] = typer.Option(None, "--wiki", "-w"),
):
    """Import a raw source file into kb/sources/raw/ and record it in kb.db.

    Re-importing the same file (same SHA-256) is a no-op — the existing
    source id is printed so callers can chain. Files are copied, not moved.
    """
    if source_type not in ids.SOURCE_TYPES:
        E.cli_error(
            E.INGEST_NOT_FOUND,
            f"Unknown --type {source_type!r}.",
            f"Valid types: {', '.join(sorted(ids.SOURCE_TYPES))}",
        )
    src = Path(path)
    if not src.exists() or not src.is_file():
        E.cli_error(E.INGEST_NOT_FOUND, f"Source file not found: {src}")
    if src.stat().st_size == 0:
        E.cli_error(E.INGEST_EMPTY, f"Source file is empty: {src}")

    root = _resolve_wiki_root(wiki)
    layout = KBLayout(root)
    layout.ensure_layout()

    async def _run() -> tuple[str, bool]:
        db = KBDB(layout.db_path)
        await db.init()
        try:
            return await _import_source_impl(
                src_path=src, source_type=source_type, db=db, layout=layout,
                title=title, valid_on=valid_on, authority=authority, author=author,
            )
        except sqlite3.IntegrityError as exc:
            raise typer.Exit(code=1) from exc

    src_id, was_existing = asyncio.run(_run())
    if was_existing:
        typer.echo(f"Already imported as {src_id} (sha256 match).")
    else:
        typer.echo(f"Imported: {src_id}")


# ---------------------------------------------------------------------------
# kb backfill — seed sources from existing audit.db.ingests
# ---------------------------------------------------------------------------


@kb_app.command("backfill")
def backfill_cmd(
    wiki: Optional[str] = typer.Option(None, "--wiki", "-w"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be done; write nothing"),
):
    """Seed kb.db from audit.db.ingests.

    Walks the existing ingest audit table and creates one ``sources`` row per
    record. Files that still exist on disk are linked via ``raw_path``;
    missing files are recorded with ``raw_path = NULL`` and
    ``authority = unknown`` so the gap is visible.

    Idempotent — re-running skips sha256 values already present.
    """
    from synthadoc.storage.log import AuditDB
    root = _resolve_wiki_root(wiki)
    layout = KBLayout(root)
    layout.ensure_layout()

    async def _run() -> tuple[int, int, int]:
        import aiosqlite
        audit_path = root / ".synthadoc" / "audit.db"
        # init() ensures the table exists even on wikis that have never ingested
        await AuditDB(audit_path).init()
        # Read the full row (source_hash is not in the public list_ingests projection)
        async with aiosqlite.connect(audit_path) as adb:
            adb.row_factory = aiosqlite.Row
            async with adb.execute(
                "SELECT source_hash, source_size, source_path, wiki_page, "
                "tokens, cost_usd, ingested_at FROM ingests ORDER BY id ASC"
            ) as cur:
                rows = await cur.fetchall()
        records = [dict(r) for r in rows]

        db = KBDB(layout.db_path)
        await db.init()

        seeded = 0
        skipped_dup = 0
        skipped_missing_hash = 0

        for r in records:
            sha = r.get("source_hash")
            if not sha:
                skipped_missing_hash += 1
                continue
            existing = await db.find_source_by_sha256(sha)
            if existing is not None:
                skipped_dup += 1
                continue

            source_path = Path(r.get("source_path") or "")
            file_present = source_path.exists() and source_path.is_file()
            ingested_at = r.get("ingested_at") or datetime.now(timezone.utc).isoformat()
            iso_date = (ingested_at[:10] if len(ingested_at) >= 10
                        else datetime.now(timezone.utc).date().isoformat())

            # Best-effort source_type from extension; default to "document"
            ext = source_path.suffix.lower()
            source_type = {
                ".md": "document", ".txt": "document",
                ".pdf": "document", ".docx": "document",
                ".pptx": "deck", ".xlsx": "document", ".csv": "document",
            }.get(ext, "document")

            slug_seed = source_path.stem or sha[:8]
            base_slug = ids.slugify(slug_seed) if slug_seed else f"x-{sha[:8]}"
            base_id = ids.source_id(source_type, iso_date, base_slug)
            # Resolve any collision among already-backfilled IDs
            n = 1
            candidate = base_id
            while await db.source_exists(candidate):
                n += 1
                candidate = f"{base_id}-{n}"
            src_id = candidate

            if dry_run:
                typer.echo(f"would seed: {src_id}  (file={'present' if file_present else 'missing'})")
                seeded += 1
                continue

            await db.insert_source(
                id=src_id,
                source_type=source_type,
                title=source_path.stem or src_id,
                authority="informal" if file_present else "unknown",
                raw_path=str(source_path).replace("\\", "/") if file_present else None,
                parsed_path=None,
                created_at=ingested_at,
                ingested_at=ingested_at,
                sha256=sha,
                author=None,
            )
            seeded += 1

        return seeded, skipped_dup, skipped_missing_hash

    seeded, skipped_dup, skipped_missing = asyncio.run(_run())
    label = "would seed" if dry_run else "seeded"
    typer.echo(f"Backfill complete: {seeded} {label}, {skipped_dup} already present, "
               f"{skipped_missing} skipped (no source_hash).")


# ---------------------------------------------------------------------------
# kb relink — one-shot full rescan of the links table
# ---------------------------------------------------------------------------


@kb_app.command("relink")
def relink_cmd(
    wiki: Optional[str] = typer.Option(None, "--wiki", "-w"),
):
    """Rebuild the links table from on-disk markdown.

    The render agents emit links on each write, but manual edits or
    `git checkout` can leave the table stale. Re-run after any bulk edit.
    """
    root = _resolve_wiki_root(wiki)
    layout = KBLayout(root)
    if not layout.db_path.exists():
        E.cli_error(
            E.WIKI_INVALID,
            "kb.db not found.",
            "Run `synthadoc kb init` first.",
        )

    async def _run() -> dict:
        from synthadoc.kb.links import relink_all
        db = KBDB(layout.db_path)
        await db.init()
        return await relink_all(db, layout)

    counts = asyncio.run(_run())
    typer.echo(
        f"Relink complete: {counts['links_emitted']} link(s) from "
        f"{counts['files']} file(s) ({counts['skipped']} skipped)."
    )


# ---------------------------------------------------------------------------
# kb maintenance
# ---------------------------------------------------------------------------


maintenance_app = typer.Typer(name="maintenance",
                              help="Layer 3 maintenance jobs (conflicts, stale, evidence, history).")
kb_app.add_typer(maintenance_app)


@maintenance_app.command("run")
def maintenance_run_cmd(
    wiki: Optional[str] = typer.Option(None, "--wiki", "-w"),
    skip_histories: bool = typer.Option(
        False, "--skip-histories",
        help="Skip per-entity history.md re-render (faster)",
    ),
):
    """Run every maintenance job and emit kb_health.md + a timestamped report."""
    root = _resolve_wiki_root(wiki)
    layout = KBLayout(root)
    if not layout.db_path.exists():
        E.cli_error(
            E.WIKI_INVALID,
            "kb.db not found.",
            "Run `synthadoc kb init` first.",
        )

    async def _run():
        from synthadoc.kb.maintenance.report import run_all
        db = KBDB(layout.db_path)
        await db.init()
        return await run_all(db, layout, render_histories=not skip_histories)

    report = asyncio.run(_run())

    typer.echo("Maintenance complete.")
    for key, value in sorted(report.counts.items()):
        typer.echo(f"  {key:<32} {value}")
    typer.echo(f"  histories_rendered               {report.histories_rendered}")
    typer.echo(f"  health:   {report.health_path}")
    typer.echo(f"  report:   {report.report_path}")
    if report.errors:
        typer.echo("\nErrors:", err=True)
        for err in report.errors:
            typer.echo(f"  - {err}", err=True)


# ---------------------------------------------------------------------------
# kb review — human triage of the review queue
# ---------------------------------------------------------------------------


review_app = typer.Typer(
    name="review",
    help="Triage the KB review queue: list pending items, reject facts, "
         "merge duplicate entities, lock reviewed pages.",
)
kb_app.add_typer(review_app)


def _review_ctx(wiki: Optional[str]):
    """Resolve (layout, rules) for a review command, or exit with a hint."""
    from synthadoc.kb import rules as kb_rules
    root = _resolve_wiki_root(wiki)
    layout = KBLayout(root)
    if not layout.db_path.exists():
        E.cli_error(
            E.WIKI_INVALID, "kb.db not found.", "Run `synthadoc kb init` first.",
        )
    return layout, kb_rules.load(layout.config_path)


def _echo_result(result) -> None:
    typer.echo(f"{result.action}: {result.target_id}")
    if result.detail:
        typer.echo(f"  {result.detail}")
    if result.entity_rendered:
        verb = ("queued a proposal for" if result.queued_for_review
                else "re-rendered")
        typer.echo(f"  {verb} entity {result.entity_rendered}")
    for w in result.warnings:
        typer.echo(f"  ! {w}", err=True)


@review_app.command("list")
def review_list_cmd(
    wiki: Optional[str] = typer.Option(None, "--wiki", "-w"),
):
    """Show everything awaiting a human decision: conflicts, duplicate
    candidates, unreviewed counts, and any queued proposals."""
    from synthadoc.kb import review as kb_review
    layout, _ = _review_ctx(wiki)

    async def _run():
        db = KBDB(layout.db_path)
        await db.init()
        return await kb_review.gather_pending(db, layout)

    p = asyncio.run(_run())

    typer.echo("Unreviewed:")
    typer.echo(f"  facts      {p.facts_unreviewed}")
    typer.echo(f"  decisions  {p.decisions_unreviewed}")
    typer.echo(f"  entities   {p.entities_unreviewed}")

    typer.echo(f"\nConflicts ({len(p.conflicts)}) - resolve with `kb review reject <fact-id>`:")
    for c in p.conflicts:
        typer.echo(f"  - {c.entity_id} | {c.fact_type} @ {c.valid_at}")
        typer.echo(f"      values: {', '.join(repr(v) for v in c.values)}")
        for fid in c.fact_ids:
            typer.echo(f"        {fid}")

    typer.echo(f"\nDuplicate candidates ({len(p.duplicates)}) - merge with "
               f"`kb review merge <dup-id> --into <keeper-id>`:")
    for d in p.duplicates:
        typer.echo(f"  - [{d.entity_type}] {d.a_id}  <->  {d.b_id}  ({d.reason})")

    if p.review_queue_has_entries:
        typer.echo(f"\nQueued proposals against reviewed pages: {p.review_queue_path}")
    typer.echo("\nFull reports under kb/maintenance/.")


@review_app.command("reject")
def review_reject_cmd(
    fact_id: str = typer.Argument(..., help="Fact id to reject"),
    wiki: Optional[str] = typer.Option(None, "--wiki", "-w"),
):
    """Reject a fact so the resolver ignores it everywhere, then re-render
    its entity. Use this to resolve a conflict by dropping the wrong value."""
    from synthadoc.kb import review as kb_review
    layout, rules = _review_ctx(wiki)

    async def _run():
        db = KBDB(layout.db_path)
        await db.init()
        return await kb_review.reject_fact(db, layout, rules, fact_id)

    try:
        _echo_result(asyncio.run(_run()))
    except KeyError as exc:
        E.cli_error(E.WIKI_INVALID, str(exc), "Check the fact id with `kb review list`.")


@review_app.command("accept-fact")
def review_accept_fact_cmd(
    fact_id: str = typer.Argument(..., help="Fact id to confirm"),
    wiki: Optional[str] = typer.Option(None, "--wiki", "-w"),
):
    """Mark a single fact reviewed (confirms it; does not lock the page)."""
    from synthadoc.kb import review as kb_review
    layout, rules = _review_ctx(wiki)

    async def _run():
        db = KBDB(layout.db_path)
        await db.init()
        return await kb_review.accept_fact(db, layout, rules, fact_id)

    try:
        _echo_result(asyncio.run(_run()))
    except KeyError as exc:
        E.cli_error(E.WIKI_INVALID, str(exc), "Check the fact id with `kb review list`.")


@review_app.command("accept")
def review_accept_cmd(
    entity_id: str = typer.Argument(..., help="Entity id to lock as reviewed"),
    wiki: Optional[str] = typer.Option(None, "--wiki", "-w"),
):
    """Lock an entity's current state as reviewed. Future contradicting facts
    are then queued in kb/maintenance/review_queue.md instead of auto-applied."""
    from synthadoc.kb import review as kb_review
    layout, rules = _review_ctx(wiki)

    async def _run():
        db = KBDB(layout.db_path)
        await db.init()
        return await kb_review.set_entity_reviewed(db, layout, rules, entity_id, reviewed=True)

    try:
        _echo_result(asyncio.run(_run()))
    except KeyError as exc:
        E.cli_error(E.WIKI_INVALID, str(exc), "Check the entity id with `kb review list`.")


@review_app.command("reopen")
def review_reopen_cmd(
    entity_id: str = typer.Argument(..., help="Entity id to unlock"),
    wiki: Optional[str] = typer.Option(None, "--wiki", "-w"),
):
    """Unlock a reviewed entity so its page is auto-rendered again."""
    from synthadoc.kb import review as kb_review
    layout, rules = _review_ctx(wiki)

    async def _run():
        db = KBDB(layout.db_path)
        await db.init()
        return await kb_review.set_entity_reviewed(db, layout, rules, entity_id, reviewed=False)

    try:
        _echo_result(asyncio.run(_run()))
    except KeyError as exc:
        E.cli_error(E.WIKI_INVALID, str(exc), "Check the entity id with `kb review list`.")


@review_app.command("alias")
def review_alias_cmd(
    alias: str = typer.Argument(..., help="Variant name to fold in, e.g. 'AURA Program'"),
    into: str = typer.Option(..., "--into", help="Canonical name, e.g. 'AURA'"),
    entity_type: str = typer.Option(..., "--type", "-t",
        help="Entity type: project | person | topic | requirement"),
    wiki: Optional[str] = typer.Option(None, "--wiki", "-w"),
):
    """Declare that two names are the same entity, in kb_aliases.yaml.

    Unlike `merge`, this needs no existing entities — use it to pre-seed
    aliases before a fresh import so duplicates never form. The file is
    version-controllable (lives at the wiki root, beside kb_config.yaml)."""
    from synthadoc.kb import aliases as kb_aliases
    root = _resolve_wiki_root(wiki)
    layout = KBLayout(root)
    try:
        kb_aliases.add_alias(
            layout.aliases_path, entity_type=entity_type,
            canonical_name=into, alias_name=alias,
        )
    except kb_aliases.AliasError as exc:
        E.cli_error(E.WIKI_INVALID, str(exc),
                    "Valid types: project, person, topic, requirement.")
    typer.echo(f"alias: '{alias}' -> '{into}' [{entity_type}]")
    typer.echo(f"  recorded in {layout.aliases_path}")
    typer.echo("  applied on the next import (restart `synthadoc serve` to pick it up).")


@review_app.command("merge")
def review_merge_cmd(
    dup_id: str = typer.Argument(..., help="Duplicate entity id to merge away"),
    into_id: str = typer.Option(..., "--into", help="Keeper entity id"),
    wiki: Optional[str] = typer.Option(None, "--wiki", "-w"),
):
    """Merge a duplicate entity into a keeper: its facts/decisions/unknowns
    move to the keeper, the keeper page is re-rendered, and the duplicate
    becomes a tombstone."""
    from synthadoc.kb import review as kb_review
    layout, rules = _review_ctx(wiki)

    async def _run():
        db = KBDB(layout.db_path)
        await db.init()
        return await kb_review.merge_entity(db, layout, rules, dup_id, into_id)

    try:
        _echo_result(asyncio.run(_run()))
    except (KeyError, ValueError) as exc:
        E.cli_error(E.WIKI_INVALID, str(exc), "Check ids with `kb review list`.")


# Attach to the root app — done at import time by main.py
app.add_typer(kb_app)
