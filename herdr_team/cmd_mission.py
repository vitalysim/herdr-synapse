"""``mission``: every team on one screen, and its popup (``prefix+d``) (0.19).

The model is ``herdr_team.mission``. The popup lists the cards lane by lane;
Enter on an agent card focuses its pane and closes the popup, Enter on any other
card shows the command that deals with it, ``r`` re-reads.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from typing import Any, Dict, List, Optional

from herdr_team import mission as M
from herdr_team.cli import Command, api_for, emit, layout_for
from herdr_team.errors import EXIT_OK, HerdrTeamError

ENTRYPOINT = "mission"


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ascii", action="store_true", help="plain markers instead of unicode")
    parser.add_argument("--width", type=int, default=0, metavar="N")


def _run(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    team = getattr(args, "team", None)
    report = M.gather(layout, team_filter=team if team and "/" not in str(team) else None)
    width = args.width or (shutil.get_terminal_size((100, 24)).columns if sys.stdout.isatty() else 100)
    return emit(args, report, lambda: "\n".join(M.format_lines(report, width=width, ascii_only=bool(args.ascii))))


def _add_pane_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--force", action="store_true", help="run from a plain shell instead of the popup")
    parser.add_argument("--ascii", action="store_true")


def _run_pane(args: argparse.Namespace) -> int:
    from herdr_team import paths as _p

    env = dict(args.env)
    layout = layout_for(args)
    if env.get("HERDR_PLUGIN_ENTRYPOINT_ID") != ENTRYPOINT and not getattr(args, "force", False):
        raise HerdrTeamError("not_a_plugin_pane", "mission-pane runs in the mission popup; use `herdr-synapse ui mission`, `herdr-synapse mission`, or pass --force", 1)
    if not _p.socket_allowed(layout.config_dir, layout.socket):
        sys.stderr.write("herdr-synapse mission-pane skipped: socket not allowed\n")
        return EXIT_OK
    if not sys.stdout.isatty():
        raise HerdrTeamError("no_tty", "mission-pane needs a terminal (use `herdr-synapse mission`)", 1)
    import curses

    return int(curses.wrapper(_loop, layout, api_for(args, layout), bool(args.ascii)))


def flatten(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Header rows and card rows in lane order: what the popup's cursor moves over."""
    rows: List[Dict[str, Any]] = []
    for lane in M.LANES:
        cards = report["lanes"].get(lane) or []
        rows.append({"header": "{} ({})".format(M.LANE_TITLES[lane], len(cards))})
        rows.extend({"card": card} for card in cards)
    return rows


def activate(card: Dict[str, Any], api: Any) -> Optional[str]:
    """Enter on a card: focus the agent's pane (returns None: close), else the command to run."""
    if card.get("kind") in ("member", "dialog") and card.get("pane_id"):
        try:
            api.request("pane.focus", {"pane_id": card["pane_id"]})
            return None
        except HerdrTeamError as err:
            return "could not focus {}: {}".format(card["pane_id"], err.code)
    if card.get("argv"):
        return "$ " + " ".join(card["argv"])
    return "nothing to run for this card"


def _loop(stdscr: Any, layout: Any, api: Any, ascii_only: bool) -> int:
    import curses

    from herdr_team.console import read_key

    curses.curs_set(0)
    stdscr.nodelay(False)
    report = M.gather(layout)
    rows = flatten(report)
    cursor = next((i for i, r in enumerate(rows) if "card" in r), 0)
    scroll = 0
    status = ""
    bullet = "-" if ascii_only else "•"
    while True:
        height, width = stdscr.getmaxyx()
        room = max(1, height - 3)
        if cursor < scroll:
            scroll = cursor
        elif cursor >= scroll + room:
            scroll = cursor - room + 1
        stdscr.erase()
        for index, row in enumerate(rows[scroll:scroll + room]):
            y = index
            selected = scroll + index == cursor
            if "header" in row:
                text, attr = row["header"], curses.A_BOLD
            else:
                card = row["card"]
                text = "  {} [{}] {}{}".format(bullet, card["team"], card["title"], "  · " + card["detail"] if card.get("detail") else "")
                attr = curses.A_REVERSE if selected else curses.A_NORMAL
            try:
                stdscr.addnstr(y, 0, text, max(1, width - 1), attr)
            except curses.error:
                pass
        footer = status or "↑↓ move · Enter focus the agent or show the command · r refresh · q close"
        try:
            stdscr.addnstr(height - 1, 0, footer[: max(1, width - 1)], max(1, width - 1), curses.A_DIM)
        except curses.error:
            pass
        stdscr.refresh()
        key = read_key(stdscr)
        status = ""
        if key in ("q", "ESC", "CTRL_C"):
            return 0
        cards = [i for i, r in enumerate(rows) if "card" in r]
        if key in ("j", "DOWN") and cards:
            later = [i for i in cards if i > cursor]
            cursor = later[0] if later else cursor
        elif key in ("k", "UP") and cards:
            earlier = [i for i in cards if i < cursor]
            cursor = earlier[-1] if earlier else cursor
        elif key == "r":
            report = M.gather(layout)
            rows = flatten(report)
            cards = [i for i, r in enumerate(rows) if "card" in r]
            cursor = cards[0] if cards else 0
        elif key == "ENTER" and 0 <= cursor < len(rows) and "card" in rows[cursor]:
            message = activate(rows[cursor]["card"], api)
            if message is None:
                return 0
            status = message


COMMANDS: List[Command] = [
    Command("mission", "every team at a glance: what needs you, what is blocked, working, done and idle", _add_arguments, _run),
    Command("mission-pane", "mission control popup (launched by the manifest pane)", _add_pane_arguments, _run_pane, hidden=True),
]
