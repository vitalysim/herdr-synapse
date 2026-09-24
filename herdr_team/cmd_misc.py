"""Command group: doctor setup keys install-cli gc prune view teardown nudge mute unmute pause focus read.

Setup and diagnostics (plan section 10, ``docs/cli.md`` section 9) plus the
delivery commands of section 8 that only write job files or ``mute.json``
for the daemon. ``doctor`` never fails on warnings and runs ``herdr plugin
list --json`` only when ``HERDR_TEAM_ALLOW_HERDR=1`` is set; every other
Herdr call goes over the socket through ``api_for`` (a ``FakeApi`` in tests).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from herdr_team import asks as _asks
from herdr_team import charter as _charter
from herdr_team import cmd_board as _cmd_board
from herdr_team import gate
from herdr_team import PLUGIN_ID, VERSION
from herdr_team import api as _api
from herdr_team import operator as _operator
from herdr_team import cli as _cli
from herdr_team import paths as _paths
from herdr_team import roster as _roster
from herdr_team import store
from herdr_team import workdir as _workdir
from herdr_team.cli import api_for, emit, layout_for
from herdr_team.cmd_board import (
    agent_members,
    daemon_status,
    default_team_of,
    enqueue_job,
    env_of,
    load_doc,
    members_of,
    mute_state,
    now_iso,
    parse_iso,
    pid_alive,
    read_console_json,
    resolve_author,
    resolve_team,
    toast_delivery,
    view_state,
    warn,
)
from herdr_team.errors import EXIT_DAEMON_DOWN, EXIT_REFUSED, HerdrTeamError, UsageError
from herdr_team.identity import Author
from herdr_team.paths import Layout, SessionPaths, TeamPaths


from herdr_team.cli import Command

ALLOW_HERDR_ENV = "HERDR_TEAM_ALLOW_HERDR"
GC_MAX_AGE_S = 7 * 24 * 3600
VIEW_SOURCE = "plugin:" + PLUGIN_ID
VIEW_PROBE_SOURCE = PLUGIN_ID + ".probe"
VIEW_LABEL_MAX = 20
VIEW_LABEL_MAX_TWO = 27
FULL_SCREEN_KINDS = frozenset({"claude", "opencode", "codex", "kilo", "omp"})
DEFAULT_MUTE = "10m"

KEYS: Dict[str, str] = {"team-up": "prefix+t", "compose": "prefix+m", "console": "prefix+u", "toggle-view": "prefix+y", "usage": "prefix+i", "knowledge": "prefix+f"}
KEY_DESCRIPTIONS: Dict[str, str] = {"team-up": "team up: pick agents", "compose": "post to the team board", "console": "open the team console", "toggle-view": "toggle the team agents view", "usage": "usage limits across agents", "knowledge": "team knowledge bases"}
#: The order the bindings are printed in.
KEY_ACTIONS = ("team-up", "compose", "console", "toggle-view", "usage", "knowledge")

#: The token a stale sidebar block is missing; ``doctor`` looks for it.
COLOR_SLOT_TOKEN = "$team_c1"
#: The token a block from before the context gauge is missing. Kept separate
#: from the colour check so the advice names what is actually absent.
CONTEXT_TOKEN = "$" + _roster.CONTEXT_TOKENS[0]
#: One cell per team colour slot, then the role and the task. Herdr styles a cell from a fixed
#: ``fg`` and cannot colour by a token's value, but a row drops the tokens that have no value, so
#: only the cell matching the member's team ever renders. That is what colour-codes the teams.
COLOR_ROW = "  " + ", ".join(
    ['{{ token = "${}", fg = "{}" }}'.format(_roster.color_slot_key(slot + 1), hex_value) for slot, hex_value in enumerate(_roster.TEAM_COLOR_HEX)]
    + ['{ token = "$team_role", dim = true }', '{ token = "$team_task", fg = "#89b4fa" }']
    + ['{{ token = "${}", fg = "{}" }}'.format(key, hex_value)
       for key, hex_value in zip(_roster.CONTEXT_TOKENS, ("#7f849c", "#fabd2f", "#fb4934"))]
)
COLOR_ROW = "  [" + COLOR_ROW.strip() + "],"

SIDEBAR_SNIPPET = """[ui.sidebar.agents]
rows = [
  ["state_icon", "agent"],
{color_row}
  ["workspace", "tab"],
]
[ui.sidebar.agents.rows_by_agent]
claude = [
  ["state_icon", "agent"],
{color_row}
  ["terminal_title_stripped"],
  ["workspace", "tab"],
]
""".format(color_row=COLOR_ROW)

OPTIONAL_SNIPPET = """[ui]
# Distinct static glyphs for blocked, working, done, idle, and unknown (changes every agent's marks).
status_indicators = "symbols"
# Room for `$team_role`, `$team_task` and the context gauge next to the agent name.
sidebar_width = 34
sidebar_max_width = 42
"""

SETUP_NOTES = [
    "Paste the required block into ~/.config/herdr/config.toml, then run: herdr server reload-config (an invalid snippet rejects the whole config; check with: herdr config check).",
    "Sidebar rows only appear after the first pane.report_metadata write; members are stamped at join.",
    "The optional block is cosmetic; status_indicators = \"symbols\" changes glyphs for every agent, not only team members.",
    "Key bindings are never installed automatically; herdr-synapse keys check validates the snippet against your config.",
]


# --------------------------------------------------------------------------
# keys


def keys_snippet(keys: Optional[Dict[str, str]] = None) -> str:
    keys = keys or KEYS
    blocks = []
    for action in KEY_ACTIONS:
        blocks.append(
            "[[keys.command]]\nkey = \"{}\"\ntype = \"plugin_action\"\ncommand = \"{}.{}\"\ndescription = \"{}\"\n".format(
                keys[action], PLUGIN_ID, action, KEY_DESCRIPTIONS[action]
            )
        )
    return "\n".join(blocks)


_KEY_LINE_RE = re.compile(r"^\s*#?\s*([a-z_]+)\s*=\s*\"([^\"]*)\"")
_COMMAND_KEY_RE = re.compile(r"^\s*key\s*=\s*\"([^\"]*)\"")


def parse_key_bindings(text: str, defaults: bool) -> Dict[str, str]:
    """``{binding: action}`` from a config or ``--default-config`` text (``[keys]`` and ``[[keys.command]]``)."""
    out: Dict[str, str] = {}
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[[keys.command]]"):
            section = "command"
            continue
        if stripped.startswith("[keys.indexed]"):
            section = "indexed"
            continue
        if stripped.startswith("[keys]"):
            section = "keys"
            continue
        if stripped.startswith("[") and not stripped.startswith("[keys"):
            section = None
            continue
        if section == "keys":
            if not defaults and stripped.startswith("#"):
                continue
            match = _KEY_LINE_RE.match(line)
            if match and match.group(2):
                out[match.group(2)] = match.group(1)
        elif section == "command":
            if stripped.startswith("#") and not defaults:
                continue
            match = _COMMAND_KEY_RE.match(line)
            if match and match.group(1):
                out[match.group(1)] = "keys.command"
    return out


def key_collisions(default_text: Optional[str], user_text: Optional[str], keys: Optional[Dict[str, str]] = None) -> List[Dict[str, str]]:
    keys = keys or KEYS
    bound: Dict[str, str] = {}
    if default_text:
        bound.update(parse_key_bindings(default_text, defaults=True))
    if user_text:
        bound.update(parse_key_bindings(user_text, defaults=False))
    out = []
    for action, key in keys.items():
        owner = bound.get(key)
        if owner and owner != "keys.command":
            out.append({"key": key, "bound_to": owner, "action": action})
        elif owner == "keys.command" and user_text and "{}.{}".format(PLUGIN_ID, action) not in user_text:
            out.append({"key": key, "bound_to": "keys.command", "action": action})
    return out


def _add_keys_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("action", choices=("print", "check"))


def _run_keys(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    if args.action == "print":
        return emit(args, {"snippet": keys_snippet(), "keys": dict(KEYS)}, keys_snippet())
    api = api_for(args, layout)
    default_text: Optional[str] = None
    result = api.run(["--default-config"], pin_socket=False)
    if result.ok:
        default_text = result.stdout
    user_text: Optional[str] = None
    try:
        user_text = (layout.config_dir / "config.toml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        user_text = None
    check = api.run(["config", "check"], pin_socket=False)
    output = (check.stdout or "") + (check.stderr or "")
    collisions = key_collisions(default_text, user_text)
    ok = check.ok and not collisions
    payload = {"ok": ok, "collisions": collisions, "output": output.strip(), "default_config_read": default_text is not None}

    def human() -> str:
        lines = ["config check: {}".format("ok" if check.ok else "failed")]
        for c in collisions:
            lines.append("collision: {} is bound to {} ({})".format(c["key"], c["bound_to"], c["action"]))
        if not collisions:
            lines.append("no collisions for {}".format(", ".join(KEYS.values())))
        if output.strip():
            lines.append(output.strip())
        return "\n".join(lines)

    return emit(args, payload, human)


# --------------------------------------------------------------------------
# setup


def _add_setup_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--print-config", dest="print_config", action="store_true")
    parser.add_argument("--no-probe", dest="no_probe", action="store_true", help="skip the one notification.show toast probe")


def _setup_toast_probe(args: argparse.Namespace) -> Tuple[Optional[str], Dict[str, Any]]:
    """Plan 7.2: ``setup`` probes ``notification.show`` once and reports the effective mode.

    Best effort: ``setup --print-config`` must keep working with no server
    (it only prints TOML), so an unreachable or unlisted socket yields
    ``probed: false`` with the reason instead of an error.
    """
    probe: Dict[str, Any] = {"probed": False, "shown": False, "reason": None}
    if getattr(args, "no_probe", False):
        probe["reason"] = "skipped"
        return None, probe
    try:
        layout = layout_for(args)
    except HerdrTeamError as err:
        probe["reason"] = err.code
        return None, probe
    delivery = toast_delivery(layout.config_dir)
    try:
        if not _paths.socket_allowed(layout.config_dir, layout.socket):
            probe["reason"] = "socket_not_allowed"
            return delivery, probe
    except HerdrTeamError:
        pass
    api = api_for(args, layout)
    try:
        api.ping(timeout=2.0)
    except HerdrTeamError as err:
        probe["reason"] = err.code
        return delivery, probe
    probe = _toast_probe(api)
    if probe.get("reason") == "disabled":
        delivery = "off"
    return delivery, probe


def teams_with_nothing_to_restore(layout: Any) -> List[str]:
    """Teams whose members would learn almost nothing from ``orient``.

    ``orient`` can only hand back what the operator wrote. A team with no rules
    and no instructions gives a compacted member its name and the charter
    headline and little else, which looks like the command is broken when it is
    the team that is empty. Naming it here is cheaper than finding out after an
    agent has lost the plot.
    """
    from herdr_team import charter as _charter

    out: List[str] = []
    try:
        names = sorted(p.name for p in layout.session.teams_dir.iterdir() if p.is_dir())
    except OSError:
        return out
    for name in names:
        try:
            doc = store.read_json(layout.team(name).team_json)
        except (HerdrTeamError, OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        members = [m for m in doc.get("members") or [] if isinstance(m, dict) and m.get("kind") != "human" and m.get("status") != "left"]
        if not members:
            continue
        try:
            rules = _charter.get_rules(layout, name) or ""
        except (HerdrTeamError, OSError):
            rules = ""
        empty = [str(m.get("name")) for m in members if not (_charter.get_instructions(layout, name, str(m.get("name"))) or "").strip()]
        if not rules.strip():
            out.append("team {} has no rules; every member reads them: herdr-synapse knowledge set \"...\"".format(name))
        if empty:
            out.append("team {}: {} of {} members have empty instructions ({}); herdr-synapse instructions <name> --set \"...\"".format(
                name, len(empty), len(members), ", ".join(empty[:3]) + ("…" if len(empty) > 3 else "")))
    return out


def unwritable_project_dirs(layout: Any) -> List[str]:
    """Teams whose project directory has gone away or turned read-only.

    The mirror is refreshed best effort, so a moved checkout or a read-only
    mount stops updating the folder with nothing on screen to say why. This is
    the one place that says it out loud.
    """
    out: List[str] = []
    try:
        names = sorted(p.name for p in layout.session.teams_dir.iterdir() if p.is_dir())
    except OSError:
        return out
    for name in names:
        try:
            doc = store.read_json(layout.team(name).team_json)
        except (HerdrTeamError, OSError, ValueError):
            continue
        project = _workdir.project_dir_of(doc if isinstance(doc, dict) else {})
        if not project:
            continue
        path = Path(project)
        if not path.is_dir():
            out.append("team {}: project directory {} is gone; run: herdr-synapse project set <path> or project clear".format(name, project))
        elif not os.access(os.fspath(path), os.W_OK):
            out.append("team {}: project directory {} is not writable; the team folder has stopped updating".format(name, project))
    return out


def sidebar_missing_team_colors(config_dir: Path) -> bool:
    """True when config.toml configures the Agents sidebar but predates the team colour cells.

    Such a config renders the role and the task but no team name, with nothing on screen to say
    why, so ``doctor`` points at the re-paste. A config with no sidebar block at all is not
    stale: the user simply has not set the sidebar up.
    """
    try:
        text = (config_dir / "config.toml").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    return "[ui.sidebar.agents]" in text and COLOR_SLOT_TOKEN not in text


def sidebar_missing_context(config_dir: Path) -> bool:
    """True when config.toml has a sidebar block that predates the context gauge."""
    try:
        text = (config_dir / "config.toml").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    return "[ui.sidebar.agents]" in text and COLOR_SLOT_TOKEN in text and CONTEXT_TOKEN not in text


def _run_setup(args: argparse.Namespace) -> int:
    if not args.print_config:
        raise UsageError("setup --print-config prints the config blocks; setup never edits config")
    required = SIDEBAR_SNIPPET + "\n" + keys_snippet()
    delivery, toast_probe = _setup_toast_probe(args)
    payload = {"required": required, "optional": OPTIONAL_SNIPPET, "notes": list(SETUP_NOTES), "toast_delivery": delivery, "toast_probe": toast_probe}
    if toast_probe.get("probed"):
        toast_note = "toast delivery: {} (probe: {})".format(delivery, toast_probe.get("reason"))
    else:
        toast_note = "toast delivery: {} (not probed: {})".format(delivery if delivery is not None else "unknown", toast_probe.get("reason"))
    human = "# --- required: sidebar rows and key bindings ---\n{}\n# --- optional: glyphs and sidebar width ---\n{}\n{}\n# {}".format(
        required, OPTIONAL_SNIPPET, "\n".join("# note: " + n for n in SETUP_NOTES), toast_note
    )
    return emit(args, payload, human)


# --------------------------------------------------------------------------
# doctor


def state_root_candidates(env: Dict[str, str], config_dir: Path, team_arg: Optional[str]) -> List[Dict[str, Any]]:
    """Every candidate of the resolution order with the value it would contribute."""
    out: List[Dict[str, Any]] = []
    team_path: Optional[str] = None
    if team_arg and _paths.looks_like_path(team_arg):
        try:
            team_path = os.fspath(_paths.state_root_from_team_dir(Path(os.path.expanduser(team_arg))))
        except HerdrTeamError:
            team_path = None
    out.append({"source": _paths.STATE_SOURCE_TEAM_ARG, "path": team_path})
    out.append({"source": _paths.STATE_SOURCE_OVERRIDE, "path": env.get("HERDR_TEAM_STATE_DIR") or None})
    team_dir = env.get("HERDR_TEAM_DIR")
    team_dir_root: Optional[str] = None
    if team_dir:
        try:
            team_dir_root = os.fspath(_paths.state_root_from_team_dir(Path(team_dir)))
        except HerdrTeamError:
            team_dir_root = None
    out.append({"source": _paths.STATE_SOURCE_TEAM_DIR, "path": team_dir_root})
    out.append({"source": _paths.STATE_SOURCE_PLUGIN, "path": env.get("HERDR_PLUGIN_STATE_DIR") or None})
    pointer = None
    try:
        p = _paths.read_pointer(config_dir)
        pointer = os.fspath(p) if p else None
    except HerdrTeamError:
        pointer = None
    out.append({"source": _paths.STATE_SOURCE_POINTER, "path": pointer})
    try:
        out.append({"source": _paths.STATE_SOURCE_XDG, "path": os.fspath(_paths.default_state_root(config_dir, env))})
    except HerdrTeamError:
        out.append({"source": _paths.STATE_SOURCE_XDG, "path": None})
    return out


def pointer_warnings(config_dir: Path, env: Mapping[str, str]) -> List[str]:
    """Name a pointer that sends this config dir somewhere other than its default.

    The pointer outranks Herdr's derived state dir, so a stray one silently
    hides every team from the plugin's panes and from the next daemon start.
    """
    try:
        pointed = _paths.read_pointer(config_dir)
        default = _paths.default_state_root(config_dir, env)
    except HerdrTeamError:
        return []
    if pointed is None or os.path.realpath(os.fspath(pointed)) == os.path.realpath(os.fspath(default)):
        return []
    fix = "if your teams are not there, rewrite {} or delete it".format(_paths.pointer_file(config_dir))
    if not pointed.is_dir():
        return ["state pointer names {}, which does not exist; {}".format(pointed, fix)]
    return ["state pointer redirects this config dir to {} instead of {}; {}".format(pointed, default, fix)]


def _plugin_entry(plugins: Any, source: str) -> Dict[str, Any]:
    for entry in plugins or []:
        if isinstance(entry, dict) and entry.get("plugin_id") == PLUGIN_ID:
            return {"installed": True, "enabled": bool(entry.get("enabled")), "path": entry.get("plugin_root") or entry.get("manifest_path"), "warnings": list(entry.get("warnings") or []), "source": source}
    return {"installed": False, "enabled": False, "path": None, "warnings": [], "source": source}


def _plugin_state(args: argparse.Namespace, layout: Layout, env: Dict[str, str], reachable: bool = True) -> Dict[str, Any]:
    """Plugin registry state: ``plugin.list`` over the socket by default (PK-09), the CLI only as an opt-in fallback."""
    api = api_for(args, layout)
    if reachable:
        try:
            result = api.request("plugin.list", {})
            plugins = result.get("plugins") if isinstance(result, dict) else result
            return _plugin_entry(plugins, "plugin.list")
        except HerdrTeamError as err:
            if err.code not in ("unknown_method", "invalid_params", "herdr_protocol") and err.exit_code != 3:
                return {"installed": None, "enabled": None, "path": None, "warnings": [], "error": "plugin.list: {}".format(err.code)}
    if env.get(ALLOW_HERDR_ENV) != "1":
        return {"installed": None, "enabled": None, "path": None, "warnings": [], "skipped": "Herdr unreachable over the socket; set {}=1 to run herdr plugin list".format(ALLOW_HERDR_ENV)}
    # ``herdr plugin list --json`` prints the socket envelope ``{"id","result":{"type":"plugin_list","plugins":[...]}}``;
    # ``run_json`` strips it to the ``plugin_list`` result.
    try:
        parsed = api.run_json(["plugin", "list", "--json"], timeout=3.0)
    except HerdrTeamError as err:
        if err.code == "herdr_protocol":
            return {"installed": None, "enabled": None, "path": None, "warnings": [], "error": "plugin list printed no JSON"}
        return {"installed": None, "enabled": None, "path": None, "warnings": [], "error": "plugin list: {}".format(err.message)[:300]}
    plugins = parsed.get("plugins") if isinstance(parsed, dict) else parsed
    return _plugin_entry(plugins, "plugin list --json")


def _toast_probe(api: Any) -> Dict[str, Any]:
    """One ``notification.show`` (plan 7.2: ``doctor`` probes once and prints the effective mode)."""
    try:
        result = api.request("notification.show", {"title": "herdr-synapse doctor", "body": "toast probe; nothing to do", "sound": "none"})
    except HerdrTeamError as err:
        return {"probed": True, "shown": False, "reason": err.code}
    reason = result.get("reason") if isinstance(result, dict) else None
    shown = bool(result.get("shown")) if isinstance(result, dict) else False
    return {"probed": True, "shown": shown, "reason": str(reason or ("shown" if shown else "unknown"))}


def _run_doctor(args: argparse.Namespace) -> int:
    env = env_of(args)
    warnings: List[str] = []
    errors: List[str] = []
    layout = layout_for(args)
    api = api_for(args, layout)
    herdr: Dict[str, Any] = {"bin": _api.herdr_bin(env), "version": None, "protocol": None, "reachable": False}
    try:
        pong = api.ping(timeout=2.0)
        herdr["version"] = pong.get("version")
        herdr["protocol"] = pong.get("protocol")
        herdr["reachable"] = True
    except HerdrTeamError as err:
        warnings.append("Herdr unreachable at {}: {}".format(layout.socket, err.message))
    try:
        allowed = _paths.socket_allowed(layout.config_dir, layout.socket)
    except HerdrTeamError:
        allowed = True
    socket_info = {"path": os.fspath(layout.socket), "source": layout.env_socket.source, "session_name": layout.env_socket.session_name, "allowed": allowed}
    if not allowed:
        warnings.append("socket is not listed in allowed-sockets; hooks, actions, and daemon start are no-ops here")
    pointer_path = _paths.pointer_file(layout.config_dir)
    pointer = os.fspath(pointer_path) if pointer_path.exists() else None
    warnings.extend(pointer_warnings(layout.config_dir, env))
    state_root = {"path": os.fspath(layout.state_root.path), "source": layout.state_root.source, "candidates": state_root_candidates(env, layout.config_dir, getattr(args, "team", None))}
    if not layout.state_root.path.exists():
        warnings.append("state root {} does not exist yet".format(layout.state_root.path))
    if "Mobile Documents" in os.fspath(layout.state_root.path):
        warnings.append("state root lives in a cloud-synced directory")
    stale = sidebar_missing_team_colors(layout.config_dir)
    if stale:
        warnings.append("sidebar rows predate team colours; run: herdr-synapse setup --print-config, re-paste the block, then herdr server reload-config")
    elif sidebar_missing_context(layout.config_dir):
        warnings.append("sidebar rows predate the context gauge; run: herdr-synapse setup --print-config, re-paste the block, then herdr server reload-config")
    for line in unwritable_project_dirs(layout):
        warnings.append(line)
    for line in teams_with_nothing_to_restore(layout):
        warnings.append(line)
    from . import pi_support

    for team_name in layout.session.list_teams():
        doc = store.read_json(layout.session.team(team_name).team_json, default={})
        for member in doc.get("members", []):
            if member.get("kind") == "pi" and member.get("status") == "active" and pi_support.read_snapshot(layout.session, member) is None:
                warnings.append("{}: Pi runtime report missing/stale; install Herdr's Pi integration, run `herdr-synapse hooks install pi`, then /reload in Pi".format(member.get("name")))
    for grant in _operator.active_all(layout.session):
        # Authority nobody remembers granting is the failure mode worth naming.
        warnings.append("{} acts with your authority in team {} ({}); revoke: herdr-synapse operator revoke {} --team {}".format(
            grant.get("member"), grant.get("team"),
            "until {}".format(grant["expires_at"]) if grant.get("expires_at") else "no expiry",
            grant.get("member"), grant.get("team")))
    plugin = _plugin_state(args, layout, env, reachable=bool(herdr["reachable"]))
    if plugin.get("installed") is False:
        warnings.append("plugin {} is not installed".format(PLUGIN_ID))
    elif plugin.get("enabled") is False and plugin.get("installed"):
        warnings.append("plugin {} is disabled".format(PLUGIN_ID))
    for w in plugin.get("warnings") or []:
        warnings.append("plugin: {}".format(w))
    delivery = toast_delivery(layout.config_dir)
    toast_probe: Dict[str, Any] = {"probed": False, "shown": False, "reason": None}
    if herdr["reachable"] and not getattr(args, "no_probe", False):
        toast_probe = _toast_probe(api)
        if toast_probe.get("reason") == "disabled":
            delivery = "off"
            warnings.append("toast probe: the server reports notifications disabled (reload the config after changing [ui.toast])")
        elif toast_probe.get("reason") == "no_foreground_client":
            warnings.append("toast probe: no foreground client; toasts wait until a client attaches")
    if delivery == "off":
        warnings.append("[ui.toast] delivery is off: no toasts reach the human; the console badge is the primary channel")
    console_lifecycle: Optional[Dict[str, Any]] = None
    if herdr["reachable"] and not getattr(args, "no_fix", False):
        from herdr_team import cmd_ui as _cmd_ui

        console_lifecycle = _cmd_ui.reconcile_console(layout, api, env)
        for note in console_lifecycle.get("closed") or []:
            warnings.append("closed a dead console shell in {}".format(note))
    daemon = daemon_status(layout.session)
    if not daemon["alive"]:
        warnings.append("notifier daemon not running ({})".format(daemon.get("reason")))
    if daemon.get("herdr_version") and herdr.get("version") and daemon["herdr_version"] != herdr["version"]:
        warnings.append("daemon saw herdr {} but the server reports {}".format(daemon["herdr_version"], herdr["version"]))
    teams: List[Dict[str, Any]] = []
    for name in layout.session.list_teams():
        try:
            doc = load_doc(layout.team(name))
        except HerdrTeamError as err:
            errors.append("team {}: {}".format(name, err.message))
            continue
        members = agent_members(doc)
        missing = len([m for m in members if m.get("status") not in ("active", "starting")])
        try:
            knowledge = _workdir.status(layout, name)
            missing_missions = list(knowledge.get("missing_missions") or [])
            with_missions = int(knowledge.get("with_missions") or 0)
        except (HerdrTeamError, OSError) as err:
            missing_missions = []
            with_missions = 0
            warnings.append("team {} Mission check failed: {}".format(name, err))
        teams.append({"team": name, "members": len(members), "missing": missing, "missions": with_missions, "missing_missions": missing_missions})
        if missing_missions:
            warnings.append("team {} has no Mission for: {}".format(name, ", ".join(missing_missions)))
        if doc.get("socket") and os.path.realpath(str(doc["socket"])) != os.fspath(layout.socket):
            warnings.append("team {} belongs to socket {}".format(name, doc["socket"]))
    console = read_console_json(layout.session)
    console_open = bool(console.get("open")) and pid_alive(int(console.get("pid") or 0))
    payload = {
        "ok": not errors, "version": VERSION, "python": platform.python_version(),
        "herdr": herdr, "socket": socket_info, "slug": layout.slug, "config_dir": os.fspath(layout.config_dir),
        "state_root": state_root, "pointer": pointer, "plugin": plugin, "toast_delivery": delivery, "toast_probe": toast_probe,
        "daemon": daemon, "teams": teams, "console": {"open": console_open, "pane_id": console.get("pane_id"), "lifecycle": console_lifecycle},
        "warnings": warnings, "errors": errors,
    }

    def human() -> str:
        lines = [
            "herdr-synapse {} (python {})".format(VERSION, payload["python"]),
            "herdr: {} {} protocol {} ({})".format(herdr["bin"], herdr["version"] or "?", herdr["protocol"] or "?", "reachable" if herdr["reachable"] else "unreachable"),
            "socket: {} ({}){}".format(socket_info["path"], socket_info["source"], "" if allowed else " NOT ALLOWED"),
            "slug: {}    config dir: {}".format(layout.slug, layout.config_dir),
            "state root: {} ({})".format(state_root["path"], state_root["source"]),
        ]
        for c in state_root["candidates"]:
            lines.append("  {:<28} {}".format(c["source"], c["path"] or "-"))
        lines.append("pointer: {}".format(pointer or "none"))
        if plugin.get("skipped"):
            lines.append("plugin: skipped ({})".format(plugin["skipped"]))
        else:
            lines.append("plugin: installed={} enabled={} path={}".format(plugin.get("installed"), plugin.get("enabled"), plugin.get("path") or "-"))
        lines.append("toast delivery: {}{}".format(delivery, " (probe: {})".format(toast_probe["reason"]) if toast_probe.get("probed") else ""))
        lines.append("daemon: {}{}".format("alive" if daemon["alive"] else "down", " (pid {}, beat {}s ago)".format(daemon["pid"], daemon["beat_age_s"]) if daemon["alive"] else " ({})".format(daemon.get("reason"))))
        for t in teams:
            lines.append("team {}: {} members, {} missing, {}/{} with Missions".format(t["team"], t["members"], t["missing"], t["missions"], t["members"]))
        lines.append("console: {}".format("open in {}".format(console.get("pane_id")) if console_open else "closed"))
        for w in warnings:
            lines.append("warning: {}".format(w))
        for e in errors:
            lines.append("error: {}".format(e))
        lines.append("ok" if payload["ok"] else "problems found")
        return "\n".join(lines)

    return emit(args, payload, human)


# --------------------------------------------------------------------------
# install-cli


def _add_install_cli_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--yes", action="store_true", help="actually create the symlink")
    parser.add_argument("--dir", metavar="PATH", help="install directory (default ~/.local/bin)")


def _run_install_cli(args: argparse.Namespace) -> int:
    env = env_of(args)
    home = _paths.home_dir(env)
    target = _paths.plugin_root() / "bin" / "herdr-synapse"
    directory = Path(os.path.expanduser(args.dir)) if args.dir else home / ".local" / "bin"
    link = directory / "herdr-synapse"
    created = False
    replaced = False
    existing = None
    if link.is_symlink() or link.exists():
        existing = os.readlink(link) if link.is_symlink() else "file"
    if args.yes:
        _paths.ensure_dir(directory, 0o755)
        if link.is_symlink():
            if os.path.realpath(link) != os.path.realpath(target):
                os.unlink(link)
                os.symlink(target, link)
                replaced = True
        elif link.exists():
            raise HerdrTeamError("path_exists", "{} exists and is not a symlink; remove it first".format(link), EXIT_REFUSED, {"path": os.fspath(link)})
        else:
            os.symlink(target, link)
            created = True
    payload = {"path": os.fspath(link), "target": os.fspath(target), "created": created, "replaced": replaced, "dry_run": not args.yes, "existing": existing}
    if not args.yes:
        human = "would symlink {} -> {} (pass --yes)".format(link, target)
    else:
        human = "{} -> {} ({})".format(link, target, "created" if created else "replaced" if replaced else "already linked")
    return emit(args, payload, human)


# --------------------------------------------------------------------------
# gc / prune


def _dir_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return time.time()


def _run_gc(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    sessions_dir = layout.state_root.path / _paths.SESSIONS_DIR
    removed: List[str] = []
    kept: List[str] = []
    try:
        names = sorted(os.listdir(sessions_dir))
    except OSError:
        names = []
    now = time.time()
    for name in names:
        root = sessions_dir / name
        if not root.is_dir() or root.is_symlink():
            continue
        session = _paths.session_paths(layout.state_root.path, name)
        socket_path: Optional[str] = None
        info = store.read_json(session.daemon_json)
        if isinstance(info, dict) and isinstance(info.get("socket"), str):
            socket_path = info["socket"]
        recorded = store.read_bytes(session.socket_file)
        if recorded:
            socket_path = recorded.decode("utf-8", "replace").strip() or socket_path
        if socket_path is None:
            for team_name in session.list_teams():
                doc = store.read_json(session.team(team_name).team_json)
                if isinstance(doc, dict) and isinstance(doc.get("socket"), str):
                    socket_path = doc["socket"]
                    break
        socket_gone = socket_path is None or not os.path.exists(socket_path)
        if name == layout.slug and os.path.exists(layout.socket):
            socket_gone = False
        lock_free = True
        if session.daemon_lock.exists():
            lock = store.daemon_lock(session)
            lock_free = lock.try_acquire()
            if lock_free:
                lock.release()
        old = now - _dir_mtime(root) > GC_MAX_AGE_S
        if socket_gone and lock_free and old:
            shutil.rmtree(root, ignore_errors=True)
            removed.append(os.fspath(root))
        else:
            kept.append(os.fspath(root))
    return emit(args, {"removed": removed, "kept": kept}, "removed {} session tree{}, kept {}".format(len(removed), "" if len(removed) == 1 else "s", len(kept)))


def _add_prune_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--keep-days", dest="keep_days", type=int, required=True, metavar="N")


def _run_prune(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    team_name = resolve_team(args, layout, None)
    assert team_name is not None
    team = layout.team(team_name)
    if args.keep_days < 0:
        raise UsageError("--keep-days must be >= 0")
    cutoff = time.time() - args.keep_days * 86400
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = layout.session.team_archive(team_name, "prune-" + stamp) / "archive"
    archived: List[str] = []
    freed = 0
    with store.team_lock(team):
        try:
            names = sorted(os.listdir(team.archive_dir))
        except OSError:
            names = []
        for name in names:
            if not re.match(r"^board\.\d+-\d+\.jsonl$", name):
                continue
            path = team.archive_dir / name
            try:
                st = path.lstat()
            except OSError:
                continue
            if st.st_mtime > cutoff:
                continue
            _paths.ensure_dir(destination)
            os.replace(path, destination / name)
            archived.append(os.fspath(destination / name))
            freed += st.st_size
        if archived:
            try:
                store.BoardStore(team).rebuild_archive_index()
            except HerdrTeamError:
                pass
    return emit(args, {"team": team_name, "archived": archived, "bytes_freed": freed}, "archived {} segment{} ({} bytes)".format(len(archived), "" if len(archived) == 1 else "s", freed))


# --------------------------------------------------------------------------
# view / teardown


def view_label(teams: List[str]) -> str:
    if not teams:
        return "team:none"
    if len(teams) == 1:
        return "team:{}".format(teams[0])[:VIEW_LABEL_MAX]
    return "team:{}".format("+".join(teams))[:VIEW_LABEL_MAX_TWO]


def write_view_state(layout: Layout, teams: List[str]) -> None:
    """Record that our view is showing ``teams`` (``view.json``), for ``view_state`` and for dissolve."""
    _paths.ensure_session_dirs(layout.session)
    store.write_json(layout.session.view_json, {"view": "on", "source": VIEW_SOURCE, "label": view_label(teams), "socket": os.fspath(layout.socket), "teams": teams, "set_at": now_iso()})


def view_request(teams: List[str]) -> Dict[str, Any]:
    """The ``agent.view.set`` params of plan section 11: ``team == X OR status in [blocked]``."""
    team_filters = [{"op": "eq", "field": {"token": "team"}, "value": t} for t in teams]
    return {
        "source": VIEW_SOURCE,
        "label": view_label(teams),
        "filter": {"op": "any", "filters": team_filters + [{"op": "in", "field": "status", "values": ["blocked"]}]},
        "sort": [
            {"field": "attention", "order": "desc"},
            {"field": {"token": "team_role"}, "order": "asc"},
            {"field": "state_change_seq", "order": "desc"},
        ],
    }


def probe_view_owner(api: Any) -> Tuple[str, Optional[str]]:
    """``own | none | foreign`` via ``agent.view.clear`` with the probe source; raises ``plugin_disabled``."""
    try:
        result = api.request("agent.view.clear", {"source": VIEW_PROBE_SOURCE})
    except HerdrTeamError as err:
        text = "{} {}".format(err.message, json.dumps(err.details, default=str))
        if err.exit_code == 3 or err.code in ("unknown_method", "invalid_params", "herdr_protocol"):
            raise
        if err.code in ("plugin_disabled", "plugin_not_found", "plugin_not_enabled"):
            raise HerdrTeamError("plugin_disabled", "the {} plugin is disabled or not linked; agent views need an enabled plugin source".format(PLUGIN_ID), EXIT_REFUSED, {"herdr_code": err.code})
        if VIEW_SOURCE in text:
            return "own", VIEW_SOURCE
        owner = err.details.get("source") or err.details.get("current_source")
        if owner is None:
            match = re.search(r"source[\s:=]+['\"]?([A-Za-z0-9_.:-]+)", err.message)
            owner = match.group(1) if match else None
        if err.code in ("agent_view_not_set", "view_not_set", "no_agent_view"):
            return "none", None
        return "foreign", str(owner) if owner else None
    if isinstance(result, dict) and result.get("cleared") is False:
        return "none", None
    if isinstance(result, dict) and result.get("source") and result.get("source") != VIEW_PROBE_SOURCE:
        return ("own", VIEW_SOURCE) if result.get("source") == VIEW_SOURCE else ("foreign", str(result.get("source")))
    return "none", None


def _add_view_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("action", choices=("on", "off", "toggle"))
    parser.add_argument("--force", action="store_true")


def _add_doctor_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--no-probe", dest="no_probe", action="store_true", help="skip the one notification.show toast probe")
    parser.add_argument("--no-fix", dest="no_fix", action="store_true", help="report only; do not close or reopen a dead console pane")


def _run_view(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    if not _paths.socket_allowed(layout.config_dir, layout.socket):
        # Plan 4.1 / PK-07: manifest actions are no-ops for a socket the rig has not listed.
        return emit(args, {"skipped": "socket_not_allowed", "socket": os.fspath(layout.socket)}, "view skipped: socket not allowed")
    api = api_for(args, layout)
    previous = view_state(layout.session)
    owner, owner_source = probe_view_owner(api)
    if owner == "foreign" and not args.force:
        raise HerdrTeamError("view_foreign", "the agent view belongs to {}; pass --force to replace it".format(owner_source or "another source"), EXIT_REFUSED, {"owner": owner_source})
    if args.action != "toggle":
        want = args.action
    elif owner == "foreign":
        # M7 UI-06 (rig, 2026-09-05): a foreign source had replaced our view while view.json still
        # said "on", so toggle --force cleared the foreign view instead of replacing it (plan 11:
        # foreign -> refuse unless --force replaces). Our view is not showing; toggle turns it on.
        want = "on"
    else:
        want = "off" if (previous == "on" or owner == "own") else "on"
    teams = layout.session.list_teams()
    label = view_label(teams)
    if want == "on":
        if not teams:
            raise HerdrTeamError("team_not_found", "no team exists in this session; nothing to show", EXIT_REFUSED)
        api.request("agent.view.set", view_request(teams))
        write_view_state(layout, teams)
    else:
        if owner in ("own", "foreign"):
            try:
                api.request("agent.view.clear", {"source": VIEW_SOURCE if owner == "own" else None})
            except HerdrTeamError:
                if owner == "own":
                    raise
        try:
            os.unlink(layout.session.view_json)
        except OSError:
            pass
    payload = {"view": want, "source": VIEW_SOURCE, "label": label, "owner": owner, "previous": previous}
    return emit(args, payload, "team view {} ({})".format(want, label))


def _run_teardown(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    api = api_for(args, layout)
    tokens_cleared = 0
    labels_cleared = 0
    for name in layout.session.list_teams():
        try:
            team = _roster.load_team(layout.team(name))
        except HerdrTeamError:
            continue
        for member in team.agents():
            if not member.pane_id:
                continue
            results = _roster.execute_token_commands(api, _roster.token_commands(member, name, clear=True))
            if results and all(r["ok"] for r in results):
                tokens_cleared += 1
            if _roster.label_pane(api, member.pane_id, None):
                labels_cleared += 1
    view_cleared = False
    if layout.session.view_json.exists():
        try:
            api.request("agent.view.clear", {"source": VIEW_SOURCE})
        except HerdrTeamError:
            pass
        try:
            os.unlink(layout.session.view_json)
            view_cleared = True
        except OSError:
            pass
    console = read_console_json(layout.session)
    if console and not pid_alive(int(console.get("pid") or 0)):
        try:
            os.unlink(layout.session.console_json)
        except OSError:
            pass
    daemon_stopped = False
    status = daemon_status(layout.session)
    if status["alive"]:
        from herdr_team import daemon as _daemon

        try:
            daemon_stopped = bool(_daemon.stop_daemon(layout.session))
        except OSError:
            daemon_stopped = False
    payload = {"tokens_cleared": tokens_cleared, "labels_cleared": labels_cleared, "view_cleared": view_cleared, "daemon_stopped": daemon_stopped}
    return emit(args, payload, "teardown: {} token sets, {} labels cleared, view {}, daemon {}".format(tokens_cleared, labels_cleared, "cleared" if view_cleared else "untouched", "stopped" if daemon_stopped else "not running"))


# --------------------------------------------------------------------------
# delivery: nudge, mute, unmute, pause, focus, read


def _open_delivery(args: argparse.Namespace, require_server: bool = False) -> Tuple[Layout, Any, Author, str, TeamPaths, Dict[str, Any]]:
    layout = layout_for(args)
    api = api_for(args, layout)
    hinted = resolve_team(args, layout, None, required=False)
    author = resolve_author(args, layout, api, team=hinted, require_server=require_server)
    team_name = resolve_team(args, layout, author)
    assert team_name is not None
    team = layout.team(team_name)
    return layout, api, author, team_name, team, load_doc(team)


def _member_or_raise(doc: Dict[str, Any], name: str, team_name: str) -> Dict[str, Any]:
    return _cmd_board.member_or_raise(doc, name, team_name)


def _require_daemon(layout: Layout) -> None:
    status = daemon_status(layout.session)
    if not status["alive"]:
        raise HerdrTeamError("daemon_down", "the team notifier is not running ({}); run: herdr-synapse daemon start".format(status.get("reason")), EXIT_DAEMON_DOWN)


def _add_nudge_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("name")
    parser.add_argument("--force", action="store_true", help="skip done_hold and the per-target interval (never the gate)")


def _run_nudge(args: argparse.Namespace) -> int:
    layout, api, author, team_name, team, doc = _open_delivery(args)
    member = _member_or_raise(doc, args.name, team_name)
    _require_daemon(layout)
    job = enqueue_job(team, "nudge", str(member["name"]), author, force=args.force)
    return emit(args, {"team": team_name, "member": member["name"], "job": job}, "nudge job {} queued for {}".format(job, member["name"]))


def _add_focus_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("name")


def _run_focus(args: argparse.Namespace) -> int:
    layout, api, author, team_name, team, doc = _open_delivery(args)
    member = _member_or_raise(doc, args.name, team_name)
    _require_daemon(layout)
    job = enqueue_job(team, "focus", str(member["name"]), author, extra={"pane_id": member.get("pane_id")})
    return emit(args, {"team": team_name, "member": member["name"], "job": job}, "focus job {} queued for {}".format(job, member["name"]))


_DURATION_RE = re.compile(r"^(\d+)([smhd]?)$")


def parse_duration_s(text: str) -> int:
    match = _DURATION_RE.match(text.strip())
    if not match:
        raise UsageError("--for expects a duration like 10m, 2h, 90s")
    value = int(match.group(1))
    unit = match.group(2) or "m"
    return value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _until_iso(seconds: int) -> str:
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(when.microsecond // 1000)


def _write_mute(team: TeamPaths, mutate: Any) -> Dict[str, Any]:
    with store.team_lock(team):
        doc = mute_state(team)
        mutate(doc)
        store.write_json(team.mute_json, doc)
    return doc


def _mute_payload(team_name: str, doc: Dict[str, Any]) -> Dict[str, Any]:
    muted = {"*": doc.get("*")}
    for key, value in doc.items():
        if key != "*":
            muted[key] = value
    return {"team": team_name, "muted": muted}


def _add_mute_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("name", nargs="?")
    parser.add_argument("--all", dest="all_members", action="store_true")
    parser.add_argument("--for", dest="duration", metavar="DURATION", help="e.g. 10m (default: until unmute)")


# --------------------------------------------------------------------------
# interrupts


def _add_interrupts_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("mode", nargs="?", metavar="show|off|on|<kind>[,<kind>]", help="which kinds a teammate's `post --interrupt` may be typed into mid-turn (default: show; `on` restores the default list)")
    parser.add_argument("--cooldown", metavar="10m", help="one interrupt per sender and target inside this window")


def interrupts_view(doc: Dict[str, Any]) -> Dict[str, Any]:
    """The effective interrupt settings of a roster (``config.gate`` overrides or the gate defaults)."""
    config = doc.get("config") if isinstance(doc.get("config"), dict) else {}
    gate_cfg = config.get("gate") if isinstance(config.get("gate"), dict) else {}
    kinds = gate_cfg.get("interrupt_kinds")
    cooldown = gate_cfg.get("interrupt_cooldown_ms")
    return {
        "kinds": [str(k) for k in kinds] if isinstance(kinds, list) else list(gate.INTERRUPT_KINDS),
        "cooldown_ms": int(cooldown) if isinstance(cooldown, (int, float)) and not isinstance(cooldown, bool) else gate.INTERRUPT_COOLDOWN_MS,
        "default_kinds": list(gate.INTERRUPT_KINDS),
    }


def _interrupts_text(team_name: str, view: Dict[str, Any]) -> str:
    kinds = view["kinds"]
    where = "off" if not kinds else "into working {} members".format(", ".join(kinds))
    return "{}: interrupts {} · one per sender and teammate per {} min".format(team_name, where, max(1, view["cooldown_ms"] // 60000))


#: Defaults for ``config.ask``. Deliberately NOT under ``config.gate``: that
#: namespace is whitelisted, and one unknown key there throws the whole gate
#: config back to its defaults rather than erroring.
ASK_DEFAULTS: Dict[str, Any] = {
    "block": True,
    "block_kinds": list(_asks.ASKING_KINDS),
    #: Under Claude Code's 10-minute cap on a shell command, so a wait ends
    #: with our own error rather than a killed process.
    "timeout_s": 480,
    "popup": True,
}


def ask_view(doc: Dict[str, Any]) -> Dict[str, Any]:
    """The effective human-in-the-loop settings of a roster (``config.ask`` over the defaults)."""
    config = doc.get("config") if isinstance(doc.get("config"), dict) else {}
    ask = config.get("ask") if isinstance(config.get("ask"), dict) else {}
    kinds = ask.get("block_kinds")
    timeout = ask.get("timeout_s")
    return {
        "block": bool(ask["block"]) if isinstance(ask.get("block"), bool) else ASK_DEFAULTS["block"],
        "block_kinds": [str(k) for k in kinds] if isinstance(kinds, list) else list(ASK_DEFAULTS["block_kinds"]),
        "timeout_s": int(timeout) if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0 else ASK_DEFAULTS["timeout_s"],
        "popup": bool(ask["popup"]) if isinstance(ask.get("popup"), bool) else ASK_DEFAULTS["popup"],
        "defaults": dict(ASK_DEFAULTS),
    }


def _ask_text(team_name: str, view: Dict[str, Any]) -> str:
    waiting = "waits for you on {}".format(", ".join(view["block_kinds"])) if view["block"] and view["block_kinds"] else "never waits"
    return "{}: an agent {} (up to {} min) · popup {}".format(
        team_name, waiting, max(1, view["timeout_s"] // 60), "on" if view["popup"] else "off")


def _add_ask_policy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("mode", nargs="?", metavar="show", help="print the current policy (the default)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--block", dest="block", action="store_true", default=None, help="an asking post waits for you")
    group.add_argument("--no-block", dest="block", action="store_false", default=None, help="agents never wait")
    parser.add_argument("--kinds", metavar="question,blocked", help="which post kinds wait ('none' for no kinds)")
    parser.add_argument("--timeout", metavar="8m", help="how long a post waits before giving up")
    popup = parser.add_mutually_exclusive_group()
    popup.add_argument("--popup", dest="popup", action="store_true", default=None, help="raise the popup (default)")
    popup.add_argument("--no-popup", dest="popup", action="store_false", default=None, help="toast only, no popup")


def _run_ask_policy(args: argparse.Namespace) -> int:
    """Show or set what happens when an agent addresses you.

    Same shape as ``interrupts``: the bare command is a read that needs no
    authority, every write is operator-only and audited, and the view is
    re-read from disk so it reports what was actually stored.
    """
    layout, api, author, team_name, team, doc = _open_delivery(args)
    changing = any(getattr(args, k, None) is not None for k in ("block", "popup", "kinds", "timeout"))
    if not changing:
        view = ask_view(doc)
        return emit(args, dict(view, team=team_name, changed=False), _ask_text(team_name, view))

    _charter.require_human(layout, team_name, author, "ask-policy")
    kinds = None
    if args.kinds is not None:
        if str(args.kinds).strip() in ("none", "off", ""):
            kinds = []
        else:
            kinds = [k.strip() for k in str(args.kinds).split(",") if k.strip()]
            for kind in kinds:
                if kind not in _cmd_board.POST_KINDS:
                    raise UsageError("ask-policy --kinds takes post kinds: {}".format(", ".join(_cmd_board.POST_KINDS)))
    timeout_s = parse_duration_s(args.timeout) if args.timeout else None
    if timeout_s is not None and timeout_s <= 0:
        raise UsageError("ask-policy --timeout must be positive")

    def mutate(t: _roster.Team) -> None:
        ask = dict(t.config.get("ask")) if isinstance(t.config.get("ask"), dict) else {}
        if args.block is not None:
            ask["block"] = bool(args.block)
        if args.popup is not None:
            ask["popup"] = bool(args.popup)
        if kinds is not None:
            ask["block_kinds"] = list(kinds)
        if timeout_s is not None:
            ask["timeout_s"] = int(timeout_s)
        t.config["ask"] = ask

    _roster.update_team(team, mutate)
    view = ask_view(load_doc(team))
    _charter.audit(layout, team_name, "ask_policy_set", author, {k: view[k] for k in ("block", "block_kinds", "timeout_s", "popup")})
    return emit(args, dict(view, team=team_name, changed=True), _ask_text(team_name, view))


def _run_interrupts(args: argparse.Namespace) -> int:
    layout, api, author, team_name, team, doc = _open_delivery(args)
    mode = getattr(args, "mode", None)
    cooldown = getattr(args, "cooldown", None)
    if mode in (None, "show") and not cooldown:
        view = interrupts_view(doc)
        return emit(args, dict(view, team=team_name, changed=False), _interrupts_text(team_name, view))
    _charter.require_human(layout, team_name, author, "interrupts")
    kinds: Optional[List[str]] = None
    if mode not in (None, "show"):
        if mode == "off":
            kinds = []
        elif mode in ("on", "default"):
            kinds = list(gate.INTERRUPT_KINDS)
        else:
            kinds = [k.strip() for k in str(mode).split(",") if k.strip()]
            for kind in kinds:
                if not re.match(r"^[a-z][a-z0-9_-]{0,31}\Z", kind):
                    raise UsageError("interrupts: kinds are lowercase agent kinds such as claude or codex, comma separated")
    cooldown_ms = parse_duration_s(cooldown) * 1000 if cooldown else None

    def mutate(t: _roster.Team) -> None:
        gate_cfg = dict(t.config.get("gate")) if isinstance(t.config.get("gate"), dict) else {}
        if kinds is not None:
            gate_cfg["interrupt_kinds"] = list(kinds)
        if cooldown_ms is not None:
            gate_cfg["interrupt_cooldown_ms"] = int(cooldown_ms)
        t.config["gate"] = gate_cfg

    _roster.update_team(team, mutate)
    view = interrupts_view(load_doc(team))
    return emit(args, dict(view, team=team_name, changed=True), _interrupts_text(team_name, view))


def _run_mute(args: argparse.Namespace, force_all: bool = False) -> int:
    layout, api, author, team_name, team, doc = _open_delivery(args)
    all_members = force_all or getattr(args, "all_members", False)
    if not all_members and not getattr(args, "name", None):
        raise UsageError("mute <name> or mute --all")
    until: Optional[str] = _until_iso(parse_duration_s(args.duration)) if getattr(args, "duration", None) else None
    key = "*" if all_members else str(_member_or_raise(doc, args.name, team_name)["name"])

    def mutate(m: Dict[str, Any]) -> None:
        m[key] = until

    mute_doc = _write_mute(team, mutate)
    return emit(args, _mute_payload(team_name, mute_doc), "muted {}{}".format("all members" if key == "*" else key, " until {}".format(until) if until else ""))


def _run_pause(args: argparse.Namespace) -> int:
    args.name = None
    args.all_members = True
    return _run_mute(args, force_all=True)


def _add_unmute_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("name", nargs="?")
    parser.add_argument("--all", dest="all_members", action="store_true")


def _run_unmute(args: argparse.Namespace) -> int:
    layout, api, author, team_name, team, doc = _open_delivery(args)
    if not args.all_members and not args.name:
        raise UsageError("unmute <name> or unmute --all")

    def mutate(m: Dict[str, Any]) -> None:
        if args.all_members:
            m.clear()
        else:
            m.pop(str(args.name), None)

    mute_doc = _write_mute(team, mutate)
    return emit(args, _mute_payload(team_name, mute_doc), "unmuted {}".format("everyone" if args.all_members else args.name))


def _add_pause_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--for", dest="duration", metavar="DURATION")


def _add_read_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("name")
    parser.add_argument("--lines", type=int, metavar="N", help="refused for every member (plan 7.3): scrolling types keys into an idle alternate screen")


def _run_read(args: argparse.Namespace) -> int:
    layout, api, author, team_name, team, doc = _open_delivery(args, require_server=True)
    member = _member_or_raise(doc, args.name, team_name)
    if args.lines is not None:
        # Plan 7.3 "Never --lines on a member": no kind list can tell an alternate screen from a scrollback
        # here, and the daemon is the only process allowed to type into a member.
        raise HerdrTeamError("lines_refused", "--lines is refused for members ({}): scrolling an idle alternate screen types keys into the agent; the visible screen is all `read` shows".format(member.get("kind")), EXIT_REFUSED, {"kind": member.get("kind")})
    pane_id = member.get("pane_id")
    if not pane_id:
        raise HerdrTeamError("member_not_found", "{} has no pane".format(member["name"]), EXIT_REFUSED)
    params: Dict[str, Any] = {"target": pane_id, "source": "visible"}
    result = api.request("agent.read", params)
    # ``agent.read`` answers ``pane_read``: the screen lives in ``read.text`` (schema fixture, protocol 20).
    text = _api.read_text(result)
    lines = str(text or "").split("\n")
    return emit(args, {"team": team_name, "member": member["name"], "pane_id": pane_id, "lines": lines}, "\n".join(lines))


def _add_inbox_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--human", dest="human", action="store_true", help="posts addressed to human plus the notifier's attention file (plan 7.2)")
    parser.add_argument("--last", type=int, default=30, metavar="N")
    parser.add_argument("--since", type=int, metavar="SEQ")


def _read_attention(team: TeamPaths, last: int) -> List[Dict[str, Any]]:
    raw = store.read_bytes(team.human_attention, b"") or b""
    out: List[Dict[str, Any]] = []
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out[-max(0, last):] if last > 0 else []


def _run_inbox(args: argparse.Namespace) -> int:
    """``inbox --human``: what the human has not seen: posts to ``human`` and the mirrored toasts."""
    layout, api, author, team_name, team, doc = _open_delivery(args)
    from herdr_team.cmd_board import board_read_all, cursor_get, reader_id

    records = board_read_all(team)
    reader = reader_id(author) if author.is_human else "human@human"
    cursor = int(cursor_get(team, reader).get("seq", 0)) if reader else 0
    posts = [r for r in records if "human" in (r.get("to") or []) and r.get("from") != "human"]
    if args.since is not None:
        posts = [r for r in posts if r["seq"] > args.since]
    posts = posts[-max(0, args.last):] if args.last > 0 else []
    attention = _read_attention(team, args.last)
    payload = {"team": team_name, "reader": reader, "cursor": cursor, "unread": sum(1 for r in posts if r["seq"] > cursor), "posts": posts, "attention": attention}

    def human() -> str:
        from herdr_team.cmd_board import render_board

        lines = [render_board(posts) if posts else "no posts to human"]
        if attention:
            lines.append("-- attention ({}):".format(len(attention)))
            for entry in attention:
                lines.append("  {} {} [{}] {}".format(entry.get("ts"), entry.get("kind"), entry.get("reason"), entry.get("title")))
        return "\n".join(lines)

    return emit(args, payload, human)


def _no_arguments(parser: argparse.ArgumentParser) -> None:
    pass


def _add_kinds_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("action", choices=("list", "trust", "untrust"), help="list kinds.json, or set/clear the owner trust override for a kind")
    parser.add_argument("kind", nargs="?", help="agent kind label (claude, codex, ...) for trust/untrust")
    parser.add_argument("--reason", default="owner override", help="free text recorded with the trust entry")


def _kinds_row(kind: str, entry: Any) -> Dict[str, Any]:
    entry = entry if isinstance(entry, dict) else {}
    probe = entry.get("probe") if isinstance(entry.get("probe"), dict) else {}
    return {
        "kind": kind,
        "delivers": _roster.kind_entry_trusted(entry),
        "trusted": bool(entry.get("trusted")),
        "verified": bool(entry.get("verified")),
        "probe_ok": bool(probe.get("ok")),
        "multiline": entry.get("multiline") if isinstance(entry.get("multiline"), dict) else None,
        "trusted_at": entry.get("trusted_at"),
        "reason": entry.get("reason"),
    }


def _run_kinds(args: argparse.Namespace) -> int:
    """``kinds list|trust <kind>|untrust <kind>``: the owner override behind gate 4 (plan 8.2).

    A fresh session has no ``kinds.json`` and delivers nothing until a kind is verified by the
    ledger, probed with ``hooks probe``, or trusted here (observed live 2026-09-05: every
    delivery ``held: kind_unverified`` until the override existed). ``trust`` is the human
    saying "this kind has been verified elsewhere"; it is recorded with a reason and a time.
    """
    layout = layout_for(args)
    path = layout.session.kinds_json
    doc = store.read_json(path, default=None)
    if not isinstance(doc, dict):
        doc = {}
    if args.action == "list":
        rows = [_kinds_row(kind, doc.get(kind)) for kind in sorted(doc)]
        lines = ["{:<12} {:<9} {}".format(r["kind"], "delivers" if r["delivers"] else "held", ", ".join(k for k in ("trusted", "verified", "probe_ok") if r[k]) or "-") for r in rows]
        return emit(args, {"kinds": rows, "path": os.fspath(path)}, "\n".join(lines) if lines else "no kinds recorded yet: run `herdr-synapse kinds trust <kind>` for the kinds you have verified")
    kind = (args.kind or "").strip().lower()
    if not kind:
        raise UsageError("kinds {} needs a kind label".format(args.action))
    if kind not in _roster.KIND_LABELS:
        raise HerdrTeamError("kind_unknown", "{!r} is not an agent kind the installed Herdr can start".format(kind), EXIT_REFUSED, {"kind": kind, "kinds": sorted(_roster.KIND_LABELS)})
    entry = doc.get(kind) if isinstance(doc.get(kind), dict) else {}
    if args.action == "trust":
        entry["trusted"] = True
        entry["trusted_at"] = _roster.now_iso()
        entry["trusted_by"] = "owner"
        entry["reason"] = args.reason
        human = "{}: deliveries enabled (owner override)".format(kind)
    else:
        for key in ("trusted", "trusted_at", "trusted_by", "reason"):
            entry.pop(key, None)
        human = "{}: owner override removed{}".format(kind, "" if _roster.kind_entry_trusted(entry) else "; deliveries held until verified or probed")
    doc[kind] = entry
    os.makedirs(os.path.dirname(os.fspath(path)), mode=0o700, exist_ok=True)
    store.write_json(path, doc)
    return emit(args, {"kind": kind, "action": args.action, "row": _kinds_row(kind, entry), "path": os.fspath(path)}, human)


COMMANDS: List[Command] = [
    Command("doctor", "diagnose sockets, state dirs, plugin state, daemon, and config", _add_doctor_arguments, _run_doctor),
    Command("inbox", "posts addressed to the human plus the notifier's attention file (--human)", _add_inbox_arguments, _run_inbox),
    Command("setup", "print the config blocks to paste (--print-config); never edits config", _add_setup_arguments, _run_setup),
    Command("keys", "print the key-binding snippet or check it against your config", _add_keys_arguments, _run_keys),
    Command("install-cli", "symlink ~/.local/bin/herdr-synapse to this plugin (--yes)", _add_install_cli_arguments, _run_install_cli),
    Command("ask-policy", "show or set what happens when an agent addresses you", _add_ask_policy_arguments, _run_ask_policy),
    Command("interrupts", "show or set which kinds a teammate's post --interrupt may reach mid-turn (human only to change)", _add_interrupts_arguments, _run_interrupts),
    Command("gc", "remove session trees whose socket is gone, lock free, older than 7 days", _no_arguments, _run_gc),
    Command("prune", "archive old board segments into _archive", _add_prune_arguments, _run_prune),
    Command("view", "turn the team Agents view on, off, or toggle it", _add_view_arguments, _run_view),
    Command("kinds", "list kinds.json or set/clear the owner trust override that lets a kind receive nudges", _add_kinds_arguments, _run_kinds),
    Command("teardown", "dead-daemon cleanup: clear tokens, labels, view, stale console record", _no_arguments, _run_teardown),
    Command("nudge", "ask the notifier to evaluate a nudge for a member now", _add_nudge_arguments, _run_nudge),
    Command("mute", "silence nudges and toasts for a member or everyone", _add_mute_arguments, _run_mute),
    Command("unmute", "lift a mute", _add_unmute_arguments, _run_unmute),
    Command("pause", "alias for mute --all", _add_pause_arguments, _run_pause),
    Command("focus", "ask the notifier to focus a member's pane", _add_focus_arguments, _run_focus),
    Command("read", "read a member's visible screen (never --lines on full-screen kinds)", _add_read_arguments, _run_read),
]
