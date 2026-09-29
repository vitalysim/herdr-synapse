"""Restore saved team members into fresh panes without replacing their roster."""
from __future__ import annotations

import argparse
import os
import shutil
from typing import Any, Dict, List, Optional

from herdr_team import features, permissions
from herdr_team import models, roster, store
from herdr_team import cmd_roster as commands
from herdr_team.cli import Command, api_for, emit, layout_for
from herdr_team.cmd_board import audit, check_write_session, env_of
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError


def _arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("team_pos", metavar="team")
    parser.add_argument("--workspace", metavar="ID", help="destination workspace (default: current)")
    parser.add_argument("--dry-run", action="store_true", help="show which members would resume or start fresh without changes")
    parser.add_argument("--refresh-flags", dest="refresh_flags", action="store_true",
                        help="also relaunch running members whose live command line is missing Synapse's launch flags")


def _rows(api: Any, method: str, key: str) -> List[Dict[str, Any]]:
    result = api.request(method, {})
    if not isinstance(result, dict) or not isinstance(result.get(key), list):
        raise HerdrTeamError("restore_unavailable", "{} returned no {} list".format(method, key), EXIT_REFUSED)
    return result[key]


def _relaunch_control(member: Any, model: Optional[str], effort: Optional[str], policy: str,
                      current_argv: Optional[List[str]], mcp: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The ``control`` block of the repair restart, in the shape the notifier validates.

    ``mcp: true`` is the one field that distinguishes it from an ordinary
    controlled restart: it tells the notifier to rebuild the expected argv with
    the team's canvas spec injected rather than carried over from a live
    command line that does not have it.
    """
    return {"action": "restart", "kind": member.kind, "permissions": policy, "model": model, "effort": effort,
            "profile": member.profile, "exit": models.exit_keystroke(member.kind),
            "argv": models.restart_argv(member.kind, member.session, model, effort, current_argv, policy, member.profile, mcp=mcp),
            "preserved": models.preserved_launch_args(member.kind, current_argv, policy, member.profile),
            "after": models.post_start_keystrokes(member.kind, effort), "mcp": bool(mcp)}


def missing_flags_for(team: roster.Team, member: Any, current_argv: Optional[List[str]], mcp: Optional[Dict[str, Any]]) -> List[str]:
    """``models.missing_launch_flags`` for one member, with the team's saved policy applied."""
    if not current_argv:
        return []
    return models.missing_launch_flags(member.kind, current_argv, permissions.effective(team.config, member), mcp)


def empty_labelled_panes(team: roster.Team, agents: List[Dict[str, Any]], panes: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """``{member name: pane row}`` for members whose own labelled pane is still there and empty.

    One matcher, ``roster.rehydrate_match`` step (f), so ``restore`` and the
    notifier never disagree about which pane belongs to whom. A roster written
    before labels existed has its label derived here, exactly as the daemon
    derives it -- filled in on the loaded copy only, which ``restore`` discards;
    nothing is written back.
    """
    members = []
    for member in team.agents():
        if member.label is None and member.role:
            member.label = roster.label_for(team.name, member.role)
        members.append(member)
    found = roster.rehydrate_match(members, agents, panes).empty_panes
    return {str(entry["member"]): entry["pane"] for entry in found if isinstance(entry.get("pane"), dict)}


def plan_restore(team: roster.Team, agents: List[Dict[str, Any]], panes: List[Dict[str, Any]], env: Dict[str, str],
                 mcp: Optional[Dict[str, Any]] = None, refresh_flags: bool = False,
                 live_argv: Optional[Dict[str, List[str]]] = None) -> List[Dict[str, Any]]:
    """Identity evidence wins over cached status; pane IDs alone never identify a member.

    ``mcp`` is ``features.mcp_spec`` for the team: resumed and fresh members
    both get the canvas MCP flags while its canvas is on.

    ``refresh_flags`` is the repair for a member Herdr restored on its own: it
    is running, so the ordinary plan skips it, but its command line came from
    Herdr's canned ``<binary> resume <id>`` and carries none of the flags
    ``models.launch_args`` would have added. With ``live_argv`` mapping a
    member to its foreground argv, such a member is planned as a relaunch
    instead of skipped; a member whose flags are all there stays skipped.

    A member with no live agent whose own labelled pane is still standing is
    planned into *that* pane (``reuse_pane``), so a team comes back in its own
    splits instead of in a new tab beside its empty ones.
    """
    result = []
    reusable = empty_labelled_panes(team, agents, panes)
    for member in team.agents():
        from herdr_team import swap
        if swap.active(member):
            result.append({"name": member.name, "role": member.role, "kind": member.kind, "status": "skipped", "reason": "unfinished swap; use swap --retry"})
            continue
        item: Dict[str, Any] = {"name": member.name, "role": member.role, "kind": member.kind, "cwd": member.cwd}
        live = next((a for a in agents if
                     (member.terminal_id and a.get("terminal_id") == member.terminal_id)
                     or (a.get("agent") == member.kind and roster.same_session_value(roster.session_of(a), member.session))), None)
        pane = next((p for p in panes if member.terminal_id and p.get("terminal_id") == member.terminal_id), None)
        missing_flags = missing_flags_for(team, member, (live_argv or {}).get(member.name), mcp) if refresh_flags else []
        relaunch = bool(missing_flags) and live is not None and isinstance(live.get("pane_id"), str)
        if (live is not None or pane is not None) and not relaunch:
            item.update(status="skipped", reason="already running or its reserved pane still exists", pane_id=(live or pane)["pane_id"])
            if missing_flags:
                item["missing_launch_flags"] = missing_flags
                item["reason"] = "running with {} missing, but it has no pane to relaunch in".format(", ".join(missing_flags))
        elif any(a.get("name") == member.name for a in agents) and not relaunch:
            item.update(status="failed", reason="agent name is already used by another terminal")
        else:
            try:
                model, effort = models.effective_setting(team.config, member)
                # A malformed recorded session is a failure, never permission to silently start fresh.
                mode = "relaunch" if relaunch else ("resumed" if member.session else "fresh")
                if member.session:
                    source = roster.RESUME_COMMANDS.get(member.session.get("source"))
                    if source and source[0] != member.kind:
                        raise HerdrTeamError("session_mismatch", "recorded conversation belongs to a different agent kind", EXIT_REFUSED)
                policy = permissions.effective(team.config, member)
                argv = (models.resume_argv(member.kind, member.session, model, effort, policy, member.profile, mcp=mcp) if member.session
                        else models.fresh_argv(member.kind, model, effort, permissions=policy, profile=member.profile, mcp=mcp))
                if not member.session:
                    argv[0] = next((spec[2][0] for spec in roster.RESUME_COMMANDS.values() if spec[0] == member.kind), argv[0])
                if member.cwd and not os.path.isdir(member.cwd):
                    raise HerdrTeamError("cwd_missing", "working directory is missing: {}".format(member.cwd), EXIT_REFUSED)
                if not shutil.which(argv[0], path=env.get("PATH", os.defpath)):
                    raise HerdrTeamError("command_not_found", "{} is not on PATH".format(argv[0]), EXIT_REFUSED)
                item.update(status="planned", mode=mode, argv=argv, model=model, effort=effort,
                            permissions=permissions.view(team.config, member),
                            expected_terminal=member.terminal_id, expected_generation=member.generation,
                            expected_session=dict(member.session) if member.session else None)
                if relaunch:
                    # The member is running, so nothing here launches anything: the notifier
                    # owns typing into a pane (it is the only caller of ``agent.prompt``), and
                    # it already has an exit-then-resume path that waits for the member to be
                    # idle. This records that restart, with the canvas spec carried explicitly
                    # because the live argv is precisely what is missing it.
                    item.update(missing_launch_flags=missing_flags, pane_id=live["pane_id"],
                                control=_relaunch_control(member, model, effort, policy,
                                                          (live_argv or {}).get(member.name), mcp))
                elif member.name in reusable:
                    item["reuse_pane"] = dict(reusable[member.name])
            except HerdrTeamError as err:
                item.update(status="failed", reason=err.message)
        result.append(item)
    return result


def _reserve(book: roster.Roster, item: Dict[str, Any], pane: Dict[str, Any]) -> None:
    def mutate(team: roster.Team) -> None:
        member = team.find(item["name"])
        if (member is None or member.status == "left" or member.terminal_id != item["expected_terminal"]
                or member.generation != item["expected_generation"]):
            raise HerdrTeamError("member_changed", "{} changed during restore; refresh and retry".format(item["name"]), EXIT_REFUSED)
        member.terminal_id = pane["terminal_id"]
        member.pane_id = pane["pane_id"]
        member.tab_id = pane.get("tab_id")
        member.workspace_id = pane.get("workspace_id")
        member.status = "starting"
        if item["mode"] == "fresh":
            member.session = None
            member.briefed_at = None
            member.briefing_seq = None
    book.update(mutate)
    item.update(pane_id=pane["pane_id"], terminal_id=pane["terminal_id"])


def _brief(book: roster.Roster, item: Dict[str, Any], author: Any) -> None:
    from herdr_team.cmd_board import board_append, build_record, enqueue_job
    keys = models.post_start_keystrokes(item["kind"], item["effort"])
    if keys:
        record = build_record(author, [item["name"]], "direct", "restore: apply recorded effort", socket_path=os.fspath(book.layout.socket))
        record["control"] = {"action": "model", "kind": item["kind"], "model": None,
                             "effort": item["effort"], "setting": models.label(item["model"], item["effort"]),
                             "keystrokes": keys, "brief_after": True}
        seq = board_append(book.paths, record)
        enqueue_job(book.paths, "control", item["name"], author, extra={"seq": seq, "action": "model", "swap_id": item.get("swap_id")})
    else:
        roster.write_briefing_job(book.paths, item["name"], requested_by=commands._requested_by(author))


def _launch(book: roster.Roster, api: Any, item: Dict[str, Any], author: Any) -> None:
    from . import session_names
    args, naming = item["argv"][1:], None
    if item["mode"] == "fresh":
        try:
            args, naming = session_names.prepare(item["kind"], item["name"], args)
        except (OSError, HerdrTeamError) as err:
            item["naming_warning"] = str(err)
    started = commands._start_agent(api, item["name"], item["kind"], item["pane_id"], args=args)
    if started.get("terminal_id") and started["terminal_id"] != item["terminal_id"]:
        def moved(team: roster.Team) -> None:
            member = team.find(item["name"])
            if member is None or member.status == "left" or member.terminal_id != item["terminal_id"]:
                raise HerdrTeamError("member_changed", "member changed while starting", EXIT_REFUSED)
            member.terminal_id = started["terminal_id"]
        book.update(moved)
        item["terminal_id"] = started["terminal_id"]
    resolved = commands._wait_for_agent(api, item["pane_id"], item["kind"], commands.AGENT_START_TIMEOUT_MS / 1000.0)
    if resolved is None:
        raise HerdrTeamError("agent_not_ready", "agent did not become ready; inspect its reserved pane", EXIT_REFUSED)
    if started.get("terminal_id") and resolved.terminal_id != started["terminal_id"]:
        raise HerdrTeamError("member_changed", "terminal changed while starting", EXIT_REFUSED)
    current = book.load().find(item["name"])
    if current is None or current.status == "left" or current.terminal_id != item["terminal_id"]:
        raise HerdrTeamError("member_changed", "member changed while starting", EXIT_REFUSED)
    if item["mode"] in ("resumed", "relaunch") and item["expected_session"] and resolved.agent_session and not roster.same_session_value(item["expected_session"], resolved.agent_session):
        raise HerdrTeamError("session_mismatch", "agent reported a different conversation; inspect its pane", EXIT_REFUSED)
    book.bind(api, item["name"], resolved, socket=os.fspath(book.layout.socket), expected_terminal=item["terminal_id"], expected_generation=item["expected_generation"])
    if naming is not None:
        try:
            session_names.arm(book.paths, book.load().find(item["name"]), naming, resolved.agent_session)
        except (OSError, HerdrTeamError) as err:
            item["naming_warning"] = str(err)
    _brief(book, item, author)
    item["status"] = item["mode"]


def _mark_failed(book: roster.Roster, item: Dict[str, Any]) -> None:
    def failed(team: roster.Team) -> None:
        member = team.find(item["name"])
        if member and member.status != "left" and member.terminal_id == item.get("terminal_id"):
            member.status = "failed"

    book.update(failed)


def _plan(api: Any, book: roster.Roster, env: Dict[str, str], mcp: Optional[Dict[str, Any]], refresh: bool) -> List[Dict[str, Any]]:
    """``plan_restore`` with the live foreground argv of every running member, but only when asked.

    ``pane.process_info`` is one socket call per member; without
    ``--refresh-flags`` nothing reads the result, so nothing asks for it.
    """
    team = book.load()
    agents = _rows(api, "agent.list", "agents")
    panes = _rows(api, "pane.list", "panes")
    live_argv: Dict[str, List[str]] = {}
    if refresh:
        for member in team.agents():
            pane_id = member.pane_id
            if not isinstance(pane_id, str) or not pane_id:
                continue
            if not any(a.get("pane_id") == pane_id and a.get("terminal_id") == member.terminal_id for a in agents):
                continue
            try:
                info = api.request("pane.process_info", {"pane_id": pane_id}, timeout=5.0).get("process_info")
            except HerdrTeamError:
                continue
            argv = models.foreground_argv(member.kind, info.get("foreground_processes") if isinstance(info, dict) else None)
            if argv:
                live_argv[member.name] = argv
    return plan_restore(team, agents, panes, env, mcp=mcp, refresh_flags=refresh, live_argv=live_argv)


def _request_relaunch(book: roster.Roster, item: Dict[str, Any], author: Any) -> None:
    """Record the repair restart of a running member for the notifier to carry out.

    ``restore`` does not type into a pane and does not stop a process: the
    notifier owns both, it waits for the member to be idle first, and it
    revalidates the whole control block against the roster before it acts. So
    the repair is a board record plus a control job, exactly as ``models
    --apply restart`` writes one -- the difference is only which flags the argv
    carries and why.
    """
    from herdr_team.cmd_board import board_append, build_record, enqueue_job

    control = item["control"]
    record = build_record(author, [item["name"]], "direct",
                          "restart {} to restore its launch flags ({}): {}".format(
                              item["name"], ", ".join(item["missing_launch_flags"]), " ".join(control["argv"])),
                          socket_path=os.fspath(book.layout.socket))
    record["control"] = control
    seq = board_append(book.paths, record)
    job = enqueue_job(book.paths, "control", item["name"], author, extra={"seq": seq, "action": "restart"})
    item.update(status="relaunch", record_seq=seq, job=job,
                reason="the notifier exits it when idle and resumes its session with the missing flags")


def _report(args: argparse.Namespace, items: List[Dict[str, Any]]) -> None:
    counts = {key: sum(i["status"] == key for i in items) for key in ("resumed", "relaunch", "fresh", "skipped", "failed")}
    args.stderr.write("restore: " + ", ".join("{} {}".format(n, key) for key, n in counts.items()) + "\n")
    args.stderr.flush()


def _run(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    api = api_for(args, layout)
    env = env_of(args)
    name = commands._team_arg(args, layout, args.team_pos)
    check_write_session(args, layout, name)
    author = commands._author(args, layout, api, team=name)
    commands._human_only(layout, name, author, "restore")
    book = roster.Roster(layout, name)
    book.load()  # Refuse missing/dissolved teams before creating a lock directory.
    mcp = features.mcp_spec(layout, book.paths)
    refresh = bool(getattr(args, "refresh_flags", False))
    if args.dry_run:
        items = _plan(api, book, env, mcp, refresh)
        return emit(args, {"team": name, "dry_run": True, "members": items}, "\n".join("{}: {} ({})".format(i["name"], i["status"], i.get("mode", i.get("reason", ""))) for i in items))
    with store.FileLock(book.paths.root / "restore.lock", timeout=0, code="restore_busy"):
        items = _plan(api, book, env, mcp, refresh)
        pending = [i for i in items if i["status"] == "planned"]
        commands._ensure_daemon(layout, env)
        workspace = args.workspace or env.get("HERDR_WORKSPACE_ID")
        tabs = []
        # A running member with missing flags is repaired by the notifier, in its own pane.
        for item in [i for i in pending if i["mode"] == "relaunch"]:
            try:
                _request_relaunch(book, item, author)
            except HerdrTeamError as err:
                item.update(status="failed", reason=err.message)
        # A member whose own pane is still standing goes back into it: no new split, no new tab.
        in_place = [i for i in pending if i["status"] == "planned" and isinstance(i.get("reuse_pane"), dict)
                    and (not workspace or i["reuse_pane"].get("workspace_id") in (None, workspace))]
        for item in in_place:
            pane = item["reuse_pane"]
            try:
                if not pane.get("terminal_id"):
                    raise HerdrTeamError("terminal_unknown", "{}'s own pane has no terminal identity".format(item["name"]), EXIT_REFUSED)
                _reserve(book, item, pane)
                if pane.get("tab_id") not in tabs:
                    tabs.append(pane.get("tab_id"))
            except HerdrTeamError as err:
                item.update(status="failed", reason=err.message, pane_id=pane.get("pane_id"))
        for item in in_place:
            if item["status"] != "planned":
                continue
            args.stderr.write("restoring {} ({}) in its own pane {}…\n".format(item["name"], item["mode"], item["pane_id"]))
            args.stderr.flush()
            try:
                _launch(book, api, item, author)
            except HerdrTeamError as err:
                item.update(status="failed", reason=err.message)
                _mark_failed(book, item)
            _report(args, items)
        done = {id(i) for i in in_place}
        pending = [i for i in pending if i["status"] == "planned" and id(i) not in done and i["mode"] != "relaunch"]
        for offset in range(0, len(pending), commands.MAX_LAYOUT_LEAVES):
            group = pending[offset:offset + commands.MAX_LAYOUT_LEAVES]
            request = commands.build_layout_request(name, group, os.fspath(book.paths.root), workspace)
            if offset:
                request["tab_label"] += ":{}".format(offset // commands.MAX_LAYOUT_LEAVES + 1)
            applied = api.request("layout.apply", request, timeout=15.0)
            pane_ids = commands.layout_pane_ids(applied.get("layout", applied).get("root"))
            if len(pane_ids) != len(group):
                raise HerdrTeamError("layout_failed", "restore layout returned an unexpected number of panes", EXIT_REFUSED, {"pane_ids": pane_ids})
            # Reserve every pane before any potentially slow agent start.
            for item, pane_id in zip(group, pane_ids):
                try:
                    pane = api.request("pane.get", {"pane_id": pane_id})["pane"]
                    if not pane.get("terminal_id"):
                        raise HerdrTeamError("terminal_unknown", "new pane has no terminal identity", EXIT_REFUSED)
                    _reserve(book, item, pane)
                    if pane.get("tab_id") not in tabs:
                        tabs.append(pane.get("tab_id"))
                except HerdrTeamError as err:
                    item.update(status="failed", reason=err.message, pane_id=pane_id)
            for item in group:
                if item["status"] != "planned":
                    continue
                args.stderr.write("restoring {} ({})…\n".format(item["name"], item["mode"]))
                args.stderr.flush()
                try:
                    _launch(book, api, item, author)
                except HerdrTeamError as err:
                    item.update(status="failed", reason=err.message)
                    _mark_failed(book, item)
                _report(args, items)
        payload = {"team": name, "tabs": tabs, "members": items,
                   "counts": {key: sum(i["status"] == key for i in items) for key in ("resumed", "relaunch", "fresh", "skipped", "failed")}}
        audit(layout, name, "restore", author, {"counts": payload["counts"], "tabs": tabs})
        emit(args, payload, "\n".join("{}: {}{}".format(i["name"], i["status"], " — " + i["reason"] if i.get("reason") else "") for i in items) or "no agent members to restore")
        return EXIT_REFUSED if payload["counts"]["failed"] else 0


COMMANDS = [Command("restore", "restore missing team agents in new tabs (human only)", _arguments, _run)]
