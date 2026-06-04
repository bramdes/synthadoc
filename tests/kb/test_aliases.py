# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for the version-controllable entity alias map (synthadoc.kb.aliases)."""

from __future__ import annotations

import pytest

from synthadoc.kb import aliases
from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.entity_linker import EntityLinker
from synthadoc.kb.layout import KBLayout


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def test_parse_empty_returns_empty():
    assert aliases.parse("").is_empty
    assert aliases.parse("aliases: {}").is_empty


def test_parse_maps_alias_slugs_to_canonical():
    a = aliases.parse(
        "aliases:\n"
        "  project:\n"
        "    AURA:\n"
        "      - AURA Program\n"
        "      - aura-egp-program\n"
    )
    c = a.resolve("project", "aura-program")
    assert c is not None and c.name == "AURA" and c.slug == "aura"
    assert a.resolve("project", "aura-egp-program").slug == "aura"
    # Canonical resolves to itself
    assert a.resolve("project", "aura").slug == "aura"
    # Unrelated slug is not aliased
    assert a.resolve("project", "bedrock") is None


def test_parse_rejects_unknown_entity_type():
    with pytest.raises(aliases.AliasError):
        aliases.parse("aliases:\n  widget:\n    Foo:\n      - bar\n")


def test_parse_rejects_non_list_members():
    with pytest.raises(aliases.AliasError):
        aliases.parse("aliases:\n  project:\n    AURA: not-a-list\n")


# ---------------------------------------------------------------------------
# add_alias (round-trips through load)
# ---------------------------------------------------------------------------


def test_add_alias_creates_and_is_idempotent(tmp_path):
    p = tmp_path / "kb_aliases.yaml"
    aliases.add_alias(p, entity_type="project", canonical_name="AURA", alias_name="AURA Program")
    aliases.add_alias(p, entity_type="project", canonical_name="AURA", alias_name="AURA Program")
    aliases.add_alias(p, entity_type="project", canonical_name="AURA", alias_name="AURA EGP Program")

    a = aliases.load(p)
    assert a.resolve("project", "aura-program").slug == "aura"
    assert a.resolve("project", "aura-egp-program").slug == "aura"
    # Idempotent: AURA Program recorded once
    text = p.read_text(encoding="utf-8")
    assert text.count("AURA Program") == 1


def test_add_alias_groups_by_canonical_slug_not_casing(tmp_path):
    p = tmp_path / "kb_aliases.yaml"
    aliases.add_alias(p, entity_type="project", canonical_name="AURA", alias_name="AURA Program")
    # Different casing of the canonical should fold into the same group
    aliases.add_alias(p, entity_type="project", canonical_name="aura", alias_name="AURA Release")
    a = aliases.load(p)
    assert a.resolve("project", "aura-release").slug == "aura"
    assert a.resolve("project", "aura-program").slug == "aura"


# ---------------------------------------------------------------------------
# linker integration — the payoff: variants attach to canonical at import time
# ---------------------------------------------------------------------------


@pytest.fixture
async def wired(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()
    return layout, db


async def test_linker_applies_declared_alias_from_the_start(wired):
    layout, db = wired
    # Declare the alias BEFORE any entity exists (pre-seeding a fresh import).
    aliases.add_alias(layout.aliases_path, entity_type="project",
                      canonical_name="AURA", alias_name="AURA Program")

    el = EntityLinker(db, layout)  # loads kb_aliases.yaml on construction
    r1 = await el.resolve(name="AURA Program", entity_type="project")
    # Resolves straight to the canonical entity — no aura-program row created.
    assert r1.entity_id == ids.entity_id("project", "aura")
    assert await db.get_entity(ids.entity_id("project", "aura-program")) is None

    # A later mention of the canonical name lands on the same entity.
    r2 = await el.resolve(name="AURA", entity_type="project")
    assert r2.entity_id == r1.entity_id


async def test_linker_without_alias_still_fragments(wired):
    """Sanity: without a declared alias, the variant gets its own entity."""
    layout, db = wired
    el = EntityLinker(db, layout)
    a = await el.resolve(name="AURA", entity_type="project")
    b = await el.resolve(name="AURA Program", entity_type="project")
    assert a.entity_id != b.entity_id
