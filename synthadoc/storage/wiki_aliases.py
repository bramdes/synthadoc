# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Version-controllable page-slug alias map (``wiki_aliases.yaml``).

The page tier picks a slug per subject via the LLM decision step, so a weak or
offline model fragments the same subject into several pages (``liz``/``lizer``/
``lyzr``). This file is the *durable* record that several slugs are one page:
``synthadoc wiki merge`` records the decision here, and :class:`IngestAgent`
applies it **during ingest** so a declared variant is rewritten to its
canonical slug before any page is created — duplicates never re-form.

It is the page-tier analogue of ``kb_aliases.yaml`` (fact tier). Unlike the
fact tier there is no entity_type dimension — slugs are unique across the whole
wiki — so the map is flat:

    # wiki_aliases.yaml
    aliases:
      lyzr:                 # canonical slug (the keeper page)
        - liz
        - lizer
        - glazer
      gc-tst:
        - gc-tst-d

Everything is slugified on load, so you may hand-write display names or slugs.
Editing this file is non-destructive; it only affects *future* ingests — run
``synthadoc wiki merge`` (or hand-merge) to fold pages that already exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

import yaml

from synthadoc.kb import ids as _ids


class WikiAliasError(ValueError):
    """Raised when wiki_aliases.yaml is structurally invalid."""


@dataclass(frozen=True)
class WikiAliases:
    """Parsed alias map: alias_slug -> canonical_slug."""

    by_slug: Mapping[str, str] = field(default_factory=dict)

    def resolve(self, slug: str) -> Optional[str]:
        """Return the canonical slug for *slug*, or None if not aliased.

        Resolution is transitive-safe: the map is flattened at parse time so a
        single lookup always lands on the final canonical slug.
        """
        if not slug:
            return None
        return self.by_slug.get(_ids.slugify(slug))

    def canonical(self, slug: str) -> str:
        """Return the canonical slug for *slug*, or *slug* unchanged."""
        return self.resolve(slug) or slug

    @property
    def is_empty(self) -> bool:
        return not self.by_slug


def parse(text: str) -> WikiAliases:
    """Parse a wiki_aliases.yaml document into a typed :class:`WikiAliases`."""
    if not text or not text.strip():
        return WikiAliases()
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise WikiAliasError(f"wiki_aliases.yaml parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise WikiAliasError("wiki_aliases.yaml top-level must be a mapping")
    raw = data.get("aliases", {})
    if not isinstance(raw, dict):
        raise WikiAliasError("'aliases' must be a mapping of canonical_slug -> [aliases]")

    by_slug: dict[str, str] = {}
    for canonical, alias_list in raw.items():
        canon_slug = _ids.slugify(str(canonical))
        if alias_list is None:
            alias_list = []
        if not isinstance(alias_list, list):
            raise WikiAliasError(f"aliases[{canonical!r}] must be a list")
        for alias in alias_list:
            alias_slug = _ids.slugify(str(alias))
            if alias_slug and alias_slug != canon_slug:
                by_slug[alias_slug] = canon_slug

    # Flatten one level of chaining: if a canonical slug is itself an alias of
    # another, redirect to the ultimate target (cap iterations to avoid cycles).
    for _ in range(len(by_slug)):
        changed = False
        for alias_slug, canon in list(by_slug.items()):
            if canon in by_slug and by_slug[canon] != alias_slug:
                by_slug[alias_slug] = by_slug[canon]
                changed = True
        if not changed:
            break
    return WikiAliases(by_slug=by_slug)


def load(path: Path) -> WikiAliases:
    """Load and parse ``wiki_aliases.yaml`` at *path*. Missing file → empty."""
    p = Path(path)
    if not p.exists():
        return WikiAliases()
    return parse(p.read_text(encoding="utf-8"))


_HEADER = (
    "# Page-slug aliases for the wiki (page tier). Declares that several slugs\n"
    "# are the SAME page; applied during ingest so duplicates never re-form.\n"
    "# Managed by `synthadoc wiki merge`; safe to hand-edit + commit.\n\n"
)


def add_alias(path: Path, *, canonical_slug: str, alias_slug: str) -> None:
    """Idempotently record that *alias_slug* is the same page as *canonical_slug*.

    Creates ``wiki_aliases.yaml`` if absent, preserving existing entries. Used
    by ``synthadoc wiki merge`` so a merge becomes a durable, committable rule.
    """
    canon = _ids.slugify(canonical_slug)
    alias = _ids.slugify(alias_slug)
    if not canon or not alias:
        raise WikiAliasError("canonical_slug and alias_slug must be non-empty")
    if canon == alias:
        raise WikiAliasError("a slug cannot be an alias of itself")

    p = Path(path)
    data: dict = {}
    if p.exists():
        loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            data = loaded
    aliases = data.setdefault("aliases", {})
    if not isinstance(aliases, dict):
        raise WikiAliasError("'aliases' must be a mapping")

    # Find the canonical group by slug match (tolerate display-name keys).
    target_key = None
    for key in aliases:
        if _ids.slugify(str(key)) == canon:
            target_key = key
            break
    if target_key is None:
        target_key = canon
        aliases[target_key] = []
    members = aliases[target_key]
    if members is None:
        members = []
        aliases[target_key] = members
    if not isinstance(members, list):
        raise WikiAliasError(f"aliases[{target_key!r}] must be a list")

    if alias not in {_ids.slugify(str(m)) for m in members}:
        members.append(alias)

    p.write_text(
        _HEADER + yaml.dump(data, default_flow_style=False,
                            allow_unicode=True, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )
