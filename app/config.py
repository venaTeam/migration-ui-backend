"""Environment configuration."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass

from app.urls import parse_absolute_url

LOG_LEVELS = ("debug", "info", "warn", "error")
COOKIE_SECURE_MODES = ("auto", "true", "false")
MIN_SESSION_SECRET_LENGTH = 16
ADMIN_CREDENTIALS_REQUIRED = (
    "GRAFANA_ADMIN_TOKEN, or GRAFANA_ADMIN_USERNAME and GRAFANA_ADMIN_PASSWORD, must be set"
)


def parse_origins(value: str) -> tuple[str, ...]:
    """CORS_ORIGIN: one origin or a comma-separated list."""
    return tuple(origin.strip() for origin in value.split(",") if origin.strip())


@dataclass(frozen=True)
class Settings:
    port: int = 3000
    compliance_service_url: str = "http://localhost:8000"
    grafana_url: str = "http://localhost:3000"
    session_secret: str = "change-me-in-production"
    # Idle timeout, sliding with use.
    session_ttl_minutes: float = 10080
    # Absolute cap from login, however active the session is.
    session_max_ttl_days: float = 30
    # last_used_at is written at most this often.
    session_touch_interval_seconds: float = 300
    # How often a session's user is re-checked with the admin account.
    session_recheck_hours: float = 24
    session_cookie_secure: str = "auto"
    log_level: str = "info"
    cors_origins: tuple[str, ...] = ("http://localhost:5173",)
    # The registry and session SQLite files live here, on a persistent volume.
    registry_data_dir: str = "./data"
    grafana_admin_username: str | None = None
    grafana_admin_password: str | None = None
    # Service-account token (glsa_...); wins over the admin username/password.
    grafana_admin_token: str | None = None
    grafana_insecure_tls: bool = False
    # Bearer token for /api/rules/migration-status; the route is off when unset.
    migration_status_token: str | None = None

    @property
    def has_admin_credentials(self) -> bool:
        return bool(
            self.grafana_admin_token
            or (self.grafana_admin_username and self.grafana_admin_password)
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read and validate the environment; raises ValueError listing every problem."""
        env = os.environ if env is None else env
        defaults = cls()
        errors: list[str] = []

        def number(name: str, default: float) -> float:
            raw = env.get(name)
            if raw is None:
                return default
            # Number(raw) in the Express service: a blank string is 0.
            try:
                value = float(raw) if raw.strip() else 0.0
            except ValueError:
                value = math.nan
            if not math.isfinite(value):
                errors.append(f"{name} must be a number")
                return default
            return value

        def url(name: str, default: str) -> str:
            value = env.get(name, default)
            if parse_absolute_url(value) is None:
                errors.append(f"{name} must be a URL")
            return value

        def choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
            value = env.get(name, default)
            if value not in allowed:
                errors.append(f"{name} must be one of {', '.join(allowed)}")
            return value

        session_secret = env.get("SESSION_SECRET", defaults.session_secret)
        if len(session_secret) < MIN_SESSION_SECRET_LENGTH:
            errors.append(f"SESSION_SECRET must be at least {MIN_SESSION_SECRET_LENGTH} characters")

        settings = cls(
            port=int(number("UI_BACKEND_PORT", defaults.port)),
            compliance_service_url=url("COMPLIANCE_SERVICE_URL", defaults.compliance_service_url),
            grafana_url=url("GRAFANA_URL", defaults.grafana_url),
            session_secret=session_secret,
            session_ttl_minutes=number("SESSION_TTL_MINUTES", defaults.session_ttl_minutes),
            session_max_ttl_days=number("SESSION_MAX_TTL_DAYS", defaults.session_max_ttl_days),
            session_touch_interval_seconds=number(
                "SESSION_TOUCH_INTERVAL_SECONDS", defaults.session_touch_interval_seconds
            ),
            session_recheck_hours=number("SESSION_RECHECK_HOURS", defaults.session_recheck_hours),
            session_cookie_secure=choice(
                "SESSION_COOKIE_SECURE", defaults.session_cookie_secure, COOKIE_SECURE_MODES
            ),
            log_level=choice("LOG_LEVEL", defaults.log_level, LOG_LEVELS),
            cors_origins=parse_origins(env["CORS_ORIGIN"])
            if "CORS_ORIGIN" in env
            else defaults.cors_origins,
            registry_data_dir=env.get("REGISTRY_DATA_DIR", defaults.registry_data_dir),
            grafana_admin_username=env.get("GRAFANA_ADMIN_USERNAME"),
            grafana_admin_password=env.get("GRAFANA_ADMIN_PASSWORD"),
            grafana_admin_token=env.get("GRAFANA_ADMIN_TOKEN"),
            grafana_insecure_tls=env.get("GRAFANA_INSECURE_TLS") == "true",
            migration_status_token=env.get("MIGRATION_STATUS_TOKEN") or None,
        )
        if not settings.has_admin_credentials:
            errors.append(ADMIN_CREDENTIALS_REQUIRED)
        if errors:
            raise ValueError("Invalid configuration: " + "; ".join(errors))
        return settings
