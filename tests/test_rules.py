"""Rule routes, with Grafana and the compliance service mocked."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.registry.store import Registry
from tests.conftest import (
    ADMIN_TOKEN,
    COMPLIANCE,
    COMPLIANT_ANNOTATIONS,
    COMPLIANT_LABELS,
    GRAFANA,
    RULES,
    USERNAME,
    grafana_rule,
    login,
    mock_login,
    mock_permissions,
    mock_schema,
)

FOLDER_URL = f"{GRAFANA}/dashboards/f/f1/ops"
EVALUATION = {
    "rule_uid": "o1",
    "rule_title": "CPU High",
    "folder_uid": "f1",
    "standard_version": "0.1.0",
    "overall_status": "NON_COMPLIANT",
    "fields": [],
}
TEAM_EDIT = [{"teamId": 7, "permission": 2}]
TEAM_VIEW = [{"teamId": 7, "permission": 1}]
NO_EDIT = "You do not have Edit/Admin permission for the folder this alert rule belongs to."
ADMIN_REJECTED = (
    "Grafana rejected the alerts-migration admin account (HTTP 401). "
    "Ask an administrator to check the configured Grafana admin credentials."
)


def record_clone(registry: Registry, original: str, created: str, folder: str = "f1") -> None:
    registry.record_migration(
        original_uid=original,
        original_title="CPU High",
        created_uid=created,
        created_title="CPU High-v2",
        folder_uid=folder,
        deleted_original=False,
        created_via="clone",
        migrated_by=USERNAME,
    )


def clone_body(**extra: Any) -> dict[str, Any]:
    return {"labels": dict(COMPLIANT_LABELS), "annotations": dict(COMPLIANT_ANNOTATIONS), **extra}


class TestLoad:
    @pytest.fixture(autouse=True)
    def _rule(self, mock: respx.MockRouter) -> None:
        mock.get(f"{RULES}/o1").respond(json=grafana_rule("o1", "CPU High"))
        mock_permissions(mock, TEAM_VIEW)

    def test_evaluates_with_the_admin_token(
        self, logged_in: TestClient, mock: respx.MockRouter
    ) -> None:
        evaluate = mock.post(f"{COMPLIANCE}/api/v1/evaluate").respond(json=EVALUATION)
        response = logged_in.post(
            "/api/rules/load", json={"rule_url": f"{GRAFANA}/alerting/grafana/o1/view"}
        )
        assert (response.status_code, response.json()) == (200, EVALUATION)
        request = evaluate.calls.last.request
        assert request.headers["X-Grafana-Token"] == ADMIN_TOKEN
        assert request.headers["X-Grafana-Url"] == GRAFANA
        assert "X-Grafana-Password" not in request.headers
        assert json.loads(request.content) == {"rule_url": "o1", "grafana_url": GRAFANA}

    def test_fills_a_missing_folder_uid(
        self, logged_in: TestClient, mock: respx.MockRouter
    ) -> None:
        mock.post(f"{COMPLIANCE}/api/v1/evaluate").respond(json={**EVALUATION, "folder_uid": None})
        response = logged_in.post("/api/rules/load", json={"rule_url": "o1"})
        assert response.json()["folder_uid"] == "f1"

    def test_needs_view_on_the_rules_folder(
        self, logged_in: TestClient, mock: respx.MockRouter
    ) -> None:
        mock_permissions(mock, [{"teamId": 99, "permission": 4}])
        evaluate = mock.post(f"{COMPLIANCE}/api/v1/evaluate").respond(json=EVALUATION)
        response = logged_in.post("/api/rules/load", json={"rule_url": "o1"})
        assert response.status_code == 403
        assert response.json() == {
            "error": "Forbidden",
            "message": "You don't have access to this rule's folder.",
        }
        assert not evaluate.called

    def test_unknown_and_unparseable_rules(
        self, logged_in: TestClient, mock: respx.MockRouter
    ) -> None:
        mock.get(f"{RULES}/zz").respond(404)
        response = logged_in.post("/api/rules/load", json={"rule_url": "zz"})
        assert response.status_code == 404
        assert response.json()["message"] == "Rule 'zz' not found or not accessible"
        for bad in ("ftp://g/alerting/grafana/o1/view", "not a uid!", "https://g/alerting/new"):
            response = logged_in.post("/api/rules/load", json={"rule_url": bad})
            assert response.status_code == 400, bad
            assert response.json() == {"error": "Bad Request", "message": "Not a Grafana rule URL."}

    def test_flags_a_migrated_rule_whose_copy_exists(
        self, logged_in: TestClient, mock: respx.MockRouter, registry: Registry
    ) -> None:
        record_clone(registry, "o1", "c1")
        mock.post(f"{COMPLIANCE}/api/v1/evaluate").respond(json=EVALUATION)
        mock.get(f"{RULES}/c1").respond(json=grafana_rule("c1", "CPU High-v2"))
        response = logged_in.post("/api/rules/load", json={"rule_url": "o1"})
        assert response.json() == {**EVALUATION, "migrated": True}
        mock.get(f"{RULES}/c1").respond(404)
        assert "migrated" not in logged_in.post("/api/rules/load", json={"rule_url": "o1"}).json()

    def test_rejected_admin_credentials_are_a_502_not_a_401(
        self, logged_in: TestClient, mock: respx.MockRouter
    ) -> None:
        mock.post(f"{COMPLIANCE}/api/v1/evaluate").respond(401, json={"detail": "no"})
        response = logged_in.post("/api/rules/load", json={"rule_url": "o1"})
        assert response.status_code == 502
        assert response.json() == {"error": "Grafana Admin Error", "message": ADMIN_REJECTED}

        mock.get(f"{RULES}/o1").respond(401)
        response = logged_in.post("/api/rules/load", json={"rule_url": "o1"})
        assert (response.status_code, response.json()["error"]) == (502, "Grafana Admin Error")


class TestLoadFolder:
    def test_lists_the_folder_with_the_admin_account(
        self, logged_in: TestClient, mock: respx.MockRouter
    ) -> None:
        mock_permissions(mock, TEAM_VIEW)
        rules = [grafana_rule("a", "A"), grafana_rule("b", ""), grafana_rule("x", "X", folder="f2")]
        listing = mock.get(RULES).respond(json=rules)
        # A bare folder UID works too.
        for folder_url in (FOLDER_URL, "f1"):
            response = logged_in.post("/api/rules/load-folder", json={"folder_url": folder_url})
            assert response.json() == {
                "folder_uid": "f1",
                "rules": [
                    {"uid": "a", "title": "A", "rule_url": f"{GRAFANA}/alerting/grafana/a/view"},
                    {
                        "uid": "b",
                        "title": "Untitled rule",
                        "rule_url": f"{GRAFANA}/alerting/grafana/b/view",
                    },
                ],
            }
        assert listing.calls.last.request.headers["Authorization"] == f"Bearer {ADMIN_TOKEN}"

    def test_needs_view_on_the_folder(self, logged_in: TestClient, mock: respx.MockRouter) -> None:
        mock_permissions(mock, [])
        listing = mock.get(RULES).respond(json=[])
        for path in ("/api/rules/load-folder", "/api/rules/load-folder-classified"):
            response = logged_in.post(path, json={"folder_url": FOLDER_URL})
            assert response.status_code == 403
            assert response.json() == {
                "error": "Forbidden",
                "message": "You don't have access to this folder.",
            }
        assert not listing.called

    def test_unparseable_folder(self, logged_in: TestClient) -> None:
        for bad in (f"{GRAFANA}/", "has space", "a/b"):
            response = logged_in.post("/api/rules/load-folder", json={"folder_url": bad})
            assert response.status_code == 400, bad
            assert response.json()["message"] == "Could not parse folder UID from URL."

    def test_unknown_folder_and_rejected_admin(
        self, logged_in: TestClient, mock: respx.MockRouter
    ) -> None:
        mock.get(f"{GRAFANA}/api/folders/f1/permissions").respond(404)
        response = logged_in.post("/api/rules/load-folder", json={"folder_url": "f1"})
        assert (response.status_code, response.json()["message"]) == (404, "Folder 'f1' not found")
        mock.get(f"{GRAFANA}/api/folders/f1/permissions").respond(403)
        response = logged_in.post("/api/rules/load-folder", json={"folder_url": "f1"})
        assert (response.status_code, response.json()["error"]) == (502, "Grafana Admin Error")


class TestLoadFolderClassified:
    def test_splits_seeds_and_flags(
        self, logged_in: TestClient, mock: respx.MockRouter, registry: Registry
    ) -> None:
        mock_schema(mock)
        mock_permissions(mock, TEAM_VIEW)
        not_true = grafana_rule("w1", "Wrong-v2", compliant=True)
        not_true["labels"]["Appchi - ops"] = "TRUE"
        rules = [
            grafana_rule("o1", "CPU High"),
            grafana_rule("n1", "Disk Full"),
            grafana_rule("c1", "CPU High-v2", compliant=True),
            not_true,
        ]
        mock.get(RULES).respond(json=rules)
        response = logged_in.post("/api/rules/load-folder-classified", json={"folder_url": "f1"})
        missing = ["Send-Alert", "Appchi-$operator", "component", "message"]
        body = response.json()
        assert body["counts"] == {"v1": 3, "v2": 1}
        assert [(r["uid"], r.get("migrated"), r["missing_fields"]) for r in body["v1"]] == [
            ("n1", None, missing),
            # The operator label must be exactly "true", as in the engine.
            ("w1", None, ["Appchi-$operator"]),
            ("o1", True, missing),
        ]
        assert body["v2"] == [
            {"uid": "c1", "title": "CPU High-v2", "rule_url": f"{GRAFANA}/alerting/grafana/c1/view"}
        ]
        assert registry.is_folder_seeded("f1")

        # Deleting the v2 copy un-flags the original.
        mock.get(RULES).respond(json=rules[:2])
        response = logged_in.post("/api/rules/load-folder-classified", json={"folder_url": "f1"})
        assert all("migrated" not in rule for rule in response.json()["v1"])

    def test_schema_failure(self, logged_in: TestClient, mock: respx.MockRouter) -> None:
        mock_permissions(mock, TEAM_VIEW)
        mock.get(f"{COMPLIANCE}/api/v1/standard/schema").respond(500, json={})
        response = logged_in.post("/api/rules/load-folder-classified", json={"folder_url": "f1"})
        assert response.status_code == 502
        assert response.json() == {
            "error": "Folder load failed",
            "message": "Failed to load standard schema: 500",
        }


class TestClone:
    @pytest.fixture(autouse=True)
    def _original(self, mock: respx.MockRouter) -> None:
        mock_schema(mock)
        mock.get(f"{RULES}/o1").respond(json=grafana_rule("o1", "CPU High"))
        mock.get(RULES).respond(json=[grafana_rule("o1", "CPU High")])

    def test_permission_denied(self, logged_in: TestClient, mock: respx.MockRouter) -> None:
        mock_permissions(
            mock, [{"teamId": 99, "permission": 4}, {"userLogin": USERNAME, "permission": 1}]
        )
        create = mock.post(RULES).respond(json={"uid": "new1"})
        response = logged_in.post("/api/rules/o1/clone", json=clone_body())
        assert response.status_code == 403
        assert response.json() == {"error": "Request failed", "message": NO_EDIT}
        assert not create.called

    def test_allowed_by_team_creates_the_copy_and_records_it(
        self, logged_in: TestClient, mock: respx.MockRouter, registry: Registry
    ) -> None:
        mock_permissions(mock, TEAM_EDIT)
        create = mock.post(RULES).respond(json={"uid": "new1"})
        response = logged_in.post("/api/rules/o1/clone", json=clone_body())
        assert response.json() == {
            "status": "ok",
            "action": "created",
            "rule_uid": "new1",
            "rule_url": f"{GRAFANA}/alerting/grafana/new1/view",
            "title": "CPU High-v2",
        }
        request = create.calls.last.request
        assert request.headers["X-Disable-Provenance"] == "true"
        assert request.headers["Authorization"] == f"Bearer {ADMIN_TOKEN}"
        sent = json.loads(request.content)
        assert {"uid", "id", "updated", "provenance"}.isdisjoint(sent)
        assert (sent["title"], sent["folderUID"], sent["labels"]) == (
            "CPU High-v2",
            "f1",
            COMPLIANT_LABELS,
        )
        row = registry.latest_migration_for_original("o1")
        assert row is not None
        assert (row.created_uid, row.created_via, row.deleted_original, row.migrated_by) == (
            "new1",
            "clone",
            0,
            USERNAME,
        )

        mock.get(f"{RULES}/new1").respond(json=grafana_rule("new1", "CPU High-v2"))
        response = logged_in.post("/api/rules/o1/clone", json=clone_body())
        assert response.status_code == 409
        assert response.json()["message"] == (
            "This rule has already been migrated (copy: CPU High-v2). "
            "Delete the v2 copy first if you want to migrate it again."
        )

    def test_another_destination_needs_view_on_the_source(
        self, logged_in: TestClient, mock: respx.MockRouter
    ) -> None:
        mock_permissions(mock, [{"teamId": 7, "permission": 2}], folder="f2")
        mock.get(f"{RULES}").respond(json=[])
        create = mock.post(RULES).respond(json={"uid": "new1"})
        mock_permissions(mock, [])
        body = clone_body(folder_url=f"{GRAFANA}/dashboards/f/f2")
        response = logged_in.post("/api/rules/o1/clone", json=body)
        assert response.status_code == 403
        assert response.json()["message"] == (
            "You do not have access to the folder this alert rule belongs to."
        )
        assert not create.called

        mock_permissions(mock, TEAM_VIEW)
        response = logged_in.post("/api/rules/o1/clone", json=body)
        assert response.status_code == 200
        assert json.loads(create.calls.last.request.content)["folderUID"] == "f2"

    def test_updates_a_same_titled_rule_in_the_destination(
        self, logged_in: TestClient, mock: respx.MockRouter
    ) -> None:
        mock_permissions(mock, [{"userLogin": USERNAME, "permission": 4}])
        mock.get(RULES).respond(
            json=[grafana_rule("o1", "CPU High"), grafana_rule("c9", "CPU High-v2")]
        )
        update = mock.put(f"{RULES}/c9").respond(json={"uid": "c9"})
        response = logged_in.post("/api/rules/o1/clone", json=clone_body())
        assert response.json()["action"] == "updated"
        assert json.loads(update.calls.last.request.content)["uid"] == "c9"

    def test_missing_required_fields_and_invalid_payloads(self, logged_in: TestClient) -> None:
        body = {
            "labels": {**COMPLIANT_LABELS, "Appchi - ops": " TRUE"},
            "annotations": {"message": "  "},
        }
        response = logged_in.post("/api/rules/o1/clone", json=body)
        assert response.status_code == 400
        assert response.json()["message"] == (
            "Missing required fields: Appchi-$operator (label 'Appchi - ops' must have the value 'true'), message"
        )
        response = logged_in.post(
            "/api/rules/o1/clone", json={"labels": {"a": 1}, "annotations": {}}
        )
        assert response.json()["message"] == "labels and annotations are required."
        assert (
            logged_in.post("/api/rules/o1/clone", json=clone_body(folder_url=None)).status_code
            == 400
        )


class TestCloneAndDelete:
    @pytest.fixture(autouse=True)
    def _original(self, mock: respx.MockRouter) -> None:
        mock_schema(mock)
        mock.get(f"{RULES}/o1").respond(json=grafana_rule("o1", "CPU High"))
        mock.get(RULES).respond(json=[grafana_rule("o1", "CPU High")])

    def test_records_one_row(
        self, logged_in: TestClient, mock: respx.MockRouter, registry: Registry
    ) -> None:
        mock_permissions(mock, TEAM_EDIT)
        mock.post(RULES).respond(json={"uid": "new1"})
        delete = mock.delete(f"{RULES}/o1").respond(204)
        response = logged_in.post("/api/rules/o1/clone-and-delete", json=clone_body())
        assert (response.status_code, response.json()["original_deleted"]) == (200, True)
        assert delete.called
        rows = registry.migrations_for_folder("f1")
        assert [
            (r.original_uid, r.created_uid, r.created_via, r.deleted_original) for r in rows
        ] == [("o1", "new1", "clone-and-delete", 1)]

    def test_no_edit_on_the_source_changes_nothing(
        self, logged_in: TestClient, mock: respx.MockRouter
    ) -> None:
        mock_permissions(mock, TEAM_VIEW)
        mock_permissions(mock, TEAM_EDIT, folder="f2")
        create = mock.post(RULES).respond(json={"uid": "new1"})
        body = clone_body(folder_url=f"{GRAFANA}/dashboards/f/f2")
        response = logged_in.post("/api/rules/o1/clone-and-delete", json=body)
        assert response.status_code == 403
        assert response.json() == {
            "error": "Forbidden",
            "message": "You do not have Edit/Admin permission for the folder the original alert rule belongs to.",
        }
        assert not create.called

    def test_the_copy_is_recorded_even_when_the_delete_fails(
        self, logged_in: TestClient, mock: respx.MockRouter, registry: Registry
    ) -> None:
        mock_permissions(mock, TEAM_EDIT)
        mock.post(RULES).respond(json={"uid": "new1"})
        mock.delete(f"{RULES}/o1").respond(500, text="boom")
        response = logged_in.post("/api/rules/o1/clone-and-delete", json=clone_body())
        assert response.status_code == 502
        row = registry.latest_migration_for_original("o1")
        assert row is not None
        assert (row.created_uid, row.deleted_original) == ("new1", 0)


class TestFolderPermission:
    """Role rank, the login org's role, and the org-admin shortcut."""

    @pytest.fixture(autouse=True)
    def _original(self, mock: respx.MockRouter) -> None:
        mock_schema(mock)
        mock.get(f"{RULES}/o1").respond(json=grafana_rule("o1", "CPU High"))
        mock.get(RULES).respond(json=[])
        mock.post(RULES).respond(json={"uid": "new1"})

    def clone(
        self, client: TestClient, mock: respx.MockRouter, role: str, grants: list[dict[str, Any]]
    ) -> int:
        mock_login(mock, role=role, teams=[])
        assert login(client).status_code == 200
        mock_permissions(mock, grants)
        status: int = client.post("/api/rules/o1/clone", json=clone_body()).status_code
        return status

    @pytest.mark.parametrize(
        ("role", "grants", "expected"),
        [
            ("Editor", [{"builtInRole": "Editor", "permission": 2}], 200),
            ("Editor", [{"role": "Viewer", "permission": 2}], 200),
            ("Viewer", [{"role": "Editor", "permission": 2}], 403),
            # The user is Admin in org 2 only; the login org's role (Viewer) counts.
            ("Viewer", [{"role": "Admin", "permission": 4}], 403),
            ("Editor", [{"role": "Editor", "permission": 1}], 403),
            ("Viewer", [{"userLogin": USERNAME}], 403),
            ("Viewer", [{"userLogin": USERNAME, "permission": 2}], 200),
            # Org admins may edit any folder, as in Grafana.
            ("Admin", [], 200),
        ],
    )
    def test_grants(
        self,
        client: TestClient,
        mock: respx.MockRouter,
        role: str,
        grants: list[dict[str, Any]],
        expected: int,
    ) -> None:
        assert self.clone(client, mock, role, grants) == expected


