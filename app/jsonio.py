"""JSON in and out, matching express.json() and JSON.stringify."""

from __future__ import annotations

import codecs
import json
from typing import Any, NoReturn

from starlette.datastructures import Headers
from starlette.requests import Request

from app.errors import BodyError

# express.json()'s default limit ("100kb").
JSON_BODY_LIMIT_BYTES = 100 * 1024
_JSON_WHITESPACE = " \t\n\r"


def dumps(value: Any) -> str:
    """JSON.stringify: compact, non-ASCII kept as is."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} is not valid JSON")


def _has_body(headers: Headers) -> bool:
    return "transfer-encoding" in headers or headers.get("content-length", "").strip().isdigit()


def _charset(params: str) -> str | None:
    for param in params.split(";"):
        key, _, value = param.partition("=")
        if key.strip().lower() == "charset":
            return value.strip().strip('"').lower()
    return None


async def read_json_body(request: Request) -> Any:
    """The body as express.json() hands it to a route: `{}` unless a JSON body was sent."""
    headers = request.headers
    media_type, _, params = headers.get("content-type", "").partition(";")
    if media_type.strip().lower() != "application/json" or not _has_body(headers):
        return {}
    charset = _charset(params) or "utf-8"
    try:
        if not charset.startswith("utf-"):
            raise LookupError(charset)
        codecs.lookup(charset)
    except LookupError:
        raise BodyError(415, "Unsupported Media Type") from None
    length = headers.get("content-length", "").strip()
    if length.isdigit() and int(length) > JSON_BODY_LIMIT_BYTES:
        raise BodyError(413, "Payload Too Large")
    raw = await request.body()
    if len(raw) > JSON_BODY_LIMIT_BYTES:
        raise BodyError(413, "Payload Too Large")
    if not raw:
        return {}
    text = raw.decode(charset, errors="replace")
    # Strict mode: only objects and arrays.
    if text.lstrip(_JSON_WHITESPACE)[:1] not in ("{", "["):
        raise BodyError(400, "Bad Request")
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except ValueError:
        raise BodyError(400, "Bad Request") from None
