"""URL parsing, configuration and classification."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from app.session.store import SESSIONS_SCHEMA_SQL, SqliteSessionStore
from app.standard import classify_rule_against_schema
from app.urls import parse_folder_uid, parse_rule_uid, rule_view_url
from tests.conftest import COMPLIANT_ANNOTATIONS, COMPLIANT_LABELS, SCHEMA

ADMIN_ENV = {"GRAFANA_ADMIN_TOKEN": "glsa_x"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://g/alerting/grafana/abc-1/view", "abc-1"),
        ("https://g/grafana/alerting/grafana/abc/edit?tab=query", "abc"),
        ("http://g/alerting/abc/view/", "abc"),
        ("https://g/alerting/grafana/abc", "abc"),
        ("  abc_2 ", "abc_2"),
        ("https://g/alerting/new", None),
        ("https://g/alerting/grafana/a b/view", None),
        ("ftp://g/alerting/grafana/abc/view", None),
        ("javascript:alert(1)//x", None),
        ("/alerting/grafana/abc/view", None),
        ("", None),
        ("x" * 41, None),
    ],
)
def test_parse_rule_uid(value: str, expected: str | None) -> None:
    assert parse_rule_uid(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://g/dashboards/f/f1/ops", "f1"),
        ("https://g/sub/folders/f1/", "f1"),
        ("f1", "f1"),
        (" f1 ", "f1"),
        ("not a uid", None),
        ("https://g/", None),
    ],
)
def test_parse_folder_uid(value: str, expected: str | None) -> None:
    assert parse_folder_uid(value) == expected


def test_rule_view_url_keeps_the_sub_path() -> None:
    assert (
        rule_view_url("https://g/grafana/", "abc") == "https://g/grafana/alerting/grafana/abc/view"
    )


class TestSettings:
    def test_defaults(self) -> None:
        settings = Settings.from_env(ADMIN_ENV)
        assert (settings.session_ttl_minutes, settings.session_max_ttl_days) == (10080, 30)
        assert (settings.session_touch_interval_seconds, settings.session_recheck_hours) == (
            300,
            24,
        )
        assert settings.session_cookie_secure == "auto"
        assert settings.cors_origins == ("http://localhost:5173",)
        assert settings.migration_status_token is None

    def test_cors_origin_list(self) -> None:
        settings = Settings.from_env({**ADMIN_ENV, "CORS_ORIGIN": "https://a, https://b,"})
        assert settings.cors_origins == ("https://a", "https://b")

    def test_admin_credentials_are_required(self) -> None:
        with pytest.raises(ValueError, match="GRAFANA_ADMIN_TOKEN"):
            Settings.from_env({})
        with pytest.raises(ValueError, match="GRAFANA_ADMIN_TOKEN"):
            Settings.from_env({"GRAFANA_ADMIN_USERNAME": "admin"})
        settings = Settings.from_env(
            {"GRAFANA_ADMIN_USERNAME": "admin", "GRAFANA_ADMIN_PASSWORD": "pw"}
        )
        assert settings.has_admin_credentials

    def test_invalid_values_are_all_reported(self) -> None:
        with pytest.raises(ValueError) as info:
            Settings.from_env(
                {
                    **ADMIN_ENV,
                    "SESSION_COOKIE_SECURE": "yes",
                    "SESSION_MAX_TTL_DAYS": "forever",
                    "SESSION_SECRET": "short",
                }
            )
        message = str(info.value)
        for name in ("SESSION_COOKIE_SECURE", "SESSION_MAX_TTL_DAYS", "SESSION_SECRET"):
            assert name in message


class TestClassification:
    def test_compliant(self) -> None:
        assert classify_rule_against_schema(SCHEMA, COMPLIANT_LABELS, COMPLIANT_ANNOTATIONS) == []

    @pytest.mark.parametrize("value", ["TRUE", "true ", "false", ""])
    def test_the_operator_label_must_be_exactly_true(self, value: str) -> None:
        labels = {**COMPLIANT_LABELS, "Appchi - ops": value}
        assert classify_rule_against_schema(SCHEMA, labels, COMPLIANT_ANNOTATIONS) == [
            "Appchi-$operator"
        ]

    def test_send_alert_is_case_insensitive_and_component_falls_back_to_object(self) -> None:
        labels = {"Send-Alert": "True", "Appchi - ops": "true", "object": "db"}
        assert classify_rule_against_schema(SCHEMA, labels, COMPLIANT_ANNOTATIONS) == []


@pytest.mark.parametrize(
    "value", ["https://g/dashboards/f/..", "https://g/folders/.", "https://g/dashboards/f/a.b"]
)
def test_folder_uids_from_urls_are_validated(value: str) -> None:
    assert parse_folder_uid(value) is None


def test_touch_interval_never_exceeds_half_the_ttl(tmp_path: Path) -> None:
    db = Database(tmp_path / "s.db", SESSIONS_SCHEMA_SQL)
    store = SqliteSessionStore(
        db, ttl_seconds=120, max_ttl_seconds=3600, touch_interval_seconds=300
    )
    assert store._touch_interval == 60


def test_a_failed_commit_does_not_wedge_later_transactions(tmp_path: Path) -> None:
    db = Database(
        tmp_path / "t.db",
        "CREATE TABLE IF NOT EXISTS p (id INTEGER PRIMARY KEY);"
        "CREATE TABLE IF NOT EXISTS c (pid INTEGER REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED);",
    )
    with db.connection() as conn:
        conn.execute("PRAGMA foreign_keys=ON")
    # A deferred foreign-key violation fails COMMIT and SQLite keeps the transaction open.
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute("INSERT INTO c VALUES (99)")
    with db.transaction() as conn:
        conn.execute("INSERT INTO p VALUES (1)")
    db.close()
