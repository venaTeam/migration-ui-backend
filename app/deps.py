"""Per-app services and per-request dependencies."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

import httpx
from fastapi import Depends, Request

from app.compliance.client import ComplianceClient
from app.config import Settings
from app.errors import ApiError
from app.grafana.client import GrafanaClient
from app.registry.store import Registry
from app.session.middleware import STATE_KEY, SessionContext
from app.session.recheck import SessionRechecker
from app.session.store import SessionData, SessionStore
from app.standard import StandardSchemaCache

UNAUTHORIZED_MESSAGE = "Session expired or invalid."


@dataclass
class Services:
    settings: Settings
    registry: Registry
    sessions: SessionStore
    compliance: ComplianceClient
    # Every Grafana call after login goes through the admin account.
    admin: GrafanaClient
    grafana_http: httpx.AsyncClient
    standard_schema: StandardSchemaCache
    rechecker: SessionRechecker
    clock: Callable[[], float]

    def user_grafana(self, username: str, password: str) -> GrafanaClient:
        """The user's own credentials; used only to check them at login."""
        return GrafanaClient(
            self.grafana_http, self.settings.grafana_url, username=username, password=password
        )


@dataclass
class Caller:
    """The logged-in user behind a guarded request."""

    session: SessionContext
    data: SessionData

    @property
    def login(self) -> str:
        return self.data.login


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


def get_session(request: Request) -> SessionContext:
    context: SessionContext = getattr(request.state, STATE_KEY)
    return context


ServicesDep = Annotated[Services, Depends(get_services)]
SessionDep = Annotated[SessionContext, Depends(get_session)]


async def require_session(session: SessionDep, services: ServicesDep) -> Caller:
    """The auth guard: 401 unless the request carries a live session of a current user."""
    data = session.data
    if data is None or not await services.rechecker.ensure_current(session):
        raise ApiError(401, "Unauthorized", UNAUTHORIZED_MESSAGE)
    # The re-check refreshes this same object.
    return Caller(session, data)


CallerDep = Annotated[Caller, Depends(require_session)]
