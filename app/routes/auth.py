"""Login and logout."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.deps import ServicesDep, SessionDep
from app.errors import error_response
from app.grafana.errors import GrafanaAPIError, GrafanaAuthError
from app.grafana.users import role_in_org, team_refs
from app.session.store import SessionData
from app.validation import NonEmptyStr, StrictBody, parse_body

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth")


class LoginBody(StrictBody):
    grafana_username: NonEmptyStr
    grafana_password: NonEmptyStr


@router.post("/login")
async def login(request: Request, services: ServicesDep, session: SessionDep) -> JSONResponse:
    body = await parse_body(request, LoginBody, "Invalid login payload.")
    grafana = services.user_grafana(body.grafana_username, body.grafana_password)

    # The password is checked by Grafana (and LDAP behind it) and then dropped.
    try:
        user = await grafana.get_current_user()
    except GrafanaAuthError:
        return error_response(401, "Unauthorized", "Invalid Grafana credentials.")
    except GrafanaAPIError as exc:
        return error_response(exc.status_code, "Grafana API Error", str(exc))
    except Exception as exc:
        return error_response(502, "Grafana API Error", f"Could not reach Grafana: {exc}")

    teams: list[dict[str, Any]] = []
    org_role = None
    verified_at = services.clock()
    try:
        user_teams, user_orgs, admin_org = await asyncio.gather(
            grafana.get_current_user_teams(),
            grafana.get_current_user_orgs(),
            services.admin.current_org_id(),
        )
        teams = team_refs(user_teams)
        # The role that counts is the one in the org the admin account works in,
        # not the user's current org.
        org_role = role_in_org(user_orgs, admin_org)
    except Exception as exc:
        # Logged in without them; the next request re-checks with the admin account.
        log.warning("[login] could not fetch user teams/org role: %s", exc)
        verified_at = 0.0

    try:
        session.login(
            SessionData(
                login=user.get("login") or body.grafana_username,
                user_id=user.get("id"),
                org_role=org_role,
                teams=teams,
                last_verified_at=verified_at,
            )
        )
    except Exception:
        log.exception("Session save failed")
        return error_response(500, "Session Error", "Could not save session.")
    return JSONResponse({"status": "ok"})


@router.post("/logout")
async def logout(session: SessionDep) -> JSONResponse:
    session.destroy()
    return JSONResponse({"status": "ok"})
