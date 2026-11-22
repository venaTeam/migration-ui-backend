"""Cross-origin rules: CORS headers for the allowed origins, and an Origin check
that rejects state-changing requests from any other site."""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.jsonio import dumps

ALLOWED_METHODS = b"GET,HEAD,PUT,PATCH,POST,DELETE"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CROSS_ORIGIN_MESSAGE = "Requests from this origin are not allowed."


class CorsMiddleware:
    """CORS answered as the `cors` npm package does: the request's origin is echoed
    when it is allowed, and any OPTIONS request is a preflight answered with 204."""

    def __init__(self, app: ASGIApp, *, origins: tuple[str, ...]) -> None:
        self.app = app
        self.origins = origins

    def _headers(self, scope: Scope) -> list[tuple[bytes, bytes]]:
        if "*" in self.origins:
            headers = [(b"access-control-allow-origin", b"*")]
        else:
            headers = [(b"vary", b"Origin")]
            origin = Headers(scope=scope).get("origin")
            if origin is not None and origin in self.origins:
                headers.append((b"access-control-allow-origin", origin.encode("latin-1")))
        headers.append((b"access-control-allow-credentials", b"true"))
        return headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cors_headers = self._headers(scope)
        if scope["method"] == "OPTIONS":
            headers = [
                *cors_headers,
                (b"access-control-allow-methods", ALLOWED_METHODS),
                (b"vary", b"Access-Control-Request-Headers"),
            ]
            requested = Headers(scope=scope).get("access-control-request-headers")
            if requested:
                headers.append((b"access-control-allow-headers", requested.encode("latin-1")))
            headers.append((b"content-length", b"0"))
            await send({"type": "http.response.start", "status": 204, "headers": headers})
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*cors_headers, *message.get("headers", [])]}
            await send(message)

        await self.app(scope, receive, send_with_cors)


def _first(value: str | None) -> str:
    return (value or "").split(",")[0].strip()


def _hostname(netloc: str) -> str:
    return (urlsplit(f"//{netloc}").hostname or "").lower()


class OriginCheckMiddleware:
    """403 for a state-changing request whose Origin is neither an allowed origin
    nor this service's own host. Requests without an Origin header pass."""

    def __init__(
        self, app: ASGIApp, *, origins: tuple[str, ...], exempt_paths: frozenset[str]
    ) -> None:
        self.app = app
        self.origins = {origin.lower() for origin in origins}
        self.exempt_paths = exempt_paths

    def _allowed(self, origin: str, headers: Headers) -> bool:
        if "*" in self.origins or origin.lower() in self.origins:
            return True
        # Our own host as the browser saw it, through the frontend's nginx. Scheme and
        # port are ignored, like SameSite: nginx's $host drops the port, and its
        # X-Forwarded-Proto may be http behind an edge-terminated Route.
        own_host = _hostname(_first(headers.get("x-forwarded-host")) or headers.get("host", ""))
        return bool(own_host) and _hostname(urlsplit(origin).netloc) == own_host

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] not in SAFE_METHODS
            and scope["path"] not in self.exempt_paths
        ):
            headers = Headers(scope=scope)
            origin = headers.get("origin")
            if origin is not None and not self._allowed(origin, headers):
                body = dumps({"error": "Forbidden", "message": CROSS_ORIGIN_MESSAGE}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
