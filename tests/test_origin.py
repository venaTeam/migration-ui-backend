"""CORS headers and the Origin check on state-changing requests."""

from __future__ import annotations

from pathlib import Path

import respx
from fastapi.testclient import TestClient

from app.config import Settings, parse_origins
from app.main import create_app
from tests.conftest import GRAFANA, login, mock_login

UI = "https://migration.example"
OTHER_UI = "https://migration-dev.example"


def client_for(tmp_path: Path, cors_origin: str) -> TestClient:
    settings = Settings(
        grafana_url=GRAFANA,
        registry_data_dir=str(tmp_path),
        grafana_admin_token="t",
        cors_origins=parse_origins(cors_origin),
    )
    return TestClient(create_app(settings))


class TestCors:
    def test_echoes_an_allowed_origin_only(self, tmp_path: Path) -> None:
        with client_for(tmp_path, f"{UI}, {OTHER_UI}") as client:
            for origin in (UI, OTHER_UI):
                response = client.get("/api/health", headers={"Origin": origin})
                assert response.headers["access-control-allow-origin"] == origin
                assert response.headers["access-control-allow-credentials"] == "true"
            response = client.get("/api/health", headers={"Origin": "https://evil.example"})
            assert "access-control-allow-origin" not in response.headers
            assert response.headers["vary"] == "Origin"

    def test_preflight(self, tmp_path: Path) -> None:
        with client_for(tmp_path, UI) as client:
            response = client.options(
                "/api/rules/load",
                headers={"Origin": UI, "Access-Control-Request-Headers": "content-type"},
            )
        assert response.status_code == 204
        assert response.headers["access-control-allow-origin"] == UI
        assert response.headers["access-control-allow-headers"] == "content-type"
        assert response.headers["access-control-allow-methods"] == "GET,HEAD,PUT,PATCH,POST,DELETE"


class TestOriginCheck:
    def test_blocks_a_foreign_site(self, tmp_path: Path, mock: respx.MockRouter) -> None:
        mock_login(mock)
        with client_for(tmp_path, UI) as client:
            response = login(client, Origin="https://evil.example")
            assert response.status_code == 403
            assert response.json() == {
                "error": "Forbidden",
                "message": "Requests from this origin are not allowed.",
            }
            assert not mock.calls
            assert login(client, Origin="null").status_code == 403

    def test_allows_listed_own_and_absent_origins(
        self, tmp_path: Path, mock: respx.MockRouter
    ) -> None:
        mock_login(mock)
        with client_for(tmp_path, f"{UI},{OTHER_UI}") as client:
            assert login(client, Origin=OTHER_UI).status_code == 200
            assert login(client).status_code == 200
            # Our own host, as forwarded by the frontend's nginx: scheme and port don't matter.
            assert login(client, Origin="http://testserver:8080").status_code == 200
            assert (
                login(
                    client,
                    Origin="https://vena.apps.example",
                    **{"X-Forwarded-Host": "vena.apps.example", "X-Forwarded-Proto": "http"},
                ).status_code
                == 200
            )

    def test_reads_are_not_checked(self, tmp_path: Path) -> None:
        with client_for(tmp_path, UI) as client:
            response = client.get("/api/health", headers={"Origin": "https://evil.example"})
            assert response.status_code == 200
