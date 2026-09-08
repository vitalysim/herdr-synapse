"""Author resolution for every CLI invocation (plan 4.3).

Tiers, in order:

1. ``HERDR_PLUGIN_EVENT`` or ``HERDR_PLUGIN_ID`` without
   ``HERDR_PLUGIN_ENTRYPOINT_ID`` -> ``system`` (never a member, never
   human).
2. ``HERDR_PLUGIN_ENTRYPOINT_ID=console`` in the terminal recorded in
   ``console.json`` (confirmed by ``pane.get`` on ``HERDR_PANE_ID`` and
   ``pane.process_info`` on that pane) -> ``human``; ``verified`` only when
   the pane reports ``focused: true`` at Enter, else ``console-unfocused``.
   Any other entrypoint (compose, picker) -> ``human`` via ``popup``,
   unverified.
3. ``HERDR_PANE_ID`` -> ``pane.get`` (alias-tolerant after a move) -> the
   current ``pane_id``, ``terminal_id``, ``agent``. Agent present:
   ``agent.get`` for the live name, roster member by ``terminal_id`` (else
   ``HERDR_TEAM_MEMBER`` unverified, else ``not_a_member``); ``--as human``
   is refused with ``author_mismatch`` and audited. Agent absent: candidate
   human, verified when the pane's foreground process group is ours and
   the group leader's parent is the pane shell; otherwise
   ``cli-unverified`` with a reason, never refused.
4. Nothing set -> ``--team`` or ``--session`` required (``team_required``);
   ``human`` via ``outside``, unverified.

``--as <member>`` is never accepted (the CLI has no such flag).
``from_label`` (``--name`` or ``HERDR_TEAM_HUMAN``) is honoured only for
verified human posts. ``--relayed-for human`` is accepted only from a
member and recorded as ``relayed_for``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from herdr_team import sanitize, store
from herdr_team.errors import EXIT_REFUSED, EXIT_UNREACHABLE, HerdrTeamError
from herdr_team.paths import Layout, ensure_team_dirs, env_workspace, team_name_from_arg
from herdr_team import operator as _operator
from herdr_team import roster as _roster

VIA_CLI = "cli"
VIA_CLI_UNVERIFIED = "cli-unverified"
VIA_CONSOLE = "console"
VIA_CONSOLE_UNFOCUSED = "console-unfocused"
VIA_POPUP = "popup"
VIA_OUTSIDE = "outside"
VIA_HOOK = "hook"
VIA_SYSTEM = "system"

AUTHOR_HUMAN = "human"
AUTHOR_SYSTEM = "system"

TIER_SYSTEM = 1
TIER_CONSOLE = 2
TIER_PANE = 3
TIER_OUTSIDE = 4

CONSOLE_ENTRYPOINT = "console"
POPUP_ENTRYPOINTS = ("compose", "picker", "who")

LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}\Z")
ANCESTRY_MAX_DEPTH = 16
PS_TIMEOUT_S = 2.0


@dataclass
class Author:
    """Who is running this command, and how sure we are."""

    name: str  # member name, "human", or "system"
    kind: Optional[str]  # agent kind label, "human", or None
    via: str  # one of the VIA_* constants
    verified: bool
    pane_id: Optional[str] = None
    terminal_id: Optional[str] = None
    workspace_id: Optional[str] = None
    tab_id: Optional[str] = None
    from_label: Optional[str] = None
    team: Optional[str] = None
    reason: Optional[str] = None  # why unverified, printed to stderr
    origin: Dict[str, Any] = field(default_factory=dict)  # the record's ``origin`` object
    tier: int = 0
    relayed_for: Optional[str] = None
    generation: Optional[int] = None
    #: A live delegation from the operator (``herdr_team.operator``). Only ever
    #: set on a verified roster member, and only by an explicit grant.
    operator: bool = False

    @property
    def is_human(self) -> bool:
        return self.name == AUTHOR_HUMAN

    @property
    def trusted_human(self) -> bool:
        """The operator, from an origin the board's own reader rule trusts.

        ``is_human`` is a name, and a name was what every authority gate
        tested -- so a process that reached an entrypoint tier with one
        variable set was the operator to charter, rules, manager and grant
        writes alike. This applies ``human_origin_ok`` to the author the way
        readers apply it to a record: console, popup, outside, or a shell
        whose ancestry was confirmed. A shell that could not be confirmed is
        still *named* human and still posts; it just carries no authority.
        """
        return self.is_human and human_origin_ok({"via": self.via, "verified": self.verified})

    @property
    def is_system(self) -> bool:
        return self.name == AUTHOR_SYSTEM

    @property
    def is_member(self) -> bool:
        return self.name not in (AUTHOR_HUMAN, AUTHOR_SYSTEM)

    def to_json(self) -> Dict[str, Any]:
        obj: Dict[str, Any] = {
            "name": self.name, "kind": self.kind, "via": self.via, "verified": self.verified,
            "pane_id": self.pane_id, "terminal_id": self.terminal_id, "team": self.team, "tier": self.tier,
        }
        if self.from_label:
            obj["from_label"] = self.from_label
        if self.relayed_for:
            obj["relayed_for"] = self.relayed_for
        if self.operator:
            obj["operator"] = True
        if self.reason:
            obj["reason"] = self.reason
        return obj


# --------------------------------------------------------------------------
# helpers


def sanitize_label(label: Optional[str]) -> Optional[str]:
    """``[A-Za-z0-9._-]{1,32}``; ``label_invalid`` otherwise (``sanitize.sanitize_label``)."""
    if label is None or label == "":
        return None
    return sanitize.sanitize_label(label)


def _base_origin(via: str, verified: bool, layout: Layout) -> Dict[str, Any]:
    return {
        "via": via, "verified": bool(verified), "pid": os.getpid(), "ppid": os.getppid(),
        "workspace_id": None, "tab_id": None, "socket": os.fspath(layout.socket),
    }


def ps_table() -> Optional[Dict[int, int]]:
    """``{pid: ppid}`` for every process visible to ``ps``; None when ``ps`` is unavailable."""
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=PS_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    table: Dict[int, int] = {}
    for line in completed.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            table[int(parts[0])] = int(parts[1])
        except ValueError:
            continue
    return table or None


def ancestry(pid: int, table: Optional[Dict[int, int]], max_depth: int = ANCESTRY_MAX_DEPTH) -> List[int]:
    """``[pid, ppid, ...]`` up to ``max_depth`` levels using ``table`` (own ppid when the table is missing)."""
    chain = [pid]
    if table is None:
        if pid == os.getpid():
            chain.append(os.getppid())
        return chain
    current = pid
    for _ in range(max_depth):
        parent = table.get(current)
        if parent is None or parent <= 1 or parent == current:
            if parent == 1:
                chain.append(1)
            break
        chain.append(parent)
        current = parent
    return chain


def _process_info(api: Any, pane_id: str) -> Optional[Dict[str, Any]]:
    try:
        result = api.request("pane.process_info", {"pane_id": pane_id})
    except HerdrTeamError:
        return None
    info = result.get("process_info") if isinstance(result, dict) else None
    return info if isinstance(info, dict) else None


def verify_shell_ancestry(api: Any, pane_id: str, own_pgrp: int, table: Optional[Dict[int, int]] = None) -> Tuple[bool, Optional[str]]:
    """``pane process-info``: foreground pgid equals ours and the leader's parent is ``shell_pid``.

    Returns ``(verified, reason)``; the reason explains an unverified result.
    """
    info = _process_info(api, pane_id)
    if info is None:
        return False, "pane process-info unavailable for {}".format(pane_id)
    fg_pgid = info.get("foreground_process_group_id")
    shell_pid = info.get("shell_pid")
    if fg_pgid is None:
        return False, "pane {} reports no foreground process group".format(pane_id)
    if int(fg_pgid) != int(own_pgrp):
        return False, "foreground process group {} of pane {} is not ours ({})".format(fg_pgid, pane_id, own_pgrp)
    if shell_pid is None:
        return False, "pane {} reports no shell pid".format(pane_id)
    leader = int(fg_pgid)
    if leader == os.getpid():
        leader_parent: Optional[int] = os.getppid()
    else:
        resolved = table if table is not None else ps_table()
        leader_parent = resolved.get(leader) if resolved else None
    if leader_parent is None:
        return False, "cannot determine the parent of process group leader {}".format(leader)
    if int(leader_parent) != int(shell_pid):
        return False, "process group leader {} is not a child of the pane shell {}".format(leader, shell_pid)
    return True, None


def confirm_pane_ancestry(api: Any, pane_id: str, own_pid: Optional[int] = None, table: Optional[Dict[int, int]] = None) -> Tuple[Optional[bool], Optional[str]]:
    """Is this process a descendant of the pane's foreground job or shell?

    ``(True, None)`` when an ancestor is a foreground process or the shell,
    ``(False, reason)`` when process info is definitive and none is,
    ``(None, reason)`` when it cannot be determined.
    """
    info = _process_info(api, pane_id)
    if info is None:
        return None, "pane process-info unavailable for {}".format(pane_id)
    pids = set()
    for proc in info.get("foreground_processes") or []:
        if isinstance(proc, dict) and isinstance(proc.get("pid"), int):
            pids.add(proc["pid"])
    if isinstance(info.get("shell_pid"), int):
        pids.add(info["shell_pid"])
    if isinstance(info.get("foreground_process_group_id"), int):
        pids.add(info["foreground_process_group_id"])
    # init is every process's ancestor, so a pane reporting pid 1 (or 0) would
    # otherwise claim every caller in the session as its own descendant.
    pids = {p for p in pids if p > 1}
    if not pids:
        return None, "pane {} reports no usable processes".format(pane_id)
    resolved = table if table is not None else ps_table()
    if resolved is None:
        return None, "ps unavailable"
    chain = ancestry(own_pid if own_pid is not None else os.getpid(), resolved)
    if any(p in pids for p in chain):
        return True, None
    return False, "this process is not a descendant of pane {}".format(pane_id)


# --------------------------------------------------------------------------
# audit


#: Agent panes examined before giving up on the "am I inside one?" question.
MAX_ANCESTRY_PANES = 24


def hosting_agent_pane(api: Any, own_pid: Optional[int] = None, table: Optional[Dict[int, int]] = None) -> Optional[Dict[str, Any]]:
    """The agent pane this process is running inside, when there is one.

    An agent's shell command is a descendant of that agent's own pane whatever
    the environment says. ``HERDR_PANE_ID`` is set by Herdr, but a member can
    unset it, and every human-only gate tests only that the author is *named*
    ``human`` -- so dropping one variable used to turn any member into the
    operator. The process tree cannot be unset the same way, so it is what
    decides here.

    Returns the ``pane.list`` row on positive evidence, else None. Absence of
    evidence is not evidence of absence: an unreachable server or a missing
    ``ps`` leaves the caller exactly where it was, because refusing on a
    failed lookup would lock the operator out of their own CLI.
    """
    try:
        result = api.request("pane.list", {})
    except HerdrTeamError:
        return None
    panes = result.get("panes") if isinstance(result, dict) else None
    if not isinstance(panes, list):
        return None
    resolved = table if table is not None else ps_table()
    if resolved is None:
        return None
    pid = own_pid if own_pid is not None else os.getpid()
    checked = 0
    for pane in panes:
        if not isinstance(pane, dict) or not pane.get("agent") or not isinstance(pane.get("pane_id"), str):
            continue
        checked += 1
        if checked > MAX_ANCESTRY_PANES:
            break
        inside, _why = confirm_pane_ancestry(api, pane["pane_id"], own_pid=pid, table=resolved)
        if inside is True:
            return pane
    return None


def audit(layout: Layout, team: Optional[str], event: str, author: Author, details: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Append one line to ``<team>/audit.jsonl``; returns the path (None without a team)."""
    if not team:
        return None
    try:
        team_paths = layout.team(team)
    except HerdrTeamError:
        return None
    ensure_team_dirs(team_paths)
    entry = {
        "ts": _roster.now_iso(),
        "event": event,
        "author": author.name,
        "via": author.via,
        "pane_id": author.pane_id,
        "terminal_id": author.terminal_id,
        "verified": bool(author.verified),
        "details": dict(details or {}),
    }
    store.append_line(team_paths.audit_jsonl, json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), fsync=False)
    return os.fspath(team_paths.audit_jsonl)


