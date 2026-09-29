"""``whiteboard``: the visual layer's switches and the page (0.21).

Three switches (``herdr_team.features`` owns their semantics and authority):

* ``whiteboard enable | disable``: the layer for this Herdr session, off by
  default; the operator in person. ``disable`` also stops the page server and
  clears the watch ``doing`` tokens, and nothing stored is deleted.
* ``whiteboard team on | off``: this team's canvas, on by default once the
  layer is; the operator or a delegate.
* ``whiteboard viz on | off``: this team's live visuals (agent HTML and
  JavaScript in a sealed frame), on by default whenever the canvas is; same
  authority as the team switch.

Every flip that changes what agents may do posts one ``whiteboard_state``
awareness record, so members learn it from the board rather than from a
refusal.

``whiteboard open`` starts the loopback page server (``whiteboard_server``)
when needed, mints a one-use opening ticket and opens the browser; over SSH
or without a browser it prints the URL and an ``ssh -L`` line instead. The
plugin action (``prefix+a``) runs as the plugin, not as the operator, so it
hands off to the ``whiteboard`` popup, whose process the popup tier verifies
as the human: only a page started by the operator can write.

``whiteboard views`` prints the generated team views (``herdr_team.views``);
``clear`` archives the team's canvas and ``purge`` deletes stored canvases.
The contract is ``.local/prd/canvas-contracts.md`` section 10.2.
"""
from __future__ import annotations

import argparse
import getpass
import socket as _socket
import sys
import time
import webbrowser
from typing import Any, Callable, Dict, List, Optional

from herdr_team import PLUGIN_ID
from herdr_team import features
from herdr_team import paths as _paths
from herdr_team import roster as _roster
from herdr_team.cli import Command, api_for, emit, layout_for
from herdr_team.cmd_board import _open_team, audit, env_of, resolve_author, resolve_team, warn
from herdr_team.errors import EXIT_REFUSED, EXIT_UNREACHABLE, HerdrTeamError, UsageError
from herdr_team.paths import SessionPaths, TeamPaths

#: Test hook: opens the page URL in the operator's browser.
OPEN_BROWSER: Callable[[str], bool] = webbrowser.open

ACTIONS = ("status", "enable", "disable", "team", "viz", "open", "stop", "views", "clear", "purge")
#: The page's canvas engines (canvas v2 phase 6, 1.4): v2 is the page's default; v1 is the classic Excalidraw canvas.
ENGINES = ("v1", "v2")
SWITCH_ACTIONS = ("team", "viz")
#: The manifest ``[[panes]]`` entry the plugin action hands off to.
POPUP_ENTRYPOINT = "whiteboard"
POPUP_RETRY_CODES = ("plugin_pane_open_failed", "ui_busy", "popup_open", "popup_already_open")
POPUP_RETRY_DELAY_S = 0.5
#: How long an opening ticket lives (``whiteboard_server.TICKET_TTL_S``), for the printed hint.
TICKET_HINT_S = 120
#: ``doctor`` warns about a page server nobody has used for this long.
SERVER_UNUSED_WARN_S = 24 * 3600
SSH_ENV = ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")


# --------------------------------------------------------------------------
# shared views of the switch (me, orient, the briefing, doctor)


def canvas_version(team: TeamPaths) -> Optional[int]:
    """The team canvas's version, or None when there is none or it cannot be read."""
    from herdr_team import canvas as _canvas

    try:
        return int(_canvas.current_version(team))
    except Exception:  # noqa: BLE001 - the version is decoration on me and the briefing; never fail them over it
        return None


def canvas_summary(team: TeamPaths) -> Optional[Dict[str, Any]]:
    """``canvas.summary(team)``, or None when it cannot be read."""
    from herdr_team import canvas as _canvas

    try:
        summary = _canvas.summary(team)
    except Exception:  # noqa: BLE001 - status and doctor report what they can
        return None
    return summary if isinstance(summary, dict) else None


