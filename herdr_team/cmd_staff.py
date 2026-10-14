"""Same-team worker staffing, with durable reservations and no broad delegation."""
from __future__ import annotations

import argparse
import copy
import os
from typing import Any, Dict, List

from herdr_team import charter, model_inventory as I, roster, staffing as S, store, work
from herdr_team.cli import Command, add_global_arguments, emit
from herdr_team.cmd_board import _open_team, audit, board_append, build_record, enqueue_job, require_daemon
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, UsageError


def _arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="staff_action", required=True)
    for action in ("status", "pause", "resume", "configure", "add", "retire"):
        child = sub.add_parser(action, allow_abbrev=False)
        add_global_arguments(child, nested=True)
        if action == "configure":
            child.add_argument("--allow-provider", action="append")
            child.add_argument("--max-workers", type=int)
        if action == "add":
            child.add_argument("role")
            child.add_argument("harness", choices=I.KINDS)
            child.add_argument("--name")
            child.add_argument("--model", required=True)
            child.add_argument("--effort")
            child.add_argument("--profile")
            child.add_argument("--mission", required=True)
            child.add_argument("--acceptance", required=True)
            child.add_argument("--request-id", required=True)
            child.add_argument("--work-id")
            child.add_argument("--fallback-for", metavar="MEMBER", help="one manager-chosen fallback for a failed work attempt")
            child.add_argument("--reason", help="required explanation for a fallback")
        if action == "retire":
            child.add_argument("member")


def reconcile(paths: Any, api: Any) -> None:
    """Reconcile on explicit commands only; uncertainty retains capacity and history."""
    team = roster.load_team(paths)
    for request_id, attempt in S.attempts(team).items():
        if attempt.get("phase") not in ("starting", "ambiguous", "retiring"):
            continue
        pane_id, terminal = attempt.get("pane_id"), attempt.get("terminal_id")
        if not pane_id or not terminal:
            continue
        try:
            pane = api.request("pane.get", {"pane_id": pane_id}).get("pane")
        except HerdrTeamError:
            continue
        if not isinstance(pane, dict) or pane.get("terminal_id") != terminal:
            continue
        member = next((m for m in team.members if (m.staffing or {}).get("request_id") == request_id), None)
        if attempt["phase"] == "retiring" and not pane.get("agent"):
            if not member or member.terminal_id != terminal or member.generation != attempt.get("generation", member.generation):
                continue
            try:
                info = api.request("pane.process_info", {"pane_id": pane_id}).get("process_info")
            except HerdrTeamError:
                continue
            processes = info.get("foreground_processes") if isinstance(info, dict) else None
            if not isinstance(processes, list) or any(not isinstance(p, dict) or p.get("pid") != info.get("shell_pid") for p in processes):
                continue
            # Stopped, not merely idle: preserve the row, session, pane and all artifacts.
            S.phase(paths, request_id, "retired")
            def retired(doc: roster.Team) -> None:
                row = doc.find(member.name)
                if row and row.terminal_id == terminal and (row.staffing or {}).get("request_id") == request_id:
                    row.status = "unbound"
            roster.update_team(paths, retired)
        elif member and pane.get("agent") == member.kind:
            try:
                agent = api.request("agent.get", {"target": pane_id}).get("agent")
            except HerdrTeamError:
                continue
            if isinstance(agent, dict) and agent.get("terminal_id") == terminal and agent.get("name") == member.name:
                if attempt["phase"] != "retiring":
                    S.phase(paths, request_id, "active")


