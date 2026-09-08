"""The popup an operator answers: every ask still waiting on them.

Herdr allows exactly one popup at a time, so this is not one modal per post —
it is the queue. It opens when something addressed to the operator is
unanswered, lists everything pending, and answers or dismisses each in place.

The pure half (``AskModel``, ``ask_lines``, ``ask_key``) has no curses in it and
is what the tests drive. The loop is ``cmd_usage``'s: a tick that never blocks,
a re-armed timeout every pass, and a return that ends the process, which is how
a popup closes — Herdr reaps it when the pane dies. Submitting shells out to the
CLI exactly as ``compose`` does, so author resolution, validation and locking
stay in one code path.
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from herdr_team import asks as _asks
from herdr_team import store
from herdr_team.cli import Command, api_for, emit, layout_for
from herdr_team.errors import EXIT_OK, EXIT_REFUSED, HerdrTeamError

ENTRYPOINT = "asks"
TICK_S = 0.5
#: Long, because this popup is meant to sit there until the operator gets to
#: it, and closing it early is what loses the ask.
IDLE_WATCHDOG_S = 1800.0
REFRESH_S = 2.0
MAX_REPLY_CHARS = 500


@dataclass
class AskModel:
    team: str
    asks: List[Dict[str, Any]] = field(default_factory=list)
    index: int = 0
    input: str = ""
    status: Optional[str] = None
    width: int = 80
    height: int = 20

    @property
    def current(self) -> Optional[Dict[str, Any]]:
        if not self.asks:
            return None
        self.index = max(0, min(self.index, len(self.asks) - 1))
        return self.asks[self.index]


@dataclass
class Intent:
    kind: str  # reply | dismiss | quit | none | error
    args: Dict[str, Any] = field(default_factory=dict)


def age_label(record: Dict[str, Any], now: Optional[float] = None) -> str:
    """How long this ask has been waiting, for the header."""
    from herdr_team.roster import parse_iso

    at = parse_iso(record.get("ts"))
    if at is None:
        return "?"
    seconds = max(0.0, (time.time() if now is None else now) - at)
    if seconds < 90:
        return "{:.0f}s".format(seconds)
    if seconds < 5400:
        return "{:.0f}m".format(seconds / 60)
    return "{:.0f}h".format(seconds / 3600)


def ask_lines(model: AskModel, now: Optional[float] = None) -> List[str]:
    """The whole popup, as plain lines. Pure: the tests drive this."""
    width = max(30, model.width)
    record = model.current
    if record is None:
        return ["{} · nothing is waiting on you".format(model.team), "", "q closes"]
    head = "{} · {} of {} waiting on you".format(model.team, model.index + 1, len(model.asks))
    lines = [head[:width], ""]
    lines.append("#{} {} · {} · waiting {}".format(
        record.get("seq"), record.get("from"), record.get("kind"), age_label(record, now))[:width])
    lines.append("")
    body = " ".join(str(record.get("text") or "").split())
    room = max(1, (model.height - 9)) * (width - 2)
    body = body[:room] + ("…" if len(body) > room else "")
    while body:
        lines.append(body[: width - 2])
        body = body[width - 2:]
    lines.append("")
    lines.append("> " + model.input)
    lines.append("")
    lines.append("Enter replies · Tab next · Esc leaves it waiting · q closes")
    if model.status:
        lines.append(model.status[:width])
    return lines


def ask_key(model: AskModel, key: str) -> Intent:
    """One keystroke against the model. Pure."""
    record = model.current
    model.status = None
    if key in ("CTRL_C", "q") and not model.input:
        return Intent("quit")
    if key == "ESC":
        if model.input:
            model.input = ""
            return Intent("none")
        if record is None:
            return Intent("quit")
        # Dismissing is "not now", never "answered": the ask stays on the
        # board, and the daemon simply stops reopening the popup for it.
        return Intent("dismiss", {"seq": record.get("seq")})
    if key in ("TAB", "DOWN") and model.asks:
        model.index = (model.index + 1) % len(model.asks)
        model.input = ""
        return Intent("none")
    if key in ("BTAB", "UP") and model.asks:
        model.index = (model.index - 1) % len(model.asks)
        model.input = ""
        return Intent("none")
    if key == "ENTER":
        if record is None:
            return Intent("quit")
        text = model.input.strip()
        if not text:
            model.status = "type an answer, or Esc to leave it waiting"
            return Intent("error")
        return Intent("reply", {"seq": record.get("seq"), "to": record.get("from"), "text": text})
    if key == "BACKSPACE":
        model.input = model.input[:-1]
        return Intent("none")
    if len(key) == 1 and key.isprintable():
        if len(model.input) < MAX_REPLY_CHARS:
            model.input += key
        return Intent("none")
    return Intent("none")


def reply_args(team: str, intent: Intent) -> List[str]:
    """The CLI argv that answers one ask. ``--no-wait`` so the popup never blocks."""
    args = intent.args
    return ["--json", "--team", team, "post", "--to", str(args["to"]), "--kind", "answer",
            "--reply-to", str(args["seq"]), "--no-wait", str(args["text"])]


# --------------------------------------------------------------------------
# the popup


def _refresh(model: AskModel, team_paths: Any) -> None:
    seq = (model.current or {}).get("seq")
    model.asks = _asks.pending(team_paths, dismissed=_asks.dismissed(team_paths))
    for i, record in enumerate(model.asks):
        if record.get("seq") == seq:
            model.index = i
            break
    else:
        model.index = min(model.index, max(0, len(model.asks) - 1))


def _loop(stdscr: Any, args: argparse.Namespace, team: str, team_paths: Any) -> int:
    import curses

    from herdr_team.console import draw_lines, read_key, run_cli

    curses.raw()
    curses.noecho()
    stdscr.keypad(True)
    model = AskModel(team=team)
    _refresh(model, team_paths)
    last_refresh = time.monotonic()
    last_key = time.monotonic()
    while True:
        height, width = stdscr.getmaxyx()
        model.height, model.width = height, width
        draw_lines(stdscr, ask_lines(model)[: max(1, height)], None, None)
        stdscr.timeout(int(TICK_S * 1000))
        key = read_key(stdscr, int(TICK_S * 1000))
        now = time.monotonic()
        if key is None:
            if now - last_refresh >= REFRESH_S:
                _refresh(model, team_paths)
                last_refresh = now
                if not model.asks:
                    return EXIT_OK  # answered elsewhere; nothing left to hold the slot for
            if now - last_key >= IDLE_WATCHDOG_S:
                return EXIT_OK
            continue
        last_key = now
        intent = ask_key(model, key)
        if intent.kind == "quit":
            return EXIT_OK
        if intent.kind == "dismiss":
            _asks.dismiss(team_paths, [intent.args["seq"]])
            _refresh(model, team_paths)
            model.input = ""
            if not model.asks:
                return EXIT_OK
            continue
        if intent.kind == "reply":
            code, out, err = run_cli(reply_args(team, intent), dict(args.env))
            if err:
                model.status = "not sent: {}".format(err.get("message") or err.get("code"))
                continue
            model.input = ""
            _refresh(model, team_paths)
            if not model.asks:
                return EXIT_OK


def _add_pane_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--force", action="store_true", help="run outside the popup")


def _run_pane(args: argparse.Namespace) -> int:
    from herdr_team import paths as _paths
    from herdr_team.cmd_board import resolve_team

    env = dict(args.env)
    layout = layout_for(args)
    if env.get("HERDR_PLUGIN_ENTRYPOINT_ID") != ENTRYPOINT and not getattr(args, "force", False):
        raise HerdrTeamError("not_a_plugin_pane", "asks-pane runs in the asks popup; use `herdr-synapse asks` or pass --force", EXIT_REFUSED)
    if not _paths.socket_allowed(layout.config_dir, layout.socket):
        sys.stderr.write("herdr-synapse asks-pane skipped: socket not allowed\n")
        return EXIT_OK
    team_name = resolve_team(args, layout, None) or ""
    if not team_name:
        raise HerdrTeamError("team_required", "no team; pass --team <name>", EXIT_REFUSED)
    if not sys.stdout.isatty():
        raise HerdrTeamError("no_tty", "asks-pane needs a terminal", EXIT_REFUSED)
    import curses

    return int(curses.wrapper(_loop, args, team_name, layout.team(team_name)))


def _add_asks_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dismiss", metavar="SEQ", type=int, help="stop the popup reopening for this ask")


def _run_asks(args: argparse.Namespace) -> int:
    """List what is waiting on you, or open the popup that answers it."""
    from herdr_team.cmd_board import resolve_team

    layout = layout_for(args)
    api = api_for(args, layout)
    team_name = resolve_team(args, layout, None)
    if not team_name:
        raise HerdrTeamError("team_required", "no team; pass --team <name>", EXIT_REFUSED)
    team_paths = layout.team(team_name)
    if args.dismiss is not None:
        _asks.dismiss(team_paths, [int(args.dismiss)])
        return emit(args, {"team": team_name, "dismissed": int(args.dismiss)}, "#{} will not reopen the popup".format(args.dismiss))
    pending = _asks.pending(team_paths, dismissed=_asks.dismissed(team_paths))
    payload = {"team": team_name, "pending": [
        {"seq": r.get("seq"), "from": r.get("from"), "kind": r.get("kind"), "ts": r.get("ts"),
         "text": " ".join(str(r.get("text") or "").split())[:200]} for r in pending]}

    def human() -> str:
        if not pending:
            return "nothing is waiting on you in {}".format(team_name)
        lines = ["{} waiting on you in {}:".format(len(pending), team_name)]
        for r in pending:
            lines.append("  #{} {} ({}, waiting {}): {}".format(
                r.get("seq"), r.get("from"), r.get("kind"), age_label(r),
                " ".join(str(r.get("text") or "").split())[:80]))
        lines.append("answer one: herdr-synapse post --to <name> --reply-to <seq> \"<text>\"")
        return "\n".join(lines)

    return emit(args, payload, human)


COMMANDS = [
    Command("asks", "what is waiting on you, and answer it", _add_asks_arguments, _run_asks),
    Command("asks-pane", "the waiting-on-you popup (launched by the manifest pane)", _add_pane_arguments, _run_pane, hidden=True),
]
