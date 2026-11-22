"""Error responses in the `{error, message}` shape the frontend reads."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from app.grafana.errors import (
    FolderNotFoundError,
    GrafanaAdminAuthError,
    GrafanaAPIError,
    GrafanaError,
    RuleNotFoundError,
)

ADMIN_ERROR = "Grafana Admin Error"


class ApiError(Exception):
    """An error answered as `{"error": ..., "message": ...}` with an HTTP status."""

    def __init__(self, status: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message


class BodyError(Exception):
    """A body express.json() would reject; answered as plain text, as Express did."""

    def __init__(self, status: int, text: str) -> None:
        super().__init__(text)
        self.status = status
        self.text = text


def error_response(status: int, error: str, message: str) -> JSONResponse:
    return JSONResponse({"error": error, "message": message}, status_code=status)


def compliance_error(exc: Exception) -> JSONResponse:
    """A call to the compliance service failed outright (network, bad JSON, ...)."""
    if isinstance(exc, ApiError | GrafanaError):
        return grafana_error(exc, "Compliance Service Error")
    return error_response(502, "Compliance Service Error", str(exc))


def grafana_error(exc: Exception, fallback_error: str) -> JSONResponse:
    """The answer for a failed Grafana-backed action; anything unexpected is a 502."""
    if isinstance(exc, ApiError):
        return error_response(exc.status, exc.error, exc.message)
    if isinstance(exc, GrafanaAdminAuthError):
        return error_response(502, ADMIN_ERROR, str(exc))
    if isinstance(exc, RuleNotFoundError | FolderNotFoundError):
        return error_response(404, "Not Found", str(exc))
    if isinstance(exc, GrafanaAPIError):
        status = 403 if exc.status_code == 403 else 502 if exc.status_code >= 500 else 400
        return error_response(status, "Grafana API Error", str(exc))
    return error_response(502, fallback_error, str(exc))


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> Response:
        return error_response(exc.status, exc.error, exc.message)

    @app.exception_handler(BodyError)
    async def _body_error(_request: Request, exc: BodyError) -> Response:
        return PlainTextResponse(exc.text, status_code=exc.status)
