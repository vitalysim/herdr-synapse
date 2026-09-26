"""``model`` and ``models``: which model and how much thinking each member gets.

``models`` holds the team defaults per kind (``config.models``); ``model``
reads or sets one member. A change is recorded on the roster and announced
on the board; whether it *applies* now depends on the harness
(``herdr_team.models``): Claude takes ``/model`` and ``/effort`` typed live;
OpenCode effort can be selected exactly through ``/variants``; model changes
for Codex and OpenCode take effect at the next ``resume`` -- or now, with
``--apply restart``, when the notifier exits and resumes the session. OpenCode
variant selection follows that resume because its full TUI has no effort flag.

Authority (owner decision, 2026-09-08): the operator or a delegate may set
anyone, the team manager may set anyone, a member may set itself with
``--self``. Live and restart go through the control-job path, whose record
needs a verified origin exactly as ``compact`` does.
"""
from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List, Optional, Tuple

from herdr_team import charter as _charter
from herdr_team import harnesses as _harnesses
from herdr_team import identity as _identity
from herdr_team import permissions as _permissions
from herdr_team import models as _models
from herdr_team import paths as _paths
from herdr_team import roster as _roster
from herdr_team import store
from herdr_team.cli import Command, emit
from herdr_team.errors import EXIT_REFUSED, EXIT_UNREACHABLE, HerdrTeamError, UsageError

APPLY_MODES = ("live", "next", "restart")


# --------------------------------------------------------------------------
# helpers


def _config_models(doc: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    config = doc.get("config") if isinstance(doc.get("config"), dict) else {}
    models = config.get("models") if isinstance(config.get("models"), dict) else {}
    return {str(k): dict(v) for k, v in models.items() if isinstance(v, dict)}


def member_view(doc: Dict[str, Any], member: Dict[str, Any], observed: Optional[str] = None) -> Dict[str, Any]:
    """One row of the ``model`` table: configured halves, where each comes from, and what the harness reports."""
    config = doc.get("config") if isinstance(doc.get("config"), dict) else {}
    model, effort = _models.effective_setting(config, member)
    model_src, effort_src = _models.source_of(config, member)
    return {
        "name": member.get("name"), "kind": member.get("kind"), "profile": member.get("profile"),
        "model": model, "effort": effort, "setting": _models.label(model, effort),
        "model_source": model_src, "effort_source": effort_src,
        "observed": observed,
        "observed_matches": _models.observed_matches(member.get("kind"), model, observed) if (model and observed) else None,
    }


def observed_models(layout: Any, team_name: str) -> Dict[str, Optional[str]]:
    """``who.json``'s context reading per member, when the notifier has one."""
    doc = store.read_json(layout.session.who_json, default=None)
    out: Dict[str, Optional[str]] = {}
    teams = doc.get("teams") if isinstance(doc, dict) else None
    team = teams.get(team_name) if isinstance(teams, dict) else None
    for member in (team or {}).get("members") or []:
        if isinstance(member, dict) and isinstance(member.get("name"), str):
            context = member.get("context")
            out[member["name"]] = context.get("model") if isinstance(context, dict) and isinstance(context.get("model"), str) else None
    return out


def running_argv(api: Any, member: Dict[str, Any]) -> Optional[List[str]]:
    """Best-effort argv for policy flags that should survive a restart."""
    pane_id = member.get("pane_id")
    if not isinstance(pane_id, str) or not pane_id:
        return None
    try:
        result = api.request("pane.process_info", {"pane_id": pane_id})
    except HerdrTeamError:
        return None
    info = result.get("process_info") if isinstance(result, dict) else None
    processes = info.get("foreground_processes") if isinstance(info, dict) else None
    return _models.foreground_argv(member.get("kind"), processes)


def model_authority(layout: Any, team_name: str, doc: Dict[str, Any], author: Any, target: str, on_self: bool, action: str = "model set") -> str:
    """Who is changing whose model (or profile): ``operator``, ``manager``, or ``self``; anything else is refused and audited."""
    if on_self:
        if not author.is_member:
            raise HerdrTeamError("not_a_member", "--self runs from a member's own pane; you are {}".format(author.name), EXIT_UNREACHABLE, {"author": author.name})
        if target != author.name:
            raise UsageError("--self acts on your own pane; drop the name or the flag")
        return "self"
    if author.trusted_human or getattr(author, "operator", False):
        if getattr(author, "operator", False) and not author.is_human:
            _identity.audit(layout, team_name, "operator_action", author, {"action": action, "member": target})
        return "operator"
    if author.is_member:
        row = next((m for m in doc.get("members") or [] if isinstance(m, dict) and m.get("name") == author.name), None)
        if row is not None and row.get("manager"):
            return "manager"
    _identity.audit(layout, team_name, "author_mismatch", author, {"action": action, "member": target})
    if author.is_human:
        raise HerdrTeamError("author_mismatch", _identity.authority_refusal(action, author), EXIT_REFUSED, {"action": action, "author": author.name, "via": author.via})
    raise HerdrTeamError(
        "author_mismatch",
        "{} is for the operator, the team manager, or the member itself (--self); {} is neither, so post a request to the manager".format(action, author.name),
        EXIT_REFUSED, {"action": action, "author": author.name, "via": author.via},
    )


def restart_control(kind: str, session: Any, model: Optional[str], effort: Optional[str], profile: Optional[str],
                    current_argv: Optional[List[str]], mode: str) -> Dict[str, Any]:
    """The control block of a controlled restart: exit the harness, resume its session with this setting and profile.

    The notifier rebuilds exactly this from the record before it acts, so
    every field that shapes the argv is recorded here.
    """
    preserved = _models.preserved_launch_args(kind, current_argv, mode, profile)
    argv = _models.restart_argv(kind, session, model, effort, current_argv, permissions=mode, profile=profile)  # session_unknown / session_unsupported surface here
    return {"action": "restart", "kind": kind, "permissions": mode, "model": model, "effort": effort, "profile": profile,
            "exit": _models.exit_keystroke(kind), "argv": argv, "preserved": preserved, "after": _models.post_start_keystrokes(kind, effort)}


# --------------------------------------------------------------------------
# models: team defaults per kind


def _add_models_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="models_action", metavar="<show|set|clear>")
    sub.add_parser("show", help="the team defaults per kind (the default)")
    setter = sub.add_parser("set", help="set the default for a kind: models set claude opus@medium")
    setter.add_argument("kind")
    setter.add_argument("setting", metavar="MODEL[@EFFORT]")
    setter.add_argument("--unlisted", action="store_true", help="accept a model the harness does not list here")
    clearer = sub.add_parser("clear", help="drop the default for a kind")
    clearer.add_argument("kind")


