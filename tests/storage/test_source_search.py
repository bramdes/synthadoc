# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
import sqlite3

import pytest

from synthadoc.storage.source_search import (
    SourceSearch, _chunk_text, _source_date,
)


# BM25 needs a corpus: IDF for a term is non-positive when it appears in (nearly)
# every doc, so tiny 1-2 doc fixtures score everything <= 0 and return nothing.
# Production has hundreds of sources; these fillers give the fixture realistic
# document frequencies so a discriminating term earns a positive score.
_FILLERS = [
    (f"source.document.2026-04-{d:02d}.filler-{d}",
     f"2026-04-{d:02d} Unrelated Meeting {d}",
     f"Team {d} discussed hiring, office logistics, and travel plans for topic {d}.")
    for d in range(1, 13)
]


def _make_kb(root, sources):
    """Create a minimal kb.db + parsed files. `sources` = [(id, title, text)].

    Callers pass their salient sources; `_FILLERS` are added automatically so
    BM25 document frequencies are realistic.
    """
    sources = list(sources) + _FILLERS
    (root / ".synthadoc").mkdir(parents=True, exist_ok=True)
    parsed_dir = root / "kb" / "sources" / "parsed" / "documents"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(root / ".synthadoc" / "kb.db")
    con.execute(
        "CREATE TABLE sources (id TEXT PRIMARY KEY, source_type TEXT, title TEXT, "
        "authority TEXT, raw_path TEXT, parsed_path TEXT, created_at TEXT, "
        "ingested_at TEXT, author TEXT, sha256 TEXT)"
    )
    for sid, title, text in sources:
        fname = sid.replace("source.document.", "").replace(".", "-") + ".md"
        rel = f"kb/sources/parsed/documents/{fname}"
        (root / rel).write_text(text, encoding="utf-8")
        con.execute(
            "INSERT INTO sources (id, source_type, title, parsed_path, sha256) "
            "VALUES (?, 'document', ?, ?, ?)",
            (sid, title, rel, sid),
        )
    con.commit()
    con.close()


def test_chunk_text_splits_on_paragraphs():
    text = "para one is here.\n\n" + ("x " * 500) + "\n\ntail para."
    chunks = _chunk_text(text, chunk_chars=200)
    assert len(chunks) >= 2
    # An oversized single paragraph is emitted whole, not cut.
    assert any("x x x" in c for c in chunks)
    assert chunks[0].startswith("para one")


def test_source_date_parses_from_id():
    assert _source_date("source.document.2026-05-12.egp-budget-sim") == "2026-05-12"
    assert _source_date("not-a-source-id") == ""


def test_search_ranks_matching_source(tmp_path):
    _make_kb(tmp_path, [
        ("source.document.2026-05-12.egp-budget",
         "2026-05-12 EGP Budget Simulation",
         "The EGP baseline was 9.5 out of 22.9 million.\n\nVikram warned it "
         "would not drop to 15 or 16."),
        ("source.document.2026-05-02.condo-viewing",
         "2026-05-02 Condo Viewing",
         "A four-bedroom condo unit was assessed for purchase."),
    ])
    ss = SourceSearch(tmp_path)
    hits = ss.search(["egp", "baseline", "22.9"], top_n=5)
    assert hits, "expected at least one source hit"
    assert hits[0].source_id == "source.document.2026-05-12.egp-budget"
    assert hits[0].date == "2026-05-12"
    assert "22.9" in hits[0].text


def test_search_empty_when_no_kb(tmp_path):
    # No kb.db at all — must return [] rather than raise.
    assert SourceSearch(tmp_path).search(["anything"]) == []


def test_per_source_cap_limits_one_source(tmp_path):
    big = "\n\n".join(f"paragraph {i} mentions egp cost figures." for i in range(10))
    _make_kb(tmp_path, [
        ("source.document.2026-05-12.egp-a", "EGP A", big),
        ("source.document.2026-05-13.egp-b", "EGP B", big),
    ])
    ss = SourceSearch(tmp_path, chunk_chars=40)  # force many chunks per source
    hits = ss.search(["egp", "cost", "figures"], top_n=6, per_source_cap=2)
    from collections import Counter
    per_src = Counter(h.source_id for h in hits)
    assert per_src, "expected non-empty hits"
    assert all(v <= 2 for v in per_src.values())


def test_invalidate_index_rebuilds(tmp_path):
    _make_kb(tmp_path, [
        ("source.document.2026-05-12.egp", "EGP", "egp budget baseline detail."),
    ])
    ss = SourceSearch(tmp_path)
    assert ss.search(["egp"])
    # Add a second source, then invalidate — the new one must become searchable.
    con = sqlite3.connect(tmp_path / ".synthadoc" / "kb.db")
    rel = "kb/sources/parsed/documents/2026-05-20-new.md"
    (tmp_path / rel).write_text("brand new topic quantexa context layer.", encoding="utf-8")
    con.execute("INSERT INTO sources (id, source_type, title, parsed_path, sha256) "
                "VALUES ('source.document.2026-05-20.new', 'document', 'New', ?, 'h2')",
                (rel,))
    con.commit(); con.close()
    assert not ss.search(["quantexa"])   # stale cache: not visible yet
    ss.invalidate_index()
    assert ss.search(["quantexa"])        # now visible
