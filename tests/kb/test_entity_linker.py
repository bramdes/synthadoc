# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for synthadoc.kb.entity_linker."""

from __future__ import annotations

import pytest

from synthadoc.kb import ids
from synthadoc.kb.db import KBDB
from synthadoc.kb.entity_linker import EntityLinker
from synthadoc.kb.layout import KBLayout


@pytest.fixture
async def linker(tmp_path):
    db = KBDB(tmp_path / "kb.db")
    await db.init()
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    return EntityLinker(db, layout), db


async def test_resolve_creates_new_entity(linker):
    el, db = linker
    result = await el.resolve(name="Document Intelligence", entity_type="project")
    assert result.created is True
    assert result.entity_id == ids.entity_id("project", "document-intelligence")
    row = await db.get_entity(result.entity_id)
    assert row["name"] == "Document Intelligence"
    assert row["slug"] == "document-intelligence"
    assert row["path"].endswith("/index.md")


async def test_resolve_returns_existing(linker):
    el, _ = linker
    first = await el.resolve(name="Document Intelligence", entity_type="project")
    second = await el.resolve(name="Document Intelligence", entity_type="project")
    assert second.entity_id == first.entity_id
    assert second.created is False


async def test_resolve_treats_case_and_accents_as_same(linker):
    el, _ = linker
    a = await el.resolve(name="Document Intelligence", entity_type="project")
    b = await el.resolve(name="document intelligence", entity_type="project")
    # Accent variant — note slugify strips combining marks
    c = await el.resolve(name="Document  Intelligénce", entity_type="project")
    assert a.entity_id == b.entity_id == c.entity_id
    assert b.created is False
    assert c.created is False


async def test_resolve_distinguishes_entity_types(linker):
    el, _ = linker
    proj = await el.resolve(name="Pravin", entity_type="project")
    person = await el.resolve(name="Pravin", entity_type="person")
    assert proj.entity_id != person.entity_id
    assert proj.created is True
    assert person.created is True


async def test_resolve_auto_create_false_returns_none(linker):
    el, _ = linker
    result = await el.resolve(
        name="Ghost Project", entity_type="project", auto_create=False
    )
    assert result is None


async def test_resolve_auto_create_false_returns_existing(linker):
    el, _ = linker
    await el.resolve(name="Doc Intel", entity_type="project")
    result = await el.resolve(
        name="Doc Intel", entity_type="project", auto_create=False
    )
    assert result is not None
    assert result.created is False


async def test_resolve_rejects_unknown_entity_type(linker):
    el, _ = linker
    with pytest.raises(ValueError, match="entity_type"):
        await el.resolve(name="x", entity_type="device")


async def test_resolve_rejects_empty_name(linker):
    el, _ = linker
    with pytest.raises(ValueError, match="non-empty"):
        await el.resolve(name="", entity_type="project")
    with pytest.raises(ValueError, match="non-empty"):
        await el.resolve(name="   ", entity_type="project")


async def test_resolve_many_preserves_order(linker):
    el, _ = linker
    items = [
        ("Project A", "project"),
        ("Pravin", "person"),
        ("Project A", "project"),  # duplicate — second call returns created=False
    ]
    results = await el.resolve_many(items)
    assert len(results) == 3
    assert results[0].created is True
    assert results[1].created is True
    assert results[2].created is False
    assert results[0].entity_id == results[2].entity_id


async def test_resolve_many_skips_when_no_auto_create(linker):
    el, _ = linker
    # Seed one entity
    await el.resolve(name="Known", entity_type="project")
    results = await el.resolve_many(
        [("Known", "project"), ("Unknown", "project")],
        auto_create=False,
    )
    assert len(results) == 1
    assert results[0].entity_id == ids.entity_id("project", "known")
