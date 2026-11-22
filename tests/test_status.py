"""POST /api/rules/migration-status, for alerts-bi."""

from __future__ import annotations

from pathlib import Path

import httpx
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.registry.store import Registry
from tests.conftest import ADMIN_TOKEN, GRAFANA, RULES, STATUS_TOKEN

PATH = "/api/rules/migration-status"
AUTH = {"Authorization": f"Bearer {STATUS_TOKEN}"}


def record(registry: Registry, original: str, created: str) -> None:
    registry.record_migration(
        original_uid=original,
        original_title=None,
        created_uid=created,
        created_title=f"{created} title",
        folder_uid="f1",
        deleted_original=False,
        created_via="clone",
        migrated_by="alice",
    )


class TestMigrationStatus:
    def test_lists_rules_whose_copy_still_exists(
        self, client: TestClient, mock: respx.MockRouter, registry: Registry
    ) -> None:
        record(registry, "a", "a-v2")
        record(registry, "b", "b-v2")
        # The latest row counts: c's newest copy is gone, though an older one exists.
        record(registry, "c", "c-old")
        record(registry, "c", "c-new")
        listing = mock.get(RULES).respond(json=[{"uid": "a-v2"}, {"uid": "c-old"}, {"uid": "x"}])
        response = client.post(
            PATH, json={"rule_uids": ["b", "a", "c", "never", "a"]}, headers=AUTH
        )
        assert (response.status_code, response.json()) == (200, {"migrated": ["a"]})
        assert listing.calls.last.request.headers["Authorization"] == f"Bearer {ADMIN_TOKEN}"

    def test_a_hundred_uids_cost_one_grafana_call(
        self, client: TestClient, mock: respx.MockRouter, registry: Registry
    ) -> None:
        for i in range(100):
            record(registry, f"r{i}", f"r{i}-v2")
        listing = mock.get(RULES).respond(json=[{"uid": f"r{i}-v2"} for i in range(0, 100, 2)])
        uids = [f"r{i}" for i in range(100)]
        response = client.post(PATH, json={"rule_uids": uids}, headers=AUTH)
        assert response.json() == {"migrated": uids[::2]}
        assert len(mock.calls) == 1
        assert listing.call_count == 1

    def test_no_grafana_call_without_registry_rows(
        self, client: TestClient, mock: respx.MockRouter
    ) -> None:
        response = client.post(PATH, json={"rule_uids": ["a", "b"]}, headers=AUTH)
        assert response.json() == {"migrated": []}
        assert len(mock.calls) == 0

    def test_needs_the_token(self, client: TestClient) -> None:
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": STATUS_TOKEN}):
            response = client.post(PATH, json={"rule_uids": ["a"]}, headers=headers)
            assert response.status_code == 401, headers
            assert response.json() == {"error": "Unauthorized", "message": "Invalid service token."}

    def test_needs_no_session_and_no_origin(
        self, client: TestClient, mock: respx.MockRouter
    ) -> None:
        headers = {**AUTH, "Origin": "https://alerts-bi.example"}
        response = client.post(PATH, json={"rule_uids": ["a"]}, headers=headers)
        assert response.status_code == 200

    def test_validates_the_body(self, client: TestClient) -> None:
        bodies: list[dict[str, object]] = [
            {"rule_uids": []},
            {"rule_uids": ["a"] * 101},
            {"rule_uids": [1]},
            {},
        ]
        for body in bodies:
            response = client.post(PATH, json=body, headers=AUTH)
            assert response.status_code == 400
            assert response.json()["message"] == "rule_uids must list 1 to 100 rule UIDs."

    def test_grafana_failure_is_a_502(
        self, client: TestClient, mock: respx.MockRouter, registry: Registry
    ) -> None:
        record(registry, "a", "a-v2")
        for failure in (httpx.Response(500), httpx.Response(401)):
            mock.get(RULES).mock(return_value=failure)
            assert client.post(PATH, json={"rule_uids": ["a"]}, headers=AUTH).status_code == 502
        mock.get(RULES).mock(side_effect=httpx.ConnectTimeout("slow"))
        assert client.post(PATH, json={"rule_uids": ["a"]}, headers=AUTH).status_code == 502

    def test_off_without_a_configured_token(self, tmp_path: Path) -> None:
        settings = Settings(
            grafana_url=GRAFANA, registry_data_dir=str(tmp_path), grafana_admin_token="t"
        )
        with TestClient(create_app(settings)) as client:
            response = client.post(PATH, json={"rule_uids": ["a"]}, headers=AUTH)
        assert (response.status_code, response.json()) == (404, {"detail": "Not Found"})
