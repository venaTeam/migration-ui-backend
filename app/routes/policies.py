"""Notification policies, proxied from the compliance service."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.deps import ServicesDep, require_session
from app.errors import compliance_error
from app.standard import OPERATOR_LABEL_PREFIX
from app.urls import quote_component

router = APIRouter(dependencies=[Depends(require_session)])


@router.get("/api/notification-policies")
async def list_policies(request: Request, services: ServicesDep) -> JSONResponse:
    # Express took `prefix` only when it was a single string.
    values = request.query_params.getlist("prefix")
    prefix = values[0] if len(values) == 1 else OPERATOR_LABEL_PREFIX
    try:
        response = await services.compliance.request(
            "GET",
            f"/notification-policies?prefix={quote_component(prefix)}",
        )
        return JSONResponse(response.json(), status_code=response.status_code)
    except Exception as exc:
        return compliance_error(exc)
