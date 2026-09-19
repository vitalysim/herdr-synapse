"""Durable creation of a fresh agent that takes over one logical team member."""
from __future__ import annotations

import copy
import os
import shutil
import uuid
from typing import Any, Dict, Optional

from herdr_team import models, permissions, roster, store
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError

KINDS = models.KINDS


def active(member: Any) -> bool:
    operation = member.get("swap") if isinstance(member, dict) else getattr(member, "swap", None)
    return isinstance(operation, dict) and operation.get("phase") not in ("complete", "cancelled")


def require_available(member: Any) -> None:
    if active(member):
        raise HerdrTeamError("swap_busy", "member has an unfinished swap; use swap --status or --retry", EXIT_REFUSED)


def current(team: Any, name: str) -> Dict[str, Any]:
    doc = store.RosterStore(team).load()
    return next((m for m in doc.get("members", []) if m.get("name") == name), {})


def stale_job(member: Dict[str, Any], job: Dict[str, Any]) -> bool:
    operation = member.get("swap") or {}
    if job.get("kind") not in ("say", "control", "probe") or not operation:
        return False
    if job.get("target_generation") is not None and job["target_generation"] != member.get("generation", 1):
        return True
    if job.get("swap_id") == operation.get("id") and operation.get("phase") in ("briefing", "complete"):
        return False
    seq = job.get("seq")
    return active(member) or not isinstance(seq, int) or seq <= operation.get("cutoff_seq", 0)


def plan(team: roster.Team, name: str, kind: str, setting: Optional[str], env: Dict[str, str]) -> Dict[str, Any]:
    member = team.find(name)
    if member is None or member.is_human or member.status == "left":
        raise HerdrTeamError("member_not_found", "{} is not an agent member".format(name), EXIT_REFUSED)
    require_available(member)
    if kind not in KINDS:
        raise HerdrTeamError("swap_kind_unsupported", "choose {}".format(", ".join(KINDS)), EXIT_REFUSED)
    # Saved overrides are native to their harness. Never carry them across kinds.
    saved = next((p for p in reversed(member.agent_history) if p.get("kind") == kind), {})
    dest = {"kind": kind, "model": saved.get("model"), "effort": saved.get("effort")}
    if kind == member.kind:
        dest.update(model=member.model, effort=member.effort)
    if setting:
        model, effort = models.parse_setting(setting)
        dest.update(model=model, effort=effort)
    model, effort = models.effective_setting(team.config, dest)
    models.validate(kind, model, effort)
    argv = models.fresh_argv(kind, model, effort, permissions=permissions.effective(team.config, member))
    if not shutil.which(argv[0], path=env.get("PATH", os.defpath)):
        raise HerdrTeamError("command_not_found", "{} is not on PATH".format(argv[0]), EXIT_REFUSED)
    if not member.cwd or not os.path.isdir(member.cwd):
        raise HerdrTeamError("cwd_missing", "member working directory is missing", EXIT_REFUSED)
    return {"member": name, "name": name, "role": member.role, "kind": kind, "model": model, "effort": effort,
            "argv": argv, "cwd": member.cwd, "source": member.to_json(),
            "permissions": permissions.view(team.config, dict(kind=kind, permissions=member.permissions)),
            "workspace_id": member.workspace_id, "fresh": True}


def change(book: roster.Roster, name: str, operation_id: str, mutate: Any) -> Dict[str, Any]:
    def update(doc: Dict[str, Any]) -> None:
        member = next((m for m in doc.get("members", []) if m.get("name") == name), None)
        if member is None or member.get("status") == "left" or (member.get("swap") or {}).get("id") != operation_id:
            raise HerdrTeamError("member_changed", "member changed during swap", EXIT_REFUSED)
        mutate(member, member["swap"])
    doc = store.RosterStore(book.paths).update(update, swap_id=operation_id)
    return next(m["swap"] for m in doc["members"] if m.get("name") == name)


def phase(book: roster.Roster, name: str, op: Dict[str, Any], value: str, **fields: Any) -> Dict[str, Any]:
    return change(book, name, op["id"], lambda m, s: s.update(phase=value, error=None, **fields))


