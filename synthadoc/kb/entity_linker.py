# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Deterministic name → entity_id resolution.

A fact arrives carrying the name of its subject ("Document Intelligence",
"Pravin"). Before the fact can be persisted it needs a stable
``entity_id``. This module is the single point that performs that
resolution. Behaviour for v0.3:

1. Slugify the name.
2. If an entity row exists with that ``(entity_type, slug)``, return it —
   following ``merged_into`` to the canonical entity if it was merged.
3. If ``auto_create`` is True (default), insert a new entity row and
   return its id.
4. Otherwise return ``None`` and let the caller decide.

``merged_into`` chasing is what makes ``synthadoc kb review merge`` durable:
once a duplicate slug (e.g. ``aura-program``) is merged into a canonical
entity (``aura``), every future fact that resolves to that slug is attached
to the canonical entity instead of re-fragmenting it.

Open follow-ups (Layer 3):

- BM25 fuzzy matching against existing entity names.
- LLM tie-breaker for ambiguous candidates.

Those are explicitly deferred. The pure-deterministic linker is enough to
get a working pipeline and a deterministic test corpus.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from synthadoc.kb import aliases as _aliases
from synthadoc.kb import ids as _ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout


@dataclass(frozen=True)
class LinkResult:
    """Outcome of a single resolve call."""

    entity_id: str
    created: bool       # True if this call inserted a new entity row


class EntityLinker:
    """Stateless name resolver. Reuse one instance per wiki — cheap to construct."""

    def __init__(self, db: KBDB, layout: KBLayout) -> None:
        self._db = db
        self._layout = layout
        # Declared aliases (kb_aliases.yaml) are applied during import so
        # variant names resolve to the canonical entity from the start.
        self._aliases = _aliases.load(layout.aliases_path)

    async def resolve(
        self,
        *,
        name: str,
        entity_type: str,
        auto_create: bool = True,
    ) -> Optional[LinkResult]:
        """Return the entity_id for *name*, creating it if absent.

        Returns ``None`` only when ``auto_create=False`` and no row exists.
        Raises ``ValueError`` on unknown ``entity_type``.
        """
        if entity_type not in _ids.ENTITY_TYPES:
            raise ValueError(
                f"unknown entity_type {entity_type!r}; "
                f"valid: {sorted(_ids.ENTITY_TYPES)!r}"
            )
        if not name or not name.strip():
            raise ValueError("name must be non-empty")

        slug = _ids.slugify(name)

        # Apply declared aliases first: a variant name/slug is rewritten to the
        # canonical (name, slug) before any DB work, so the fact attaches to the
        # canonical entity and a duplicate is never created.
        canonical = self._aliases.resolve(entity_type, slug)
        if canonical is not None:
            name = canonical.name
            slug = canonical.slug

        eid = _ids.entity_id(entity_type, slug)

        existing = await self._db.get_entity(eid)
        if existing is not None:
            return LinkResult(entity_id=await self._canonical(eid), created=False)

        # Also check by (entity_type, slug) in case a future caller constructs
        # the id differently. With the current generator this is the same lookup,
        # but the row-based query future-proofs us against ID-format changes.
        rows = await self._db.fetchall(
            "SELECT id FROM entities WHERE entity_type = ? AND slug = ? LIMIT 1",
            (entity_type, slug),
        )
        if rows:
            return LinkResult(entity_id=await self._canonical(rows[0]["id"]), created=False)

        if not auto_create:
            return None

        path = str(
            self._layout.entity_index_path(entity_type, slug)
            .relative_to(self._layout.root)
        ).replace("\\", "/")

        await self._db.upsert_entity(
            id=eid,
            entity_type=entity_type,
            name=name.strip(),
            slug=slug,
            path=path,
        )
        return LinkResult(entity_id=eid, created=True)

    async def _canonical(self, eid: str) -> str:
        """Follow the ``merged_into`` chain to the surviving entity id.

        Returns *eid* unchanged when it was never merged. Guards against
        cycles (a self- or mutual-merge can't loop forever)."""
        seen: set[str] = set()
        cur = eid
        while cur and cur not in seen:
            seen.add(cur)
            row = await self._db.get_entity(cur)
            if row is None or not row.get("merged_into"):
                break
            cur = row["merged_into"]
        return cur

    async def resolve_many(
        self,
        items: list[tuple[str, str]],
        *,
        auto_create: bool = True,
    ) -> list[LinkResult]:
        """Resolve a list of ``(name, entity_type)`` pairs in sequence.

        Order-preserving. Duplicates within the same call are collapsed to
        a single ``created=True`` followed by ``created=False`` results.
        """
        out: list[LinkResult] = []
        for name, et in items:
            result = await self.resolve(
                name=name, entity_type=et, auto_create=auto_create
            )
            if result is None:
                # Caller asked for no auto_create and the entity didn't exist.
                # Surface the gap explicitly by skipping; callers can detect
                # via len(out) < len(items).
                continue
            out.append(result)
        return out
