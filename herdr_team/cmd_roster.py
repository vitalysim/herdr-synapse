"""Command group: create add remove leave bind dissolve use teams rename me who audit charter brief.

Roster writes go through ``herdr_team.roster`` (``Roster``, ``resolve_target``,
``claim_check``, token projection, briefing jobs); charter and brief writes
through ``herdr_team.charter``; author resolution and the shared board
helpers come from ``herdr_team.cmd_board``'s support layer. Every command's
``--json`` shape follows ``docs/cli.md`` sections 4 to 6.

``create`` validates every role and name (grammar, reserved words, kind
labels, live-name collisions, duplicates within the call) before anything
touches Herdr, so a refusal leaves no half-named team (plan 5.2, 5.5).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time as _time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from herdr_team import SKILL_VERSION, VERSION
from herdr_team import api as _api
from herdr_team import charter as _charter
from herdr_team import cli as _cli
from herdr_team import daemon as _daemon
from herdr_team import identity as _identity
from herdr_team import paths as _paths
from herdr_team import render as _render
from herdr_team import roster as _roster
from herdr_team import store
from herdr_team.cli import api_for, emit, layout_for
from herdr_team.cmd_board import (
    AUTHOR_HUMAN,
    addressed_to,
    agent_members,
    audit,
    board_read_all,
    charter_headline,
    charter_of,
    charter_summary,
    cli_path,
    cursor_get,
    check_write_session,
    cursors_all,
    daemon_status,
    default_team_of,
    enqueue_job,
    env_of,
    load_doc,
    members_of,
    mute_state,
    notifier_state,
    nudges_state,
    read_console_json,
    read_task,
    render_charter,
    resolve_author,
    resolve_team,
    toast_delivery,
    unread_for,
    view_state,
    warn,
    write_console_json,
)
from herdr_team.errors import EXIT_REFUSED, EXIT_UNREACHABLE, HerdrTeamError, UsageError
from herdr_team.identity import Author
from herdr_team.paths import Layout, TeamPaths


from herdr_team.cli import Command

MAX_LAYOUT_LEAVES = 24
AGENT_START_TIMEOUT_MS = 60000
AGENT_START_RETRY_S = 0.5
AGENT_START_RETRY_WINDOW_S = 10.0
#: ``create --new``: ``starting`` members are polled by pane id this often up to the start timeout (plan 5.2).
STARTING_POLL_S = 5.0
#: ``--from-workspace`` waits this long for ``launch_pending`` agents to settle (RS-11: bounded by the start timeout).
FROM_WORKSPACE_SETTLE_S = AGENT_START_TIMEOUT_MS / 1000.0
FROM_WORKSPACE_POLL_S = 0.5
DEFAULT_ROLE_FALLBACK = "agent"
#: Injectable clocks for the settle and starting loops (tests patch these).
_sleep = _time.sleep
_monotonic = _time.monotonic
NO_DAEMON_ENV = "HERDR_TEAM_NO_DAEMON"
SKILL_MARKER_RE = re.compile(r"<!--\s*herdr-team skill v(\d+)")
SKILL_INSTALL_PATHS = (".agents/skills/herdr-team/SKILL.md", ".claude/skills/herdr-team/SKILL.md")


# --------------------------------------------------------------------------
# helpers


def _human_only(layout: Layout, team: str, author: Author, action: str) -> None:
    if author.is_human:
        return
    audit(layout, team, "author_mismatch", author, {"action": action, "resolved": author.name, "via": author.via})
    raise HerdrTeamError("author_mismatch", "{} is human only; this pane is {!r} ({})".format(action, author.name, author.via), EXIT_REFUSED, {"action": action, "author": author.name, "via": author.via})


def _author(args: argparse.Namespace, layout: Layout, api: Any, team: Optional[str] = None, require_server: bool = True, as_human: bool = False) -> Author:
    hint = team if team is not None else resolve_team(args, layout, None, required=False)
    return resolve_author(args, layout, api, team=hint, require_server=require_server, as_human=as_human)


def _create_author(args: argparse.Namespace, layout: Layout, api: Any, team_name: str) -> Author:
    """Author for ``create``: existing teams are the hint for member panes; outside Herdr the new team is enough."""
    try:
        hint = resolve_team(args, layout, None, required=False)
    except HerdrTeamError as err:
        if err.code != "team_ambiguous":
            raise
        hint = None
    try:
        return resolve_author(args, layout, api, team=hint, require_server=True)
    except HerdrTeamError as err:
        if err.code != "team_required":
            raise
    return resolve_author(args, layout, api, team=team_name, require_server=True)


def _requested_by(author: Author) -> Dict[str, Any]:
    return {"name": author.name, "via": author.via, "verified": bool(author.verified)}


def _ensure_daemon(layout: Layout, env: Dict[str, str]) -> None:
    if env.get(NO_DAEMON_ENV) == "1":
        return
    try:
        _daemon.ensure_daemon(layout, env)
    except (HerdrTeamError, OSError):
        return


def _set_default_team(layout: Layout, name: str) -> None:
    doc = read_console_json(layout.session)
    doc["default_team"] = name
    write_console_json(layout.session, doc)


def _team_arg(args: argparse.Namespace, layout: Layout, positional: Optional[str]) -> str:
    """The team name for roster commands: positional first, then the usual inference."""
    if positional:
        return _paths.validate_team_name(positional)
    name = resolve_team(args, layout, None, required=True)
    assert name is not None
    return name


def _default_role_for(kind: Optional[str], used: Sequence[str], names_plain: bool = False) -> str:
    """Plan 5.5: the role defaults to the kind label (``claude``, ``codex``); several members may share it.

    Under prefixed naming the member name is ``<team>-<kind>`` (``-2`` for a
    second one), so the name can never be mistaken for a kind. With
    ``--names plain`` the name would *be* the kind label, so the fallback
    ``agent``/``agent2``... applies there.
    """
    candidates: List[str] = []
    if kind and not names_plain:
        candidates.append(str(kind))
    candidates.append(DEFAULT_ROLE_FALLBACK)
    for candidate in candidates:
        try:
            _roster.validate_role(candidate, allow_kind_label=not names_plain)
        except HerdrTeamError:
            continue
        if candidate == kind:
            return candidate  # roles may repeat: two Claudes are both ``claude``, named ``t-claude`` and ``t-claude-2``
        if candidate not in used:
            return candidate
        for ordinal in range(2, 100):
            suffixed = "{}{}".format(candidate, ordinal)
            try:
                _roster.validate_role(suffixed)
            except HerdrTeamError:
                break
            if suffixed not in used:
                return suffixed
    raise HerdrTeamError("role_invalid", "cannot derive a role for kind {!r}; pass :<role>".format(kind), EXIT_REFUSED, {"kind": kind})


def settle_workspace_agents(api: Any, workspace_id: str, timeout_s: Optional[float] = None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """``agent.list`` rows of ``workspace_id`` once no row is ``launch_pending`` or the start timeout elapsed.

    Returns ``(settled, still_pending)``. Plan 12 (Selection): the CLI waits
    for a starting agent to settle, bounded by the start timeout, instead of
    dropping it.
    """
    limit = FROM_WORKSPACE_SETTLE_S if timeout_s is None else timeout_s
    deadline = _monotonic() + max(0.0, limit)
    while True:
        listing = api.request("agent.list", {})
        rows = [r for r in (listing.get("agents") or []) if isinstance(r, dict) and r.get("workspace_id") == workspace_id]
        pending = [r for r in rows if r.get("launch_pending")]
        settled = [r for r in rows if not r.get("launch_pending")]
        if not pending or _monotonic() >= deadline:
            return settled, pending
        _sleep(FROM_WORKSPACE_POLL_S)


def _parse_brief_args(values: Optional[List[str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for value in values or []:
        name, sep, text = value.partition("=")
        if not sep or not name.strip() or not text.strip():
            raise UsageError("--brief expects <name>=\"<text>\"")
        out[name.strip()] = text
    return out


# --------------------------------------------------------------------------
# the batch join routine (create, add, --from-workspace, --new)


class _JoinSpec:
    def __init__(self, target: str, role: Optional[str], name: Optional[str], brief: Optional[str] = None) -> None:
        self.target = target
        self.role = role
        self.name = name
        self.brief = brief
        self.resolved: Optional[_roster.ResolvedTarget] = None
        self.final_role: str = ""
        self.final_name: str = ""
        self.renamed = False


def parse_member_spec(spec: str) -> _JoinSpec:
    """``<target>[:<role>[:<name>]]``; pane ids carry one colon (``w2:p1``).

    Herdr numbers panes and workspaces in base 36 (``w1:p9`` is followed by
    ``w1:pA``), so the pane part is alphanumeric, not decimal (M5 rig finding).
    """
    parts = spec.split(":")
    if len(parts) >= 2 and re.match(r"^p[A-Za-z0-9]+$", parts[1]) and re.match(r"^w[A-Za-z0-9]+$", parts[0]):
        target = parts[0] + ":" + parts[1]
        rest = parts[2:]
    else:
        target = parts[0]
        rest = parts[1:]
    if not target:
        raise UsageError("--member needs a pane id or agent name")
    role = rest[0] if len(rest) >= 1 and rest[0] else None
    name = rest[1] if len(rest) >= 2 and rest[1] else None
    if len(rest) > 2:
        raise UsageError("--member takes at most <target>:<role>:<name>")
    return _JoinSpec(target, role, name)


def validate_join_batch(team: _roster.Team, api: Any, specs: List[_JoinSpec], names_plain: bool, layout: Optional[Layout] = None, steal: bool = False) -> None:
    """Phase 1 of the join: resolve targets, validate every role and name, pre-check claims, before any write."""
    used_roles: List[str] = []
    live = _roster.live_names(api)
    existing = [m.name for m in team.members if m.status != "left"]
    pending_names: List[str] = []
    for spec in specs:
        spec.resolved = _roster.resolve_target(api, spec.target)
        if spec.resolved.kind is None:
            raise HerdrTeamError("not_an_agent", "pane {} hosts no detected agent".format(spec.resolved.pane_id), EXIT_REFUSED, {"target": spec.target})
    # Live names held by the targets of this very call are vacated by their renames (RS-11: a
    # second codex already named ``<team>-codex-2`` must not block the batch), so they are not
    # "taken" for the other specs; ``order_join_specs`` renames the holder first.
    vacating = {s.resolved.name for s in specs if s.resolved is not None and s.resolved.name}
    for spec in specs:
        assert spec.resolved is not None
        if spec.role is not None:
            spec.final_role = _roster.validate_role(spec.role, allow_kind_label=not names_plain)
        else:
            spec.final_role = _default_role_for(spec.resolved.kind, used_roles, names_plain)
        if names_plain and spec.final_role in used_roles and spec.name is None:
            raise HerdrTeamError("name_invalid", "--names plain needs distinct roles; role {!r} repeats".format(spec.final_role), EXIT_REFUSED, {"role": spec.final_role})
        used_roles.append(spec.final_role)
        if spec.name is not None:
            wanted = _roster.validate_member_name(spec.name)
        else:
            wanted = _roster.derive_name(team.team, spec.final_role, "plain" if names_plain else team.naming)
        taken = set(existing) | set(pending_names) | {n for n in live if n not in vacating}
        if wanted in taken:
            if spec.name is not None:
                code = "name_taken" if wanted in existing or wanted in pending_names else "agent_name_taken"
                raise HerdrTeamError(code, "name {!r} is already in use".format(wanted), EXIT_REFUSED, {"name": wanted, "roster": existing, "candidates": sorted(taken)})
            wanted = _roster.unique_name(wanted, taken)
        spec.final_name = wanted
        pending_names.append(wanted)
    order_join_specs(specs)
    seen_terminals: Dict[str, str] = {}
    for spec in specs:
        assert spec.resolved is not None
        if spec.resolved.terminal_id in seen_terminals:
            raise HerdrTeamError("member_claimed", "target {} appears twice in this call".format(spec.target), EXIT_REFUSED, {"terminal_id": spec.resolved.terminal_id})
        seen_terminals[spec.resolved.terminal_id] = spec.final_name
        if not steal and layout is not None:
            owner = _roster.claim_owner(layout, spec.resolved.terminal_id, exclude_team=team.team)
            if owner is not None:
                raise HerdrTeamError(
                    "member_claimed",
                    "terminal {} is already {!r} in team {!r} (use --steal to move it)".format(spec.resolved.terminal_id, owner[1].name, owner[0]),
                    EXIT_REFUSED,
                    {"owner_team": owner[0], "member": owner[1].name, "terminal_id": spec.resolved.terminal_id},
                )


def order_join_specs(specs: List[_JoinSpec]) -> List[_JoinSpec]:
    """Validated specs in an order Herdr's ``agent.rename`` accepts.

    A spec whose ``final_name`` is another target's *current* live name must
    rename after that target has moved to its own final name (Herdr refuses a
    duplicate live name, and a mid-batch refusal would leave a half-named
    team). Two targets swapping names cannot be ordered; that is ``name_taken``
    with the advice to choose names explicitly.
    """
    by_live: Dict[str, _JoinSpec] = {}
    for spec in specs:
        if spec.resolved is not None and spec.resolved.name:
            by_live[spec.resolved.name] = spec
    ordered: List[_JoinSpec] = []
    done: set = set()
    visiting: set = set()

    def visit(spec: _JoinSpec) -> None:
        key = id(spec)
        if key in done:
            return
        holder = by_live.get(spec.final_name)
        if key in visiting:
            raise HerdrTeamError("name_taken", "the names of {} and {} would swap; choose names explicitly".format(spec.target, holder.target if holder is not None else "?"), EXIT_REFUSED, {"name": spec.final_name})
        visiting.add(key)
        if holder is not None and holder is not spec and holder.resolved is not None and holder.resolved.name != holder.final_name:
            visit(holder)
        visiting.discard(key)
        done.add(key)
        ordered.append(spec)

    for spec in specs:
        visit(spec)
    specs[:] = ordered
    return specs


def perform_join(layout: Layout, api: Any, team: _roster.Team, spec: _JoinSpec, steal: bool, force_rename: bool, env: Dict[str, str], author: Author) -> Tuple[_roster.Member, str]:
    """Phase 2 for one validated spec: claim check, rename, label, roster append, tokens, briefing job."""
    resolved = spec.resolved
    assert resolved is not None
    previous_owner = _roster.claim_check(layout, resolved.terminal_id, team.team, steal=steal)
    label = _roster.label_for(team.team, spec.final_role)
    if resolved.name != spec.final_name or force_rename:
        _roster.rename_agent(api, resolved.pane_id, spec.final_name)
        spec.renamed = True
    _roster.label_pane(api, resolved.pane_id, label)
    member = _roster.Member(
        name=spec.final_name, role=spec.final_role, kind=str(resolved.kind), terminal_id=resolved.terminal_id,
        pane_id=resolved.pane_id, workspace_id=resolved.workspace_id, tab_id=resolved.tab_id, label=label,
        cwd=resolved.cwd, brief=spec.brief, managed=False, status="active", generation=1, delivery="nudge",
        verified_kind=_roster._kind_verified(layout, str(resolved.kind)), joined_at=_roster.now_iso(), last_seen_at=_roster.now_iso(),
    )
    roster = _roster.Roster(layout, team.team)
    saved, _prev = roster.add_member(member, steal=steal or previous_owner is not None, socket=os.fspath(layout.socket))
    team.members = saved.members
    team.revision = saved.revision
    _roster.execute_token_commands(api, _roster.token_commands(member, team.team))
    # Join sets the member's cursor at the current max (plan 6.2); catch-up is board --last 30.
    try:
        from herdr_team.cmd_board import board_max_seq, cursor_advance

        cursor_advance(layout.team(team.team), member.name, board_max_seq(layout.team(team.team)), member.terminal_id, "cli")
    except HerdrTeamError:
        pass
    job = _roster.write_briefing_job(roster.paths, member.name, requested_by=_requested_by(author))
    return member, job.stem


def _member_json(member: _roster.Member, renamed: bool = False) -> Dict[str, Any]:
    obj = member.to_json()
    obj.pop("brief", None)
    obj["renamed"] = renamed
    return obj


# --------------------------------------------------------------------------
# create --new: layout and agent start (pure builders, live calls through api)


def parse_spawn_spec(spec: str) -> Tuple[str, str, Optional[str]]:
    """``<role>:<kind>[:<cwd>]``; the cwd may contain colons."""
    parts = spec.split(":", 2)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise UsageError("--spawn expects <role>:<kind>[:<cwd>]")
    role = _roster.validate_role(parts[0])
    kind = parts[1]
    if kind not in _roster.KIND_LABELS:
        raise HerdrTeamError("kind_unknown", "{!r} is not an agent kind the installed Herdr can start".format(kind), EXIT_REFUSED, {"kind": kind, "kinds": sorted(_roster.KIND_LABELS)})
    cwd = parts[2] if len(parts) == 3 and parts[2] else None
    return role, kind, cwd


def build_layout_request(team: str, leaves: List[Dict[str, Any]], team_dir: str, workspace_id: Optional[str] = None) -> Dict[str, Any]:
    """One ``layout.apply`` request: balanced split tree, ``tab_label``, ``focus:false``, per-leaf env (plan 5.2)."""
    if not leaves:
        raise UsageError("--new needs at least one --spawn")
    if len(leaves) > MAX_LAYOUT_LEAVES:
        raise HerdrTeamError("too_many_members", "at most {} panes per layout".format(MAX_LAYOUT_LEAVES), EXIT_REFUSED, {"count": len(leaves)})

    def node(items: List[Dict[str, Any]], depth: int) -> Dict[str, Any]:
        if len(items) == 1:
            leaf = items[0]
            env = {
                "HERDR_TEAM": team, "HERDR_TEAM_ROLE": leaf["role"], "HERDR_TEAM_MEMBER": leaf["name"], "HERDR_TEAM_DIR": team_dir,
            }
            out: Dict[str, Any] = {"type": "pane", "label": _roster.label_for(team, leaf["role"]), "env": env}
            if leaf.get("cwd"):
                out["cwd"] = leaf["cwd"]
            return out
        half = (len(items) + 1) // 2
        return {
            "type": "split", "direction": "right" if depth % 2 == 0 else "down", "ratio": round(half / len(items), 4),
            "first": node(items[:half], depth + 1), "second": node(items[half:], depth + 1),
        }

    request: Dict[str, Any] = {"root": node(leaves, 0), "tab_label": "team:{}".format(team), "focus": False}
    if workspace_id:
        request["workspace_id"] = workspace_id
    return request


def layout_pane_ids(root: Any) -> List[str]:
    """Leaf pane ids of a returned layout, first-to-second order (the order leaves were given)."""
    if not isinstance(root, dict):
        return []
    if root.get("type") == "pane":
        return [str(root.get("pane_id"))] if root.get("pane_id") else []
    return layout_pane_ids(root.get("first")) + layout_pane_ids(root.get("second"))


def agent_start_argv(name: str, kind: str, pane_id: str, timeout_ms: int = AGENT_START_TIMEOUT_MS) -> List[str]:
    return ["agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", str(int(timeout_ms))]


def _started_agent(stdout: str) -> Optional[Dict[str, Any]]:
    """The ``AgentInfo`` a successful ``herdr agent start`` printed, or None.

    The real CLI prints ``{"id":"cli:agent:start","result":{"type":"agent_started",
    "agent":AgentInfo,"argv":[...]}}`` (``src/cli/agent.rs``); the envelope is
    stripped with ``unwrap_cli_response``. Anything else (a bare ``{}`` from a
    fake, non-JSON output, an empty line) yields None: a successful exit is
    trusted regardless of what was printed, the pane poll settles the rest.
    """
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    try:
        result = _api.unwrap_cli_response(parsed, ["agent", "start"])
    except HerdrTeamError:
        return None
    if isinstance(result, dict) and result.get("type") not in (None, "agent_started"):
        return None
    return _api.agent_of(result)


def _start_agent(api: Any, name: str, kind: str, pane_id: str, sleep: Any = None) -> Dict[str, Any]:
    """``herdr agent start`` with the plan's ``agent_pane_busy`` retry (500 ms for 10 s).

    Returns ``{"pane_id", "started", "terminal_id", "agent"}``; ``terminal_id``
    and ``agent`` come from the CLI's ``agent_started`` result when it printed
    one (``_started_agent``) and are None otherwise.
    """
    import time as _time

    sleeper = sleep if sleep is not None else _time.sleep
    deadline = _time.monotonic() + AGENT_START_RETRY_WINDOW_S
    while True:
        result = api.run(agent_start_argv(name, kind, pane_id), timeout=(AGENT_START_TIMEOUT_MS / 1000.0) + 5.0)
        if result.ok:
            agent = _started_agent(result.stdout)
            terminal_id = str(agent["terminal_id"]) if agent and agent.get("terminal_id") else None
            return {"pane_id": pane_id, "started": True, "terminal_id": terminal_id, "agent": agent}
        text = (result.stderr or "") + (result.stdout or "")
        if "agent_pane_busy" in text and _time.monotonic() < deadline:
            try:
                api.request("agent.get", {"target": pane_id})
                raise HerdrTeamError("agent_pane_busy", "pane {} already hosts an agent".format(pane_id), EXIT_REFUSED, {"pane_id": pane_id})
            except HerdrTeamError as err:
                if err.code != "agent_not_found":
                    raise
            sleeper(AGENT_START_RETRY_S)
            continue
        raise HerdrTeamError("agent_start_failed", "agent start {} in {} failed: {}".format(name, pane_id, text.strip()[:300]), EXIT_REFUSED, {"pane_id": pane_id, "name": name})


def _pane_terminal(api: Any, pane_id: str) -> Optional[str]:
    try:
        pane = api.request("pane.get", {"pane_id": pane_id}).get("pane")
    except HerdrTeamError:
        return None
    return str(pane["terminal_id"]) if isinstance(pane, dict) and pane.get("terminal_id") else None


def _toast_job(team_paths: TeamPaths, author: Author, title: str, body: str) -> Optional[str]:
    """The CLI never calls ``notification.show``; it asks the daemon through a ``toast`` job."""
    try:
        return enqueue_job(team_paths, "toast", None, author, extra={"title": title, "body": body, "toast_kind": "roster"})
    except HerdrTeamError:
        return None


def _wait_for_agent(api: Any, pane_id: str, kind: str, timeout_s: float) -> Optional[_roster.ResolvedTarget]:
    """Poll ``agent.get`` by pane id every ``STARTING_POLL_S`` until the agent settled or the deadline passed."""
    deadline = _monotonic() + max(0.0, timeout_s)
    while True:
        try:
            resolved = _roster.resolve_target(api, pane_id)
            if resolved.kind == kind or (resolved.kind and kind not in _roster.KIND_LABELS):
                return resolved
        except HerdrTeamError as err:
            if err.code not in ("agent_not_found", "not_an_agent", "launch_pending", "agent_not_ready"):
                raise
        if _monotonic() >= deadline:
            return None
        _sleep(STARTING_POLL_S)


def _spawn_member(layout: Layout, api: Any, team: _roster.Team, team_paths: TeamPaths, leaf: Dict[str, Any], pane_id: str, briefs: Dict[str, str], env: Dict[str, str], author: Author, args: argparse.Namespace) -> Dict[str, Any]:
    """One ``--spawn`` leaf (plan 5.2, 12): record ``starting``, start, poll by pane id, then ``active`` or ``failed``.

    Nothing here raises for a single leaf: a trust or login dialog that never
    settles leaves the member ``failed`` with a toast, and the other leaves
    still join.
    """
    name = str(leaf["name"])
    role = str(leaf["role"])
    kind = str(leaf["kind"])
    brief = briefs.get(name) or briefs.get(role)
    roster = _roster.Roster(layout, team.team)
    label = _roster.label_for(team.team, role)
    member = _roster.Member(
        name=name, role=role, kind=kind, terminal_id=_pane_terminal(api, pane_id), pane_id=pane_id,
        workspace_id=pane_id.split(":")[0] if ":" in pane_id else None, tab_id=None, label=label, cwd=leaf.get("cwd"),
        brief=_roster._sanitize_brief(brief) if brief else None, managed=True, status="starting", generation=1, delivery="nudge",
        verified_kind=_roster._kind_verified(layout, kind), joined_at=_roster.now_iso(), last_seen_at=None,
    )
    saved, _prev = roster.add_member(member, steal=args.steal, socket=os.fspath(layout.socket))
    team.members = saved.members
    team.revision = saved.revision
    out: Dict[str, Any] = {"member": _member_json(member, False), "job": None}
    try:
        started = _start_agent(api, name, kind, pane_id)
        started_terminal = started.get("terminal_id")
        if started_terminal and started_terminal != member.terminal_id:
            # the CLI's agent_started result names the terminal the agent runs in;
            # trust it over the pre-start pane.get guess so a failed member still
            # points at the right terminal for ``bind``
            member = roster.set_status(name, "starting", terminal_id=started_terminal)
            out["member"] = _member_json(member, False)
        resolved = _wait_for_agent(api, pane_id, kind, AGENT_START_TIMEOUT_MS / 1000.0)
    except HerdrTeamError as err:
        resolved = None
        warn(args, "{}: agent start failed in {}: {}".format(name, pane_id, err.message))
    if resolved is None:
        failed = roster.set_status(name, "failed", last_seen_at=_roster.now_iso())
        warn(args, "{} did not settle in {} within {:g}s; marked failed (bind it once the dialog is answered)".format(name, pane_id, AGENT_START_TIMEOUT_MS / 1000.0))
        _toast_job(team_paths, author, "herdr-team {}: {} failed to start".format(team.team, name), "{} in {} never settled; answer its dialog and run: herdr-team bind {} {} {}".format(name, pane_id, team.team, name, pane_id))
        out["member"] = _member_json(failed, False)
        return out
    renamed = False
    if resolved.name != name:
        try:
            _roster.rename_agent(api, resolved.pane_id, name)
            renamed = True
        except HerdrTeamError as err:
            warn(args, "{}: rename failed ({}); the daemon re-applies it".format(name, err.code))
    _roster.label_pane(api, resolved.pane_id, label)
    active = roster.set_status(
        name, "active", terminal_id=resolved.terminal_id, pane_id=resolved.pane_id, workspace_id=resolved.workspace_id,
        tab_id=resolved.tab_id, cwd=resolved.cwd or leaf.get("cwd"), last_seen_at=_roster.now_iso(), managed=True,
    )
    if member.terminal_id and member.terminal_id != resolved.terminal_id:
        _roster.remove_pane_record(layout.session, member.terminal_id)
    _roster.write_pane_record(layout.session, resolved.terminal_id, team.team, name, 1)
    _roster.execute_token_commands(api, _roster.token_commands(active, team.team))
    try:
        from herdr_team.cmd_board import board_max_seq, cursor_advance

        cursor_advance(team_paths, name, board_max_seq(team_paths), resolved.terminal_id, "cli")
    except HerdrTeamError:
        pass
    job = _roster.write_briefing_job(team_paths, name, requested_by=_requested_by(author))
    out["member"] = _member_json(active, renamed)
    out["job"] = job.stem
    team.members = roster.load().members
    return out


# --------------------------------------------------------------------------
# create


def _add_create_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("team_pos", metavar="team")
    parser.add_argument("--charter", metavar="TEXT")
    parser.add_argument("--charter-file", dest="charter_file", metavar="PATH")
    parser.add_argument("--ref", action="append", default=[], metavar="PATH")
    parser.add_argument("--member", action="append", default=[], metavar="TARGET[:ROLE[:NAME]]")
    parser.add_argument("--brief", action="append", default=[], metavar="NAME=TEXT")
    parser.add_argument("--from-workspace", dest="from_workspace", metavar="ID")
    parser.add_argument("--names", choices=("prefixed", "plain"), default="prefixed")
    parser.add_argument("--rename", action="store_true", help="rename targets that already carry a name")
    parser.add_argument("--steal", action="store_true")
    parser.add_argument("--reuse", action="store_true", help="add to an existing team of this name")
    parser.add_argument("--use", action="store_true", help="make this team the default even when another team exists")
    parser.add_argument("--new", action="store_true", help="lay out fresh panes and start agents (--spawn)")
    parser.add_argument("--workspace", metavar="ID", help="workspace for --new (default: the current one)")
    parser.add_argument("--spawn", action="append", default=[], metavar="ROLE:KIND[:CWD]")


def _run_create(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    api = api_for(args, layout)
    env = env_of(args)
    team_name = _paths.validate_team_name(args.team_pos)
    author = _create_author(args, layout, api, team_name)
    if args.charter is not None and args.charter_file is not None:
        raise UsageError("pass --charter or --charter-file, not both")
    if args.charter is not None or args.charter_file is not None:
        _human_only(layout, team_name, author, "create --charter")
    if args.new and (args.member or args.from_workspace):
        raise UsageError("--new takes --spawn, not --member or --from-workspace")
    if not args.new and not args.member and not args.from_workspace:
        raise UsageError("create needs --member, --from-workspace, or --new --spawn")
    briefs = _parse_brief_args(args.brief)
    names_plain = args.names == "plain"
    known_before = layout.session.list_teams()
    existing_paths = layout.team(team_name)
    if existing_paths.team_json.is_file() and not args.reuse:
        raise HerdrTeamError("team_exists", "team {!r} already exists (use --reuse)".format(team_name), EXIT_REFUSED, {"team": team_name})

    # Phase 1: resolve and validate everything before any write.
    specs: List[_JoinSpec] = []
    spawn: List[Dict[str, Any]] = []
    if args.new:
        roles_seen: List[str] = []
        for item in args.spawn:
            role, kind, cwd = parse_spawn_spec(item)
            if names_plain and role in roles_seen:
                raise HerdrTeamError("name_invalid", "--names plain needs distinct roles; role {!r} repeats".format(role), EXIT_REFUSED, {"role": role})
            roles_seen.append(role)
            base = _roster.derive_name(team_name, role, "plain" if names_plain else "prefixed")
            name = _roster.unique_name(base, [s["name"] for s in spawn] + _roster.live_names(api))
            spawn.append({"role": role, "kind": kind, "cwd": cwd, "name": name})
        if not spawn:
            raise UsageError("--new needs at least one --spawn")
    else:
        for item in args.member:
            specs.append(parse_member_spec(item))
        if args.from_workspace:
            settled, still_pending = settle_workspace_agents(api, args.from_workspace)
            for row in settled:
                if any(s.target in (row.get("pane_id"), row.get("name")) for s in specs):
                    continue
                specs.append(_JoinSpec(str(row.get("pane_id")), None, None))
            for row in still_pending:
                warn(args, "skipping {}: still starting after {:g}s; add it later with: herdr-team add {} {}".format(row.get("pane_id"), FROM_WORKSPACE_SETTLE_S, team_name, row.get("pane_id")))
            args._still_pending = [str(r.get("pane_id")) for r in still_pending]
            if not specs:
                raise HerdrTeamError("agent_not_found", "no settled agents in workspace {}".format(args.from_workspace), EXIT_REFUSED, {"workspace_id": args.from_workspace, "pending": args._still_pending})
    charter_body: Optional[str] = None
    if args.charter is not None:
        charter_body = _charter._sanitize_charter_text(args.charter)
    shell = _roster.Team(team=team_name, socket=os.fspath(layout.socket), state_dir=os.fspath(layout.state_root.path), created_at=_roster.now_iso(), naming="plain" if names_plain else "prefixed")
    if existing_paths.team_json.is_file():
        shell = _roster.load_team(existing_paths)
    if specs:
        validate_join_batch(shell, api, specs, names_plain, layout=layout, steal=args.steal)
        for spec in specs:
            spec.brief = briefs.get(spec.final_name) or briefs.get(spec.final_role)
    for unknown in set(briefs) - {s.final_name for s in specs} - {s.final_role for s in specs} - {s["name"] for s in spawn} - {s["role"] for s in spawn}:
        raise HerdrTeamError("member_not_found", "--brief names {!r}, which is not being added".format(unknown), EXIT_REFUSED, {"name": unknown})

    # Phase 2: writes.
    fresh = not existing_paths.team_json.is_file()
    team = _roster.create_team(layout, team_name, naming="plain" if names_plain else "prefixed", charter=None, reuse=args.reuse)
    team_paths = layout.team(team_name)
    try:
        return _create_members(args, layout, api, env, author, team, team_paths, specs, spawn, briefs, names_plain, charter_body, known_before)
    except BaseException:
        if fresh and not agent_members(load_doc(team_paths)):
            shutil.rmtree(team_paths.root, ignore_errors=True)
        raise


def _create_members(args: argparse.Namespace, layout: Layout, api: Any, env: Dict[str, str], author: Author, team: _roster.Team, team_paths: TeamPaths, specs: List[_JoinSpec], spawn: List[Dict[str, Any]], briefs: Dict[str, str], names_plain: bool, charter_body: Optional[str], known_before: List[str]) -> int:
    team_name = team.team
    charter_doc: Optional[Dict[str, Any]] = None
    if charter_body is not None or args.charter_file is not None:
        charter = _charter.set_charter(layout, team_name, author, charter_body, args.charter_file, list(args.ref))
        charter_doc = charter.to_json()
        team = _roster.load_team(team_paths)
    members_out: List[Dict[str, Any]] = []
    jobs: List[str] = []
    failed: List[Dict[str, Any]] = []
    if args.new:
        request = build_layout_request(team_name, spawn, os.fspath(team_paths.root), args.workspace)
        applied = api.request("layout.apply", request, timeout=15.0)
        root = applied.get("layout", applied).get("root") if isinstance(applied, dict) else None
        pane_ids = layout_pane_ids(root)
        if len(pane_ids) != len(spawn):
            raise HerdrTeamError("layout_failed", "layout.apply returned {} panes for {} leaves".format(len(pane_ids), len(spawn)), EXIT_REFUSED, {"pane_ids": pane_ids})
        for leaf, pane_id in zip(spawn, pane_ids):
            outcome = _spawn_member(layout, api, team, team_paths, leaf, pane_id, briefs, env, author, args)
            members_out.append(outcome["member"])
            if outcome.get("job"):
                jobs.append(outcome["job"])
            if outcome["member"].get("status") == "failed":
                failed.append(outcome["member"])
        if failed and len(failed) == len(spawn):
            warn(args, "no member started; the team {} is kept with {} failed member(s); fix the panes and run: herdr-team bind".format(team_name, len(failed)))
    else:
        for spec in specs:
            member, job = perform_join(layout, api, team, spec, args.steal, args.rename, env, author)
            members_out.append(_member_json(member, spec.renamed))
            jobs.append(job)
    _ensure_daemon(layout, env)
    default_team = default_team_of(layout.session)
    set_default = False
    others = [k for k in known_before if k != team_name]
    if default_team in (None, team_name) and not others:
        _set_default_team(layout, team_name)
        set_default = True
    elif args.use:
        _set_default_team(layout, team_name)
        set_default = True
    elif default_team != team_name:
        warn(args, "default team stays {!r}; run: herdr-team use {}".format(default_team or (others[0] if others else "?"), team_name))
    payload = {
        "team": team_name, "team_dir": os.fspath(team_paths.root), "created": True, "members": members_out,
        "charter": {"seq": charter_doc["seq"], "headline": charter_headline(charter_doc)} if charter_doc else None,
        "notifier": notifier_state(layout.session), "default_team": set_default, "briefing_jobs": jobs,
        "pending": list(getattr(args, "_still_pending", []) or []), "failed": [m["name"] for m in failed],
    }

    def human() -> str:
        lines = ["team {} created ({} member{})".format(team_name, len(members_out), "" if len(members_out) == 1 else "s")]
        for m in members_out:
            lines.append("  {} ({}, {}) {}{}{}".format(m["name"], m["role"], m["kind"], m["pane_id"], " renamed" if m.get("renamed") else "", "" if m.get("status") in (None, "active") else " " + str(m.get("status"))))
        for pane_id in payload["pending"]:
            lines.append("  {} still starting; add it later".format(pane_id))
        if charter_doc:
            lines.append("charter #{}: {}".format(charter_doc["seq"], charter_headline(charter_doc)))
        lines.append("notifier: {}".format(payload["notifier"]))
        return "\n".join(lines)

    return emit(args, payload, human)


# --------------------------------------------------------------------------
# add / remove / leave / bind / dissolve / use / teams / rename


def _add_add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("team_pos", metavar="team")
    parser.add_argument("target")
    parser.add_argument("--role", metavar="ROLE")
    parser.add_argument("--as", dest="as_name", metavar="NAME")
    parser.add_argument("--brief", metavar="TEXT")
    parser.add_argument("--steal", action="store_true")
    parser.add_argument("--rename", action="store_true")


def _run_add(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    api = api_for(args, layout)
    team_name = _paths.validate_team_name(args.team_pos)
    author = _author(args, layout, api, team=team_name)
    team = _roster.load_team(layout.team(team_name))
    _roster.check_session(layout, team, allow_mismatch=bool(getattr(args, "socket", None)))
    spec = _JoinSpec(args.target, args.role, args.as_name, args.brief)
    validate_join_batch(team, api, [spec], team.naming == "plain", layout=layout, steal=args.steal)
    member, job = perform_join(layout, api, team, spec, args.steal, args.rename, env_of(args), author)
    # Every other member hears about the newcomer: an urgent system broadcast the daemon nudges for
    # (the newcomer itself gets the briefing instead, which lists its teammates).
    joined = _roster.append_system_record(
        layout.team(team_name), "member_joined",
        "{} joined team {} as {} ({}, {})".format(member.name, team_name, member.role, member.kind, member.pane_id),
        to=["all"], extra={"urgent": True, "member": member.name, "role": member.role, "member_kind": member.kind}, socket=os.fspath(layout.socket),
    )
    _ensure_daemon(layout, env_of(args))
    kind_trusted = _roster.kind_trusted(store.read_json(layout.session.kinds_json, default=None), str(member.kind or ""))
    if not kind_trusted:
        # Gate 4 holds every delivery to an untrusted kind; say so now instead of leaving the member "unbriefed".
        warn(args, "{} is a {} agent and that kind is not trusted for delivery yet: nothing is typed into it (no briefing, no nudges) until you run: herdr-team kinds trust {}".format(member.name, member.kind, member.kind))
    payload = {"team": team_name, "member": _member_json(member, spec.renamed), "renamed": spec.renamed, "notifier": notifier_state(layout.session), "briefing_job": job, "joined_record": joined, "kind_trusted": kind_trusted}
    return emit(args, payload, "{} joined {} as {} ({})".format(member.name, team_name, member.role, member.pane_id))


def _add_remove_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("team_pos", metavar="team")
    parser.add_argument("name")
    parser.add_argument("--keep-name", dest="keep_name", action="store_true")


def _run_remove(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    api = api_for(args, layout)
    team_name = _paths.validate_team_name(args.team_pos)
    author = _author(args, layout, api, team=team_name)
    check_write_session(args, layout, team_name)
    result = _roster.Roster(layout, team_name).remove_member(api, args.name, keep_name=args.keep_name, reason="removed by {}".format(author.name), socket=os.fspath(layout.socket))
    payload = {"team": team_name, "removed": result["removed"], "tokens_cleared": bool(result["tokens_cleared"]), "name_cleared": bool(result["name_cleared"])}
    return emit(args, payload, "{} removed from {}".format(result["removed"], team_name))


def _run_leave(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    api = api_for(args, layout)
    author = _author(args, layout, api)
    if not author.is_member or not author.team:
        raise HerdrTeamError("not_a_member", "leave runs from a member pane", EXIT_UNREACHABLE, {"author": author.name})
    result = _roster.Roster(layout, author.team).leave(api, author.name, socket=os.fspath(layout.socket))
    return emit(args, {"team": author.team, "left": result["left"]}, "{} left {}".format(result["left"], author.team))


def _add_bind_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("team_pos", metavar="team")
    parser.add_argument("name")
    parser.add_argument("target")


def _run_bind(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    api = api_for(args, layout)
    team_name = _paths.validate_team_name(args.team_pos)
    _author(args, layout, api, team=team_name)
    check_write_session(args, layout, team_name)
    target = _roster.resolve_target(api, args.target)
    result = _roster.Roster(layout, team_name).bind(api, args.name, target, socket=os.fspath(layout.socket))
    _ensure_daemon(layout, env_of(args))
    member = result["member"]
    member.pop("brief", None)
    return emit(args, {"team": team_name, "member": member, "previous_terminal_id": result["previous_terminal_id"]}, "{} bound to {}".format(member["name"], member["pane_id"]))


def _add_dissolve_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("team_pos", metavar="team")
    parser.add_argument("--yes", action="store_true")


def _run_dissolve(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    api = api_for(args, layout)
    team_name = _paths.validate_team_name(args.team_pos)
    author = _author(args, layout, api, team=team_name, require_server=False)
    _human_only(layout, team_name, author, "dissolve")
    if not args.yes:
        raise HerdrTeamError("confirmation_required", "dissolve archives team {!r}; pass --yes".format(team_name), EXIT_REFUSED, {"team": team_name})
    _roster.load_team(layout.team(team_name))
    check_write_session(args, layout, team_name)
    result = _roster.Roster(layout, team_name).dissolve(api)
    view_cleared = False
    if view_state(layout.session) == "on":
        try:
            api.request("agent.view.clear", {"source": "plugin:herdr-team"})
        except HerdrTeamError:
            pass
        try:
            os.unlink(layout.session.view_json)
            view_cleared = True
        except OSError:
            pass
    console = read_console_json(layout.session)
    if console.get("default_team") == team_name:
        console.pop("default_team", None)
        remaining = [t for t in layout.session.list_teams() if t != team_name]
        if len(remaining) == 1:
            console["default_team"] = remaining[0]
        write_console_json(layout.session, console)
    payload = {"team": team_name, "archived_to": result["archived_to"], "members_cleared": result["members_cleared"], "view_cleared": view_cleared}
    return emit(args, payload, "team {} archived to {}".format(team_name, result["archived_to"]))


def _add_use_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("team_pos", metavar="team")


def _run_use(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    team_name = _paths.validate_team_name(args.team_pos)
    if team_name not in layout.session.list_teams():
        raise HerdrTeamError("team_not_found", "team {!r} does not exist in this session".format(team_name), EXIT_REFUSED, {"team": team_name, "teams": layout.session.list_teams()})
    check_write_session(args, layout, team_name)
    _set_default_team(layout, team_name)
    return emit(args, {"default_team": team_name}, "default team: {}".format(team_name))


def _run_teams(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    teams = _roster.list_teams(layout)
    payload = {"teams": teams, "session": layout.slug}

    def human() -> str:
        if not teams:
            return "no teams in session {}".format(layout.slug)
        lines = ["session {}".format(layout.slug)]
        for t in teams:
            lines.append("  {}{}  {} member{}  {}  {}".format(t["team"], " (default)" if t.get("default") else "", t["members"], "" if t["members"] == 1 else "s", "running" if t["running"] else "stopped", t["team_dir"]))
        return "\n".join(lines)

    return emit(args, payload, human)


def _add_rename_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("old")
    parser.add_argument("new")


def _run_rename(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    api = api_for(args, layout)
    author = _author(args, layout, api)
    team_name = resolve_team(args, layout, author)
    assert team_name is not None
    check_write_session(args, layout, team_name)
    team = _roster.load_team(layout.team(team_name))
    member = team.find(args.old)
    if member is None or member.is_human:
        raise HerdrTeamError("member_not_found", "{!r} is not an agent member of {!r}".format(args.old, team_name), EXIT_REFUSED, {"name": args.old, "roster": team.names()})
    new = _roster.validate_member_name(args.new)
    if member.pane_id:
        _roster.rename_agent(api, member.pane_id, new)
    _roster.Roster(layout, team_name).adopt_rename(member.name, new, socket=os.fspath(layout.socket))
    return emit(args, {"team": team_name, "old": member.name, "new": new}, "{} is now {} (old name resolves for 10 min)".format(member.name, new))


# --------------------------------------------------------------------------
# me / who / audit


def skill_installed_version(env: Dict[str, str]) -> Optional[int]:
    home = env.get("HOME")
    if not home:
        return None
    for rel in SKILL_INSTALL_PATHS:
        path = Path(home) / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = SKILL_MARKER_RE.search(text)
        if match:
            return int(match.group(1))
        return 0
    return None


def _run_me(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    api = api_for(args, layout)
    author = _author(args, layout, api, require_server=False)
    if not author.is_member or not author.team:
        raise HerdrTeamError("not_a_member", "me runs from a member pane; you are {}".format(author.name), EXIT_UNREACHABLE, {"author": author.name, "via": author.via, "hint": "known teams: {}".format(", ".join(layout.session.list_teams()) or "none")})
    team_name = author.team
    team_paths = layout.team(team_name)
    doc = load_doc(team_paths)
    me = next((m for m in members_of(doc) if m.get("name") == author.name), None)
    if me is None:
        raise HerdrTeamError("not_a_member", "{} is not on the roster of {}".format(author.name, team_name), EXIT_UNREACHABLE, {"author": author.name, "team": team_name})
    records = board_read_all(team_paths)
    cursor = int(cursor_get(team_paths, author.name).get("seq", 0))
    unread = unread_for(team_paths, records, author.name)
    installed = skill_installed_version(env_of(args))
    teammates = [
        {"name": m.get("name"), "role": m.get("role"), "kind": m.get("kind"), "status": m.get("status")}
        for m in members_of(doc) if m.get("name") != author.name and m.get("status") != "left"
    ]
    charter = charter_of(doc)
    payload = {
        "team": team_name, "name": author.name, "role": me.get("role"), "kind": me.get("kind"), "pane_id": author.pane_id or me.get("pane_id"),
        "terminal_id": author.terminal_id or me.get("terminal_id"), "brief": me.get("brief"), "charter": charter_summary(charter),
        "teammates": teammates, "unread": unread, "cursor": cursor, "verified": bool(author.verified), "via": author.via,
        "skill_version": SKILL_VERSION, "skill_installed": installed, "skill_ok": installed == SKILL_VERSION,
        "cli": cli_path(), "notifier": notifier_state(layout.session),
    }
    if installed is not None and installed != SKILL_VERSION:
        warn(args, "installed skill v{} differs from v{}; run: herdr-team skill install".format(installed, SKILL_VERSION))
    return emit(args, payload, lambda: _render.render_me(payload, doc))


def _add_who_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("team_pos", metavar="team", nargs="?")
    parser.add_argument("--role", metavar="ROLE")
    parser.add_argument("--brief", action="store_true")
    parser.add_argument("--ascii", action="store_true")


def _who_from_roster(layout: Layout, team_name: str, doc: Dict[str, Any], agents: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """The ``members`` list of ``who --json`` built from the roster and an optional ``agent list`` snapshot."""
    team = _roster.Team.from_json(doc)
    charter = charter_of(doc)
    who = _roster.build_who_json({team_name: team}, agents or [], {team_name: charter} if charter else {}, {team_name: mute_state(layout.team(team_name))}, {}, socket=os.fspath(layout.socket))
    members = who["teams"][team_name]["members"]
    if agents is None:
        for m in members:
            m["unreachable"] = True
    return members


def _who_kinds(layout: Layout, members: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """``kinds.json`` summarised per agent kind on the roster (M6 SK-03): the trust flags the
    daemon's gate 4 reads plus the ``multiline`` paste-probe record, so ``who --json`` shows
    whether a kind keeps a two-line prompt in one submission without opening the state file."""
    doc = store.read_json(layout.session.kinds_json, default={})
    doc = doc if isinstance(doc, dict) else {}
    out: Dict[str, Dict[str, Any]] = {}
    for member in members:
        kind = member.get("kind")
        if not isinstance(kind, str) or not kind or kind == "human" or kind in out:
            continue
        entry = doc.get(kind) if isinstance(doc.get(kind), dict) else {}
        probe = entry.get("probe") if isinstance(entry.get("probe"), dict) else None
        multiline = entry.get("multiline") if isinstance(entry.get("multiline"), dict) else None
        out[kind] = {
            "trusted": bool(entry.get("trusted")),
            "verified": bool(entry.get("verified")),
            "probe_ok": bool(probe and probe.get("ok")),
            "multiline": multiline,
        }
    return out


def _who_payload(args: argparse.Namespace, layout: Layout, api: Any, team_name: str, human_reader: str) -> Dict[str, Any]:
    team_paths = layout.team(team_name)
    doc = load_doc(team_paths)
    status = daemon_status(layout.session)
    who_doc = store.read_json(layout.session.who_json)
    source = "who.json"
    members: List[Dict[str, Any]]
    if status["alive"] and isinstance(who_doc, dict) and isinstance(who_doc.get("teams"), dict) and team_name in who_doc["teams"]:
        members = [dict(m) for m in (who_doc["teams"][team_name].get("members") or []) if isinstance(m, dict)]
    else:
        try:
            listing = api.request("agent.list", {})
            agents = [a for a in listing.get("agents") or [] if isinstance(a, dict)]
            source = "agent-list"
        except HerdrTeamError as err:
            if err.exit_code != EXIT_UNREACHABLE and err.code not in ("unknown_method",):
                raise
            agents = None
            source = "unreachable"
        members = _who_from_roster(layout, team_name, doc, agents)
    records = board_read_all(team_paths)
    cursors = cursors_all(team_paths)
    for m in members:
        name = str(m.get("name"))
        cursor = int((cursors.get(name) or {}).get("seq", 0))
        m["unread"] = sum(1 for r in records if r["seq"] > cursor and r.get("from") != name and addressed_to(r, name, False))
        if not m.get("last_headline"):
            task = read_task(team_paths, name) if _paths.FILE_STEM_RE.match(name) else None
            if task:
                m["last_headline"] = task.get("headline")
        member_doc = next((x for x in members_of(doc) if x.get("name") == name), {})
        m.setdefault("brief", member_doc.get("brief"))
    kinds = _who_kinds(layout, members)
    if args.role:
        members = [m for m in members if m.get("role") == args.role]
    charter = charter_of(doc)
    payload: Dict[str, Any] = {
        "team": team_name, "source": source,
        "daemon": {"alive": bool(status["alive"]), "beat_age_s": status["beat_age_s"], "pid": status["pid"]},
        "charter": dict(charter, headline=charter_headline(charter)) if charter else None,
        "default_team": default_team_of(layout.session) == team_name,
        "view": view_state(layout.session), "toasts": toast_delivery(layout.config_dir), "nudges": nudges_state(team_paths),
        "unread_for_you": unread_for(team_paths, records, human_reader, is_human=True),
        "members": members,
        "kinds": kinds,
    }
    return payload


def _run_who(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    api = api_for(args, layout)
    if args.team_pos:
        team_name = _paths.validate_team_name(args.team_pos)
        author = _author(args, layout, api, team=team_name, require_server=False)
    else:
        author = _author(args, layout, api, require_server=False)
        resolved = resolve_team(args, layout, author)
        assert resolved is not None
        team_name = resolved
    human_reader = "human@" + (author.from_label or "human") if not author.is_member else author.name
    payload = _who_payload(args, layout, api, team_name, human_reader)
    if payload["source"] == "unreachable":
        warn(args, "cannot reach Herdr; showing roster state only")

    def human() -> str:
        return _render.render_who(payload, team_name, ascii_only=args.ascii, brief=args.brief, role=None)

    return emit(args, payload, human)


def _add_audit_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("team_pos", metavar="team", nargs="?")
    parser.add_argument("--last", type=int, metavar="N")


def _run_audit(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    team_name = _team_arg(args, layout, args.team_pos)
    entries = _identity.read_audit(layout, team_name, args.last)
    payload = {"team": team_name, "entries": entries}

    def human() -> str:
        if not entries:
            return "no audit entries for {}".format(team_name)
        return "\n".join("{}  {}  {} via {}{}  {}".format(e.get("ts"), e.get("event"), e.get("author"), e.get("via"), " " + str(e.get("pane_id")) if e.get("pane_id") else "", e.get("details") or "") for e in entries)

    return emit(args, payload, human)


# --------------------------------------------------------------------------
# charter / brief


CHARTER_ACTIONS = ("set", "edit", "history")


def _add_charter_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("what", nargs="?", help="set | edit | history | <team>")
    parser.add_argument("text", nargs="?", help="charter text for `charter set`")
    parser.add_argument("--file", metavar="PATH")
    parser.add_argument("--ref", action="append", default=[], metavar="PATH")
    parser.add_argument("--urgent", action="store_true")


def _charter_json(charter: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not charter:
        return None
    return {"seq": int(charter.get("seq", 0)), "text": charter.get("text"), "refs": list(charter.get("refs") or []), "updated_at": charter.get("updated_at"), "updated_by": charter.get("updated_by") or "human"}


def editor_command(env: Dict[str, str]) -> List[str]:
    """``$VISUAL`` or ``$EDITOR`` split like a shell would (``code --wait``), default ``vi``."""
    raw = env.get("VISUAL") or env.get("EDITOR") or "vi"
    try:
        argv = shlex.split(raw)
    except ValueError as err:
        raise HerdrTeamError("editor_failed", "cannot parse $EDITOR {!r}: {}".format(raw, err), EXIT_REFUSED, {"editor": raw})
    if not argv:
        raise HerdrTeamError("editor_failed", "$EDITOR is empty", EXIT_REFUSED, {"editor": raw})
    return argv


def run_editor(argv: List[str], path: str) -> None:
    """The one interactive subprocess of the plugin (no stdin=DEVNULL, no timeout: it is the human's editor)."""
    try:
        completed = subprocess.run(argv + [path], check=False)
    except OSError as err:
        raise HerdrTeamError("editor_failed", "cannot run editor {}: {}".format(" ".join(argv), err), EXIT_REFUSED, {"editor": argv, "errno": getattr(err, "errno", None)})
    if completed.returncode != 0:
        raise HerdrTeamError("editor_failed", "editor {} exited {}; charter unchanged".format(" ".join(argv), completed.returncode), EXIT_REFUSED, {"editor": argv, "returncode": completed.returncode})


def _editor_text(initial: str, env: Dict[str, str]) -> str:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise HerdrTeamError("no_tty", "charter edit needs a terminal; use charter set --file", EXIT_REFUSED)
    argv = editor_command(env)
    fd, name = tempfile.mkstemp(prefix="herdr-team-charter-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(initial)
        run_editor(argv, name)
        with open(name, "r", encoding="utf-8") as handle:
            return handle.read()
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


def _run_charter(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    api = api_for(args, layout)
    what = args.what
    action = what if what in CHARTER_ACTIONS else None
    positional_team = None if action else what
    if positional_team:
        team_name = _paths.validate_team_name(positional_team)
        author = _author(args, layout, api, team=team_name, require_server=False)
    else:
        author = _author(args, layout, api, require_server=False)
        resolved = resolve_team(args, layout, author)
        assert resolved is not None
        team_name = resolved
    team_paths = layout.team(team_name)
    if action is None:
        if args.text is not None:
            raise UsageError("charter set \"<text>\" to change the charter")
        doc = load_doc(team_paths)
        charter = _charter_json(charter_of(doc))
        return emit(args, {"team": team_name, "charter": charter}, lambda: render_charter(charter) if charter else "charter: none")
    if action == "history":
        history = _charter.charter_history(layout, team_name)
        return emit(args, {"team": team_name, "history": history}, lambda: "\n".join("#{} charter {} ({}): {}".format(h["seq"], h["charter_seq"], h["ts"], (h.get("text") or "").split("\n")[0][:120]) for h in history) or "no charter history")
    check_write_session(args, layout, team_name)
    if action == "edit":
        _human_only(layout, team_name, author, "charter edit")
        current = charter_of(load_doc(team_paths))
        edited = _editor_text(str((current or {}).get("text") or ""), env_of(args))
        fd, tmp = tempfile.mkstemp(prefix="herdr-team-charter-", suffix=".md", dir=os.fspath(team_paths.root))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(edited)
        try:
            charter = _charter.set_charter(layout, team_name, author, None, tmp, list(args.ref), urgent=args.urgent)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    else:
        if args.text is None and args.file is None:
            raise UsageError("charter set needs \"<text>\" or --file <path>")
        charter = _charter.set_charter(layout, team_name, author, args.text, args.file, list(args.ref), urgent=args.urgent)
    records = board_read_all(team_paths)
    record_seq = max([r["seq"] for r in records if r.get("kind") == "system" and r.get("event") == "charter_updated"] or [0])
    payload = {"team": team_name, "charter": charter.to_json(), "record_seq": record_seq, "urgent": bool(args.urgent)}
    return emit(args, payload, "charter #{} set for {} (board #{}{})".format(charter.seq, team_name, record_seq, ", urgent" if args.urgent else ""))


def _add_brief_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("name")
    parser.add_argument("--set", dest="set_text", metavar="TEXT")
    parser.add_argument("--format", choices=("text", "context"), default="text")


def _run_brief(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    api = api_for(args, layout)
    author = _author(args, layout, api, require_server=False)
    team_name = resolve_team(args, layout, author)
    assert team_name is not None
    team_paths = layout.team(team_name)
    if args.set_text is not None:
        check_write_session(args, layout, team_name)
        result = _charter.set_brief(layout, team_name, author, args.name, args.set_text)
        return emit(args, {"team": team_name, "member": result["member"], "brief": result["brief"]}, "brief for {}: {}".format(result["member"], result["brief"]))
    doc = load_doc(team_paths)
    member = next((m for m in agent_members(doc) if m.get("name") == args.name), None)
    if member is None:
        raise HerdrTeamError("member_not_found", "{!r} is not an agent member of {!r}".format(args.name, team_name), EXIT_REFUSED, {"name": args.name, "roster": [m.get("name") for m in agent_members(doc)]})
    if args.format == "context":
        charter = charter_of(doc)
        lines = ["[herdr-team briefing context for {} ({}) in team {}; the charter and your brief carry the human's authority]".format(member["name"], member.get("role"), team_name)]
        lines.append("charter #{}: {}".format(charter.get("seq"), charter.get("text")) if charter else "charter: none yet, ask human")
        if member.get("brief"):
            lines.append("your brief: {}".format(member["brief"]))
        lines.append("teammates: " + ", ".join("{} ({})".format(m.get("name"), m.get("role")) for m in members_of(doc) if m.get("name") != member["name"] and m.get("status") != "left"))
        records = board_read_all(team_paths)
        lines.append("unread posts for you: {}".format(unread_for(team_paths, records, member["name"])))
        out = getattr(args, "stdout", None) or sys.stdout
        out.write("\n".join(lines) + "\n")
        out.flush()
        return 0
    status = daemon_status(layout.session)
    if not status["alive"]:
        raise HerdrTeamError("daemon_down", "the team notifier is not running ({}); run: herdr-team daemon start".format(status.get("reason")), 5)
    job = enqueue_job(team_paths, "brief", member["name"], author)
    return emit(args, {"team": team_name, "member": member["name"], "job": job}, "briefing job {} queued for {}".format(job, member["name"]))


def _no_arguments(parser: argparse.ArgumentParser) -> None:
    pass


COMMANDS: List[Command] = [
    Command("create", "form a team from live agents (--member/--from-workspace) or fresh panes (--new --spawn)", _add_create_arguments, _run_create),
    Command("add", "add one live agent to a team", _add_add_arguments, _run_add),
    Command("remove", "remove a member (tokens and label cleared, tombstone kept)", _add_remove_arguments, _run_remove),
    Command("leave", "leave your team (from a member pane)", _no_arguments, _run_leave),
    Command("bind", "re-attach a missing member to a live agent", _add_bind_arguments, _run_bind),
    Command("dissolve", "archive a team and clear every member's tokens and labels", _add_dissolve_arguments, _run_dissolve),
    Command("use", "set the default team for human posts", _add_use_arguments, _run_use),
    Command("teams", "list the teams of this session (works offline)", _no_arguments, _run_teams),
    Command("rename", "rename a member (agent rename + roster + board note)", _add_rename_arguments, _run_rename),
    Command("me", "who am I: name, role, charter, teammates, unread", _no_arguments, _run_me),
    Command("who", "the roster with live status (reads who.json)", _add_who_arguments, _run_who),
    Command("audit", "author-mismatch and other audited events", _add_audit_arguments, _run_audit),
    Command("charter", "print, set, edit, or list the history of the team charter (human only to write)", _add_charter_arguments, _run_charter),
    Command("brief", "set a member's role brief (--set) or enqueue its briefing", _add_brief_arguments, _run_brief),
]
