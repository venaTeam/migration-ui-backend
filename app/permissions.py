"""Folder permission checks for the user, applied before every admin-account action."""

from __future__ import annotations

import math
from enum import IntEnum
from typing import TYPE_CHECKING

from app.grafana.client import GrafanaClient

if TYPE_CHECKING:
    from app.deps import Caller

# A built-in role grant means "this role or higher", so an Admin passes an
# Editor-only grant.
BUILT_IN_ROLE_RANK = {"viewer": 1, "editor": 2, "admin": 3}


class FolderPermission(IntEnum):
    """Grafana's folder permission levels (4 is Admin)."""

    VIEW = 1
    EDIT = 2


async def check_folder_permission(
    admin: GrafanaClient, folder_uid: str, caller: Caller, level: FolderPermission
) -> bool:
    """True when the user holds `level` or more on the folder by role, team or user grant."""
    org_role = caller.data.org_role
    # Org admins can do anything in any folder of their org, as in Grafana.
    if isinstance(org_role, str) and org_role.lower() == "admin":
        return True

    items = await admin.get_folder_permissions(folder_uid)
    team_ids = {team.get("id") for team in caller.data.teams}
    for item in items:
        permission = item.get("permission")
        # Fail closed: a grant without a numeric level grants nothing.
        if (
            isinstance(permission, bool)
            or not isinstance(permission, int | float)
            or permission < level
        ):
            continue

        # Grafana 9+ names built-in role grants "builtInRole"; older versions "role".
        role_value = item.get("builtInRole")
        if role_value is None:
            role_value = item.get("role")
        if (
            isinstance(role_value, str)
            and role_value
            and isinstance(org_role, str)
            and org_role
            and BUILT_IN_ROLE_RANK.get(org_role.lower(), -1)
            >= BUILT_IN_ROLE_RANK.get(role_value.lower(), math.inf)
        ):
            return True

        team_id = item.get("teamId")
        if team_id and team_id in team_ids:
            return True

        user_login = item.get("userLogin")
        if user_login and user_login == caller.login:
            return True

    return False
