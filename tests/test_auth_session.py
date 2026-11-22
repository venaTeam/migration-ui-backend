"""Login, logout, the auth guard, and the session lifecycle."""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.session.middleware import COOKIE_NAME, read_session_id, sign, unsign
from app.session.store import SESSIONS_FILE_NAME, SessionData, SqliteSessionStore, hash_session_id
from tests.conftest import (
    COMPLIANCE,
    DAY,
    GRAFANA,
    PASSWORD,
    USER_ID,
    USERNAME,
    FakeClock,
    login,
    mock_login,
    use_cookie,
)

UNAUTHORIZED = {"error": "Unauthorized", "message": "Session expired or invalid."}
GUARDED = [
    ("GET", "/api/standard/schema"),
    ("GET", "/api/notification-policies"),
    ("POST", "/api/backtest"),
    ("POST", "/api/rules/load"),
    ("POST", "/api/rules/load-list"),
    ("POST", "/api/rules/load-folder"),
    ("POST", "/api/rules/load-folder-classified"),
    ("POST", "/api/rules/create"),
    ("POST", "/api/rules/r1/clone"),
    ("POST", "/api/rules/r1/clone-and-delete"),
    ("PUT", "/api/rules/view/r1"),
    ("DELETE", "/api/rules/view/r1"),
]


def session_id(settings: Settings, client: TestClient) -> str:
    sid = read_session_id(f"{COOKIE_NAME}={client.cookies[COOKIE_NAME]}", settings.session_secret)
    assert sid is not None
    return sid


def session_of(settings: Settings, store: SqliteSessionStore, client: TestClient) -> SessionData:
    session = store.get(session_id(settings, client))
    assert session is not None
    return session


def mock_schema_proxy(mock: respx.MockRouter) -> respx.Route:
    return mock.get(f"{COMPLIANCE}/api/v1/standard/schema").respond(json={"version": "0.1.0"})


def sessions_file(settings: Settings) -> Path:
    return Path(settings.registry_data_dir) / SESSIONS_FILE_NAME


