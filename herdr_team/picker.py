"""The team-up popup (curses), plan 5.5 and 11.

``agent list`` rows sorted by workspace then pane, greyed when
``launch_pending``; Space toggles, ``w`` scopes to the current workspace,
``a`` selects all; then team name, charter (multi-line, ``Ctrl-O`` loads a
file), per member role, name, and brief; confirm screen; the popup exits
and the action performs create, rename, label, tokens, briefing jobs, view.
When teams exist, a target stage follows the selection: a numbered list of
"add to team X" rows and "create a new team". Adding skips the charter stage
and joins the selected agents through one ``herdr-synapse add`` each.
Esc and Ctrl-C exit; 10 min idle watchdog; refreshes on ``who.json``.

The state machine lives in ``tui_model`` (``PickerModel``,
``picker_apply_key``); this module reads ``agent.list`` once through
``api`` (plus on ``r``), marks agents already in a roster from the session's
``team.json`` files, runs the curses loop, and finally executes the
``create`` spec through the ``herdr-synapse`` CLI so the join routine runs in
exactly one code path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Set

from herdr_team import paths as _paths
from herdr_team import store
from herdr_team import tui_model
from herdr_team.errors import EXIT_OK, EXIT_REFUSED, HerdrTeamError, emit_error
from herdr_team.paths import Layout
from herdr_team.tui_model import Intent, PickerModel

IDLE_WATCHDOG_S = 600.0
TICK_S = 0.5
ENTRYPOINT = "picker"


def load_context(env: Dict[str, str]) -> Dict[str, Any]:
    raw = env.get("HERDR_PLUGIN_CONTEXT_JSON") or ""
    try:
        doc = json.loads(raw) if raw else {}
    except ValueError:
        doc = {}
    doc = doc if isinstance(doc, dict) else {}
    if not doc.get("workspace_id") and env.get("HERDR_WORKSPACE_ID"):
        doc["workspace_id"] = env["HERDR_WORKSPACE_ID"]
    if not doc.get("focused_pane_id") and env.get("HERDR_PANE_ID"):
        doc["focused_pane_id"] = env["HERDR_PANE_ID"]
    return doc


def session_teams(layout: Optional[Layout]) -> Dict[str, Dict[str, Any]]:
    """``{team: team.json}`` for every team in the session (read-only, no locks)."""
    out: Dict[str, Dict[str, Any]] = {}
    if layout is None:
        return out
    for name in layout.session.list_teams():
        doc = store.read_json(layout.team(name).team_json, None)
        if isinstance(doc, dict) and isinstance(doc.get("members"), list):
            out[name] = doc
    return out


def session_rosters(layout: Optional[Layout]) -> Dict[str, List[Dict[str, Any]]]:
    """``{team: members}`` for every team in the session (read-only, no locks)."""
    return {name: [m for m in doc["members"] if isinstance(m, dict)] for name, doc in session_teams(layout).items()}


def fetch_agents(api: Any) -> List[Dict[str, Any]]:
    result = api.request("agent.list", {})
    agents = result.get("agents") if isinstance(result, dict) else None
    return [a for a in (agents or []) if isinstance(a, dict)]


def _folder_status(layout: Optional[Layout], teams: List[str]) -> Dict[str, Dict[str, Any]]:
    """``workdir.status`` per team, for the tree. A broken team is skipped, never fatal."""
    from herdr_team import workdir as _workdir

    out: Dict[str, Dict[str, Any]] = {}
    if layout is None:
        return out
    for name in teams:
        try:
            out[name] = _workdir.status(layout, name)
        except Exception:  # noqa: BLE001 - the picker must open even when one team is unreadable
            continue
    return out


def build_model(api: Any, context: Dict[str, Any], layout: Optional[Layout] = None) -> PickerModel:
    """Rows from ``agent.list``, claimed rows from the session rosters, scope from the context."""
    agents = fetch_agents(api)
    focused = context.get("workspace_id") or (context.get("focused_pane_id") or "").split(":")[0] or None
    rows = tui_model.picker_rows_from_agent_list(agents, focused)
    teams = session_teams(layout)
    rosters = {name: [m for m in doc["members"] if isinstance(m, dict)] for name, doc in teams.items()}
    tui_model.mark_claimed(rows, rosters)
    model = PickerModel(rows=rows, focused_workspace=focused)
    model.rosters = rosters
    model.charters = {name: doc["charter"] for name, doc in teams.items() if isinstance(doc.get("charter"), dict)}
    model.folders = _folder_status(layout, sorted(teams))
    model.live_names = {str(a.get("name")) for a in agents if a.get("name")}
    model.existing_teams = sorted(rosters)
    model.existing_team_sizes = {team: len([m for m in members if m.get("kind") != "human" and m.get("status") != "left"]) for team, members in rosters.items()}
    model.trusted_kinds = trusted_kinds(layout)
    model.scope_workspace = focused if focused and any(r.workspace_id == focused for r in rows) else None
    return model


def trusted_kinds(layout: Optional[Layout]) -> Optional[Set[str]]:
    """Kinds ``kinds.json`` lets the daemon type into; None without a session (the confirm screen then says nothing)."""
    if layout is None:
        return None
    from herdr_team import roster as _roster

    doc = store.read_json(layout.session.kinds_json, default=None)
    if not isinstance(doc, dict):
        return set()
    return {str(kind) for kind in doc if _roster.kind_trusted(doc, str(kind))}


def refresh_rows(model: PickerModel, api: Any, layout: Optional[Layout]) -> None:
    """Re-read ``agent.list`` (on ``r`` and after every action), keeping selections and the cursor."""
    # Herdr recycles pane numbers, so a typed role or name must follow the terminal, not the pane.
    by_terminal = {r.terminal_id: r for r in model.rows if r.terminal_id}
    by_pane = {r.pane_id: r for r in model.rows if r.pane_id}
    node = tui_model.node_at(model)
    keep_key = node.key if node is not None else ""
    fresh = build_model(api, {"workspace_id": model.focused_workspace}, layout)
    for row in fresh.rows:
        old = by_terminal.get(row.terminal_id) if row.terminal_id else None
        if old is None:
            old = by_pane.get(row.pane_id)
        if old is not None:
            row.selected = old.selected and tui_model.selectable(row)
            row.role, row.member_name, row.brief = old.role, old.member_name, old.brief
    model.rows = fresh.rows
    model.live_names = fresh.live_names
    model.existing_teams = fresh.existing_teams
    model.existing_team_sizes = fresh.existing_team_sizes
    model.rosters = fresh.rosters
    model.charters = fresh.charters
    model.folders = fresh.folders
    model.collapsed &= set(model.rosters)
    if not tui_model.focus_node(model, keep_key):
        model.cursor = min(model.cursor, max(0, len(tui_model.picker_tree(model)) - 1))


def refresh_status_from_who(model: PickerModel, layout: Optional[Layout]) -> None:
    """Cheap tick refresh: agent status of listed rows from ``who.json`` (no socket)."""
    if layout is None:
        return
    who = store.read_json(layout.session.who_json, None)
    if not isinstance(who, dict):
        return
    by_pane: Dict[str, str] = {}
    who_members: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for team_name, team_doc in (who.get("teams") or {}).items():
        for m in (team_doc or {}).get("members") or []:
            if not isinstance(m, dict):
                continue
            if m.get("pane_id") and m.get("agent_status"):
                by_pane[str(m["pane_id"])] = str(m["agent_status"])
            if m.get("name"):
                who_members.setdefault(str(team_name), {})[str(m["name"])] = m
    model.who_members = who_members
    for row in model.rows:
        if row.pane_id in by_pane:
            row.agent_status = by_pane[row.pane_id]


def load_charter_file(model: PickerModel, path: str) -> None:
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as handle:
            text = handle.read(tui_model.MAX_CHARTER_CHARS + 1)
    except OSError as err:
        model.error = "cannot read {}: {}".format(path, err)
        return
    if len(text) > tui_model.MAX_CHARTER_CHARS:
        model.error = "file is over {} chars; use create --charter-file".format(tui_model.MAX_CHARTER_CHARS)
        return
    model.charter_lines = text.rstrip("\n").split("\n") if text.strip() else []
    model.error = None
    model.status = "loaded {} ({} lines)".format(path, len(model.charter_lines))


def create_args(spec: Dict[str, Any]) -> List[str]:
    """``herdr-synapse create`` argv for a picker spec."""
    args: List[str] = ["create", str(spec["team"])]
    if spec.get("charter"):
        args += ["--charter", str(spec["charter"])]
    if spec.get("project"):
        args += ["--project", str(spec["project"])]
    for member in spec.get("members") or []:
        target = "{}:{}:{}".format(member["target"], member["role"], member["name"])
        args += ["--member", target]
        if member.get("brief"):
            args += ["--brief", "{}={}".format(member["name"], member["brief"])]
        if member.get("setting"):
            args += ["--model", "{}={}".format(member["name"], member["setting"])]
    return args


def add_args(spec: Dict[str, Any], member: Dict[str, Any]) -> List[str]:
    """``herdr-synapse add`` argv for one member of a picker spec in ``add`` mode (an existing team)."""
    args: List[str] = ["add", str(spec["team"]), str(member["target"]), "--role", str(member["role"]), "--as", str(member["name"])]
    if member.get("brief"):
        args += ["--brief", str(member["brief"])]
    if member.get("setting"):
        args += ["--model", str(member["setting"])]
    return args


def _loop(stdscr: Any, model: PickerModel, api: Any, layout: Optional[Layout], env: Optional[Dict[str, str]] = None, actions: bool = True) -> Optional[Dict[str, Any]]:
    import curses

    from herdr_team.console import draw_lines, enable_bracketed_paste, disable_bracketed_paste, read_key

    curses.raw()
    curses.noecho()
    stdscr.keypad(True)
    enable_bracketed_paste()
    last_key = time.monotonic()
    pending_path: Optional[str] = None
    try:
        while True:
            height, width = stdscr.getmaxyx()
            lines = tui_model.picker_lines(model, width, height)
            cursor = tui_model.picker_cursor(model, lines, width)
            if pending_path is not None:
                lines = lines[: max(0, height - 1)] + ["file path: " + pending_path]
                cursor = (len(lines) - 1, min(width - 1, tui_model.display_width(lines[-1])))
            try:
                curses.curs_set(1 if cursor is not None else 0)  # no stray cursor on the list stages
            except curses.error:
                pass
            draw_lines(stdscr, lines, cursor)
            # Re-armed every pass: ``_read_escape`` used to clear this, which left
            # the loop blocking for ever with no tick and no idle watchdog.
            stdscr.timeout(int(TICK_S * 1000))
            key = read_key(stdscr, int(TICK_S * 1000))
            if key is None:
                if time.monotonic() - last_key > IDLE_WATCHDOG_S:
                    return None
                refresh_status_from_who(model, layout)
                continue
            last_key = time.monotonic()
            if pending_path is not None:
                if key == "ENTER":
                    load_charter_file(model, pending_path)
                    pending_path = None
                elif key in ("ESC", "CTRL_C"):
                    pending_path = None
                elif key == "BACKSPACE":
                    pending_path = pending_path[:-1]
                elif len(key) == 1 and key.isprintable():
                    pending_path += key
                continue
            intent = tui_model.picker_apply_key(model, key)
            if intent is None:
                continue
            if intent.kind == "quit":
                return None
            if intent.kind == "refresh":
                try:
                    refresh_rows(model, api, layout)
                    model.status = "refreshed"
                except HerdrTeamError as err:
                    model.error = "refresh failed: {}".format(err.message)
                continue
            if intent.kind == "load_file":
                pending_path = ""
                continue
            if intent.kind in ACTION_INTENTS:
                if not actions:
                    model.error = "--dry-run: member actions are disabled"
                    continue
                # ``run_cli`` blocks for up to ACTION_TIMEOUT_S with no redraw, so say what is happening
                # before it starts, and drop whatever was typed into the frozen popup afterwards.
                model.status = ACTION_LABELS.get(intent.kind, "working").format(**{k: intent.args.get(k) for k in ("member", "team")}) + "…"
                model.error = None
                height, width = stdscr.getmaxyx()
                draw_lines(stdscr, tui_model.picker_lines(model, width, height), None)
                keep_open = execute_action(intent, model, api, layout, dict(env or {}))
                try:
                    curses.flushinp()
                except curses.error:
                    pass
                last_key = time.monotonic()
                if not keep_open:
                    return None
                continue
            if intent.kind == "create":
                return intent.args
    finally:
        disable_bracketed_paste()


def run(layout: Layout, api: Any, env: Dict[str, str], actions: bool = True) -> Optional[Dict[str, Any]]:
    """Returns the create request the action executes, or None when cancelled or when an action ran."""
    import curses

    model = build_model(api, load_context(env), layout)
    model.ascii_only = bool(env.get("HERDR_TEAM_ASCII"))
    return curses.wrapper(_loop, model, api, layout, env, actions)


#: Member actions the tree can run while the popup stays open.
ACTION_INTENTS = ("member_rename", "member_goal", "member_send_goal", "member_remove", "member_focus", "member_resume", "member_manager", "member_model", "team_folder_set", "team_board_open", "team_dissolve")
#: ``remove`` and ``rename`` do several socket round trips plus a lock wait; the console's 20 s is too
#: tight for them, and a timeout kills the CLI mid-change (M8 review).
ACTION_TIMEOUT_S = 45.0

ACTION_LABELS = {
    "member_rename": "renaming {member}",
    "member_goal": "saving the goal for {member}",
    "member_send_goal": "sending the goal to {member}",
    "member_remove": "removing {member} from {team}",
    "member_focus": "going to {member}",
    "member_resume": "looking up the session of {member}",
    "member_manager": "making {member} the team manager",
    "member_model": "setting the model for {member}",
    "team_folder_set": "setting the folder for {team}",
    "team_dissolve": "dissolving {team}",
    "team_board_open": "opening the {team} board",
}


def action_args(intent: Any) -> List[str]:
    """The CLI argv for one member action (``remove`` takes the team twice: global flag and positional)."""
    args = intent.args
    team = str(args["team"])
    member = str(args.get("member") or "")
    if intent.kind == "member_remove":
        argv = ["--team", team, "remove", team, member]
        if args.get("keep_name"):
            argv.append("--keep-name")
        return argv
    if intent.kind == "member_rename":
        return ["--team", team, "rename", member, str(args["new"])]
    if intent.kind == "team_board_open":
        return ["ui", "console", "--team", team]
    if intent.kind == "team_folder_set":
        return ["--team", team, "project", "set", str(args.get("path") or "")]
    if intent.kind == "team_dissolve":
        # The tree asked already, so ``--yes`` here is the answer, not a bypass.
        return ["--team", team, "dissolve", team, "--yes"]
    if intent.kind == "member_goal":
        return ["--team", team, "brief", member, "--set", str(args.get("text") or "")]
    if intent.kind == "member_send_goal":
        return ["--team", team, "brief", member]
    if intent.kind == "member_resume":
        return ["--team", team, "resume", member, "--print"]
    if intent.kind == "member_manager":
        return ["--team", team, "manager", "--clear"] if args.get("clear") else ["--team", team, "manager", member]
    if intent.kind == "member_model":
        return ["--team", team, "model", member, str(args.get("setting") or "")]
    return ["--team", team, "focus", member]


def action_failure_status(intent: Any, err: Dict[str, Any]) -> str:
    """One line naming what failed and what to do about it."""
    code = str(err.get("code") or "error")
    message = str(err.get("message") or code)
    member = str(intent.args.get("member") or "")
    if intent.kind == "team_folder_set":
        if code in ("path_invalid", "workdir_foreign_file"):
            return message
        if code == "author_mismatch":
            return "setting a team folder is human only"
    if code == "daemon_down":
        tail = "the goal is saved; only sending it needs the notifier" if intent.kind == "member_send_goal" else "start it with: herdr-synapse daemon start"
        return "the team notifier is not running ({})".format(tail)
    if code == "agent_name_taken":
        candidates = err.get("candidates")
        hint = "; try {}".format(", ".join(str(c) for c in candidates[:3])) if isinstance(candidates, list) and candidates else ""
        return "Herdr already has an agent called {}{}".format(intent.args.get("new"), hint)
    if code in ("name_taken", "name_invalid", "name_reserved", "text_too_long"):
        return message
    if code == "member_not_found":
        return "{} is not in {} any more; press r to refresh".format(member, intent.args.get("team"))
    if code in ("session_unknown", "session_unsupported"):
        return "{}: {}".format(member, message)
    if code in ("team_not_found", "team_session_mismatch"):
        return "team {} is not in this session; press r".format(intent.args.get("team"))
    if code in ("lock_timeout", "board_locked"):
        return "the roster is busy; try again"
    if code == "author_mismatch":
        return "changing a goal is human only"
    if code == "cli_timeout":
        return "{} took too long; press r to see what happened".format(intent.kind.replace("member_", ""))
    return "{}: {}".format(code, message)


def action_success_status(intent: Any, out: Any) -> str:
    args = intent.args
    member = str(args.get("member") or "")
    if intent.kind == "team_board_open":
        if isinstance(out, dict) and out.get("opened") is False:
            return "the {} board is already open in {}; focused it".format(args.get("team"), out.get("pane_id"))
        return "opened the {} board".format(args.get("team"))
    if intent.kind == "team_dissolve":
        archived = (out or {}).get("archived_to") if isinstance(out, dict) else None
        return "{} dissolved; its board was archived to {}".format(args.get("team"), archived or "the session archive")
    if intent.kind == "team_folder_set":
        written = len((out or {}).get("written") or []) if isinstance(out, dict) else 0
        return "{} now has a folder at {} ({} file{} written)".format(
            args.get("team"), args.get("path"), written, "" if written == 1 else "s")
    if intent.kind == "member_rename":
        return "{} is now {} (the old name still resolves for 10 min)".format(member, args.get("new"))
    if intent.kind == "member_goal":
        saved = (out or {}).get("brief") if isinstance(out, dict) else None
        if not saved:
            return "goal cleared for {}; it keeps the old one until you send a new briefing".format(member)
        return "goal saved for {}; it does not reach the agent until you send it (action 3)".format(member)
    if intent.kind == "member_send_goal":
        return "briefing queued for {}; it lands once the agent is idle".format(member)
    if intent.kind == "member_resume":
        command = (out or {}).get("command") if isinstance(out, dict) else None
        return "in a shell pane run: herdr-synapse resume {}  ({})".format(member, command or "no command")
    if intent.kind == "member_model":
        setting = (out or {}).get("setting") if isinstance(out, dict) else args.get("setting")
        apply = (out or {}).get("apply") if isinstance(out, dict) else None
        return "{} runs {} {}".format(member, setting, {"live": "once the notifier types it (it is idle first)", "restart": "after the notifier restarts it", "next": "from its next resume"}.get(str(apply), "(recorded)"))
    if intent.kind == "member_manager":
        if args.get("clear"):
            return "{} is no longer the team manager; the team was told".format(member)
        previous = (out or {}).get("previous") if isinstance(out, dict) else None
        return "{} is the team manager{}; every member was told".format(member, " (was {})".format(previous) if previous else "")
    if intent.kind == "member_remove":
        kept = " (its Herdr agent name was kept)" if args.get("keep_name") else ""
        return "{} removed from {}{}".format(member, args.get("team"), kept)
    return "focusing {}".format(member)


def member_still_matches(layout: Optional[Layout], intent: Any) -> bool:
    """Guard against a roster that changed while the tree sat on screen or a call blocked."""
    if layout is None:
        return True
    if not intent.args.get("member"):
        return True  # a team-level action (the folder) is not about one member
    doc = store.read_json(layout.team(str(intent.args["team"])).team_json, None)
    if not isinstance(doc, dict):
        return False
    expected = intent.args.get("terminal_id")
    for member in doc.get("members") or []:
        if not isinstance(member, dict) or member.get("name") != intent.args.get("member"):
            continue
        if member.get("status") == "left":
            return False
        return expected is None or member.get("terminal_id") == expected
    return False


def execute_action(intent: Any, model: PickerModel, api: Any, layout: Optional[Layout], env: Dict[str, str]) -> bool:
    """Run one member action through the CLI. Returns False when the popup should close."""
    from herdr_team.console import run_cli

    if not member_still_matches(layout, intent):
        model.error = "{} changed while this was open; press r to refresh".format(intent.args.get("member"))
        model.stage = "select"
        return True
    rc, out, err = run_cli(action_args(intent), env, timeout=ACTION_TIMEOUT_S)
    if err:
        model.error = action_failure_status(intent, err)
        if intent.kind not in ("member_rename", "member_goal", "team_folder_set"):
            model.stage = "select"  # a text stage keeps what was typed so it can be corrected
        return True
    model.error = None
    model.status = action_success_status(intent, out)
    if intent.kind == "member_focus":
        return False
    try:
        refresh_rows(model, api, layout)
    except HerdrTeamError as err_obj:
        model.status = "{} (refresh failed: {})".format(model.status, err_obj.message)
    model.stage = "select"
    if intent.kind == "member_rename":
        tui_model.focus_node(model, "member:{}/{}".format(intent.args["team"], intent.args["new"]))
    elif intent.kind in ("member_remove", "team_folder_set"):
        tui_model.focus_node(model, "team:{}".format(intent.args["team"]))
    else:
        tui_model.focus_node(model, "member:{}/{}".format(intent.args["team"], intent.args["member"]))
    return True


def execute_add(spec: Dict[str, Any], env: Dict[str, str]) -> int:
    """One ``herdr-synapse add`` per selected agent (the join routine briefs each); stops at the first refusal."""
    from herdr_team.console import run_cli

    added: List[Dict[str, Any]] = []
    for member in spec.get("members") or []:
        rc, out, err = run_cli(add_args(spec, member), env)
        if err:
            err = dict(err)
            err["added_before_failure"] = [m.get("member", {}).get("name") if isinstance(m.get("member"), dict) else m.get("name") for m in added]
            sys.stderr.write(json.dumps(err, ensure_ascii=False) + "\n")
            return rc or EXIT_REFUSED
        added.append(out if isinstance(out, dict) else {"name": member.get("name")})
    sys.stdout.write(json.dumps({"team": spec.get("team"), "mode": "add", "added": added}, ensure_ascii=False) + "\n")
    return EXIT_OK


def execute_create(spec: Dict[str, Any], env: Dict[str, str]) -> int:
    """Run the picker's result: ``create`` for a new team, ``add`` per member for an existing one."""
    if spec.get("mode") == "add":
        return execute_add(spec, env)
    from herdr_team.console import run_cli

    rc, out, err = run_cli(create_args(spec), env)
    if err:
        sys.stderr.write(json.dumps(err, ensure_ascii=False) + "\n")
        return rc or EXIT_REFUSED
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    return rc