def _fallback(paths: Any, team: roster.Team, args: argparse.Namespace) -> Dict[str, Any]:
    if not args.fallback_for:
        return {}
    if not args.reason or not args.work_id:
        raise UsageError("fallback needs --work-id and --reason")
    item = work.load(paths).get(work.parse_id(args.work_id))
    previous = team.find(args.fallback_for)
    if not previous or not previous.staffing or not item or not item.attempts:
        raise HerdrTeamError("fallback_unavailable", "fallback needs an owned worker and its failed work attempt", EXIT_REFUSED)
    attempt = item.attempts[-1]
    if attempt.owner != previous.name or attempt.outcome != "failed" or attempt.gen != previous.generation:
        raise HerdrTeamError("fallback_unavailable", "work is not settled as a failed attempt by this worker", EXIT_REFUSED)
    key = item.id + ":" + str(attempt.n)
    for reservation in S.attempts(team).values():
        if reservation.get("fallback", {}).get("attempt_key") == key:
            if reservation.get("request_id") != args.request_id:
                raise HerdrTeamError("fallback_exhausted", "this work attempt already used its one fallback; the operator decides further retries", EXIT_REFUSED)
    # Quota is often shared across agents/providers; a retry must not disguise it as capacity.
    failure = (args.reason + " " + str(attempt.summary or "")).lower()
    if "quota" in failure or "rate limit" in failure or "rate_limit" in failure or "429" in failure:
        raise HerdrTeamError("fallback_quota", "quota failures need an operator decision, not blind fallback", EXIT_REFUSED)
    return {"attempt_key": key, "previous": previous.name, "reason": args.reason}


