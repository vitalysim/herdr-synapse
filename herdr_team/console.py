"""The console plugin pane (curses), plan 7.3.

Data sources: ``who.json`` and the board tail only; no socket subscription.
Writes ``console.json`` ``{pane_id, terminal_id, pid, open:true, default_team,
human_label}`` on start and ``open:false`` on exit. Single writer for
``from: human`` with the console origin; verified only when ``pane get`` on
itself reports ``focused: true`` at Enter.

Everything decision-like lives in ``tui_model``. This module owns: file
reads (``who.json``, ``mute.json``, cursors, ``console.json``, the board
tail with the plan 6.3 contract), the curses loop, and executing intents by
shelling out to the ``herdr-synapse`` CLI (so author resolution, validation,
and locking happen in exactly one code path) or, for ``/peek``, calling
``agent read --source visible`` through ``api``.

The curses helpers (``read_key``, ``draw_lines``, bracketed paste) are
shared with ``picker`` and ``compose``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from herdr_team import VERSION
from herdr_team import api as _api
from herdr_team import paths as _paths
from herdr_team import store
from herdr_team import tui_model
from herdr_team.errors import EXIT_OK, EXIT_REFUSED, HerdrTeamError, emit_error
from herdr_team.paths import Layout, TeamPaths
from herdr_team.tui_model import ConsoleModel, Intent

REFRESH_S = 0.25
#: The curses input timeout, re-armed every loop pass (see ``_read_escape``).
TICK_MS = int(REFRESH_S * 1000)
#: ncurses waits ESCDELAY ms for a sequence after a bare Esc; its default 1000
#: is a visible stall in a feed that refreshes four times a second.
ESCDELAY_MS = 25
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

    def reset(self) -> None:
        """Forget everything and follow the board from its current start (after a wipe: the cache would otherwise keep showing archived posts)."""
        self._tailer.close()
        # From the fresh board's first seq: a tailer started at zero would read
        # the archive segments back in, which is exactly what a wipe removed
        # from view. ``board.seq`` records where the active file now begins.
        doc = store.read_json(self.team.board_seq, None)
        first = doc.get("active_first_seq") if isinstance(doc, dict) else None
        start = max(0, int(first) - 1) if isinstance(first, int) and not isinstance(first, bool) and first > 0 else 0
        self._tailer = store.BoardTailer(self.team, start_seq=start, persist=False)
        self.records = []
        self._by_seq = {}
        self.resets += 1

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


def human_label_for(layout: Layout, env: Dict[str, str], terminal_id: Optional[str] = None) -> str:
    """This console's label. Per console, so ``/as`` in one board does not
    re-point another board's unread cursor."""
    from herdr_team.cmd_board import console_entry

    entry = console_entry(layout.session, terminal_id) if terminal_id else None
    label = (entry or {}).get("human_label")
    if not label:
        label = read_console_json(layout).get("human_label")
    return str(label or env.get("HERDR_TEAM_HUMAN") or "human")


def _team_dir_exists(layout: Layout, team: str) -> bool:
    try:
        return layout.team(team).team_json.is_file()
    except HerdrTeamError:
        return False


def pick_team(layout: Layout, requested: Optional[str], env: Dict[str, str]) -> str:
    """``--team``, else ``HERDR_TEAM``, else this space's team, else ``default_team``, else the only team."""
    if requested:
        if not _team_dir_exists(layout, requested):
            raise HerdrTeamError("team_not_found", "no team {} in session {}".format(requested, layout.slug), EXIT_REFUSED, {"team": requested})
        return requested
    env_team = env.get("HERDR_TEAM")
    if env_team and _team_dir_exists(layout, env_team):
        return env_team
    # A console restored by Herdr after a restart comes back without
    # ``HERDR_TEAM``; the space it came back in still says which board it is.
    from herdr_team.cmd_board import workspace_team

    space = workspace_team(layout.session, _paths.env_workspace(env))
    if space and _team_dir_exists(layout, space):
        return space
    default = read_console_json(layout).get("default_team")
    if isinstance(default, str) and _team_dir_exists(layout, default):
        return default
    teams = layout.session.list_teams()
    if len(teams) == 1:
        return teams[0]
    if not teams:
        raise HerdrTeamError("team_not_found", "no team in session {}; create one first (herdr-synapse create or the team-up action)".format(layout.slug), EXIT_REFUSED)
    raise HerdrTeamError("team_ambiguous", "several teams in this session; pass --team or run herdr-synapse use <team>", EXIT_REFUSED, {"teams": teams})


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
        #: Last ``input_signature`` and when the model was last rebuilt.
        self.signature: Optional[Tuple[Any, ...]] = None
        self.last_build = 0.0
        #: This console's terminal id: the registry key for its entry.
        self.terminal_id: Optional[str] = None
        self.swap_process: Optional[Any] = None


