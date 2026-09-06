"""UI commands: the pane entrypoints and ``ui <picker|compose|console|who|usage|close>``.

The pane entrypoints (``console``, ``compose``, ``picker``) run the curses
UIs when Herdr launches a manifest pane (``HERDR_PLUGIN_ENTRYPOINT_ID`` set);
from a plain shell they refuse with ``not_a_plugin_pane`` unless
``--target-pane`` / ``--force`` is given (docs/cli.md "Pane entrypoints").

``ui`` opens those panes over the socket with ``plugin.pane.open`` (plan 7.1,
docs/cli.md section 9): one retry after 500 ms on ``plugin_pane_open_failed``
(popup already open, terminal too small, ``ui_busy``), then a fallback to the
console entrypoint with an explicit ``--target-pane``. ``ui close`` speaks
``popup.close`` because ``herdr popup`` is not a CLI command on 0.8.2.

``plugin.pane.open`` answers ``plugin_pane_opened`` (pane id at
``plugin_pane.pane.pane_id``) for split/tab/overlay/zoomed placements and a
bare ``{"type":"ok"}`` for ``popup`` (``open_plugin_popup_pane`` in
``src/app/api/plugins/panes.rs``); ``herdr_team.api.plugin_pane_id`` reads
both, so ``pane_id`` is null for popups.
"""

from __future__ import annotations

import argparse
import calendar
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from herdr_team import PLUGIN_ID, compose, console, picker
from herdr_team.api import plugin_pane_id
from herdr_team.cli import Command, api_for, emit, layout_for
from herdr_team.errors import HerdrTeamError

UI_TARGETS = ("picker", "compose", "console", "who", "usage", "knowledge", "close")
#: Manifest placement per entrypoint (herdr-plugin.toml ``[[panes]]``).
PLACEMENTS = {"picker": "popup", "compose": "popup", "console": "split", "who": "popup", "usage": "popup", "knowledge": "popup"}
#: ``ui who`` opens the console entrypoint as a popup started on its roster
#: box (``console.START_VIEW_ENV``).
WHO_ENV = console.START_VIEW_ENV
RETRY_DELAY_S = 0.5
RETRY_CODES = ("plugin_pane_open_failed", "ui_busy", "popup_open", "popup_already_open")
NO_DAEMON_ENV = "HERDR_TEAM_NO_DAEMON"


def _add_ui_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("target", choices=UI_TARGETS, help="which plugin pane to open (or close the popup)")
    parser.add_argument("--target-pane", metavar="ID", help="pane to split or to hand the console fallback")
    parser.add_argument("--no-retry", action="store_true", help="do not retry after plugin_pane_open_failed")


def open_params(target: str, target_pane: Optional[str], env: Dict[str, str], team: Optional[str]) -> Dict[str, Any]:
    """``plugin.pane.open`` params for ``target`` (the manifest fixes placement and size)."""
    entrypoint = "console" if target == "who" else target
    pane_env: Dict[str, str] = {}
    if team:
        pane_env["HERDR_TEAM"] = team
    if target == "who":
        pane_env[WHO_ENV] = "who"
    params: Dict[str, Any] = {"plugin_id": PLUGIN_ID, "entrypoint": entrypoint, "focus": True}
    if pane_env:
        params["env"] = pane_env
    if target_pane and PLACEMENTS.get(target, "popup") != "popup":
        # HP-07 (2026-09-05, Herdr 0.8.2): ``plugin.pane.open`` rejects ``target_pane_id`` for a
        # popup with ``invalid_params`` ("overlay and popup plugin panes target the active pane"),
        # which is not retryable and skipped the "popup already open" retry. ``--target-pane`` for
        # a popup target names the console fallback split only.
        params["target_pane_id"] = target_pane
    if target == "who":
        params["placement"] = "popup"
    return params


def _is_retryable(err: HerdrTeamError) -> bool:
    if err.code in RETRY_CODES:
        return True
    text = (err.message or "").lower()
    return "popup already open" in text or "too small" in text or "busy" in text


