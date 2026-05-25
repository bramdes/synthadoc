# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Discriminated frontmatter read/write for the fact-tier files.

Every fact-tier markdown file starts with a YAML frontmatter block that
carries an ``id`` and a ``type`` discriminator. This module is the **only**
code path that reads or writes those headers — every new agent and CLI
command goes through it. This is how we keep the schema from drifting.

The supported types are listed in :data:`SUPPORTED_TYPES` and each has its
own required fields. Unknown types and missing required fields raise a
:class:`FrontmatterError` rather than silently round-tripping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from synthadoc.kb import ids as _ids


# ---------------------------------------------------------------------------
# Type vocabulary
# ---------------------------------------------------------------------------


SUPPORTED_TYPES = frozenset({
    "source",
    "source_summary",
    "entity",
    "fact",
    "derived_conclusion",
    "decision",
    "unknown",
    "history",
    "maintenance_report",
})

REVIEW_STATUSES = frozenset({"unreviewed", "reviewed", "rejected", "needs_clarification"})
CONFIDENCES = frozenset({"high", "medium", "low"})
AUTHORITIES = frozenset({"formal", "informal", "unknown"})


# Required fields per page type. Extra fields are allowed and round-tripped
# verbatim so we don't lose user/agent additions.
_REQUIRED: dict[str, tuple[str, ...]] = {
    "source": ("id", "type", "source_type", "title", "authority",
               "raw_path", "parsed_path", "created_at", "ingested_at", "sha256"),
    "source_summary": ("id", "type", "source_id", "review_status", "confidence"),
    "entity": ("id", "type", "current_state_review_status"),
    "fact": ("id", "type", "entity_id", "fact_type", "value",
             "valid_at", "observed_at", "source_id", "source_path",
             "source_quote", "confidence", "review_status"),
    "derived_conclusion": ("id", "type", "entity_id", "conclusion_type",
                           "value", "confidence", "review_status", "based_on"),
    "decision": ("id", "type", "decision_date", "status", "authority",
                 "review_status", "source_id"),
    "unknown": ("id", "type", "status", "created_at"),
    "history": ("id", "type", "entity_id", "last_rebuilt"),
    "maintenance_report": ("id", "type", "generated_at"),
}


# ---------------------------------------------------------------------------
# Errors and result types
# ---------------------------------------------------------------------------


class FrontmatterError(ValueError):
    """Raised when a frontmatter block fails validation."""


@dataclass
class Frontmatter:
    """Parsed frontmatter + body. ``data`` carries the YAML dict verbatim."""

    data: dict[str, Any]
    body: str

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def type(self) -> str:
        return self.data["type"]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse(text: str) -> Frontmatter:
    """Parse a markdown string with a leading ``---``-fenced YAML block.

    Raises :class:`FrontmatterError` if the block is missing, the YAML
    fails to parse, or the document is missing required fields for its
    declared ``type``.
    """
    if not text.startswith("---"):
        raise FrontmatterError("missing leading '---' frontmatter fence")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise FrontmatterError("missing closing '---' frontmatter fence")
    try:
        data = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"YAML parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise FrontmatterError(
            f"frontmatter must be a mapping, got {type(data).__name__}"
        )
    body = parts[2].lstrip("\n")
    _validate(data)
    return Frontmatter(data=data, body=body)


