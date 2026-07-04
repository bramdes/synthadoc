# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""Stable ID generation and validation for the temporal KB tier.

Every fact-tier object — source, source_summary, entity, fact, conclusion,
decision, unknown, history — carries an ID generated through this module.
No other code path may concatenate ID strings directly.

ID shapes (see plan §4):

    source.<source_type>.<YYYY-MM-DD>.<slug>
    summary.source.<source_type>.<YYYY-MM-DD>.<slug>
    entity.<entity_type>.<slug>
    fact.<entity_type>.<entity_slug>.<fact_type>.<YYYY-MM-DD>
    conclusion.<entity_type>.<entity_slug>.<conclusion_type>.<YYYY-MM-DD>
    decision.<YYYY-MM-DD>.<slug>
    unknown.<entity_type>.<entity_slug>.<slug>
    history.<entity_type>.<entity_slug>

All IDs are lowercase ASCII, dot-separated. Slugs are kebab-case.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Vocabularies — closed for v0.3 per plan §14 Q3
# ---------------------------------------------------------------------------

SOURCE_TYPES = frozenset({
    "meeting_transcript",
    "document",
    "email",
    "deck",
    "note",
})

ENTITY_TYPES = frozenset({
    "project",
    "person",
    "topic",
    "requirement",
})

# Spec §4.3, repeated here so the validator is self-contained.
FACT_TYPES = frozenset({
    "project.status",
    "project.owner",
    "project.scope",
    "project.milestone",
    "person.role_on_project",
    "topic.definition",
    "requirement.coverage",
    "decision.made",
    "open_issue.created",
    "open_issue.closed",
    "assumption.created",
    "assumption.invalidated",
})

# Short alias used inside source / summary IDs (folder names too).
_SOURCE_TYPE_ALIAS: dict[str, str] = {
    "meeting_transcript": "meeting",
    "document": "document",
    "email": "email",
    "deck": "deck",
    "note": "note",
}