def read_audit(layout: Layout, team: str, last: Optional[int] = None) -> List[Dict[str, Any]]:
    raw = store.read_bytes(layout.team(team).audit_jsonl, default=b"") or b""
    entries: List[Dict[str, Any]] = []
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    if last is not None and last >= 0:
        entries = entries[-last:] if last else []
    return entries


def refuse_as_human(layout: Layout, author: Author, details: Optional[Dict[str, Any]] = None) -> HerdrTeamError:
    """``author_mismatch`` for ``--as human`` from a member, system, or hook path; audited."""
    info: Dict[str, Any] = {"requested": "human", "resolved": author.name, "via": author.via}
    if details:
        info.update(details)
    audit(layout, author.team, "author_mismatch", author, info)
    return HerdrTeamError(
        "author_mismatch",
        "this pane is {!r}, not the human; use --relayed-for human to relay the operator".format(author.name),
        EXIT_REFUSED,
        {"author": author.name, "via": author.via, "pane_id": author.pane_id},
    )


# --------------------------------------------------------------------------
# roster lookups


def _teams_to_search(layout: Layout, team: Optional[str]) -> List[str]:
    if team:
        return [team]
    return layout.session.list_teams()


def find_member(layout: Layout, terminal_id: Optional[str], team: Optional[str] = None) -> Optional[Tuple[str, _roster.Member]]:
    """``(team, member)`` for a live terminal id: pane record first, then every roster."""
    if not terminal_id:
        return None
    record = _roster.read_pane_record(layout.session, terminal_id)
    candidates: List[str] = []
    if isinstance(record, dict) and isinstance(record.get("team"), str) and (team is None or record["team"] == team):
        candidates.append(record["team"])
    for name in _teams_to_search(layout, team):
        if name not in candidates:
            candidates.append(name)
    for name in candidates:
        try:
            doc = _roster.load_team(layout.team(name))
        except HerdrTeamError:
            continue
        member = doc.find_by_terminal(terminal_id)
        if member is not None:
            return name, member
    return None


