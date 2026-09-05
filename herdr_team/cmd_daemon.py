"""Command group: ``daemon start|stop|status`` and ``notifier stats`` (docs/cli.md section 9).

``daemon start`` is the manifest's ``[[startup]]`` command and the
``daemon-start`` action: it detaches the notifier (plan 8.1) and returns
once the grandchild has reported, so ``bin/hook`` short-circuits from the
first event. A second start with a live daemon exits 0 ``already_running``;
``--replace`` waits up to 10 s for the lock, then SIGTERMs the recorded pid
(start-time verified) and takes over. ``socket_not_allowed`` exits 0 with
``{"skipped": "socket_not_allowed"}`` so the rig's ``allowed-sockets`` file
keeps startup hooks inert elsewhere.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Optional

from herdr_team import VERSION, paths, store
from herdr_team import cli as _cli
from herdr_team.cli import emit, layout_for
from herdr_team.errors import EXIT_OK, HerdrTeamError, UsageError



def _daemon_mod() -> Any:
    from herdr_team import daemon as _daemon

    return _daemon


def _info_json(info: Any) -> Optional[Dict[str, Any]]:
    if info is None:
        return None
    obj = info.to_json() if hasattr(info, "to_json") else dict(info)
    return {
        "pid": obj.get("pid"),
        "start_time": obj.get("start_time"),
        "socket": obj.get("socket"),
        "herdr_version": obj.get("herdr_version"),
        "protocol": obj.get("protocol"),
        "version": obj.get("version"),
        "beat_at": obj.get("beat_at"),
    }


def _write_pointer(layout: paths.Layout) -> Optional[str]:
    """Record the state root for this config dir (plan 4.1); best effort, never fatal."""
    if layout.state_root.source == paths.STATE_SOURCE_TEAM_ARG:
        return None
    try:
        return os.fspath(paths.write_pointer(layout.config_dir, layout.state_root.path))
    except (HerdrTeamError, OSError):
        return None


# --------------------------------------------------------------------------
# daemon start|stop|status


def _daemon_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument("--replace", action="store_true", help="start: take over from a running daemon")
    parser.add_argument("--allow-version", action="store_true", help="start: run against a Herdr other than 0.8.x")
    parser.add_argument("--dry-nudge", action="store_true", help="start: log nudges instead of typing them (HERDR_TEAM_DRY_NUDGE=1)")
    parser.add_argument("--timeout", type=float, default=10.0, help="stop: seconds to wait for the daemon to exit")


def _human_start(payload: Dict[str, Any]) -> str:
    if payload.get("skipped"):
        return "daemon start skipped: {}\n".format(payload["skipped"])
    daemon = payload.get("daemon") or {}
    if payload.get("already_running"):
        return "notifier already running (pid {}, started {})\n".format(daemon.get("pid"), daemon.get("start_time"))
    verb = "replaced the running notifier" if payload.get("replaced") else "notifier started"
    return "{}: pid {} on {} (Herdr {}, protocol {})\nsession dir: {}\n".format(
        verb, daemon.get("pid"), daemon.get("socket"), daemon.get("herdr_version") or "?", daemon.get("protocol") or "?", payload.get("session_dir"),
    )


def _after_start(args: argparse.Namespace, layout: paths.Layout, payload: Dict[str, Any]) -> None:
    """Startup-hook duties once a daemon is confirmed (plan 11, 7.3): reapply the view, tidy the console.

    Both are best effort over the socket; a server that lacks a method or is
    not reachable only leaves the corresponding key ``None``.
    """
    payload["view"] = None
    payload["console"] = None
    try:
        api = _cli.api_for(args, layout)
    except HerdrTeamError:
        return
    doc = store.read_json(layout.session.view_json, default=None)
    if isinstance(doc, dict) and doc.get("view", doc.get("on")) in (True, "on"):
        teams = layout.session.list_teams()
        if teams:
            from herdr_team import cmd_misc as _cmd_misc

            try:
                api.request("agent.view.set", _cmd_misc.view_request(teams))
                payload["view"] = "reapplied"
            except HerdrTeamError as err:
                payload["view"] = "error:{}".format(err.code)
        else:
            # Plan 11: reapplied only when a team exists for that socket; a stale view.json is dropped.
            try:
                os.unlink(layout.session.view_json)
            except OSError:
                pass
            payload["view"] = "cleared"
    try:
        from herdr_team import cmd_ui as _cmd_ui

        payload["console"] = _cmd_ui.reconcile_console(layout, api, dict(args.env))
    except Exception as err:  # noqa: BLE001 - never fail the startup hook over the console
        payload["console"] = {"error": "{}: {}".format(type(err).__name__, err)}


def run_daemon_start(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    session = layout.session
    payload: Dict[str, Any] = {
        "session_dir": os.fspath(session.root), "started": False, "already_running": False, "replaced": False, "daemon": None,
    }
    if not paths.socket_allowed(layout.config_dir, layout.socket):
        payload["skipped"] = "socket_not_allowed"
        payload["socket"] = os.fspath(layout.socket)
        return emit(args, payload, lambda: _human_start(payload))
    paths.ensure_session_dirs(session)
    dmod = _daemon_mod()
    if not args.replace and dmod.daemon_alive(session):
        payload["already_running"] = True
        payload["daemon"] = _info_json(dmod.read_daemon_info(session))
        payload["pointer"] = _write_pointer(layout)
        _after_start(args, layout, payload)
        return emit(args, payload, lambda: _human_start(payload))
    result = dmod.detach_and_run(layout, args.env, replace=bool(args.replace), allow_version=bool(args.allow_version), dry_nudge=bool(args.dry_nudge))
    status = result.get("status")
    if status == "error":
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        code = str(error.get("code") or "daemon_start_failed")
        message = str(error.get("message") or "the daemon did not start")
        details = {k: v for k, v in error.items() if k not in ("code", "message")}
        details["session_dir"] = os.fspath(session.root)
        raise HerdrTeamError(code, message, None, details)
    payload["already_running"] = status == "already_running"
    payload["started"] = status == "started"
    payload["replaced"] = bool(result.get("replaced"))
    payload["daemon"] = _info_json(result.get("daemon")) if isinstance(result.get("daemon"), dict) else _info_json(dmod.read_daemon_info(session))
    payload["intermediate_exit_ms"] = result.get("intermediate_exit_ms")
    payload["pointer"] = _write_pointer(layout)
    if payload["started"] or payload["already_running"]:
        _after_start(args, layout, payload)
    return emit(args, payload, lambda: _human_start(payload))


def run_daemon_stop(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    dmod = _daemon_mod()
    info = dmod.read_daemon_info(layout.session)
    pid = info.pid if info is not None else None
    stopped = dmod.stop_daemon(layout.session, timeout_s=float(args.timeout))
    payload = {"stopped": bool(stopped), "pid": pid if stopped else (pid if info is not None and dmod.info_alive(info) else None)}
    return emit(args, payload, lambda: ("notifier stopped (pid {})\n".format(pid) if stopped else "no live notifier for {}\n".format(layout.session.root)))


def daemon_status(layout: paths.Layout) -> Dict[str, Any]:
    """The ``daemon status`` object (docs/cli.md section 9); also embedded by ``doctor``."""
    dmod = _daemon_mod()
    session = layout.session
    info = dmod.read_daemon_info(session)
    alive = dmod.info_alive(info)
    beat = dmod.beat_age_s(session)
    who = store.read_json(session.who_json, default=None)
    pending: Dict[str, int] = {}
    if isinstance(who, dict) and isinstance(who.get("teams"), dict):
        for name, team in who["teams"].items():
            if isinstance(team, dict):
                try:
                    pending[str(name)] = int(team.get("pending") or 0)
                except (TypeError, ValueError):
                    pending[str(name)] = 0
    teams = session.list_teams()
    ledger: Dict[str, Any] = {"wrong_target": 0}
    per_team: Dict[str, Any] = {}
    from herdr_team.ledger import Ledger

    for name in teams:
        try:
            counts = Ledger(session.team(name)).counts()
        except HerdrTeamError:
            continue
        per_team[name] = counts
        for key, value in counts.items():
            if isinstance(value, int):
                ledger[key] = ledger.get(key, 0) + value
    return {
        "alive": bool(alive),
        "pid": info.pid if info is not None else None,
        "start_time": info.start_time if info is not None else None,
        "beat_age_s": round(beat, 3) if beat is not None else None,
        "socket": os.fspath(layout.socket),
        "socket_source": layout.env_socket.source,
        "herdr_version": info.herdr_version if info is not None else None,
        "protocol": info.protocol if info is not None else None,
        "version": info.version if info is not None else None,
        "plugin_version": VERSION,
        "session_dir": os.fspath(session.root),
        "teams": teams,
        "pending": pending,
        "ledger": ledger,
        "ledger_by_team": per_team,
        "identity_env_unset": info.identity_env_unset if info is not None else None,
        "manifest_version": info.manifest_version if info is not None else None,
    }


def _human_status(payload: Dict[str, Any]) -> str:
    lines = ["notifier: {}".format("alive" if payload["alive"] else "down")]
    if payload.get("pid"):
        lines.append("  pid {} started {} (beat {}s ago)".format(payload["pid"], payload.get("start_time"), payload.get("beat_age_s")))
    lines.append("  socket: {} ({})".format(payload["socket"], payload["socket_source"]))
    lines.append("  herdr: {} protocol {}; plugin {}".format(payload.get("herdr_version") or "?", payload.get("protocol") or "?", payload.get("version") or payload.get("plugin_version")))
    lines.append("  teams: {}".format(", ".join(payload["teams"]) if payload["teams"] else "none"))
    if payload["pending"]:
        lines.append("  pending: " + ", ".join("{}={}".format(k, v) for k, v in payload["pending"].items()))
    ledger = payload.get("ledger") or {}
    lines.append("  ledger: wrong_target={} landed_working={} intents={}".format(ledger.get("wrong_target", 0), ledger.get("landed_working", 0), ledger.get("intents", 0)))
    return "\n".join(lines) + "\n"


def run_daemon_status(args: argparse.Namespace) -> int:
    payload = daemon_status(layout_for(args))
    return emit(args, payload, lambda: _human_status(payload))


def run_daemon(args: argparse.Namespace) -> int:
    if args.action == "start":
        return run_daemon_start(args)
    if args.action == "stop":
        return run_daemon_stop(args)
    return run_daemon_status(args)


# --------------------------------------------------------------------------
# notifier stats


def _notifier_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("action", choices=("stats",))
    parser.add_argument("--kind", metavar="KIND", help="only this agent kind's clean rate")


def _human_stats(payload: Dict[str, Any]) -> str:
    lines: List[str] = []
    for team, stats in payload["teams"].items():
        counts = stats.get("counts") or {}
        lines.append("team {}: intents {} open {} landed_working {} landed_in_turn {} transient {} wrong_target {}".format(
            team, counts.get("intents", 0), counts.get("open_intents", 0), counts.get("landed_working", 0),
            counts.get("landed_in_turn", 0), counts.get("transient", 0), counts.get("wrong_target", 0),
        ))
        for kind, bucket in (stats.get("kinds") or {}).items():
            rate = bucket.get("clean_rate")
            lines.append("  {}: {} round trips, clean rate {}{}".format(
                kind, bucket.get("round_trips", 0), "n/a" if rate is None else "{:.0%}".format(rate), " (verified)" if bucket.get("verified") else "",
            ))
    if not lines:
        lines.append("no teams in {}".format(payload["session_dir"]))
    return "\n".join(lines) + "\n"


def run_notifier(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    from herdr_team.ledger import Ledger

    team_name = paths.team_name_from_arg(getattr(args, "team", None), args.env)
    names = layout.session.list_teams()
    if team_name is not None:
        if team_name not in names:
            raise HerdrTeamError("team_not_found", "team {!r} does not exist in this session".format(team_name), None, {"team": team_name, "teams": names})
        names = [team_name]
    teams: Dict[str, Any] = {}
    for name in names:
        stats = Ledger(layout.team(name)).stats()
        if args.kind:
            stats["kinds"] = {k: v for k, v in stats.get("kinds", {}).items() if k == args.kind}
        teams[name] = stats
    payload = {"session_dir": os.fspath(layout.session.root), "teams": teams, "wrong_target": sum(int((s.get("counts") or {}).get("wrong_target", 0)) for s in teams.values())}
    return emit(args, payload, lambda: _human_stats(payload))


Command = _cli.Command

COMMANDS: List[Command] = [
    Command(
        name="daemon",
        help="start, stop, or inspect the notifier daemon",
        add_arguments=_daemon_args,
        run=run_daemon,
        description="daemon start [--replace] [--allow-version] | daemon stop | daemon status. The daemon is the only process that types into members or shows toasts.",
    ),
    Command(
        name="notifier",
        help="delivery ledger statistics per team and kind",
        add_arguments=_notifier_args,
        run=run_notifier,
        description="notifier stats [--team NAME] [--kind KIND]: intents, results, outcomes, clean rates, and the wrong_target counter (must stay 0).",
    ),
]