#: Rebuild at least this often even when nothing on disk moved, so relative age
#: labels ("3m ago") and mute countdowns keep moving.
STALE_REBUILD_S = 5.0


def _stat_key(path: Any) -> Optional[Tuple[int, int, int]]:
    try:
        st = os.stat(os.fspath(path))
    except OSError:
        return None
    # The inode matters: ``store.atomic_write`` renames a fresh file into place,
    # so a same-second rewrite is invisible to mtime alone.
    return (int(st.st_mtime_ns), int(st.st_size), int(st.st_ino))


def watch_paths(layout: Layout, team: str) -> List[Any]:
    """Every file ``build_model`` reads. A reader added without a path here is
    exactly how a console goes stale, so ``test_console_live`` asserts they match."""
    team_paths = layout.team(team)
    paths: List[Any] = [
        team_paths.board_jsonl, team_paths.team_json, team_paths.mute_json, team_paths.audit_jsonl,
        layout.session.who_json, layout.session.console_json, layout.session.view_json, layout.session.daemon_json, layout.session.links_json,
    ]
    try:
        paths.extend(sorted(team_paths.cursors_dir.iterdir()))
    except OSError:
        pass
    return paths


def input_signature(layout: Layout, team: str, width: int, height: int) -> Tuple[Any, ...]:
    """A cheap fingerprint of everything the model is built from (~12 stats)."""
    return (width, height) + tuple(_stat_key(p) for p in watch_paths(layout, team))


def default_export_dir(layout: Layout, team: str) -> Path:
    """Where a console ``/export`` with no path writes: the team folder's
    ``exports/`` when the team has a project directory, else the user's home."""
    from herdr_team import workdir as _workdir

    try:
        doc = store.read_json(layout.team(team).team_json, None)
        project = _workdir.project_dir_of(doc if isinstance(doc, dict) else {})
    except (HerdrTeamError, OSError, ValueError):
        project = None
    if project:
        target = _workdir.paths_for(project, team)["exports"]
        try:
            _paths.ensure_dir(target)
            return target
        except (HerdrTeamError, OSError):
            pass
    return Path(os.path.expanduser("~"))


def build_model(layout: Layout, team: str, state: Optional[ConsoleState] = None, previous: Optional[ConsoleModel] = None, env: Optional[Dict[str, str]] = None) -> ConsoleModel:
    env = env if env is not None else {}
    if state is None:
        state = ConsoleState(layout, team, env)
    state.tail.poll()
    who = read_who(layout)
    team_paths = layout.team(team)
    roster_doc = store.read_json(team_paths.team_json, None)
    daemon_doc = store.read_json(layout.session.daemon_json, None)
    roster_members = [m for m in (roster_doc or {}).get("members", []) if isinstance(m, dict)] if isinstance(roster_doc, dict) else []
    model = tui_model.build_console_model(
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
        human_label=human_label_for(layout, state.env, getattr(state, "terminal_id", None)),
        ascii_only=state.ascii_only,
        now=datetime.now(timezone.utc),
        previous=previous,
        audit=read_audit_warnings(layout, team),
        runtime=tui_model.runtime_from_daemon(daemon_doc, VERSION),
    )
    # ``@@`` searches the members' project directories (the roster's cwd), not the console's own cwd.
    model.file_roots = tui_model.member_file_roots(roster_members)
    model.links = team_links(layout, team, who)
    return model


