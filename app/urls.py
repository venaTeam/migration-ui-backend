"""URL helpers: config validation, Grafana rule and folder URLs."""

from __future__ import annotations

import re
from urllib.parse import SplitResult, quote, urlsplit

_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*")
_HOST_REQUIRED = {"http", "https", "ws", "wss", "ftp"}
_DASHBOARDS_FOLDER = re.compile(r"/dashboards/f/([^/]+)")
_FOLDERS = re.compile(r"/folders/([^/]+)")
# Grafana's own UID rule (util.IsValidShortUID, at most 40 characters).
_UID = re.compile(r"[A-Za-z0-9_-]{1,40}")
# The compliance service's rule-URL patterns (src/grafana/client.py _extract_uid).
_RULE_PATHS = (
    re.compile(r"/alerting/grafana/(?P<uid>[^/]+)/(?:view|edit)"),
    re.compile(r"/alerting/(?P<uid>[^/]+)/(?:view|edit)"),
)
_RESERVED_SEGMENTS = {"view", "edit", "new", "grafana", "alerting"}
# Characters encodeURIComponent leaves as they are.
_URI_COMPONENT_SAFE = "-_.!~*'()"


def parse_absolute_url(value: str) -> SplitResult | None:
    """Split an absolute URL; None where JavaScript's `new URL(value)` would throw."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if not _SCHEME.fullmatch(parts.scheme):
        return None
    if parts.scheme.lower() in _HOST_REQUIRED and not parts.hostname:
        return None
    return parts


def _bare_uid(value: str) -> str | None:
    """A value with no slashes is a UID, if it is a valid one."""
    if "/" in value or "\\" in value:
        return None
    value = value.strip()
    return value if _UID.fullmatch(value) else None


def parse_folder_uid(value: str) -> str | None:
    """Folder UID from a folder URL (`/dashboards/f/<uid>`, `/folders/<uid>`) or a bare UID."""
    if "/" not in value and "\\" not in value:
        return _bare_uid(value)
    parts = parse_absolute_url(value)
    if parts is None:
        return None
    path = parts.path.removesuffix("/")
    for pattern in (_DASHBOARDS_FOLDER, _FOLDERS):
        match = pattern.search(path)
        if match:
            return match.group(1) if _UID.fullmatch(match.group(1)) else None
    return None


def parse_rule_uid(value: str) -> str | None:
    """Rule UID from an http(s) rule URL or a bare UID, read as the compliance service reads it."""
    if "/" not in value and "\\" not in value:
        return _bare_uid(value)
    parts = parse_absolute_url(value)
    if parts is None or parts.scheme.lower() not in ("http", "https"):
        return None
    path = parts.path.rstrip("/")
    uid: str | None = None
    for pattern in _RULE_PATHS:
        match = pattern.search(path)
        if match and match.group("uid") not in _RESERVED_SEGMENTS:
            uid = match.group("uid")
            break
    else:
        segments = [segment for segment in path.split("/") if segment]
        if segments and segments[-1] not in _RESERVED_SEGMENTS:
            uid = segments[-1]
    return uid if uid is not None and _UID.fullmatch(uid) else None


def rule_view_url(grafana_url: str, uid: object) -> str:
    """The rule's page in Grafana; any sub-path of GRAFANA_URL is kept."""
    return f"{grafana_url.removesuffix('/')}/alerting/grafana/{uid}/view"


def quote_component(value: str) -> str:
    """encodeURIComponent."""
    return quote(value, safe=_URI_COMPONENT_SAFE)
