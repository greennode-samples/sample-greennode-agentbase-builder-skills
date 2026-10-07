"""Per-user data access. EVERY method takes `owner` (= current_user()) and filters by it — never by a tool argument.

SQLite (stdlib) keeps the template dependency-free and runnable locally. It is a local file ⇒ NOT shared
across Runtime replicas: before deploying with more than 1 replica, keep this interface and swap the bodies for
Postgres (asyncpg / SQLAlchemy async), e.g. `SELECT ... FROM notes WHERE owner = $1`.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner           TEXT NOT NULL,
    text            TEXT NOT NULL,
    idempotency_key TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (owner, idempotency_key)
);
CREATE INDEX IF NOT EXISTS notes_owner_id ON notes (owner, id);
"""


@dataclass(frozen=True)
class NoteRow:
    id: int
    text: str
    created_at: str


class NotesStore:
    def __init__(self, path: str):
        self._path = os.path.abspath(path)  # fixed at startup, independent of later cwd
        with closing(self._connect()) as c, c:
            c.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        # Short-lived connection per call ⇒ safe with asyncio.to_thread (sqlite3 objects are per-thread)
        return sqlite3.connect(self._path, timeout=5)

    async def add(self, owner: str, text: str, idempotency_key: str | None = None) -> NoteRow:
        """Insert a note, or return the note already stored under (owner, idempotency_key)."""
        return await asyncio.to_thread(self._add, owner, text, idempotency_key)

    async def list(self, owner: str, limit: int, offset: int) -> tuple[list[NoteRow], int]:
        return await asyncio.to_thread(self._list, owner, limit, offset)

    async def delete(self, owner: str, note_id: int) -> bool:
        return await asyncio.to_thread(self._delete, owner, note_id)

    def _add(self, owner: str, text: str, idempotency_key: str | None) -> NoteRow:
        key = idempotency_key or None  # "" = no key
        now = datetime.now(UTC).isoformat(timespec="seconds")
        # One transaction, atomic under concurrency: a retry (or a concurrent call) with the same key
        # hits the UNIQUE index and inserts nothing, then reads back the note stored the first time.
        with closing(self._connect()) as c, c:
            cur = c.execute(
                "INSERT INTO notes (owner, text, idempotency_key, created_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (owner, idempotency_key) DO NOTHING",
                (owner, text, key, now),
            )
            if key is None:
                return NoteRow(int(cur.lastrowid), text, now)
            row = c.execute(
                "SELECT id, text, created_at FROM notes WHERE owner = ? AND idempotency_key = ?",
                (owner, key),
            ).fetchone()
            return NoteRow(*row)

    def _list(self, owner: str, limit: int, offset: int) -> tuple[list[NoteRow], int]:
        with closing(self._connect()) as c:
            total = c.execute("SELECT COUNT(*) FROM notes WHERE owner = ?", (owner,)).fetchone()[0]
            rows = c.execute(
                "SELECT id, text, created_at FROM notes WHERE owner = ? ORDER BY id LIMIT ? OFFSET ?",
                (owner, limit, offset),
            ).fetchall()
        return [NoteRow(*r) for r in rows], total

    def _delete(self, owner: str, note_id: int) -> bool:
        with closing(self._connect()) as c, c:
            cur = c.execute("DELETE FROM notes WHERE owner = ? AND id = ?", (owner, note_id))
            return cur.rowcount > 0
