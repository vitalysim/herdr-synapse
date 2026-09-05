"""The team-up popup (curses), plan 5.5 and 11.

``agent list`` rows sorted by workspace then pane, greyed when
``launch_pending``; Space toggles, ``w`` scopes to the current workspace,
``a`` selects all; then team name, charter (multi-line, ``Ctrl-O`` loads a
file), per member role, name, and brief; confirm screen; the popup exits
and the action performs create, rename, label, tokens, briefing jobs, view.
Esc and Ctrl-C exit; 10 min idle watchdog; refreshes on ``who.json``.

The state machine lives in ``tui_model`` (``PickerModel``,
``picker_apply_key``); this module reads ``agent.list`` once through
``api`` (plus on ``r``), marks agents already in a roster from the session's
``team.json`` files, runs the curses loop, and finally executes the
``create`` spec through the ``herdr-team`` CLI so the join routine runs in
exactly one code path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

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


def session_rosters(layout: Optional[Layout]) -> Dict[str, List[Dict[str, Any]]]:
    """``{team: members}`` for every team in the session (read-only, no locks)."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    if layout is None:
        return out
    for name in layout.session.list_teams():
        doc = store.read_json(layout.team(name).team_json, None)
        if isinstance(doc, dict) and isinstance(doc.get("members"), list):
            out[name] = [m for m in doc["members"] if isinstance(m, dict)]
    return out


def fetch_agents(api: Any) -> List[Dict[str, Any]]:
    result = api.request("agent.list", {})
    agents = result.get("agents") if isinstance(result, dict) else None
    return [a for a in (agents or []) if isinstance(a, dict)]


def build_model(api: Any, context: Dict[str, Any], layout: Optional[Layout] = None) -> PickerModel:
    """Rows from ``agent.list``, claimed rows from the session rosters, scope from the context."""
    agents = fetch_agents(api)
    focused = context.get("workspace_id") or (context.get("focused_pane_id") or "").split(":")[0] or None
    rows = tui_model.picker_rows_from_agent_list(agents, focused)
    rosters = session_rosters(layout)
    tui_model.mark_claimed(rows, rosters)
    model = PickerModel(rows=rows, focused_workspace=focused)
    model.live_names = {str(a.get("name")) for a in agents if a.get("name")}
    model.existing_teams = sorted(rosters)
    model.scope_workspace = focused if focused and any(r.workspace_id == focused for r in rows) else None
    return model


def refresh_rows(model: PickerModel, api: Any, layout: Optional[Layout]) -> None:
    """Re-read ``agent.list`` (on ``r``), keeping selections and wizard input by pane id."""
    keep = {r.pane_id: r for r in model.rows}
    fresh = build_model(api, {"workspace_id": model.focused_workspace}, layout)
    for row in fresh.rows:
        old = keep.get(row.pane_id)
        if old is not None:
            row.selected = old.selected and tui_model.selectable(row)
            row.role, row.member_name, row.brief = old.role, old.member_name, old.brief
    model.rows = fresh.rows
    model.live_names = fresh.live_names
    model.existing_teams = fresh.existing_teams
    model.cursor = min(model.cursor, max(0, len(tui_model.visible_rows(model)) - 1))


def refresh_status_from_who(model: PickerModel, layout: Optional[Layout]) -> None:
    """Cheap tick refresh: agent status of listed rows from ``who.json`` (no socket)."""
    if layout is None:
        return
    who = store.read_json(layout.session.who_json, None)
    if not isinstance(who, dict):
        return
    by_pane: Dict[str, str] = {}
    for team_doc in (who.get("teams") or {}).values():
        for m in (team_doc or {}).get("members") or []:
            if isinstance(m, dict) and m.get("pane_id") and m.get("agent_status"):
                by_pane[str(m["pane_id"])] = str(m["agent_status"])
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
    """``herdr-team create`` argv for a picker spec."""
    args: List[str] = ["create", str(spec["team"])]
    if spec.get("charter"):
        args += ["--charter", str(spec["charter"])]
    for member in spec.get("members") or []:
        target = "{}:{}:{}".format(member["target"], member["role"], member["name"])
        args += ["--member", target]
        if member.get("brief"):
            args += ["--brief", "{}={}".format(member["name"], member["brief"])]
    return args


def _loop(stdscr: Any, model: PickerModel, api: Any, layout: Optional[Layout]) -> Optional[Dict[str, Any]]:
    import curses

    from herdr_team.console import draw_lines, enable_bracketed_paste, disable_bracketed_paste, read_key

    curses.raw()
    curses.noecho()
    stdscr.keypad(True)
    stdscr.timeout(int(TICK_S * 1000))
    enable_bracketed_paste()
    last_key = time.monotonic()
    pending_path: Optional[str] = None
    try:
        while True:
            height, width = stdscr.getmaxyx()
            lines = tui_model.picker_lines(model, width, height)
            if pending_path is not None:
                lines = lines[: max(0, height - 1)] + ["file path: " + pending_path]
            draw_lines(stdscr, lines, (min(len(lines), height) - 1, min(width - 1, tui_model.display_width(lines[-1]) if lines else 0)))
            key = read_key(stdscr)
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
            if intent.kind == "create":
                return intent.args
    finally:
        disable_bracketed_paste()


def run(layout: Layout, api: Any, env: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Returns the create request the action executes, or None when cancelled."""
    import curses

    model = build_model(api, load_context(env), layout)
    return curses.wrapper(_loop, model, api, layout)


def execute_create(spec: Dict[str, Any], env: Dict[str, str]) -> int:
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
        raise HerdrTeamError("not_a_plugin_pane", "picker runs in the team-up popup; use `herdr-team ui picker` or pass --force", EXIT_REFUSED)
    if not _paths.socket_allowed(layout.config_dir, layout.socket):
        # Plan 4.1 / PK-07: an unlisted socket makes the pane a no-op before any socket call.
        sys.stderr.write("herdr-team picker skipped: socket not allowed\n")
        return EXIT_OK
    if not sys.stdout.isatty():
        raise HerdrTeamError("no_tty", "picker needs a terminal", EXIT_REFUSED)
    api = _cli.api_for(args, layout)
    spec = run(layout, api, env)
    if spec is None:
        return EXIT_OK
    if getattr(args, "dry_run", False):
        sys.stdout.write(json.dumps(spec, ensure_ascii=False) + "\n")
        return EXIT_OK
    return execute_create(spec, env)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="herdr-team picker", allow_abbrev=False)
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
