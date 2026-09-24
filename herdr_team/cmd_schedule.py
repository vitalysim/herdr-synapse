"""``schedule``: recurring operator posts that the notifier writes on time.

The model, the cron engine and the firing live in ``herdr_team.schedules``;
this module adds the argument grammar, the authority checks and the audit
lines. Writing a schedule is operator authority (``_human_only``: the operator
or a member it delegated to), because the notifier later posts it *as the
operator*. A ``--precheck`` is a shell command run as the user, so anything
that can make one run (``add``, ``enable``, ``run`` of such a schedule) needs
the operator in person (``_strictly_human``); a delegate may still create,
remove and pause schedules without one.

Reading (``list``, ``show``, ``next``) is open to everyone on the team: a
member seeing what will land on it tomorrow is the point.
"""
from __future__ import annotations

import argparse
from typing import Any, Dict, List, Optional

from herdr_team import schedules as _schedules
from herdr_team.cli import Command, add_global_arguments, emit
from herdr_team.errors import EXIT_REFUSED, EXIT_USAGE, HerdrTeamError, UsageError
from herdr_team.identity import Author, audit

ACTIONS = ("add", "list", "show", "rm", "enable", "disable", "run", "next")
DEFAULT_NEXT_COUNT = 10
MAX_NEXT_COUNT = 100
#: ``add`` warns when two fires are closer than this: every fire is a post that wakes its recipients.
BUSY_INTERVAL_S = 10 * 60


# --------------------------------------------------------------------------
# arguments


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="schedule_action", metavar="<{}>".format("|".join(ACTIONS)))

    def action(name: str, help_text: str) -> argparse.ArgumentParser:
        child = sub.add_parser(name, help=help_text, description=help_text, allow_abbrev=False)
        add_global_arguments(child, nested=True)
        return child

    add = action("add", "create a schedule; prints its id and the next three fire times (operator)")
    add.add_argument("text", help="what the post says, as you would write it to the team")
    when = add.add_mutually_exclusive_group(required=True)
    when.add_argument("--cron", metavar='"M H DOM MON DOW"', help="five cron fields: minute hour day-of-month month day-of-week (names, lists, ranges, steps)")
    when.add_argument("--every", choices=_schedules.PRESETS, help="a common timetable instead of --cron")
    add.add_argument("--at", metavar="HH:MM", help="time of day for daily, weekdays and weekly (default 09:00); the minute for hourly (:15)")
    add.add_argument("--day", metavar="mon..sun", help="day(s) for --every weekly, e.g. mon or mon,thu (default mon)")
    add.add_argument("--tz", metavar="IANA", help="time zone, e.g. Europe/Berlin (default: this machine's local zone)")
    add.add_argument("--to", metavar="TARGET", action="append", required=True, help="name[,name...] | all | role:<role> (repeatable)")
    add.add_argument("--kind", choices=_schedules.KINDS, default=_schedules.DEFAULT_KIND, help="post kind (default request)")
    add.add_argument("--name", metavar="NAME", help="a short name, usable in place of the id")
    add.add_argument("--precheck", metavar="CMD", help="shell command run first; a non-zero exit skips that run (operator in person only)")
    add.add_argument("--precheck-timeout", dest="precheck_timeout", metavar="DURATION", default=str(_schedules.DEFAULT_PRECHECK_TIMEOUT_S), help="how long the precheck may take (default 60 s; a bare number is seconds)")
    add.add_argument("--grace", metavar="DURATION", default="30m", help="how late a run missed while the notifier was down may still fire (default 30m; a bare number is minutes)")
    add.add_argument("--disabled", action="store_true", help="create it switched off")
    add.add_argument("--force", action="store_true", help="save it even when the text looks like a secret")
    add.add_argument("--as-work", dest="as_work", action="store_true", help="each run is a work item (owned by the recipient when it names one member) instead of a post")
    add.add_argument("--acceptance", metavar="TEXT", help="with --as-work: the evidence that proves each run done")

    action("list", "every schedule of the team with its next fire and last outcome")
    for name, help_text in (
        ("show", "one schedule in full: definition, state, next five fires"),
        ("rm", "delete a schedule (operator)"),
        ("enable", "switch a schedule on; runs missed while it was off are not owed (operator)"),
        ("disable", "switch a schedule off (operator)"),
        ("run", "fire a schedule now, precheck included; the timetable is unchanged (operator)"),
    ):
        action(name, help_text).add_argument("ref", metavar="ID", help="the schedule id (s1) or its name")
    nxt = action("next", "the upcoming fires across the team's schedules, soonest first")
    nxt.add_argument("--count", type=int, default=DEFAULT_NEXT_COUNT, metavar="N", help="how many (default 10)")