def _run_models(args: argparse.Namespace) -> int:
    from herdr_team.cmd_board import _open_team, check_write_session

    action = getattr(args, "models_action", None) or "show"
    layout, api, author, team_name, team, doc = _open_team(args, require_server=False, write=action != "show")
    if action == "show":
        payload = {"team": team_name, "models": _config_models(doc), "kinds": list(_models.KINDS), "efforts": {k: list(v) if v else None for k, v in _models.EFFORTS.items()}}

        def human() -> str:
            rows = payload["models"]
            if not rows:
                return "no team defaults; set one with: herdr-synapse models set <kind> <model>[@<effort>]"
            return "\n".join("{:10s} {}".format(kind, _models.label(v.get("model"), v.get("effort")) or "-") for kind, v in sorted(rows.items()))

        return emit(args, payload, human)
    _charter.require_human(layout, team_name, author, "models {}".format(action))
    check_write_session(args, layout, team_name)
    kind = str(args.kind).strip()
    if kind not in _models.KINDS:
        raise HerdrTeamError("model_unsupported", "no verified model or effort flags for a {} agent; supported: {}".format(kind, ", ".join(_models.KINDS)), EXIT_REFUSED, {"kind": kind, "supported": list(_models.KINDS)})
    model = effort = None
    if action == "set":
        model, effort = _models.parse_setting(args.setting)
        _models.validate(kind, model, effort)
        _harnesses.check_model(kind, model, effort, None, dict(getattr(args, "env", None) or {}), unlisted=bool(getattr(args, "unlisted", False)))

    def mutate(t: _roster.Team) -> None:
        models = t.config.get("models") if isinstance(t.config.get("models"), dict) else {}
        models = dict(models)
        if action == "set":
            models[kind] = {"model": model, "effort": effort}
        else:
            models.pop(kind, None)
        t.config["models"] = models

    _roster.update_team(team, mutate)
    _identity.audit(layout, team_name, "models_{}".format(action), author, {"kind": kind, "model": model, "effort": effort})
    payload = {"team": team_name, "kind": kind, "model": model, "effort": effort, "setting": _models.label(model, effort), "action": action}
    return emit(args, payload, "{} default for {}: {}".format(team_name, kind, _models.label(model, effort) or "cleared"))


# --------------------------------------------------------------------------
# model: one member


