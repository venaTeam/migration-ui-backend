"""The standard schema, proxied from the compliance service."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.deps import ServicesDep, require_session
from app.errors import compliance_error

router = APIRouter(prefix="/api/standard", dependencies=[Depends(require_session)])


@router.get("/schema")
async def get_schema(services: ServicesDep) -> JSONResponse:
    try:
        response = await services.compliance.request("GET", "/standard/schema")
        return JSONResponse(response.json(), status_code=response.status_code)
    except Exception as exc:
        return compliance_error(exc)