# --------------------------------------------------------------------------
# helpers


def _action_of(args: argparse.Namespace) -> Dict[str, Any]:
    if getattr(args, "acceptance", None) and not getattr(args, "as_work", False):
        raise UsageError("--acceptance goes with --as-work")
    if getattr(args, "as_work", False):
        action: Dict[str, Any] = {"type": _schedules.ACTION_WORK}
        if args.acceptance:
            action["acceptance"] = " ".join(str(args.acceptance).split())
        return action
    return {"type": _schedules.ACTION_POST}


def _open(args: argparse.Namespace, write: bool) -> Any:
    from herdr_team.cmd_board import _open_team

    return _open_team(args, require_server=False, write=write)


def _authorize(layout: Any, team: str, author: Author, action: str, precheck: bool) -> None:
    """Operator authority, and the operator in person for anything that can run a precheck."""
    from herdr_team.cmd_roster import _human_only, _strictly_human

    if not precheck:
        _human_only(layout, team, author, "schedule {}".format(action))
        return
    if not author.trusted_human and getattr(author, "operator", False):
        audit(layout, team, "author_mismatch", author, {"action": "schedule {} (precheck)".format(action), "resolved": author.name, "via": author.via})
        raise HerdrTeamError(
            "author_mismatch",
            "a schedule with --precheck runs a shell command as the operator, so only the operator in person may {} one; "
            "a delegated member may add schedules without a precheck".format(action),
            EXIT_REFUSED, {"action": "schedule {}".format(action), "author": author.name, "via": author.via},
        )
    _strictly_human(layout, team, author, "schedule {} with a precheck".format(action))


def _find(team: Any, ref: str) -> _schedules.Schedule:
    schedules, _invalid = _schedules.load(team)
    sched = _schedules.find(schedules, ref)
    if sched is None:
        raise HerdrTeamError("schedule_not_found", "no schedule {!r} in this team; see herdr-synapse schedule list".format(ref), EXIT_REFUSED,
                             {"ref": ref, "schedules": [s.id for s in schedules]})
    return sched


def _to_label(sched: _schedules.Schedule) -> str:
    return ",".join(sched.to)


def _fires(sched: _schedules.Schedule, after: float, count: int, env: Dict[str, str]) -> List[Any]:
    try:
        _label, zone = _schedules.zone_of(sched.tz, env)
        return _schedules.next_fires(sched.spec(), after, count, tz=zone)
    except HerdrTeamError:
        return []


def _zone_label(sched: _schedules.Schedule, env: Dict[str, str]) -> str:
    try:
        return _schedules.zone_of(sched.tz, env)[0]
    except HerdrTeamError:
        return str(sched.tz)


def _row(sched: _schedules.Schedule, state: Dict[str, Any], nxt: List[Any], env: Dict[str, str]) -> Dict[str, Any]:
    return dict(sched.to_json(), when=_schedules.describe(sched), zone=_zone_label(sched, env),
                next=nxt[0].isoformat() if nxt else None, state=state)


def _last_text(state: Dict[str, Any]) -> str:
    outcome = state.get("last_outcome")
    if outcome:
        seq = " #{}".format(state["last_seq"]) if state.get("last_seq") else ""
        return "last {}{} ({})".format(outcome, seq, state.get("last_slot") or "?")
    if state.get("last_manual_outcome"):
        seq = " #{}".format(state["last_manual_seq"]) if state.get("last_manual_seq") else ""
        return "run by hand: {}{} ({})".format(state["last_manual_outcome"], seq, state.get("last_manual_at") or "?")
    return "never run"


def _notifier_warning(args: argparse.Namespace, layout: Any) -> str:
    from herdr_team.cmd_board import notifier_state, warn

    state = notifier_state(layout.session)
    if state == "offline":
        warn(args, "notifier offline: schedules fire only while it runs (herdr-synapse daemon start)")
    return state


# --------------------------------------------------------------------------
# add


