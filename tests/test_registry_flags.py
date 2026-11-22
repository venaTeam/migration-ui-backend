"""The migrations registry: port of src/registry/flags.test.ts, plus file compatibility."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from app.db import Database
from app.registry.flags import RegistryRule, base_title_of, compute_migrated_flags, seed_folder
from app.registry.store import REGISTRY_FILE_NAME, REGISTRY_SCHEMA_SQL, Registry

# Mirrors the real schema's shape: one required label (component), one required
# annotation (message), one defaulted label (Send-Alert). Hand-made "-v2" rules lack them.
SCHEMA: dict[str, Any] = {
    "version": "0.1.0",
    "fields": [
        {
            "name": "Send-Alert",
            "location": "label",
            "required": True,
            "explanation": "",
            "constraints": {"exact_value": "true"},
            "default_value": "true",
        },
        {
            "name": "component",
            "location": "label",
            "required": True,
            "explanation": "",
            "constraints": {"min_length": 1},
            "default_value": None,
        },
        {
            "name": "message",
            "location": "annotation",
            "required": True,
            "explanation": "",
            "constraints": {"min_length": 1},
            "default_value": None,
        },
    ],
}


def rule(uid: str, title: str, compliant: bool = True) -> RegistryRule:
    if compliant:
        return RegistryRule(
            uid, title, {"component": "cpu", "Send-Alert": "true"}, {"message": "m"}
        )
    return RegistryRule(uid, title, {}, {})


def open_registry(path: Path) -> Registry:
    return Registry(Database(path, REGISTRY_SCHEMA_SQL))


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    return tmp_path / REGISTRY_FILE_NAME


@pytest.fixture
def registry(registry_path: Path) -> Iterator[Registry]:
    registry = open_registry(registry_path)
    yield registry
    registry.close()


class TestBaseTitleOf:
    def test_strips_the_suffix_the_tool_produces(self) -> None:
        # build_cloned_rule appends "-v2" verbatim: "CPU High" -> "CPU High-v2".
        assert base_title_of("CPU High-v2") == "CPU High"

    def test_strips_a_hand_typed_suffix_with_a_space(self) -> None:
        assert base_title_of("CPU High -v2") == "CPU High"

    def test_walks_chains_one_hop_at_a_time(self) -> None:
        assert base_title_of("CPU High-v2-v2") == "CPU High-v2"

    def test_none_without_the_suffix(self) -> None:
        assert base_title_of("CPU High") is None

    def test_a_rule_titled_exactly_v2_has_an_empty_base(self) -> None:
        assert base_title_of("-v2") == ""

    def test_a_trailing_newline_is_not_the_end(self) -> None:
        # JavaScript's `$` (no m flag) does not match before a final newline.
        assert base_title_of("CPU High-v2\n") is None


class TestSeedFolder:
    """The honesty table (plan section 6.2)."""

    def test_pair_writes_a_row_with_the_original_kept(self, registry: Registry) -> None:
        rules = [rule("o1", "CPU High", False), rule("c1", "CPU High-v2")]
        assert seed_folder(registry, "f", rules, SCHEMA) == 1
        rows = registry.migrations_for_folder("f")
        assert len(rows) == 1
        assert (rows[0].original_uid, rows[0].created_uid, rows[0].deleted_original) == (
            "o1",
            "c1",
            0,
        )
        assert rows[0].created_via == "seed"

    def test_pair_with_a_hand_typed_title_is_matched(self, registry: Registry) -> None:
        rules = [rule("o1", "CPU High", False), rule("c1", "CPU High -v2")]
        seed_folder(registry, "f", rules, SCHEMA)
        rows = registry.migrations_for_folder("f")
        assert [(row.original_uid, row.created_uid) for row in rows] == [("o1", "c1")]

    def test_lone_compliant_clone_writes_a_row_with_the_original_deleted(
        self, registry: Registry
    ) -> None:
        assert seed_folder(registry, "f", [rule("lone", "CPU High-v2")], SCHEMA) == 1
        row = registry.migrations_for_folder("f")[0]
        assert (row.original_uid, row.original_title, row.created_uid, row.deleted_original) == (
            None,
            "CPU High",
            "lone",
            1,
        )

    def test_chain_writes_one_row_per_hop(self, registry: Registry) -> None:
        rules = [
            rule("o1", "CPU High", False),
            rule("c1", "CPU High-v2"),
            rule("c2", "CPU High-v2-v2"),
        ]
        assert seed_folder(registry, "f", rules, SCHEMA) == 2
        rows = registry.migrations_for_folder("f")
        assert sorted((row.original_uid, row.created_uid) for row in rows) == [
            ("c1", "c2"),
            ("o1", "c1"),
        ]

    def test_hand_made_v2_failing_the_standard_writes_nothing(self, registry: Registry) -> None:
        assert seed_folder(registry, "f", [rule("hand", "foo-v2", False)], SCHEMA) == 0
        assert registry.migrations_for_folder("f") == []

    def test_lone_base_rule_writes_nothing(self, registry: Registry) -> None:
        assert seed_folder(registry, "f", [rule("o1", "CPU High", False)], SCHEMA) == 0
        assert registry.migrations_for_folder("f") == []

    def test_runs_exactly_once_per_folder(self, registry: Registry) -> None:
        rules = [rule("o1", "CPU High", False), rule("c1", "CPU High-v2")]
        assert seed_folder(registry, "f", rules, SCHEMA) == 1
        assert seed_folder(registry, "f", rules, SCHEMA) == 0
        # ...and the one-time mark survives a restart.
        registry.close()
        assert seed_folder(registry, "f", rules, SCHEMA) == 0

    def test_skips_hops_the_registry_already_has(self, registry: Registry) -> None:
        registry.record_migration(
            original_uid="o1",
            original_title="CPU High",
            created_uid="c1",
            created_title="CPU High-v2",
            folder_uid="f",
            deleted_original=False,
            created_via="clone",
            migrated_by="alice",
        )
        rules = [rule("o1", "CPU High", False), rule("c1", "CPU High-v2")]
        assert seed_folder(registry, "f", rules, SCHEMA) == 0
        assert len(registry.migrations_for_folder("f")) == 1
        assert registry.is_folder_seeded("f")


class TestComputeMigratedFlags:
    """The copy-exists guard (plan section 3.2)."""

    def test_flags_the_original_only_while_its_copy_exists(self, registry: Registry) -> None:
        registry.record_migration(
            original_uid="o1",
            original_title="CPU High",
            created_uid="c1",
            created_title="CPU High-v2",
            folder_uid="f",
            deleted_original=False,
            created_via="clone",
            migrated_by=None,
        )
        assert compute_migrated_flags(registry, "f", {"c1"}).get("o1") is True
        # Copy deleted: the original is migratable again.
        assert compute_migrated_flags(registry, "f", set()).get("o1") is False

    def test_never_flags_created_only_rows(self, registry: Registry) -> None:
        registry.record_migration(
            original_uid=None,
            original_title=None,
            created_uid="c1",
            created_title="New rule",
            folder_uid="f",
            deleted_original=False,
            created_via="create",
            migrated_by=None,
        )
        assert compute_migrated_flags(registry, "f", {"c1"}) == {}


class TestRegistryFile:
    def test_reads_a_file_written_by_the_express_service(self, registry_path: Path) -> None:
        # sql.js leaves a rollback-journal file with this schema.
        conn = sqlite3.connect(registry_path)
        conn.executescript(REGISTRY_SCHEMA_SQL)
        conn.execute(
            "INSERT INTO migrations (original_uid, original_title, created_uid, created_title,"
            " folder_uid, deleted_original, created_via, migrated_by)"
            " VALUES ('o1', 'CPU High', 'c1', 'CPU High-v2', 'f', 0, 'clone', 'bob')"
        )
        conn.execute("INSERT INTO seed_scan (folder_uid, pairs_found) VALUES ('f', 1)")
        conn.commit()
        conn.close()

        registry = open_registry(registry_path)
        try:
            row = registry.latest_migration_for_original("o1")
            assert row is not None
            assert (row.created_uid, row.migrated_by) == ("c1", "bob")
            assert registry.is_folder_seeded("f")
        finally:
            registry.close()

    def test_runs_in_wal_mode_and_leaves_no_wal_file_on_close(
        self, registry: Registry, registry_path: Path
    ) -> None:
        registry.mark_folder_seeded("f", 0)
        with closing(sqlite3.connect(registry_path)) as other:
            assert other.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        registry.close()
        assert not Path(f"{registry_path}-wal").exists()
        conn = sqlite3.connect(registry_path)
        try:
            # Back to the rollback-journal format sql.js reads.
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            assert conn.execute("SELECT folder_uid FROM seed_scan").fetchall() == [("f",)]
        finally:
            conn.close()

    def test_a_failing_registry_never_raises(self, tmp_path: Path) -> None:
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        registry = open_registry(blocker / REGISTRY_FILE_NAME)
        registry.record_migration(
            original_uid="o1",
            original_title=None,
            created_uid="c1",
            created_title="t",
            folder_uid="f",
            deleted_original=False,
            created_via="clone",
            migrated_by=None,
        )
        assert registry.migrations_for_folder("f") == []
        assert registry.latest_migration_for_original("o1") is None
        assert registry.is_folder_seeded("f") is False


class TestRowUpdates:
    def test_record_returns_the_row_id_and_marks_the_original_deleted(
        self, registry: Registry
    ) -> None:
        row_id = registry.record_migration(
            original_uid="o1",
            original_title="CPU High",
            created_uid="c1",
            created_title="CPU High-v2",
            folder_uid="f",
            deleted_original=False,
            created_via="clone-and-delete",
            migrated_by="alice",
        )
        assert row_id is not None
        registry.mark_original_deleted(row_id)
        row = registry.latest_migration_for_original("o1")
        assert row is not None
        assert (row.id, row.deleted_original) == (row_id, 1)

    def test_latest_rows_for_several_originals(self, registry: Registry) -> None:
        for original, created in (("a", "a1"), ("b", "b1"), ("a", "a2")):
            registry.record_migration(
                original_uid=original,
                original_title=None,
                created_uid=created,
                created_title=created,
                folder_uid="f",
                deleted_original=False,
                created_via="clone",
                migrated_by=None,
            )
        latest = registry.latest_migrations_for_originals(["a", "b", "c"])
        assert {uid: row.created_uid for uid, row in latest.items()} == {"a": "a2", "b": "b1"}
        assert registry.latest_migrations_for_originals([]) == {}
