"""The console plugin pane (curses), plan 7.3.

Data sources: ``who.json`` and the board tail only; no socket subscription.
Writes ``console.json`` ``{pane_id, terminal_id, pid, open:true, default_team,
human_label}`` on start and ``open:false`` on exit. Single writer for
``from: human`` with the console origin; verified only when ``pane get`` on
itself reports ``focused: true`` at Enter.

Everything decision-like lives in ``tui_model``. This module owns: file
reads (``who.json``, ``mute.json``, cursors, ``console.json``, the board
tail with the plan 6.3 contract), the curses loop, and executing intents by
shelling out to the ``herdr-team`` CLI (so author resolution, validation,
and locking happen in exactly one code path) or, for ``/peek``, calling
``agent read --source visible`` through ``api``.

The curses helpers (``read_key``, ``draw_lines``, bracketed paste) are
shared with ``picker`` and ``compose``.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from herdr_team import api as _api
from herdr_team import paths as _paths
from herdr_team import store
from herdr_team import tui_model
from herdr_team.errors import EXIT_OK, EXIT_REFUSED, HerdrTeamError, emit_error
from herdr_team.paths import Layout, TeamPaths
from herdr_team.tui_model import ConsoleModel, Intent

REFRESH_S = 0.25
CLI_TIMEOUT_S = 20.0
PEEK_TIMEOUT_S = 5.0
FOCUS_CHECK_TIMEOUT_S = 2.0
ENTRYPOINT = "console"
#: ``ui who`` sets this to ``who`` so the console starts on the roster box (plan 11: ``ui who`` in a popup).
START_VIEW_ENV = "HERDR_TEAM_CONSOLE_VIEW"

_PASTE_ON = b"\x1b[?2004h"
_PASTE_OFF = b"\x1b[?2004l"


# --------------------------------------------------------------------------
# board tail (plan 6.3, in-memory cache; no locks, reads only complete lines)


class BoardTail:
    """Follow ``board.jsonl`` with the plan 6.3 tailer contract (``store.BoardTailer``, in memory).

    The store's tailer drains the old fd across a rotation, reads the
    archive segments covering a gap, tolerates ENOENT, and reports a reset
    when a reopened or rewritten file restarts at or below the watermark.
    This wrapper keeps the console's record cache and counts reopens
    (``resets``): rotation, truncation, replacement, and detected resets.
    """

    def __init__(self, team: TeamPaths) -> None:
        self.team = team
        self._tailer = store.BoardTailer(team, persist=False)
        self.records: List[Dict[str, Any]] = []
        self._by_seq: Dict[int, Dict[str, Any]] = {}
        self.resets = 0
        self.warnings: List[str] = []

    @property
    def inode(self) -> Optional[int]:
        return self._tailer.inode

    @property
    def offset(self) -> int:
        return self._tailer.offset

    def close(self) -> None:
        self._tailer.close()

    def poll(self) -> int:
        """Read new complete lines; returns how many records were added."""
        before_inode, before_offset, before_resets = self._tailer.inode, self._tailer.offset, self._tailer.resets
        try:
            fresh = self._tailer.poll()
        except (OSError, HerdrTeamError):
            return 0
        if self._tailer.warnings:
            self.warnings.extend(self._tailer.warnings)
            self._tailer.warnings.clear()
        reopened = (
            (before_inode is not None and self._tailer.inode is not None and self._tailer.inode != before_inode)
            or self._tailer.offset < before_offset
            or self._tailer.resets != before_resets
        )
        if reopened:
            self.resets += 1
        added = 0
        for rec in fresh:
            if rec.get("synthetic") or not isinstance(rec.get("seq"), int):
                continue  # a reset note is counted above; the feed shows records only
            if rec["seq"] in self._by_seq:
                continue
            self._by_seq[rec["seq"]] = rec
            added += 1
        if added:
            self.records = [self._by_seq[s] for s in sorted(self._by_seq)]
        return added


# --------------------------------------------------------------------------
# file readers


def read_cursors(team: TeamPaths) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    try:
        names = sorted(os.listdir(team.cursors_dir))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        doc = store.read_json(team.cursors_dir / name, None)
        if isinstance(doc, dict):
            out[name[: -len(".json")]] = doc
    return out


AUDIT_WARNINGS_SHOWN = 20


def read_audit_warnings(layout: Layout, team: str) -> List[Dict[str, Any]]:
    """The newest ``audit.jsonl`` warnings (plan 7.1: refused ``--as human`` and forged pane ids show in the console)."""
    from herdr_team.identity import read_audit

    try:
        entries = read_audit(layout, team)
    except (HerdrTeamError, OSError):
        return []
    warnings = [e for e in entries if e.get("event") in tui_model.AUDIT_WARNING_EVENTS]
    return warnings[-AUDIT_WARNINGS_SHOWN:]


def read_mutes(team: TeamPaths) -> Dict[str, Any]:
    doc = store.read_json(team.mute_json, None)
    return doc if isinstance(doc, dict) else {}


def read_who(layout: Layout) -> Dict[str, Any]:
    doc = store.read_json(layout.session.who_json, None)
    return doc if isinstance(doc, dict) else {}


def read_console_json(layout: Layout) -> Dict[str, Any]:
    doc = store.read_json(layout.session.console_json, None)
    return doc if isinstance(doc, dict) else {}


def view_is_on(layout: Layout, team: str) -> bool:
    doc = store.read_json(layout.session.view_json, None)
    if not isinstance(doc, dict):
        return False
    if doc.get("on") is True:
        return True
    label = doc.get("label")
    if isinstance(label, str) and team in label:
        return True
    teams = doc.get("teams")
    return isinstance(teams, list) and team in teams


def toast_mode(layout: Layout, who: Dict[str, Any]) -> Optional[str]:
    mode = who.get("toasts")
    if isinstance(mode, str) and mode:
        return mode
    daemon = store.read_json(layout.session.daemon_json, None)
    if isinstance(daemon, dict) and isinstance(daemon.get("toasts"), str):
        return daemon["toasts"]
    return None


def roster_members(layout: Layout, team: str) -> List[Dict[str, Any]]:
    doc = store.read_json(layout.team(team).team_json, None)
    if isinstance(doc, dict) and isinstance(doc.get("members"), list):
        return [m for m in doc["members"] if isinstance(m, dict)]
    return []


def human_label_for(layout: Layout, env: Dict[str, str]) -> str:
    console_json = read_console_json(layout)
    label = console_json.get("human_label") or env.get("HERDR_TEAM_HUMAN") or "human"
    return str(label)


def _team_dir_exists(layout: Layout, team: str) -> bool:
    try:
        return layout.team(team).team_json.is_file()
    except HerdrTeamError:
        return False


def pick_team(layout: Layout, requested: Optional[str], env: Dict[str, str]) -> str:
    """``--team``, else ``HERDR_TEAM``, else ``default_team`` from ``console.json``, else the only team."""
    if requested:
        if not _team_dir_exists(layout, requested):
            raise HerdrTeamError("team_not_found", "no team {} in session {}".format(requested, layout.slug), EXIT_REFUSED, {"team": requested})
        return requested
    env_team = env.get("HERDR_TEAM")
    if env_team and _team_dir_exists(layout, env_team):
        return env_team
    default = read_console_json(layout).get("default_team")
    if isinstance(default, str) and _team_dir_exists(layout, default):
        return default
    teams = layout.session.list_teams()
    if len(teams) == 1:
        return teams[0]
    if not teams:
        raise HerdrTeamError("team_not_found", "no team in session {}; create one first (herdr-team create or the team-up action)".format(layout.slug), EXIT_REFUSED)
    raise HerdrTeamError("team_ambiguous", "several teams in this session; pass --team or run herdr-team use <team>", EXIT_REFUSED, {"teams": teams})


# --------------------------------------------------------------------------
# model assembly


class ConsoleState:
    """Runtime-side bookkeeping the model does not need to know about."""

    def __init__(self, layout: Layout, team: str, env: Dict[str, str]) -> None:
        self.layout = layout
        self.team = team
        self.env = env
        self.tail = BoardTail(layout.team(team))
        self.width = 80
        self.height = 24
        self.ascii_only = env.get("HERDR_TEAM_ASCII", "") == "1"


def build_model(layout: Layout, team: str, state: Optional[ConsoleState] = None, previous: Optional[ConsoleModel] = None, env: Optional[Dict[str, str]] = None) -> ConsoleModel:
    env = env if env is not None else {}
    if state is None:
        state = ConsoleState(layout, team, env)
    state.tail.poll()
    who = read_who(layout)
    team_paths = layout.team(team)
    return tui_model.build_console_model(
        team,
        who,
        state.tail.records,
        cursors=read_cursors(team_paths),
        mutes=read_mutes(team_paths),
        console_json=read_console_json(layout),
        width=state.width,
        height=state.height,
        view_on=view_is_on(layout, team),
        toasts=toast_mode(layout, who),
        human_label=human_label_for(layout, state.env),
        ascii_only=state.ascii_only,
        now=datetime.now(timezone.utc),
        previous=previous,
        audit=read_audit_warnings(layout, team),
    )


def refresh(model: ConsoleModel, layout: Layout, state: Optional[ConsoleState] = None) -> None:
    """Re-read ``who.json`` and tail the board with the plan 6.3 contract, updating ``model`` in place."""
    if state is None:
        state = ConsoleState(layout, model.team, {})
    state.width = model.width
    state.height = model.height
    fresh = build_model(layout, model.team, state, previous=model)
    model.header = fresh.header
    model.roster_lines = fresh.roster_lines
    model.feed = fresh.feed
    model.members = fresh.members
    model.human_label = fresh.human_label


def write_console_record(layout: Layout, record: Dict[str, Any]) -> None:
    _paths.ensure_session_dirs(layout.session)
    store.write_json(layout.session.console_json, record)


# --------------------------------------------------------------------------
# intent execution


def cli_path() -> Path:
    return _paths.plugin_root() / "bin" / "herdr-team"


def run_cli(args: Sequence[str], env: Dict[str, str], timeout: float = CLI_TIMEOUT_S) -> Tuple[int, Any, Optional[Dict[str, Any]]]:
    """Run ``herdr-team --json <args>``; returns ``(rc, stdout json or None, error json or None)``."""
    argv = [os.fspath(cli_path()), "--json"] + [str(a) for a in args]
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=dict(env))
    except subprocess.TimeoutExpired:
        return 5, None, {"code": "cli_timeout", "message": "herdr-team {} took over {:g}s".format(args[0] if args else "", timeout)}
    except OSError as err:
        return 1, None, {"code": "cli_unavailable", "message": str(err)}
    out: Any = None
    err_obj: Optional[Dict[str, Any]] = None
    try:
        out = json.loads(proc.stdout.decode("utf-8", "replace") or "null")
    except ValueError:
        out = None
    first = proc.stderr.decode("utf-8", "replace").strip().split("\n", 1)[0] if proc.stderr else ""
    if first:
        try:
            parsed = json.loads(first)
            if isinstance(parsed, dict) and "code" in parsed:
                err_obj = parsed
        except ValueError:
            err_obj = {"code": "cli_failed", "message": first} if proc.returncode else None
    return proc.returncode, out, err_obj


def post_args(intent: Intent, team: str) -> List[str]:
    a = intent.args
    args: List[str] = ["--team", team, "post", str(a.get("text", ""))]
    to = a.get("to") or []
    if to:
        args += ["--to", ",".join(to)]
    kind = a.get("kind")
    if kind and kind != "note":
        args += ["--kind", str(kind)]
    if a.get("reply_to") is not None:
        args += ["--reply-to", str(a["reply_to"])]
    if a.get("urgent"):
        args.append("--urgent")
    for ref in a.get("refs") or []:
        args += ["--ref", str(ref)]
    if a.get("spill"):
        args.append("--spill")
    if a.get("label"):
        args += ["--name", str(a["label"])]
    return args


def pane_get_cli(api: Any, pane_id: str, timeout: float) -> Optional[Dict[str, Any]]:
    """``herdr pane get <id>`` through the CLI; the ``PaneInfo`` or None.

    The CLI takes exactly one argument (``--json`` is a usage error, exit
    2) and always prints the socket envelope
    ``{"id":"cli:pane:get","result":{"type":"pane_info","pane":{...}}}``;
    ``run_json`` strips the envelope and ``api.pane_of`` picks the pane.
    """
    try:
        return _api.pane_of(api.run_json(["pane", "get", pane_id], timeout=timeout))
    except (HerdrTeamError, ValueError, OSError):
        return None


def focused_now(api: Any, pane_id: Optional[str]) -> bool:
    """``pane get`` on our own pane at Enter; True when Herdr says it is focused."""
    if not pane_id or api is None:
        return False
    pane = pane_get_cli(api, pane_id, FOCUS_CHECK_TIMEOUT_S)
    return bool(pane is not None and pane.get("focused"))


def peek_lines(api: Any, member: Dict[str, Any]) -> List[str]:
    pane_id = member.get("pane_id")
    if not pane_id:
        return ["{} has no pane right now".format(member.get("name"))]
    try:
        result = api.run(["agent", "read", str(pane_id), "--source", "visible", "--format", "text"], timeout=PEEK_TIMEOUT_S)
    except HerdrTeamError as err:
        return ["cannot read {}: {}".format(member.get("name"), err.message)]
    if not result.ok:
        return ["agent read failed: {}".format((result.stderr or result.stdout).strip()[:200])]
    return result.stdout.split("\n")


def execute_intent(intent: Intent, model: ConsoleModel, state: ConsoleState, api: Any) -> bool:
    """Perform a runtime intent; returns False when the console should exit."""
    team = model.team
    env = state.env
    kind = intent.kind
    if kind in ("none", "filter", "help", "error", "as"):
        if kind == "as":
            doc = read_console_json(state.layout)
            doc["human_label"] = intent.args.get("label")
            write_console_record(state.layout, doc)
        return True
    if kind == "quit":
        return False
    if kind == "post":
        pane_id = env.get("HERDR_PANE_ID")
        model.focused = focused_now(api, pane_id)
        intent.args["focused"] = model.focused
        rc, out, err = run_cli(post_args(intent, team), env)
        if err:
            model.status = "post failed: {}: {}".format(err.get("code"), err.get("message"))
        elif isinstance(out, dict):
            model.status = "posted #{} to {}{}".format(out.get("seq"), ",".join(out.get("to") or []), "" if model.focused else " (unfocused: unverified)")
            if out.get("notifier") == "offline":
                model.status += " · notifier offline"
        else:
            model.status = "post exited {}".format(rc)
        return True
    if kind == "retract":
        rc, out, err = run_cli(["--team", team, "retract", str(intent.args.get("seq"))], env)
        model.status = "retract failed: {}".format(err.get("message")) if err else "retracted #{} (record #{})".format(intent.args.get("seq"), (out or {}).get("seq") if isinstance(out, dict) else "?")
        return True
    if kind in ("mute", "unmute"):
        args = ["--team", team, kind]
        member = intent.args.get("member")
        args += [member] if member else ["--all"]
        if kind == "mute" and intent.args.get("for"):
            args += ["--for", str(intent.args["for"])]
        rc, out, err = run_cli(args, env)
        model.status = "{} failed: {}".format(kind, err.get("message")) if err else "{} {}".format(kind, member or "all")
        return True
    if kind == "nudge":
        args = ["--team", team, "nudge", str(intent.args.get("member"))]
        if intent.args.get("force"):
            args.append("--force")
        rc, out, err = run_cli(args, env)
        model.status = "nudge failed: {}".format(err.get("message")) if err else "nudge queued for {} (job {})".format(intent.args.get("member"), (out or {}).get("job") if isinstance(out, dict) else "?")
        return True
    if kind == "focus":
        rc, out, err = run_cli(["--team", team, "focus", str(intent.args.get("member"))], env)
        model.status = "focus failed: {}".format(err.get("message")) if err else "focus queued for {}".format(intent.args.get("member"))
        return True
    if kind == "remove":
        rc, out, err = run_cli(["--team", team, "remove", team, str(intent.args.get("member"))], env)
        model.status = "remove failed: {}".format(err.get("message")) if err else "removed {}".format(intent.args.get("member"))
        return True
    if kind == "use":
        new_team = str(intent.args.get("team"))
        if not _team_dir_exists(state.layout, new_team):
            model.status = "no team {} in this session".format(new_team)
            return True
        rc, out, err = run_cli(["use", new_team], env)
        if err:
            model.status = "use failed: {}".format(err.get("message"))
            return True
        state.team = new_team
        state.tail = BoardTail(state.layout.team(new_team))
        model.team = new_team
        model.status = "switched to team {}".format(new_team)
        return True
    if kind == "charter":
        if intent.args.get("action") == "set":
            args = ["--team", team, "charter", "set", str(intent.args.get("text"))]
            if intent.args.get("urgent"):
                args.append("--urgent")
            rc, out, err = run_cli(args, env)
            model.status = "charter failed: {}".format(err.get("message")) if err else "charter updated (#{})".format(((out or {}).get("charter") or {}).get("seq") if isinstance(out, dict) else "?")
            return True
        doc = store.read_json(state.layout.team(team).team_json, None)
        charter = (doc or {}).get("charter") if isinstance(doc, dict) else None
        if not charter:
            model.peek = tui_model.box(["(no charter; /charter set <text>)"], model.width, "charter")
        else:
            lines = str(charter.get("text") or "").split("\n")
            refs = charter.get("refs") or []
            if refs:
                lines.append("")
                lines.extend("ref: {}".format(r) for r in refs)
            model.peek = tui_model.box(lines, model.width, "charter #{}".format(charter.get("seq")))
        model.status = "Esc closes"
        return True
    if kind == "who":
        model.peek = tui_model.box(tui_model.roster_lines_for(model.members, model.width - 4, model.ascii_only), model.width, "who")
        model.status = "Esc closes"
        return True
    if kind == "peek":
        name = intent.args.get("member")
        member = next((m for m in model.members if m.get("name") == name), None)
        if member is None:
            model.status = "no member {}".format(name)
            return True
        model.peek = tui_model.box(peek_lines(api, member), model.width, "peek {} (visible screen, markers replaced; Esc closes)".format(name))
        model.status = "Esc closes"
        return True
    model.status = "unhandled intent {}".format(kind)
    return True


# --------------------------------------------------------------------------
# curses helpers shared by the three runtimes


def enable_bracketed_paste() -> None:
    try:
        os.write(1, _PASTE_ON)
    except OSError:
        pass


def disable_bracketed_paste() -> None:
    try:
        os.write(1, _PASTE_OFF)
    except OSError:
        pass


def read_key(stdscr: Any) -> Optional[str]:
    """One key in the ``tui_model`` vocabulary, or None on the tick timeout."""
    import curses

    try:
        ch = stdscr.get_wch()
    except curses.error:
        return None
    if ch == -1:
        return None
    if isinstance(ch, int):
        table = {
            curses.KEY_RESIZE: "RESIZE", curses.KEY_UP: "UP", curses.KEY_DOWN: "DOWN", curses.KEY_LEFT: "LEFT",
            curses.KEY_RIGHT: "RIGHT", curses.KEY_HOME: "HOME", curses.KEY_END: "END", curses.KEY_NPAGE: "PGDN",
            curses.KEY_PPAGE: "PGUP", curses.KEY_BACKSPACE: "BACKSPACE", curses.KEY_DC: "DELETE", curses.KEY_ENTER: "ENTER",
        }
        return table.get(ch, None)
    if ch == "\x1b":
        return _read_escape(stdscr)
    control = {
        "\r": "ENTER", "\n": "ENTER", "\t": "TAB", "\x7f": "BACKSPACE", "\x08": "BACKSPACE", "\x03": "CTRL_C",
        "\x01": "CTRL_A", "\x05": "CTRL_E", "\x0b": "CTRL_K", "\x15": "CTRL_U", "\x04": "CTRL_D", "\x0f": "CTRL_O",
    }
    if ch in control:
        return control[ch]
    return ch if isinstance(ch, str) else None


def _read_escape(stdscr: Any) -> Optional[str]:
    import curses

    stdscr.nodelay(True)
    try:
        seq = ""
        for _ in range(16):
            try:
                nxt = stdscr.get_wch()
            except curses.error:
                break
            if isinstance(nxt, int):
                # A curses keycode (arrow, resize) right behind an Escape is its own key.
                curses.ungetch(nxt)
                break
            if seq == "" and (nxt == "\x1b" or (nxt < " " and nxt not in ("\r", "\n"))):
                # M7 UI-03 (rig, 2026-09-05): a burst of Escapes, or Esc followed by a control
                # key, arrived as one read and collapsed into a single ``ESC`` that also ate the
                # control key. Only ``[``/``O`` sequences and Alt+Enter continue an escape; push
                # anything else back so the next ``read_key`` sees it.
                curses.unget_wch(nxt)
                break
            seq += nxt
            if seq in ("\r", "\n"):
                return "ALT_ENTER"
            if seq.startswith("[") and len(seq) > 1 and "@" <= seq[-1] <= "~":
                break
    finally:
        stdscr.nodelay(False)
    if seq in ("", None):
        return "ESC"
    if seq == "[200~":
        return "PASTE_START"
    if seq == "[201~":
        return "PASTE_END"
    arrows = {"[A": "UP", "[B": "DOWN", "[C": "RIGHT", "[D": "LEFT", "[H": "HOME", "[F": "END", "[5~": "PGUP", "[6~": "PGDN", "[3~": "DELETE"}
    if seq in arrows:
        return arrows[seq]
    return "ESC"


def draw_lines(stdscr: Any, lines: List[str], cursor: Optional[Tuple[int, int]] = None) -> None:
    import curses

    stdscr.erase()
    height, width = stdscr.getmaxyx()
    for row, line in enumerate(lines[:height]):
        try:
            stdscr.addnstr(row, 0, line, max(1, width - 1))
        except curses.error:
            pass
    if cursor is not None:
        y, x = cursor
        try:
            stdscr.move(min(max(0, y), height - 1), min(max(0, x), width - 1))
        except curses.error:
            pass
    stdscr.refresh()


def input_cursor_position(model: ConsoleModel, lines_before: int, width: int) -> Tuple[int, int]:
    """Screen (y, x) of the input cursor given how many lines precede the input area."""
    before = model.input[: model.cursor]
    rows = before.split("\n")
    y = lines_before + len(rows) - 1
    prefix = tui_model.INPUT_PROMPT if len(rows) == 1 else "  "
    x = tui_model.display_width(prefix) + tui_model.display_width(rows[-1])
    return y, min(x, max(0, width - 1))


# --------------------------------------------------------------------------
# main loop


def _loop(stdscr: Any, state: ConsoleState, api: Any) -> int:
    import curses

    curses.raw()
    curses.noecho()
    stdscr.keypad(True)
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    stdscr.timeout(int(REFRESH_S * 1000))
    enable_bracketed_paste()
    state.height, state.width = stdscr.getmaxyx()
    model = build_model(state.layout, state.team, state, env=state.env)
    if state.env.get(START_VIEW_ENV) == "who":
        # ``herdr-team ui who`` opens this entrypoint as a popup on the roster box.
        execute_intent(tui_model.Intent("who"), model, state, api)
    last_refresh = time.monotonic()
    try:
        while True:
            state.height, state.width = stdscr.getmaxyx()
            model.width, model.height = state.width, state.height
            lines = tui_model.render_console(model, state.width, state.height)
            input_count = len(tui_model.input_lines(model, state.width))
            y, x = input_cursor_position(model, len(lines) - input_count, state.width)
            draw_lines(stdscr, lines, (y, x))
            key = read_key(stdscr)
            if key is None or key == "RESIZE":
                if time.monotonic() - last_refresh >= REFRESH_S:
                    refresh(model, state.layout, state)
                    last_refresh = time.monotonic()
                continue
            intent = tui_model.apply_key(model, key)
            if intent is not None and not execute_intent(intent, model, state, api):
                return EXIT_OK
            if intent is not None and intent.kind not in ("none", "filter", "help", "error"):
                refresh(model, state.layout, state)
                last_refresh = time.monotonic()
    finally:
        disable_bracketed_paste()


class ConsoleTerminated(Exception):
    """Raised by the SIGTERM handler inside the curses loop so ``run`` unwinds through its ``finally``."""


def install_sigterm_handler() -> Any:
    """SIGTERM ends the console cleanly (UI-05): the ``finally`` in ``run`` writes ``open:false`` and the exit is 0.

    Without it Python dies on the signal, ``console.json`` keeps ``open:true``
    with a dead pid, and ``console.sh`` shows the failure hint and waits for
    a key instead of letting the pane close. SIGHUP is deliberately left at
    its default: a session stop hangs the PTY up and must leave ``open:true``
    so the restart reopens the console; the pane-closed-by-a-human case is
    recorded by the daemon and hook from ``pane.closed``. Returns the
    previous handler (None when handlers cannot be installed here).
    """

    def on_term(signum: int, _frame: Any) -> None:
        raise ConsoleTerminated(signum)

    try:
        return signal.signal(signal.SIGTERM, on_term)
    except (ValueError, OSError):  # not the main thread
        return None


def run(layout: Layout, api: Any, team: Optional[str], env: Dict[str, str]) -> int:
    """curses wrapper; returns the exit code for ``console.sh``."""
    import curses

    chosen = pick_team(layout, team, env)
    state = ConsoleState(layout, chosen, env)
    # Plan 4.1: the console records the state root for this config dir like the startup hook does.
    try:
        if layout.state_root.source != _paths.STATE_SOURCE_TEAM_ARG and _paths.socket_allowed(layout.config_dir, layout.socket):
            _paths.write_pointer(layout.config_dir, layout.state_root.path)
    except (HerdrTeamError, OSError):
        pass
    pane_id = env.get("HERDR_PANE_ID")
    terminal_id = None
    if pane_id and api is not None:
        pane = pane_get_cli(api, pane_id, FOCUS_CHECK_TIMEOUT_S)
        terminal_id = pane.get("terminal_id") if pane is not None and isinstance(pane.get("terminal_id"), str) else None
    record = read_console_json(layout)
    record.pop("closed_at", None)  # written by the pane.closed path; meaningless once the console is open again
    record.update(
        {
            "pane_id": pane_id,
            "terminal_id": terminal_id,
            "pid": os.getpid(),
            "open": True,
            "default_team": record.get("default_team") or chosen,
            "human_label": record.get("human_label") or env.get("HERDR_TEAM_HUMAN") or "human",
            "opened_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        }
    )
    write_console_record(layout, record)
    previous = install_sigterm_handler()
    try:
        try:
            return int(curses.wrapper(_loop, state, api))
        except ConsoleTerminated:
            return EXIT_OK  # a deliberate stop: the pane closes and the record below says closed
    finally:
        if previous is not None:
            try:
                signal.signal(signal.SIGTERM, previous)
            except (ValueError, OSError, TypeError):
                pass
        closing = read_console_json(layout)
        closing["open"] = False
        closing["pid"] = None
        write_console_record(layout, closing)


# --------------------------------------------------------------------------
# entrypoint (registered by cmd_misc as ``console``)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target-pane", metavar="ID", help="run outside a plugin pane, attributing posts to this pane")
    parser.add_argument("--force", action="store_true", help="run from a plain shell (unverified human)")


def run_args(args: argparse.Namespace) -> int:
    from herdr_team import cli as _cli

    env = dict(args.env)
    layout = _cli.layout_for(args)
    entry = env.get("HERDR_PLUGIN_ENTRYPOINT_ID")
    target = getattr(args, "target_pane", None)
    if entry != ENTRYPOINT and not target and not getattr(args, "force", False):
        raise HerdrTeamError("not_a_plugin_pane", "console runs in the plugin pane; use `herdr-team ui console` or pass --target-pane <id>", EXIT_REFUSED)
    if not _paths.socket_allowed(layout.config_dir, layout.socket):
        # Plan 4.1 / PK-07: an unlisted socket makes the pane a no-op before any
        # socket call or console.json write, so console.sh shows the hint.
        sys.stderr.write("herdr-team console skipped: socket not allowed\n")
        return EXIT_OK
    if target:
        env["HERDR_PANE_ID"] = target
    if not sys.stdout.isatty():
        raise HerdrTeamError("no_tty", "console needs a terminal", EXIT_REFUSED)
    api = _cli.api_for(args, layout)
    return run(layout, api, getattr(args, "team", None), env)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="herdr-team console", allow_abbrev=False)
    parser.add_argument("--team")
    parser.add_argument("--session")
    parser.add_argument("--socket")
    parser.add_argument("--json", action="store_true")
    add_arguments(parser)
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    args.env = dict(os.environ)
    try:
        return run_args(args)
    except HerdrTeamError as err:
        return emit_error(err)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