def _run_add(args: argparse.Namespace) -> int:
    from herdr_team.cmd_board import prepare_text

    layout, _api, author, team_name, team, doc = _open(args, write=True)
    _authorize(layout, team_name, author, "add", bool(args.precheck))
    env = dict(args.env)
    now = _schedules._now()

    text, _truncated, _body = prepare_text(args.text, spill=False, force=args.force)
    if args.cron is not None:
        if args.at is not None or args.day is not None:
            raise UsageError("--at and --day go with --every; put the time in the --cron fields instead")
        cron = " ".join(args.cron.split())
    else:
        cron = _schedules.preset_cron(args.every, args.at, args.day)
    spec = _schedules.parse_cron(cron)
    if args.tz is not None:
        zone_name: Optional[str] = args.tz.strip()
        zone = _schedules.load_zone(args.tz.strip())
    else:
        zone_name, zone = _schedules.local_zone(env)
    to, to_role = _schedules.validate_recipients(doc, args.to)
    targets = [part.strip() for arg in args.to for part in arg.split(",") if part.strip()]
    name = args.name.strip() if args.name else None
    if name is not None and (not _schedules.NAME_RE.match(name) or _schedules.ID_RE.match(name)):
        raise UsageError("--name takes lowercase letters, digits, dot, dash and underscore (at most 40), and must not look like an id (s12)")
    precheck = args.precheck.strip() if args.precheck else None
    if args.precheck is not None and not precheck:
        raise UsageError("--precheck needs a command")
    timeout_s = _schedules.parse_duration(args.precheck_timeout, "--precheck-timeout", "s", _schedules.MAX_PRECHECK_TIMEOUT_S)
    if timeout_s < 1:
        raise UsageError("--precheck-timeout must be at least 1s")
    grace_s = _schedules.parse_duration(args.grace, "--grace", "m", _schedules.MAX_GRACE_S)
    fires = _schedules.next_fires(spec, now, 3, tz=zone)
    if not fires:
        raise HerdrTeamError("schedule_invalid", "{!r} never fires".format(cron), EXIT_USAGE, {"cron": cron})

    stamp = _schedules.iso_now(now)
    entry: Dict[str, Any] = {
        "id": None, "name": name, "text": text, "cron": cron, "every": args.every, "tz": zone_name,
        "to": targets, "kind": args.kind, "action": _action_of(args),
        "precheck": precheck, "precheck_timeout_s": timeout_s, "grace_s": grace_s, "enabled": not args.disabled,
        "created_at": stamp, "created_by": author.name, "updated_at": stamp,
    }
    # Validates the whole entry as the daemon will read it, and the marked text's length with the longest id.
    probe = _schedules.Schedule.from_json(dict(entry, id="s999999"))
    prepare_text(_schedules.post_text(probe), spill=False, force=True)

    def mutate(document: Dict[str, Any]) -> str:
        rows = document.setdefault("schedules", [])
        if len(rows) >= _schedules.MAX_SCHEDULES:
            raise HerdrTeamError("schedule_limit", "a team keeps at most {} schedules; remove one first".format(_schedules.MAX_SCHEDULES), EXIT_REFUSED)
        if name is not None and any(isinstance(r, dict) and r.get("name") == name for r in rows):
            raise HerdrTeamError("schedule_exists", "a schedule named {!r} already exists".format(name), EXIT_REFUSED, {"name": name})
        entry["id"] = _schedules.allocate_id(document)
        rows.append(entry)
        return str(entry["id"])

    sid = _schedules.update_document(team, mutate)
    sched = _schedules.Schedule.from_json(entry)
    audit(layout, team_name, "schedule_add", author, {"id": sid, "name": name, "cron": cron, "tz": zone_name, "to": targets, "kind": args.kind,
                                                     "precheck": precheck, "enabled": sched.enabled})
    notifier = _notifier_warning(args, layout)
    if len(fires) >= 2 and (fires[1] - fires[0]).total_seconds() < BUSY_INTERVAL_S:
        from herdr_team.cmd_board import warn

        warn(args, "{} fires every {:.0f} min; each fire is a post that wakes its recipients".format(sid, (fires[1] - fires[0]).total_seconds() / 60))
    zone_label = zone_name or "local"
    payload = {
        "team": team_name, "id": sid, "schedule": sched.to_json(), "when": _schedules.describe(sched), "zone": zone_label,
        "to": to, "to_role": to_role, "next": [f.isoformat() for f in fires], "notifier": notifier,
    }

    def human() -> str:
        what = "work item" if sched.action_type == _schedules.ACTION_WORK else args.kind
        lines = ["{} added{}: {} ({}) -> {} as a {}{}".format(
            sid, " ({})".format(name) if name else "", _schedules.describe(sched), zone_label, _to_label(sched), what,
            "" if sched.enabled else " [disabled]")]
        lead = "next:"
        for when in fires:
            lines.append("{:5s} {}".format(lead, _schedules.human_time(when)))
            lead = ""
        if precheck:
            lines.append("precheck: {} (timeout {})".format(precheck, _schedules.format_duration(timeout_s)))
        return "\n".join(lines)

    return emit(args, payload, human)


# --------------------------------------------------------------------------
# list, show, next


def _run_list(args: argparse.Namespace) -> int:
    layout, _api, _author, team_name, team, _doc = _open(args, write=False)
    schedules, invalid = _schedules.load(team)
    state = _schedules.read_state(team)
    now = _schedules._now()
    env = dict(args.env)
    nexts = [_fires(s, now, 1, env) if s.enabled else [] for s in schedules]
    rows = [_row(s, state.get(s.id) or {}, nxt, env) for s, nxt in zip(schedules, nexts)]
    payload = {"team": team_name, "schedules": rows, "invalid": invalid, "path": str(_schedules.definitions_path(team))}

    def human() -> str:
        if not rows and not invalid:
            return "no schedules; add one with: herdr-synapse schedule add \"<text>\" --every weekdays --at 09:00 --to <name|all|role:x>"
        lines = []
        for sched, row, fires in zip(schedules, rows, nexts):
            flags = "".join((" [disabled]" if not sched.enabled else "", " [precheck]" if sched.precheck else ""))
            nxt = "next {}".format(_schedules.human_time(fires[0])) if fires else "no next fire"
            lines.append("{:<5s} {}{}: {} ({}) -> {}  {}; {}{}".format(
                sched.id, sched.name or "", "" if sched.name else "-", row["when"], row["zone"], _to_label(sched), nxt,
                _last_text(row["state"]), flags))
        lines.extend("{} INVALID: {}".format(bad["key"], bad["error"]) for bad in invalid)
        return "\n".join(lines)

    return emit(args, payload, human)


def _run_show(args: argparse.Namespace) -> int:
    layout, _api, _author, team_name, team, _doc = _open(args, write=False)
    sched = _find(team, args.ref)
    state = _schedules.read_state(team).get(sched.id) or {}
    env = dict(args.env)
    fires = _fires(sched, _schedules._now(), 5, env) if sched.enabled else []
    payload = {"team": team_name, "schedule": sched.to_json(), "when": _schedules.describe(sched), "zone": _zone_label(sched, env),
               "next": [f.isoformat() for f in fires], "state": state}

    def human() -> str:
        lines = [
            "{}{}  {}".format(sched.id, " ({})".format(sched.name) if sched.name else "", "enabled" if sched.enabled else "disabled"),
            "when:     {} ({}), cron '{}'".format(payload["when"], payload["zone"], sched.cron),
            "posts:    {} to {}: {}".format(sched.kind, _to_label(sched), sched.text),
            "action:   {}".format(sched.action_type),
            "grace:    {}".format(_schedules.format_duration(sched.grace_s)),
        ]
        if sched.precheck:
            lines.append("precheck: {} (timeout {})".format(sched.precheck, _schedules.format_duration(sched.precheck_timeout_s)))
        lines.append("created:  {} by {}".format(sched.created_at or "?", sched.created_by or "?"))
        lines.append("last:     {}".format(_last_text(state)))
        if state.get("last_detail"):
            lines.append("          {}".format(state["last_detail"]))
        if state.get("last_manual_at"):
            lines.append("manual:   {} {} by {}".format(state.get("last_manual_at"), state.get("last_manual_outcome"), state.get("last_manual_by")))
        for index, when in enumerate(fires):
            lines.append("{:9s} {}".format("next:" if index == 0 else "", _schedules.human_time(when)))
        return "\n".join(lines)

    return emit(args, payload, human)


def _run_next(args: argparse.Namespace) -> int:
    layout, _api, _author, team_name, team, _doc = _open(args, write=False)
    count = int(args.count)
    if not 1 <= count <= MAX_NEXT_COUNT:
        raise UsageError("--count is 1 to {}".format(MAX_NEXT_COUNT))
    schedules, _invalid = _schedules.load(team)
    now = _schedules._now()
    rows = _schedules.upcoming(schedules, now, count, env=dict(args.env))
    items = [{"at": when.isoformat(), "id": sched.id, "name": sched.name, "zone": label, "to": list(sched.to), "kind": sched.kind, "text": sched.text}
             for when, sched, label in rows]
    payload = {"team": team_name, "next": items}

    def human() -> str:
        if not items:
            return "nothing scheduled" if not schedules else "no enabled schedule fires"
        return "\n".join("{}  {} -> {}: {}".format(_schedules.human_time(when), sched.display, _to_label(sched), _one_line(sched.text))
                         for when, sched, _label in rows)

    return emit(args, payload, human)


def _one_line(text: str, limit: int = 80) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# --------------------------------------------------------------------------
# rm, enable, disable, run


def _run_rm(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, _doc = _open(args, write=True)
    sched = _find(team, args.ref)
    _authorize(layout, team_name, author, "rm", False)

    def mutate(document: Dict[str, Any]) -> None:
        document["schedules"] = [r for r in document.get("schedules") or [] if not (isinstance(r, dict) and r.get("id") == sched.id)]

    _schedules.update_document(team, mutate)
    _schedules.update_state(team, {sched.id: None})
    audit(layout, team_name, "schedule_rm", author, {"id": sched.id, "name": sched.name, "cron": sched.cron, "text": sched.text})
    return emit(args, {"team": team_name, "id": sched.id, "removed": sched.to_json()}, "{} removed".format(sched.display))


def _set_enabled(args: argparse.Namespace, enabled: bool) -> int:
    verb = "enable" if enabled else "disable"
    layout, _api, author, team_name, team, _doc = _open(args, write=True)
    sched = _find(team, args.ref)
    _authorize(layout, team_name, author, verb, enabled and bool(sched.precheck))
    now = _schedules._now()
    stamp = _schedules.iso_now(now)
    changed = sched.enabled != enabled

    def mutate(document: Dict[str, Any]) -> None:
        for row in document.get("schedules") or []:
            if isinstance(row, dict) and row.get("id") == sched.id:
                row["enabled"] = enabled
                row["updated_at"] = stamp

    if changed:
        _schedules.update_document(team, mutate)
        if enabled:
            # Armed now: the slots of the time it was off are not owed, and not reported as missed.
            _schedules.update_state(team, {sched.id: {"armed_at": stamp, "disabled_seen": False}})
    audit(layout, team_name, "schedule_{}".format(verb), author, {"id": sched.id, "changed": changed})
    sched.enabled = enabled
    fires = _fires(sched, now, 1, dict(args.env)) if enabled else []
    payload = {"team": team_name, "id": sched.id, "enabled": enabled, "changed": changed, "next": fires[0].isoformat() if fires else None}
    text = "{} {}{}".format(sched.display, verb + "d" if changed else "already " + verb + "d",
                            "; next {}".format(_schedules.human_time(fires[0])) if fires else "")
    if enabled:
        _notifier_warning(args, layout)
    return emit(args, payload, text)


def _run_enable(args: argparse.Namespace) -> int:
    return _set_enabled(args, True)


def _run_disable(args: argparse.Namespace) -> int:
    return _set_enabled(args, False)


def _run_run(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, _doc = _open(args, write=True)
    sched = _find(team, args.ref)
    _authorize(layout, team_name, author, "run", bool(sched.precheck))
    result = _schedules.run_now(layout, team_name, sched, run_by=author.name, env=dict(args.env))
    audit(layout, team_name, "schedule_run", author, {"id": sched.id, "outcome": result["outcome"], "seq": result.get("seq"),
                                                     "precheck": (result.get("precheck") or {}).get("status")})
    if result["outcome"] == "failed":
        check = result.get("precheck") or {}
        raise HerdrTeamError("schedule_precheck_failed", "{}: the precheck {} and nothing was posted{}".format(
            sched.display, "timed out after {}".format(_schedules.format_duration(sched.precheck_timeout_s)) if check.get("status") == "timeout" else "could not run",
            ": {}".format(check.get("output")) if check.get("output") else ""), EXIT_REFUSED, {"id": sched.id, "precheck": check})
    payload = dict(result, team=team_name)
    if result["outcome"] == "skipped":
        text = "{}: the precheck exited {}, so nothing was posted".format(sched.display, (result.get("precheck") or {}).get("exit_code"))
    else:
        text = "{}: posted #{} to {}".format(sched.display, result.get("seq"), ",".join(result.get("to") or []))
    return emit(args, payload, text)


# --------------------------------------------------------------------------
# dispatch

_HANDLERS = {
    "add": _run_add, "list": _run_list, "show": _run_show, "next": _run_next,
    "rm": _run_rm, "enable": _run_enable, "disable": _run_disable, "run": _run_run,
}


def _run(args: argparse.Namespace) -> int:
    return _HANDLERS[getattr(args, "schedule_action", None) or "list"](args)


COMMANDS: List[Command] = [
    Command("schedule", "recurring posts on a timetable: add, list, show, next, run, enable, disable, rm (writes: operator)", _add_arguments, _run,
            description="Recurring posts the notifier writes on time, as the operator: "
                        "herdr-synapse schedule add \"<text>\" (--cron \"M H DOM MON DOW\" | --every hourly|daily|weekdays|weekly) --to <name|all|role:x>. "
                        "See docs/cli.md, section 9l."),
]
