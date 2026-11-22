"""Request-body validation with the semantics of the zod schemas it replaces."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator
from starlette.requests import Request

from app.errors import ApiError
from app.jsonio import read_json_body


class StrictBody(BaseModel):
    """A zod `z.object()`: exact types, unknown keys dropped, null never accepted."""

    model_config = ConfigDict(strict=True, extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _reject_nulls(cls, data: Any) -> Any:
        # zod's .optional() accepts a missing key, not null.
        if isinstance(data, dict) and any(
            name in data and data[name] is None for name in cls.model_fields
        ):
            raise ValueError("null is not allowed")
        return data


def _integral_float_to_int(value: Any) -> Any:
    # JSON 5.0 is the number 5 in JavaScript, so z.number().int() accepts it.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


NonEmptyStr = Annotated[str, Field(min_length=1)]
JsInt = Annotated[int, BeforeValidator(_integral_float_to_int)]
JsNumber = int | float
StringRecord = dict[str, str]


async def parse_body[BodyT: StrictBody](
    request: Request, model: type[BodyT], message: str
) -> BodyT:
    """Validate the JSON body, or answer 400 with the route's message."""
    raw = await read_json_body(request)
    try:
        return model.model_validate(raw)
    except ValidationError:
        raise ApiError(400, "Bad Request", message) from None
