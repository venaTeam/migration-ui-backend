"""Shared fixtures: the app with a temp data dir, a fake clock, and mocked Grafana/compliance."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.registry.store import Registry
from app.session.middleware import COOKIE_NAME
from app.session.store import SqliteSessionStore

GRAFANA = "http://grafana.test"
COMPLIANCE = "http://compliance.test"
USERNAME = "alice"
PASSWORD = "s3cret"
USER_ID = 42
ADMIN_TOKEN = "glsa_admin"
STATUS_TOKEN = "status-token-0123"
RULES = f"{GRAFANA}/api/v1/provisioning/alert-rules"
DAY = 86400.0

# Same shape as the real schema: an exact-value label, the operator label, a
# length-checked label with the object fallback, and a required annotation.
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
            "name": "Appchi-$operator",
            "location": "label",
            "required": True,
            "explanation": "",
            "constraints": {"starts_with": "Appchi - "},
            "default_value": None,
        },
        {
            "name": "component",
            "location": "label",
            "required": True,
            "explanation": "",
            "constraints": {"min_length": 2},
            "default_value": None,
        },
        {
            "name": "message",
            "location": "annotation",
            "required": True,
            "explanation": "",
            "constraints": {"min_length": 2},
            "default_value": None,
        },
    ],
}
COMPLIANT_LABELS = {"Send-Alert": "true", "Appchi - ops": "true", "component": "cpu"}
COMPLIANT_ANNOTATIONS = {"message": "CPU is high"}


class FakeClock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        grafana_url=GRAFANA,
        compliance_service_url=COMPLIANCE,
        registry_data_dir=str(tmp_path),
        session_secret="test-secret-0123456789",
        grafana_admin_token=ADMIN_TOKEN,
        migration_status_token=STATUS_TOKEN,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def app(settings: Settings, clock: FakeClock) -> FastAPI:
    return create_app(settings, clock=clock)


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    """Grafana and the compliance service; unmocked calls fail the test."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def client(app: FastAPI, mock: respx.MockRouter) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def registry(app: FastAPI) -> Registry:
    registry: Registry = app.state.services.registry
    return registry


@pytest.fixture
def store(app: FastAPI) -> SqliteSessionStore:
    store: SqliteSessionStore = app.state.services.sessions
    return store


def mock_login(
    mock: respx.MockRouter,
    *,
    role: str = "Viewer",
    teams: list[dict[str, Any]] | None = None,
) -> None:
    """Grafana as the user sees it at login: current org 1, where the user has `role`."""
    mock.get(f"{GRAFANA}/api/user").respond(
        json={"id": USER_ID, "login": USERNAME, "orgId": 1, "email": "alice@example.com"}
    )
    mock.get(f"{GRAFANA}/api/user/teams").respond(
        json=[{"id": 7, "name": "ops", "memberCount": 3}] if teams is None else teams
    )
    mock.get(f"{GRAFANA}/api/user/orgs").respond(
        json=[
            {"orgId": 2, "name": "Other", "role": "Admin"},
            {"orgId": 1, "name": "Main", "role": role},
        ]
    )


def use_cookie(client: TestClient, value: str) -> None:
    """Send this session cookie from now on (set as the server's own would be)."""
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, value, domain="testserver.local")


def login(client: TestClient, **headers: str) -> httpx.Response:
    response: httpx.Response = client.post(
        "/api/auth/login",
        json={"grafana_username": USERNAME, "grafana_password": PASSWORD},
        headers=headers,
    )
    return response


@pytest.fixture
def logged_in(client: TestClient, mock: respx.MockRouter) -> TestClient:
    mock_login(mock)
    assert login(client).status_code == 200
    return client


def mock_schema(mock: respx.MockRouter) -> None:
    mock.get(f"{COMPLIANCE}/api/v1/standard/schema").respond(json=SCHEMA)


def mock_permissions(
    mock: respx.MockRouter, items: list[dict[str, Any]], folder: str = "f1"
) -> respx.Route:
    return mock.get(f"{GRAFANA}/api/folders/{folder}/permissions").respond(json=items)


def grafana_rule(
    uid: str, title: str, *, folder: str = "f1", compliant: bool = False
) -> dict[str, Any]:
    return {
        "id": 1,
        "uid": uid,
        "title": title,
        "folderUID": folder,
        "ruleGroup": "g",
        "condition": "A",
        "data": [],
        "labels": dict(COMPLIANT_LABELS) if compliant else {"team": "ops"},
        "annotations": dict(COMPLIANT_ANNOTATIONS) if compliant else {},
        "provenance": "",
        "updated": "2026-01-01T00:00:00Z",
    }
