# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
import pytest

from synthadoc.storage import wiki_aliases as wa
from synthadoc.storage import wiki_merge
from synthadoc.storage.wiki import WikiStorage, WikiPage


def _page(content, title="T", tags=None):
    return WikiPage(title=title, tags=tags or [], content=content,
                    status="active", confidence="medium", sources=[])


def _mk(root):
    store = WikiStorage(root / "wiki")
    return store


def test_merge_appends_relinks_deletes_and_aliases(tmp_wiki):
    store = _mk(tmp_wiki)
    store.write_page("lyzr", _page("# Lyzr\n\nKeeper body.\n\n_— Source: [[s1]] · 2026-06-09_",
                                   title="Lyzr", tags=["AI"]), folder="projects")
    store.write_page("liz", _page("# Liz\n\nVariant body.\n\n_— Source: [[s2]] · 2026-06-11_",
                                  title="Liz", tags=["UI"]), folder="projects")
    # A third page that links to the dup must get relinked.
    store.write_page("gc-tst", _page("Uses [[liz]] for prototyping and [[liz|Lizer]].",
                                     title="GC TST"), folder="projects")
    # index referencing both
    (tmp_wiki / "wiki" / "index.md").write_text(
        "## Recently Added\n- [[lyzr]] — Lyzr\n- [[liz]] — Liz\n",
        encoding="utf-8")

    result = wiki_merge.merge_pages(store, tmp_wiki, "liz", "lyzr")

    # dup deleted
    assert not store.page_exists("liz")
    # keeper has both bodies + both source citations
    keeper = store.read_page("lyzr")
    assert "Keeper body." in keeper.content
    assert "Variant body." in keeper.content
    assert keeper.content.count("_— Source:") == 2
    assert "merged from liz" in keeper.content
    # tags unioned
    assert "AI" in keeper.tags and "UI" in keeper.tags
    # third page relinked (no [[liz]] left)
    other = store.read_page("gc-tst")
    assert "[[liz]]" not in other.content and "[[liz|" not in other.content
    assert "[[lyzr]]" in other.content and "[[lyzr|Lizer]]" in other.content
    # index: dup line gone, keeper line kept
    idx = (tmp_wiki / "wiki" / "index.md").read_text(encoding="utf-8")
    assert "[[liz]]" not in idx
    assert "[[lyzr]]" in idx
    assert result.index_lines_removed == 1
    # alias recorded durably
    aliases = wa.load(tmp_wiki / "wiki_aliases.yaml")
    assert aliases.resolve("liz") == "lyzr"
    assert result.alias_recorded


def test_merge_does_not_touch_source_filename_links(tmp_wiki):
    store = _mk(tmp_wiki)
    store.write_page("lyzr", _page("# Lyzr\n\nKeeper."), folder="projects")
    # 'liz' page whose body cites a source file whose NAME contains 'liz...'
    store.write_page("liz", _page(
        "# Liz\n\n_— Source: [[2026-06-11 Lizer_Claude Code Push]] · 2026-06-11_"),
        folder="projects")
    wiki_merge.merge_pages(store, tmp_wiki, "liz", "lyzr")
    keeper = store.read_page("lyzr")
    # the source-filename wikilink must survive verbatim (not rewritten)
    assert "[[2026-06-11 Lizer_Claude Code Push]]" in keeper.content


def test_merge_rejects_bad_args(tmp_wiki):
    store = _mk(tmp_wiki)
    store.write_page("lyzr", _page("# Lyzr\n\nx"), folder="projects")
    with pytest.raises(ValueError):
        wiki_merge.merge_pages(store, tmp_wiki, "lyzr", "lyzr")       # same slug
    with pytest.raises(ValueError):
        wiki_merge.merge_pages(store, tmp_wiki, "ghost", "lyzr")      # dup missing
    with pytest.raises(ValueError):
        wiki_merge.merge_pages(store, tmp_wiki, "lyzr", "missing")    # keeper missing
    with pytest.raises(ValueError):
        wiki_merge.merge_pages(store, tmp_wiki, "index", "lyzr")      # structural


def test_no_alias_flag_skips_recording(tmp_wiki):
    store = _mk(tmp_wiki)
    store.write_page("lyzr", _page("# Lyzr\n\nx"), folder="projects")
    store.write_page("liz", _page("# Liz\n\ny"), folder="projects")
    result = wiki_merge.merge_pages(store, tmp_wiki, "liz", "lyzr", record_alias=False)
    assert not result.alias_recorded
    assert not (tmp_wiki / "wiki_aliases.yaml").exists()


def test_find_duplicate_candidates(tmp_wiki):
    store = _mk(tmp_wiki)
    for slug, title in [("liz", "Liz"), ("lizer", "Lizer"),
                        ("gc-tst", "GC TST"), ("gc-tst-d", "GC TST D"),
                        ("egp", "EGP"), ("byob", "BYOB")]:
        store.write_page(slug, _page(f"# {title}\n\nbody", title=title), folder="projects")

    cands = wiki_merge.find_duplicate_candidates(store)
    pairs = {frozenset((c.a, c.b)) for c in cands}
    assert frozenset(("liz", "lizer")) in pairs        # similar slug
    assert frozenset(("gc-tst", "gc-tst-d")) in pairs  # dash extension
    # unrelated pages are not flagged together
    assert frozenset(("egp", "byob")) not in pairs


def test_find_duplicate_candidates_skips_declared_aliases(tmp_wiki):
    store = _mk(tmp_wiki)
    store.write_page("lyzr", _page("# Lyzr\n\nx", title="Lyzr"), folder="projects")
    store.write_page("lizer", _page("# Lizer\n\ny", title="Lizer"), folder="projects")
    aliases = wa.parse("aliases:\n  lyzr:\n    - lizer\n")
    cands = wiki_merge.find_duplicate_candidates(store, aliases=aliases)
    pairs = {frozenset((c.a, c.b)) for c in cands}
    assert frozenset(("lyzr", "lizer")) not in pairs   # already declared
