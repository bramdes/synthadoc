# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Deterministic, LLM-free page-tier merge + duplicate detection.

``merge_pages`` folds a duplicate wiki page into a keeper: appends its body
(provenance footers intact), rewrites every ``[[dup]]`` wikilink to the keeper,
fixes the index, deletes the duplicate, and records the decision in
``wiki_aliases.yaml`` so future ingests auto-canonicalize the variant. None of
it calls an LLM — it runs identically on an offline laptop.

``find_duplicate_candidates`` is a heuristic (string-similarity) scan that
surfaces *likely* duplicate slugs for a human to confirm; it never merges
anything on its own.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from synthadoc.storage import wiki_aliases
from synthadoc.storage.wiki import WikiStorage, sanitize_tags

# Pages that are structural / auto-generated, never merge targets or candidates.
_SKIP_SLUGS = frozenset({"index", "log", "dashboard", "overview", "purpose"})


@dataclass
class WikiMergeResult:
    dup: str
    keeper: str
    links_rewritten_files: int = 0
    index_lines_removed: int = 0
    sources_in_keeper: int = 0
    alias_recorded: bool = False
    warnings: list[str] = field(default_factory=list)


def _wikilink_pattern(slug: str) -> re.Pattern:
    """Match ``[[slug]]`` or ``[[slug|alias]]`` only — never a longer slug or a
    source-filename link, because the slug is anchored right after ``[[`` and
    must be immediately followed by ``]]`` or ``|``."""
    return re.compile(r"\[\[" + re.escape(slug) + r"(\]\]|\|)")


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        m = re.search(r"\n---[ \t]*\n", text[3:])
        if m:
            return text[3 + m.end():]
    return text


def merge_pages(
    store: WikiStorage,
    wiki_root: Path,
    dup_slug: str,
    keeper_slug: str,
    *,
    record_alias: bool = True,
) -> WikiMergeResult:
    """Merge *dup_slug* into *keeper_slug*. Deterministic, no LLM.

    Raises ValueError if the slugs are equal, the keeper is missing, the dup is
    missing, or either is a structural page.
    """
    dup_slug = dup_slug.strip()
    keeper_slug = keeper_slug.strip()
    if dup_slug == keeper_slug:
        raise ValueError("dup and keeper are the same slug")
    if dup_slug in _SKIP_SLUGS or keeper_slug in _SKIP_SLUGS:
        raise ValueError(f"refusing to merge a structural page ({_SKIP_SLUGS & {dup_slug, keeper_slug}})")

    keeper_page = store.read_page(keeper_slug)
    if keeper_page is None:
        raise ValueError(f"keeper page not found: {keeper_slug!r}")
    dup_page = store.read_page(dup_slug)
    if dup_page is None:
        raise ValueError(f"duplicate page not found: {dup_slug!r}")

    result = WikiMergeResult(dup=dup_slug, keeper=keeper_slug)

    # 1. Append the dup body to the keeper (provenance footers ride along),
    #    union the tags. A marker documents the merge in the source.
    with store.page_lock(keeper_slug):
        keeper_page = store.read_page(keeper_slug)
        keeper_page.content = (
            keeper_page.content.rstrip()
            + f"\n\n<!-- merged from {dup_slug} -->\n\n"
            + dup_page.content.strip() + "\n"
        )
        keeper_page.tags = sanitize_tags(list(keeper_page.tags) + list(dup_page.tags))
        store.write_page(keeper_slug, keeper_page)

    # 2. Delete the duplicate file.
    dup_path = store._find_existing_path(dup_slug)
    if dup_path is not None:
        dup_path.unlink()

    # 3. Rewrite [[dup]] -> [[keeper]] in every page EXCEPT the index (handled
    #    separately so its labels never get mangled).
    pat = _wikilink_pattern(dup_slug)
    repl = f"[[{keeper_slug}" + r"\1"
    for slug, path in store.iter_page_paths():
        if slug == "index":
            continue
        text = path.read_text(encoding="utf-8")
        new = pat.sub(repl, text)
        if new != text:
            path.write_text(new, encoding="utf-8", newline="\n")
            result.links_rewritten_files += 1

    # 4. Index: drop any line that links to the dup; ensure the keeper is listed.
    index_path = Path(store._root) / "index.md"
    if index_path.exists():
        lines = index_path.read_text(encoding="utf-8").splitlines()
        keep_line = re.compile(r"^\s*[-*]\s*\[\[" + re.escape(dup_slug) + r"(\]\]|\|)")
        kept = [ln for ln in lines if not keep_line.match(ln)]
        result.index_lines_removed = len(lines) - len(kept)
        if kept != lines:
            index_path.write_text("\n".join(kept) + "\n", encoding="utf-8", newline="\n")
        # Make sure the keeper still appears somewhere in the index.
        body = "\n".join(kept)
        if f"[[{keeper_slug}]]" not in body and f"[[{keeper_slug}|" not in body:
            store.append_to_index(keeper_slug, keeper_page.title or keeper_slug)

    result.sources_in_keeper = (store.read_page(keeper_slug).content.count("_— Source:"))

    # 5. Record the durable alias so future ingests auto-canonicalize.
    if record_alias:
        wiki_aliases.add_alias(
            Path(wiki_root) / "wiki_aliases.yaml",
            canonical_slug=keeper_slug, alias_slug=dup_slug,
        )
        result.alias_recorded = True

    return result