# Slug character class — ASCII alphanumeric + a handful of CJK ranges, matching
# the existing _slugify in agents/ingest_agent.py. Keeping this consistent so a
# wiki page slug and a fact-tier slug derived from the same title produce the
# same string.
_SLUG_CHARS_RE = re.compile(
    r"[^a-z0-9一-鿿぀-ゟ゠-ヿ가-힯]+"
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SLUG_MAX_LEN = 64


# ---------------------------------------------------------------------------
# Slug normalisation
# ---------------------------------------------------------------------------


def slugify(text: str) -> str:
    """Normalise *text* to a kebab-case slug.

    NFKD-decomposes accented characters and drops combining marks so e.g.
    ``"Café Münster"`` becomes ``"cafe-munster"`` rather than
    ``"cafe-mu-nster"``. Lowercases, replaces runs of non-slug characters
    with a single dash, trims leading/trailing dashes, and caps length at
    64. Falls back to a content-hash slug if the result would be empty.
    """
    if not text:
        raise ValueError("slugify requires a non-empty string")
    normalized = unicodedata.normalize("NFKD", text)
    # Drop combining marks so decomposed accents disappear instead of becoming dashes.
    stripped = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    slug = _SLUG_CHARS_RE.sub("-", stripped.lower()).strip("-")
    if not slug:
        slug = "x-" + hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
    return slug[:_SLUG_MAX_LEN].rstrip("-") or slug[:_SLUG_MAX_LEN]


def slug_with_suffix(base_slug: str, n: int) -> str:
    """Append a numeric collision suffix ``-<n>`` to *base_slug*, keeping the
    result a valid slug within the length cap.

    ``slugify`` caps slugs at :data:`_SLUG_MAX_LEN`, so naively gluing ``-2``
    onto a slug that is already at the cap yields a 65+ char string that is no
    longer idempotent under ``slugify`` — every downstream ``_check_slug`` on
    it then raises ``slug must already be a valid slug``. This trims the base
    to leave room for the suffix instead, so the result always round-trips.
    """
    if n < 1:
        raise ValueError(f"collision suffix n must be >= 1 (got {n})")
    suffix = f"-{n}"
    if len(base_slug) + len(suffix) > _SLUG_MAX_LEN:
        base_slug = base_slug[: _SLUG_MAX_LEN - len(suffix)].rstrip("-")
    return f"{base_slug}{suffix}"


def _check_date(value: str, field: str) -> None:
    if not _DATE_RE.match(value):
        raise ValueError(f"{field} must be YYYY-MM-DD (got {value!r})")


def _check_in(value: str, allowed: frozenset[str], field: str) -> None:
    if value not in allowed:
        raise ValueError(
            f"{field}={value!r} not in allowed set {sorted(allowed)!r}"
        )


def _check_slug(value: str, field: str) -> None:
    if not value or value != slugify(value):
        raise ValueError(
            f"{field} must already be a valid slug (got {value!r}); "
            f"call slugify() first"
        )


# ---------------------------------------------------------------------------
# ID generators — pure, deterministic
# ---------------------------------------------------------------------------


def source_id(source_type: str, valid_on: str | date, slug: str) -> str:
    """``source.<alias>.<YYYY-MM-DD>.<slug>``"""
    _check_in(source_type, SOURCE_TYPES, "source_type")
    iso = valid_on.isoformat() if isinstance(valid_on, date) else valid_on
    _check_date(iso, "valid_on")
    _check_slug(slug, "slug")
    return f"source.{_SOURCE_TYPE_ALIAS[source_type]}.{iso}.{slug}"


def summary_source_id(src_id: str) -> str:
    """``summary.<source_id>``"""
    if not src_id.startswith("source."):
        raise ValueError(f"src_id must start with 'source.' (got {src_id!r})")
    return f"summary.{src_id}"


def entity_id(entity_type: str, slug: str) -> str:
    """``entity.<entity_type>.<slug>``"""
    _check_in(entity_type, ENTITY_TYPES, "entity_type")
    _check_slug(slug, "slug")
    return f"entity.{entity_type}.{slug}"


def fact_id(ent_id: str, fact_type: str, valid_at: str | date) -> str:
    """``fact.<entity_type>.<slug>.<fact_type>.<YYYY-MM-DD>``"""
    et, es = _split_entity_id(ent_id)
    _check_in(fact_type, FACT_TYPES, "fact_type")
    iso = valid_at.isoformat() if isinstance(valid_at, date) else valid_at
    _check_date(iso, "valid_at")
    return f"fact.{et}.{es}.{fact_type}.{iso}"


def conclusion_id(ent_id: str, conclusion_type: str, generated_on: str | date) -> str:
    """``conclusion.<entity_type>.<slug>.<conclusion_type>.<YYYY-MM-DD>``

    ``conclusion_type`` is an open string (no fixed vocab — conclusions are
    derived and varied). It must be dot-free lowercase alphanumeric + dashes
    so it doesn't collide with the ID delimiter.
    """
    et, es = _split_entity_id(ent_id)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", conclusion_type):
        raise ValueError(
            f"conclusion_type must be lowercase alphanumeric/dash/underscore "
            f"(got {conclusion_type!r})"
        )
    iso = generated_on.isoformat() if isinstance(generated_on, date) else generated_on
    _check_date(iso, "generated_on")
    return f"conclusion.{et}.{es}.{conclusion_type}.{iso}"


def decision_id(decision_date: str | date, slug: str) -> str:
    """``decision.<YYYY-MM-DD>.<slug>``"""
    iso = decision_date.isoformat() if isinstance(decision_date, date) else decision_date
    _check_date(iso, "decision_date")
    _check_slug(slug, "slug")
    return f"decision.{iso}.{slug}"


def unknown_id(ent_id: str, slug: str) -> str:
    """``unknown.<entity_type>.<entity_slug>.<slug>``"""
    et, es = _split_entity_id(ent_id)
    _check_slug(slug, "slug")
    return f"unknown.{et}.{es}.{slug}"


def history_id(ent_id: str) -> str:
    """``history.<entity_type>.<entity_slug>``"""
    et, es = _split_entity_id(ent_id)
    return f"history.{et}.{es}"


# ---------------------------------------------------------------------------
# Collision suffix
# ---------------------------------------------------------------------------


def with_collision_suffix(base: str, is_taken: Callable[[str], bool]) -> str:
    """Return *base*, or ``base-2``, ``base-3``, … until ``is_taken`` returns False.

    Callers provide the existence check (typically a DB lookup) so this module
    stays free of DB knowledge. The suffix is appended to the final dotted
    segment with a dash, preserving the parseable shape.
    """
    if not is_taken(base):
        return base
    for n in range(2, 1000):
        candidate = f"{base}-{n}"
        if not is_taken(candidate):
            return candidate
    raise RuntimeError(f"too many collisions for ID base {base!r}")


# ---------------------------------------------------------------------------
# Validators (used by frontmatter.py and the migration scripts)
# ---------------------------------------------------------------------------


_ID_PREFIXES = ("source.", "summary.source.", "entity.", "fact.", "conclusion.",
                "decision.", "unknown.", "history.")


def is_valid_id(value: str) -> bool:
    """Cheap structural check — does *value* look like one of our IDs?"""
    if not isinstance(value, str) or not value:
        return False
    return any(value.startswith(p) for p in _ID_PREFIXES)


def id_kind(value: str) -> str:
    """Return the prefix kind (``source``, ``summary``, ``entity``, …)."""
    for p in _ID_PREFIXES:
        if value.startswith(p):
            return p.rstrip(".").split(".")[0]
    raise ValueError(f"not a recognised KB id: {value!r}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _split_entity_id(ent_id: str) -> tuple[str, str]:
    """Return (entity_type, entity_slug) from an ``entity.<type>.<slug>`` ID."""
    if not ent_id.startswith("entity."):
        raise ValueError(f"expected entity.* id (got {ent_id!r})")
    parts = ent_id.split(".", 2)
    if len(parts) != 3:
        raise ValueError(f"malformed entity id {ent_id!r}")
    _check_in(parts[1], ENTITY_TYPES, "entity_type")
    _check_slug(parts[2], "entity_slug")
    return parts[1], parts[2]


def source_type_alias(source_type: str) -> str:
    """Public accessor for the folder-name alias of a source_type."""
    _check_in(source_type, SOURCE_TYPES, "source_type")
    return _SOURCE_TYPE_ALIAS[source_type]


def split_source_id(src_id: str) -> tuple[str, str, str]:
    """Reverse of :func:`source_id`. Returns (source_type, YYYY-MM-DD, slug).

    Raises ``ValueError`` if the ID is malformed.
    """
    if not src_id.startswith("source."):
        raise ValueError(f"not a source id: {src_id!r}")
    body = src_id[len("source."):]
    parts = body.split(".", 2)
    if len(parts) != 3:
        raise ValueError(f"malformed source id {src_id!r}")
    alias, iso, slug = parts
    _check_date(iso, "date")
    _check_slug(slug, "slug")
    inverse = {v: k for k, v in _SOURCE_TYPE_ALIAS.items()}
    if alias not in inverse:
        raise ValueError(f"unknown source alias {alias!r} in {src_id!r}")
    return inverse[alias], iso, slug


def optional_resolved(value: Optional[str | date]) -> Optional[str]:
    """Coerce a date|str|None into an ISO string or None. Useful for callers."""
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    _check_date(value, "date")
    return value
