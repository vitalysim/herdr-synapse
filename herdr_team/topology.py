"""Pure ASCII projection of teams, managers, members, and session links.

The team picker owns navigation and clipping.  This module owns only the
logical rows so the topology remains testable without curses or a live Herdr
server, and so adding the view does not put another renderer into the already
busy picker state machine.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple


def _active_members(rosters: Dict[str, List[Dict[str, Any]]], team: str) -> List[Dict[str, Any]]:
    return [
        member
        for member in rosters.get(team, [])
        if isinstance(member, dict)
        and member.get("kind") != "human"
        and member.get("status") != "left"
    ]


def _member_suffix(member: Dict[str, Any], live: Optional[Dict[str, Any]]) -> str:
    details: List[str] = []
    role = member.get("role")
    kind = member.get("kind")
    state = (live or {}).get("agent_status") or member.get("agent_status") or member.get("status")
    if role:
        details.append(str(role))
    if kind:
        details.append(str(kind))
    if state and state not in ("active",):
        details.append(str(state))
    return " [{}]".format(" | ".join(details)) if details else ""


def _link_rows(
    links: Dict[str, List[Dict[str, Any]]],
    managers: Dict[str, Optional[str]],
) -> List[Tuple[str, str, str, Optional[str], Optional[str]]]:
    """Unique ``(a, b, state, manager_a, manager_b)`` rows in stable order."""
    seen: Set[str] = set()
    rows: List[Tuple[str, str, str, Optional[str], Optional[str]]] = []
    for team in sorted(links):
        for row in links.get(team) or []:
            if not isinstance(row, dict) or not row.get("team"):
                continue
            other = str(row["team"])
            a, b = sorted((team, other))
            identity = str(row.get("id") or "{}--{}".format(a, b))
            if identity in seen:
                continue
            seen.add(identity)
            rows.append((a, b, str(row.get("state") or "active"), managers.get(a), managers.get(b)))
    return sorted(rows, key=lambda row: (row[0], row[1]))


def link_count(links: Dict[str, List[Dict[str, Any]]], managers: Dict[str, Optional[str]]) -> int:
    return len(_link_rows(links, managers))


def lines(
    rosters: Dict[str, List[Dict[str, Any]]],
    managers: Dict[str, Optional[str]],
    links: Dict[str, List[Dict[str, Any]]],
    who_members: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
) -> List[str]:
    """Logical, unwrapped ASCII rows for the Teams view topology screen."""
    live_by_team = who_members or {}
    out: List[str] = []
    teams = sorted(rosters)
    for team_index, team in enumerate(teams):
        members = _active_members(rosters, team)
        manager = managers.get(team)
        by_name = {str(member.get("name")): member for member in members if member.get("name")}
        out.append("+-- TEAM {}".format(team))
        if manager:
            member = by_name.get(str(manager), {"name": manager})
            out.append("|   * MANAGER {}{}".format(manager, _member_suffix(member, (live_by_team.get(team) or {}).get(str(manager)))))
        else:
            out.append("|   ! MANAGER not set")
        peers = [member for member in members if str(member.get("name") or "") != str(manager or "")]
        for index, member in enumerate(peers):
            branch = "`--" if index == len(peers) - 1 else "+--"
            name = str(member.get("name") or "?")
            out.append("|   {} MEMBER {}{}".format(branch, name, _member_suffix(member, (live_by_team.get(team) or {}).get(name))))
        if not peers:
            out.append("|   `-- (no other members)")
        if team_index != len(teams) - 1:
            out.append("|")

    if not teams:
        out.append("(no teams in this session)")
    out.extend(("", "LINKS"))
    link_rows = _link_rows(links, managers)
    if not link_rows:
        out.append("  (no team-to-team links)")
    for a, b, state, manager_a, manager_b in link_rows:
        left = "{}/{}".format(a, manager_a or "no-manager")
        right = "{}/{}".format(b, manager_b or "no-manager")
        out.append("  {} <====[{}]====> {}".format(left, state, right))
    return out
