"""The visual layer's switches, and what a harness is given at launch to reach the canvas (0.21).

Three switches, safest default first (``.local/prd/canvas-contracts.md``
section 3):

* **The layer**, per Herdr session: ``<session>/features.json``
  ``{"v": 1, "whiteboard": {"enabled": bool, "by", "via", "at"}}``. Off by
  default; only the operator in person flips it.
* **A team's canvas**: ``team.json`` ``config.whiteboard.enabled``. On by
  default, so enabling the layer turns every team on; the operator or a
  delegate switches one team off.
* **A team's live visuals** (``viz``: agent HTML and JavaScript in a sealed
  frame): ``config.whiteboard.viz``. On by default whenever the team's canvas
  is on; same authority as the team switch.

Off means off: every canvas command and MCP tool refuses with
``whiteboard_off`` (a ``viz`` operation with ``viz_off``) and changes
nothing. Stored canvases and watch flags are kept, so switching back on
brings them back.

This module also owns the launch-time MCP contract: ``mcp_spec`` says which
server a member Synapse starts is given (``None`` while its team's canvas is
off), ``mcp_launch_args`` turns that into the harness's own flags, and
``is_preserved_mcp_value`` is the pure predicate ``models.preserved_launch_args``
uses so a controlled restart carries exactly those flags and nothing else.
Nothing here starts a process or talks to Herdr; the only writes are
``features.json``, ``team.json`` (through ``store.RosterStore``), the team's
``whiteboard/mcp.json``, and the ``whiteboard_state`` board record.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from herdr_team import store
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError
from herdr_team.paths import SessionPaths, TeamPaths, ensure_dir, plugin_root

FEATURES_SCHEMA = 1
FEATURES_FILE = "features.json"
#: The key of the layer switch in ``features.json`` and of the team switches in ``team.json`` ``config``.
LAYER = "whiteboard"
#: ``<team>/whiteboard/``: everything the canvas stores for one team.
WHITEBOARD_DIR = "whiteboard"
#: The skill reference served only while the caller's team canvas is on.
SKILL_REFERENCE = "canvas"
CLI = "herdr-synapse"

LEVEL_LAYER = "layer"
LEVEL_TEAM = "team"

#: The MCP server name every harness registers. Codex config keys allow
#: ``[A-Za-z0-9_-]``, and Claude Code prefixes tools ``mcp__<server>__``.
MCP_SERVER_NAME = "synapse_canvas"
MCP_CONFIG_FILE = "mcp.json"
#: Kinds with a launch flag that adds an MCP server without editing any global config.
#: Pi is not one: it has no ``--mcp-config`` (0.85.1 exits on any unknown flag no
#: extension registered), so Pi members reach the canvas through the CLI.
MCP_KINDS = ("claude", "codex")
#: The flags ``mcp_launch_args`` emits per kind; ``models.preserved_launch_args``
#: keeps these (arity 1) only when ``is_preserved_mcp_value`` accepts the value.
PRESERVED_MCP_FLAGS: Dict[str, tuple] = {"claude": ("--mcp-config",), "codex": ("-c",)}
CODEX_OVERRIDE_PREFIX = "mcp_servers.{}.".format(MCP_SERVER_NAME)
CODEX_OVERRIDE_KEYS = ("command", "args")
_MAX_VALUE_CHARS = 4096


# --------------------------------------------------------------------------
# paths


def features_path(session: SessionPaths) -> Path:
    """``<session>/features.json``: the session-wide layer switch."""
    return session.root / FEATURES_FILE


def whiteboard_dir(team: TeamPaths) -> Path:
    """``<team>/whiteboard/``: events, scene, assets, cursors, renders, ``mcp.json``."""
    return team.root / WHITEBOARD_DIR


def mcp_config_path(team: TeamPaths) -> Path:
    """``<team>/whiteboard/mcp.json``: the file Claude Code is given with ``--mcp-config``."""
    return whiteboard_dir(team) / MCP_CONFIG_FILE


# --------------------------------------------------------------------------
# the layer switch (session)


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def load(session: SessionPaths) -> Dict[str, Any]:
    """``features.json`` normalised: the layer is off unless the file says ``enabled: true``."""
    raw = store.read_json(features_path(session), default=None)
    doc: Dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    layer = doc.get(LAYER) if isinstance(doc.get(LAYER), dict) else {}
    doc["v"] = FEATURES_SCHEMA
    doc[LAYER] = {
        "enabled": layer.get("enabled") is True,
        "by": _text(layer.get("by")), "via": _text(layer.get("via")), "at": _text(layer.get("at")),
    }
    return doc


def layer_enabled(session: SessionPaths) -> bool:
    """Whether the whiteboard layer is on for this Herdr session (default off)."""
    return bool(load(session)[LAYER]["enabled"])


def set_layer(session: SessionPaths, enabled: bool, by: str, via: str) -> Dict[str, Any]:
    """Flip the session layer; ``{"changed", "enabled", "previous", "by", "via", "at"}``. The caller checks authority."""
    doc = load(session)
    previous = bool(doc[LAYER]["enabled"])
    enabled = bool(enabled)
    if previous == enabled:
        return dict(doc[LAYER], changed=False, previous=previous)
    doc[LAYER] = {"enabled": enabled, "by": by, "via": via, "at": store.now_iso()}
    store.write_json(features_path(session), doc)
    return dict(doc[LAYER], changed=True, previous=previous)


def require_layer(session: SessionPaths) -> None:
    """Raise ``whiteboard_off`` (scope ``session``) unless the layer is on."""
    if not layer_enabled(session):
        raise HerdrTeamError(
            "whiteboard_off",
            "the whiteboard is off for this Herdr session; the operator turns it on with: {} whiteboard enable".format(CLI),
            EXIT_REFUSED, {"scope": "session"},
        )


# --------------------------------------------------------------------------
# team switches (team.json config.whiteboard)


def team_settings(doc: Any) -> Dict[str, Any]:
    """``config.whiteboard`` of a ``team.json`` document with defaults: canvas on, viz on."""
    config = doc.get("config") if isinstance(doc, dict) and isinstance(doc.get("config"), dict) else {}
    raw = config.get(LAYER) if isinstance(config.get(LAYER), dict) else {}
    return {
        "enabled": raw.get("enabled") is not False,
        "viz": raw.get("viz") is not False,
        "by": _text(raw.get("by")), "via": _text(raw.get("via")), "at": _text(raw.get("at")),
    }


@dataclass(frozen=True)
class TeamSwitch:
    """The effective state of one team's canvas: the session layer AND the team's own switches."""

    team: str
    layer: bool
    enabled: bool
    viz_enabled: bool

    @property
    def on(self) -> bool:
        """Canvas commands, MCP tools, the page and the skill reference are available."""
        return self.layer and self.enabled

    @property
    def viz(self) -> bool:
        """``viz`` operations are accepted and live frames run on the page."""
        return self.on and self.viz_enabled

    def to_json(self) -> Dict[str, Any]:
        return {"team": self.team, "layer": self.layer, "enabled": self.enabled, "viz_enabled": self.viz_enabled,
                "on": self.on, "viz": self.viz}


def _load_doc(team: TeamPaths, doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return doc if isinstance(doc, dict) else store.RosterStore(team).load()


def team_switch(session: SessionPaths, team: TeamPaths, doc: Optional[Dict[str, Any]] = None) -> TeamSwitch:
    """The team's effective switch; ``doc`` is its ``team.json`` when the caller already loaded it (else ``team_not_found``)."""
    settings = team_settings(_load_doc(team, doc))
    return TeamSwitch(team.name, layer_enabled(session), bool(settings["enabled"]), bool(settings["viz"]))


def set_team(team: TeamPaths, enabled: Optional[bool] = None, viz: Optional[bool] = None, by: str = "human", via: str = "cli") -> Dict[str, Any]:
    """Set the team's canvas and/or viz switch; ``{"changed": [keys], "before": settings, "after": settings}``.

    ``None`` leaves a switch alone. Nothing is written when nothing changes.
    The caller checks authority (``check_authority(author, LEVEL_TEAM)``).
    """
    before = team_settings(store.RosterStore(team).load())
    wanted = {"enabled": enabled, "viz": viz}
    if all(value is None or bool(value) == before[key] for key, value in wanted.items()):
        return {"changed": [], "before": before, "after": before}
    changed: List[str] = []

    def mutate(doc: Dict[str, Any]) -> None:
        del changed[:]
        config = doc.get("config") if isinstance(doc.get("config"), dict) else {}
        current = team_settings(doc)
        entry = dict(config.get(LAYER)) if isinstance(config.get(LAYER), dict) else {}
        for key, value in wanted.items():
            if value is not None and bool(value) != current[key]:
                entry[key] = bool(value)
                changed.append(key)
        if changed:
            entry.update(by=by, via=via, at=store.now_iso())
            config[LAYER] = entry
            doc["config"] = config

    saved = store.RosterStore(team).update(mutate)
    return {"changed": list(changed), "before": before, "after": team_settings(saved)}


def require_on(session: SessionPaths, team: TeamPaths, doc: Optional[Dict[str, Any]] = None) -> TeamSwitch:
    """The team's switch, or ``whiteboard_off`` (details ``scope``: ``session`` or ``team``)."""
    switch = team_switch(session, team, doc)
    if not switch.layer:
        raise HerdrTeamError(
            "whiteboard_off",
            "the whiteboard is off for this Herdr session; the operator turns it on with: {} whiteboard enable".format(CLI),
            EXIT_REFUSED, {"scope": "session", "team": team.name},
        )
    if not switch.enabled:
        raise HerdrTeamError(
            "whiteboard_off",
            "the whiteboard is off for team {}; the operator turns it on with: {} --team {} whiteboard team on".format(team.name, CLI, team.name),
            EXIT_REFUSED, {"scope": "team", "team": team.name},
        )
    return switch


def require_viz(session: SessionPaths, team: TeamPaths, doc: Optional[Dict[str, Any]] = None) -> TeamSwitch:
    """``require_on``, then ``viz_off`` unless the team's live visuals are on."""
    switch = require_on(session, team, doc)
    if not switch.viz:
        raise HerdrTeamError(
            "viz_off",
            "live visuals (viz) are off for team {}; draw with shapes, svg, graph or chart instead, or ask the operator: {} --team {} whiteboard viz on".format(team.name, CLI, team.name),
            EXIT_REFUSED, {"scope": "viz", "team": team.name},
        )
    return switch


def status(session: SessionPaths, teams: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """``{"session": {...layer}, "teams": {name: {"enabled", "viz", "on", "viz_on"}}}``; unreadable teams are skipped."""
    layer = load(session)[LAYER]
    names = sorted(session.list_teams()) if teams is None else [str(name) for name in teams]
    out: Dict[str, Dict[str, Any]] = {}
    for name in names:
        try:
            settings = team_settings(store.RosterStore(session.team(name)).load())
        except (HerdrTeamError, OSError, ValueError):
            continue
        on = bool(layer["enabled"] and settings["enabled"])
        out[name] = {"enabled": settings["enabled"], "viz": settings["viz"], "on": on, "viz_on": bool(on and settings["viz"]),
                     "by": settings["by"], "at": settings["at"]}
    return {"session": dict(layer), "teams": out}


def me_line(switch: TeamSwitch, version: Optional[int] = None) -> str:
    """The one line ``me``, ``orient`` and the briefing show about the whiteboard."""
    if not switch.layer:
        return "whiteboard: off"
    if not switch.enabled:
        return "whiteboard: off for this team"
    parts = ["whiteboard: on", "live visuals {}".format("on" if switch.viz else "off")]
    if version is not None:
        parts.append("canvas v{}".format(int(version)))
    return "{}; how to draw: {} skill get --reference {}".format(" · ".join(parts), CLI, SKILL_REFERENCE)


# --------------------------------------------------------------------------
# authority


def check_authority(author: Any, level: str, layout: Any = None, team_name: Optional[str] = None) -> str:
    """``"operator"`` or ``"delegate"``, else ``author_mismatch`` (audited when ``layout`` is given).

    ``LEVEL_LAYER`` (the session switch, purge) is the operator in person:
    a trusted human origin. ``LEVEL_TEAM`` (a team's canvas and viz
    switches, clear) also accepts a verified member holding a live operator
    delegation.
    """
    from herdr_team import identity as _identity

    action = "whiteboard {}".format("enable/disable" if level == LEVEL_LAYER else "team switches")
    if getattr(author, "trusted_human", False):
        return "operator"
    if level == LEVEL_TEAM and getattr(author, "operator", False) and getattr(author, "is_member", False) and getattr(author, "verified", False):
        if layout is not None:
            _identity.audit(layout, team_name, "operator_action", author, {"action": action})
        return "delegate"
    if layout is not None:
        _identity.audit(layout, team_name, "author_mismatch", author, {"action": action})
    if getattr(author, "is_human", False):
        message = _identity.authority_refusal(action, author)
    elif level == LEVEL_LAYER:
        message = "{} is for the operator in person; {} is not".format(action, getattr(author, "name", "?"))
    else:
        message = "{} is for the operator or a member the operator delegated to; {} is neither, so post a request to the operator".format(
            action, getattr(author, "name", "?"))
    raise HerdrTeamError("author_mismatch", message, EXIT_REFUSED,
                         {"action": action, "author": getattr(author, "name", None), "via": getattr(author, "via", None)})


# --------------------------------------------------------------------------
# telling the team (whiteboard_state, awareness)


def state_notice(before: TeamSwitch, after: TeamSwitch) -> Optional[str]:
    """The ``whiteboard_state`` text for a change of one team's effective switch, or None when nothing agents see changed."""
    team = after.team
    if after.on and not before.on:
        return ("the whiteboard is on for team {}: members can draw on the shared canvas and read it with {} canvas look; "
                "how: {} skill get --reference {}. Live visuals (viz) are {}.").format(team, CLI, CLI, SKILL_REFERENCE, "on" if after.viz else "off")
    if before.on and not after.on:
        return "the whiteboard is off for team {}: canvas commands are refused until the operator turns it back on; nothing drawn is lost".format(team)
    if after.on and before.viz != after.viz:
        return "live visuals (viz) are now {} for team {}".format("on" if after.viz else "off", team)
    return None


def announce(layout: Any, team: TeamPaths, before: TeamSwitch, after: TeamSwitch, by: str) -> Optional[int]:
    """Append the ``whiteboard_state`` record for this change to the team's board; its seq, or None (nothing to say, or the board refused)."""
    text = state_notice(before, after)
    if text is None:
        return None
    from herdr_team import roster as _roster

    try:
        return _roster.append_system_record(
            team, "whiteboard_state", text, to=["all"],
            extra={"whiteboard": {"before": before.to_json(), "after": after.to_json(), "by": by}},
            socket=os.fspath(layout.socket),
        )
    except HerdrTeamError:
        return None


def announce_layer(layout: Any, previous: bool, enabled: bool, by: str) -> Dict[str, int]:
    """After the session layer flipped: one ``whiteboard_state`` record per team whose effective switch changed; ``{team: seq}``."""
    out: Dict[str, int] = {}
    if bool(previous) == bool(enabled):
        return out
    for name in layout.session.list_teams():
        team = layout.session.team(name)
        try:
            settings = team_settings(store.RosterStore(team).load())
        except (HerdrTeamError, OSError, ValueError):
            continue
        before = TeamSwitch(name, bool(previous), settings["enabled"], settings["viz"])
        after = TeamSwitch(name, bool(enabled), settings["enabled"], settings["viz"])
        seq = announce(layout, team, before, after, by)
        if seq is not None:
            out[name] = seq
    return out


# --------------------------------------------------------------------------
# launch-time MCP injection


def mcp_command(layout: Any, team: TeamPaths) -> Dict[str, Any]:
    """The stdio MCP server for one team: this checkout's launcher, pinned to the session socket and the team dir."""
    return {
        "server": MCP_SERVER_NAME,
        "command": os.path.abspath(os.fspath(plugin_root() / "bin" / CLI)),
        "args": ["--socket", os.path.abspath(os.fspath(layout.socket)), "--team", os.path.abspath(os.fspath(team.root)), "canvas", "mcp"],
    }


def mcp_config_document(spec: Dict[str, Any]) -> Dict[str, Any]:
    """The ``--mcp-config`` file body (the ``mcpServers`` shape Claude Code reads)."""
    return {"mcpServers": {MCP_SERVER_NAME: {"type": "stdio", "command": spec["command"], "args": list(spec["args"])}}}


def write_mcp_config(team: TeamPaths, spec: Dict[str, Any]) -> Path:
    """Write ``whiteboard/mcp.json`` (0600) when its content differs; returns the path."""
    path = mcp_config_path(team)
    ensure_dir(path.parent)
    body = mcp_config_document(spec)
    if store.read_json(path, default=None) != body:
        store.write_json(path, body)
    return path


def mcp_spec(layout: Any, team: TeamPaths, doc: Optional[Dict[str, Any]] = None, write: bool = True) -> Optional[Dict[str, Any]]:
    """What a member Synapse starts now is given to reach the canvas, or None while the team's canvas is off.

    ``{"server", "command", "args", "config_file"}``, identical for every
    member of the team and on every launch path, so a spawn, a resume, a
    restore and a swap build the same flags. ``write`` refreshes
    ``whiteboard/mcp.json``. Never raises: a member is started without the
    tools rather than not started.
    """
    try:
        if not team_switch(layout.session, team, doc).on:
            return None
        spec = dict(mcp_command(layout, team), config_file=os.path.abspath(os.fspath(mcp_config_path(team))))
        if write:
            write_mcp_config(team, spec)
        return spec
    except (HerdrTeamError, OSError, ValueError, KeyError):
        return None


def mcp_launch_args(kind: Any, spec: Optional[Dict[str, Any]]) -> List[str]:
    """The harness flags that add the canvas MCP server at launch; ``[]`` without a spec or for a kind with no such flag.

    Claude Code reads the server from ``whiteboard/mcp.json``; Codex gets
    two ``-c`` config overrides whose values are TOML (JSON strings and
    arrays are valid TOML), so its own config files are never edited. Every
    other kind, Pi included, has no such flag and uses the CLI.
    """
    key = str(kind or "").strip()
    if not spec or key not in MCP_KINDS:
        return []
    if key == "claude":
        return ["--mcp-config", str(spec["config_file"])]
    return [
        "-c", "{}command={}".format(CODEX_OVERRIDE_PREFIX, json.dumps(str(spec["command"]))),
        "-c", "{}args={}".format(CODEX_OVERRIDE_PREFIX, json.dumps([str(a) for a in spec["args"]], separators=(",", ":"))),
    ]


def is_preserved_mcp_value(kind: Any, flag: str, value: Any) -> bool:
    """Whether a live ``flag value`` pair is Synapse's own canvas injection, so a controlled restart may carry it.

    Pure string checks: ``--mcp-config`` must name an absolute
    ``.../whiteboard/mcp.json``; a Codex ``-c`` must set
    ``mcp_servers.synapse_canvas.command`` or ``.args``. Anything else a user
    passed with the same flag (an inline JSON config that may hold secrets,
    another server, an unrelated override) is not preserved.
    """
    key = str(kind or "").strip()
    if flag not in PRESERVED_MCP_FLAGS.get(key, ()) or not isinstance(value, str) or not value:
        return False
    if len(value) > _MAX_VALUE_CHARS or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return False
    if flag == "--mcp-config":
        path = Path(value)
        return path.is_absolute() and path.name == MCP_CONFIG_FILE and path.parent.name == WHITEBOARD_DIR
    if not value.startswith(CODEX_OVERRIDE_PREFIX) or "=" not in value:
        return False
    name = value[len(CODEX_OVERRIDE_PREFIX):].split("=", 1)[0]
    return name in CODEX_OVERRIDE_KEYS