def _add(args: argparse.Namespace, layout: Any, api: Any, author: Any, paths: Any) -> Dict[str, Any]:
    from herdr_team import cmd_roster as C, harnesses, instructions_doc

    role = roster.validate_role(args.role)
    team = roster.load_team(paths)
    S.require_manager(team, author)
    config = S.policy(team.config)
    if config is None:
        raise HerdrTeamError("staffing_disabled", "team was not enrolled at creation", EXIT_REFUSED)
    cwd = config["cwd"]
    if not os.path.isdir(cwd):
        raise UsageError("the team's staffing working directory is missing")
    name = args.name or roster.derive_name(team.team, role, team.naming)
    roster.validate_member_name(name)
    if args.request_id not in S.attempts(team) and (paths.instructions(name).exists() or paths.instructions(name).is_symlink()):
        raise HerdrTeamError("worker_instructions_exist", "a document already occupies the new worker's private instructions path; it is not adopted or overwritten", EXIT_REFUSED)
    mission = charter.sanitize(args.mission, charter.MAX_INSTRUCTIONS_CHARS, code="instructions_too_long")
    acceptance = str(args.acceptance or "").strip()
    if not mission.strip() or not acceptance or len(acceptance) > work.MAX_FIELD_CHARS:
        raise UsageError("staff add needs a non-empty Mission and acceptance (at most 1000 characters)")
    profile = harnesses.check_profile(args.harness, args.profile, cwd, dict(args.env), unlisted=False)
    chosen = S.selection(team, args.harness, args.model, args.effort, profile, cwd, dict(args.env))
    fallback = _fallback(paths, team, args)
    work_id = work.parse_id(args.work_id) if args.work_id else None
    if work_id and work_id not in work.load(paths):
        raise HerdrTeamError("work_not_found", "work item does not exist", EXIT_REFUSED)
    # Existing restore/creation operations share this lock. No two processes lay out the same request.
    with store.FileLock(paths.root / "restore.lock", timeout=0, code="staffing_busy"):
        reconcile(paths, api)
        fallback = _fallback(paths, roster.load_team(paths), args)
        attempt = S.reserve(paths, author, args.request_id, name, role, chosen, mission, acceptance, work_id)
        if attempt.get("reused"):
            # Even after a crash, never repeat layout/launch blindly. status reconciles exact identities.
            return {"team": team.team, "attempt": attempt, "reused": True}
        if fallback:
            S.phase(paths, args.request_id, "reserved", fallback=fallback)
        leaf = {"name": name, "role": role, "kind": args.harness, "profile": profile, "cwd": cwd,
                "model": args.model, "effort": args.effort, "launch_model": args.model, "launch_effort": args.effort,
                "permissions": None, "staffing": {"request_id": args.request_id}, "defer_briefing": True,
                "initial_instructions": instructions_doc.to_text([("Mission", (mission + "\n\nAcceptance: " + acceptance).splitlines())])}
        S.phase(paths, args.request_id, "laying_out")
        try:
            applied = api.request("layout.apply", C.build_layout_request(team.team, [leaf], os.fspath(paths.root)), timeout=15)
            root = applied.get("layout", applied).get("root") if isinstance(applied, dict) else None
            pane_ids = C.layout_pane_ids(root)
            if len(pane_ids) != 1:
                raise HerdrTeamError("layout_failed", "staffing layout returned no unique pane", EXIT_REFUSED)
            pane_id = pane_ids[0]
            terminal = C._pane_terminal(api, pane_id)
            if not terminal:
                raise HerdrTeamError("pane_unconfirmed", "new worker terminal cannot be identified; reservation retained", EXIT_REFUSED)
            S.phase(paths, args.request_id, "starting", pane_id=pane_id, terminal_id=terminal)
            fresh = roster.load_team(paths)
            S.require_manager(fresh, author)
            S.launch_guard(fresh, leaf, chosen, starting=True)
            launch_args = copy.copy(args)
            launch_args.steal = False
            outcome = C._spawn_member(layout, api, fresh, paths, leaf, pane_id, {name: mission}, dict(args.env), author, launch_args)
            member = roster.load_team(paths).find(name)
            S.phase(paths, args.request_id, "active" if member and member.status == "active" else "ambiguous",
                    terminal_id=member.terminal_id if member else terminal, generation=member.generation if member else 1)
            if member and member.status == "active":
                # Bind work before a briefing can reach the new worker.
                if work_id:
                    event = {"op": work.OP_ASSIGN, "id": work_id, "by": author.name, "ts": roster.now_iso(), "owner": name}
                    work.append(paths, event)
                else:
                    event = {"op": work.OP_CREATE, "id": None, "by": author.name, "by_via": author.via, "title": role + ": " + mission.splitlines()[0][:150],
                             "brief": {"target": mission[:1000], "acceptance": acceptance, "ownership": name}, "owner": name,
                             "deps": [], "review_by": [team.manager().name] if team.manager() else ["human"]}
                    _before, _after, saved = work.append(paths, event)
                    work_id = saved["id"]
                    S.phase(paths, args.request_id, "active", work_id=work_id)
                from herdr_team import cmd_work
                current_doc = roster.load_team(paths).to_json()
                cmd_work._post(layout, paths, current_doc, author, [name], "request", "{} for you. Done when: {}. Read work show {}".format(work_id, acceptance, work_id), {"id": work_id, "op": "assign"})
                outcome["job"] = C._queue_spawn_briefing(layout, paths, leaf, member, author)
            audit(layout, team.team, "staff_add", author, {"member": name, "request_id": args.request_id, "route": chosen["route"], "fallback": fallback})
            return {"team": team.team, "attempt": S.attempts(roster.load_team(paths))[args.request_id], "outcome": outcome}
        except BaseException:
            S.phase(paths, args.request_id, "ambiguous")
            raise


