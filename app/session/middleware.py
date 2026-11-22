"""The session cookie.

Cookie `sessionId`, value `s:<id>.<HMAC-SHA256>` signed with SESSION_SECRET (the
express-session format). HttpOnly, SameSite=Lax, Path=/; Secure per
SESSION_COOKIE_SECURE, where "auto" means the request came over https (directly
or per X-Forwarded-Proto). Sliding expiry: every response for a live session
re-sends the cookie and touches the store. No cookie is set until login.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from email.utils import formatdate
from urllib.parse import quote, unquote

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.session.store import SessionData, SessionStore

COOKIE_NAME = "sessionId"
STATE_KEY = "session"
CLEARED_COOKIE = f"{COOKIE_NAME}=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT"
_COOKIE_VALUE_SAFE = "-_.!~*'()"


class SessionContext:
    """The request's session as the routes see it."""

    def __init__(self, store: SessionStore, sid: str | None, data: SessionData | None) -> None:
        self._store = store
        self.sid = sid
        self.data = data
        self.cleared = False

    def login(self, data: SessionData) -> None:
        """Start a new session; an id from before the login is never reused."""
        new_sid = self._store.create(data)
        if self.sid is not None:
            self._store.delete(self.sid)
        self.sid, self.data, self.cleared = new_sid, data, False

    def save(self) -> None:
        if self.sid is not None and self.data is not None:
            self._store.update(self.sid, self.data)

    def destroy(self) -> None:
        if self.sid is not None:
            self._store.delete(self.sid)
        self.sid, self.data, self.cleared = None, None, True


def sign(value: str, secret: str) -> str:
    """cookie-signature's sign()."""
    mac = hmac.new(secret.encode(), value.encode(), hashlib.sha256).digest()
    return f"{value}.{base64.b64encode(mac).decode('ascii').rstrip('=')}"


def unsign(signed: str, secret: str) -> str | None:
    value = signed[: signed.rfind(".")]
    return value if hmac.compare_digest(sign(value, secret), signed) else None


def _cookie(header: str | None, name: str) -> str | None:
    """A cookie's value; the first one wins, as in the `cookie` package."""
    for pair in (header or "").split(";"):
        key, sep, value = pair.partition("=")
        if sep and key.strip() == name:
            value = value.strip()
            if len(value) > 1 and value[0] == value[-1] == '"':
                value = value[1:-1]
            return unquote(value)
    return None


def read_session_id(cookie_header: str | None, secret: str) -> str | None:
    raw = _cookie(cookie_header, COOKIE_NAME)
    if raw is None or not raw.startswith("s:"):
        return None
    return unsign(raw[2:], secret)


def session_cookie(sid: str, secret: str, expires_at: float, secure: bool) -> str:
    value = quote(f"s:{sign(sid, secret)}", safe=_COOKIE_VALUE_SAFE)
    parts = [f"{COOKIE_NAME}={value}", "Path=/", f"Expires={formatdate(expires_at, usegmt=True)}"]
    parts.append("HttpOnly")
    if secure:
        parts.append("Secure")
    parts.append("SameSite=Lax")
    return "; ".join(parts)


def is_secure(scope: Scope) -> bool:
    """https directly, or per the first X-Forwarded-Proto value."""
    if scope.get("scheme") == "https":
        return True
    forwarded = Headers(scope=scope).get("x-forwarded-proto", "")
    return forwarded.split(",")[0].strip().lower() == "https"


class SessionMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        store: SessionStore,
        secret: str,
        cookie_secure: str = "auto",
    ) -> None:
        self.app = app
        self.store = store
        self.secret = secret
        self.cookie_secure = cookie_secure

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        sid = read_session_id(Headers(scope=scope).get("cookie"), self.secret)
        data = self.store.get(sid) if sid is not None else None
        context = SessionContext(self.store, sid if data is not None else None, data)
        scope.setdefault("state", {})[STATE_KEY] = context

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                cookie = self._cookie_for(context, scope)
                if cookie is not None:
                    headers = [
                        *message.get("headers", []),
                        (b"set-cookie", cookie.encode("latin-1")),
                    ]
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cookie)

    def _cookie_for(self, context: SessionContext, scope: Scope) -> str | None:
        if context.cleared:
            return CLEARED_COOKIE
        if context.sid is None or context.data is None:
            return None
        self.store.touch(context.sid, context.data)
        secure = is_secure(scope) if self.cookie_secure == "auto" else self.cookie_secure == "true"
        return session_cookie(
            context.sid, self.secret, self.store.cookie_expires_at(context.data), secure
        )
