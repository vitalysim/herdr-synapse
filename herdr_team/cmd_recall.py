"""``recall``: search the board, facts, work items, artifacts and the canvas in one ranked list.

The index and the ranking are ``herdr_team.recall``. Anyone on the team may
search: every source is something a member can already read.

A canvas hit names the element it found, and the line says how to see it in
place, because the words alone rarely say enough about a drawing.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional

from herdr_team import recall as R
from herdr_team import workdir as _workdir
from herdr_team.cli import Command, emit
from herdr_team.cmd_board import _open_team
from herdr_team.facts import parse_when


def artifacts_dir(doc: Dict[str, Any], team_name: str) -> Optional[Path]:
    project = _workdir.project_dir_of(doc)
    if not project:
        return None
    return _workdir.paths_for(project, team_name)["artifacts"]


def _label(hit: Dict[str, Any]) -> str:
    info = hit.get("info") or {}
    if hit["kind"] == "fact":
        return "[fact {} · {} · {}]".format(hit["ref"], info.get("status"), info.get("confidence"))
    if hit["kind"] == "work":
        return "[work {} · {}]".format(hit["ref"], info.get("status"))
    if hit["kind"] == "file":
        return "[file {}]".format(hit["ref"])
    if hit["kind"] == "canvas":
        imported = info.get("imported") or {}
        origin = " · inherited from {}".format(imported.get("team")) if imported.get("team") else ""
        return "[canvas {} · {}{}]".format(hit["ref"], info.get("type") or "mark", origin)
    return "[post {} · {} · {}]".format(hit["ref"], hit.get("author"), hit.get("when") or "?")


def _follow_up(hit: Dict[str, Any]) -> str:
    """How to see the hit in place. Only the canvas needs one: a mark's words rarely say what it
    is doing on the board, and ``look --around`` shows it with its neighbours."""
    if hit["kind"] == "canvas":
        return "  → herdr-synapse canvas look --around {}".format(hit["ref"])
    return ""


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("query", help="words to look for; \"quoted words\" must appear together")
    parser.add_argument("--kind", action="append", choices=R.KINDS, help="only posts, facts, work items, artifact files or canvas marks (repeatable)")
    parser.add_argument("--about", metavar="SUBJECT", help="rank what is about this subject first")
    parser.add_argument("--as-of", dest="as_of", metavar="DATE", help="what the team believed on that date")
    parser.add_argument("--limit", type=int, default=R.DEFAULT_LIMIT)
    parser.add_argument("--no-refresh", dest="no_refresh", action="store_true", help="search the index as it is, without bringing it up to date")


def _run(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False)
    as_of = parse_when(args.as_of, "--as-of")
    result = R.search(team, artifacts_dir(doc, team_name), args.query, kinds=args.kind, about=args.about, as_of=as_of,
                      limit=args.limit, refresh_index=not args.no_refresh)
    result["team"] = team_name

    def human() -> str:
        if not result["hits"]:
            return "nothing on the board, in the facts, the work items, the artifacts or on the canvas matches {!r}".format(args.query)
        lines: List[str] = []
        for hit in result["hits"]:
            lines.append("{} {}{}".format(_label(hit), " ".join(hit["snippet"].split()), _follow_up(hit)))
        return "\n".join(lines)

    return emit(args, result, human)


COMMANDS: List[Command] = [
    Command("recall", "search the board, facts, work items, artifacts and the canvas in one ranked list", _add_arguments, _run),
]
