# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Folder layout constants, path helpers, and the raw-source write guard.

Plan §5 — the fact tier lives entirely under ``<wiki_root>/kb/``, and the
``kb/sources/raw/`` subtree is immutable after the initial import. This module
centralises that knowledge so callers never compose paths by hand and the
immutability guard has exactly one enforcement point.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from synthadoc.kb import ids as _ids


# ---------------------------------------------------------------------------
# Folder constants (relative to wiki root)
# ---------------------------------------------------------------------------

KB_DIR = "kb"
SOURCES_DIR = "kb/sources"
SOURCES_RAW_DIR = "kb/sources/raw"
SOURCES_PARSED_DIR = "kb/sources/parsed"
SOURCE_SUMMARIES_DIR = "kb/source_summaries"
ENTITIES_DIR = "kb/entities"
FACTS_DIR = "kb/facts"
CONCLUSIONS_DIR = "kb/conclusions"
DECISIONS_DIR = "kb/decisions"
UNKNOWNS_DIR = "kb/unknowns"
MAINTENANCE_DIR = "kb/maintenance"
MAINTENANCE_REPORTS_DIR = "kb/maintenance/reports"

KB_DB_PATH = ".synthadoc/kb.db"
KB_CONFIG_PATH = "kb_config.yaml"
# Version-controllable entity alias map (sibling of kb_config.yaml). Lives at
# the wiki root — NOT under .synthadoc/ — so it can be committed alongside the
# wiki and survives a reset/re-import.
KB_ALIASES_PATH = "kb_aliases.yaml"

# Per-source-type subfolder names. Mirrors the alias used in source IDs so
# folder names match the human-visible ID.
_SOURCE_SUBDIR = {
    "meeting_transcript": "meetings",
    "document": "documents",
    "email": "emails",
    "deck": "decks",
    "note": "notes",
}

# Entity-type → subfolder under kb/entities/, kb/facts/, etc. Plural for
# human readability (matches plan §5).
_ENTITY_SUBDIR = {
    "project": "projects",
    "person": "people",
    "topic": "topics",
    "requirement": "requirements",
}


class RawSourceImmutableError(PermissionError):
    """Raised when a write is attempted under ``kb/sources/raw/`` outside of import."""