def team_links(layout: Layout, team: str, who: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """This team's active links: ``who.json`` when the notifier wrote them, the registry otherwise."""
    rows = (((who or {}).get("teams") or {}).get(team) or {}).get("links") if isinstance(who, dict) else None
    if isinstance(rows, list):
        return [dict(r) for r in rows if isinstance(r, dict)]
    from herdr_team import links as _links

    try:
        return _links.summary(layout.session, team)
    except (HerdrTeamError, OSError):
        return []


def refresh(model: ConsoleModel, layout: Layout, state: Optional[ConsoleState] = None) -> None:
    """Re-read ``who.json`` and tail the board with the plan 6.3 contract, updating ``model`` in place."""
    if state is None:
        state = ConsoleState(layout, model.team, {})
    state.width = model.width
    state.height = model.height
    # Formatting the feed costs ~18% CPU at 4 Hz on a 300-record board, and a
    # stat is ~10,000x cheaper, so skip the rebuild when nothing it reads moved.
    now = time.monotonic()
    signature = input_signature(layout, model.team, state.width, state.height)
    if signature == state.signature and now - state.last_build < STALE_REBUILD_S:
        if model.watching_say:
            tui_model.settle_say_watch(model, state.tail.records, now)
        return
    state.signature, state.last_build = signature, now
    fresh = build_model(layout, model.team, state, previous=model)
    model.header = fresh.header
    model.roster_lines = fresh.roster_lines
    model.feed = fresh.feed
    model.members = fresh.members
    model.links = fresh.links
    model.human_label = fresh.human_label
    model.file_roots = fresh.file_roots
    model.runtime = fresh.runtime
    if model.watching_say:
        tui_model.settle_say_watch(model, state.tail.records, time.monotonic())


def write_console_record(layout: Layout, record: Dict[str, Any]) -> None:
    _paths.ensure_session_dirs(layout.session)
    store.write_json(layout.session.console_json, record)


# --------------------------------------------------------------------------
# intent execution


def cli_path() -> Path:
    return _paths.plugin_root() / "bin" / "herdr-synapse"


def run_cli(args: Sequence[str], env: Dict[str, str], timeout: float = CLI_TIMEOUT_S) -> Tuple[int, Any, Optional[Dict[str, Any]]]:
    """Run ``herdr-synapse --json <args>``; returns ``(rc, stdout json or None, error json or None)``."""
    argv = [os.fspath(cli_path()), "--json"] + [str(a) for a in args]
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=dict(env))
    except subprocess.TimeoutExpired:
        return 5, None, {"code": "cli_timeout", "message": "herdr-synapse {} took over {:g}s".format(args[0] if args else "", timeout)}
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


#: Status hints for ``say`` refusals the human can act on (docs/cli.md section 7); other codes show the CLI message.
SAY_HINTS = {
    "author_mismatch": "only the human can type into a member",
    "member_not_found": "no such agent member (see /who)",
    "echo_rejected": "the text looks like a herdr-synapse header; reword it",
    "secret_detected": "the text looks like a secret; it is never typed",
    "daemon_down": "the notifier is not running; run: herdr-synapse daemon start",
    "say_unverified": "this console is not verified as the human (is its pane focused?); click it and retry",
    "say_control_command": "that would end, clear, or switch the member's session; !!{member} text forces it",
    "kind_unverified": "that agent kind is not trusted yet; run: herdr-synapse kinds trust <kind>",
    "say_multiline": "one line only; post multi-line text with @name instead",
    "say_too_long": "500 characters at most; post longer text with @name instead",
}


def say_args(intent: Intent, team: str) -> List[str]:
    """``herdr-synapse --json --team T say --no-wait [--force] -- <member> <text>``; the outcome comes from the board tail."""
    a = intent.args
    args: List[str] = ["--team", team, "say", "--no-wait"]
    if a.get("force"):
        args.append("--force")
    return args + ["--", str(a.get("member")), str(a.get("text", ""))]


