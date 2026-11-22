"""Re-checking a session's user with the admin account.

The password is checked only at login, so at least every SESSION_RECHECK_HOURS
the user is looked up again: a user Grafana no longer has is logged out, and
everyone else gets their role and teams refreshed. If Grafana can't answer,
the session is kept and the check is retried a few minutes later.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from app.aio import gather_limited
from app.grafana.client import GrafanaClient
from app.grafana.errors import GrafanaForbiddenError
from app.grafana.users import role_in_org, team_refs
from app.session.middleware import SessionContext

log = logging.getLogger(__name__)

RECHECK_RETRY_SECONDS = 300
TEAM_CACHE_SECONDS = 3600


@dataclass(frozen=True)
class VerifiedUser:
    user_id: int | None
    org_role: str | None
    teams: list[dict[str, Any]]


class TeamDirectory:
    """Every team's members, from the org team endpoints; shared by all sessions."""

    def __init__(
        self,
        admin: GrafanaClient,
        *,
        ttl_seconds: float = TEAM_CACHE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._admin = admin
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._teams: list[tuple[dict[str, Any], set[str]]] | None = None
        self._loaded_at = 0.0

    async def teams_of(self, login: str) -> list[dict[str, Any]]:
        async with self._lock:
            if self._teams is None or self._clock() - self._loaded_at >= self._ttl:
                self._teams = await self._load()
                self._loaded_at = self._clock()
        return [team for team, members in self._teams if login in members]

    async def _load(self) -> list[tuple[dict[str, Any], set[str]]]:
        teams = team_refs(await self._admin.list_teams())
        members = await gather_limited(
            partial(self._admin.get_team_members, team["id"]) for team in teams
        )
        return [
            (team, {str(member.get("login")) for member in team_members})
            for team, team_members in zip(teams, members, strict=True)
        ]


class SessionRechecker:
    def __init__(
        self,
        admin: GrafanaClient,
        teams: TeamDirectory,
        *,
        interval_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._admin = admin
        self._teams = teams
        self._interval = interval_seconds
        self._clock = clock
        self._retry_at: dict[str, float] = {}
        self._inflight: dict[str, asyncio.Task[VerifiedUser | None]] = {}

    async def ensure_current(self, session: SessionContext) -> bool:
        """Re-check the session's user when due. False when the user is gone and the
        session was ended; True otherwise, including when Grafana could not answer."""
        sid, data = session.sid, session.data
        if sid is None or data is None:
            return False
        now = self._clock()
        if now - data.last_verified_at < self._interval or self._retry_at.get(sid, 0.0) > now:
            return True

        # Parallel requests of one session share one re-check.
        task = self._inflight.get(sid)
        if task is None:
            task = asyncio.create_task(self._verify(data.login))
            self._inflight[sid] = task
            task.add_done_callback(lambda _done: self._inflight.pop(sid, None))
        try:
            user = await asyncio.shield(task)
        except Exception as exc:
            log.warning("[recheck] could not re-check %s, keeping the session: %s", data.login, exc)
            self._retry_at[sid] = self._clock() + RECHECK_RETRY_SECONDS
            return True

        if user is None:
            log.info("[recheck] %s is gone from Grafana; ending the session", data.login)
            session.destroy()
            return False
        data.user_id, data.org_role, data.teams = user.user_id, user.org_role, user.teams
        data.last_verified_at = self._clock()
        self._retry_at.pop(sid, None)
        try:
            session.save()
        except Exception:
            log.warning("[recheck] could not save the refreshed session", exc_info=True)
        return True

    def prune(self) -> None:
        """Forget retry times that have passed."""
        now = self._clock()
        for sid in [sid for sid, retry_at in self._retry_at.items() if retry_at <= now]:
            del self._retry_at[sid]

    async def _verify(self, login: str) -> VerifiedUser | None:
        """The user's current role and teams, or None when Grafana no longer has them."""
        try:
            user = await self._admin.lookup_user(login)
        except GrafanaForbiddenError:
            # The admin account is not a Grafana server admin: use the org endpoints.
            return await self._verify_in_org(login)
        if user is None or user.get("isDisabled"):
            return None
        user_id = user["id"]
        orgs, teams, admin_org = await asyncio.gather(
            self._admin.get_user_orgs(user_id),
            self._admin.get_user_teams(user_id),
            self._admin.current_org_id(),
        )
        return VerifiedUser(user_id, role_in_org(orgs, admin_org), team_refs(teams))

    async def _verify_in_org(self, login: str) -> VerifiedUser | None:
        member = await self._admin.find_org_user(login)
        # Grafana keeps disabled users in their orgs.
        if member is None or member.get("isDisabled"):
            return None
        role = member.get("role")
        return VerifiedUser(
            member.get("userId"),
            role if isinstance(role, str) else None,
            await self._teams.teams_of(login),
        )