# ---------------------------------------------------------------------------
# Layout helper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KBLayout:
    """All path helpers for one wiki root.

    Instantiate once per wiki; pass it down rather than re-deriving paths.
    """

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())

    # --- top-level dirs ---

    @property
    def kb(self) -> Path:
        return self.root / KB_DIR

    @property
    def db_path(self) -> Path:
        return self.root / KB_DB_PATH

    @property
    def config_path(self) -> Path:
        return self.root / KB_CONFIG_PATH

    @property
    def aliases_path(self) -> Path:
        return self.root / KB_ALIASES_PATH

    @property
    def maintenance_dir(self) -> Path:
        return self.root / MAINTENANCE_DIR

    @property
    def reports_dir(self) -> Path:
        return self.root / MAINTENANCE_REPORTS_DIR

    # --- source paths ---

    def source_raw_dir(self, source_type: str) -> Path:
        sub = _source_subdir(source_type)
        return self.root / SOURCES_RAW_DIR / sub

    def source_parsed_dir(self, source_type: str) -> Path:
        sub = _source_subdir(source_type)
        return self.root / SOURCES_PARSED_DIR / sub

    def source_summary_dir(self, source_type: str) -> Path:
        sub = _source_subdir(source_type)
        return self.root / SOURCE_SUMMARIES_DIR / sub

    def source_raw_path(self, source_type: str, filename: str) -> Path:
        return self.source_raw_dir(source_type) / filename

    def source_parsed_path(self, source_type: str, filename: str) -> Path:
        return self.source_parsed_dir(source_type) / filename

    # --- entity paths ---

    def entity_dir(self, entity_type: str, slug: str) -> Path:
        sub = _entity_subdir(entity_type)
        return self.root / ENTITIES_DIR / sub / slug

    def entity_index_path(self, entity_type: str, slug: str) -> Path:
        return self.entity_dir(entity_type, slug) / "index.md"

    def entity_history_path(self, entity_type: str, slug: str) -> Path:
        return self.entity_dir(entity_type, slug) / "history.md"

    # --- fact paths ---

    def fact_dir(self, entity_type: str, entity_slug: str, fact_type: str) -> Path:
        sub = _entity_subdir(entity_type)
        return self.root / FACTS_DIR / sub / entity_slug / fact_type

    def fact_path(
        self,
        entity_type: str,
        entity_slug: str,
        fact_type: str,
        valid_at: str,
        value_slug: str,
    ) -> Path:
        return self.fact_dir(entity_type, entity_slug, fact_type) / (
            f"{valid_at}-{value_slug}.md"
        )

    # --- decision / unknown paths ---

    def decision_path(self, decision_date: str, slug: str) -> Path:
        return self.root / DECISIONS_DIR / f"{decision_date}-{slug}.md"

    def unknown_path(self, entity_type: str, entity_slug: str, slug: str) -> Path:
        sub = _entity_subdir(entity_type)
        return self.root / UNKNOWNS_DIR / sub / entity_slug / f"{slug}.md"

    # --- guards ---

    def assert_not_raw_source(self, path: Path, *, allow_import: bool = False) -> None:
        """Refuse to write under ``kb/sources/raw/`` unless ``allow_import=True``.

        The only legitimate writer of raw-source files is the import flow
        (``synthadoc kb import-source``). Every other code path — agents,
        maintenance jobs, consolidate, scaffold — must leave raw sources alone.
        """
        if allow_import:
            return
        try:
            resolved = Path(path).resolve()
        except OSError:
            resolved = Path(path)
        raw_root = (self.root / SOURCES_RAW_DIR).resolve()
        try:
            resolved.relative_to(raw_root)
        except ValueError:
            return
        raise RawSourceImmutableError(
            f"refusing to write under {raw_root} — raw sources are immutable. "
            f"If this is the import flow, pass allow_import=True."
        )

    def ensure_layout(self) -> None:
        """Create every top-level KB folder. Idempotent."""
        for rel in (
            SOURCES_RAW_DIR, SOURCES_PARSED_DIR, SOURCE_SUMMARIES_DIR,
            ENTITIES_DIR, FACTS_DIR, CONCLUSIONS_DIR, DECISIONS_DIR,
            UNKNOWNS_DIR, MAINTENANCE_DIR, MAINTENANCE_REPORTS_DIR,
        ):
            (self.root / rel).mkdir(parents=True, exist_ok=True)
        # Per-source-type subfolders for raw + parsed + summaries
        for st in _SOURCE_SUBDIR.values():
            (self.root / SOURCES_RAW_DIR / st).mkdir(parents=True, exist_ok=True)
            (self.root / SOURCES_PARSED_DIR / st).mkdir(parents=True, exist_ok=True)
            (self.root / SOURCE_SUMMARIES_DIR / st).mkdir(parents=True, exist_ok=True)
        # Per-entity-type subfolders for entities / facts / conclusions / unknowns
        for et in _ENTITY_SUBDIR.values():
            (self.root / ENTITIES_DIR / et).mkdir(parents=True, exist_ok=True)
            (self.root / FACTS_DIR / et).mkdir(parents=True, exist_ok=True)
            (self.root / CONCLUSIONS_DIR / et).mkdir(parents=True, exist_ok=True)
            (self.root / UNKNOWNS_DIR / et).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _source_subdir(source_type: str) -> str:
    if source_type not in _SOURCE_SUBDIR:
        raise ValueError(
            f"unknown source_type {source_type!r}; "
            f"valid: {sorted(_ids.SOURCE_TYPES)!r}"
        )
    return _SOURCE_SUBDIR[source_type]


def _entity_subdir(entity_type: str) -> str:
    if entity_type not in _ENTITY_SUBDIR:
        raise ValueError(
            f"unknown entity_type {entity_type!r}; "
            f"valid: {sorted(_ids.ENTITY_TYPES)!r}"
        )
    return _ENTITY_SUBDIR[entity_type]
