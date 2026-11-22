"""POST /api/rules/load-list: several rules at once."""

from __future__ import annotations

import asyncio

import httpx
import respx
from fastapi.testclient import TestClient

from app.registry.store import Registry
from tests.conftest import GRAFANA, RULES, USERNAME, grafana_rule, mock_permissions, mock_schema

LIMIT_MESSAGE = {"error": "Bad Request", "message": "rule_urls must list 1 to 50 rule URLs."}


def url(uid: str) -> str:
    return f"{GRAFANA}/alerting/grafana/{uid}/view"


class TestLoadList:
    def test_sorts_rules_into_loadable_and_skipped(
        self, logged_in: TestClient, mock: respx.MockRouter, registry: Registry
    ) -> None:
        mock_schema(mock)
        mock_permissions(mock, [{"teamId": 7, "permission": 1}])
        f1_perms = mock_permissions(mock, [{"teamId": 7, "permission": 1}])
        mock_permissions(mock, [], folder="secret")
        mock.get(f"{RULES}/a").respond(json=grafana_rule("a", "A"))
        mock.get(f"{RULES}/b").respond(json=grafana_rule("b", ""))
        mock.get(f"{RULES}/gone").respond(404)
        mock.get(f"{RULES}/hidden").respond(json=grafana_rule("hidden", "H", folder="secret"))
        mock.get(f"{RULES}/v2").respond(json=grafana_rule("v2", "V", compliant=True))
        mock.get(f"{RULES}/done").respond(json=grafana_rule("done", "D"))
        mock.get(f"{RULES}/done-v2").respond(json=grafana_rule("done-v2", "D-v2", compliant=True))
        mock.get(f"{RULES}/redo").respond(json=grafana_rule("redo", "R"))
        mock.get(f"{RULES}/redo-v2").respond(404)
        for original in ("done", "redo"):
            registry.record_migration(
                original_uid=original,
                original_title=original,
                created_uid=f"{original}-v2",
                created_title=f"{original}-v2",
                folder_uid="f1",
                deleted_original=False,
                created_via="clone",
                migrated_by=USERNAME,
            )

        hand_typed = f"{GRAFANA}/alerting/b/edit?tab=query"
        rule_urls = [
            url("a"),
            "ftp://nope/alerting/grafana/x/view",
            hand_typed,
            url("gone"),
            url("a"),
            url("hidden"),
            url("v2"),
            url("done"),
            "redo",
        ]
        response = logged_in.post("/api/rules/load-list", json={"rule_urls": rule_urls})
        assert response.status_code == 200
        assert response.json() == {
            "rules": [
                {"uid": "a", "title": "A", "rule_url": url("a"), "folder_uid": "f1"},
                {"uid": "b", "title": "Untitled rule", "rule_url": url("b"), "folder_uid": "f1"},
                # Its copy is gone, so it can be migrated again.
                {"uid": "redo", "title": "R", "rule_url": url("redo"), "folder_uid": "f1"},
            ],
            "skipped": [
                {
                    "rule_url": "ftp://nope/alerting/grafana/x/view",
                    "reason": "invalid_url",
                    "message": "Not a Grafana rule URL.",
                },
                {
                    "rule_url": url("gone"),
                    "reason": "not_found",
                    "message": "Rule not found in Grafana.",
                },
                {
                    "rule_url": url("hidden"),
                    "reason": "forbidden",
                    "message": "You don't have access to this rule's folder.",
                },
                {
                    "rule_url": url("v2"),
                    "reason": "already_v2",
                    "message": "Already on the v2 standard.",
                },
                {
                    "rule_url": url("done"),
                    "reason": "migrated",
                    "message": "Already migrated by this tool.",
                },
            ],
        }
        # One permission check per folder.
        assert f1_perms.call_count == 1

    def test_bounds(self, logged_in: TestClient) -> None:
        bodies: list[dict[str, object]] = [
            {"rule_urls": []},
            {"rule_urls": [url(f"r{i}") for i in range(51)]},
            {"rule_urls": "a"},
            {"rule_urls": [1]},
            {},
        ]
        for body in bodies:
            response = logged_in.post("/api/rules/load-list", json=body)
            assert (response.status_code, response.json()) == (400, LIMIT_MESSAGE)

    def test_fifty_after_de_duplication_is_allowed(
        self, logged_in: TestClient, mock: respx.MockRouter
    ) -> None:
        mock_schema(mock)
        mock.get(url__regex=rf"{RULES}/r\d+").respond(404)
        rule_urls = [url(f"r{i}") for i in range(50)] * 2
        response = logged_in.post("/api/rules/load-list", json={"rule_urls": rule_urls})
        assert response.status_code == 200
        assert len(response.json()["skipped"]) == 50

    def test_grafana_calls_are_bounded(self, logged_in: TestClient, mock: respx.MockRouter) -> None:
        mock_schema(mock)
        in_flight = {"now": 0, "max": 0}

        async def slow_missing_rule(request: httpx.Request) -> httpx.Response:
            in_flight["now"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["now"])
            await asyncio.sleep(0.01)
            in_flight["now"] -= 1
            return httpx.Response(404)

        mock.get(url__regex=rf"{RULES}/r\d+").mock(side_effect=slow_missing_rule)
        rule_urls = [url(f"r{i}") for i in range(30)]
        assert (
            logged_in.post("/api/rules/load-list", json={"rule_urls": rule_urls}).status_code == 200
        )
        assert 1 < in_flight["max"] <= 8

    def test_a_grafana_failure_fails_the_request(
        self, logged_in: TestClient, mock: respx.MockRouter
    ) -> None:
        mock_schema(mock)
        mock.get(f"{RULES}/a").respond(500, text="boom")
        response = logged_in.post("/api/rules/load-list", json={"rule_urls": [url("a")]})
        assert response.status_code == 502

    def test_a_failed_copy_check_lists_the_rule_instead_of_failing(
        self, logged_in: TestClient, mock: respx.MockRouter, registry: Registry
    ) -> None:
        mock_schema(mock)
        mock_permissions(mock, [{"teamId": 7, "permission": 1}])
        mock.get(f"{RULES}/a").respond(json=grafana_rule("a", "A"))
        mock.get(f"{RULES}/b").respond(json=grafana_rule("b", "B"))
        mock.get(f"{RULES}/a-v2").respond(500, text="boom")
        registry.record_migration(
            original_uid="a",
            original_title="a",
            created_uid="a-v2",
            created_title="a-v2",
            folder_uid="f1",
            deleted_original=False,
            created_via="clone",
            migrated_by=USERNAME,
        )
        response = logged_in.post("/api/rules/load-list", json={"rule_urls": [url("a"), url("b")]})
        assert response.status_code == 200
        assert [rule["uid"] for rule in response.json()["rules"]] == ["a", "b"]

    def test_two_spellings_of_one_rule_list_it_once(
        self, logged_in: TestClient, mock: respx.MockRouter
    ) -> None:
        mock_schema(mock)
        mock_permissions(mock, [{"teamId": 7, "permission": 1}])
        mock.get(f"{RULES}/a").respond(json=grafana_rule("a", "A"))
        edit = f"{GRAFANA}/alerting/grafana/a/edit"
        response = logged_in.post("/api/rules/load-list", json={"rule_urls": [url("a"), edit]})
        assert [rule["uid"] for rule in response.json()["rules"]] == ["a"]
        assert response.json()["skipped"] == []
