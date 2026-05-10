# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from filelock import FileLock

_FRONTMATTER_FIELDS = ("title", "tags", "status", "confidence", "created", "sources", "orphan", "categories")


@dataclass
class SourceRef:
    file: str
    hash: str
    size: int
    ingested: str


@dataclass
class WikiPage:
    title: str
    tags: list[str]
    content: str
    status: str
    confidence: str
    sources: list[SourceRef]
    created: Optional[str] = None
    orphan: bool = False
    categories: list[str] = field(default_factory=list)
    # SHA-256 of `content` at the moment ConsolidateAgent last rewrote this page.
    # Used to short-circuit re-runs when nothing has changed since.
    consolidated_hash: Optional[str] = None
    # Subfolder under wiki root where this page lives (e.g. "people", "projects").
    # None means the page is at the wiki root. Runtime-only — not serialized.
    folder: Optional[str] = None


def _sources_to_dicts(sources: list[SourceRef]) -> list[dict]:
    return [
        {"file": s.file, "hash": s.hash, "size": s.size, "ingested": s.ingested}
        for s in sources
    ]


def _sources_from_dicts(raw: list) -> list[SourceRef]:
    result = []
    for item in (raw or []):
        if isinstance(item, dict):
            result.append(SourceRef(
                file=item.get("file", ""),
                hash=item.get("hash", ""),
                size=item.get("size", 0),
                ingested=item.get("ingested", ""),
            ))
    return result


