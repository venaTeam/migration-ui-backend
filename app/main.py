"""FastAPI application factory: `uvicorn app.main:create_app --factory`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.compliance.client import COMPLIANCE_TIMEOUT, ComplianceClient
from app.config import ADMIN_CREDENTIALS_REQUIRED, Settings
from app.cors import CorsMiddleware, OriginCheckMiddleware
from app.db import Database
from app.deps import Services
from app.errors import register_error_handlers
from app.grafana.client import GrafanaClient
from app.registry.store import REGISTRY_FILE_NAME, REGISTRY_SCHEMA_SQL, Registry
from app.routes import auth, backtest, policies, rules, standard, status
from app.session.middleware import SessionMiddleware
from app.session.recheck import SessionRechecker, TeamDirectory
from app.session.store import SESSIONS_FILE_NAME, SESSIONS_SCHEMA_SQL, SqliteSessionStore
from app.standard import StandardSchemaCache

log = logging.getLogger(__name__)

SESSION_CLEANUP_SECONDS = 3600
_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}


async def _clean_up_sessions(sessions: SqliteSessionStore, rechecker: SessionRechecker) -> None:
    while True:
        await asyncio.sleep(SESSION_CLEANUP_SECONDS)
        try:
            removed = sessions.delete_expired()
            if removed:
                log.info("[sessions] removed %d expired session(s)", removed)
        except Exception:
            log.warning("[sessions] cleanup failed", exc_info=True)
        rechecker.prune()


def create_app(
    settings: Settings | None = None, *, clock: Callable[[], float] = time.time
) -> FastAPI:
    if settings is None:
        load_dotenv(Path.cwd() / ".env")
        settings = Settings.from_env()
    if not settings.has_admin_credentials:
        raise ValueError(f"Invalid configuration: {ADMIN_CREDENTIALS_REQUIRED}")

    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("app").setLevel(_LOG_LEVELS[settings.log_level])

    data_dir = Path(settings.registry_data_dir)
    registry_db = Database(data_dir / REGISTRY_FILE_NAME, REGISTRY_SCHEMA_SQL)
    registry = Registry(registry_db)
    # A file of its own, so the registry file stays readable by the Express service.
    sessions_db = Database(data_dir / SESSIONS_FILE_NAME, SESSIONS_SCHEMA_SQL)
    sessions = SqliteSessionStore(
        sessions_db,
        ttl_seconds=settings.session_ttl_minutes * 60,
        max_ttl_seconds=settings.session_max_ttl_days * 86400,
        touch_interval_seconds=settings.session_touch_interval_seconds,
        clock=clock,
    )

    # The token wins over the admin username/password.
    token = settings.grafana_admin_token or None
    username = None if token else settings.grafana_admin_username
    password = None if token else settings.grafana_admin_password
    grafana_http = httpx.AsyncClient(verify=not settings.grafana_insecure_tls)
    compliance_http = httpx.AsyncClient(timeout=COMPLIANCE_TIMEOUT)
    admin = GrafanaClient(
        grafana_http,
        settings.grafana_url,
        token=token,
        username=username,
        password=password,
        admin=True,
    )
    compliance = ComplianceClient(
        compliance_http,
        settings.compliance_service_url,
        grafana_url=settings.grafana_url,
        token=token,
        username=username,
        password=password,
    )
    rechecker = SessionRechecker(
        admin,
        TeamDirectory(admin, clock=clock),
        interval_seconds=settings.session_recheck_hours * 3600,
        clock=clock,
    )
    services = Services(
        settings=settings,
        registry=registry,
        sessions=sessions,
        compliance=compliance,
        admin=admin,
        grafana_http=grafana_http,
        standard_schema=StandardSchemaCache(compliance),
        rechecker=rechecker,
        clock=clock,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Open the registry now so a clean stop always checkpoints a leftover -wal.
        with registry_db.connection():
            pass
        log.info("UI backend listening on port %s", settings.port)
        log.info("Compliance service: %s", settings.compliance_service_url)
        removed = sessions.delete_expired()
        if removed:
            log.info("[sessions] removed %d expired session(s)", removed)
        cleanup = asyncio.create_task(_clean_up_sessions(sessions, rechecker))
        try:
            yield
        finally:
            cleanup.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup
            await grafana_http.aclose()
            await compliance_http.aclose()
            registry.close()
            sessions_db.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = services
    register_error_handlers(app)

    @app.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    # The status route first: it sits under /api/rules but needs no session.
    for module in (status, auth, standard, policies, rules, backtest):
        app.include_router(module.router)

    # The last added runs first: CORS answers preflights, then the origin check,
    # then the session loads.
    app.add_middleware(
        SessionMiddleware,
        store=sessions,
        secret=settings.session_secret,
        cookie_secure=settings.session_cookie_secure,
    )
    app.add_middleware(
        OriginCheckMiddleware,
        origins=settings.cors_origins,
        exempt_paths=frozenset({status.STATUS_PATH}),
    )
    app.add_middleware(CorsMiddleware, origins=settings.cors_origins)
    return app
