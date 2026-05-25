# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for synthadoc.kb.db."""

from __future__ import annotations

import sqlite3

import pytest

from synthadoc.kb import ids
from synthadoc.kb.db import KBDB, SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    d = KBDB(tmp_path / "kb.db")
    await d.init()
    return d


def _source_kwargs(**overrides):
    base = dict(
        id=ids.source_id("meeting_transcript", "2026-05-22", "doc-intel-update"),
        source_type="meeting_transcript",
        title="Doc Intel Update",
        authority="informal",
        raw_path="kb/sources/raw/meetings/x.md",
        parsed_path="kb/sources/parsed/meetings/x.md",
        created_at="2026-05-22T10:00:00+08:00",
        ingested_at="2026-05-23T09:00:00+08:00",
        sha256="hash-1",
    )
    base.update(overrides)
    return base


def _entity_kwargs(**overrides):
    eid = ids.entity_id("project", "doc-intel")
    base = dict(
        id=eid,
        entity_type="project",
        name="Document Intelligence",
        slug="doc-intel",
        path="kb/entities/projects/doc-intel/index.md",
    )
    base.update(overrides)
    return base


def _fact_kwargs(eid: str, src_id: str, valid_at: str, value: str, **overrides):
    fid = ids.fact_id(eid, "project.status", valid_at)
    base = dict(
        id=fid,
        entity_id=eid,
        fact_type="project.status",
        value=value,
        valid_at=valid_at,
        observed_at="2026-05-23T09:00:00+08:00",
        source_id=src_id,
        source_path="kb/sources/parsed/meetings/x.md",
        source_quote="Drop 1 is now complete.",
        confidence="high",
        path=f"kb/facts/projects/doc-intel/project.status/{valid_at}-{value}.md",
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_init_is_idempotent(tmp_path):
    d = KBDB(tmp_path / "kb.db")
    await d.init()
    await d.init()
    assert (await d.schema_version()) == SCHEMA_VERSION


async def test_schema_version_recorded(db):
    assert (await db.schema_version()) == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


async def test_insert_and_get_source(db):
    kw = _source_kwargs()
    await db.insert_source(**kw)
    got = await db.get_source(kw["id"])
    assert got["title"] == "Doc Intel Update"
    assert got["sha256"] == "hash-1"


async def test_find_source_by_sha256(db):
    kw = _source_kwargs()
    await db.insert_source(**kw)
    got = await db.find_source_by_sha256("hash-1")
    assert got is not None and got["id"] == kw["id"]
    assert (await db.find_source_by_sha256("missing")) is None


async def test_duplicate_sha256_rejected(db):
    await db.insert_source(**_source_kwargs())
    with pytest.raises(sqlite3.IntegrityError):
        await db.insert_source(**_source_kwargs(
            id=ids.source_id("meeting_transcript", "2026-05-22", "other"),
        ))


async def test_source_exists(db):
    kw = _source_kwargs()
    assert not await db.source_exists(kw["id"])
    await db.insert_source(**kw)
    assert await db.source_exists(kw["id"])


async def test_list_sources_orders_by_ingest_time(db):
    await db.insert_source(**_source_kwargs(
        id=ids.source_id("meeting_transcript", "2026-05-01", "a"),
        sha256="h-a", ingested_at="2026-05-01T00:00:00Z",
    ))
    await db.insert_source(**_source_kwargs(
        id=ids.source_id("meeting_transcript", "2026-05-02", "b"),
        sha256="h-b", ingested_at="2026-05-02T00:00:00Z",
    ))
    sources = await db.list_sources()
    assert [s["sha256"] for s in sources] == ["h-a", "h-b"]


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


async def test_upsert_entity_inserts(db):
    kw = _entity_kwargs()
    await db.upsert_entity(**kw)
    got = await db.get_entity(kw["id"])
    assert got["name"] == "Document Intelligence"
    assert got["current_state_review_status"] == "unreviewed"


async def test_upsert_entity_updates_on_conflict(db):
    await db.upsert_entity(**_entity_kwargs())
    await db.upsert_entity(**_entity_kwargs(name="DocIntel"))
    got = await db.get_entity(_entity_kwargs()["id"])
    assert got["name"] == "DocIntel"


async def test_list_entities_filters_by_type(db):
    await db.upsert_entity(**_entity_kwargs())
    await db.upsert_entity(
        id=ids.entity_id("person", "pravin"),
        entity_type="person",
        name="Pravin",
        slug="pravin",
        path="kb/entities/people/pravin/index.md",
    )
    assert len(await db.list_entities("project")) == 1
    assert len(await db.list_entities("person")) == 1
    assert len(await db.list_entities()) == 2


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


async def test_insert_fact(db):
    src_kw = _source_kwargs()
    await db.insert_source(**src_kw)
    ent_kw = _entity_kwargs()
    await db.upsert_entity(**ent_kw)
    await db.insert_fact(**_fact_kwargs(
        eid=ent_kw["id"], src_id=src_kw["id"],
        valid_at="2026-05-22", value="completed",
    ))
    facts = await db.list_facts()
    assert len(facts) == 1
    assert facts[0]["value"] == "completed"
    assert facts[0]["superseded_by"] is None


async def test_fact_with_unknown_entity_rejected(db):
    src_kw = _source_kwargs()
    await db.insert_source(**src_kw)
    with pytest.raises(sqlite3.IntegrityError):
        await db.insert_fact(**_fact_kwargs(
            eid=ids.entity_id("project", "ghost"),
            src_id=src_kw["id"],
            valid_at="2026-05-22", value="x",
        ))


async def test_mark_superseded_writes_both_columns(db):
    src_kw = _source_kwargs()
    ent_kw = _entity_kwargs()
    await db.insert_source(**src_kw)
    await db.upsert_entity(**ent_kw)

    old = _fact_kwargs(eid=ent_kw["id"], src_id=src_kw["id"],
                       valid_at="2026-05-01", value="in_progress")
    new = _fact_kwargs(eid=ent_kw["id"], src_id=src_kw["id"],
                       valid_at="2026-05-22", value="completed")
    await db.insert_fact(**old)
    await db.insert_fact(**new)

    await db.mark_superseded(old["id"], new["id"])

    old_row = await db.get_fact(old["id"])
    assert old_row["superseded_by"] == new["id"]

    rows = await db.fetchall("SELECT * FROM fact_supersession")
    assert len(rows) == 1
    assert rows[0]["superseded_id"] == old["id"]
    assert rows[0]["superseder_id"] == new["id"]


async def test_mark_superseded_is_idempotent(db):
    src_kw = _source_kwargs()
    ent_kw = _entity_kwargs()
    await db.insert_source(**src_kw)
    await db.upsert_entity(**ent_kw)

    a = _fact_kwargs(eid=ent_kw["id"], src_id=src_kw["id"],
                     valid_at="2026-05-01", value="a")
    b = _fact_kwargs(eid=ent_kw["id"], src_id=src_kw["id"],
                     valid_at="2026-05-22", value="b")
    await db.insert_fact(**a)
    await db.insert_fact(**b)

    await db.mark_superseded(a["id"], b["id"])
    await db.mark_superseded(a["id"], b["id"])  # second call should not crash
    rows = await db.fetchall("SELECT * FROM fact_supersession")
    assert len(rows) == 1


async def test_self_supersession_rejected(db):
    with pytest.raises(ValueError):
        await db.mark_superseded("fact.x", "fact.x")


async def test_list_facts_active_only(db):
    src_kw = _source_kwargs()
    ent_kw = _entity_kwargs()
    await db.insert_source(**src_kw)
    await db.upsert_entity(**ent_kw)

    a = _fact_kwargs(eid=ent_kw["id"], src_id=src_kw["id"],
                     valid_at="2026-05-01", value="in_progress")
    b = _fact_kwargs(eid=ent_kw["id"], src_id=src_kw["id"],
                     valid_at="2026-05-22", value="completed")
    await db.insert_fact(**a)
    await db.insert_fact(**b)
    await db.mark_superseded(a["id"], b["id"])

    all_facts = await db.list_facts()
    active = await db.list_facts(active_only=True)
    assert len(all_facts) == 2
    assert len(active) == 1
    assert active[0]["value"] == "completed"


async def test_count_facts(db):
    assert (await db.count_facts()) == 0
    src_kw = _source_kwargs()
    ent_kw = _entity_kwargs()
    await db.insert_source(**src_kw)
    await db.upsert_entity(**ent_kw)
    await db.insert_fact(**_fact_kwargs(
        eid=ent_kw["id"], src_id=src_kw["id"],
        valid_at="2026-05-22", value="completed",
    ))
    assert (await db.count_facts()) == 1
