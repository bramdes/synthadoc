# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Chen / axoviq.com
"""SQLite index for the temporal KB tier.

Schema follows plan §6. ``kb.db`` is a separate file from ``audit.db`` so the
fact tier can be rebuilt independently from the markdown source of truth. If
the DB is lost, scanning the markdown should reproduce it.

Patterns mirror :class:`synthadoc.storage.log.AuditDB`: aiosqlite, init-on-demand,
one connection per call. Migrations are append-only — bump
:data:`SCHEMA_VERSION` and add a numbered block to :func:`_apply_migrations`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite


SCHEMA_VERSION = 1


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS sources (
    id            TEXT PRIMARY KEY,
    source_type   TEXT NOT NULL,
    title         TEXT NOT NULL,
    authority     TEXT NOT NULL,
    raw_path      TEXT,
    parsed_path   TEXT,
    created_at    TEXT NOT NULL,
    ingested_at   TEXT NOT NULL,
    author        TEXT,
    sha256        TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS source_summaries (
    id             TEXT PRIMARY KEY,
    source_id      TEXT NOT NULL REFERENCES sources(id),
    path           TEXT NOT NULL,
    review_status  TEXT NOT NULL DEFAULT 'unreviewed',
    confidence     TEXT NOT NULL DEFAULT 'medium',
    generated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
    id                          TEXT PRIMARY KEY,
    entity_type                 TEXT NOT NULL,
    name                        TEXT NOT NULL,
    slug                        TEXT NOT NULL,
    path                        TEXT NOT NULL,
    status                      TEXT,
    merged_into                 TEXT REFERENCES entities(id),
    current_state_review_status TEXT NOT NULL DEFAULT 'unreviewed',
    last_reviewed               TEXT,
    last_rebuilt                TEXT
);
CREATE INDEX IF NOT EXISTS idx_entities_type_slug ON entities(entity_type, slug);

CREATE TABLE IF NOT EXISTS facts (
    id             TEXT PRIMARY KEY,
    entity_id      TEXT NOT NULL REFERENCES entities(id),
    fact_type      TEXT NOT NULL,
    value          TEXT NOT NULL,
    value_raw      TEXT,
    valid_at       TEXT NOT NULL,
    observed_at    TEXT NOT NULL,
    source_id      TEXT NOT NULL REFERENCES sources(id),
    source_path    TEXT NOT NULL,
    source_span    TEXT,
    source_quote   TEXT NOT NULL,
    confidence     TEXT NOT NULL,
    review_status  TEXT NOT NULL DEFAULT 'unreviewed',
    superseded_by  TEXT REFERENCES facts(id),
    path           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_entity_type_valid
    ON facts(entity_id, fact_type, valid_at);
CREATE INDEX IF NOT EXISTS idx_facts_source ON facts(source_id);

CREATE TABLE IF NOT EXISTS fact_supersession (
    superseded_id TEXT NOT NULL REFERENCES facts(id),
    superseder_id TEXT NOT NULL REFERENCES facts(id),
    PRIMARY KEY (superseded_id, superseder_id)
);

CREATE TABLE IF NOT EXISTS conclusions (
    id              TEXT PRIMARY KEY,
    entity_id       TEXT NOT NULL REFERENCES entities(id),
    conclusion_type TEXT NOT NULL,
    value           TEXT NOT NULL,
    confidence      TEXT NOT NULL,
    review_status   TEXT NOT NULL DEFAULT 'unreviewed',
    reasoning       TEXT,
    path            TEXT NOT NULL,
    generated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conclusion_basis (
    conclusion_id TEXT NOT NULL REFERENCES conclusions(id),
    fact_id       TEXT NOT NULL REFERENCES facts(id),
    PRIMARY KEY (conclusion_id, fact_id)
);

CREATE TABLE IF NOT EXISTS decisions (
    id            TEXT PRIMARY KEY,
    entity_id     TEXT REFERENCES entities(id),
    decision_date TEXT NOT NULL,
    status        TEXT NOT NULL,
    authority     TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    source_id     TEXT NOT NULL REFERENCES sources(id),
    path          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unknowns (
    id                  TEXT PRIMARY KEY,
    entity_id           TEXT REFERENCES entities(id),
    status              TEXT NOT NULL DEFAULT 'open',
    created_at          TEXT NOT NULL,
    resolved_at         TEXT,
    resolved_by_fact_id TEXT REFERENCES facts(id),
    path                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS links (
    from_path TEXT NOT NULL,
    to_path   TEXT NOT NULL,
    link_type TEXT NOT NULL,
    PRIMARY KEY (from_path, to_path, link_type)
);

CREATE TABLE IF NOT EXISTS kb_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class KBDB:
    """Async wrapper around the temporal-KB SQLite database."""

    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def init(self) -> None:
        """Create tables and stamp the schema version. Idempotent."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await _apply_migrations(db)
            await db.commit()

    # -----------------------------------------------------------------------
    # Sources
    # -----------------------------------------------------------------------

    async def insert_source(
        self,
        *,
        id: str,
        source_type: str,
        title: str,
        authority: str,
        raw_path: Optional[str],
        parsed_path: Optional[str],
        created_at: str,
        ingested_at: str,
        sha256: str,
        author: Optional[str] = None,
    ) -> None:
        """Insert a source row. Raises ``sqlite3.IntegrityError`` on duplicate ``sha256``."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                "INSERT INTO sources (id, source_type, title, authority, raw_path, "
                "parsed_path, created_at, ingested_at, author, sha256) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (id, source_type, title, authority, raw_path, parsed_path,
                 created_at, ingested_at, author, sha256),
            )
            await db.commit()

    async def find_source_by_sha256(self, sha256: str) -> Optional[dict]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM sources WHERE sha256 = ? LIMIT 1", (sha256,)
            ) as cur:
                row = await cur.fetchone()
            return dict(row) if row else None

    async def get_source(self, id: str) -> Optional[dict]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM sources WHERE id = ?", (id,)
            ) as cur:
                row = await cur.fetchone()
            return dict(row) if row else None

    async def source_exists(self, id: str) -> bool:
        return (await self.get_source(id)) is not None

    async def list_sources(self) -> list[dict]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM sources ORDER BY ingested_at"
            ) as cur:
                rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # -----------------------------------------------------------------------
    # Entities
    # -----------------------------------------------------------------------

    async def upsert_entity(
        self,
        *,
        id: str,
        entity_type: str,
        name: str,
        slug: str,
        path: str,
        status: str = "active",
        current_state_review_status: str = "unreviewed",
    ) -> None:
        """Insert or update an entity row keyed by ``id``."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                "INSERT INTO entities (id, entity_type, name, slug, path, status, "
                "current_state_review_status) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "  entity_type = excluded.entity_type,"
                "  name = excluded.name,"
                "  slug = excluded.slug,"
                "  path = excluded.path,"
                "  status = excluded.status",
                (id, entity_type, name, slug, path, status,
                 current_state_review_status),
            )
            await db.commit()

    async def get_entity(self, id: str) -> Optional[dict]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM entities WHERE id = ?", (id,)
            ) as cur:
                row = await cur.fetchone()
            return dict(row) if row else None

    async def list_entities(
        self, entity_type: Optional[str] = None
    ) -> list[dict]:
        sql = "SELECT * FROM entities"
        args: tuple = ()
        if entity_type:
            sql += " WHERE entity_type = ?"
            args = (entity_type,)
        sql += " ORDER BY entity_type, slug"
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, args) as cur:
                rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # -----------------------------------------------------------------------
    # Facts
    # -----------------------------------------------------------------------

    async def insert_fact(
        self,
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
        path: str,
        value_raw: Optional[str] = None,
        source_span: Optional[str] = None,
        review_status: str = "unreviewed",
    ) -> None:
        """Insert a fact row. Use :meth:`mark_superseded` separately to record supersession."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                "INSERT INTO facts (id, entity_id, fact_type, value, value_raw, "
                "valid_at, observed_at, source_id, source_path, source_span, "
                "source_quote, confidence, review_status, path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (id, entity_id, fact_type, value, value_raw, valid_at,
                 observed_at, source_id, source_path, source_span,
                 source_quote, confidence, review_status, path),
            )
            await db.commit()

    async def mark_superseded(
        self, superseded_id: str, superseder_id: str
    ) -> None:
        """Record that *superseder_id* supersedes *superseded_id*.

        Writes both the denormalised ``facts.superseded_by`` column and the
        normalised ``fact_supersession`` row in one transaction. Safe to call
        repeatedly — duplicates are ignored.
        """
        if superseded_id == superseder_id:
            raise ValueError("a fact cannot supersede itself")
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            try:
                await db.execute("BEGIN")
                await db.execute(
                    "UPDATE facts SET superseded_by = ? WHERE id = ?",
                    (superseder_id, superseded_id),
                )
                await db.execute(
                    "INSERT OR IGNORE INTO fact_supersession "
                    "(superseded_id, superseder_id) VALUES (?, ?)",
                    (superseded_id, superseder_id),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def get_fact(self, id: str) -> Optional[dict]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM facts WHERE id = ?", (id,)
            ) as cur:
                row = await cur.fetchone()
            return dict(row) if row else None

    async def list_facts(
        self,
        entity_id: Optional[str] = None,
        fact_type: Optional[str] = None,
        active_only: bool = False,
    ) -> list[dict]:
        sql = "SELECT * FROM facts WHERE 1=1"
        args: list = []
        if entity_id:
            sql += " AND entity_id = ?"
            args.append(entity_id)
        if fact_type:
            sql += " AND fact_type = ?"
            args.append(fact_type)
        if active_only:
            sql += " AND superseded_by IS NULL"
        sql += " ORDER BY valid_at DESC, observed_at DESC"
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(args)) as cur:
                rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def count_facts(self) -> int:
        async with aiosqlite.connect(self._path) as db:
            async with db.execute("SELECT COUNT(*) FROM facts") as cur:
                row = await cur.fetchone()
            return int(row[0]) if row else 0

    # -----------------------------------------------------------------------
    # Decisions
    # -----------------------------------------------------------------------

    async def insert_decision(
        self,
        *,
        id: str,
        decision_date: str,
        status: str,
        authority: str,
        source_id: str,
        path: str,
        entity_id: Optional[str] = None,
        review_status: str = "unreviewed",
    ) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                "INSERT INTO decisions (id, entity_id, decision_date, status, "
                "authority, review_status, source_id, path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (id, entity_id, decision_date, status, authority,
                 review_status, source_id, path),
            )
            await db.commit()

    async def get_decision(self, id: str) -> Optional[dict]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM decisions WHERE id = ?", (id,)
            ) as cur:
                row = await cur.fetchone()
            return dict(row) if row else None

    async def list_decisions(
        self,
        source_id: Optional[str] = None,
        entity_id: Optional[str] = None,
    ) -> list[dict]:
        sql = "SELECT * FROM decisions WHERE 1=1"
        args: list = []
        if source_id:
            sql += " AND source_id = ?"
            args.append(source_id)
        if entity_id:
            sql += " AND entity_id = ?"
            args.append(entity_id)
        sql += " ORDER BY decision_date DESC, id"
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(args)) as cur:
                rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # -----------------------------------------------------------------------
    # Unknowns
    # -----------------------------------------------------------------------

    async def insert_unknown(
        self,
        *,
        id: str,
        created_at: str,
        path: str,
        entity_id: Optional[str] = None,
        status: str = "open",
    ) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                "INSERT INTO unknowns (id, entity_id, status, created_at, path) "
                "VALUES (?, ?, ?, ?, ?)",
                (id, entity_id, status, created_at, path),
            )
            await db.commit()

    async def get_unknown(self, id: str) -> Optional[dict]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM unknowns WHERE id = ?", (id,)
            ) as cur:
                row = await cur.fetchone()
            return dict(row) if row else None

    async def list_unknowns(
        self,
        entity_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[dict]:
        sql = "SELECT * FROM unknowns WHERE 1=1"
        args: list = []
        if entity_id:
            sql += " AND entity_id = ?"
            args.append(entity_id)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY created_at DESC, id"
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(args)) as cur:
                rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # -----------------------------------------------------------------------
    # Conclusions
    # -----------------------------------------------------------------------

    async def insert_conclusion(
        self,
        *,
        id: str,
        entity_id: str,
        conclusion_type: str,
        value: str,
        confidence: str,
        path: str,
        generated_at: str,
        reasoning: Optional[str] = None,
        review_status: str = "unreviewed",
        based_on: tuple[str, ...] = (),
    ) -> None:
        """Insert a conclusion row + its conclusion_basis rows in one transaction."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            try:
                await db.execute("BEGIN")
                await db.execute(
                    "INSERT INTO conclusions (id, entity_id, conclusion_type, "
                    "value, confidence, review_status, reasoning, path, generated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (id, entity_id, conclusion_type, value, confidence,
                     review_status, reasoning, path, generated_at),
                )
                for fact_id in based_on:
                    await db.execute(
                        "INSERT OR IGNORE INTO conclusion_basis "
                        "(conclusion_id, fact_id) VALUES (?, ?)",
                        (id, fact_id),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    # -----------------------------------------------------------------------
    # Generic helpers
    # -----------------------------------------------------------------------

    async def execute(self, sql: str, args: tuple = ()) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(sql, args)
            await db.commit()

    async def fetchall(self, sql: str, args: tuple = ()) -> list[dict]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, args) as cur:
                rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def schema_version(self) -> int:
        async with aiosqlite.connect(self._path) as db:
            async with db.execute(
                "SELECT value FROM kb_meta WHERE key = 'schema_version'"
            ) as cur:
                row = await cur.fetchone()
            return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


async def _apply_migrations(db: aiosqlite.Connection) -> None:
    """Apply pending migrations on the open connection."""
    # Ensure kb_meta exists so we can read schema_version
    await db.execute(
        "CREATE TABLE IF NOT EXISTS kb_meta ("
        " key TEXT PRIMARY KEY,"
        " value TEXT NOT NULL"
        ")"
    )
    async with db.execute(
        "SELECT value FROM kb_meta WHERE key = 'schema_version'"
    ) as cur:
        row = await cur.fetchone()
    current = int(row[0]) if row else 0

    if current < 1:
        for stmt in _split_sql(_SCHEMA_V1):
            await db.execute(stmt)
        current = 1

    # Future migrations: `if current < 2: ...; current = 2`

    await db.execute(
        "INSERT INTO kb_meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(current),),
    )


def _split_sql(script: str) -> list[str]:
    """Split a multi-statement SQL string on ``;`` (good enough for our DDL)."""
    out: list[str] = []
    buf: list[str] = []
    for line in script.splitlines():
        buf.append(line)
        if line.strip().endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt and not stmt.startswith("--"):
                out.append(stmt)
            buf = []
    return out
