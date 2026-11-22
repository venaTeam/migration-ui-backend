"""Migration status for alerts-bi: which of its rules this tool has migrated.

Called service to service with a bearer token: no session and no origin check.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.deps import ServicesDep
from app.errors import error_response
from app.validation import StrictBody, parse_body

log = logging.getLogger(__name__)

STATUS_PATH = "/api/rules/migration-status"
MAX_STATUS_UIDS = 100
STATUS_MESSAGE = f"rule_uids must list 1 to {MAX_STATUS_UIDS} rule UIDs."

router = APIRouter()


class StatusBody(StrictBody):
    rule_uids: list[str]


def _bearer_token_matches(request: Request, token: str) -> bool:
    scheme, _, given = request.headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(
        given.strip().encode(), token.encode()
    )


@router.post(STATUS_PATH)
async def migration_status(request: Request, services: ServicesDep) -> JSONResponse:
    token = services.settings.migration_status_token
    if not token:
        # Off: answer as for a route that doesn't exist.
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    if not _bearer_token_matches(request, token):
        return error_response(401, "Unauthorized", "Invalid service token.")

    body = await parse_body(request, StatusBody, STATUS_MESSAGE)
    if not 1 <= len(body.rule_uids) <= MAX_STATUS_UIDS:
        return error_response(400, "Bad Request", STATUS_MESSAGE)
    uids = list(dict.fromkeys(body.rule_uids))

    latest = services.registry.latest_migrations_for_originals(uids)
    copies = {uid: row.created_uid for uid, row in latest.items() if row.created_uid}
    if not copies:
        return JSONResponse({"migrated": []})
    # One listing of all rules answers every copy-exists question at once.
    try:
        existing = {rule.get("uid") for rule in await services.admin.list_rules()}
    except Exception as exc:
        log.warning("[migration-status] Grafana listing failed: %s", exc)
        return error_response(502, "Grafana API Error", str(exc))
    return JSONResponse({"migrated": [uid for uid in uids if copies.get(uid) in existing]})