class WikiStorage:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_meta = threading.Lock()

    def _assert_in_root(self, path: Path) -> None:
        resolved = path.resolve()
        root_resolved = self._root.resolve()
        try:
            resolved.relative_to(root_resolved)
        except ValueError:
            raise PermissionError(
                f"Path {resolved} is outside wiki root {root_resolved}"
            )

    def _iter_subfolders(self):
        """Yield immediate subdirectories of the wiki root, skipping dotfiles."""
        for p in self._root.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                yield p

    def _find_existing_path(self, slug: str) -> Optional[Path]:
        """Locate <slug>.md across the wiki root and its subfolders.

        Root takes precedence; subfolders are checked in alphabetical order.
        Returns None if no file matches. Pages can sit anywhere in this layout
        because slugs (and therefore [[wikilinks]]) are unique per wiki.
        """
        root_match = self._root / f"{slug}.md"
        if root_match.exists():
            return root_match
        for sub in sorted(self._iter_subfolders(), key=lambda p: p.name):
            cand = sub / f"{slug}.md"
            if cand.exists():
                return cand
        return None

    def _page_path(self, slug: str, folder: Optional[str] = None) -> Path:
        """Return the on-disk path for reading or writing a page.

        Existing pages keep their location — the `folder` arg is ignored.
        For a new page, `folder` selects a subdirectory beneath the wiki root;
        omit to keep the page at the root.
        """
        existing = self._find_existing_path(slug)
        if existing is not None:
            self._assert_in_root(existing)
            return existing
        if folder:
            page_path = self._root / folder / f"{slug}.md"
        else:
            page_path = self._root / f"{slug}.md"
        self._assert_in_root(page_path)
        return page_path

    def write_page(
        self,
        slug: str,
        page_or_content,
        frontmatter: Optional[dict] = None,
        folder: Optional[str] = None,
    ) -> None:
        if isinstance(page_or_content, WikiPage):
            page = page_or_content
            fm: dict = {
                "title": page.title,
                "tags": page.tags,
                "status": page.status,
                "confidence": page.confidence,
                "created": page.created,
                "sources": _sources_to_dicts(page.sources),
                "orphan": page.orphan,
            }
            if page.categories:
                fm["categories"] = page.categories
            if page.consolidated_hash:
                fm["consolidated_hash"] = page.consolidated_hash
            body = page.content
        else:
            fm = frontmatter or {}
            body = page_or_content

        yaml_str = yaml.dump(fm, default_flow_style=False, allow_unicode=True)
        text = f"---\n{yaml_str}---\n\n{body}"
        target = self._page_path(slug, folder=folder)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def read_page(self, slug: str) -> Optional[WikiPage]:
        target = self._find_existing_path(slug)
        if target is None:
            return None

        raw = target.read_text(encoding="utf-8")

        # Parse frontmatter block
        fm: dict = {}
        body = raw
        if raw.startswith("---"):
            parts = raw.split("---", 2)
            if len(parts) >= 3:
                fm = yaml.safe_load(parts[1]) or {}
                body = parts[2].lstrip("\n")

        sources = _sources_from_dicts(fm.get("sources", []))
        raw_cats = fm.get("categories", [])
        categories = raw_cats if isinstance(raw_cats, list) else [raw_cats] if raw_cats else []
        rel_parent = target.parent.relative_to(self._root)
        folder = None if str(rel_parent) == "." else str(rel_parent).replace("\\", "/")
        return WikiPage(
            title=fm.get("title", ""),
            tags=fm.get("tags", []),
            content=body,
            status=fm.get("status", ""),
            confidence=fm.get("confidence", ""),
            sources=sources,
            created=fm.get("created"),
            orphan=bool(fm.get("orphan", False)),
            categories=categories,
            consolidated_hash=fm.get("consolidated_hash") or None,
            folder=folder,
        )

    def page_exists(self, slug: str) -> bool:
        return self._find_existing_path(slug) is not None

    def iter_page_paths(self) -> list[tuple[str, Path]]:
        """Return (slug, path) for every page in the root and its subfolders.

        Root pages first, then one level of subfolders alphabetically. If a slug
        exists in both root and a subfolder, the root version wins (matches
        `_find_existing_path`).
        """
        out: list[tuple[str, Path]] = []
        seen: set[str] = set()
        for p in sorted(self._root.glob("*.md")):
            if p.stem not in seen:
                seen.add(p.stem)
                out.append((p.stem, p))
        for sub in sorted(self._iter_subfolders(), key=lambda p: p.name):
            for p in sorted(sub.glob("*.md")):
                if p.stem not in seen:
                    seen.add(p.stem)
                    out.append((p.stem, p))
        return out

    def list_pages(self) -> list[str]:
        return [slug for slug, _ in self.iter_page_paths()]

    def append_to_index(self, slug: str, title: str) -> None:
        """Append a newly created page entry to wiki/index.md under 'Recently Added'.

        No-ops silently if index.md does not exist or if the slug is already
        referenced anywhere in the file (prevents duplicates after re-ingest).
        Also stamps categories: [Recently Added] on the page's own frontmatter.
        """
        index_path = self._root / "index.md"
        if not index_path.exists():
            return
        raw = index_path.read_text(encoding="utf-8")
        # Skip if this slug is already linked anywhere in the index
        if f"[[{slug}]]" in raw or f"[[{slug}|" in raw:
            return
        entry = f"- [[{slug}]] — {title}"
        if "## Recently Added" in raw:
            raw = raw.rstrip() + f"\n{entry}\n"
        else:
            raw = raw.rstrip() + f"\n\n## Recently Added\n{entry}\n"
        index_path.write_text(raw, encoding="utf-8")
        # Stamp the page itself so it's queryable by category
        self._add_category(slug, "Recently Added")

    def set_page_categories(self, slug: str, categories: list[str]) -> None:
        """Replace the categories list on a page's frontmatter (idempotent)."""
        page = self.read_page(slug)
        if page is None:
            return
        page.categories = categories
        with self.page_lock(slug):
            self.write_page(slug, page)

    def _add_category(self, slug: str, category: str) -> None:
        """Add a single category to a page without removing existing ones."""
        page = self.read_page(slug)
        if page is None:
            return
        if category not in page.categories:
            page.categories = page.categories + [category]
            with self.page_lock(slug):
                self.write_page(slug, page)

    def _get_thread_lock(self, slug: str) -> threading.Lock:
        with self._locks_meta:
            if slug not in self._locks:
                self._locks[slug] = threading.Lock()
            return self._locks[slug]

    @contextmanager
    def page_lock(self, slug: str):
        lock = self._get_thread_lock(slug)
        lock_file = self._root / f".{slug}.lock"
        file_lock = FileLock(str(lock_file))
        with lock:
            with file_lock:
                yield
