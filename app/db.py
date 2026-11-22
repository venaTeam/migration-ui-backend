"""The SQLite file on the persistent volume.

The Express service read and wrote this same file through sql.js, which keeps
the whole database in memory and rewrites the file on every write. The two
services must never run against the file at the same time.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

BUSY_TIMEOUT_SECONDS = 5.0


class Database:
    """One lazily opened connection to the file, shared by its users under a lock."""

    def __init__(self, path: Path, schema_sql: str) -> None:
        self.path = path
        self._schema_sql = schema_sql
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.path,
            timeout=BUSY_TIMEOUT_SECONDS,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            conn.row_factory = sqlite3.Row
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            conn.executescript(self._schema_sql)
        except BaseException:
            conn.close()
            raise
        log.info("[db] opened %s (journal_mode=%s)", self.path, mode)
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """The connection, held exclusively; opened on first use and retried after a failure."""
        with self._lock:
            if self._conn is None:
                self._conn = self._open()
            yield self._conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A write transaction, committed on success and rolled back on error."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def close(self) -> None:
        """Close and leave a plain rollback-journal file with no -wal beside it.

        That is the format sql.js reads, so the Express image can take the file over.
        """
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute("PRAGMA journal_mode=DELETE")
            except sqlite3.Error:
                log.warning("[db] could not leave WAL mode for %s", self.path, exc_info=True)
            finally:
                self._conn.close()
                self._conn = None
