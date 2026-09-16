"""Command group: ``usage`` (the report) and the ``usage-pane`` popup entrypoint (docs/cli.md section 9).

``herdr-synapse usage`` lists every agent of the session grouped by the provider
account it draws on and prints that provider's limit windows (session, week,
per model) the way ``/usage`` in Claude Code or ``/status`` in Codex do, for
all agents at once. ``ui usage`` (``prefix+i``) opens the same report as a
popup that refreshes itself every minute; ``r`` refreshes now, ``q`` closes.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from herdr_team import sanitize, store
from herdr_team import usage
from herdr_team import usage as _usage
from herdr_team.cli import Command, api_for, emit, layout_for
from herdr_team.cmd_roster import _author
from herdr_team.errors import EXIT_OK, EXIT_REFUSED, HerdrTeamError, UsageError

ENTRYPOINT = "usage"
REFRESH_S = 60.0
TICK_S = 0.5
IDLE_WATCHDOG_S = 1800.0
PANE_HINTS = "r refresh · ↑↓ scroll · q close"


def fetch_agents(api: Any) -> List[Dict[str, Any]]:
    result = api.request("agent.list", {})
    agents = result.get("agents") if isinstance(result, dict) else None
    return [a for a in (agents or []) if isinstance(a, dict)]


def build_report(args: argparse.Namespace, fetch: bool, timeout: float) -> Dict[str, Any]:
    """Agents from Herdr when reachable (an unreachable server is reported, not fatal), then the providers."""
    env = dict(args.env)
    layout = layout_for(args)
    agents: List[Dict[str, Any]] = []
    agents_error: Optional[str] = None
    try:
        api = api_for(args, layout)
        agents = fetch_agents(api)
    except HerdrTeamError as err:
        agents_error = err.message or err.code
    return usage.collect(env, agents, fetch=fetch, timeout=timeout, agents_error=agents_error)


# --------------------------------------------------------------------------
# usage


def _add_usage_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--no-fetch", dest="no_fetch", action="store_true", help="no network: local logs only (Codex); other providers show 'not fetched'")
    parser.add_argument("--timeout", type=float, default=usage.DEFAULT_TIMEOUT_S, metavar="S", help="per-provider network timeout (default {:g}s)".format(usage.DEFAULT_TIMEOUT_S))
    parser.add_argument("--ascii", action="store_true", help="ASCII bars and marks")
    parser.add_argument("--width", type=int, metavar="N", help="render width (default: the terminal's, or 100)")


def _run_usage(args: argparse.Namespace) -> int:
    report = build_report(args, fetch=not args.no_fetch, timeout=max(1.0, float(args.timeout)))
    width = args.width or (shutil.get_terminal_size((100, 24)).columns if sys.stdout.isatty() else 100)
    return emit(args, report, lambda: usage.format_text(report, width=width, ascii_only=bool(args.ascii)))


# --------------------------------------------------------------------------
# usage-pane (curses popup)


class PaneState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.report: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.refreshing = False
        self.last_refresh = 0.0
        self.scroll = 0


def _refresh_in_background(state: PaneState, args: argparse.Namespace, timeout: float) -> None:
    def work() -> None:
        try:
            report = build_report(args, fetch=True, timeout=timeout)
            with state.lock:
                state.report = report
                state.error = None
        except Exception as err:  # noqa: BLE001 - the popup must never die on a source bug
            with state.lock:
                state.error = "{}: {}".format(err.__class__.__name__, err)
        finally:
            with state.lock:
                state.refreshing = False
                state.last_refresh = time.monotonic()

    with state.lock:
        if state.refreshing:
            return
        state.refreshing = True
    threading.Thread(target=work, name="herdr-synapse-usage-refresh", daemon=True).start()


def _style_attrs(has_colors: bool) -> Dict[str, int]:
    import curses

    from herdr_team import console

    attrs = {"title": curses.A_BOLD, "normal": 0, "warn": curses.A_BOLD, "crit": curses.A_BOLD | curses.A_REVERSE, "dim": curses.A_DIM, "error": curses.A_BOLD}
    if has_colors:
        yellow = console._COLOR_PAIRS.get("yellow")
        red = console._COLOR_PAIRS.get("red")
        if yellow:
            attrs["warn"] = curses.color_pair(yellow) | curses.A_BOLD
        if red:
            attrs["crit"] = curses.color_pair(red) | curses.A_BOLD
            attrs["error"] = curses.color_pair(red)
    return attrs


def pane_lines(state: PaneState, width: int, height: int, ascii_only: bool, now: Optional[datetime] = None) -> Tuple[List[str], List[str]]:
    """``(lines, styles)`` for the popup: the report rows, a status row, scrolled to ``state.scroll``."""
    with state.lock:
        report = state.report
        error = state.error
        refreshing = state.refreshing
        scroll = state.scroll
    if report is None:
        rows: List[Tuple[str, str]] = [("Usage limits · {}".format(PANE_HINTS), "title"), ("", "normal"), ("fetching usage from each provider…" if refreshing else (error or "no report yet"), "dim" if refreshing else "error")]
    else:
        rows = usage.format_report(report, width=width, now=now, ascii_only=ascii_only, hints=PANE_HINTS)
        if error:
            rows.append(("refresh failed: {}".format(error), "error"))
        if refreshing:
            rows[0] = (rows[0][0] + " · refreshing…", rows[0][1])
    body_height = max(1, height - 1)
    max_scroll = max(0, len(rows) - body_height)
    scroll = min(scroll, max_scroll)
    with state.lock:
        state.scroll = scroll
    visible = rows[scroll : scroll + body_height]
    lines = [text for text, _style in visible]
    styles = [style for _text, style in visible]
    while len(lines) < body_height:
        lines.append("")
        styles.append("normal")
    footer = "rows {}-{} of {}".format(scroll + 1, min(len(rows), scroll + body_height), len(rows)) if max_scroll else ""
    lines.append(footer)
    styles.append("dim")
    return lines, styles


def _loop(stdscr: Any, args: argparse.Namespace, timeout: float, ascii_only: bool) -> int:
    import curses

    from herdr_team.console import draw_lines, init_colors, read_key

    curses.raw()
    curses.noecho()
    stdscr.keypad(True)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    attrs = _style_attrs(init_colors())
    state = PaneState()
    _refresh_in_background(state, args, timeout)
    last_key = time.monotonic()
    while True:
        height, width = stdscr.getmaxyx()
        lines, styles = pane_lines(state, width, height, ascii_only)
        draw_lines(stdscr, lines, None, [attrs.get(s, 0) for s in styles])
        # Re-armed every pass: ``_read_escape`` used to clear this, which left
        # the loop blocking for ever with no tick and no idle watchdog.
        stdscr.timeout(int(TICK_S * 1000))
        key = read_key(stdscr, int(TICK_S * 1000))
        now = time.monotonic()
        with state.lock:
            due = not state.refreshing and now - state.last_refresh >= REFRESH_S and state.report is not None
        if due:
            _refresh_in_background(state, args, timeout)
        if key is None:
            if now - last_key > IDLE_WATCHDOG_S:
                return EXIT_OK
            continue
        last_key = now
        if key in ("q", "Q", "ESC", "CTRL_C"):
            return EXIT_OK
        if key in ("r", "R"):
            _refresh_in_background(state, args, timeout)
        elif key in ("DOWN", "j"):
            with state.lock:
                state.scroll += 1
        elif key in ("UP", "k"):
            with state.lock:
                state.scroll = max(0, state.scroll - 1)
        elif key == "PGDN":
            with state.lock:
                state.scroll += max(1, height - 2)
        elif key == "PGUP":
            with state.lock:
                state.scroll = max(0, state.scroll - max(1, height - 2))
        elif key == "HOME":
            with state.lock:
                state.scroll = 0


def _add_pane_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--force", action="store_true", help="run from a plain shell instead of the popup")
    parser.add_argument("--timeout", type=float, default=usage.DEFAULT_TIMEOUT_S, metavar="S")
    parser.add_argument("--ascii", action="store_true")


def _run_pane(args: argparse.Namespace) -> int:
    from herdr_team import paths as _paths

    env = dict(args.env)
    layout = layout_for(args)
    if env.get("HERDR_PLUGIN_ENTRYPOINT_ID") != ENTRYPOINT and not getattr(args, "force", False):
        raise HerdrTeamError("not_a_plugin_pane", "usage-pane runs in the usage popup; use `herdr-synapse ui usage`, `herdr-synapse usage`, or pass --force", EXIT_REFUSED)
    if not _paths.socket_allowed(layout.config_dir, layout.socket):
        sys.stderr.write("herdr-synapse usage-pane skipped: socket not allowed\n")
        return EXIT_OK
    if not sys.stdout.isatty():
        raise HerdrTeamError("no_tty", "usage-pane needs a terminal (use `herdr-synapse usage`)", EXIT_REFUSED)
    import curses

    return int(curses.wrapper(_loop, args, max(1.0, float(args.timeout)), bool(args.ascii)))


# --------------------------------------------------------------------------
# context: the sibling of usage, per member rather than per provider account


def _add_context_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("member", nargs="?", help="one member (default: every member of the team)")
    parser.add_argument("--ascii", action="store_true", help="ASCII bars")
    parser.add_argument("--width", type=int, default=0, help="output width")


def _run_context(args: argparse.Namespace) -> int:
    """How full each member's context window is.

    ``usage`` answers "how much of my plan have I spent"; this answers "how much
    room does this agent have left before it forgets". The reading comes from
    the notifier, which reads each harness's own files; when the notifier is
    down the files are read directly here so the answer is never simply blank.
    """
    from herdr_team import context as _context
    from herdr_team import models as _models
    from herdr_team import roster as _roster
    from herdr_team.cmd_board import daemon_status, load_doc, members_of, resolve_team

    layout = layout_for(args)
    api = api_for(args, layout)
    author = _author(args, layout, api, require_server=False)
    team_name = resolve_team(args, layout, author)
    if team_name is None:
        raise UsageError("no team; pass --team <name>")
    team_paths = layout.team(team_name)
    doc = load_doc(team_paths)
    who_doc = store.read_json(layout.session.who_json)
    live = daemon_status(layout.session).get("alive")
    published: Dict[str, Any] = {}
    if live and isinstance(who_doc, dict):
        for entry in ((who_doc.get("teams") or {}).get(team_name) or {}).get("members") or []:
            if isinstance(entry, dict) and entry.get("name"):
                published[str(entry["name"])] = entry.get("context")

    rows: List[Dict[str, Any]] = []
    for member in members_of(doc):
        name = str(member.get("name") or "")
        if not name or member.get("kind") == "human" or member.get("status") == "left":
            continue
        if args.member and name != args.member:
            continue
        reading = published.get(name)
        if member.get("kind") == "pi":
            from . import pi_support

            data = pi_support.read_snapshot(layout.session, member)
            reading = pi_support.reading(data).to_json() if data else None
        elif not isinstance(reading, dict):
            record = _roster.read_pane_record(layout.session, member.get("terminal_id")) or {}
            configured_model = _models.effective_setting(doc.get("config"), member)[0]
            found = _context.read_member(member.get("kind"), member.get("session"), record,
                                         home=_context.home_dir(args.env), configured_model=configured_model)
            reading = found.to_json() if found is not None else None
        rows.append({"name": name, "kind": member.get("kind"), "context": reading})
    if args.member and not rows:
        raise HerdrTeamError("member_not_found", "{!r} is not an agent member of team {!r}".format(args.member, team_name), EXIT_REFUSED,
                             {"name": args.member, "team": team_name})
    payload = {"team": team_name, "source": "who.json" if published else "files", "members": rows}
    width = args.width or (shutil.get_terminal_size((100, 24)).columns if sys.stdout.isatty() else 100)
    return emit(args, payload, lambda: render_context(payload, width, bool(args.ascii)))


def render_context(payload: Dict[str, Any], width: int, ascii_only: bool) -> str:
    rows = payload.get("members") or []
    if not rows:
        return "no agent members in team {}".format(payload.get("team"))
    name_w = max(len(str(r.get("name"))) for r in rows)
    kind_w = max(len(str(r.get("kind") or "?")) for r in rows)
    cells = max(10, min(24, width - name_w - kind_w - 34))
    lines = ["team {} context".format(payload.get("team"))]
    for row in rows:
        reading = row.get("context")
        if not isinstance(reading, dict):
            lines.append("  {}  {}  unknown (no reader for this kind, or nothing written yet)".format(
                str(row.get("name")).ljust(name_w), str(row.get("kind") or "?").ljust(kind_w)))
            continue
        if not isinstance(reading.get("percent"), (int, float)):
            model_name = sanitize.headline(reading.get("model"), 80)
            model = " ({})".format(model_name) if model_name else ""
            window = "{:,}".format(reading["window"]) if isinstance(reading.get("window"), int) else "unknown"
            used = "{:,}".format(reading["used"]) if isinstance(reading.get("used"), int) else "unknown"
            lines.append("  {}  {}  {:>12} tokens  window {}{}".format(
                str(row.get("name")).ljust(name_w), str(row.get("kind") or "?").ljust(kind_w),
                used, window, model))
            continue
        percent = float(reading["percent"])
        mark = {"critical": " !!", "warning": " !"}.get(_usage.severity_for(percent) or "", "")
        if reading.get("estimated"):
            mark += " (estimated)"
        lines.append("  {}  {}  {} {:>3.0f}%  {:>12} / {:<12}{}".format(
            str(row.get("name")).ljust(name_w), str(row.get("kind") or "?").ljust(kind_w),
            _usage.bar(percent, cells, ascii_only), percent,
            "{:,}".format(int(reading.get("used") or 0)), "{:,}".format(int(reading.get("window") or 0)), mark))
    return "\n".join(lines)


COMMANDS: List[Command] = [
    Command("context", "how full each member's context window is", _add_context_arguments, _run_context),
    Command("usage", "usage limits (session, week, per model) of every provider the session's agents draw on", _add_usage_arguments, _run_usage),
    Command("usage-pane", "usage popup (launched by the manifest pane)", _add_pane_arguments, _run_pane, hidden=True),
]
