# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""BM25 retrieval over the fact-tier *source* layer (verbatim parsed text).

`HybridSearch` covers the page tier — the consolidated wiki topic/entity pages.
Those pages are great for "current state" and relationships but lossy for dense
numeric/temporal detail (a figure quoted on a specific date, who said it). The
verbatim text survives in the fact tier: the `sources` table points at parsed
files on disk (`parsed_path`) that hold the full meeting transcript/optim text.

`SourceSearch` indexes those parsed files, chunked, so a query can pull the
relevant paragraph from each of several meetings. It is deliberately BM25-only
(the BM25-over-raw baseline it mirrors already beats page-tier retrieval on
"over time" questions; no embedding cost is needed to reach parity). See
docs/plans/source-retrieval-query-design-v0.3.md.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rank_bm25 import BM25Okapi

from synthadoc.kb import ids as _ids
from synthadoc.kb.layout import KBLayout
from synthadoc.storage.search import HybridSearch

logger = logging.getLogger(__name__)


@dataclass
class SourceChunk:
    """One retrieved passage of verbatim source text."""
    source_id: str
    title: str
    date: str          # meeting date (YYYY-MM-DD) parsed from the source id, or ""
    score: float
    text: str


def _chunk_text(text: str, chunk_chars: int) -> list[str]:
    """Split into ~chunk_chars windows on blank-line (paragraph) boundaries.

    Paragraphs longer than chunk_chars are emitted whole rather than cut
    mid-sentence — a slightly-oversized chunk is cheaper than a figure severed
    from its context.
    """
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if buf and len(buf) + len(p) + 2 > chunk_chars:
            chunks.append(buf)
            buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        chunks.append(buf)
    return chunks


def _source_date(source_id: str) -> str:
    """Meeting date from a source id, or '' if it can't be parsed.

    The id (``source.<type>.<YYYY-MM-DD>.<slug>``) carries the meeting date;
    the `sources.created_at` column is ingest time, not the meeting date.
    """
    try:
        return _ids.split_source_id(source_id)[1]
    except (ValueError, IndexError):
        return ""


class SourceSearch:
    """BM25 over chunked verbatim source text from the fact tier.

    Corpus is built lazily from the `sources` table + parsed files on disk and
    cached; call `invalidate_index()` after an ingest so new sources are picked
    up. Read-only: uses a plain sqlite3 connection (the enumeration is a simple
    SELECT and this keeps the retriever synchronous, mirroring
    `HybridSearch.bm25_search`).
    """

    def __init__(self, wiki_root: Path, *, chunk_chars: int = 800) -> None:
        self._layout = KBLayout(wiki_root)
        self._chunk_chars = chunk_chars
        # cache: (list[SourceChunk-without-score], list[tokenized-chunk])
        self._cached: Optional[tuple[list[SourceChunk], list[list[str]]]] = None

    def invalidate_index(self) -> None:
        """Drop the in-memory corpus cache. Call after any source ingest."""
        self._cached = None

    def _iter_sources(self) -> list[tuple[str, str, str]]:
        """Return (id, title, parsed_path) for every source, or [] if no KB yet."""
        db_path = self._layout.db_path
        if not db_path.exists():
            return []
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            return []
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT id, title, parsed_path FROM sources ORDER BY id"
            ).fetchall()
        except sqlite3.OperationalError:
            return []  # sources table absent (fresh/empty wiki)
        finally:
            con.close()
        return [(r["id"], r["title"], r["parsed_path"]) for r in rows]

    def _corpus(self) -> tuple[list[SourceChunk], list[list[str]]]:
        if self._cached is not None:
            return self._cached
        metas: list[SourceChunk] = []
        tokenized: list[list[str]] = []
        root = self._layout.root
        n_sources = 0
        for source_id, title, parsed_path in self._iter_sources():
            if not parsed_path:
                continue
            fpath = root / parsed_path
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue  # parsed file missing — skip, don't fail the whole query
            n_sources += 1
            date = _source_date(source_id)
            for chunk in _chunk_text(text, self._chunk_chars):
                metas.append(SourceChunk(
                    source_id=source_id, title=title, date=date,
                    score=0.0, text=chunk,
                ))
                tokenized.append(HybridSearch._tokenize(f"{title} {chunk}"))
        logger.info("source corpus built — %d sources, %d chunks",
                    n_sources, len(metas))
        self._cached = (metas, tokenized)
        return self._cached

    def search(self, query_terms: list[str], top_n: int = 6,
               per_source_cap: int = 2) -> list[SourceChunk]:
        """Top source passages for the query.

        `per_source_cap` limits how many chunks a single source may contribute,
        so one long meeting can't crowd out the other meetings a multi-meeting
        question needs.
        """
        metas, corpus = self._corpus()
        if not corpus:
            return []
        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(HybridSearch._tokenize(" ".join(query_terms)))
        ranked = sorted(
            range(len(metas)), key=lambda i: scores[i], reverse=True
        )
        out: list[SourceChunk] = []
        per_source: dict[str, int] = {}
        for i in ranked:
            score = float(scores[i])
            if score <= 0:
                break
            m = metas[i]
            if per_source.get(m.source_id, 0) >= per_source_cap:
                continue
            per_source[m.source_id] = per_source.get(m.source_id, 0) + 1
            out.append(SourceChunk(
                source_id=m.source_id, title=m.title, date=m.date,
                score=score, text=m.text,
            ))
            if len(out) >= top_n:
                break
        return out
