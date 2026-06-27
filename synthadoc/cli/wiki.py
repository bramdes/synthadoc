# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""CLI for page-tier (wiki/) maintenance — deterministic, LLM-free.

    synthadoc wiki merge <dup> --into <keeper>   Fold a duplicate page into a keeper
    synthadoc wiki dedup                          Report likely duplicate pages

Both run entirely on the local filesystem — no `synthadoc serve`, no LLM — so
they work on an offline laptop. ``merge`` records the decision in
``wiki_aliases.yaml`` so future ingests auto-canonicalize the variant.
"""

from __future__ import annotations

from typing import Optional

import typer

from synthadoc import errors as E
from synthadoc.cli.kb import _resolve_wiki_root
from synthadoc.cli.main import app
from synthadoc.storage import wiki_aliases as _wiki_aliases
from synthadoc.storage import wiki_merge as _wiki_merge
from synthadoc.storage.wiki import WikiStorage

wiki_app = typer.Typer(
    name="wiki",
    help="Page-tier (wiki/) maintenance: merge duplicate pages, find duplicates.",
)
app.add_typer(wiki_app)


@wiki_app.command("merge")
def wiki_merge_cmd(
    dup: str = typer.Argument(..., help="Duplicate page slug to merge away"),
    into: str = typer.Option(..., "--into", help="Keeper page slug"),
    wiki: Optional[str] = typer.Option(None, "--wiki", "-w"),
    no_alias: bool = typer.Option(
        False, "--no-alias",
        help="Merge this once but do NOT record a durable alias rule."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would change without writing."),
):
    """Fold duplicate page *dup* into keeper *--into*: append its body, rewrite
    every [[dup]] wikilink to the keeper, fix the index, delete the duplicate,
    and record the alias in wiki_aliases.yaml so future ingests fold it
    automatically. Deterministic — no LLM, no server.
    """
    root = _resolve_wiki_root(wiki)
    store = WikiStorage(root / "wiki")

    if dry_run:
        keeper_pg = store.read_page(into)
        dup_pg = store.read_page(dup)
        if keeper_pg is None:
            E.cli_error(E.WIKI_INVALID, f"Keeper page not found: {into!r}",
                        "Check the slug with `synthadoc wiki dedup` or in Obsidian.")
        if dup_pg is None:
            E.cli_error(E.WIKI_INVALID, f"Duplicate page not found: {dup!r}",
                        "Check the slug.")
        typer.echo(f"[dry run] would merge '{dup}' → '{into}':")
        typer.echo(f"  append {len(dup_pg.content)} chars "
                   f"({dup_pg.content.count('_— Source:')} source citations)")
        typer.echo(f"  rewrite [[{dup}]] links across the wiki, delete {dup}.md")
        if not no_alias:
            typer.echo(f"  record alias  {dup} → {into}  in wiki_aliases.yaml")
        raise typer.Exit(0)

    try:
        result = _wiki_merge.merge_pages(
            store, root, dup, into, record_alias=not no_alias)
    except ValueError as exc:
        E.cli_error(E.WIKI_INVALID, str(exc),
                    "Check the slugs with `synthadoc wiki dedup`.")

    typer.echo(f"Merged '{result.dup}' → '{result.keeper}'")
    typer.echo(f"  links rewritten in {result.links_rewritten_files} page(s)")
    typer.echo(f"  index lines removed: {result.index_lines_removed}")
    typer.echo(f"  keeper now cites {result.sources_in_keeper} source(s)")
    if result.alias_recorded:
        typer.echo(f"  recorded alias  {result.dup} → {result.keeper}  "
                   f"(future ingests auto-fold)")
    for w in result.warnings:
        typer.echo(f"  ! {w}", err=True)


@wiki_app.command("dedup")
def wiki_dedup_cmd(
    wiki: Optional[str] = typer.Option(None, "--wiki", "-w"),
    threshold: float = typer.Option(
        0.72, "--threshold",
        help="Slug-similarity threshold (0–1); lower surfaces more candidates."),
    limit: int = typer.Option(40, "--limit", help="Max candidate pairs to show."),
):
    """Report likely-duplicate pages (string similarity — no LLM) with a ready
    `wiki merge` command for each. Confirms nothing automatically; you decide
    which to merge.
    """
    root = _resolve_wiki_root(wiki)
    store = WikiStorage(root / "wiki")
    aliases = _wiki_aliases.load(root / "wiki_aliases.yaml")

    candidates = _wiki_merge.find_duplicate_candidates(
        store, aliases=aliases, slug_threshold=threshold)

    if not candidates:
        typer.echo("No likely-duplicate pages found.")
        raise typer.Exit(0)

    shown = candidates[:limit]
    typer.echo(f"Likely duplicate pages ({len(candidates)} pair(s); showing {len(shown)}).")
    typer.echo("Review each — pick the better-developed page as the keeper:\n")
    for c in shown:
        typer.echo(f"  {c.a}  <->  {c.b}   ({c.score:.0%}, {c.reason})")
        typer.echo(f"      synthadoc wiki merge {c.a} --into {c.b}   "
                   f"# or swap dup/keeper")
    if len(candidates) > len(shown):
        typer.echo(f"\n… {len(candidates) - len(shown)} more (raise --limit to see them).")