def prepare(book: roster.Roster, spec: Dict[str, Any], author: Any, note: str = "") -> Dict[str, Any]:
    from herdr_team.cmd_hooks import render_board_context
    source = copy.deepcopy(spec["source"])
    source.pop("swap", None)
    source.pop("agent_history", None)
    records = store.BoardStore(book.paths).read(last=100, include_retracted=False)
    relevant = [r for r in records if r.get("from") == spec["member"] or spec["member"] in r.get("to", []) or "all" in r.get("to", [])]
    handoff = render_board_context(relevant[-20:], max_posts=20, max_bytes=6000)
    op = {"id": uuid.uuid4().hex, "phase": "prepared", "source": source,
          "destination": {k: v for k, v in spec.items() if k not in ("source", "member")},
          "created_at": roster.now_iso(), "requested_by": author.to_json(),
          "cutoff_seq": max((r.get("seq", 0) for r in records), default=0),
          "handoff": handoff, "note": note, "pane": None}
    def update(doc: Dict[str, Any]) -> None:
        member = next((m for m in doc.get("members", []) if m.get("name") == spec["member"]), None)
        if member is None or member.get("status") == "left" or member.get("terminal_id") != source.get("terminal_id") or member.get("generation", 1) != source["generation"]:
            raise HerdrTeamError("member_changed", "member changed while planning the swap", EXIT_REFUSED)
        require_available(member)
        member["swap"] = op
    store.RosterStore(book.paths).update(update)
    return op


