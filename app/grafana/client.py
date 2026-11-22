"""Grafana HTTP API client, for the admin account or (at login only) a user."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from app.grafana.errors import (
    FolderNotFoundError,
    GrafanaAdminAuthError,
    GrafanaAPIError,
    GrafanaAuthError,
    GrafanaForbiddenError,
    RuleNotFoundError,
)
from app.jsonio import dumps
from app.urls import quote_component

GRAFANA_TIMEOUT_SECONDS = 15.0
ALERT_RULES_PATH = "/api/v1/provisioning/alert-rules"
PAGE_SIZE = 1000
_JSON = "application/json"


class GrafanaClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        admin: bool = False,
        timeout: float = GRAFANA_TIMEOUT_SECONDS,
    ) -> None:
        self._http = http
        self._base_url = base_url
        self._org_id: int | None = None
        self._username = username
        self._password = password
        self._token = token
        self._admin = admin
        self._timeout = timeout

    def _authorization(self) -> str:
        if self._token:
            return f"Bearer {self._token}"
        pair = f"{self._username}:{self._password}".encode()
        return "Basic " + base64.b64encode(pair).decode("ascii")

    async def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        disable_provenance: bool = False,
    ) -> tuple[int, str]:
        # Appended, so a sub-path in GRAFANA_URL is kept.
        url = self._base_url.removesuffix("/") + path
        headers = {"Accept": _JSON, "Content-Type": _JSON, "Authorization": self._authorization()}
        if disable_provenance:
            headers["X-Disable-Provenance"] = "true"
        try:
            response = await self._http.request(
                method,
                url,
                headers=headers,
                content=None if body is None else dumps(body),
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            raise GrafanaAPIError(
                f"Request to Grafana timed out after {int(self._timeout * 1000)}ms", 504
            ) from None
        except httpx.HTTPError as exc:
            raise GrafanaAPIError(f"Could not reach Grafana: {exc}", 502) from exc
        return response.status_code, response.text

    def _check(self, status: int, body: str, action: str, rule_uid: str | None = None) -> None:
        if self._admin and status in (401, 403):
            raise GrafanaAdminAuthError(status)
        if status == 401:
            raise GrafanaAuthError()
        if rule_uid is not None and status == 404:
            raise RuleNotFoundError(rule_uid)
        if not 200 <= status < 300:
            raise GrafanaAPIError(f"Failed to {action}: {body}", status)

    async def _get(self, path: str, action: str) -> Any:
        status, body = await self._request(path)
        self._check(status, body, action)
        return json.loads(body)

    # Alert rules

    async def get_rule(self, uid: str) -> dict[str, Any]:
        status, body = await self._request(f"{ALERT_RULES_PATH}/{quote_component(uid)}")
        self._check(status, body, "fetch rule", uid)
        return json.loads(body)  # type: ignore[no-any-return]

    async def create_editable_rule(self, rule: dict[str, Any]) -> dict[str, Any]:
        status, body = await self._request(
            ALERT_RULES_PATH, method="POST", body=rule, disable_provenance=True
        )
        self._check(status, body, "create editable rule")
        return json.loads(body)  # type: ignore[no-any-return]

    async def list_rules(self) -> list[dict[str, Any]]:
        rules = await self._get(ALERT_RULES_PATH, "list rules")
        if not isinstance(rules, list):
            raise TypeError("Grafana returned a non-list for the alert rules")
        return rules

    async def list_rules_in_folder(self, folder_uid: str) -> list[dict[str, Any]]:
        return [rule for rule in await self.list_rules() if rule.get("folderUID") == folder_uid]

    async def update_rule(self, uid: str, rule: dict[str, Any]) -> dict[str, Any]:
        status, body = await self._request(
            f"{ALERT_RULES_PATH}/{quote_component(uid)}",
            method="PUT",
            body=rule,
            disable_provenance=True,
        )
        self._check(status, body, "update rule", uid)
        return json.loads(body)  # type: ignore[no-any-return]

    async def delete_rule(self, uid: str) -> None:
        status, body = await self._request(
            f"{ALERT_RULES_PATH}/{quote_component(uid)}", method="DELETE"
        )
        self._check(status, body, "delete rule", uid)

    async def get_folder_permissions(self, folder_uid: str) -> list[dict[str, Any]]:
        status, body = await self._request(
            f"/api/folders/{quote_component(folder_uid)}/permissions"
        )
        if status == 404:
            raise FolderNotFoundError(folder_uid)
        self._check(status, body, "fetch folder permissions")
        parsed = json.loads(body)
        return parsed if isinstance(parsed, list) else parsed.get("items") or []

    # The signed-in user (login, with the user's own credentials)

    async def get_current_user(self) -> dict[str, Any]:
        return await self._get("/api/user", "fetch user")  # type: ignore[no-any-return]

    async def get_current_user_orgs(self) -> list[dict[str, Any]]:
        return await self._get("/api/user/orgs", "fetch user orgs")  # type: ignore[no-any-return]

    async def get_current_user_teams(self) -> list[dict[str, Any]]:
        return await self._get("/api/user/teams", "fetch user teams")  # type: ignore[no-any-return]

    async def current_org_id(self) -> int:
        """The org these credentials work in (cached): where folders and rules are read."""
        if self._org_id is None:
            org = await self._get("/api/org", "fetch current org")
            self._org_id = int(org["id"])
        return self._org_id

    # Other users (admin account, for the session re-check)

    async def lookup_user(self, login: str) -> dict[str, Any] | None:
        """The user by login; None when Grafana has no such user.

        Raises GrafanaForbiddenError when the admin account is not a Grafana server admin.
        """
        status, body = await self._request(
            f"/api/users/lookup?loginOrEmail={quote_component(login)}"
        )
        if status == 404:
            return None
        if status == 403:
            raise GrafanaForbiddenError("user lookup needs a Grafana server admin")
        self._check(status, body, "look up user")
        return json.loads(body)  # type: ignore[no-any-return]

    async def get_user_orgs(self, user_id: int) -> list[dict[str, Any]]:
        return await self._get(f"/api/users/{user_id}/orgs", "fetch user orgs")  # type: ignore[no-any-return]

    async def get_user_teams(self, user_id: int) -> list[dict[str, Any]]:
        return await self._get(f"/api/users/{user_id}/teams", "fetch user teams")  # type: ignore[no-any-return]

    async def find_org_user(self, login: str) -> dict[str, Any] | None:
        """The current org's member with exactly this login (with its role), or None."""
        page = 1
        while True:
            result = await self._get(
                f"/api/org/users/search?query={quote_component(login)}"
                f"&perpage={PAGE_SIZE}&page={page}",
                "search org users",
            )
            users = result.get("orgUsers") or []
            match = next((user for user in users if user.get("login") == login), None)
            if match is not None or len(users) < PAGE_SIZE:
                return match
            page += 1

    async def list_teams(self) -> list[dict[str, Any]]:
        teams: list[dict[str, Any]] = []
        page = 1
        while True:
            result = await self._get(
                f"/api/teams/search?perpage={PAGE_SIZE}&page={page}", "search teams"
            )
            batch = result.get("teams") or []
            teams.extend(batch)
            if len(batch) < PAGE_SIZE:
                return teams
            page += 1

    async def get_team_members(self, team_id: int) -> list[dict[str, Any]]:
        return await self._get(f"/api/teams/{team_id}/members", "fetch team members")  # type: ignore[no-any-return]