class TestLogin:
    def test_sets_the_session_cookie(
        self, client: TestClient, mock: respx.MockRouter, clock: FakeClock
    ) -> None:
        mock_login(mock)
        response = login(client)
        assert (response.status_code, response.json()) == (200, {"status": "ok"})
        cookie = response.headers["set-cookie"]
        assert cookie.startswith(f"{COOKIE_NAME}=s%3A")
        for attribute in ("; Path=/", "; HttpOnly", "; SameSite=Lax"):
            assert attribute in cookie
        assert "Secure" not in cookie
        expires = cookie.split("Expires=")[1].split(";")[0]
        # Seven days of idle time by default.
        assert abs(parsedate_to_datetime(expires).timestamp() - (clock.now + 7 * DAY)) < 2

    def test_checks_the_password_with_grafana_itself(
        self, client: TestClient, mock: respx.MockRouter
    ) -> None:
        mock_login(mock)
        login(client)
        expected = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
        paths = {call.request.url.path: call.request for call in mock.calls}
        # /api/org is the admin account finding its own org.
        user_paths = {p: r for p, r in paths.items() if p != "/api/org"}
        assert set(user_paths) == {"/api/user", "/api/user/teams", "/api/user/orgs"}
        assert all(r.headers["Authorization"] == f"Basic {expected}" for r in user_paths.values())

    def test_role_comes_from_the_admin_accounts_org(
        self,
        settings: Settings,
        store: SqliteSessionStore,
        client: TestClient,
        mock: respx.MockRouter,
    ) -> None:
        # Viewer in org 1 (the admin account's org), Admin in org 2 (the current org).
        mock_login(mock, role="Viewer")
        mock.get(f"{GRAFANA}/api/user").respond(json={"id": USER_ID, "login": USERNAME, "orgId": 2})
        login(client)
        assert session_of(settings, store, client).org_role == "Viewer"

    def test_keeps_who_the_user_is_and_never_the_password(
        self,
        settings: Settings,
        store: SqliteSessionStore,
        client: TestClient,
        mock: respx.MockRouter,
    ) -> None:
        mock_login(mock, role="Editor")
        login(client)
        session = session_of(settings, store, client)
        assert (session.login, session.user_id, session.org_role) == (USERNAME, USER_ID, "Editor")
        assert session.teams == [{"id": 7, "name": "ops"}]
        raw = sessions_file(settings).read_bytes()
        assert PASSWORD.encode() not in raw
        sid = session_id(settings, client)
        assert sid.encode() not in raw
        with closing(sqlite3.connect(sessions_file(settings))) as conn:
            assert conn.execute("SELECT id_hash FROM sessions").fetchall() == [
                (hash_session_id(sid),)
            ]

    def test_cookie_secure_modes(self, tmp_path: Path, mock: respx.MockRouter) -> None:
        for mode, proto, expected in [
            ("auto", "https, http", True),
            ("auto", "http", False),
            ("true", "http", True),
            ("false", "https", False),
        ]:
            settings = Settings(
                grafana_url=GRAFANA,
                registry_data_dir=str(tmp_path / mode / proto),
                grafana_admin_token="t",
                session_cookie_secure=mode,
            )
            mock_login(mock)
            with TestClient(create_app(settings)) as client:
                cookie = login(client, **{"X-Forwarded-Proto": proto}).headers["set-cookie"]
            assert ("; Secure" in cookie) is expected, (mode, proto)

    def test_missing_teams_are_filled_in_by_the_next_request(
        self,
        settings: Settings,
        store: SqliteSessionStore,
        client: TestClient,
        mock: respx.MockRouter,
    ) -> None:
        mock_login(mock)
        mock.get(f"{GRAFANA}/api/user/teams").respond(500, text="boom")
        assert login(client).status_code == 200
        session = session_of(settings, store, client)
        assert (session.org_role, session.teams, session.last_verified_at) == (None, [], 0.0)

        mock.get(f"{GRAFANA}/api/users/lookup").respond(
            json={"id": USER_ID, "login": USERNAME, "orgId": 1}
        )
        mock.get(f"{GRAFANA}/api/users/{USER_ID}/orgs").respond(
            json=[{"orgId": 1, "role": "Editor"}]
        )
        mock.get(f"{GRAFANA}/api/users/{USER_ID}/teams").respond(json=[{"id": 9, "name": "db"}])
        mock_schema_proxy(mock)
        assert client.get("/api/standard/schema").status_code == 200
        session = session_of(settings, store, client)
        assert (session.org_role, session.teams) == ("Editor", [{"id": 9, "name": "db"}])

    def test_rejects_bad_credentials(self, client: TestClient, mock: respx.MockRouter) -> None:
        mock.get(f"{GRAFANA}/api/user").respond(
            401, json={"message": "invalid username or password"}
        )
        response = login(client)
        assert response.status_code == 401
        assert response.json() == {
            "error": "Unauthorized",
            "message": "Invalid Grafana credentials.",
        }
        assert "set-cookie" not in response.headers

    def test_grafana_errors(self, client: TestClient, mock: respx.MockRouter) -> None:
        mock.get(f"{GRAFANA}/api/user").respond(503, text="down")
        response = login(client)
        assert response.status_code == 503
        assert response.json() == {
            "error": "Grafana API Error",
            "message": "Failed to fetch user: down",
        }

        mock.get(f"{GRAFANA}/api/user").mock(side_effect=httpx.ConnectError("refused"))
        response = login(client)
        assert response.status_code == 502
        assert response.json() == {
            "error": "Grafana API Error",
            "message": "Could not reach Grafana: refused",
        }

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"grafana_username": "alice"},
            {"grafana_username": "", "grafana_password": "x"},
            {"grafana_username": "alice", "grafana_password": 5},
            {"grafana_username": "alice", "grafana_password": None},
            ["alice", "x"],
        ],
    )
    def test_rejects_an_invalid_payload(self, client: TestClient, body: object) -> None:
        response = client.post("/api/auth/login", json=body)
        assert response.status_code == 400
        assert response.json() == {"error": "Bad Request", "message": "Invalid login payload."}

    def test_malformed_json_is_a_plain_400(self, client: TestClient) -> None:
        response = client.post(
            "/api/auth/login", content="{nope", headers={"Content-Type": "application/json"}
        )
        assert (response.status_code, response.text) == (400, "Bad Request")

    def test_a_non_json_body_counts_as_empty(self, client: TestClient) -> None:
        response = client.post(
            "/api/auth/login",
            content=json.dumps({"grafana_username": "a", "grafana_password": "b"}),
            headers={"Content-Type": "text/plain"},
        )
        assert response.status_code == 400
        assert response.json()["message"] == "Invalid login payload."

    def test_login_starts_a_new_session_id(
        self, logged_in: TestClient, mock: respx.MockRouter
    ) -> None:
        first = logged_in.cookies[COOKIE_NAME]
        assert login(logged_in).status_code == 200
        assert logged_in.cookies[COOKIE_NAME] != first
        use_cookie(logged_in, first)
        mock_schema_proxy(mock)
        assert logged_in.get("/api/standard/schema").status_code == 401