def pane_at(api: Any, pane_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not pane_id:
        return None
    try:
        return api.request("pane.get", {"pane_id": pane_id}).get("pane")
    except HerdrTeamError as err:
        if err.code == "pane_not_found":
            return None
        raise


def rows(api: Any, method: str, key: str) -> list:
    result = api.request(method, {})
    if not isinstance(result, dict) or not isinstance(result.get(key), list):
        raise HerdrTeamError("herdr_protocol", "{} returned no {} list".format(method, key), EXIT_REFUSED)
    return result[key]


def source_pane(api: Any, source: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pane = pane_at(api, source.get("pane_id"))
    agents = rows(api, "agent.list", "agents")
    views = [p for p in rows(api, "pane.list", "panes") if p.get("terminal_id") == source.get("terminal_id")]
    if len(views) > 1:
        raise HerdrTeamError("swap_shared_terminal", "source terminal has multiple pane views; close its other views before swapping", EXIT_REFUSED)
    for agent in agents:
        if agent.get("terminal_id") == source.get("terminal_id") or roster.same_session_value(roster.session_of(agent), source.get("session")):
            if pane is None or agent.get("pane_id") != pane.get("pane_id"):
                raise HerdrTeamError("member_changed", "source agent moved; rebind it before swapping", EXIT_REFUSED)
    if pane is not None and pane.get("terminal_id") != source.get("terminal_id"):
        raise HerdrTeamError("member_changed", "source pane now has a different terminal; nothing was closed", EXIT_REFUSED)
    if pane is not None and pane.get("agent") not in (None, source["kind"]):
        raise HerdrTeamError("member_changed", "source pane now hosts a different agent; nothing was closed", EXIT_REFUSED)
    live = next((a for a in agents if pane and a.get("pane_id") == pane.get("pane_id")), None)
    if live and ((live.get("name") and live["name"] != source.get("name")) or (roster.session_of(live) and source.get("session") and not roster.same_session_value(roster.session_of(live), source["session"]))):
        raise HerdrTeamError("member_changed", "source conversation changed; refresh before swapping", EXIT_REFUSED)
    return pane


def allocate(book: roster.Roster, api: Any, name: str, op: Dict[str, Any]) -> Dict[str, Any]:
    from herdr_team import cmd_roster as commands
    # A labelled tab makes an ambiguous layout timeout recoverable without spawning duplicates.
    label = "swap:" + op["id"][:16]
    tabs = rows(api, "tab.list", "tabs")
    existing = [t for t in tabs if t.get("label") == label]
    if len(existing) > 1:
        raise HerdrTeamError("swap_layout_ambiguous", "multiple swap tabs found; inspect them before retrying", EXIT_REFUSED)
    if existing:
        panes = [p for p in rows(api, "pane.list", "panes") if p.get("tab_id") == existing[0]["tab_id"]]
        if len(panes) != 1:
            raise HerdrTeamError("swap_layout_changed", "reserved swap tab no longer has one pane", EXIT_REFUSED)
        pane = panes[0]
    else:
        request = commands.build_layout_request(book.name, [op["destination"]], os.fspath(book.paths.root), op["destination"].get("workspace_id"))
        request["tab_label"] = label
        result = api.request("layout.apply", request, timeout=15)
        ids = commands.layout_pane_ids(result.get("layout", result).get("root"))
        if len(ids) != 1:
            raise HerdrTeamError("layout_failed", "swap did not create exactly one pane", EXIT_REFUSED)
        pane = pane_at(api, ids[0])
    if not pane or not pane.get("terminal_id") or pane.get("agent"):
        raise HerdrTeamError("swap_pane_busy", "replacement pane must be an empty shell", EXIT_REFUSED)
    return phase(book, name, op, "stopping", pane=pane)


def cancel(book: roster.Roster, api: Any, name: str, op: Dict[str, Any]) -> Dict[str, Any]:
    """Abandon a pre-takeover attempt; retain the source conversation for resume."""
    step = op.get("resume_phase") if op.get("phase") == "failed" else op.get("phase")
    if step not in ("prepared", "stopping", "starting"):
        raise HerdrTeamError("swap_already_bound", "takeover has completed; use --retry to finish its briefing", EXIT_REFUSED)
    try:
        source = source_pane(api, op["source"])
    except HerdrTeamError as err:
        if err.code not in ("member_changed", "swap_shared_terminal"):
            raise
        # Cancellation never closes the source. Release its reservation so
        # the operator can rebind a moved source after abandoning the swap.
        source = None
    reserved = op.get("pane")
    if reserved:
        pane = pane_at(api, reserved["pane_id"])
        if pane and pane.get("terminal_id") != reserved["terminal_id"]:
            raise HerdrTeamError("swap_pane_changed", "reserved terminal changed; nothing was closed", EXIT_REFUSED)
        if pane and pane.get("agent"):
            target = roster.resolve_target(api, pane["pane_id"])
            if not op.get("launch_attempted") or target.name != name or target.kind != op["destination"]["kind"]:
                raise HerdrTeamError("swap_pane_changed", "reserved pane hosts an unrelated agent; nothing was closed", EXIT_REFUSED)
        if pane:
            api.request("pane.close", {"pane_id": pane["pane_id"]})
            if pane_at(api, pane["pane_id"]) is not None:
                raise HerdrTeamError("swap_stop_failed", "replacement pane still exists", EXIT_REFUSED)
    elif step == "prepared":
        # A layout request may have succeeded before its response was lost.
        # Resolve that allocation first so cancellation cannot leak a reserved tab.
        tabs = rows(api, "tab.list", "tabs")
        if any(t.get("label") == "swap:" + op["id"][:16] for t in tabs):
            return cancel(book, api, name, allocate(book, api, name, op))
    def abandon(member: Dict[str, Any], state: Dict[str, Any]) -> None:
        state.update(phase="cancelled", error=None)
        member["status"] = "active" if source and source.get("agent") else "missing"
    return change(book, name, op["id"], abandon)


def execute(book: roster.Roster, api: Any, name: str, op: Dict[str, Any], author: Any, progress: Any) -> Dict[str, Any]:
    from herdr_team import cmd_restore, cmd_roster as commands
    if op.get("phase") == "failed":
        op = phase(book, name, op, op["resume_phase"])
    try:
        if op["phase"] == "prepared":
            source_pane(api, op["source"])
            progress("creating replacement pane")
            op = allocate(book, api, name, op)
        if op["phase"] == "stopping":
            pane = source_pane(api, op["source"])
            if pane:
                progress("closing outgoing agent pane")
                api.request("pane.close", {"pane_id": pane["pane_id"]})
                if pane_at(api, pane["pane_id"]) is not None:
                    raise HerdrTeamError("swap_stop_failed", "outgoing pane still exists", EXIT_REFUSED)
            op = phase(book, name, op, "starting")
        if op["phase"] == "starting":
            dest, reserved = op["destination"], op["pane"]
            pane = pane_at(api, reserved["pane_id"])
            if not pane or pane.get("terminal_id") != reserved["terminal_id"]:
                raise HerdrTeamError("swap_pane_changed", "replacement terminal changed; inspect its tab", EXIT_REFUSED)
            progress("starting fresh {}".format(dest["kind"]))
            if not pane.get("agent"):
                # Persist before launch; a retry observes this same reserved terminal.
                from herdr_team import session_names
                if "launch_args" not in op:
                    launch_args, naming = session_names.prepare(dest["kind"], name, dest["argv"][1:])
                    op = phase(book, name, op, "starting", launch_args=launch_args, naming=naming)
                op = phase(book, name, op, "starting", launch_attempted=True)
                commands._start_agent(api, name, dest["kind"], pane["pane_id"], args=op["launch_args"])
            elif not op.get("launch_attempted"):
                raise HerdrTeamError("swap_pane_busy", "reserved pane was occupied before this operation launched", EXIT_REFUSED)
            target = roster.resolve_target(api, pane["pane_id"])
            if target.terminal_id != reserved["terminal_id"] or target.kind != dest["kind"] or target.name != name:
                raise HerdrTeamError("swap_pane_changed", "replacement identity does not match this swap", EXIT_REFUSED)
            if target.agent_session and roster.same_session_value(target.agent_session, op["source"].get("session")):
                raise HerdrTeamError("swap_session_reused", "replacement reported the outgoing conversation", EXIT_REFUSED)
            def takeover(member: Dict[str, Any], state: Dict[str, Any]) -> None:
                history = list(member.get("agent_history") or [])
                history.append(dict(state["source"], replaced_at=roster.now_iso()))
                member.update(kind=target.kind, terminal_id=target.terminal_id, pane_id=target.pane_id,
                              workspace_id=target.workspace_id, tab_id=target.tab_id, session=target.agent_session,
                              model=dest["model"], effort=dest["effort"], managed=True, status="active",
                              generation=int(state["source"].get("generation") or 1) + 1,
                              verified_kind=roster._kind_verified(book.layout, target.kind),
                              briefed_at=None, briefing_seq=None, charter_seq_acked=None,
                              instructions_seq_acked=None, rules_seq_acked=None, agent_history=history)
                state.update(phase="briefing", error=None)
            op = change(book, name, op["id"], takeover)
        if op["phase"] == "briefing":
            progress("preparing handoff")
            member = book.load().find(name)
            if member is None:
                raise HerdrTeamError("member_not_found", name, EXIT_REFUSED)
            roster.label_pane(api, member.pane_id, member.label)
            roster.execute_token_commands(api, roster.token_commands(member, book.name, color_slot=book.load().color_slot))
            roster.remove_pane_record(book.layout.session, op["source"].get("terminal_id"))
            roster.write_pane_record(book.layout.session, member.terminal_id, book.name, name, member.generation, member.session)
            if op.get("naming"):
                from herdr_team import session_names
                session_names.arm(book.paths, member, op["naming"], member.session)
            # Brief jobs may be retried. The daemon merges them; ordinary mail remains on the board.
            cmd_restore._brief(book, dict(op["destination"], name=name, swap_id=op["id"]), author)
            announced = any(r.get("event") == "agent_swapped" and r.get("swap_id") == op["id"] for r in store.BoardStore(book.paths).read(last=100))
            if not announced:
                roster.append_system_record(book.paths, "agent_swapped", "{}: created fresh {} to replace {}; handoff queued".format(name, member.kind, op["source"]["kind"]),
                                            to=["all"], extra={"member": name, "swap_id": op["id"]}, socket=os.fspath(book.layout.socket))
            op = phase(book, name, op, "complete", completed_at=roster.now_iso())
        return op
    except (HerdrTeamError, OSError) as err:
        latest = current(book.paths, name).get("swap") or op
        if latest.get("phase") != "complete":
            change(book, name, op["id"], lambda m, s: s.update(phase="failed", resume_phase=latest["phase"], error=str(err)))
        raise