class TestCreateAndView:
    def test_create_records_a_new_rule(
        self, logged_in: TestClient, mock: respx.MockRouter, registry: Registry
    ) -> None:
        mock_schema(mock)
        mock_permissions(mock, TEAM_EDIT)
        create = mock.post(RULES).respond(json={"uid": "r9"})
        body = {"folder_url": FOLDER_URL, "title": "New", **clone_body()}
        assert logged_in.post("/api/rules/create", json=body).json() == {
            "status": "ok",
            "action": "created",
            "rule_uid": "r9",
            "rule_url": f"{GRAFANA}/alerting/grafana/r9/view",
            "title": "New",
        }
        assert json.loads(create.calls.last.request.content)["ruleGroup"] == "default"
        rows = registry.migrations_for_folder("f1")
        assert [(r.original_uid, r.created_uid, r.created_via) for r in rows] == [
            (None, "r9", "create")
        ]

    def test_view_update_must_stay_v2(self, logged_in: TestClient, mock: respx.MockRouter) -> None:
        mock_schema(mock)
        body = {"title": "T", "labels": dict(COMPLIANT_LABELS), "annotations": {}}
        response = logged_in.put("/api/rules/view/c1", json=body)
        assert response.status_code == 400
        assert response.json() == {
            "error": "Validation Error",
            "message": "This edit would take the rule out of v2. Failing fields: message",
        }

    def test_view_update_merges_labels_and_annotations(
        self, logged_in: TestClient, mock: respx.MockRouter
    ) -> None:
        mock_schema(mock)
        existing = grafana_rule("c1", "Old", compliant=True)
        existing["labels"]["team"] = "ops"
        existing["annotations"]["summary"] = "keep me?"
        mock.get(f"{RULES}/c1").respond(json=existing)
        mock_permissions(mock, TEAM_EDIT)
        update = mock.put(f"{RULES}/c1").respond(json={"uid": "c1"})
        body = {
            "title": "New",
            "labels": {**COMPLIANT_LABELS, "Appchi - new": "true"},
            "annotations": {**COMPLIANT_ANNOTATIONS, "summary": ""},
            "clearLabels": ["Appchi - ops"],
        }
        assert logged_in.put("/api/rules/view/c1", json=body).json() == {
            "status": "ok",
            "updated": "c1",
            "title": "New",
            "rule_url": f"{GRAFANA}/alerting/grafana/c1/view",
        }
        sent = json.loads(update.calls.last.request.content)
        expected_labels = {**COMPLIANT_LABELS, "team": "ops", "Appchi - new": "true"}
        del expected_labels["Appchi - ops"]
        assert sent["labels"] == expected_labels
        assert sent["annotations"] == COMPLIANT_ANNOTATIONS

    def test_view_delete(
        self, logged_in: TestClient, mock: respx.MockRouter, registry: Registry
    ) -> None:
        mock.get(f"{RULES}/c1").respond(json=grafana_rule("c1", "CPU High-v2", compliant=True))
        mock_permissions(mock, [])
        response = logged_in.delete("/api/rules/view/c1")
        assert (response.status_code, response.json()) == (
            403,
            {"error": "Forbidden", "message": NO_EDIT},
        )
        mock_permissions(mock, TEAM_EDIT)
        mock.delete(f"{RULES}/c1").respond(204)
        assert logged_in.delete("/api/rules/view/c1").json() == {"status": "ok", "deleted": "c1"}
        rows = registry.migrations_for_folder("f1")
        assert [(r.created_uid, r.created_via, r.original_uid) for r in rows] == [
            ("c1", "view-delete", None)
        ]
        mock.get(f"{RULES}/zz").respond(404)
        response = logged_in.delete("/api/rules/view/zz")
        assert (response.status_code, response.json()["message"]) == (
            404,
            "Rule 'zz' not found or not accessible",
        )