def _retire(args: argparse.Namespace, layout: Any, api: Any, author: Any, paths: Any) -> Dict[str, Any]:
    from herdr_team import models
    with store.FileLock(paths.root / "restore.lock", timeout=0, code="staffing_busy"):
        reconcile(paths, api)
        team = roster.load_team(paths)
        S.require_manager(team, author)
        member = team.find(args.member)
        if member is None or member.manager or not member.staffing:
            raise HerdrTeamError("staffing_not_owned", "only this team's staffing-owned workers can be retired", EXIT_REFUSED)
        request_id = member.staffing["request_id"]
        if S.attempts(team)[request_id]["phase"] == "retired":
            return {"team": team.team, "member": member.name, "phase": "retired"}
        if any(item.owner == member.name and item.status in work.ACTIVE + (work.STATUS_ASSIGNED,) for item in work.load(paths).values()):
            raise HerdrTeamError("worker_busy", "worker owns active work; settle/reassign it first", EXIT_REFUSED)
        agent = api.request("agent.get", {"target": member.pane_id}).get("agent")
        if not isinstance(agent, dict) or agent.get("terminal_id") != member.terminal_id or agent.get("agent_status") not in ("idle", "done"):
            raise HerdrTeamError("worker_busy", "retirement requires the worker's exact idle/done terminal", EXIT_REFUSED)
        require_daemon(layout.session)
        record = build_record(author, [member.name], "direct", "retire owned worker " + member.name, socket_path=os.fspath(layout.socket), from_gen=author.generation)
        record["control"] = {"action": "retire", "request_id": request_id, "terminal_id": member.terminal_id, "generation": member.generation,
                             "exit": models.exit_keystroke(member.kind)}
        S.phase(paths, request_id, "retiring", name=member.name, terminal_id=member.terminal_id, generation=member.generation, pane_id=member.pane_id)
        seq = board_append(paths, record)
        job = enqueue_job(paths, "control", member.name, author, extra={"seq": seq, "action": "retire"})
        return {"team": team.team, "member": member.name, "phase": "retiring", "job": job, "capacity_released": False}


def _run(args: argparse.Namespace) -> int:
    reading = args.staff_action == "status"
    layout, api, author, team_name, paths, doc = _open_team(args, require_server=not reading, write=not reading)
    team = roster.Team.from_json(doc)
    config = S.policy(team.config)
    if reading:
        payload = {"team": team_name, "enabled": config is not None, "paused": bool((config or {}).get("paused")),
                   "task": (config or {}).get("task"), "acceptance": (config or {}).get("acceptance"),
                   "cwd": (config or {}).get("cwd"), "knowledge_dir": (config or {}).get("knowledge_dir"),
                   "max_workers": (config or {}).get("max_workers"), "used": S.capacity(team),
                   "approved_providers": sorted((config or {}).get("approved", {})), "attempts": S.attempts(team),
                   "limits": "cooperative concurrency only; no spend/total-start/expiry/CLI-subagent cap"}
        return emit(args, payload)
    if config is None:
        raise HerdrTeamError("staffing_disabled", "existing/manual teams are not automatically enrolled", EXIT_REFUSED)
    if args.staff_action == "add":
        return emit(args, _add(args, layout, api, author, paths))
    if args.staff_action == "retire":
        return emit(args, _retire(args, layout, api, author, paths))
    if args.staff_action in ("pause", "resume"):
        S.require_manager(team, author)
        paused = args.staff_action == "pause"
        def update(current: roster.Team) -> None:
            S.require_manager(current, author)
            state = S.policy(current.config)
            if not paused and state.get("paused_by") == "human" and not author.trusted_human:
                raise HerdrTeamError("operator_paused", "only the operator may resume an operator-paused team", EXIT_REFUSED)
            state["paused"] = paused
            state["paused_by"] = author.name if paused else None
        with store.FileLock(paths.root / "restore.lock", timeout=0, code="staffing_busy"):
            roster.update_team(paths, update)
        audit(layout, team_name, "staff_" + args.staff_action, author, {})
        return emit(args, {"team": team_name, "paused": paused, "ongoing_work": "unchanged"})
    S.operator(author)
    approved = S.approve(args.allow_provider, config["cwd"], dict(args.env)) if args.allow_provider else None
    maximum = S.max_workers(args.max_workers) if args.max_workers is not None else config["max_workers"]
    with store.FileLock(paths.root / "restore.lock", timeout=0, code="staffing_busy"):
        def configure(current: roster.Team) -> None:
            policy = S.policy(current.config)
            policy["max_workers"] = maximum
            if approved is not None:
                policy["approved"] = approved
        roster.update_team(paths, configure)
    audit(layout, team_name, "staff_configure", author, {"max_workers": maximum, "providers": sorted(approved or config["approved"])})
    return emit(args, {"team": team_name, "max_workers": maximum, "providers": sorted(approved or config["approved"])})


COMMANDS: List[Command] = [Command("staff", "optional manager staffing: status | add | retire | pause | resume | configure", _arguments, _run)]