class TestGuard:
    @pytest.mark.parametrize(("method", "path"), GUARDED)
    def test_401_without_a_session(self, client: TestClient, method: str, path: str) -> None:
        response = client.request(method, path, json={})
        assert (response.status_code, response.json()) == (401, UNAUTHORIZED)
        assert "set-cookie" not in response.headers

    def test_a_tampered_cookie_is_no_session(
        self, logged_in: TestClient, mock: respx.MockRouter
    ) -> None:
        mock_schema_proxy(mock)
        value = logged_in.cookies[COOKIE_NAME]
        use_cookie(logged_in, value[:-2] + ("BB" if value.endswith("AA") else "AA"))
        assert logged_in.get("/api/standard/schema").status_code == 401

    def test_health_needs_no_session(self, client: TestClient) -> None:
        response = client.get("/api/health")
        assert (response.status_code, response.json()) == (200, {"status": "ok"})


class TestLifetime:
    def test_use_slides_the_idle_timeout(
        self, logged_in: TestClient, mock: respx.MockRouter, clock: FakeClock
    ) -> None:
        mock_schema_proxy(mock)
        for _ in range(3):
            clock.advance(6 * DAY)
            assert logged_in.get("/api/standard/schema").status_code == 200
        clock.advance(7 * DAY + 1)
        assert logged_in.get("/api/standard/schema").status_code == 401

    def test_the_absolute_cap_ends_even_an_active_session(
        self,
        settings: Settings,
        logged_in: TestClient,
        mock: respx.MockRouter,
        clock: FakeClock,
    ) -> None:
        mock_schema_proxy(mock)
        # Daily use also re-checks the user daily.
        mock.get(f"{GRAFANA}/api/users/lookup").respond(
            json={"id": USER_ID, "login": USERNAME, "orgId": 1}
        )
        mock.get(f"{GRAFANA}/api/users/{USER_ID}/orgs").respond(
            json=[{"orgId": 1, "role": "Viewer"}]
        )
        mock.get(f"{GRAFANA}/api/users/{USER_ID}/teams").respond(json=[])
        for day in range(29):
            clock.advance(DAY)
            response = logged_in.get("/api/standard/schema")
            assert response.status_code == 200, day
        # The cookie never outlives the cap either.
        expires = response.headers["set-cookie"].split("Expires=")[1].split(";")[0]
        assert parsedate_to_datetime(expires).timestamp() <= clock.now + DAY + 1
        clock.advance(DAY)
        assert logged_in.get("/api/standard/schema").status_code == 401

    def test_last_used_is_written_at_most_every_five_minutes(
        self,
        settings: Settings,
        store: SqliteSessionStore,
        logged_in: TestClient,
        mock: respx.MockRouter,
        clock: FakeClock,
    ) -> None:
        mock_schema_proxy(mock)
        logged_in_at = session_of(settings, store, logged_in).last_used_at
        clock.advance(299)
        logged_in.get("/api/standard/schema")
        assert session_of(settings, store, logged_in).last_used_at == logged_in_at
        clock.advance(2)
        logged_in.get("/api/standard/schema")
        assert session_of(settings, store, logged_in).last_used_at == clock.now

    def test_expired_rows_are_deleted(self, store: SqliteSessionStore, clock: FakeClock) -> None:
        idle = store.create(SessionData(login="idle"))
        clock.advance(DAY)
        fresh = store.create(SessionData(login="fresh"))
        clock.advance(6 * DAY)
        # `idle` has now been unused for 7 days, `fresh` for 6.
        assert store.delete_expired() == 1
        assert store.get(fresh) is not None
        assert store.get(idle) is None