class TestProxies:
    def test_backtest_validates_and_uses_the_admin_account(
        self, logged_in: TestClient, mock: respx.MockRouter
    ) -> None:
        route = mock.post(f"{COMPLIANCE}/api/v1/backtest").respond(json={"fired": 0})
        for bad in (
            {"rule_query": "up", "threshold": True},
            {"rule_query": "up", "threshold": 1, "window_hours": 0},
            {"rule_query": "", "threshold": 1},
        ):
            response = logged_in.post("/api/backtest", json=bad)
            assert (response.status_code, response.json()["message"]) == (
                400,
                "Invalid backtest payload.",
            )
        response = logged_in.post(
            "/api/backtest", json={"rule_query": "up", "threshold": 0.5, "window_hours": 48.0}
        )
        assert (response.status_code, response.json()) == (200, {"fired": 0})
        request = route.calls.last.request
        assert request.headers["X-Grafana-Token"] == ADMIN_TOKEN
        assert json.loads(request.content) == {
            "rule_query": "up",
            "threshold": 0.5,
            "condition": "gt",
            "for_duration_seconds": 0,
            "window_hours": 48,
        }

    def test_policies(self, logged_in: TestClient, mock: respx.MockRouter) -> None:
        route = mock.get(f"{COMPLIANCE}/api/v1/notification-policies").respond(
            json={"policies": []}
        )
        logged_in.get("/api/notification-policies", params={"prefix": "Team & Co"})
        assert route.calls.last.request.url.query == b"prefix=Team%20%26%20Co"
        assert route.calls.last.request.headers["X-Grafana-Token"] == ADMIN_TOKEN
        logged_in.get("/api/notification-policies?prefix=a&prefix=b")
        assert route.calls.last.request.url.query == b"prefix=Appchi%20-%20"
        route.respond(401, json={})
        response = logged_in.get("/api/notification-policies")
        assert (response.status_code, response.json()["error"]) == (502, "Grafana Admin Error")


