"""``link``, ``unlink``, ``links``: the operator connects two teams through their managers.

The registry is ``herdr_team.links``; these commands add the authority check,
the audit line, and the announcement each team's manager is nudged for
(``link_established`` / ``link_broken``, ``wake: named`` in the daemon's
delivery table). Sending across a link is ``post --to team:<name>``
(``cmd_board``), not a command here.
"""
from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List, Optional

from herdr_team import links as _links
from herdr_team import paths as _paths
from herdr_team import roster as _roster
from herdr_team.cli import Command, api_for, emit, layout_for
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError
from herdr_team.identity import audit

HOW = "herdr-synapse post --to team:{other} \"<text>\""


def _author(args: argparse.Namespace, layout: Any, api: Any, team: str) -> Any:
    from herdr_team.cmd_roster import _author as roster_author, _human_only

    author = roster_author(args, layout, api, team=team, require_server=False)
    _human_only(layout, team, author, "link")
    return author


def _announce(layout: Any, team: str, event: str, text: str, manager: Optional[str], extra: Dict[str, Any]) -> Optional[int]:
    try:
        return _roster.append_system_record(layout.team(team), event, text, to=[manager, "all"] if manager else ["all"], extra=extra, socket=os.fspath(layout.socket))
    except HerdrTeamError:
        return None


def _add_pair_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("team_a", metavar="team")
    parser.add_argument("team_b", metavar="other-team")


def _add_link_arguments(parser: argparse.ArgumentParser) -> None:
    _add_pair_arguments(parser)
    parser.add_argument("--note", metavar="TEXT", help="why these teams talk, for links and the announcement")


def _run_link(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    api = api_for(args, layout)
    a, b = _paths.validate_team_name(args.team_a), _paths.validate_team_name(args.team_b)
    author = _author(args, layout, api, a)
    for team in (a, b):
        if not layout.session.team(team).team_json.is_file():
            raise HerdrTeamError("team_not_found", "team {!r} does not exist in this session".format(team), EXIT_REFUSED, {"team": team, "teams": layout.session.list_teams()})
    managers = {team: _links.manager_of(layout.session, team) for team in (a, b)}
    for team, manager in managers.items():
        if manager is None:
            raise HerdrTeamError("link_no_manager", "{} has no manager; the link runs through the managers, so set one first: herdr-synapse --team {} manager <name>".format(team, team), EXIT_REFUSED, {"team": team})
    link, created = _links.link(layout.session, a, b, author.name, note=args.note)
    audit(layout, a, "link_set", author, {"link": link.id, "other": b, "created": created, "note": args.note})
    audit(layout, b, "link_set", author, {"link": link.id, "other": a, "created": created, "note": args.note})
    records: Dict[str, Optional[int]] = {}
    if created:
        for team, other in ((a, b), (b, a)):
            text = "{} is now linked to {}: its manager is {}. Managers post to it with: {}{}".format(
                team, other, managers[other], HOW.format(other=other), " Why: {}".format(args.note) if args.note else "")
            records[team] = _announce(layout, team, "link_established", text, managers[team],
                                      {"link": link.id, "other_team": other, "other_manager": managers[other], "manager": managers[team], "note": args.note})
    payload = {"link": link.to_json(), "created": created, "managers": managers, "state": _links.state(layout.session, link), "records": records}
    return emit(args, payload, "{} <-> {} {} (managers: {} and {})".format(a, b, "linked" if created else "already linked", managers[a], managers[b]))


def _run_unlink(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    api = api_for(args, layout)
    a, b = _paths.validate_team_name(args.team_a), _paths.validate_team_name(args.team_b)
    author = _author(args, layout, api, a)
    link = _links.unlink(layout.session, a, b, author.name)
    audit(layout, a, "link_broken", author, {"link": link.id, "other": b})
    audit(layout, b, "link_broken", author, {"link": link.id, "other": a})
    records: Dict[str, Optional[int]] = {}
    for team, other in ((a, b), (b, a)):
        records[team] = _announce(layout, team, "link_broken", "the link between {} and {} was broken by the operator; posts to team:{} are refused until it is relinked".format(team, other, other),
                                  _links.manager_of(layout.session, team), {"link": link.id, "other_team": other})
    return emit(args, {"link": link.to_json(), "records": records}, "{} <-> {} unlinked".format(a, b))


def _add_links_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--all", action="store_true", help="include broken links")


def _run_links(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    rows: List[Dict[str, Any]] = []
    for link in _links.load(layout.session):
        if not link.active and not args.all:
            continue
        rows.append(dict(link.to_json(), state=_links.state(layout.session, link), managers={t: _links.manager_of(layout.session, t) for t in link.teams}))

    def human() -> str:
        if not rows:
            return "no links; connect two teams with: herdr-synapse link <team> <other-team>"
        lines = []
        for row in rows:
            a, b = row["teams"]
            lines.append("{} <-> {}  {}  ({} / {}){}".format(a, b, row["state"], row["managers"].get(a) or "no manager", row["managers"].get(b) or "no manager", "  " + str(row["note"]) if row.get("note") else ""))
        return "\n".join(lines)

    return emit(args, {"links": rows, "session": layout.slug}, human)


COMMANDS: List[Command] = [
    Command("link", "connect two teams through their managers (operator)", _add_link_arguments, _run_link),
    Command("unlink", "break the link between two teams (operator)", _add_pair_arguments, _run_unlink),
    Command("links", "the links between teams in this session and their state", _add_links_arguments, _run_links),
]