def switch_view(session: SessionPaths, team: TeamPaths, doc: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``me``'s ``whiteboard`` object: the effective switch, the canvas version while it is on, and the one line agents read."""
    switch = features.team_switch(session, team, doc)
    version = canvas_version(team) if switch.on else None
    out = switch.to_json()
    out.update(version=version, line=features.me_line(switch, version))
    return out


def server_status(layout: Any) -> Optional[Dict[str, Any]]:
    """``whiteboard_server.status(layout)``, or None when no server runs or its record cannot be read."""
    from herdr_team import whiteboard_server as _server

    try:
        return _server.status(layout)
    except Exception:  # noqa: BLE001 - a status read must not fail on the page server's record
        return None


def watched_agents(session: SessionPaths) -> List[Dict[str, Any]]:
    """The watched agents (``activity.watched``), or none when they cannot be read."""
    from herdr_team import activity as _activity

    try:
        rows = _activity.watched(session)
    except Exception:  # noqa: BLE001 - reading flags must not fail status or doctor
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _age_s(stamp: Any, now: float) -> Optional[float]:
    parsed = _roster.parse_iso(stamp) if isinstance(stamp, str) else None
    return None if parsed is None else max(0.0, now - parsed)


def server_idle_s(server: Dict[str, Any], now: Optional[float] = None) -> Optional[float]:
    """Seconds since a page last talked to the server (``page_at``, refreshed with every beat), else since it started; None when it says neither."""
    now = time.time() if now is None else now
    if server.get("streams"):
        return 0.0
    for key in ("page_at", "started_at"):
        age = _age_s(server.get(key), now)
        if age is not None:
            return age
    return None


def doctor_report(layout: Any, config_text: Optional[str], now: Optional[float] = None) -> Dict[str, Any]:
    """What ``doctor`` says about the visual layer: the three switch levels, the page server, watched agents, warnings."""
    state = features.status(layout.session)
    server = server_status(layout)
    watched = watched_agents(layout.session)
    warnings: List[str] = []
    page: Dict[str, Any] = {"running": False}
    if server:
        idle = server_idle_s(server, now)
        page = {"running": True, "pid": server.get("pid"), "port": server.get("port"), "url": server.get("url"),
                "writable": server.get("writable"), "idle_minutes": None if idle is None else int(idle // 60)}
        if not state["session"]["enabled"]:
            warnings.append("the whiteboard page server is running while the whiteboard is off; stop it: herdr-synapse whiteboard stop")
        elif idle is not None and idle >= SERVER_UNUSED_WARN_S:
            warnings.append("the whiteboard page server has run for a day with no page open; stop it: herdr-synapse whiteboard stop")
    if watched and config_text is not None and "[ui.sidebar.agents]" in config_text and "$team_doing" not in config_text:
        warnings.append("{} watched agent{} but the sidebar rows lack $team_doing; run: herdr-synapse setup --print-config, re-paste the block, then herdr server reload-config".format(
            len(watched), " is" if len(watched) == 1 else "s are"))
    if watched and not state["session"]["enabled"]:
        warnings.append("{} agent{} flagged for watch, but the whiteboard is off, so nothing is shown; herdr-synapse whiteboard enable".format(
            len(watched), " is" if len(watched) == 1 else "s are"))
    return {"session": state["session"], "teams": state["teams"], "page": page,
            "watched": [{"name": w.get("name"), "pane_id": w.get("pane_id"), "team": w.get("team")} for w in watched],
            "warnings": warnings}


# --------------------------------------------------------------------------
# arguments


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("action", nargs="?", default="status", choices=ACTIONS, metavar="ACTION",
                        help="status (default) | enable | disable | team on|off | viz on|off | open | stop | views | clear | purge")
    parser.add_argument("value", nargs="?", choices=("on", "off"), metavar="on|off", help="team and viz: on or off")
    parser.add_argument("--no-browser", dest="no_browser", action="store_true", help="open: print the URL instead of opening a browser")
    parser.add_argument("--engine", choices=ENGINES, help="open: the canvas engine for this page: v2 (the default) or v1, the classic "
                                                          "Excalidraw canvas kept for comparison (the link carries ?engine=)")
    parser.add_argument("--popup", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--all-teams", dest="all_teams", action="store_true", help="purge: every team's stored canvas, not only this team's")
    parser.add_argument("--yes", action="store_true", help="purge: do not ask first (nothing is archived)")


def _author(args: argparse.Namespace, layout: Any, api: Any) -> Any:
    """The caller, resolved with this invocation's team as a hint when there is one (several teams and no --team is no hint)."""
    try:
        hint = resolve_team(args, layout, None, required=False)
    except HerdrTeamError:
        hint = None
    return resolve_author(args, layout, api, team=hint)


def _team_hint(args: argparse.Namespace, layout: Any, author: Any) -> Optional[str]:
    try:
        return resolve_team(args, layout, author, required=False)
    except HerdrTeamError:
        return None


# --------------------------------------------------------------------------
# status


def _team_line(name: str, row: Dict[str, Any], layer: bool) -> str:
    if not row.get("enabled"):
        return "team {}: canvas off (team switch; herdr-synapse --team {} whiteboard team on)".format(name, name)
    if not layer:
        return "team {}: canvas on, waiting for the session switch; live visuals {}".format(name, "on" if row.get("viz") else "off")
    text = "team {}: canvas on · live visuals {}".format(name, "on" if row.get("viz_on") else "off")
    canvas = row.get("canvas")
    if isinstance(canvas, dict):
        text += " · v{} · {} element{}".format(canvas.get("version", 0), canvas.get("elements", 0), "" if canvas.get("elements") == 1 else "s")
        if canvas.get("comments_open"):
            text += " · {} open comment{}".format(canvas["comments_open"], "" if canvas["comments_open"] == 1 else "s")
        if canvas.get("claims_active"):
            text += " · {} active claim{}".format(canvas["claims_active"], "" if canvas["claims_active"] == 1 else "s")
    return text


def _run_status(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    state = features.status(layout.session)
    for name, row in state["teams"].items():
        row["canvas"] = canvas_summary(layout.team(name)) if row.get("on") else None
    server = server_status(layout)
    watched = watched_agents(layout.session)
    payload = dict(state, server=server, watched=len(watched))

    def human() -> str:
        layer = state["session"]
        if layer["enabled"]:
            lines = ["whiteboard: on for this Herdr session{}".format(
                " (by {} via {}, {})".format(layer.get("by"), layer.get("via"), layer.get("at")) if layer.get("by") else "")]
        else:
            lines = ["whiteboard: off for this Herdr session (the operator turns it on with: herdr-synapse whiteboard enable)"]
        if server:
            lines.append("page server: running on port {} (pid {}, {}); open it with prefix+a or herdr-synapse whiteboard open".format(
                server.get("port"), server.get("pid"), "writable" if server.get("writable") else "read-only"))
        else:
            lines.append("page server: not running")
        for name in sorted(state["teams"]):
            lines.append(_team_line(name, state["teams"][name], bool(layer["enabled"])))
        if not state["teams"]:
            lines.append("no teams in this session")
        lines.append("watched agents: {}".format(len(watched)))
        return "\n".join(lines)

    return emit(args, payload, human)


# --------------------------------------------------------------------------
# enable / disable


def _stop_server(layout: Any, reason: str, notes: List[str]) -> bool:
    from herdr_team import whiteboard_server as _server

    try:
        return bool(_server.stop(layout, reason))
    except Exception as err:  # noqa: BLE001 - the switch is already off; report the leftover, do not fail the command
        notes.append("the page server could not be stopped: {}".format(err))
        return False


def _clear_tokens(layout: Any, api: Any, notes: List[str]) -> int:
    from herdr_team import activity as _activity

    try:
        return int(_activity.clear_doing_tokens(layout, api) or 0)
    except Exception as err:  # noqa: BLE001 - the tokens carry a TTL and lapse on their own; say so and go on
        notes.append("sidebar doing tokens could not be cleared now ({}); they lapse within a minute".format(err))
        return 0


def _run_layer(args: argparse.Namespace, enabled: bool) -> int:
    layout = layout_for(args)
    api = api_for(args, layout)
    author = _author(args, layout, api)
    team_hint = _team_hint(args, layout, author)
    features.check_authority(author, features.LEVEL_LAYER, layout, team_hint)
    result = features.set_layer(layout.session, enabled, author.name, author.via)
    notes: List[str] = []
    stopped = False
    cleared = 0
    if not enabled:
        # Off means nothing runs: the server and the watch tokens go now, even
        # when the switch was already off (a leftover from a crash is still a leftover).
        stopped = _stop_server(layout, "disabled", notes)
        cleared = _clear_tokens(layout, api, notes)
    notices = features.announce_layer(layout, bool(result["previous"]), enabled, author.name) if result["changed"] else {}
    if result["changed"]:
        for name in layout.session.list_teams():
            audit(layout, name, "whiteboard_layer", author, {"enabled": enabled})
    for note in notes:
        warn(args, note)
    payload = {"enabled": enabled, "changed": bool(result["changed"]), "by": result.get("by"), "via": result.get("via"), "at": result.get("at"),
               "notices": notices, "server_stopped": stopped, "tokens_cleared": cleared, "warnings": notes}

    def human() -> str:
        if enabled:
            head = ("whiteboard enabled for this Herdr session" if result["changed"] else "the whiteboard was already on")
            lines = [head + ": watch any agent; a team's canvas stays off until you turn it on for that team "
                            "(herdr-synapse --team <team> whiteboard team on, or d on its row in prefix+t)",
                     "open the page with prefix+a or: herdr-synapse whiteboard open"]
        else:
            head = ("whiteboard disabled for this Herdr session" if result["changed"] else "the whiteboard was already off")
            lines = [head + ": canvas commands are refused and nothing is shown; nothing stored was deleted (herdr-synapse whiteboard purge does that)"]
            if stopped:
                lines.append("stopped the page server")
            if cleared:
                lines.append("{} sidebar doing token{} cleared".format(cleared, "" if cleared == 1 else "s"))
        if notices:
            lines.append("told on the board: {}".format(", ".join(sorted(notices))))
        return "\n".join(lines)

    return emit(args, payload, human)


# --------------------------------------------------------------------------
# team on|off, viz on|off


def _run_switch(args: argparse.Namespace) -> int:
    if args.value is None:
        raise UsageError("whiteboard {} takes on or off".format(args.action))
    on = args.value == "on"
    layout, _api, author, team_name, team_paths, doc = _open_team(args, require_server=True, write=True)
    who = features.check_authority(author, features.LEVEL_TEAM, layout, team_name)
    before = features.team_switch(layout.session, team_paths, doc)
    if args.action == "team":
        result = features.set_team(team_paths, enabled=on, by=author.name, via=author.via)
    else:
        result = features.set_team(team_paths, viz=on, by=author.name, via=author.via)
    after = features.team_switch(layout.session, team_paths)
    seq = features.announce(layout, team_paths, before, after, author.name) if result["changed"] else None
    if result["changed"]:
        audit(layout, team_name, "whiteboard_switch", author, {"switch": args.action, "value": args.value, "by": who})
    payload = {"team": team_name, "switch": after.to_json(), "changed": list(result["changed"]), "notice_seq": seq, "by": who}

    def human() -> str:
        what = "canvas" if args.action == "team" else "live visuals"
        state = "{} {} for team {}{}".format(what, args.value, team_name, "" if result["changed"] else " (unchanged)")
        lines = [state]
        if not after.layer:
            lines.append("the whiteboard is off for this Herdr session, so nothing shows until the operator runs: herdr-synapse whiteboard enable")
        elif args.action == "viz" and on and not after.on:
            lines.append("the team's canvas is off, so live visuals wait for: herdr-synapse --team {} whiteboard team on".format(team_name))
        if seq is not None:
            lines.append("members were told on the board (#{})".format(seq))
        if args.action == "team" and on and after.on:
            lines.append("members started from now on get the canvas tools at launch; running members can use herdr-synapse canvas now")
        return "\n".join(lines)

    return emit(args, payload, human)


# --------------------------------------------------------------------------
# open / stop


def _refuse_non_human(layout: Any, team: Optional[str], author: Any, action: str) -> None:
    """``open`` and ``stop`` belong to people: a member (or a hook) refuses ``author_mismatch``, audited."""
    if author.is_human:
        return
    audit(layout, team, "author_mismatch", author, {"action": action})
    raise HerdrTeamError("author_mismatch", "{} is for the operator; {} is not, so post a request to the operator".format(action, author.name),
                         EXIT_REFUSED, {"action": action, "author": author.name, "via": author.via})


def ssh_session(env: Dict[str, str]) -> bool:
    """True when this shell came in over SSH: a browser here would open on the wrong machine."""
    return any(env.get(key) for key in SSH_ENV)


def ssh_hint(port: Any, env: Dict[str, str]) -> str:
    """The port-forward line to run on the machine with the browser."""
    user = env.get("USER") or env.get("LOGNAME")
    if not user:
        try:
            user = getpass.getuser()
        except (KeyError, OSError):
            user = ""
    try:
        host = _socket.gethostname() or "this-host"
    except OSError:
        host = "this-host"
    target = "{}@{}".format(user, host) if user else host
    return "ssh -L {port}:127.0.0.1:{port} {target}".format(port=port, target=target)


def _open_popup(args: argparse.Namespace, layout: Any, api: Any, env: Dict[str, str]) -> int:
    """The plugin action runs as the plugin, which cannot vouch for a person; the popup's process can."""
    if not _paths.socket_allowed(layout.config_dir, layout.socket):
        return emit(args, {"opened": False, "skipped": "socket_not_allowed"}, "whiteboard open skipped: socket not allowed")
    params: Dict[str, Any] = {"plugin_id": PLUGIN_ID, "entrypoint": POPUP_ENTRYPOINT, "focus": True}
    team = env.get("HERDR_TEAM")
    if team:
        params["env"] = {"HERDR_TEAM": team}
    last: Optional[HerdrTeamError] = None
    for attempt in range(2):
        try:
            api.request("plugin.pane.open", params)
            return emit(args, {"opened": False, "popup": True}, "whiteboard: opening the popup")
        except HerdrTeamError as err:
            last = err
            if err.code not in POPUP_RETRY_CODES or attempt == 1:
                break
            time.sleep(POPUP_RETRY_DELAY_S)
    assert last is not None
    raise last


def _wait_to_close(args: argparse.Namespace) -> None:
    """In the popup, keep what was printed on screen until the human closes it (a popup closes with its command)."""
    stdin = getattr(args, "stdin", None) or sys.stdin
    try:
        if not stdin.isatty():
            return
        out = getattr(args, "stdout", None) or sys.stdout
        out.write("press Enter to close\n")
        out.flush()
        stdin.readline()
    except (OSError, ValueError):
        return


def _run_open(args: argparse.Namespace) -> int:
    if not args.popup:
        return _open(args)
    try:
        return _open(args)
    except HerdrTeamError as err:
        # A popup closes when its command exits: say why first, so the refusal is read, not flashed.
        out = getattr(args, "stdout", None) or sys.stdout
        out.write("whiteboard: {}\n".format(err.message))
        out.flush()
        _wait_to_close(args)
        raise


def with_engine(url: str, engine: Optional[str]) -> str:
    """The ticket URL with ``engine=`` when one was asked for (the ticket's redirect keeps it); none opens the page's
    default, canvas v2 (phase 6, 1.4)."""
    if not url or engine not in ENGINES:
        return url
    return "{}{}engine={}".format(url, "&" if "?" in url else "?", engine)


def _open(args: argparse.Namespace) -> int:
    from herdr_team import whiteboard_server as _server

    layout = layout_for(args)
    api = api_for(args, layout)
    env = env_of(args)
    author = _author(args, layout, api)
    if author.is_system and not env.get("HERDR_PLUGIN_EVENT") and not args.popup:
        # A plugin action (``HERDR_PLUGIN_ACTION_ID``; an event hook is refused below).
        return _open_popup(args, layout, api, env)
    team_hint = _team_hint(args, layout, author)
    _refuse_non_human(layout, team_hint, author, "whiteboard open")
    features.require_layer(layout.session)
    info = _server.start(layout, env, author, open_browser=False)
    url = with_engine(str(info.get("url") or ""), getattr(args, "engine", None))
    port = info.get("port")
    over_ssh = ssh_session(env)
    if args.no_browser:
        browser = "skipped"
    elif over_ssh:
        browser = "ssh"
    else:
        try:
            browser = "opened" if OPEN_BROWSER(url) else "failed"
        except (webbrowser.Error, OSError):
            browser = "failed"
    hint = ssh_hint(port, env) if browser in ("ssh", "failed") or (args.no_browser and over_ssh) else None
    payload = {"url": url, "port": port, "pid": info.get("pid"), "writable": bool(info.get("writable")), "started": bool(info.get("started")),
               "browser": browser, "ssh_hint": hint, "ticket_ttl_s": TICKET_HINT_S}

    def human() -> str:
        lines = ["whiteboard page: {}".format(url), "  (the link works once, within {} minutes)".format(TICKET_HINT_S // 60)]
        if browser == "opened":
            lines.append("opened in your browser")
        if not payload["writable"]:
            lines.append("read-only: this shell could not be verified as yours, so the page cannot draw; open it with prefix+a or from the console to draw")
        if hint:
            lines.append("no browser here: on the machine with your browser run {} and open the link there".format(hint))
        return "\n".join(lines)

    code = emit(args, payload, human)
    if args.popup and browser != "opened" and not getattr(args, "json", False):
        _wait_to_close(args)
    return code


def _run_stop(args: argparse.Namespace) -> int:
    from herdr_team import whiteboard_server as _server

    layout = layout_for(args)
    api = api_for(args, layout)
    author = _author(args, layout, api)
    _refuse_non_human(layout, _team_hint(args, layout, author), author, "whiteboard stop")
    stopped = bool(_server.stop(layout, "stopped"))
    return emit(args, {"stopped": stopped}, "stopped the page server" if stopped else "no page server was running")


# --------------------------------------------------------------------------
# views, clear, purge


def _run_views(args: argparse.Namespace) -> int:
    from herdr_team import views as _views

    layout, api, author, team_name, _team_paths, _doc = _open_team(args, require_server=False)
    if not author.is_human and author.team != team_name:
        # The timeline is this team's board: a member reads its own team's, as with ``board``.
        raise HerdrTeamError("not_a_member", "{} is not a member of team {}".format(author.name, team_name), EXIT_UNREACHABLE,
                             {"author": author.name, "team": team_name})
    payload = _views.team_views(layout, team_name, api=api)
    return emit(args, payload)


def _run_clear(args: argparse.Namespace) -> int:
    from herdr_team import canvas as _canvas

    layout, _api, author, team_name, team_paths, _doc = _open_team(args, require_server=True, write=True)
    who = features.check_authority(author, features.LEVEL_TEAM, layout, team_name)
    result = _canvas.clear(layout, team_paths, author.name)
    audit(layout, team_name, "whiteboard_clear", author, {"by": who, "archived": result.get("archived")})
    payload = dict(result, team=team_name)
    return emit(args, payload, "canvas of team {} archived to {}; it starts empty".format(team_name, result.get("archived")))


def _confirm_purge(args: argparse.Namespace, names: List[str]) -> bool:
    """Ask first, like ``wipe --purge``; without a terminal, refuse ``confirmation_required`` (pass --yes)."""
    question = "delete the stored canvas of {} for good? drawings, comments, assets and archives go; nothing is kept".format(", ".join(names) or "no team")
    stdin = getattr(args, "stdin", None) or sys.stdin
    out = getattr(args, "stdout", None) or sys.stdout
    try:
        interactive = stdin.isatty() and out.isatty()
    except (AttributeError, ValueError):
        interactive = False
    if not interactive:
        raise HerdrTeamError("confirmation_required", "{} (pass --yes)".format(question), EXIT_REFUSED, {"teams": names})
    out.write("{} [y/N] ".format(question))
    out.flush()
    return stdin.readline().strip().lower() in ("y", "yes")


def _run_purge(args: argparse.Namespace) -> int:
    from herdr_team import canvas as _canvas

    layout = layout_for(args)
    api = api_for(args, layout)
    author = _author(args, layout, api)
    if args.all_teams:
        names = layout.session.list_teams()
    else:
        name = resolve_team(args, layout, author)
        assert name is not None
        names = [name]
    features.check_authority(author, features.LEVEL_LAYER, layout, names[0] if len(names) == 1 else None)
    if not args.yes and not _confirm_purge(args, names):
        return emit(args, {"purged": [], "teams": names}, "nothing deleted")
    purged: List[str] = []
    for name in names:
        if _canvas.purge(layout.team(name)):
            purged.append(name)
            audit(layout, name, "whiteboard_purge", author, {})
    payload = {"purged": purged, "teams": names}
    return emit(args, payload, "deleted the stored canvas of: {}".format(", ".join(purged)) if purged else "nothing stored to delete")


# --------------------------------------------------------------------------
# dispatch


def _run(args: argparse.Namespace) -> int:
    action = args.action
    if args.value is not None and action not in SWITCH_ACTIONS:
        raise UsageError("on|off belongs to whiteboard team or whiteboard viz")
    if action == "status":
        return _run_status(args)
    if action in ("enable", "disable"):
        return _run_layer(args, action == "enable")
    if action in SWITCH_ACTIONS:
        return _run_switch(args)
    if action == "open":
        return _run_open(args)
    if action == "stop":
        return _run_stop(args)
    if action == "views":
        return _run_views(args)
    if action == "clear":
        return _run_clear(args)
    return _run_purge(args)


def _add_serve_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port", type=int, default=0, metavar="N", help="listen on this loopback port (default: a free one)")


def _run_serve(args: argparse.Namespace) -> int:
    """The page server in the foreground: what ``whiteboard open`` spawns."""
    from herdr_team import whiteboard_server as _server

    layout = layout_for(args)
    features.require_layer(layout.session)
    return int(_server.serve(layout, env_of(args), port=max(0, int(args.port or 0)), api=api_for(args, layout)))


COMMANDS: List[Command] = [
    Command(
        "whiteboard",
        "the visual layer: switches (enable, team, viz), the local page (open, stop), team views, clear, purge",
        _add_arguments,
        _run,
        description=("whiteboard [status] | enable | disable (the operator in person) | team on|off | viz on|off (the operator or a delegate) | "
                     "open [--no-browser] [--engine v1|v2] | stop (people only) | views | clear | purge [--all-teams] --yes. The layer is off by default; "
                     "once on, a team's canvas is still off until turned on for that team (team on); its live visuals are on unless switched off."),
    ),
    Command("whiteboard-serve", "the whiteboard page server in the foreground (spawned by whiteboard open)", _add_serve_arguments, _run_serve, hidden=True),
]