def say_failure_status(err: Dict[str, Any], member: str) -> str:
    code = str(err.get("code") or "cli_failed")
    hint = SAY_HINTS.get(code)
    if hint:
        return "say to {} refused: {} ({})".format(member, hint.format(member=member), code)
    return "say to {} failed: {}: {}".format(member, code, err.get("message"))


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
    if a.get("interrupt"):
        args.append("--interrupt")
    for ref in a.get("refs") or []:
        args += ["--ref", str(ref)]
    for path in a.get("files") or []:
        args += ["--file", str(path)]
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
            from herdr_team.cmd_board import upsert_console

            label = intent.args.get("label")
            if state.terminal_id:
                upsert_console(state.layout, state.terminal_id, {"human_label": label})
            else:
                doc = read_console_json(state.layout)
                doc["human_label"] = label
                write_console_record(state.layout, doc)
            model.human_label = str(label)
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
            everyone = " (every member is nudged)" if out.get("to") == ["all"] else (" (interrupt: typed into the turn when allowed)" if out.get("interrupt") else "")
            model.status = "posted #{} to {}{}{}".format(out.get("seq"), ",".join(out.get("to") or []), everyone, "" if model.focused else " (unfocused: unverified)")
            attached = [re.sub(r"^\d+-", "", str(p).rsplit("/", 1)[-1]) for p in (out.get("attached") or [])]  # payloads/<seq>-<name>
            if attached:
                model.status += " · attached {}".format(", ".join(attached))
            if out.get("notifier") == "offline":
                model.status += " · notifier offline"
        else:
            model.status = "post exited {}".format(rc)
        return True
    if kind == "say":
        # Never --wait here: the curses loop is single-threaded and run_cli blocks; the typed outcome arrives
        # through the board tail (a feed tag and, via watching_say, the status line).
        member = str(intent.args.get("member"))
        rc, out, err = run_cli(say_args(intent, team), env)
        if err:
            model.status = say_failure_status(err, member)
        elif isinstance(out, dict) and isinstance(out.get("deliveries"), list):
            sent = time.monotonic()
            watched = []
            for delivery in out["deliveries"]:
                if not isinstance(delivery, dict) or not isinstance(delivery.get("seq"), int):
                    continue
                target = str(delivery.get("member") or "?")
                model.watching_say[delivery["seq"]] = (target, sent)
                watched.append(delivery["seq"])
            if watched:
                dots = "..." if model.ascii_only else "…"
                model.status = "typing into all {} agents{} {}".format(len(watched), dots, ", ".join("#{}".format(seq) for seq in watched))
            else:
                model.status = "say exited {}".format(rc)
        elif isinstance(out, dict) and isinstance(out.get("seq"), int):
            model.status = "typing into {}{} #{}".format(member, "..." if model.ascii_only else "…", out["seq"])
            model.watching_say[out["seq"]] = (member, time.monotonic())
        else:
            model.status = "say exited {}".format(rc)
        return True
    if kind == "wipe":
        argv = ["--team", team, "wipe", "--yes"]
        if intent.args.get("purge"):
            argv.append("--purge")
        if intent.args.get("reason"):
            argv += ["--reason", str(intent.args["reason"])]
        rc, out, err = run_cli(argv, env)
        if err:
            model.status = "wipe failed: {}".format(err.get("message") or err.get("code"))
            return True
        # The cache holds every record this console has ever tailed; start over
        # so the feed shows the board as it is now, not as it was.
        state.tail.reset()
        state.signature = None
        model.feed = []
        result = out if isinstance(out, dict) else {}
        if result.get("purge"):
            model.status = "board purged: {} post(s) deleted, {} archive segment(s) and {} payload(s) removed".format(result.get("records", 0), result.get("purged_segments", 0), result.get("purged_payloads", 0))
        elif result.get("records"):
            model.status = "board cleared: {} post(s) moved to the archive; herdr-synapse board --since 1 still reads them".format(result.get("records"))
        else:
            model.status = "the board was already empty"
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
    if kind == "interrupts":
        args = ["--team", team, "interrupts"]
        if intent.args.get("mode"):
            args.append(str(intent.args["mode"]))
        if intent.args.get("cooldown"):
            args += ["--cooldown", str(intent.args["cooldown"])]
        rc, out, err = run_cli(args, env)
        if err:
            model.status = "interrupts failed: {}".format(err.get("message"))
        else:
            kinds = (out or {}).get("kinds") if isinstance(out, dict) else None
            model.status = "interrupts {} · cooldown {} min".format("off" if not kinds else "into working {} members".format(", ".join(kinds)), max(1, int((out or {}).get("cooldown_ms") or 0) // 60000))
        return True
    if kind == "focus":
        rc, out, err = run_cli(["--team", team, "focus", str(intent.args.get("member"))], env)
        model.status = "focus failed: {}".format(err.get("message")) if err else "focus queued for {}".format(intent.args.get("member"))
        return True
    if kind in ("compact", "clear"):
        member = str(intent.args.get("member"))
        args = ["--team", team, kind, member]
        if kind == "clear":
            args.append("--yes")  # the console already asked, y/n, in ``_after_parse``
        rc, out, err = run_cli(args, env)
        if err:
            model.status = "{} failed: {}".format(kind, err.get("message") or err.get("code"))
        else:
            typed = (out or {}).get("keystroke") if isinstance(out, dict) else None
            model.status = "{} queued for {}; the notifier types {} when it is idle".format(kind, member, typed or kind)
        return True
    if kind == "links":
        rc, out, err = run_cli(["--team", team, "links"], env)
        rows = [r for r in ((out or {}).get("links") or []) if isinstance(r, dict) and team in (r.get("teams") or [])] if isinstance(out, dict) else []
        if err:
            model.status = "links failed: {}".format(err.get("message") or err.get("code"))
        elif not rows:
            model.status = "{} is linked to no other team; /link <other-team> connects them through their managers".format(team)
        else:
            model.status = "links: " + "; ".join("{} ({}, manager {})".format(
                [t for t in r["teams"] if t != team][0], r.get("state"), (r.get("managers") or {}).get([t for t in r["teams"] if t != team][0]) or "none") for r in rows)
        return True
    if kind in ("link", "unlink"):
        other = str(intent.args.get("other"))
        rc, out, err = run_cli(["--team", team, kind, team, other], env)
        if err:
            model.status = "{} failed: {}".format(kind, err.get("message") or err.get("code"))
        elif kind == "link":
            managers = (out or {}).get("managers") or {} if isinstance(out, dict) else {}
            model.status = "{} <-> {} linked; managers {} and {} were told (post with /team {} <text>)".format(team, other, managers.get(team), managers.get(other), other)
        else:
            model.status = "{} <-> {} unlinked; both managers were told".format(team, other)
        return True
    if kind == "swap":
        if state.swap_process is not None:
            model.status = "a replacement is already starting in this console"
            return True
        from herdr_team.swap_ui import SwapProcess
        try:
            state.swap_process = SwapProcess(dict(intent.args, team=team), env)
            model.status = "creating replacement for {}…".format(intent.args["member"])
        except OSError as err:
            model.status = "cannot start replacement: {}".format(err)
        return True
    if kind == "model_set":
        member = str(intent.args.get("member"))
        args = ["--team", team, "model", member, str(intent.args.get("setting") or "")]
        if intent.args.get("restart"):
            args += ["--apply", "restart"]
        rc, out, err = run_cli(args, env)
        if err:
            model.status = "model failed: {}".format(err.get("message") or err.get("code"))
        else:
            apply = (out or {}).get("apply") if isinstance(out, dict) else None
            setting = (out or {}).get("setting") if isinstance(out, dict) else None
            model.status = "{}: {} recorded; {}".format(member, setting, {"live": "the notifier types it when idle", "restart": "the notifier restarts it when idle", "next": "applies at its next resume"}.get(str(apply), "recorded"))
        return True
    if kind == "asks":
        rc, out, err = run_cli(["--team", team, "asks"], env)
        if err:
            model.status = "asks failed: {}".format(err.get("message") or err.get("code"))
        else:
            pending = (out or {}).get("pending") or []
            lines = ["nothing is waiting on you"] if not pending else [
                "#{} {} ({}): {}".format(p.get("seq"), p.get("from"), p.get("kind"), p.get("text")) for p in pending]
            model.peek = tui_model.box(lines, model.width, "waiting on you (Esc closes)")
            model.status = "{} waiting · answer with /reply <seq>".format(len(pending))
        return True
    if kind == "ask_policy":
        args = ["--team", team, "ask-policy"]
        if intent.args.get("block") is True:
            args.append("--block")
        elif intent.args.get("block") is False:
            args.append("--no-block")
        if intent.args.get("timeout"):
            args += ["--timeout", str(intent.args["timeout"])]
        rc, out, err = run_cli(args, env)
        if err:
            model.status = "ask-policy failed: {}".format(err.get("message") or err.get("code"))
        else:
            model.status = "an agent {} (up to {} min)".format(
                "waits for you on " + ", ".join((out or {}).get("block_kinds") or []) if (out or {}).get("block") else "never waits",
                max(1, int((out or {}).get("timeout_s") or 0) // 60))
        return True
    if kind == "context":
        member = intent.args.get("member")
        rc, out, err = run_cli(["--team", team, "context"] + ([str(member)] if member else []), env)
        if err:
            model.status = "context failed: {}".format(err.get("message") or err.get("code"))
        else:
            from herdr_team.cmd_usage import render_context

            model.peek = tui_model.box(render_context(out or {}, model.width - 4, model.ascii_only).splitlines(), model.width, "context (Esc closes)")
            model.status = "how full each member is, read from its own harness files"
        return True
    if kind == "remove":
        rc, out, err = run_cli(["--team", team, "remove", team, str(intent.args.get("member"))], env)
        model.status = "remove failed: {}".format(err.get("message")) if err else "removed {}".format(intent.args.get("member"))
        return True
    if kind == "use":
        # A console is pinned to its team for life, so this opens that team's
        # board beside this one instead of switching this pane under you.
        # Switching also rewrote the session-wide default team, which changed
        # team inference for every other console, popup and shell.
        new_team = str(intent.args.get("team"))
        if not _team_dir_exists(state.layout, new_team):
            model.status = "no team {} in this session".format(new_team)
            return True
        if new_team == state.team:
            model.status = "this board is already {}".format(new_team)
            return True
        rc, out, err = run_cli(["ui", "console", "--team", new_team], env)
        if err:
            model.status = "could not open {}: {}".format(new_team, err.get("message") or err.get("code"))
            return True
        if isinstance(out, dict) and out.get("opened") is False:
            model.status = "{} is already open in {}; focused it".format(new_team, out.get("pane_id"))
        else:
            model.status = "opened the {} board".format(new_team)
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
    if kind == "export":
        argv = ["export", "--format", str(intent.args.get("format") or "md")]
        path = intent.args.get("path")
        if path:
            argv.append(os.path.expanduser(str(path)))
        else:
            # The console pane's cwd is the plugin directory, so an unqualified
            # export would land inside the plugin itself. Put it where the team
            # keeps things, or in the operator's home when it has no folder.
            argv.append(os.fspath(default_export_dir(state.layout, model.team)))
        rc, out, err = run_cli(argv, state.env)
        if err:
            model.status = "export failed: {}".format(err.get("message") or err.get("code") or "error")
            return True
        written = (out or {}).get("path") if isinstance(out, dict) else None
        count = (out or {}).get("records") if isinstance(out, dict) else None
        model.status = "exported {} post{} to {}".format(count, "" if count == 1 else "s", written) if written else "exported"
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


def read_key(stdscr: Any, tick_ms: Optional[int] = None) -> Optional[str]:
    """One key in the ``tui_model`` vocabulary, or None on the tick timeout.

    ``tick_ms`` is the caller's input timeout, restored after an escape
    sequence is drained. ``None`` means the caller reads blocking and wants to
    stay that way (``cmd_knowledge``'s viewer).
    """
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
        return _read_escape(stdscr, tick_ms)
    control = {
        "\r": "ENTER", "\n": "ENTER", "\t": "TAB", "\x7f": "BACKSPACE", "\x08": "BACKSPACE", "\x03": "CTRL_C",
        "\x01": "CTRL_A", "\x05": "CTRL_E", "\x0b": "CTRL_K", "\x15": "CTRL_U", "\x04": "CTRL_D", "\x0f": "CTRL_O",
    }
    if ch in control:
        return control[ch]
    return ch if isinstance(ch, str) else None


def _read_escape(stdscr: Any, tick_ms: Optional[int] = None) -> Optional[str]:
    """Drain the rest of an escape sequence without losing the caller's tick.

    ``nodelay(win, False)`` sets ncurses' fully-blocking mode; it does **not**
    restore a ``wtimeout()`` set earlier. Draining with ``nodelay(True)`` and
    restoring with ``nodelay(False)`` therefore destroyed the console's 250 ms
    tick on the first Esc or paste, and the refresh loop, which only ticks
    when ``read_key`` returns None, stopped for good. Use ``timeout()`` for
    both halves so the restore is explicit.
    """
    import curses

    stdscr.timeout(0)
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
        stdscr.timeout(tick_ms if tick_ms is not None else -1)
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


#: Member colors in roster order (curses color numbers); red is kept for warnings.
MEMBER_PALETTE = ("cyan", "green", "magenta", "yellow", "blue", "white")
_COLOR_PAIRS: Dict[str, int] = {}


def init_colors() -> bool:
    """Start curses colors once; returns False when the terminal has none (styles fall back to bold/dim)."""
    import curses

    if _COLOR_PAIRS:
        return True
    try:
        if not curses.has_colors():
            return False
        curses.start_color()
        try:
            curses.use_default_colors()
            background = -1
        except curses.error:
            background = curses.COLOR_BLACK
        names = {
            "cyan": curses.COLOR_CYAN, "green": curses.COLOR_GREEN, "magenta": curses.COLOR_MAGENTA,
            "yellow": curses.COLOR_YELLOW, "blue": curses.COLOR_BLUE, "white": curses.COLOR_WHITE, "red": curses.COLOR_RED,
        }
        for i, name in enumerate(MEMBER_PALETTE + ("red",), start=1):
            curses.init_pair(i, names[name], background)
            _COLOR_PAIRS[name] = i
        return True
    except curses.error:
        _COLOR_PAIRS.clear()
        return False


def style_attr(style: str, members: List[Dict[str, Any]], has_colors: bool) -> int:
    """curses attribute for a ``tui_model`` style key; a member gets a stable palette color by roster slot."""
    import curses

    def pair(name: str) -> int:
        return curses.color_pair(_COLOR_PAIRS[name]) if has_colors and name in _COLOR_PAIRS else 0

    if style.startswith("member:"):
        slot = tui_model.member_color_slot(style[len("member:"):], members)
        if slot is None:
            return 0
        return pair(MEMBER_PALETTE[slot % len(MEMBER_PALETTE)])
    if style == tui_model.STYLE_HUMAN:
        return curses.A_BOLD
    if style == tui_model.STYLE_MANAGER:
        return pair("yellow") | curses.A_BOLD
    if style == tui_model.STYLE_LINK:
        return pair("magenta") | curses.A_BOLD
    if style == tui_model.STYLE_SYSTEM or style == tui_model.STYLE_DIM:
        return curses.A_DIM
    if style == tui_model.STYLE_WARNING:
        return pair("red") | curses.A_BOLD
    if style == tui_model.STYLE_MENU_SELECTED:
        return curses.A_REVERSE
    if style == tui_model.STYLE_MENU:
        return curses.A_DIM
    if style == tui_model.STYLE_HEADER:
        return curses.A_BOLD
    if style == tui_model.STYLE_STATUS:
        return pair("yellow")
    return 0


def draw_lines(stdscr: Any, lines: List[str], cursor: Optional[Tuple[int, int]] = None, attrs: Optional[List[int]] = None) -> None:
    import curses

    stdscr.erase()
    height, width = stdscr.getmaxyx()
    for row, line in enumerate(lines[:height]):
        attr = attrs[row] if attrs is not None and row < len(attrs) else 0
        try:
            stdscr.addnstr(row, 0, line, max(1, width - 1), attr)
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
    # ncurses' default is a full second before a bare Esc is delivered, which
    # stalls the refresh for that whole second.
    try:
        curses.set_escdelay(ESCDELAY_MS)
    except (AttributeError, curses.error):
        pass
    enable_bracketed_paste()
    has_colors = init_colors()
    state.height, state.width = stdscr.getmaxyx()
    model = build_model(state.layout, state.team, state, env=state.env)
    if state.env.get(START_VIEW_ENV) == "who":
        # ``herdr-synapse ui who`` opens this entrypoint as a popup on the roster box.
        execute_intent(tui_model.Intent("who"), model, state, api)
    last_refresh = time.monotonic()
    try:
        while True:
            if state.swap_process is not None:
                from herdr_team.swap_ui import result_text
                result = state.swap_process.result()
                model.status = state.swap_process.progress() if result is None else result_text(result)
                if result is not None:
                    state.swap_process.close()
                    state.swap_process = None
            state.height, state.width = stdscr.getmaxyx()
            model.width, model.height = state.width, state.height
            styled = tui_model.render_console_styled(model, state.width, state.height)
            lines = [line for line, _ in styled]
            attrs = [style_attr(style, model.members, has_colors) for _, style in styled]
            input_count = len(tui_model.input_lines(model, state.width))
            y, x = input_cursor_position(model, len(lines) - input_count, state.width)
            draw_lines(stdscr, lines, (y, x), attrs)
            # Re-armed every pass: a timeout lost by anything at all self-heals
            # on the next iteration instead of parking the loop in get_wch.
            stdscr.timeout(TICK_MS)
            key = read_key(stdscr, TICK_MS)
            # Wall-clock driven, not ``key is None`` driven: a missed tick can
            # now delay the feed only until the next keypress, never for ever.
            if time.monotonic() - last_refresh >= REFRESH_S:
                refresh(model, state.layout, state)
                last_refresh = time.monotonic()
            if key is None or key == "RESIZE":
                continue
            intent = tui_model.apply_key(model, key)
            if intent is not None and not execute_intent(intent, model, state, api):
                return EXIT_OK
            if intent is not None and intent.kind not in ("none", "filter", "help", "error"):
                refresh(model, state.layout, state)
                last_refresh = time.monotonic()
    finally:
        if state.swap_process is not None:
            state.swap_process.close()
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
    # One entry per console, keyed by terminal: a session may have a board open
    # per team, and the old single record meant a second console overwrote the
    # first and silently demoted it to ``cli-unverified``.
    from herdr_team.cmd_board import console_entry as _console_entry
    from herdr_team.cmd_board import update_console_doc as _update_console_doc

    existing = _console_entry(layout.session, terminal_id) or {}

    def register(doc: Dict[str, Any]) -> None:
        entry = dict(doc["consoles"].get(terminal_id) or {})
        entry.pop("closed_at", None)  # meaningless once this console is open again
        entry.update({
            "pane_id": pane_id,
            "terminal_id": terminal_id,
            "team": chosen,
            "pid": os.getpid(),
            "open": True,
            "human_label": entry.get("human_label") or env.get("HERDR_TEAM_HUMAN") or "human",
            "opened_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        })
        if terminal_id:
            doc["consoles"][terminal_id] = entry
        doc.setdefault("default_team", chosen)

    state.terminal_id = terminal_id
    if terminal_id:
        _update_console_doc(layout.session, register)
    else:
        # No terminal id (running outside a plugin pane): nothing to register,
        # and identity will treat this console as unverified, as it did before.
        record = read_console_json(layout)
        record.setdefault("default_team", chosen)
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
        if terminal_id:
            # Only this console's entry: closing one board must not mark another
            # team's console closed.
            from herdr_team.cmd_board import close_console as _close_console

            try:
                _close_console(layout.session, terminal_id)
            except (HerdrTeamError, OSError):
                pass


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
        raise HerdrTeamError("not_a_plugin_pane", "console runs in the plugin pane; use `herdr-synapse ui console` or pass --target-pane <id>", EXIT_REFUSED)
    if not _paths.socket_allowed(layout.config_dir, layout.socket):
        # Plan 4.1 / PK-07: an unlisted socket makes the pane a no-op before any
        # socket call or console.json write, so console.sh shows the hint.
        sys.stderr.write("herdr-synapse console skipped: socket not allowed\n")
        return EXIT_OK
    if target:
        env["HERDR_PANE_ID"] = target
    if not sys.stdout.isatty():
        raise HerdrTeamError("no_tty", "console needs a terminal", EXIT_REFUSED)
    api = _cli.api_for(args, layout)
    return run(layout, api, getattr(args, "team", None), env)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="herdr-synapse console", allow_abbrev=False)
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