class TestLogout:
    def test_deletes_the_session(
        self,
        settings: Settings,
        logged_in: TestClient,
        mock: respx.MockRouter,
    ) -> None:
        mock_schema_proxy(mock)
        old_cookie = logged_in.cookies[COOKIE_NAME]
        response = logged_in.post("/api/auth/logout")
        assert (response.status_code, response.json()) == (200, {"status": "ok"})
        assert response.headers["set-cookie"] == (
            f"{COOKIE_NAME}=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT"
        )
        with closing(sqlite3.connect(sessions_file(settings))) as conn:
            assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone() == (0,)
        use_cookie(logged_in, old_cookie)
        assert logged_in.get("/api/standard/schema").status_code == 401

    def test_without_a_session(self, client: TestClient) -> None:
        response = client.post("/api/auth/logout")
        assert (response.status_code, response.json()) == (200, {"status": "ok"})


class TestCookieSigning:
    def test_round_trip(self) -> None:
        assert unsign(sign("abc", "secret"), "secret") == "abc"

    def test_rejects_another_secret_or_a_changed_value(self) -> None:
        assert unsign(sign("abc", "secret"), "other") is None
        assert unsign(sign("abc", "secret").replace("abc", "abd"), "secret") is None

    def test_matches_express_session_format(self) -> None:
        # cookie-signature: base64 HMAC-SHA256 without padding.
        assert sign("abc", "secret") == "abc.mUba1OAOkT/Ivo5dP34RCkqegy+D+wnDRShdeGONig4"


class TestRobustness:
    def test_a_non_ascii_cookie_is_ignored_not_a_500(
        self, client: TestClient, mock: respx.MockRouter
    ) -> None:
        mock_login(mock)
        client.cookies.set(COOKIE_NAME, "s%3A%C3%A9.abc", domain="testserver.local")
        assert client.get("/api/health").status_code == 200
        assert login(client).status_code == 200

    def test_a_chunked_body_over_the_limit_is_refused(self, client: TestClient) -> None:
        def chunks() -> Iterator[bytes]:
            yield b'{"grafana_username": "'
            for _ in range(200):
                yield b"x" * 1024
            yield b'"}'

        response = client.post(
            "/api/auth/login", content=chunks(), headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 413

    def test_a_json_body_with_a_byte_order_mark_is_read(
        self, client: TestClient, mock: respx.MockRouter
    ) -> None:
        mock_login(mock)
        body = "\ufeff" + json.dumps({"grafana_username": USERNAME, "grafana_password": PASSWORD})
        response = client.post(
            "/api/auth/login",
            content=body.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200

    def test_start_and_stop_leave_no_registry_wal(self, settings: Settings, app: FastAPI) -> None:
        with TestClient(app):
            pass
        data_dir = Path(settings.registry_data_dir)
        assert (data_dir / "migrations.db").exists()
        assert not (data_dir / "migrations.db-wal").exists()
