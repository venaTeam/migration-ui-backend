"""Backtest, proxied to the compliance service."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from app.deps import ServicesDep, require_session
from app.errors import compliance_error
from app.validation import JsInt, JsNumber, NonEmptyStr, StrictBody, parse_body

router = APIRouter(dependencies=[Depends(require_session)])


class BacktestBody(StrictBody):
    rule_query: NonEmptyStr
    threshold: JsNumber
    condition: Literal["gt", "gte", "lt", "lte", "eq", "neq"] = "gt"
    for_duration_seconds: JsInt = 0
    window_hours: Annotated[JsInt, Field(ge=1, le=168)] = 24


@router.post("/api/backtest")
async def backtest(request: Request, services: ServicesDep) -> JSONResponse:
    body = await parse_body(request, BacktestBody, "Invalid backtest payload.")
    try:
        response = await services.compliance.request(
            "POST", "/backtest", json_body=body.model_dump()
        )
        return JSONResponse(response.json(), status_code=response.status_code)
    except Exception as exc:
        return compliance_error(exc)
