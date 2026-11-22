"""Client for the compliance service's /api/v1 endpoints, always as the admin account."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.grafana.errors import GrafanaAdminAuthError
from app.jsonio import dumps

log = logging.getLogger(__name__)

# Node's fetch (undici) defaults: 10 s to connect, 300 s for the response.
COMPLIANCE_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
# What fetch() says for every network failure; the frontend shows this text.
FETCH_FAILED = "fetch failed"


class ComplianceFetchError(Exception):
    pass


def _byte_string(value: str) -> bytes:
    # fetch() sent header values as Latin-1 and rejected anything wider.
    for index, char in enumerate(value):
        if ord(char) > 255:
            raise TypeError(
                "Cannot convert argument to a ByteString because the character at index "
                f"{index} has a value of {ord(char)} which is greater than 255."
            )
    return value.encode("latin-1")


class ComplianceClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        *,
        grafana_url: str,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._http = http
        self._base_url = base_url
        self._grafana_url = grafana_url
        self._token = token
        self._username = username
        self._password = password

    def url(self, path: str) -> str:
        return f"{self._base_url.removesuffix('/')}/api/v1{path}"

    def _credential_headers(self) -> list[tuple[bytes, bytes]]:
        headers = [(b"X-Grafana-Url", _byte_string(self._grafana_url))]
        if self._token:
            headers.append((b"X-Grafana-Token", _byte_string(self._token)))
        else:
            headers.append((b"X-Grafana-Username", _byte_string(self._username or "")))
            headers.append((b"X-Grafana-Password", _byte_string(self._password or "")))
        return headers

    async def request(self, method: str, path: str, *, json_body: Any = None) -> httpx.Response:
        """Call the service; a 401/403 means it rejected the admin credentials."""
        headers = [
            (b"Content-Type", b"application/json"),
            (b"Accept", b"application/json"),
            *self._credential_headers(),
        ]
        try:
            response = await self._http.request(
                method,
                self.url(path),
                headers=headers,
                content=None if json_body is None else dumps(json_body),
            )
        except httpx.HTTPError as exc:
            log.warning("compliance %s %s failed: %r", method, path.split("?")[0], exc)
            raise ComplianceFetchError(FETCH_FAILED) from exc
        if response.status_code in (401, 403):
            raise GrafanaAdminAuthError(response.status_code)
        return response

    async def evaluate(self, rule_uid: str) -> httpx.Response:
        body: dict[str, Any] = {"rule_url": rule_uid, "grafana_url": self._grafana_url}
        if not self._token:
            # Without a token the service still takes the credentials in the body.
            body["grafana_username"] = self._username
            body["grafana_password"] = self._password
        return await self.request("POST", "/evaluate", json_body=body)
