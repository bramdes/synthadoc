# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
from __future__ import annotations

from typing import Optional

import typer

from synthadoc.cli.main import app
from synthadoc.cli._http import post


@app.command("consolidate")
def consolidate_cmd(
    slug: str = typer.Argument(..., help="Wiki page slug to consolidate"),
    wiki: Optional[str] = typer.Option(None, "--wiki", "-w"),
    force: bool = typer.Option(False, "--force",
        help="Re-consolidate even if the page hasn't changed since last run."),
):
    """Rewrite an accumulated wiki page into a curated, deduplicated form.

    Use this when a project/person/topic page has grown by repeated ingest
    appends and needs reorganising. Provenance footers (`_— Source: …_`) and
    [[wikilinks]] are preserved verbatim. Requires `synthadoc serve`.

    Re-running on an unchanged page is a no-op — the page's frontmatter records
    the content hash at last consolidation, so we skip when the body matches.
    Pass --force to override.
    """
    from synthadoc.cli._wiki import resolve_wiki
    wiki = resolve_wiki(wiki)
    result = post(wiki, "/consolidate", {"slug": slug, "force": force})
    if result.get("skipped"):
        typer.echo(f"Skipped {result['slug']}: {result['skip_reason']}")
        raise typer.Exit(0)
    before = result["before_chars"]
    after = result["after_chars"]
    delta = after - before
    sign = "+" if delta >= 0 else ""
    typer.echo(
        f"Consolidated {result['slug']}: {before} → {after} chars "
        f"({sign}{delta}, {result['tokens_used']} tokens, "
        f"${result['cost_usd']:.4f})"
    )