def _pane_get(api: Any, pane_id: str) -> Optional[Dict[str, Any]]:
    result = api.request("pane.get", {"pane_id": pane_id})
    pane = result.get("pane") if isinstance(result, dict) else None
    return pane if isinstance(pane, dict) else None


def _agent_name(api: Any, pane_id: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    try:
        result = api.request("agent.get", {"target": pane_id})
    except HerdrTeamError:
        return None, None
    agent = result.get("agent") if isinstance(result, dict) else None
    if not isinstance(agent, dict):
        return None, None
    return agent.get("name"), agent


def _env_team(env: Dict[str, str], team: Optional[str]) -> Optional[str]:
    if team:
        return team
    try:
        return team_name_from_arg(None, env)
    except HerdrTeamError:
        return None


def _apply_label(author: Author, label: Optional[str], env: Dict[str, str]) -> Author:
    raw = label if label is not None else env.get("HERDR_TEAM_HUMAN")
    if not raw:
        return author
    clean = sanitize_label(raw)
    if author.is_human and author.verified:
        author.from_label = clean
    else:
        author.origin["ignored_label"] = clean
        note = "--name ignored on an unverified or non-human path"
        author.reason = "{}; {}".format(author.reason, note) if author.reason else note
    return author


def _apply_operator_grant(author: Author, layout: Layout) -> Author:
    """Mark a member the operator has delegated its authority to.

    Only a *verified* roster member can hold one: an identity taken from
    ``HERDR_TEAM_MEMBER`` is a claim, not evidence, and a grant must not be
    reachable by claiming to be the member that holds it.
    """
    if not author.is_member or not author.verified or not author.team:
        return author
    grant = _operator.active(layout.session, author.team, author.name)
    if grant is None:
        return author
    author.operator = True
    author.origin["operator"] = {"granted_at": grant.get("granted_at"), "expires_at": grant.get("expires_at")}
    return author


def _apply_relay(author: Author, relayed_for: Optional[str]) -> Author:
    if relayed_for is None:
        return author
    if relayed_for != AUTHOR_HUMAN:
        raise HerdrTeamError("usage", "--relayed-for accepts only 'human'", 2, {"relayed_for": relayed_for})
    if not author.is_member:
        raise HerdrTeamError("author_mismatch", "--relayed-for human is for members relaying the operator; you are {!r}".format(author.name), EXIT_REFUSED, {"author": author.name})
    author.relayed_for = AUTHOR_HUMAN
    author.origin["relayed_for"] = AUTHOR_HUMAN
    return author


# --------------------------------------------------------------------------
# the tiers


def _tier_system(env: Dict[str, str], layout: Layout, team: Optional[str], as_human: bool) -> Author:
    via = VIA_HOOK if env.get("HERDR_PLUGIN_EVENT") else VIA_SYSTEM
    author = Author(AUTHOR_SYSTEM, None, via, True, team=_env_team(env, team), tier=TIER_SYSTEM)
    author.origin = _base_origin(via, True, layout)
    author.origin["event"] = env.get("HERDR_PLUGIN_EVENT")
    author.origin["plugin_id"] = env.get("HERDR_PLUGIN_ID")
    author.origin["pane_id"] = env.get("HERDR_PANE_ID")
    if as_human:
        raise refuse_as_human(layout, author, {"tier": TIER_SYSTEM})
    return author


def _tier_console(env: Dict[str, str], layout: Layout, api: Any, team: Optional[str], require_server: bool) -> Author:
    from herdr_team import cmd_board as _console_registry

    inside = hosting_agent_pane(api)
    if inside is not None:
        # The entrypoint variable is Herdr's, but an agent can set it too, and
        # naming the real console's pane would then pass the registry check
        # below and land as ``console-unfocused``, which readers trust. The
        # process tree settles it: a real console descends from the Herdr
        # server, a forgery descends from the agent's own pane.
        return _rerouted_agent_author(env, layout, api, inside, _env_team(env, team), False, "console entrypoint set inside an agent pane")
    console = store.read_json(layout.session.console_json, default=None)
    console = console if isinstance(console, dict) else {}
    default_team = console.get("default_team") if isinstance(console.get("default_team"), str) else None
    resolved_team = team or default_team or _env_team(env, None)
    pane_env = env.get("HERDR_PANE_ID")
    author = Author(AUTHOR_HUMAN, "human", VIA_CONSOLE_UNFOCUSED, False, pane_id=pane_env, team=resolved_team, tier=TIER_CONSOLE)
    author.origin = _base_origin(VIA_CONSOLE_UNFOCUSED, False, layout)
    author.origin["entrypoint"] = CONSOLE_ENTRYPOINT
    if not pane_env:
        author.reason = "console process has no HERDR_PANE_ID"
        return author
    pane: Optional[Dict[str, Any]] = None
    try:
        pane = _pane_get(api, pane_env)
    except HerdrTeamError as err:
        if err.exit_code == EXIT_UNREACHABLE and not require_server:
            author.reason = "Herdr unreachable: {}".format(err.message)
            return author
        raise
    if pane is None:
        author.reason = "pane {} not found".format(pane_env)
        return author
    author.pane_id = pane.get("pane_id") or pane_env
    author.terminal_id = pane.get("terminal_id")
    author.workspace_id = pane.get("workspace_id")
    author.tab_id = pane.get("tab_id")
    author.origin.update({"pane_id": author.pane_id, "terminal_id": author.terminal_id, "workspace_id": author.workspace_id, "tab_id": author.tab_id})
    # Membership in the console registry, not equality with a single recorded
    # terminal: several consoles may be open, one per team. The entry must also
    # still be running, so a stale record cannot make a dead pane's terminal
    # verify. Ancestry and focus below remain the real gates.
    entry = _console_registry.console_entry(layout.session, author.terminal_id)
    live = bool(entry) and bool(entry.get("open")) and _console_registry._pid_is_alive(entry.get("pid"))
    if not live:
        known = sorted(_console_registry.live_console_entries(layout.session))
        author.via = VIA_CLI_UNVERIFIED
        author.origin["via"] = VIA_CLI_UNVERIFIED
        author.reason = "this pane is {!r}; live consoles are {}".format(author.terminal_id, known or "none")
        return author
    # A console is pinned to the team it was opened for.
    if isinstance(entry.get("team"), str) and entry["team"] and not team:
        author.team = entry["team"]
    confirmed, reason = confirm_pane_ancestry(api, author.pane_id)
    # ``say`` needs a positive answer: ``None`` (no process info, no ``ps``) stays an unfocused-grade author there.
    author.origin["ancestry"] = "confirmed" if confirmed else ("unconfirmed" if confirmed is False else "unavailable")
    if confirmed is False:
        # Definitively not a descendant of the console pane: whatever this
        # process is, it is not the console, so it does not get the console's
        # trusted origin. ``None`` (no process info) keeps the benefit of the
        # doubt, as everywhere else.
        author.via = VIA_CLI_UNVERIFIED
        author.origin["via"] = VIA_CLI_UNVERIFIED
        author.reason = reason
        return author
    if pane.get("focused") is True:
        author.via = VIA_CONSOLE
        author.verified = True
        author.origin["via"] = VIA_CONSOLE
        author.origin["verified"] = True
    else:
        author.reason = "console pane not focused at Enter"
    return author


def _space_team(layout: Layout, workspace_id: Optional[str]) -> Optional[str]:
    """The team of ``workspace_id``, ranked above the session's ``default_team``.

    A human has no roster row to be looked up in, so their team used to come
    from one session-wide default -- which is wrong in every space but one as
    soon as a session holds two teams. Deferred import: ``cmd_board`` imports
    this module.
    """
    from herdr_team.cmd_board import workspace_team

    try:
        return workspace_team(layout.session, workspace_id)
    except (HerdrTeamError, OSError):
        return None


def _tier_popup(env: Dict[str, str], layout: Layout, api: Any, team: Optional[str]) -> Author:
    inside = hosting_agent_pane(api)
    if inside is not None:
        # A popup has no pane identity to check, which is exactly why this
        # tier used to make zero socket calls -- and why setting the variable
        # from an agent's shell made that agent the operator to every gate.
        # Same rule as ``_tier_outside``: the process tree cannot be unset.
        return _rerouted_agent_author(env, layout, api, inside, _env_team(env, team), False, "popup entrypoint set inside an agent pane")
    console = store.read_json(layout.session.console_json, default=None)
    default_team = console.get("default_team") if isinstance(console, dict) and isinstance(console.get("default_team"), str) else None
    space_team = _space_team(layout, env_workspace(env))
    author = Author(AUTHOR_HUMAN, "human", VIA_POPUP, False, team=team or space_team or default_team or _env_team(env, None), tier=TIER_CONSOLE)
    author.origin = _base_origin(VIA_POPUP, False, layout)
    author.origin["entrypoint"] = env.get("HERDR_PLUGIN_ENTRYPOINT_ID")
    context = env.get("HERDR_PLUGIN_CONTEXT_JSON")
    if context:
        try:
            parsed = json.loads(context)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            author.origin["focused_pane_id"] = parsed.get("focused_pane_id")
            author.origin["nonce"] = parsed.get("nonce") or parsed.get("launch_nonce")
    author.reason = "popup launches carry no pane identity"
    return author


def _tier_pane(env: Dict[str, str], layout: Layout, api: Any, team: Optional[str], as_human: bool, require_server: bool, team_explicit: bool = True) -> Author:
    pane_env = str(env.get("HERDR_PANE_ID"))
    env_team = _env_team(env, team)
    try:
        pane = _pane_get(api, pane_env)
    except HerdrTeamError as err:
        if err.exit_code == EXIT_UNREACHABLE and not require_server:
            return _offline_pane_author(env, layout, env_team, pane_env, err.message)
        if err.code in ("pane_not_found", "invalid_pane_id", "pane_target_not_found"):
            raise HerdrTeamError("not_a_member", "pane {} not found on this session ({})".format(pane_env, err.message), EXIT_UNREACHABLE, {"pane_id": pane_env, "herdr_code": err.code})
        raise
    if pane is None:
        raise HerdrTeamError("not_a_member", "pane {} not found on this session".format(pane_env), EXIT_UNREACHABLE, {"pane_id": pane_env})
    current_pane = str(pane.get("pane_id") or pane_env)
    terminal_id = pane.get("terminal_id")
    origin = _base_origin(VIA_CLI, False, layout)
    origin.update({
        "pane_id": current_pane, "terminal_id": terminal_id,
        "workspace_id": pane.get("workspace_id"), "tab_id": pane.get("tab_id"),
    })
    if current_pane != pane_env:
        origin["claimed_pane_id"] = pane_env
    mismatches = {}
    for key, field_name in (("HERDR_WORKSPACE_ID", "workspace_id"), ("HERDR_TAB_ID", "tab_id")):
        value = env.get(key)
        if value and pane.get(field_name) and value != pane.get(field_name):
            mismatches[key] = {"env": value, "pane": pane.get(field_name)}
    if mismatches:
        origin["env_mismatch"] = mismatches

    if pane.get("agent"):
        return _agent_pane_author(env, layout, api, pane, current_pane, origin, env_team, as_human, team_explicit)
    return _shell_pane_author(env, layout, api, pane, current_pane, origin, env_team)


def _offline_pane_author(env: Dict[str, str], layout: Layout, env_team: Optional[str], pane_env: str, message: str) -> Author:
    member_env = env.get("HERDR_TEAM_MEMBER")
    if member_env and env_team:
        try:
            _roster.validate_member_name(member_env)
        except HerdrTeamError:
            member_env = None
    if member_env and env_team:
        author = Author(member_env, env.get("HERDR_TEAM_KIND"), VIA_CLI, False, pane_id=pane_env, team=env_team, tier=TIER_PANE)
        author.origin = _base_origin(VIA_CLI, False, layout)
        author.origin["pane_id"] = pane_env
        author.origin["source"] = "env:HERDR_TEAM_MEMBER"
        author.reason = "Herdr unreachable ({}); identity from HERDR_TEAM_MEMBER".format(message)
        return author
    author = Author(AUTHOR_HUMAN, "human", VIA_CLI_UNVERIFIED, False, pane_id=pane_env, team=env_team, tier=TIER_PANE)
    author.origin = _base_origin(VIA_CLI_UNVERIFIED, False, layout)
    author.origin["pane_id"] = pane_env
    author.reason = "Herdr unreachable ({}); pane identity unverified".format(message)
    return author


def _agent_pane_author(env: Dict[str, str], layout: Layout, api: Any, pane: Dict[str, Any], current_pane: str, origin: Dict[str, Any], env_team: Optional[str], as_human: bool, team_explicit: bool = True) -> Author:
    kind = pane.get("agent")
    terminal_id = pane.get("terminal_id")
    live_name, _agent = _agent_name(api, current_pane)
    origin["agent"] = kind
    origin["live_name"] = live_name
    found = find_member(layout, terminal_id, env_team)
    if found is None and env_team is not None and not team_explicit:
        # The hint was only the session's ``default_team``: plan 4.3 ranks the roster match by
        # terminal id above it, and a terminal sits in at most one roster, so widening the search
        # cannot mis-attribute. Without this a member of any non-default team was ``not_a_member``
        # from its own pane (M5 rig finding).
        found = find_member(layout, terminal_id, None)
    if found is not None:
        team_name, member = found
        author = Author(member.name, member.kind, VIA_CLI, True, pane_id=current_pane, terminal_id=terminal_id,
                        workspace_id=pane.get("workspace_id"), tab_id=pane.get("tab_id"), team=team_name, tier=TIER_PANE,
                        generation=member.generation)
        origin["verified"] = True
        origin["generation"] = member.generation
        author.origin = origin
        if live_name and live_name != member.name and not member.retired_name_active(live_name):
            author.reason = "live agent name {!r} differs from roster name {!r}".format(live_name, member.name)
        if kind and kind != member.kind:
            author.reason = "live agent kind {!r} differs from roster kind {!r}".format(kind, member.kind)
        confirmed, reason = confirm_pane_ancestry(api, current_pane)
        origin["ancestry"] = "confirmed" if confirmed else ("unconfirmed" if confirmed is False else "unavailable")
        if confirmed is False:
            audit(layout, team_name, "pane_mismatch", author, {"reason": reason, "claimed_pane_id": origin.get("claimed_pane_id")})
            raise HerdrTeamError("pane_mismatch", "HERDR_PANE_ID names pane {} but this process does not run in it".format(current_pane), EXIT_REFUSED, {"pane_id": current_pane, "reason": reason})
        if as_human:
            raise refuse_as_human(layout, author, {"tier": TIER_PANE, "kind": kind})
        return author
    member_env = env.get("HERDR_TEAM_MEMBER")
    if member_env and env_team:
        try:
            _roster.validate_member_name(member_env)
        except HerdrTeamError:
            member_env = None
    if member_env and env_team:
        author = Author(member_env, kind, VIA_CLI, False, pane_id=current_pane, terminal_id=terminal_id,
                        workspace_id=pane.get("workspace_id"), tab_id=pane.get("tab_id"), team=env_team, tier=TIER_PANE)
        origin["source"] = "env:HERDR_TEAM_MEMBER"
        author.origin = origin
        author.reason = "terminal {} is not in the roster of {!r}; identity from HERDR_TEAM_MEMBER".format(terminal_id, env_team)
        if as_human:
            raise refuse_as_human(layout, author, {"tier": TIER_PANE, "kind": kind})
        return author
    if as_human:
        probe = Author(live_name or "agent", kind, VIA_CLI, False, pane_id=current_pane, terminal_id=terminal_id, team=env_team, tier=TIER_PANE)
        probe.origin = origin
        raise refuse_as_human(layout, probe, {"tier": TIER_PANE, "kind": kind, "member": False})
    raise HerdrTeamError(
        "not_a_member",
        "pane {} hosts a {} agent that is not in any team".format(current_pane, kind),
        EXIT_UNREACHABLE,
        {"pane_id": current_pane, "terminal_id": terminal_id, "agent": kind, "name": live_name, "hint": {"teams": layout.session.list_teams()}},
    )


def _shell_pane_author(env: Dict[str, str], layout: Layout, api: Any, pane: Dict[str, Any], current_pane: str, origin: Dict[str, Any], env_team: Optional[str]) -> Author:
    terminal_id = pane.get("terminal_id")
    team_name = env_team
    if team_name is None:
        team_name = _space_team(layout, pane.get("workspace_id"))
    if team_name is None:
        console = store.read_json(layout.session.console_json, default=None)
        if isinstance(console, dict) and isinstance(console.get("default_team"), str):
            team_name = console["default_team"]
    verified, reason = verify_shell_ancestry(api, current_pane, os.getpgrp())
    if not verified:
        # A verified shell is the foreground job of this very pane, so it cannot
        # also be a descendant of some agent's pane and the scan is skipped. An
        # unverified one might be an agent's subprocess pointed at a shell pane.
        inside = hosting_agent_pane(api)
        if inside is not None and inside.get("pane_id") != current_pane:
            return _rerouted_agent_author(env, layout, api, inside, env_team, False, "HERDR_PANE_ID names another pane")
    via = VIA_CLI if verified else VIA_CLI_UNVERIFIED
    origin["via"] = via
    origin["verified"] = bool(verified)
    origin["pgrp"] = os.getpgrp()
    author = Author(AUTHOR_HUMAN, "human", via, bool(verified), pane_id=current_pane, terminal_id=terminal_id,
                    workspace_id=pane.get("workspace_id"), tab_id=pane.get("tab_id"), team=team_name, tier=TIER_PANE)
    author.origin = origin
    author.reason = reason
    # A post typed in a dead member's pane is the human's, with that pane as origin.
    former = None
    if terminal_id:
        for name in _teams_to_search(layout, env_team):
            try:
                doc = _roster.load_team(layout.team(name))
            except HerdrTeamError:
                continue
            member = doc.find_by_terminal(terminal_id, include_left=True)
            if member is not None:
                former = (name, member.name)
                break
    if former is not None:
        origin["former_member"] = {"team": former[0], "name": former[1]}
        if author.team is None:
            author.team = former[0]
    return author


def _tier_outside(env: Dict[str, str], layout: Layout, api: Any, team: Optional[str], as_human: bool = False) -> Author:
    resolved_team = _env_team(env, team)
    if resolved_team is None and not layout.env_socket.explicit and layout.state_root.team_dir is None:
        raise HerdrTeamError(
            "team_required",
            "not inside a Herdr pane: pass --team <name|path> or --session <name>",
            EXIT_UNREACHABLE,
            {"teams": layout.session.list_teams()},
        )
    inside = hosting_agent_pane(api)
    if inside is not None:
        # No pane in the environment, but the process tree says otherwise: this is
        # an agent's own subprocess with the variable stripped. It gets its own
        # identity back, not the operator's.
        return _rerouted_agent_author(env, layout, api, inside, resolved_team, as_human, "no HERDR_PANE_ID")
    author = Author(AUTHOR_HUMAN, "human", VIA_OUTSIDE, False, team=resolved_team, tier=TIER_OUTSIDE)
    author.origin = _base_origin(VIA_OUTSIDE, False, layout)
    author.reason = "outside Herdr"
    return author


def _rerouted_agent_author(env: Dict[str, str], layout: Layout, api: Any, pane: Dict[str, Any], env_team: Optional[str], as_human: bool, why: str) -> Author:
    """Resolve as the agent whose pane this process actually runs in."""
    pane_id = str(pane.get("pane_id"))
    origin = _base_origin(VIA_CLI, False, layout)
    origin.update({
        "pane_id": pane_id, "terminal_id": pane.get("terminal_id"),
        "workspace_id": pane.get("workspace_id"), "tab_id": pane.get("tab_id"),
        "rerouted": why,
    })
    return _agent_pane_author(env, layout, api, pane, pane_id, origin, env_team, as_human, team_explicit=env_team is not None)


def resolve_author(
    env: Dict[str, str],
    layout: Layout,
    api: Any,
    team: Optional[str] = None,
    as_human: bool = False,
    relayed_for: Optional[str] = None,
    label: Optional[str] = None,
    require_server: bool = True,
    team_explicit: bool = True,
) -> Author:
    """Apply the tiers; raise ``not_a_member`` (3), ``author_mismatch`` (1), ``team_required`` (3).

    ``team_explicit`` says whether ``team`` came from ``--team`` / ``HERDR_TEAM`` /
    ``HERDR_TEAM_DIR`` (True) or only from the session's ``default_team`` (False);
    a member pane in another team is still resolved in the latter case.
    """
    env = dict(env)
    entrypoint = env.get("HERDR_PLUGIN_ENTRYPOINT_ID")
    if (env.get("HERDR_PLUGIN_EVENT") or env.get("HERDR_PLUGIN_ID")) and not entrypoint:
        author = _tier_system(env, layout, team, as_human)
    elif entrypoint == CONSOLE_ENTRYPOINT:
        author = _tier_console(env, layout, api, team, require_server)
    elif entrypoint:
        author = _tier_popup(env, layout, api, team)
    elif env.get("HERDR_PANE_ID"):
        author = _tier_pane(env, layout, api, team, as_human, require_server, team_explicit)
    else:
        author = _tier_outside(env, layout, api, team, as_human)
    if as_human and not author.is_human:
        raise refuse_as_human(layout, author, {"tier": author.tier})
    author = _apply_relay(author, relayed_for)
    author = _apply_label(author, label, env)
    author = _apply_operator_grant(author, layout)
    if author.origin:
        author.origin["via"] = author.via
        author.origin["verified"] = bool(author.verified)
    return author


def authority_refusal(action: str, author: Author) -> str:
    """Why an authority write was refused, with the way out for a real operator.

    A member is told the action is human only. A human whose shell could not be
    verified is told how to be trusted: the console, a focused pane, or the
    ``outside`` tier -- each still process-tree-checked, so none of them is a
    way in for an agent.
    """
    if author.is_human:
        why = " ({})".format(author.reason) if author.reason else ""
        return ("{} needs the operator from a trusted origin, and this shell could not be verified as yours{}. "
                "Use the team console, a focused Herdr pane, or run it outside Herdr: "
                "env -u HERDR_PANE_ID herdr-synapse --team <team> ...").format(action, why)
    return "{} is human only; this pane is {!r} ({})".format(action, author.name, author.via)


def human_origin_ok(record_origin: Dict[str, Any]) -> bool:
    """The one rule for trusting ``from: human``: console, popup, outside, or a verified shell.

    Readers apply it to a record's ``origin`` (``daemon._counts_for_nudges``,
    ``asks.answered_by``); the authority gates apply it to the author through
    ``Author.trusted_human``. Two callers, one answer.
    """
    via = record_origin.get("via")
    if via in (VIA_CONSOLE, VIA_CONSOLE_UNFOCUSED, VIA_POPUP, VIA_OUTSIDE):
        return True
    return via == VIA_CLI and bool(record_origin.get("verified"))