def read(path: Path) -> Frontmatter:
    """Read and parse the frontmatter at *path*."""
    return parse(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def dump(fm: Frontmatter | dict[str, Any], body: str = "") -> str:
    """Render a Frontmatter (or raw dict + body) to the on-disk string form."""
    if isinstance(fm, Frontmatter):
        data, body_text = fm.data, fm.body
    else:
        data, body_text = fm, body
    _validate(data)
    yaml_str = yaml.dump(data, default_flow_style=False, allow_unicode=True,
                         sort_keys=False)
    return f"---\n{yaml_str}---\n\n{body_text}"


def write(path: Path, fm: Frontmatter | dict[str, Any], body: str = "") -> None:
    """Write the rendered frontmatter+body to *path* (UTF-8, LF newlines)."""
    rendered = dump(fm, body)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# Builders — one per page type. Use these in agents/CLI; never build dicts by
# hand. They enforce the right field set and reject malformed values early.
# ---------------------------------------------------------------------------


def build_source(
    *,
    id: str,
    source_type: str,
    title: str,
    authority: str,
    raw_path: str,
    parsed_path: str,
    created_at: str,
    ingested_at: str,
    sha256: str,
    author: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if source_type not in _ids.SOURCE_TYPES:
        raise FrontmatterError(f"unknown source_type {source_type!r}")
    if authority not in AUTHORITIES:
        raise FrontmatterError(f"unknown authority {authority!r}")
    data: dict[str, Any] = {
        "id": id,
        "type": "source",
        "source_type": source_type,
        "title": title,
        "authority": authority,
        "raw_path": raw_path,
        "parsed_path": parsed_path,
        "created_at": created_at,
        "ingested_at": ingested_at,
        "sha256": sha256,
    }
    if author is not None:
        data["author"] = author
    if extra:
        data.update(extra)
    _validate(data)
    return data


def build_source_summary(
    *,
    id: str,
    source_id: str,
    review_status: str = "unreviewed",
    confidence: str = "medium",
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": id,
        "type": "source_summary",
        "source_id": source_id,
        "review_status": review_status,
        "confidence": confidence,
    }
    if extra:
        data.update(extra)
    _validate(data)
    return data


def build_entity(
    *,
    id: str,
    entity_type: str,
    status: str = "active",
    current_state_review_status: str = "unreviewed",
    last_reviewed: Optional[str] = None,
    last_rebuilt: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if entity_type not in _ids.ENTITY_TYPES:
        raise FrontmatterError(f"unknown entity_type {entity_type!r}")
    data: dict[str, Any] = {
        "id": id,
        "type": "entity",
        "entity_type": entity_type,
        "status": status,
        "current_state_review_status": current_state_review_status,
    }
    if last_reviewed is not None:
        data["last_reviewed"] = last_reviewed
    if last_rebuilt is not None:
        data["last_rebuilt"] = last_rebuilt
    if extra:
        data.update(extra)
    _validate(data)
    return data


def build_fact(
    *,
    id: str,
    entity_id: str,
    fact_type: str,
    value: str,
    valid_at: str,
    observed_at: str,
    source_id: str,
    source_path: str,
    source_quote: str,
    confidence: str,
    review_status: str = "unreviewed",
    value_raw: Optional[str] = None,
    source_span: Optional[str] = None,
    supersedes: Optional[list[str]] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if fact_type not in _ids.FACT_TYPES:
        raise FrontmatterError(f"unknown fact_type {fact_type!r}")
    if confidence not in CONFIDENCES:
        raise FrontmatterError(f"unknown confidence {confidence!r}")
    if review_status not in REVIEW_STATUSES:
        raise FrontmatterError(f"unknown review_status {review_status!r}")
    data: dict[str, Any] = {
        "id": id,
        "type": "fact",
        "entity_id": entity_id,
        "fact_type": fact_type,
        "value": value,
        "valid_at": valid_at,
        "observed_at": observed_at,
        "source_id": source_id,
        "source_path": source_path,
        "source_quote": source_quote,
        "confidence": confidence,
        "review_status": review_status,
    }
    if value_raw is not None:
        data["value_raw"] = value_raw
    if source_span is not None:
        data["source_span"] = source_span
    if supersedes:
        data["supersedes"] = list(supersedes)
    if extra:
        data.update(extra)
    _validate(data)
    return data


def build_decision(
    *,
    id: str,
    decision_date: str,
    status: str,
    authority: str,
    source_id: str,
    entity_id: Optional[str] = None,
    review_status: str = "unreviewed",
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if authority not in AUTHORITIES:
        raise FrontmatterError(f"unknown authority {authority!r}")
    if review_status not in REVIEW_STATUSES:
        raise FrontmatterError(f"unknown review_status {review_status!r}")
    data: dict[str, Any] = {
        "id": id,
        "type": "decision",
        "decision_date": decision_date,
        "status": status,
        "authority": authority,
        "review_status": review_status,
        "source_id": source_id,
    }
    if entity_id is not None:
        data["entity_id"] = entity_id
    if extra:
        data.update(extra)
    _validate(data)
    return data


def build_unknown(
    *,
    id: str,
    created_at: str,
    status: str = "open",
    entity_id: Optional[str] = None,
    resolved_at: Optional[str] = None,
    resolved_by_fact_id: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": id,
        "type": "unknown",
        "status": status,
        "created_at": created_at,
    }
    if entity_id is not None:
        data["entity_id"] = entity_id
    if resolved_at is not None:
        data["resolved_at"] = resolved_at
    if resolved_by_fact_id is not None:
        data["resolved_by_fact_id"] = resolved_by_fact_id
    if extra:
        data.update(extra)
    _validate(data)
    return data


def build_history(
    *,
    id: str,
    entity_id: str,
    last_rebuilt: str,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": id,
        "type": "history",
        "entity_id": entity_id,
        "last_rebuilt": last_rebuilt,
    }
    if extra:
        data.update(extra)
    _validate(data)
    return data


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate(data: dict[str, Any]) -> None:
    t = data.get("type")
    if t not in SUPPORTED_TYPES:
        raise FrontmatterError(
            f"type={t!r} not in supported set {sorted(SUPPORTED_TYPES)!r}"
        )
    required = _REQUIRED.get(t, ())
    missing = [f for f in required if f not in data]
    if missing:
        raise FrontmatterError(
            f"type={t!r} missing required fields {missing!r}"
        )
    # ID structural check (cheap, doesn't re-validate everything kb.ids does)
    if "id" in data and not _ids.is_valid_id(data["id"]):
        raise FrontmatterError(f"invalid id {data['id']!r}")
    # Type-specific cross-checks
    if t == "source_summary":
        if not data["source_id"].startswith("source."):
            raise FrontmatterError(
                f"source_summary.source_id must start with 'source.' "
                f"(got {data['source_id']!r})"
            )
    if t == "fact":
        ent_id = data["entity_id"]
        if not ent_id.startswith("entity."):
            raise FrontmatterError(
                f"fact.entity_id must start with 'entity.' (got {ent_id!r})"
            )