# --------------------------------------------------------------------------
# entrypoint (registered by cmd_misc as ``picker``)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--force", action="store_true", help="run from a plain shell instead of the popup")
    parser.add_argument("--dry-run", action="store_true", help="print the create spec instead of running create")


def run_args(args: argparse.Namespace) -> int:
    from herdr_team import cli as _cli

    env = dict(args.env)
    layout = _cli.layout_for(args)
    if env.get("HERDR_PLUGIN_ENTRYPOINT_ID") != ENTRYPOINT and not getattr(args, "force", False):
        raise HerdrTeamError("not_a_plugin_pane", "picker runs in the team-up popup; use `herdr-synapse ui picker` or pass --force", EXIT_REFUSED)
    if not _paths.socket_allowed(layout.config_dir, layout.socket):
        # Plan 4.1 / PK-07: an unlisted socket makes the pane a no-op before any socket call.
        sys.stderr.write("herdr-synapse picker skipped: socket not allowed\n")
        return EXIT_OK
    if not sys.stdout.isatty():
        raise HerdrTeamError("no_tty", "picker needs a terminal", EXIT_REFUSED)
    api = _cli.api_for(args, layout)
    spec = run(layout, api, env, actions=not getattr(args, "dry_run", False))
    if spec is None:
        return EXIT_OK
    if getattr(args, "dry_run", False):
        sys.stdout.write(json.dumps(spec, ensure_ascii=False) + "\n")
        return EXIT_OK
    return execute_create(spec, env)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="herdr-synapse picker", allow_abbrev=False)
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
