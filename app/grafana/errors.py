"""Grafana client errors."""

from __future__ import annotations


class GrafanaError(Exception):
    pass


class GrafanaAuthError(GrafanaError):
    """The user's own credentials were rejected (login only)."""

    def __init__(self, message: str = "Invalid Grafana credentials") -> None:
        super().__init__(message)


class GrafanaAdminAuthError(GrafanaError):
    """Grafana rejected the service's admin account. Never answered as 401:
    the frontend would log the user out over a server misconfiguration."""

    def __init__(self, status_code: int) -> None:
        super().__init__(
            f"Grafana rejected the alerts-migration admin account (HTTP {status_code}). "
            "Ask an administrator to check the configured Grafana admin credentials."
        )
        self.status_code = status_code


class GrafanaForbiddenError(GrafanaError):
    """403 on an endpoint the caller can fall back from."""


class GrafanaAPIError(GrafanaError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class RuleNotFoundError(GrafanaError):
    def __init__(self, uid: str) -> None:
        super().__init__(f"Rule '{uid}' not found or not accessible")


class FolderNotFoundError(GrafanaError):
    def __init__(self, uid: str) -> None:
        super().__init__(f"Folder '{uid}' not found")