def open_pane(api: Any, target: str, target_pane: Optional[str], env: Dict[str, str], team: Optional[str], retry: bool = True, sleep: Any = time.sleep, layout: Any = None) -> Dict[str, Any]:
    """Open ``target``; retry once, then fall back to the console with an explicit target pane.

    With ``layout`` the fallback first looks for the live console (plan 7.3, single writer) and
    focuses it instead of opening a second one; HP-07 (2026-09-05) opened a second console pane and
    overwrote ``console.json`` so the first console's posts became ``cli-unverified``.
    """
    params = open_params(target, target_pane, env, team)
    retried = False
    last: Optional[HerdrTeamError] = None
    for attempt in range(2 if retry else 1):
        try:
            result = api.request("plugin.pane.open", params)
            return {"ui": target, "opened": True, "placement": PLACEMENTS.get(target, "popup"), "pane_id": plugin_pane_id(result), "fallback": None, "retried": retried}
        except HerdrTeamError as err:
            last = err
            if not _is_retryable(err) or attempt == 1:
                break
            retried = True
            sleep(RETRY_DELAY_S)
    assert last is not None
    if target == "console":
        raise last
    if layout is not None:
        live_pane = focus_live_console(api, layout)
        if live_pane is not None:
            return {"ui": target, "opened": False, "focused": True, "placement": "split", "pane_id": live_pane, "fallback": "console", "retried": retried, "error": last.to_json()}
    fallback_pane = target_pane or env.get("HERDR_PANE_ID")
    if not fallback_pane:
        raise last
    fallback_params = open_params("console", fallback_pane, env, team)
    fallback_params["placement"] = "split"
    try:
        result = api.request("plugin.pane.open", fallback_params)
    except HerdrTeamError:
        raise last
    return {"ui": target, "opened": True, "placement": "split", "pane_id": plugin_pane_id(result), "fallback": "console", "retried": retried, "error": last.to_json()}


def focus_live_console(api: Any, layout: Any) -> Optional[str]:
    """Focus the live console recorded in ``console.json`` and return its current pane id, else None.

    Plan 7.3: a second open locates the console by ``terminal_id`` (pane ids change on move) and
    calls ``plugin.pane.focus``; the console is single-writer, so nothing may open a second one
    while this one is alive.
    """
    from herdr_team.cmd_board import read_console_json

    try:
        console = read_console_json(layout.session)
    except (HerdrTeamError, OSError):
        return None
    if not (console.get("open") and _pid_alive(console.get("pid"))):
        return None
    try:
        live = find_console_pane(api, console)
    except HerdrTeamError:
        return None
    if live is None or not isinstance(live.get("pane_id"), str):
        return None
    try:
        api.request("plugin.pane.focus", {"pane_id": live["pane_id"]})
    except HerdrTeamError:
        return None
    return str(live["pane_id"])


