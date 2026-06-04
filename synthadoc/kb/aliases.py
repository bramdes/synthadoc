# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Version-controllable entity alias map (``kb_aliases.yaml``).

Merging duplicates with ``synthadoc kb review merge`` records the decision in
``kb.db`` — but that DB is gitignored and is wiped on a clean re-import. The
alias file is the *durable*, committable record: it declares that several
names all refer to one canonical entity, and the :class:`EntityLinker`
applies it **during import** so variants never fragment in the first place.

File shape (canonical *display name* as the key; aliases may be names or
slugs — both are slugified on load)::

    # kb_aliases.yaml
    aliases:
      project:
        AURA:
          - AURA Program
          - AURA EGP Program
          - aura-release
        Doc Intel:
          - Document Intelligence
      person:
        Harsh:
          - hash

Resolution is by slug: every alias slug maps to the canonical (name, slug).
The file is a hint layer over the deterministic linker — editing it is
non-destructive; re-running import re-applies it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

import yaml

from synthadoc.kb import ids as _ids


class AliasError(ValueError):
    """Raised when kb_aliases.yaml is structurally invalid."""


@dataclass(frozen=True)
class Canonical:
    """The surviving entity an alias resolves to."""

    name: str
    slug: str


@dataclass(frozen=True)
class Aliases:
    """Parsed alias map: entity_type -> alias_slug -> Canonical."""

    by_type: Mapping[str, Mapping[str, Canonical]] = field(default_factory=dict)

    def resolve(self, entity_type: str, slug: str) -> Optional[Canonical]:
        """Return the canonical entity for *slug*, or None if not aliased."""
        return self.by_type.get(entity_type, {}).get(slug)

    @property
    def is_empty(self) -> bool:
        return not any(self.by_type.values())


def parse(text: str) -> Aliases:
    """Parse a kb_aliases.yaml document into a typed :class:`Aliases`."""
    if not text or not text.strip():
        return Aliases()
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise AliasError(f"kb_aliases.yaml parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise AliasError("kb_aliases.yaml top-level must be a mapping")
    raw = data.get("aliases", {})
    if not isinstance(raw, dict):
        raise AliasError("'aliases' must be a mapping of entity_type -> {canonical: [aliases]}")

    by_type: dict[str, dict[str, Canonical]] = {}
    for entity_type, groups in raw.items():
        if entity_type not in _ids.ENTITY_TYPES:
            raise AliasError(
                f"unknown entity_type {entity_type!r} in kb_aliases.yaml; "
                f"valid: {sorted(_ids.ENTITY_TYPES)!r}"
            )
        if not isinstance(groups, dict):
            raise AliasError(f"aliases[{entity_type!r}] must be a mapping")
        slug_map: dict[str, Canonical] = {}
        for canonical_name, alias_list in groups.items():
            canonical = Canonical(
                name=str(canonical_name).strip(),
                slug=_ids.slugify(str(canonical_name)),
            )
            if alias_list is None:
                alias_list = []
            if not isinstance(alias_list, list):
                raise AliasError(
                    f"aliases[{entity_type!r}][{canonical_name!r}] must be a list"
                )
            # The canonical name is implicitly its own alias.
            for alias in [canonical_name, *alias_list]:
                slug_map[_ids.slugify(str(alias))] = canonical
        by_type[entity_type] = slug_map
    return Aliases(by_type=by_type)


def load(path: Path) -> Aliases:
    """Load and parse ``kb_aliases.yaml`` at *path*. Missing file → empty."""
    p = Path(path)
    if not p.exists():
        return Aliases()
    return parse(p.read_text(encoding="utf-8"))


def add_alias(
    path: Path, *, entity_type: str, canonical_name: str, alias_name: str
) -> None:
    """Idempotently record that *alias_name* is the same entity as
    *canonical_name*. Creates ``kb_aliases.yaml`` if absent, preserving any
    existing entries and comments-free structure.

    Used by ``kb review merge`` so an interactive merge becomes a durable,
    committable rule that survives a re-import.
    """
    if entity_type not in _ids.ENTITY_TYPES:
        raise AliasError(f"unknown entity_type {entity_type!r}")
    p = Path(path)
    data: dict = {}
    if p.exists():
        loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            data = loaded
    aliases = data.setdefault("aliases", {})
    if not isinstance(aliases, dict):
        raise AliasError("'aliases' must be a mapping")
    group = aliases.setdefault(entity_type, {})
    if not isinstance(group, dict):
        raise AliasError(f"aliases[{entity_type!r}] must be a mapping")

    # Find the existing canonical entry by slug match (so display-name casing
    # differences don't create a second group), else create it.
    canon_slug = _ids.slugify(canonical_name)
    target_key = None
    for key in group:
        if _ids.slugify(str(key)) == canon_slug:
            target_key = key
            break
    if target_key is None:
        target_key = canonical_name
        group[target_key] = []
    members = group[target_key]
    if members is None:
        members = []
        group[target_key] = members
    if not isinstance(members, list):
        raise AliasError(f"aliases[{entity_type!r}][{target_key!r}] must be a list")

    # Don't add the alias if it's already present (by slug) or is the canonical.
    alias_slug = _ids.slugify(alias_name)
    existing_slugs = {canon_slug} | {_ids.slugify(str(m)) for m in members}
    if alias_slug not in existing_slugs:
        members.append(alias_name)

    header = (
        "# Entity aliases for the temporal KB. Declares that several names are\n"
        "# the SAME entity; applied during import so duplicates never form.\n"
        "# Managed by `synthadoc kb review merge`; safe to hand-edit + commit.\n\n"
    )
    p.write_text(
        header + yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )
