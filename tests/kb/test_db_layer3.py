# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for the Layer 3 KBDB helpers (decisions, unknowns, conclusions)."""

from __future__ import annotations

import pytest

from synthadoc.kb import ids
from synthadoc.kb.db import KBDB


@pytest.fixture
async def db(tmp_path):
    d = KBDB(tmp_path / "kb.db")
    await d.init()
    src_id = ids.source_id("meeting_transcript", "2026-05-22", "x")
    await d.insert_source(
        id=src_id, source_type="meeting_transcript", title="x",
        authority="informal", raw_path="r", parsed_path="p",
        created_at="2026-05-22T10:00:00Z",
        ingested_at="2026-05-23T09:00:00Z",
        sha256="h-1",
    )
    eid = ids.entity_id("project", "x")
    await d.upsert_entity(
        id=eid, entity_type="project", name="X", slug="x",
        path="kb/entities/projects/x/index.md",
    )
    return d, src_id, eid


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


async def test_insert_and_get_decision(db):
    d, src, eid = db
    did = ids.decision_id("2026-05-22", "drop1-complete")
    await d.insert_decision(
        id=did, decision_date="2026-05-22", status="active",
        authority="informal", source_id=src, path="kb/decisions/x.md",
        entity_id=eid,
    )
    got = await d.get_decision(did)
    assert got["id"] == did
    assert got["entity_id"] == eid


async def test_list_decisions_filters_by_source(db):
    d, src, eid = db
    await d.insert_decision(
        id=ids.decision_id("2026-05-22", "a"),
        decision_date="2026-05-22", status="active", authority="informal",
        source_id=src, path="kb/decisions/a.md", entity_id=eid,
    )
    rows = await d.list_decisions(source_id=src)
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Unknowns
# ---------------------------------------------------------------------------


async def test_insert_and_get_unknown(db):
    d, _, eid = db
    uid = ids.unknown_id(eid, "production-rollout")
    await d.insert_unknown(
        id=uid, created_at="2026-05-23", path="kb/unknowns/x.md",
        entity_id=eid,
    )
    got = await d.get_unknown(uid)
    assert got["status"] == "open"
    assert got["entity_id"] == eid


async def test_list_unknowns_filter_by_status(db):
    d, _, eid = db
    open_id = ids.unknown_id(eid, "a")
    resolved_id = ids.unknown_id(eid, "b")
    await d.insert_unknown(id=open_id, created_at="2026-05-23",
                           path="x.md", entity_id=eid, status="open")
    await d.insert_unknown(id=resolved_id, created_at="2026-05-23",
                           path="y.md", entity_id=eid, status="resolved")
    assert len(await d.list_unknowns(status="open")) == 1
    assert len(await d.list_unknowns(status="resolved")) == 1
    assert len(await d.list_unknowns()) == 2


# ---------------------------------------------------------------------------
# Conclusions + basis (transactional)
# ---------------------------------------------------------------------------


async def test_insert_conclusion_with_basis(db):
    d, src, eid = db
    # Need a fact for the basis FK
    fid = ids.fact_id(eid, "project.status", "2026-05-22")
    await d.insert_fact(
        id=fid, entity_id=eid, fact_type="project.status",
        value="completed", valid_at="2026-05-22",
        observed_at="2026-05-23T09:00:00Z",
        source_id=src, source_path="p", source_quote="ok",
        confidence="high", path="kb/facts/x.md",
    )
    cid = "conclusion.project.x.readiness.2026-05-23"
    await d.insert_conclusion(
        id=cid, entity_id=eid, conclusion_type="readiness",
        value="not_confirmed", confidence="medium",
        path="kb/conclusions/x.md",
        generated_at="2026-05-23T10:00:00Z",
        reasoning="Status completed but rollout unconfirmed.",
        based_on=(fid,),
    )
    rows = await d.fetchall("SELECT * FROM conclusions WHERE id = ?", (cid,))
    assert len(rows) == 1
    basis = await d.fetchall("SELECT * FROM conclusion_basis WHERE conclusion_id = ?", (cid,))
    assert len(basis) == 1
    assert basis[0]["fact_id"] == fid


async def test_insert_conclusion_rejects_unknown_fact(db):
    d, _, eid = db
    with pytest.raises(Exception):
        await d.insert_conclusion(
            id="conclusion.project.x.foo.2026-05-23",
            entity_id=eid, conclusion_type="foo", value="bar",
            confidence="high", path="x.md",
            generated_at="2026-05-23T10:00:00Z",
            based_on=("fact.project.x.project.status.2099-01-01",),  # not present
        )