class TestAdminVariants:
    def test_basic_auth_admin_and_a_grafana_sub_path(
        self, tmp_path: Path, mock: respx.MockRouter
    ) -> None:
        grafana = f"{GRAFANA}/grafana"
        settings = Settings(
            grafana_url=grafana,
            compliance_service_url=COMPLIANCE,
            registry_data_dir=str(tmp_path),
            grafana_admin_username="admin",
            grafana_admin_password="adminpw",
        )
        mock.get(f"{grafana}/api/user").respond(json={"id": 1, "login": USERNAME, "orgId": 1})
        mock.get(f"{grafana}/api/user/teams").respond(json=[{"id": 7, "name": "ops"}])
        mock.get(f"{grafana}/api/user/orgs").respond(json=[{"orgId": 1, "role": "Viewer"}])
        mock.get(f"{grafana}/api/org").respond(json={"id": 1})
        mock.get(f"{grafana}/api/v1/provisioning/alert-rules/o1").respond(
            json=grafana_rule("o1", "X")
        )
        mock.get(f"{grafana}/api/folders/f1/permissions").respond(json=TEAM_VIEW)
        evaluate = mock.post(f"{COMPLIANCE}/api/v1/evaluate").respond(json=EVALUATION)
        mock.get(f"{grafana}/api/v1/provisioning/alert-rules").respond(
            json=[grafana_rule("o1", "X")]
        )

        with TestClient(create_app(settings)) as client:
            assert login(client).status_code == 200
            assert client.post("/api/rules/load", json={"rule_url": "o1"}).status_code == 200
            folder = client.post("/api/rules/load-folder", json={"folder_url": "f1"}).json()

        request = evaluate.calls.last.request
        assert (request.headers["X-Grafana-Username"], request.headers["X-Grafana-Password"]) == (
            "admin",
            "adminpw",
        )
        assert "X-Grafana-Token" not in request.headers
        assert json.loads(request.content) == {
            "rule_url": "o1",
            "grafana_url": grafana,
            "grafana_username": "admin",
            "grafana_password": "adminpw",
        }
        assert folder["rules"][0]["rule_url"] == f"{grafana}/alerting/grafana/o1/view"

    def test_admin_credentials_are_required(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="GRAFANA_ADMIN_TOKEN"):
            create_app(Settings(registry_data_dir=str(tmp_path)))

    def test_grafana_unreachable_is_a_502(
        self, logged_in: TestClient, mock: respx.MockRouter
    ) -> None:
        mock.get(f"{GRAFANA}/api/folders/f1/permissions").mock(
            side_effect=httpx.ConnectError("down")
        )
        response = logged_in.post("/api/rules/load-folder", json={"folder_url": "f1"})
        assert response.status_code == 502
        assert response.json() == {
            "error": "Grafana API Error",
            "message": "Could not reach Grafana: down",
        }