CONSOLE_TITLE = "Team console"
#: After ``plugin.pane.open`` of the console entrypoint, ``reconcile_console`` leaves labelled shells alone this long.
CONSOLE_LAUNCH_GRACE_S = 15.0
SHELL_NAMES =frozenset({"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "-sh", "-bash", "-zsh", "-fish", "login"})


def _pane_list(api: Any) -> List[Dict[str, Any]]:
    try:
        result = api.request("pane.list", {})
    except HerdrTeamError:
        return []
    panes = result.get("panes") if isinstance(result, dict) else None
    return [p for p in panes or [] if isinstance(p, dict)]


def _pid_alive(pid: Any) -> bool:
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def find_console_pane(api: Any, console_json: Dict[str, Any], panes: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """The live console pane row by the ``terminal_id`` recorded in ``console.json`` (pane ids change on move)."""
    terminal_id = console_json.get("terminal_id")
    if not isinstance(terminal_id, str) or not terminal_id:
        return None
    for pane in panes if panes is not None else _pane_list(api):
        if pane.get("terminal_id") == terminal_id:
            return pane
    return None


def _foreground_is_shell(api: Any, pane_id: str) -> Optional[bool]:
    """True when the pane's foreground is only a shell (a dead post-restart console); None when unknown.

    An empty ``foreground_processes`` list is *unknown*, not a shell: the
    server reports it while a pane's process is still being spawned, and a
    console being born (``console.sh`` before python starts) must not be
    closed by a concurrent ``doctor`` or ``daemon start``.
    """
    try:
        result = api.request("pane.process_info", {"pane_id": pane_id})
    except HerdrTeamError:
        return None
    info = result.get("process_info") if isinstance(result, dict) and isinstance(result.get("process_info"), dict) else result
    if not isinstance(info, dict):
        return None
    procs = info.get("foreground_processes")
    if not isinstance(procs, list) or not procs:
        return None
    names = [str(p.get("name") or "").rsplit("/", 1)[-1] for p in procs if isinstance(p, dict)]
    return all(name in SHELL_NAMES for name in names) if names else None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _iso_age_s(value: Any, now: Optional[float] = None) -> Optional[float]:
    """Seconds since an ISO-8601 UTC stamp written by this plugin; None when absent or unparsable."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] if value.endswith("Z") else value
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%f") if "." in text else datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    stamp = calendar.timegm(parsed.timetuple()) + parsed.microsecond / 1e6
    return (time.time() if now is None else now) - stamp


def record_console_launch(layout: Any) -> None:
    """Stamp ``launched_at`` in ``console.json``: the console entrypoint was just opened and may still be booting."""
    from herdr_team.cmd_board import read_console_json, write_console_json

    try:
        console = read_console_json(layout.session)
        console["launched_at"] = _utc_now_iso()
        write_console_json(layout.session, console)
    except (HerdrTeamError, OSError):
        pass


def _launch_in_progress(console: Dict[str, Any]) -> bool:
    """True inside ``CONSOLE_LAUNCH_GRACE_S`` of the last ``launched_at`` (the pane may not have written ``console.json`` yet)."""
    age = _iso_age_s(console.get("launched_at"))
    return age is not None and 0.0 <= age < CONSOLE_LAUNCH_GRACE_S


def mark_console_closed_if_pane_gone(session: Any, api: Any) -> bool:
    """After ``pane.closed`` while the server is up: ``open:false`` once the console's terminal is gone (UI-05).

    Herdr's pane shutdown hangs the console up (SIGHUP, then SIGTERM, then
    SIGKILL at 250 ms steps), so the process cannot record its own exit, and
    the event carries only a pane id that ``console.json`` may hold stale
    after a move. ``pane.list`` by ``terminal_id`` is the truth: the pane is
    gone while the server answers, so the human (or ``plugin pane close``)
    closed it and ``daemon start``/``doctor`` must not reopen it. A session
    stop fires no ``pane.closed`` event, so a cold restart keeps ``open:true``
    and ``reconcile_console`` reopens. An unreachable or empty ``pane.list``
    is no evidence. Returns True when the record changed; never raises.
    """
    from herdr_team.cmd_board import read_console_json, write_console_json

    try:
        console = read_console_json(session)
        terminal_id = console.get("terminal_id")
        if not console.get("open") or not isinstance(terminal_id, str) or not terminal_id:
            return False
        panes = _pane_list(api)
        if not panes or any(p.get("terminal_id") == terminal_id for p in panes):
            return False
        console["open"] = False
        console["pid"] = None
        console["closed_at"] = _utc_now_iso()
        write_console_json(session, console)
        return True
    except (HerdrTeamError, OSError, ValueError, TypeError):
        return False


def reconcile_console(layout: Any, api: Any, env: Dict[str, str], reopen: bool = True) -> Dict[str, Any]:
    """Plan 7.3 / RT-05: close dead ``Team console`` shells left by a cold restart; reopen when ``open:true``.

    Returns ``{"closed": [pane ids], "reopened": pane id|None, "live": pane id|None, "unresolved": [pane ids]}``;
    never raises. A labelled shell is closed only when its foreground is
    known to be a shell and no console launch is inside
    ``CONSOLE_LAUNCH_GRACE_S``; labelled non-live panes skipped for either
    reason are listed in ``unresolved`` so the daemon can try again (the
    startup hook runs before a restored pane's shell has even been spawned,
    RT-05 in the rig).
    """
    from herdr_team.cmd_board import read_console_json, write_console_json

    out: Dict[str, Any] = {"closed": [], "reopened": None, "live": None, "unresolved": []}
    try:
        console = read_console_json(layout.session)
        panes = _pane_list(api)
        live = find_console_pane(api, console, panes) if _pid_alive(console.get("pid")) and console.get("open") else None
        if live is not None:
            out["live"] = live.get("pane_id")
        launching = _launch_in_progress(console)
        for pane in panes:
            if live is not None and pane.get("terminal_id") == live.get("terminal_id"):
                continue
            if pane.get("label") != CONSOLE_TITLE and pane.get("title") != CONSOLE_TITLE:
                continue
            if pane.get("agent"):
                continue  # an agent adopted the labelled pane; never close it
            pane_id = pane.get("pane_id")
            if not isinstance(pane_id, str):
                continue
            if launching:
                out["unresolved"].append(pane_id)
                continue  # a console opened seconds ago is booting; its pane is a bare shell until python starts
            foreground = _foreground_is_shell(api, pane_id)
            if foreground is None:
                out["unresolved"].append(pane_id)  # process info not known yet (a restored pane before its shell spawned)
                continue
            if foreground is not True:
                continue
            try:
                api.request("pane.close", {"pane_id": pane_id})
                out["closed"].append(pane_id)
            except HerdrTeamError:
                continue
        if live is None and console.get("open") and reopen:
            if console.get("pid") and not _pid_alive(console.get("pid")):
                console["open"] = False
                console["pid"] = None
                write_console_json(layout.session, console)
            team = console.get("default_team") if isinstance(console.get("default_team"), str) else None
            try:
                result = open_pane(api, "console", None, env, team, retry=False)
                out["reopened"] = result.get("pane_id") or "opened"
                record_console_launch(layout)
            except HerdrTeamError as err:
                out["reopen_error"] = err.code
    except (HerdrTeamError, OSError, ValueError, TypeError) as err:
        out["error"] = "{}: {}".format(type(err).__name__, err)
    return out


def _run_ui(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    from herdr_team import paths as _paths

    if not _paths.socket_allowed(layout.config_dir, layout.socket):
        # Plan 4.1 / PK-07: manifest actions and panes are no-ops for a socket the rig has not listed.
        return emit(args, {"ui": args.target, "skipped": "socket_not_allowed", "socket": os.fspath(layout.socket)}, "ui {} skipped: socket not allowed".format(args.target))
    api = api_for(args, layout)
    env = dict(args.env)
    target = args.target
    if target == "close":
        api.request("popup.close", {})
        return emit(args, {"ui": "close", "closed": True}, "popup closed")
    team = getattr(args, "team", None) or env.get("HERDR_TEAM")
    if team and ("/" in team or team.startswith((".", "~"))):
        team = layout.state_root.team_dir.name if layout.state_root.team_dir else None
    if target == "picker" and env.get(NO_DAEMON_ENV) != "1":
        # team-up ensures the daemon (plan 8.1); best effort, never fatal here.
        try:
            from herdr_team import daemon as _daemon

            _daemon.ensure_daemon(layout, env)
        except (HerdrTeamError, OSError):
            pass
    if target == "console":
        # Plan 7.3: a second open locates the console by terminal_id and focuses it (single writer).
        live_pane = focus_live_console(api, layout)
        if live_pane is not None:
            return emit(args, {"ui": "console", "opened": False, "focused": True, "placement": "split", "pane_id": live_pane, "fallback": None, "retried": False}, "console already open in {}; focused".format(live_pane))
    payload = open_pane(api, target, getattr(args, "target_pane", None), env, team, retry=not getattr(args, "no_retry", False), layout=layout)
    if payload.get("opened") and (target in ("console", "who") or payload.get("fallback") == "console"):
        record_console_launch(layout)  # protects the booting pane from reconcile_console's close rule
    human = "{} opened ({})".format(target, payload["placement"])
    if payload.get("fallback") and payload.get("focused"):
        human = "{} popup failed; live console {} focused instead".format(target, payload["pane_id"])
    elif payload.get("fallback"):
        human += "; popup failed, console opened in a split instead"
    return emit(args, payload, human)


def _run_console(args: argparse.Namespace) -> int:
    return console.run_args(args)


def _run_compose(args: argparse.Namespace) -> int:
    return compose.run_args(args)


def _run_picker(args: argparse.Namespace) -> int:
    return picker.run_args(args)


COMMANDS: List[Command] = [
    Command("ui", "open a plugin pane: picker, compose, console, who, usage; or close the popup", _add_ui_arguments, _run_ui),
    Command("console", "team console pane (launched by the manifest pane; --target-pane from a shell)", console.add_arguments, _run_console, hidden=True),
    Command("compose", "post popup (launched by the manifest pane)", compose.add_arguments, _run_compose, hidden=True),
    Command("picker", "team-up popup (launched by the manifest pane)", picker.add_arguments, _run_picker, hidden=True),
]