def _add_model_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("member", nargs="?", help="the member (default: every member, read-only)")
    parser.add_argument("setting", nargs="?", metavar="MODEL[@EFFORT]", help="e.g. opus@medium, gpt-5.6-luna@high, @xhigh")
    parser.add_argument("--effort", metavar="EFFORT", help="the effort half on its own")
    parser.add_argument("--self", dest="on_self", action="store_true", help="act on your own pane (members only)")
    parser.add_argument("--apply", choices=APPLY_MODES, help="live (Claude model/effort or OpenCode effort), next (at the next resume), restart (exit, resume, then finish any native UI selection)")
    parser.add_argument("--reason", metavar="TEXT", help="why, for the board record")
    parser.add_argument("--unlisted", action="store_true", help="accept a model the harness does not list here")


def _run_model(args: argparse.Namespace) -> int:
    from herdr_team.cmd_board import (
        _open_team, agent_members, board_append, build_record, check_write_session, enqueue_job, member_generation,
        member_or_raise, require_daemon,
    )

    if args.on_self and args.setting is None and args.member is not None:
        # ``model --self opus@medium``: the one positional is the setting, not a name
        args.member, args.setting = None, args.member
    reading = args.setting is None and args.effort is None
    layout, api, author, team_name, team, doc = _open_team(args, require_server=not reading, write=not reading)
    observed = observed_models(layout, team_name)
    if args.member is None and not args.on_self:
        rows = [member_view(doc, m, observed.get(str(m.get("name")))) for m in agent_members(doc)]
        payload = {"team": team_name, "members": rows, "defaults": _config_models(doc)}

        def human() -> str:
            lines = []
            for r in rows:
                seen = r["observed"]
                note = "" if not seen else ("  (runs {})".format(seen) if r["observed_matches"] is False else "  (confirmed)" if r["observed_matches"] else "  (runs {})".format(seen))
                lines.append("{:34s} {:9s} {}{}".format(str(r["name"]), str(r["kind"]), r["setting"] or "harness default", note))
            return "\n".join(lines) if lines else "no agent members"

        return emit(args, payload, human)
    name = args.member or (author.name if args.on_self else None)
    if not name:
        raise UsageError("which member? herdr-synapse model <name> [<setting>] (or --self)")
    member = member_or_raise(doc, name, team_name)
    name = str(member.get("name"))
    if reading:
        view = member_view(doc, member, observed.get(name))
        return emit(args, dict(view, team=team_name), lambda: "{}: {} ({} model, {} effort){}".format(
            name, view["setting"] or "harness default", view["model_source"], view["effort_source"],
            "" if not view["observed"] else "; runs {}".format(view["observed"])))

    # a write
    who = model_authority(layout, team_name, doc, author, name, args.on_self)
    model, effort = (None, None)
    if args.setting is not None:
        model, effort = _models.parse_setting(args.setting)
    if args.effort is not None:
        if effort is not None:
            raise UsageError("pass the effort once: <model>@<effort> or --effort")
        effort = _models._token(args.effort, "effort")
    kind = str(member.get("kind") or "")
    _models.validate(kind, model, effort)
    _harnesses.check_model(kind, model, effort, member.get("cwd") if isinstance(member.get("cwd"), str) else None,
                           dict(getattr(args, "env", None) or {}), unlisted=args.unlisted)
    apply = args.apply or ("live" if kind == "claude" or (kind == "opencode" and model is None and effort is not None) else "next")
    if apply == "live" and _models.live_keystrokes(kind, model, effort) is None:
        raise HerdrTeamError("model_apply_unsupported", "{} cannot apply that setting as an exact live command; use --apply restart or --apply next".format(kind), EXIT_REFUSED, {"kind": kind, "apply": apply})
    check_write_session(args, layout, team_name)
    if apply == "restart":
        # Planned before anything is written: a member with no recorded
        # session cannot be resumed, and must not be left half-changed.
        _models.resume_argv(kind, member.get("session"), model or _models.effective_setting(doc.get("config"), member)[0], effort or _models.effective_setting(doc.get("config"), member)[1], _permissions.effective(doc.get("config"), member),
                            member.get("profile") if isinstance(member.get("profile"), str) else None)
    if apply != "next":
        # The notifier carries out a live or restart change; without one the
        # change is refused before anything is written, not recorded half-done.
        require_daemon(layout.session)
    current_argv = running_argv(api, member) if apply == "restart" else None

    def mutate(t: _roster.Team) -> None:
        row = t.find(name)
        if row is None:
            raise HerdrTeamError("member_not_found", "{!r} is not in team {!r}".format(name, team_name), EXIT_REFUSED, {"name": name})
        if model is not None:
            row.model = model
        if effort is not None:
            row.effort = effort

    updated = _roster.update_team(team, mutate)
    row = updated.find(name)
    eff_model, eff_effort = _models.effective_setting(updated.config, row) if row is not None else (model, effort)
    setting = _models.label(eff_model, eff_effort)
    _identity.audit(layout, team_name, "model_set", author, {"member": name, "model": model, "effort": effort, "apply": apply, "by": who})
    by = "the operator" if who == "operator" else ("the team manager {}".format(author.name) if who == "manager" else "itself")
    text = "{} now runs {}{} (set by {}; applies {})".format(
        name, setting, ": {}".format(args.reason) if args.reason else "", by,
        {"live": "now", "next": "at its next resume or restart", "restart": "now, by a restart of its session"}[apply])
    _roster.append_system_record(team, "model_changed", text, to=[name, "all"],
                                 extra={"member": name, "model": eff_model, "effort": eff_effort, "requested_by": author.name, "apply": apply, "reason": args.reason},
                                 socket=os.fspath(layout.socket))
    payload: Dict[str, Any] = {"team": team_name, "member": name, "kind": kind, "model": eff_model, "effort": eff_effort, "setting": setting,
                               "apply": apply, "by": who, "job": None, "record_seq": None}
    if apply == "next":
        return emit(args, payload, "{}: {} recorded; it applies at the next resume or restart (or pass --apply restart)".format(name, setting))
    # live or restart: a control job the notifier (checked above) acts on once the member is idle
    if apply == "restart":
        mode = _permissions.effective(updated.config, row)
        control = restart_control(kind, member.get("session"), eff_model, eff_effort, row.profile if row is not None else None, current_argv, mode)
        line = "restart {}: {}".format(name, " ".join(control["argv"]))
    else:
        keystrokes = _models.live_keystrokes(kind, eff_model if model is not None else None, eff_effort if effort is not None else None) or []
        if not keystrokes:
            keystrokes = _models.live_keystrokes(kind, eff_model, eff_effort) or []
        control = {"action": "model", "kind": kind, "model": eff_model if model is not None else None, "effort": eff_effort if effort is not None else None,
                   "keystrokes": keystrokes}
        line = "model {}: {}".format(name, "; ".join(keystrokes))
    record = build_record(author, [name], "direct", line, urgent=False, socket_path=os.fspath(layout.socket), from_gen=member_generation(doc, author))
    record["control"] = control
    seq = board_append(team, record)
    job = enqueue_job(team, "control", name, author, extra={"seq": seq, "action": control["action"]})
    payload.update({"job": job, "record_seq": seq, "control": control})
    if apply == "restart":
        suffix = ", then selects its OpenCode variant" if control.get("after") else ""
        return emit(args, payload, "{}: {} recorded; the notifier exits it when idle and resumes its session{}".format(name, setting, suffix))
    return emit(args, payload, "{}: {} recorded; the notifier types {} when it is idle".format(name, setting, " then ".join(repr(k) for k in control["keystrokes"])))


