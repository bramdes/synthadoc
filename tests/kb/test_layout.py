# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Tests for synthadoc.kb.layout."""

from __future__ import annotations

import pytest

from synthadoc.kb.layout import KBLayout, RawSourceImmutableError


def test_ensure_layout_creates_full_tree(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    assert (tmp_path / "kb" / "sources" / "raw" / "meetings").is_dir()
    assert (tmp_path / "kb" / "sources" / "parsed" / "documents").is_dir()
    assert (tmp_path / "kb" / "source_summaries" / "emails").is_dir()
    assert (tmp_path / "kb" / "entities" / "projects").is_dir()
    assert (tmp_path / "kb" / "entities" / "people").is_dir()
    assert (tmp_path / "kb" / "facts" / "topics").is_dir()
    assert (tmp_path / "kb" / "conclusions" / "requirements").is_dir()
    assert (tmp_path / "kb" / "decisions").is_dir()
    assert (tmp_path / "kb" / "unknowns" / "projects").is_dir()
    assert (tmp_path / "kb" / "maintenance" / "reports").is_dir()


def test_ensure_layout_is_idempotent(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    layout.ensure_layout()  # must not raise


def test_source_raw_path(tmp_path):
    layout = KBLayout(tmp_path)
    p = layout.source_raw_path("meeting_transcript", "2026-05-22-doc-intel.md")
    assert p == (tmp_path / "kb" / "sources" / "raw" / "meetings"
                 / "2026-05-22-doc-intel.md").resolve()


def test_source_parsed_path(tmp_path):
    layout = KBLayout(tmp_path)
    p = layout.source_parsed_path("document", "brd-v1.md")
    assert "parsed" in p.parts and "documents" in p.parts


def test_entity_paths(tmp_path):
    layout = KBLayout(tmp_path)
    assert layout.entity_index_path("project", "doc-intel").name == "index.md"
    assert layout.entity_history_path("person", "pravin").name == "history.md"
    assert "people" in layout.entity_history_path("person", "pravin").parts


def test_fact_path(tmp_path):
    layout = KBLayout(tmp_path)
    p = layout.fact_path("project", "doc-intel", "project.status",
                         "2026-05-22", "completed")
    assert p.name == "2026-05-22-completed.md"
    assert "project.status" in p.parts


def test_decision_path(tmp_path):
    layout = KBLayout(tmp_path)
    p = layout.decision_path("2026-05-22", "drop1-complete")
    assert p.name == "2026-05-22-drop1-complete.md"


def test_unknown_path(tmp_path):
    layout = KBLayout(tmp_path)
    p = layout.unknown_path("project", "doc-intel", "production-rollout")
    assert p.name == "production-rollout.md"
    assert "doc-intel" in p.parts


def test_db_and_config_paths(tmp_path):
    layout = KBLayout(tmp_path)
    assert layout.db_path == (tmp_path / ".synthadoc" / "kb.db").resolve() or \
           layout.db_path.name == "kb.db"
    assert layout.config_path.name == "kb_config.yaml"


def test_unknown_source_type_raises(tmp_path):
    layout = KBLayout(tmp_path)
    with pytest.raises(ValueError, match="source_type"):
        layout.source_raw_path("video", "x.md")


def test_unknown_entity_type_raises(tmp_path):
    layout = KBLayout(tmp_path)
    with pytest.raises(ValueError, match="entity_type"):
        layout.entity_dir("device", "x")


# ---------------------------------------------------------------------------
# Raw-source immutability guard
# ---------------------------------------------------------------------------


def test_assert_not_raw_source_blocks_writes(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    target = layout.source_raw_path("meeting_transcript", "x.md")
    with pytest.raises(RawSourceImmutableError):
        layout.assert_not_raw_source(target)


def test_assert_not_raw_source_allows_import(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    target = layout.source_raw_path("meeting_transcript", "x.md")
    layout.assert_not_raw_source(target, allow_import=True)  # must not raise


def test_assert_not_raw_source_allows_other_paths(tmp_path):
    layout = KBLayout(tmp_path)
    layout.ensure_layout()
    layout.assert_not_raw_source(layout.entity_index_path("project", "x"))
    layout.assert_not_raw_source(
        layout.fact_path("project", "x", "project.status", "2026-05-22", "v")
    )
    layout.assert_not_raw_source(tmp_path / "wiki" / "anything.md")


def test_assert_not_raw_source_handles_nonexistent_paths(tmp_path):
    """The guard must work before ensure_layout — used by validators in CLI."""
    layout = KBLayout(tmp_path)
    # Even without ensure_layout, the path resolution + comparison should work.
    with pytest.raises(RawSourceImmutableError):
        layout.assert_not_raw_source(
            layout.source_raw_path("meeting_transcript", "any.md")
        )
