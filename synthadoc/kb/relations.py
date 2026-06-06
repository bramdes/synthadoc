# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Version-controllable entity relationships (``kb_relations.yaml``).

Where :mod:`synthadoc.kb.aliases` says "these names are the SAME entity",
this module says "this entity is a SUB-AREA of that one" — a parent/child
("part of") relationship the entity renderer surfaces as a *Sub-areas*
section on the parent and a *Part of* line on each child.

Unlike facts, relationships are hand-curated and deterministic: there is no
LLM extraction. The file lives at the wiki root (sibling of kb_config.yaml /
kb_aliases.yaml), commits with the wiki, and survives a clean re-import.

File shape (parent display name or slug as the key; children may be names or
slugs — both are slugified on load)::

    # kb_relations.yaml
    relations:
      project:
        Doc Intel:
          - Doc Intel intent extraction
          - doc-intel-itc-production-deployment

A child may have at most one parent (the last declaration wins, with a
warning surfaced at load is out of scope — the parser simply overwrites).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

import yaml

from synthadoc.kb import ids as _ids


class RelationError(ValueError):
    """Raised when kb_relations.yaml is structurally invalid."""


@dataclass(frozen=True)
class Relations:
    """Parsed relationship map.

    ``children`` maps entity_type -> parent_slug -> ordered child slugs.
    ``parent_of`` maps entity_type -> child_slug -> parent_slug.
    """

    children: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)
    parents: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def children_of(self, entity_type: str, slug: str) -> tuple[str, ...]:
        return self.children.get(entity_type, {}).get(slug, ())

    def parent_of(self, entity_type: str, slug: str) -> Optional[str]:
        return self.parents.get(entity_type, {}).get(slug)

    @property
    def is_empty(self) -> bool:
        return not any(self.children.values())


def parse(text: str) -> Relations:
    """Parse a kb_relations.yaml document into a typed :class:`Relations`."""
    if not text or not text.strip():
        return Relations()
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise RelationError(f"kb_relations.yaml parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise RelationError("kb_relations.yaml top-level must be a mapping")
    raw = data.get("relations", {})
    if not isinstance(raw, dict):
        raise RelationError("'relations' must be a mapping of entity_type -> {parent: [children]}")

    children: dict[str, dict[str, tuple[str, ...]]] = {}
    parents: dict[str, dict[str, str]] = {}
    for entity_type, groups in raw.items():
        if entity_type not in _ids.ENTITY_TYPES:
            raise RelationError(
                f"unknown entity_type {entity_type!r} in kb_relations.yaml; "
                f"valid: {sorted(_ids.ENTITY_TYPES)!r}"
            )
        if not isinstance(groups, dict):
            raise RelationError(f"relations[{entity_type!r}] must be a mapping")
        ct: dict[str, tuple[str, ...]] = {}
        pt: dict[str, str] = {}
        for parent_name, child_list in groups.items():
            parent_slug = _ids.slugify(str(parent_name))
            if child_list is None:
                child_list = []
            if not isinstance(child_list, list):
                raise RelationError(
                    f"relations[{entity_type!r}][{parent_name!r}] must be a list"
                )
            child_slugs: list[str] = []
            for child in child_list:
                cs = _ids.slugify(str(child))
                if cs == parent_slug:
                    continue  # an entity can't be its own sub-area
                child_slugs.append(cs)
                pt[cs] = parent_slug
            if child_slugs:
                ct[parent_slug] = tuple(dict.fromkeys(child_slugs))  # dedupe, keep order
        children[entity_type] = ct
        parents[entity_type] = pt
    return Relations(children=children, parents=parents)


def load(path: Path) -> Relations:
    """Load and parse ``kb_relations.yaml`` at *path*. Missing file → empty."""
    p = Path(path)
    if not p.exists():
        return Relations()
    return parse(p.read_text(encoding="utf-8"))


def add_relation(
    path: Path, *, entity_type: str, parent_name: str, child_name: str
) -> None:
    """Idempotently record that *child_name* is a sub-area of *parent_name*.

    Creates ``kb_relations.yaml`` if absent, preserving existing entries.
    """
    if entity_type not in _ids.ENTITY_TYPES:
        raise RelationError(f"unknown entity_type {entity_type!r}")
    if _ids.slugify(child_name) == _ids.slugify(parent_name):
        raise RelationError("an entity cannot be a sub-area of itself")
    p = Path(path)
    data: dict = {}
    if p.exists():
        loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            data = loaded
    relations = data.setdefault("relations", {})
    if not isinstance(relations, dict):
        raise RelationError("'relations' must be a mapping")
    group = relations.setdefault(entity_type, {})
    if not isinstance(group, dict):
        raise RelationError(f"relations[{entity_type!r}] must be a mapping")

    parent_slug = _ids.slugify(parent_name)
    target_key = None
    for key in group:
        if _ids.slugify(str(key)) == parent_slug:
            target_key = key
            break
    if target_key is None:
        target_key = parent_name
        group[target_key] = []
    members = group[target_key]
    if members is None:
        members = []
        group[target_key] = members
    if not isinstance(members, list):
        raise RelationError(f"relations[{entity_type!r}][{target_key!r}] must be a list")

    child_slug = _ids.slugify(child_name)
    existing_slugs = {_ids.slugify(str(m)) for m in members}
    if child_slug not in existing_slugs:
        members.append(child_name)

    header = (
        "# Entity relationships for the temporal KB. Declares parent -> sub-area\n"
        "# ('part of') links, surfaced on entity pages. Managed by\n"
        "# `synthadoc kb review relate`; safe to hand-edit + commit.\n\n"
    )
    p.write_text(
        header + yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )
