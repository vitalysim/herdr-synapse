"""The whiteboard from the teams view (``prefix+t``, 0.21): ``d`` on any row.

Everything the whiteboard asks of a person, without leaving the tree where
every agent is listed: turn the layer on or off for this Herdr session, open
the page, watch an agent, give an agent that is in no team a canvas of its own
(a team of one), and switch a team's canvas or live visuals. ``o`` on an agent
while the layer is off offers to turn it on and watch in one step.

Every choice becomes one ``whiteboard_steps`` intent: the ``herdr-synapse``
commands a person would type, in order, which ``picker.execute_whiteboard``
runs while the popup stays open. A choice that needs the layer while it is off
runs ``whiteboard enable`` first and says so in its label; the popup is the
operator in person, like the console, so the CLI's own authority checks apply
unchanged. Turning the layer off asks first.

State and rendering only, over ``tui_model.PickerModel``; ``picker`` runs the
steps.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from herdr_team import roster
from herdr_team import tui_model as tm
from herdr_team.paths import MAX_TEAM_CHARS
from herdr_team.tui_model import Intent, PickerModel, PickerNode

MENU_STAGE = "whiteboard"
SOLO_STAGES = ("solo_name", "solo_mission")
STAGES = (MENU_STAGE,) + SOLO_STAGES
INTENT = "whiteboard_steps"

ENABLE = ["whiteboard", "enable"]
DISABLE = ["whiteboard", "disable"]
OPEN = ["whiteboard", "open"]
FIRST = " (turns the whiteboard on first)"

#: What a team of one is told: keep working, and use the canvas when asked.
SOLO_MISSION = "Keep doing your current work; use the team canvas when the operator asks you to draw or to look."
SOLO_SUFFIX = "-canvas"
#: The picker trims a typed team name to this many characters (``normalize_team_name``), so a default
#: longer than that would lose its suffix on Enter.
PICKER_TEAM_CHARS = len(tm.normalize_team_name("a" * (MAX_TEAM_CHARS + 1)))


# --------------------------------------------------------------------------
# the row the menu is about


def target_of(model: PickerModel, node: Optional[PickerNode]) -> Dict[str, Any]:
    """What ``d`` was pressed on: an agent in no team, a member, a team, or nothing in particular."""
    if node is None or node.kind not in ("agent", "member", "team"):
        return {"kind": "none", "team": "", "label": "", "key": node.key if node is not None else ""}
    row = node.row
    target: Dict[str, Any] = {
        "kind": node.kind,
        "team": node.team if node.kind in ("member", "team") else "",
        "label": node.label if node.kind != "team" else node.team,
        "key": node.key,
        "pane_id": row.pane_id if row is not None else "",
        "terminal_id": tm.node_terminal(node) if node.kind != "team" else "",
        "agent_kind": (row.kind if row is not None else None) or str((node.member or {}).get("kind") or ""),
        "name": (row.name if row is not None else None) or "",
        "running": row is not None and not row.launch_pending and bool(row.terminal_id),
    }
    return target


def layer_on(model: PickerModel) -> bool:
    return model.watch_layer is True


def _first(model: PickerModel) -> str:
    return "" if layer_on(model) else FIRST


def team_switches(model: PickerModel, team: str) -> Tuple[bool, bool]:
    """``(canvas, live visuals)``: the team's own switches (a canvas is off until turned on for the team; viz is on)."""
    row = model.wb_teams.get(team) or {}
    return bool(row.get("canvas", False)), bool(row.get("viz", True))


def options(model: PickerModel) -> List[Tuple[str, str]]:
    """The menu for ``model.wb_target``: the row's own choices first, then the page and the session switch."""
    target = model.wb_target or {}
    kind = target.get("kind")
    label = str(target.get("label") or "")
    out: List[Tuple[str, str]] = []
    if kind in ("agent", "member"):
        terminal = str(target.get("terminal_id") or "")
        if terminal and terminal in model.watched:
            out.append(("unwatch", "Stop watching {}".format(label)))
        elif target.get("running"):
            out.append(("watch", "Watch {}: its sidebar row and the page show what it is doing{}".format(label, _first(model))))
    if kind == "agent" and target.get("running"):
        out.append(("solo", "Give {} a canvas of its own: a team of one{}".format(label, _first(model))))
    kind_label = str(target.get("agent_kind") or "")
    if kind == "member" and untrusted(model, kind_label):
        out.append(("trust", "Trust {} so the notifier can type to {}: its briefing and the canvas news are held until then".format(kind_label, label)))
    team = str(target.get("team") or "")
    if team:
        canvas, viz = team_switches(model, team)
        out.append(("team_canvas", "Turn off the canvas for team {}".format(team) if canvas
                    else "Turn on the canvas for team {} (only this team){}".format(team, _first(model))))
        if canvas:
            out.append(("team_viz", "{} live visuals for team {}".format("Turn off" if viz else "Turn on", team)))
    out.append(("open", "Open the whiteboard page in your browser{}".format(_first(model))))
    if layer_on(model):
        out.append(("layer_off", "Turn the whiteboard off for this session (nothing drawn is lost)"))
    else:
        out.append(("layer_on", "Turn the whiteboard on for this session (watch and the page; each team's canvas is turned on by itself)"))
    return out


# --------------------------------------------------------------------------
# keys


def untrusted(model: PickerModel, kind: str) -> bool:
    """A kind the notifier types nothing into in this session (``kinds.json``); False when unknown."""
    return bool(kind) and model.trusted_kinds is not None and kind not in model.trusted_kinds


def trust_step(kind: str, why: str) -> List[str]:
    return ["kinds", "trust", kind, "--reason", why]


def open_menu(model: PickerModel, node: Optional[PickerNode]) -> None:
    """``d`` in the tree."""
    model.wb_target = target_of(model, node)
    model.wb_index = 0
    model.stage = MENU_STAGE
    model.status = None
    model.error = None
    model.pending_action = None
    model.pending_decline = None


def steps_intent(model: PickerModel, steps: List[List[str]], done: str, **extra: Any) -> Intent:
    """One ``whiteboard_steps`` intent; ``enable`` goes first when a step needs the layer and it is off."""
    target = model.wb_target or {}
    args: Dict[str, Any] = {"steps": steps, "done": done, "member": target.get("label") or "", "team": target.get("team") or "",
                            "focus": target.get("key") or ""}
    args.update(extra)
    return Intent(INTENT, args)


def _needs_layer(model: PickerModel, steps: List[List[str]]) -> List[List[str]]:
    return steps if layer_on(model) else [list(ENABLE)] + steps


def watch_intent(model: PickerModel, watch: bool) -> Intent:
    target = model.wb_target or {}
    label = str(target.get("label") or "")
    pane, terminal = str(target.get("pane_id") or ""), str(target.get("terminal_id") or "")
    if watch:
        return steps_intent(model, _needs_layer(model, [["watch", pane or terminal]]),
                            "watching {}: its sidebar row and the page's Activity tab show what it is doing".format(label),
                            check_pane=pane, terminal_id=terminal)
    return steps_intent(model, [["unwatch", terminal or pane]], "stopped watching {}".format(label))


def start(model: PickerModel, choice: str) -> Optional[Intent]:
    target = model.wb_target or {}
    team = str(target.get("team") or "")
    if choice == "watch":
        return watch_intent(model, True)
    if choice == "unwatch":
        return watch_intent(model, False)
    if choice == "trust":
        kind = str(target.get("agent_kind") or "")
        label = str(target.get("label") or "")
        return steps_intent(model, [trust_step(kind, "trusted from the teams view for {}".format(label))],
                            "{} is trusted: the notifier can now type {}'s briefing and news once it is idle".format(kind, label))
    if choice == "solo":
        model.stage = "solo_name"
        model.error = None
        tm._set_input(model, model.solo_team or default_solo_team(model))
        return None
    if choice == "team_canvas":
        canvas, _viz = team_switches(model, team)
        if canvas:
            return steps_intent(model, [["--team", team, "whiteboard", "team", "off"]], "the canvas is off for team {}".format(team))
        return steps_intent(model, _needs_layer(model, [["--team", team, "whiteboard", "team", "on"]]),
                            "the canvas is on for team {} (only this team); its members are told at their next idle".format(team))
    if choice == "team_viz":
        _canvas, viz = team_switches(model, team)
        value = "off" if viz else "on"
        return steps_intent(model, [["--team", team, "whiteboard", "viz", value]], "live visuals are {} for team {}".format(value, team))
    if choice == "open":
        return steps_intent(model, _needs_layer(model, [list(OPEN)]), "opened the whiteboard page")
    if choice == "layer_on":
        return steps_intent(model, [list(ENABLE)], "the whiteboard is on: watch any agent; turn on a team's canvas with d on its row")
    if choice == "layer_off":
        intent = steps_intent(model, [list(DISABLE)], "the whiteboard is off; nothing drawn was deleted")
        model.pending_action = intent
        model.pending_decline = None
        model.status = "Turn the whiteboard off? The page closes and watching stops; nothing drawn is lost - y turns it off, n cancels"
        model.stage = "select"
        return None
    return None


def menu_key(model: PickerModel, key: str) -> Optional[Intent]:
    rows = options(model)
    model.error = None
    if key in ("PASTE_START", "PASTE_END"):
        model.paste_mode = key == "PASTE_START"
        return None
    if model.paste_mode:
        return None
    if key in ("ESC", "q", "d"):
        model.stage = "select"
        model.status = None
        return None
    if key in ("UP", "k"):
        model.wb_index = max(0, model.wb_index - 1)
        return None
    if key in ("DOWN", "j"):
        model.wb_index = min(len(rows) - 1, model.wb_index + 1)
        return None
    if len(key) == 1 and key.isdigit():
        number = int(key)
        if 1 <= number <= len(rows):
            model.wb_index = number - 1
            return start(model, rows[number - 1][0])
        model.error = "type a number between 1 and {}".format(len(rows))
        return None
    if key == "ENTER":
        return start(model, rows[min(model.wb_index, len(rows) - 1)][0])
    return None


def watch_offer(model: PickerModel, node: PickerNode) -> Optional[Intent]:
    """``o`` while the layer is off: ask to turn it on and watch, rather than only saying it is off."""
    model.wb_target = target_of(model, node)
    if not model.wb_target.get("running"):
        model.error = "{} is not running; watch it once its agent is up".format(node.label)
        return None
    model.pending_action = watch_intent(model, True)
    model.pending_decline = None
    model.status = "The whiteboard is off. Turn it on and watch {}? y turns it on and watches, n cancels".format(node.label)
    return None


# --------------------------------------------------------------------------
# a team of one


def default_solo_team(model: PickerModel) -> str:
    """``<agent name or kind>-canvas``, fitted to a team name and free in this session."""
    target = model.wb_target or {}
    base = tm.normalize_team_name(str(target.get("name") or target.get("agent_kind") or "agent"))
    base = (base[: PICKER_TEAM_CHARS - len(SOLO_SUFFIX)].rstrip("-_") or "agent") + SOLO_SUFFIX
    taken = set(model.existing_teams)
    if base not in taken:
        return base
    for number in range(2, 100):
        tail = "-{}".format(number)
        candidate = base[: PICKER_TEAM_CHARS - len(tail)].rstrip("-_") + tail
        if candidate not in taken:
            return candidate
    return base


def solo_member(model: PickerModel, team: str) -> Tuple[str, str]:
    """``(role, name)`` for the agent joining its own team: the wizard's ``<kind>-dev`` role, and its own
    Herdr name when that is a valid member name no roster holds, else ``<team>-<role>``."""
    target = model.wb_target or {}
    row = tm.PickerRow(pane_id=str(target.get("pane_id") or ""), workspace_id="", tab_id="", kind=target.get("agent_kind") or None,
                       name=target.get("name") or None, agent_status="idle", launch_pending=False)
    role = tm.default_role(row)
    own = str(target.get("name") or "")
    in_rosters = {str(m.get("name")) for members in model.rosters.values() for m in members
                  if isinstance(m, dict) and m.get("status") != "left" and m.get("name")}
    if own and tm.validate_member_name_local(own) is None and own not in in_rosters:
        return role, own
    base = roster.fit_member_name(team, role, tm.MAX_MEMBER_NAME_CHARS)
    return role, tm.suggest_name(base, (in_rosters | set(model.live_names)) - {own}) or base


def solo_key(model: PickerModel, key: str) -> Optional[Intent]:
    if key in ("PASTE_START", "PASTE_END"):
        model.paste_mode = key == "PASTE_START"
        return None
    if model.stage == "solo_name":
        if key == "ESC":
            model.stage = MENU_STAGE
            model.error = None
            return None
        if key == "ENTER" and not model.paste_mode:
            name = tm.normalize_team_name(model.input)
            err = tm.validate_team_name_local(name)
            if err:
                model.error = err
                return None
            if name in model.existing_teams:
                model.error = "team {} exists; pick another name (a team of one is a new team)".format(name)
                return None
            model.solo_team = name
            model.stage = "solo_mission"
            model.error = None
            tm._set_input(model, model.solo_mission or SOLO_MISSION)
            return None
        tm.edit_key(tm._TextView(model), key)
        return None
    if key == "ESC":
        model.solo_mission = model.input
        model.stage = "solo_name"
        model.error = None
        tm._set_input(model, model.solo_team)
        return None
    if key == "ENTER" and not model.paste_mode:
        mission = re.sub(r"\s+", " ", model.input).strip()
        if not mission:
            model.error = "a team of one still needs a Mission: what the agent is there to do"
            return None
        if len(mission) > tm.MAX_BRIEF_TOTAL_CHARS:
            model.error = "the Mission is {} characters; the limit is {}".format(len(mission), tm.MAX_BRIEF_TOTAL_CHARS)
            return None
        model.solo_mission = mission
        target = model.wb_target or {}
        kind = str(target.get("agent_kind") or "")
        if untrusted(model, kind):
            # Nothing is typed into an untrusted kind, so the agent would never hear of its canvas
            # (found live on 2026-09-27 with OpenCode on a fresh machine). Say so, and offer the fix.
            yes = solo_intent(model, trust=True, clear=False)
            no = solo_intent(model, trust=False)
            model.pending_action, model.pending_decline = yes, no
            model.stage = "select"
            model.status = ("{} is not trusted for typed delivery in this Herdr session, so nothing can tell {} about its canvas. "
                            "y trusts {} and creates the team, n creates it without telling it, Esc cancels").format(kind, target.get("label"), kind)
            return None
        return solo_intent(model)
    tm.edit_key(tm._TextView(model), key)
    return None


def solo_intent(model: PickerModel, trust: bool = False, clear: bool = True) -> Intent:
    """``create`` for a team of one, after ``kinds trust`` when ``trust``; its briefing tells it about the canvas."""
    target = model.wb_target or {}
    team = model.solo_team
    role, name = solo_member(model, team)
    create = ["create", team, "--member", "{}:{}:{}".format(target.get("pane_id"), role, name), "--brief", "{}={}".format(name, model.solo_mission)]
    kind = str(target.get("agent_kind") or "")
    canvas_on = ["--team", team, "whiteboard", "team", "on"]  # only this team: a canvas is per team
    steps = ([trust_step(kind, "trusted from the teams view to brief {}".format(name))] if trust else []) + [create, canvas_on]
    if untrusted(model, kind) and not trust:
        done = "{} has a canvas of its own in team {}, but is not told until you run: herdr-synapse kinds trust {}".format(name, team, kind)
    else:
        done = "{} has a canvas of its own in team {}; its briefing tells it once it is idle; d opens the page".format(name, team)
    if clear:
        model.solo_team = ""
        model.solo_mission = ""
    return steps_intent(model, _needs_layer(model, steps), done, check_pane=str(target.get("pane_id") or ""),
                        terminal_id=str(target.get("terminal_id") or ""), focus="team:" + team)


# --------------------------------------------------------------------------
# rendering


def state_line(model: PickerModel) -> str:
    if model.watch_layer is None:
        return "Whiteboard: unknown (no Herdr session)"
    if not layer_on(model):
        return "Whiteboard: off for this Herdr session"
    return "Whiteboard: on for this Herdr session · page {}".format("open" if model.wb_page else "closed")


def lines(model: PickerModel, width: int, height: int) -> Tuple[List[str], bool]:
    """Screen lines for the whiteboard stages, and whether the last one is an input line."""
    target = model.wb_target or {}
    label = str(target.get("label") or "")
    out: List[str] = []
    if model.stage == MENU_STAGE:
        about = ""
        if target.get("kind") == "team":
            canvas, viz = team_switches(model, label)
            about = "team {}: canvas {}{}".format(label, "on" if canvas else "off", ", live visuals {}".format("on" if viz else "off") if canvas else "")
        elif target.get("kind") == "member":
            about = "{} in team {}{}".format(label, target.get("team"), " · watched" if target.get("terminal_id") in model.watched else "")
        elif target.get("kind") == "agent":
            about = "{} ({}, pane {}) · in no team{}".format(label, target.get("agent_kind") or "?", target.get("pane_id") or "?",
                                                            " · watched" if target.get("terminal_id") in model.watched else "")
        header = tm._wrap_picker_text(state_line(model), width, "  ")
        if about:
            header.extend(tm._wrap_picker_text(about, width, "  "))
        header.append("")
        groups: List[List[str]] = []
        rows = options(model)
        selected = min(model.wb_index, len(rows) - 1)
        for index, (_choice, text) in enumerate(rows):
            lead = "{} {}  ".format(">" if index == selected else " ", index + 1)
            groups.append(tm._wrap_picker_text(lead + text, width, " " * tm.display_width(lead)))
        footer = tm._wrap_picker_text("type a number | Up/Down move | Enter acts | Esc or d back", width, "  ")
        reserved = len(tm.picker_message_lines(model, width, height))
        if len(header) + len(footer) + len(groups[selected]) + reserved > height:
            header = tm._wrap_picker_text(state_line(model), width, "  ")
            footer = tm._wrap_picker_text("1-9, arrows, Enter, Esc", width, "  ")
        out.extend(header)
        out.extend(tm._option_window(groups, selected, max(1, height - len(header) - len(footer) - reserved)))
        out.extend(footer)
        return out, False
    if model.stage == "solo_name":
        out.extend(tm._wrap_picker_text("A canvas of its own for {}: it joins a new team with only itself in it (Esc back)".format(label), width, "  "))
        out.extend(tm._wrap_picker_text("team name ([a-z][a-z0-9_-], up to {} characters; normalized on Enter):".format(PICKER_TEAM_CHARS), width, "  "))
        out.append(tm.INPUT_PROMPT + model.input)
        return out, True
    out.extend(tm._wrap_picker_text("Mission for {} in team {} (it is briefed once idle; Ctrl-U clears, Esc back)".format(label, model.solo_team), width, "  "))
    out.append("Mission:")
    out.append(tm.INPUT_PROMPT + model.input)
    return out, True
