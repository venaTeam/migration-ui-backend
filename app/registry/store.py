"""The migrations registry (docs/PLAN-v1-migrated-flag.md section 4).

Two tables in one SQLite file:
  migrations: one row per migration this tool performed or discovered
  seed_scan:  folders whose one-time seeder pass is complete

A registry failure never fails the migration itself: the public methods log
and fall back to "no memory". Worst case a migrated rule loses its badge once,
and the seeder finds it again on the next folder load.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from app.db import Database

log = logging.getLogger(__name__)

REGISTRY_FILE_NAME = "migrations.db"

REGISTRY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS migrations (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  original_uid     TEXT,
  original_title   TEXT,
  created_uid      TEXT NOT NULL,
  created_title    TEXT NOT NULL,
  folder_uid       TEXT,
  deleted_original INTEGER NOT NULL DEFAULT 0,
  created_via      TEXT NOT NULL,
  migrated_by      TEXT,
  migrated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_migrations_original ON migrations(original_uid);
CREATE INDEX IF NOT EXISTS idx_migrations_created  ON migrations(created_uid);
CREATE INDEX IF NOT EXISTS idx_migrations_folder   ON migrations(folder_uid);

CREATE TABLE IF NOT EXISTS seed_scan (
  folder_uid  TEXT PRIMARY KEY,
  seeded_at   TEXT NOT NULL DEFAULT (datetime('now')),
  pairs_found INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class MigrationRow:
    id: int
    original_uid: str | None
    original_title: str | None
    created_uid: str
    created_title: str
    folder_uid: str | None
    deleted_original: int
    created_via: str
    migrated_by: str | None
    migrated_at: str


def _row(row: sqlite3.Row) -> MigrationRow:
    return MigrationRow(**dict(row))


class RegistryTx:
    """Registry statements on one connection; errors propagate."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert_migration(
        self,
        *,
        original_uid: str | None,
        original_title: str | None,
        created_uid: str | None,
        created_title: str,
        folder_uid: str | None,
        deleted_original: bool,
        created_via: str,
        migrated_by: str | None,
    ) -> int:
        cursor = self._conn.execute(
            """INSERT INTO migrations
                 (original_uid, original_title, created_uid, created_title,
                  folder_uid, deleted_original, created_via, migrated_by, migrated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (
                original_uid,
                original_title,
                created_uid,
                created_title,
                folder_uid,
                1 if deleted_original else 0,
                created_via,
                migrated_by,
            ),
        )
        return int(cursor.lastrowid or 0)

    def mark_original_deleted(self, row_id: int) -> None:
        self._conn.execute("UPDATE migrations SET deleted_original = 1 WHERE id = ?", (row_id,))

    def is_folder_seeded(self, folder_uid: str) -> bool:
        cursor = self._conn.execute("SELECT 1 FROM seed_scan WHERE folder_uid = ?", (folder_uid,))
        return cursor.fetchone() is not None

    def mark_folder_seeded(self, folder_uid: str, pairs_found: int) -> None:
        self._conn.execute(
            """INSERT INTO seed_scan (folder_uid, seeded_at, pairs_found)
               VALUES (?, datetime('now'), ?)
               ON CONFLICT(folder_uid) DO UPDATE SET seeded_at = datetime('now'), pairs_found = ?""",
            (folder_uid, pairs_found, pairs_found),
        )

    def migrations_for_folder(self, folder_uid: str) -> list[MigrationRow]:
        cursor = self._conn.execute(
            "SELECT * FROM migrations WHERE folder_uid = ? ORDER BY id", (folder_uid,)
        )
        return [_row(row) for row in cursor.fetchall()]

    def latest_migration_for_original(self, original_uid: str) -> MigrationRow | None:
        cursor = self._conn.execute(
            "SELECT * FROM migrations WHERE original_uid = ? ORDER BY id DESC LIMIT 1",
            (original_uid,),
        )
        row = cursor.fetchone()
        return _row(row) if row is not None else None

    def latest_migrations_for_originals(self, original_uids: list[str]) -> dict[str, MigrationRow]:
        placeholders = ", ".join("?" for _ in original_uids)
        cursor = self._conn.execute(
            f"""SELECT m.* FROM migrations m
                JOIN (SELECT MAX(id) AS id FROM migrations
                      WHERE original_uid IN ({placeholders}) GROUP BY original_uid) latest
                  ON m.id = latest.id""",
            original_uids,
        )
        rows = [_row(row) for row in cursor.fetchall()]
        return {row.original_uid: row for row in rows if row.original_uid is not None}


class Registry:
    def __init__(self, db: Database) -> None:
        self._db = db

    @property
    def path(self) -> str:
        return str(self._db.path)

    @contextmanager
    def transaction(self) -> Iterator[RegistryTx]:
        with self._db.transaction() as conn:
            yield RegistryTx(conn)

    @contextmanager
    def _reader(self) -> Iterator[RegistryTx]:
        with self._db.connection() as conn:
            yield RegistryTx(conn)

    def _write[T](self, statement: Callable[[RegistryTx], T], what: str) -> T | None:
        try:
            with self.transaction() as tx:
                return statement(tx)
        except Exception:
            log.warning(
                "[registry] write failed (%s) — migration result unaffected", what, exc_info=True
            )
            return None

    def record_migration(
        self,
        *,
        original_uid: str | None,
        original_title: str | None,
        created_uid: str | None,
        created_title: str,
        folder_uid: str | None,
        deleted_original: bool,
        created_via: str,
        migrated_by: str | None,
    ) -> int | None:
        """Write one migration row and return its id (None if the write failed). Never raises."""
        return self._write(
            lambda tx: tx.insert_migration(
                original_uid=original_uid,
                original_title=original_title,
                created_uid=created_uid,
                created_title=created_title,
                folder_uid=folder_uid,
                deleted_original=deleted_original,
                created_via=created_via,
                migrated_by=migrated_by,
            ),
            f"recordMigration {'new' if original_uid is None else original_uid} -> {created_uid}",
        )

    def record_view_delete(
        self, uid: str, title: str, folder_uid: str | None, deleted_by: str | None
    ) -> None:
        """Audit-only row for a v2 rule deleted from the review view (plan section 5.1)."""
        self._write(
            lambda tx: tx.insert_migration(
                original_uid=None,
                original_title=None,
                created_uid=uid,
                created_title=title,
                folder_uid=folder_uid,
                deleted_original=True,
                created_via="view-delete",
                migrated_by=deleted_by,
            ),
            f"recordViewDelete {uid}",
        )

    def mark_original_deleted(self, row_id: int) -> None:
        """The row's action went on to delete the original. Never raises."""
        self._write(lambda tx: tx.mark_original_deleted(row_id), f"markOriginalDeleted {row_id}")

    def is_folder_seeded(self, folder_uid: str) -> bool:
        try:
            with self._reader() as tx:
                return tx.is_folder_seeded(folder_uid)
        except Exception:
            log.warning("[registry] seed_scan lookup failed — will rescan", exc_info=True)
            return False

    def mark_folder_seeded(self, folder_uid: str, pairs_found: int) -> None:
        self._write(
            lambda tx: tx.mark_folder_seeded(folder_uid, pairs_found),
            f"markFolderSeeded {folder_uid}",
        )

    def migrations_for_folder(self, folder_uid: str) -> list[MigrationRow]:
        try:
            with self._reader() as tx:
                return tx.migrations_for_folder(folder_uid)
        except Exception:
            log.warning(
                "[registry] migrations lookup failed — no migrated flags this load", exc_info=True
            )
            return []

    def latest_migration_for_original(self, original_uid: str) -> MigrationRow | None:
        try:
            with self._reader() as tx:
                return tx.latest_migration_for_original(original_uid)
        except Exception:
            log.warning("[registry] original lookup failed", exc_info=True)
            return None

    def latest_migrations_for_originals(self, original_uids: list[str]) -> dict[str, MigrationRow]:
        """Latest row per original uid; empty when the registry can't be read."""
        if not original_uids:
            return {}
        try:
            with self._reader() as tx:
                return tx.latest_migrations_for_originals(original_uids)
        except Exception:
            log.warning("[registry] originals lookup failed", exc_info=True)
            return {}

    def close(self) -> None:
        self._db.close()
