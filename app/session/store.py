"""Sessions and where they are kept.

The session id itself is never stored, only its SHA-256, and neither is the
user's password: a session holds who the user is and what Grafana said about
their role and teams when they were last verified.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.db import Database

# express-session's id size (uid-safe, 24 bytes).
SESSION_ID_BYTES = 24
SESSIONS_FILE_NAME = "sessions.db"

SESSIONS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
  id_hash          TEXT PRIMARY KEY,
  login            TEXT NOT NULL,
  user_id          INTEGER,
  org_role         TEXT,
  teams            TEXT NOT NULL DEFAULT '[]',
  created_at       REAL NOT NULL,
  last_used_at     REAL NOT NULL,
  last_verified_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_last_used ON sessions(last_used_at);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at);
"""


@dataclass
class SessionData:
    login: str
    user_id: int | None = None
    org_role: str | None = None
    # [{"id": ..., "name": ...}]
    teams: list[dict[str, Any]] = field(default_factory=list)
    # Unix seconds; create() sets the first two.
    created_at: float = 0.0
    last_used_at: float = 0.0
    last_verified_at: float = 0.0


class SessionStore(Protocol):
    """Keeps sessions by id, with an idle timeout and an absolute cap."""

    def create(self, data: SessionData) -> str:
        """Store a new session and return its id."""
        ...

    def get(self, sid: str) -> SessionData | None:
        """The session, or None when it is unknown or has expired."""
        ...

    def update(self, sid: str, data: SessionData) -> None:
        """Replace a live session's user data and last_verified_at."""
        ...

    def touch(self, sid: str, data: SessionData) -> None:
        """Mark the session used now, at most once per touch interval."""
        ...

    def delete(self, sid: str) -> None: ...

    def delete_expired(self) -> int:
        """Remove expired sessions; returns how many."""
        ...

    def cookie_expires_at(self, data: SessionData) -> float:
        """When the session cookie should expire if the session is used now."""
        ...


def hash_session_id(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


class SqliteSessionStore:
    def __init__(
        self,
        db: Database,
        *,
        ttl_seconds: float,
        max_ttl_seconds: float,
        touch_interval_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._ttl = ttl_seconds
        self._max_ttl = max_ttl_seconds
        # Never touch less often than half the idle TTL, or active users would expire.
        self._touch_interval = min(touch_interval_seconds, ttl_seconds / 2)
        self._clock = clock

    def _expired(self, data: SessionData, now: float) -> bool:
        return now - data.last_used_at >= self._ttl or now - data.created_at >= self._max_ttl

    def create(self, data: SessionData) -> str:
        sid = secrets.token_urlsafe(SESSION_ID_BYTES)
        now = self._clock()
        data.created_at = data.last_used_at = now
        with self._db.transaction() as conn:
            conn.execute(
                """INSERT INTO sessions (id_hash, login, user_id, org_role, teams,
                                         created_at, last_used_at, last_verified_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    hash_session_id(sid),
                    data.login,
                    data.user_id,
                    data.org_role,
                    json.dumps(data.teams),
                    data.created_at,
                    data.last_used_at,
                    data.last_verified_at,
                ),
            )
        return sid

    def get(self, sid: str) -> SessionData | None:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id_hash = ?", (hash_session_id(sid),)
            ).fetchone()
        if row is None:
            return None
        data = _session(row)
        if self._expired(data, self._clock()):
            self.delete(sid)
            return None
        return data

    def update(self, sid: str, data: SessionData) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                """UPDATE sessions SET login = ?, user_id = ?, org_role = ?, teams = ?,
                                       last_verified_at = ?
                   WHERE id_hash = ?""",
                (
                    data.login,
                    data.user_id,
                    data.org_role,
                    json.dumps(data.teams),
                    data.last_verified_at,
                    hash_session_id(sid),
                ),
            )

    def touch(self, sid: str, data: SessionData) -> None:
        now = self._clock()
        if now - data.last_used_at < self._touch_interval:
            return
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE sessions SET last_used_at = ? WHERE id_hash = ?",
                (now, hash_session_id(sid)),
            )
        data.last_used_at = now

    def delete(self, sid: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE id_hash = ?", (hash_session_id(sid),))

    def delete_expired(self) -> int:
        now = self._clock()
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE last_used_at <= ? OR created_at <= ?",
                (now - self._ttl, now - self._max_ttl),
            )
        return cursor.rowcount

    def cookie_expires_at(self, data: SessionData) -> float:
        return min(self._clock() + self._ttl, data.created_at + self._max_ttl)


def _session(row: sqlite3.Row) -> SessionData:
    return SessionData(
        login=row["login"],
        user_id=row["user_id"],
        org_role=row["org_role"],
        teams=json.loads(row["teams"]),
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        last_verified_at=row["last_verified_at"],
    )
