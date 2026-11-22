"""Reading Grafana's user, org and team answers."""

from __future__ import annotations

from typing import Any


def role_in_org(orgs: Any, org_id: Any) -> str | None:
    """The role in the given org; roles are per org."""
    if not isinstance(orgs, list):
        raise TypeError("Grafana returned a non-list for the user's orgs")
    for org in orgs:
        if isinstance(org, dict) and org.get("orgId") == org_id:
            role = org.get("role")
            return role if isinstance(role, str) else None
    return None


def team_refs(teams: Any) -> list[dict[str, Any]]:
    """Just the id and name of each team."""
    if not isinstance(teams, list):
        raise TypeError("Grafana returned a non-list for the user's teams")
    return [
        {"id": team.get("id"), "name": team.get("name")} for team in teams if isinstance(team, dict)
    ]
