"""The daily re-check of a session's user with the admin account."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.session.middleware import COOKIE_NAME, SessionContext
from app.session.recheck import SessionRechecker, TeamDirectory
from app.session.store import SessionData, SqliteSessionStore
from tests.conftest import (
    ADMIN_TOKEN,
    COMPLIANCE,
    DAY,
    GRAFANA,
    USER_ID,
    USERNAME,
    FakeClock,
    login,
    mock_login,
    use_cookie,
)
from tests.test_auth_session import session_of

LOOKUP = f"{GRAFANA}/api/users/lookup"


def mock_server_admin_lookup(
    mock: respx.MockRouter, *, role: str = "Editor", teams: list[dict[str, Any]] | None = None
) -> respx.Route:
    lookup = mock.get(LOOKUP, params={"loginOrEmail": USERNAME}).respond(
        json={"id": USER_ID, "login": USERNAME, "orgId": 1, "isDisabled": False}
    )
    mock.get(f"{GRAFANA}/api/users/{USER_ID}/orgs").respond(
        json=[{"orgId": 2, "role": "Admin"}, {"orgId": 1, "role": role}]
    )
    mock.get(f"{GRAFANA}/api/users/{USER_ID}/teams").respond(
        json=[{"id": 8, "name": "sre", "email": ""}] if teams is None else teams
    )
    return lookup


def schema_call(client: TestClient) -> int:
    status: int = client.get("/api/standard/schema").status_code
    return status


@pytest.fixture
def due(logged_in: TestClient, mock: respx.MockRouter, clock: FakeClock) -> TestClient:
    """A logged-in client whose re-check is due."""
    mock.get(f"{COMPLIANCE}/api/v1/standard/schema").respond(json={"version": "0.1.0"})
    clock.advance(DAY + 1)
    return logged_in


class TestServerAdminPath:
    def test_not_due_before_a_day(
        self, logged_in: TestClient, mock: respx.MockRouter, clock: FakeClock
    ) -> None:
        mock.get(f"{COMPLIANCE}/api/v1/standard/schema").respond(json={})
        lookup = mock_server_admin_lookup(mock)
        clock.advance(DAY - 60)
        assert schema_call(logged_in) == 200
        assert not lookup.called

    def test_refreshes_role_and_teams(
        self,
        settings: Settings,
        store: SqliteSessionStore,
        due: TestClient,
        mock: respx.MockRouter,
        clock: FakeClock,
    ) -> None:
        lookup = mock_server_admin_lookup(mock, role="Editor")
        assert schema_call(due) == 200
        assert lookup.calls.last.request.headers["Authorization"] == f"Bearer {ADMIN_TOKEN}"
        session = session_of(settings, store, due)
        assert (session.org_role, session.teams) == ("Editor", [{"id": 8, "name": "sre"}])
        assert session.last_verified_at == clock.now
        # Done for the day.
        assert schema_call(due) == 200
        assert lookup.call_count == 1

    @pytest.mark.parametrize(
        "lookup_response",
        [
            httpx.Response(404, json={"message": "user not found"}),
            httpx.Response(
                200, json={"id": USER_ID, "login": USERNAME, "orgId": 1, "isDisabled": True}
            ),
        ],
    )
    def test_a_user_gone_from_grafana_is_logged_out(
        self,
        settings: Settings,
        store: SqliteSessionStore,
        due: TestClient,
        mock: respx.MockRouter,
        lookup_response: httpx.Response,
    ) -> None:
        mock.get(LOOKUP).mock(return_value=lookup_response)
        old_cookie = due.cookies[COOKIE_NAME]
        response = due.get("/api/standard/schema")
        assert response.status_code == 401
        assert response.json()["message"] == "Session expired or invalid."
        assert response.headers["set-cookie"].startswith(f"{COOKIE_NAME}=;")
        use_cookie(due, old_cookie)
        assert schema_call(due) == 401

    @pytest.mark.parametrize(
        "failure",
        [httpx.Response(500, text="boom"), httpx.ConnectError("down"), httpx.Response(401)],
    )
    def test_an_outage_keeps_the_session_and_retries_later(
        self,
        due: TestClient,
        mock: respx.MockRouter,
        clock: FakeClock,
        failure: httpx.Response | Exception,
    ) -> None:
        route = mock.get(LOOKUP)
        if isinstance(failure, Exception):
            route.mock(side_effect=failure)
        else:
            route.mock(return_value=failure)
        assert schema_call(due) == 200
        assert schema_call(due) == 200
        assert route.call_count == 1
        clock.advance(301)
        assert schema_call(due) == 200
        assert route.call_count == 2


class TestOrgFallback:
    """The admin account is not a Grafana server admin: /api/users/lookup answers 403."""

    @pytest.fixture(autouse=True)
    def _not_server_admin(self, mock: respx.MockRouter) -> None:
        mock.get(LOOKUP).respond(403, json={"message": "Permission denied"})

    def mock_org(self, mock: respx.MockRouter, *, role: str = "Editor") -> dict[str, respx.Route]:
        return {
            "users": mock.get(f"{GRAFANA}/api/org/users/search").respond(
                json={
                    "totalCount": 2,
                    "orgUsers": [
                        {"userId": 5, "login": "alice2", "role": "Admin"},
                        {"userId": USER_ID, "login": USERNAME, "role": role},
                    ],
                    "page": 1,
                    "perPage": 1000,
                }
            ),
            "teams": mock.get(f"{GRAFANA}/api/teams/search").respond(
                json={"totalCount": 2, "teams": [{"id": 8, "name": "sre"}, {"id": 9, "name": "db"}]}
            ),
            "members8": mock.get(f"{GRAFANA}/api/teams/8/members").respond(
                json=[{"login": USERNAME, "userId": USER_ID}]
            ),
            "members9": mock.get(f"{GRAFANA}/api/teams/9/members").respond(
                json=[{"login": "bob", "userId": 3}]
            ),
        }

    def test_refreshes_from_the_org_and_shares_the_team_cache(
        self,
        settings: Settings,
        store: SqliteSessionStore,
        client: TestClient,
        two_sessions: tuple[str, str],
        mock: respx.MockRouter,
        clock: FakeClock,
    ) -> None:
        routes = self.mock_org(mock, role="Editor")
        first, second = two_sessions
        clock.advance(DAY + 1)
        use_cookie(client, first)
        assert schema_call(client) == 200
        session = session_of(settings, store, client)
        assert (session.org_role, session.teams) == ("Editor", [{"id": 8, "name": "sre"}])
        assert routes["users"].calls.last.request.url.params["query"] == USERNAME
        # Another session within the hour reuses the team memberships.
        use_cookie(client, second)
        assert schema_call(client) == 200
        assert routes["users"].call_count == 2
        assert routes["teams"].call_count == 1
        assert routes["members8"].call_count == 1

    def test_a_user_missing_from_the_org_is_logged_out(
        self, due: TestClient, mock: respx.MockRouter
    ) -> None:
        mock.get(f"{GRAFANA}/api/org/users/search").respond(
            json={"totalCount": 1, "orgUsers": [{"userId": 5, "login": "alice2", "role": "Admin"}]}
        )
        assert schema_call(due) == 401


@pytest.fixture
def two_sessions(client: TestClient, mock: respx.MockRouter) -> tuple[str, str]:
    """Two sessions of the same user: their cookies."""
    mock.get(f"{COMPLIANCE}/api/v1/standard/schema").respond(json={"version": "0.1.0"})
    mock_login(mock)
    assert login(client).status_code == 200
    first = client.cookies[COOKIE_NAME]
    client.cookies.clear()
    assert login(client).status_code == 200
    return first, client.cookies[COOKIE_NAME]


class TestConcurrentRequests:
    def test_parallel_requests_share_one_recheck(
        self, tmp_path: Any, mock: respx.MockRouter
    ) -> None:
        clock = FakeClock()
        from app.db import Database
        from app.grafana.client import GrafanaClient
        from app.session.store import SESSIONS_SCHEMA_SQL

        store = SqliteSessionStore(
            Database(tmp_path / "s.db", SESSIONS_SCHEMA_SQL),
            ttl_seconds=7 * DAY,
            max_ttl_seconds=30 * DAY,
            touch_interval_seconds=300,
            clock=clock,
        )

        async def slow_lookup(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.02)
            return httpx.Response(200, json={"id": USER_ID, "login": USERNAME, "orgId": 1})

        lookup = mock.get(LOOKUP).mock(side_effect=slow_lookup)
        mock.get(f"{GRAFANA}/api/users/{USER_ID}/orgs").respond(
            json=[{"orgId": 1, "role": "Viewer"}]
        )
        mock.get(f"{GRAFANA}/api/users/{USER_ID}/teams").respond(json=[])

        async def run() -> list[bool]:
            async with httpx.AsyncClient() as http:
                admin = GrafanaClient(http, GRAFANA, token=ADMIN_TOKEN, admin=True)
                rechecker = SessionRechecker(
                    admin, TeamDirectory(admin, clock=clock), interval_seconds=DAY, clock=clock
                )
                sid = store.create(SessionData(login=USERNAME, user_id=USER_ID))
                contexts = [SessionContext(store, sid, store.get(sid)) for _ in range(5)]
                return await asyncio.gather(*(rechecker.ensure_current(c) for c in contexts))

        assert asyncio.run(run()) == [True] * 5
        assert lookup.call_count == 1