# --------------------------------------------------------------------------
# profile: which of the harness's own agents or profiles a member runs as


def profile_view(member: Dict[str, Any]) -> Dict[str, Any]:
    kind = str(member.get("kind") or "")
    return {"name": member.get("name"), "kind": kind, "profile": member.get("profile"),
            "selectable": kind in _models.PROFILE_FLAGS, "flag": _models.PROFILE_FLAGS.get(kind)}


def _add_profile_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("member", nargs="?", help="the member (default: every member, read-only)")
    parser.add_argument("profile", nargs="?", metavar="PROFILE", help="an OpenCode or Claude Code agent, or a Codex profile (see: herdr-synapse available <harness>)")
    parser.add_argument("--clear", action="store_true", help="back to the harness's default")
    parser.add_argument("--self", dest="on_self", action="store_true", help="act on your own pane (members only)")
    parser.add_argument("--apply", choices=("next", "restart"),
                        help="next (default): at the next resume or restart; restart: the notifier exits it when idle and resumes its session with the profile")
    parser.add_argument("--unlisted", action="store_true", help="accept a profile the harness does not list here")
    parser.add_argument("--reason", metavar="TEXT", help="why, for the board record")


def _run_profile(args: argparse.Namespace) -> int:
    from herdr_team.cmd_board import (
        _open_team, agent_members, board_append, build_record, check_write_session, enqueue_job, member_generation,
        member_or_raise, require_daemon,
    )

    if args.on_self and args.profile is None and args.member is not None and not args.clear:
        # ``profile --self plan``: the one positional is the profile, not a name
        args.member, args.profile = None, args.member
    if args.profile is not None and args.clear:
        raise UsageError("name a profile or pass --clear, not both")
    reading = args.profile is None and not args.clear
    restart = args.apply == "restart"
    layout, api, author, team_name, team, doc = _open_team(args, require_server=restart, write=not reading)
    if args.member is None and not args.on_self:
        rows = [profile_view(m) for m in agent_members(doc)]

        def human() -> str:
            lines = ["{:34s} {:9s} {}".format(str(r["name"]), str(r["kind"]), r["profile"] or ("harness default" if r["selectable"] else "-"))
                     for r in rows]
            return "\n".join(lines) if lines else "no agent members"

        return emit(args, {"team": team_name, "members": rows}, human)
    name = args.member or (author.name if args.on_self else None)
    if not name:
        raise UsageError("which member? herdr-synapse profile <name> [<profile>] (or --self)")
    member = member_or_raise(doc, name, team_name)
    name = str(member.get("name"))
    if reading:
        view = profile_view(member)
        return emit(args, dict(view, team=team_name), "{}: {}".format(name, view["profile"] or ("harness default" if view["selectable"] else "{} has no profiles".format(view["kind"]))))

    who = model_authority(layout, team_name, doc, author, name, args.on_self, action="profile set")
    kind = str(member.get("kind") or "")
    cwd = member.get("cwd") if isinstance(member.get("cwd"), str) else None
    profile = None if args.clear else _harnesses.check_profile(kind, args.profile, cwd, dict(getattr(args, "env", None) or {}), unlisted=args.unlisted)
    check_write_session(args, layout, team_name)
    mode = _permissions.effective(doc.get("config"), member)
    model, effort = _models.effective_setting(doc.get("config"), member)
    control: Optional[Dict[str, Any]] = None
    if restart:
        # Planned before anything is written: no notifier to carry it out, or no
        # recorded session to resume, refuses the change instead of half-applying it.
        control = restart_control(kind, member.get("session"), model, effort, profile, running_argv(api, member), mode)
        require_daemon(layout.session)

    def mutate(t: _roster.Team) -> None:
        row = t.find(name)
        if row is None:
            raise HerdrTeamError("member_not_found", "{!r} is not in team {!r}".format(name, team_name), EXIT_REFUSED, {"name": name})
        row.profile = profile

    _roster.update_team(team, mutate)
    _identity.audit(layout, team_name, "profile_set", author, {"member": name, "profile": profile, "apply": args.apply or "next", "by": who})
    by = "the operator" if who == "operator" else ("the team manager {}".format(author.name) if who == "manager" else "itself")
    text = "{} now runs {} as {}{} (set by {}; applies {})".format(
        name, kind, "profile " + profile if profile else "the harness default", ": {}".format(args.reason) if args.reason else "", by,
        "now, by a restart of its session" if restart else "at its next resume or restart")
    _roster.append_system_record(team, "profile_changed", text, to=[name, "all"],
                                 extra={"member": name, "profile": profile, "requested_by": author.name, "apply": args.apply or "next", "reason": args.reason},
                                 socket=os.fspath(layout.socket))
    payload: Dict[str, Any] = {"team": team_name, "member": name, "kind": kind, "profile": profile, "apply": args.apply or "next", "by": who,
                               "job": None, "record_seq": None}
    if control is None:
        return emit(args, payload, "{}: {} recorded; it applies at the next resume or restart (or pass --apply restart)".format(
            name, "profile " + profile if profile else "harness default"))
    record = build_record(author, [name], "direct", "restart {}: {}".format(name, " ".join(control["argv"])), urgent=False,
                          socket_path=os.fspath(layout.socket), from_gen=member_generation(doc, author))
    record["control"] = control
    seq = board_append(team, record)
    job = enqueue_job(team, "control", name, author, extra={"seq": seq, "action": "restart"})
    payload.update({"job": job, "record_seq": seq, "control": control})
    return emit(args, payload, "{}: {} recorded; the notifier exits it when idle and resumes its session with it".format(
        name, "profile " + profile if profile else "harness default"))


COMMANDS: List[Command] = [
    Command("models", "team defaults for model and effort per kind (show | set <kind> <model>[@<effort>] | clear <kind>)", _add_models_arguments, _run_models),
    Command("model", "which model and effort a member runs with; set it (operator, manager, or --self)", _add_model_arguments, _run_model),
    Command("profile", "which of its harness's own agents or profiles a member runs as; set it (operator, manager, or --self)", _add_profile_arguments, _run_profile),
]