# ---------------------------------------------------------------------------
# Heuristic duplicate detection (no LLM)
# ---------------------------------------------------------------------------

@dataclass
class DuplicateCandidate:
    a: str
    b: str
    score: float
    reason: str


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


# Suffixes that mark a *variant* of the same page rather than a distinct
# sub-topic. Short tokens (≤2 chars: "d", "b", "v2", "2") plus a small set of
# explicit markers. "egp-tom"/"aura-codification" have long, topical suffixes
# and are therefore NOT treated as duplicates.
_VARIANT_MARKERS = frozenset({"old", "new", "copy", "draft", "wip", "tmp", "bak", "v2", "v3"})


def _variant_suffix(a: str, b: str) -> Optional[str]:
    """If one slug is the other plus a dash-bounded variant marker, return that
    marker suffix; else None. E.g. ('gc-tst-d','gc-tst') -> 'd'."""
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    if not longer.startswith(shorter + "-"):
        return None
    suffix = longer[len(shorter) + 1:]
    if len(suffix) <= 2 or suffix in _VARIANT_MARKERS:
        return suffix
    return None


def find_duplicate_candidates(
    store: WikiStorage,
    *,
    aliases: Optional[wiki_aliases.WikiAliases] = None,
    slug_threshold: float = 0.72,
    title_threshold: float = 0.86,
) -> list[DuplicateCandidate]:
    """Return likely-duplicate slug pairs for human review. Never merges.

    Flags a pair when their slugs are highly similar, their titles are highly
    similar, or one slug is a dash-bounded extension of the other (``gc-tst`` ⊂
    ``gc-tst-d``). Pairs already declared in wiki_aliases.yaml are skipped.
    """
    pages = [(slug, (store.read_page(slug) or None))
             for slug, _ in store.iter_page_paths()
             if slug not in _SKIP_SLUGS]
    titles = {slug: (pg.title if pg else "") for slug, pg in pages}
    slugs = [slug for slug, _ in pages]

    aliased = aliases or wiki_aliases.WikiAliases()

    def already_linked(x: str, y: str) -> bool:
        cx, cy = aliased.canonical(x), aliased.canonical(y)
        return cx == cy or cx == y or cy == x

    out: list[DuplicateCandidate] = []
    for i in range(len(slugs)):
        for j in range(i + 1, len(slugs)):
            a, b = slugs[i], slugs[j]
            if already_linked(a, b):
                continue
            slug_ratio = difflib.SequenceMatcher(None, a, b).ratio()
            ta, tb = _norm_title(titles[a]), _norm_title(titles[b])
            title_ratio = difflib.SequenceMatcher(None, ta, tb).ratio() if ta and tb else 0.0
            # Dash-bounded *variant* marker: one slug is the other + a short
            # marker suffix (gc-tst-d, foo-v2, bar-old). A long suffix means a
            # genuine sub-topic (egp-tom, aura-codification) — not a duplicate.
            ext_suffix = _variant_suffix(a, b)

            if ext_suffix is not None:
                out.append(DuplicateCandidate(a, b, max(slug_ratio, 0.9),
                                              f"sub-variant marker '-{ext_suffix}'"))
            elif slug_ratio >= slug_threshold:
                out.append(DuplicateCandidate(a, b, slug_ratio, "similar slug"))
            elif title_ratio >= title_threshold:
                out.append(DuplicateCandidate(a, b, title_ratio, "similar title"))

    out.sort(key=lambda c: c.score, reverse=True)
    return out
