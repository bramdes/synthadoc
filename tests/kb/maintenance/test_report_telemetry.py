# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for OTel instrumentation around run_all.

`setup_telemetry` is now idempotent — calling it again with a fresh
trace_path *adds* a new exporter to the existing provider, so this test
gets its spans in a per-test jsonl regardless of whatever previous
tests set up.
"""

from __future__ import annotations

import json

import pytest

from synthadoc.kb.db import KBDB
from synthadoc.kb.layout import KBLayout
from synthadoc.kb.maintenance.report import run_all
from synthadoc.observability.telemetry import setup_telemetry


async def test_run_all_emits_per_job_spans_with_count_attributes(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    db = KBDB(layout.db_path)
    await db.init()

    traces = tmp_path / "traces.jsonl"
    setup_telemetry(traces)

    await run_all(db, layout)
    assert traces.exists(), "OTel exporter did not write to the configured jsonl"

    spans = [json.loads(line) for line in traces.read_text().splitlines() if line.strip()]
    span_names = {s["name"] for s in spans}

    expected = {
        "kb.maintenance.contradictions",
        "kb.maintenance.stale",
        "kb.maintenance.orphan_facts",
        "kb.maintenance.facts_without_evidence",
        "kb.maintenance.conclusions_without_facts",
        "kb.maintenance.duplicates",
        "kb.maintenance.broken_links",
        "kb.maintenance.people_safety",
        "kb.maintenance.history_render",
    }
    missing = expected - span_names
    assert not missing, f"missing OTel spans: {missing}"

    by_name = {s["name"]: s for s in spans}

    # Per-job spans (the ones routed through `_safely`) attach a `count`
    # attribute when the underlying result has one.
    assert by_name["kb.maintenance.contradictions"]["attributes"].get("count") == 0
    assert by_name["kb.maintenance.broken_links"]["attributes"].get("count") == 0

    # The history-render span uses a different attribute name because it
    # rolls up across all entities.
    assert by_name["kb.maintenance.history_render"]["attributes"].get(
        "histories_rendered"
    ) == 0
