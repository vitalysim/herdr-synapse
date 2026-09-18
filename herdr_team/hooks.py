"""Manifest event reconciler (``bin/hook`` slow path) and the Claude Stop decision (plan 9.4, 10).

``bin/hook <event>`` execs ``herdr-synapse hook-event <event>`` only when no
daemon is alive. The reconciler reads ``HERDR_PLUGIN_EVENT`` and
``HERDR_PLUGIN_EVENT_JSON`` (``data.pane_id`` only; a top-level ``pane_id``
is accepted too), re-fetches truth with ``agent.get``/``pane.get`` over the
socket (timeout 3 s each, ``alarm(5)`` backstop), and rewrites the roster
under ``team.lock``. The whole thing is idempotent: running it twice for the
same event changes nothing the second time. Hooks exit 0 in every case that
is not a bug in the plugin itself; they log to ``<session>/hooks.log``
(rotating 1 MiB x 3) and print one JSON object on stdout
(``docs/cli.md`` section 9, ``hook-event``).

Hook processes are ``system`` authors (plan 4.3 step 1): they inherit the
focused pane's ``HERDR_PANE_ID`` from Herdr and must never use it as their
own identity, so the identity variables are scrubbed before any API call.

``stop_decision`` is the Python side of the Claude ``Stop`` hook: it blocks
(exit 2) only for posts newer than the last blocked seq, at most three times
per 10 minutes, never while muted, never when ``stop_hook_active`` is set.
The per-member state lives in ``<team>/hooks/<name>.last_stop_block`` (the
same file ``cmd_hooks`` uses so both agree).
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from herdr_team import store
from herdr_team.api import HerdrApi, scrub_env
from herdr_team.roster import same_session, session_key, session_of, short_session, write_briefing_job
from herdr_team.errors import HerdrTeamError
from herdr_team.paths import Layout, TeamPaths, ensure_dir, ensure_session_dirs, resolve_layout, socket_allowed

EVENTS = ("agent_detected", "pane_closed", "pane_exited")
STOP_BLOCKS_PER_WINDOW = 3
STOP_WINDOW_S = 600.0
STOP_MARKER = "[herdr-team stop]"

#: Every subprocess and socket call inside a hook is bounded by this.
CALL_TIMEOUT_S = 3.0
#: Wall-clock backstop for the whole hook process (plan 10).
ALARM_S = 5
LOG_ROTATE_BYTES = 1024 * 1024
LOG_ROTATE_KEEP = 3
#: API error codes meaning "no truth available": the hook then leaves the roster alone.
UNREACHABLE_CODES = ("server_not_running", "herdr_timeout", "herdr_protocol", "herdr_unreachable")
#: Roster statuses a hook may move a member out of.
LIVE_STATUSES = ("active", "starting", "missing", "unbound", "name_conflict", "kind_changed")

_EVENT_ALIASES = {
    "pane.agent_detected": "agent_detected",
    "pane_agent_detected": "agent_detected",
    "agent_detected": "agent_detected",
    "pane.closed": "pane_closed",
    "pane_closed": "pane_closed",
    "pane.exited": "pane_exited",
    "pane_exited": "pane_exited",
}


def _now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(now.microsecond // 1000)


def _parse_iso(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


@dataclass
class HookEvent:
    event: str
    pane_id: Optional[str]
    raw: Dict[str, Any]


def normalize_event_name(name: Optional[str]) -> Optional[str]:
    if not isinstance(name, str):
        return None
    return _EVENT_ALIASES.get(name.strip())


def parse_event(env: Dict[str, str], argv_event: Optional[str] = None) -> HookEvent:
    """Minimal fields only: ``HERDR_PLUGIN_EVENT`` and ``data.pane_id`` from ``HERDR_PLUGIN_EVENT_JSON``.

    ``argv_event`` (the ``bin/hook`` argument) wins when the environment
    carries no recognisable event name. The raw JSON is parsed once and
    only ``pane_id`` is read from it; everything else is ignored on purpose
    so event-shape drift cannot break the hook.
    """
    raw: Dict[str, Any] = {}
    text = env.get("HERDR_PLUGIN_EVENT_JSON") or ""
    if text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                raw = parsed
        except ValueError:
            raw = {}
    event = normalize_event_name(env.get("HERDR_PLUGIN_EVENT")) or normalize_event_name(argv_event)
    if event is None and isinstance(raw.get("type"), str):
        event = normalize_event_name(raw.get("type"))
    pane_id: Optional[str] = None
    data = raw.get("data")
    if isinstance(data, dict) and isinstance(data.get("pane_id"), str):
        pane_id = data["pane_id"]
    elif isinstance(raw.get("pane_id"), str):
        pane_id = raw["pane_id"]
    elif isinstance(raw.get("pane"), dict) and isinstance(raw["pane"].get("pane_id"), str):
        pane_id = raw["pane"]["pane_id"]
    return HookEvent(event or (argv_event or "unknown"), pane_id, raw)


# --------------------------------------------------------------------------
# logging


def log_line(layout: Layout, message: str) -> None:
    """Append one timestamped line to ``<session>/hooks.log`` (rotating 1 MiB x 3); never raises."""
    try:
        path = layout.session.hooks_log
        ensure_dir(path.parent)
        _rotate(path)
        store.append_line(path, "{} [{}] {}".format(_now_iso(), os.getpid(), message).encode("utf-8", "replace"), fsync=False)
    except Exception:  # noqa: BLE001 - logging must never break a hook
        return


def _rotate(path: Path, keep: int = LOG_ROTATE_KEEP, limit: int = LOG_ROTATE_BYTES) -> None:
    try:
        size = os.lstat(path).st_size
    except OSError:
        return
    if size <= limit:
        return
    for index in range(keep, 0, -1):
        src = Path(os.fspath(path) + ("" if index == 1 else ".{}".format(index - 1)))
        dst = Path(os.fspath(path) + ".{}".format(index))
        try:
            if index == keep:
                try:
                    os.unlink(dst)
                except FileNotFoundError:
                    pass
            os.replace(src, dst)
        except OSError:
            continue


# --------------------------------------------------------------------------
# roster access (direct, under team.lock; the roster module's model is not required)


def _load_doc(team: TeamPaths) -> Optional[Dict[str, Any]]:
    doc = store.read_json(team.team_json, default=None)
    if not isinstance(doc, dict) or not isinstance(doc.get("members"), list):
        return None
    return doc


def _update_members(team: TeamPaths, updates: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Apply ``{name: fields}`` under ``team.lock``; skips the write when nothing changes."""
    if not updates:
        return _load_doc(team)
    with store.team_lock(team, timeout=CALL_TIMEOUT_S):
        doc = _load_doc(team)
        if doc is None:
            return None
        changed = False
        for member in doc["members"]:
            if not isinstance(member, dict):
                continue
            fields = updates.get(str(member.get("name")))
            if not fields:
                continue
            for key, value in fields.items():
                if member.get(key) != value:
                    member[key] = value
                    changed = True
        if changed:
            doc["revision"] = int(doc.get("revision") or 0) + 1
            store.write_json(team.team_json, doc)
        return doc


def _members_on_pane(doc: Dict[str, Any], pane_id: str) -> List[Dict[str, Any]]:
    from herdr_team import swap
    return [m for m in doc.get("members", []) if isinstance(m, dict) and not swap.active(m) and m.get("pane_id") == pane_id and m.get("kind") != "human" and m.get("status") in LIVE_STATUSES]


def _member_by_session(doc: Dict[str, Any], session: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The live member that recorded ``session`` (``roster.rehydrate_match`` step (0))."""
    key = session_key(session)
    if key is None:
        return None
    for m in doc.get("members", []):
        if isinstance(m, dict) and m.get("kind") != "human" and m.get("status") in LIVE_STATUSES and session_key(m.get("session")) == key:
            return m
    return None


def _member_by_terminal(doc: Dict[str, Any], terminal_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not terminal_id:
        return None
    for m in doc.get("members", []):
        if isinstance(m, dict) and m.get("terminal_id") == terminal_id and m.get("kind") != "human" and m.get("status") != "left":
            return m
    return None


def _claimed_elsewhere(session_teams: Dict[str, Dict[str, Any]], team_name: str, terminal_id: str) -> bool:
    for other_name, other in session_teams.items():
        if other_name == team_name:
            continue
        for m in other.get("members", []):
            if isinstance(m, dict) and m.get("terminal_id") == terminal_id and m.get("status") in ("active", "starting"):
                return True
    return False


# --------------------------------------------------------------------------
# API helpers, every call bounded


def _agent_get(api: Any, pane_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """``(agent, error_code)``; ``agent`` is None with ``agent_not_found`` for a shell pane."""
    try:
        result = api.request("agent.get", {"target": pane_id}, timeout=CALL_TIMEOUT_S)
    except HerdrTeamError as err:
        return None, err.code
    agent = result.get("agent") if isinstance(result, dict) else None
    if isinstance(agent, dict) and isinstance(agent.get("terminal_id"), str):
        return agent, None
    return None, "agent_not_found"


def _pane_get(api: Any, pane_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        result = api.request("pane.get", {"pane_id": pane_id}, timeout=CALL_TIMEOUT_S)
    except HerdrTeamError as err:
        return None, err.code
    pane = result.get("pane") if isinstance(result, dict) else None
    if isinstance(pane, dict):
        return pane, None
    return None, "pane_not_found"


#: The sidebar colour slots, spelled out here rather than imported: this module deliberately keeps
#: its imports minimal because it runs on every hook under a 5 s alarm. ``test_team_colors`` pins
#: these against ``roster.color_slot_keys()``, which is the source of truth.
COLOR_SLOT_KEYS = ("team_c1", "team_c2", "team_c3", "team_c4", "team_c5", "team_c6")


def _color_slot_tokens(team_name: Optional[str], slot: Any) -> Dict[str, Any]:
    """Every slot key, with the team name in its own slot and None in the rest."""
    tokens: Dict[str, Any] = {key: None for key in COLOR_SLOT_KEYS}
    if team_name and isinstance(slot, int) and not isinstance(slot, bool) and 1 <= slot <= len(COLOR_SLOT_KEYS):
        tokens[COLOR_SLOT_KEYS[slot - 1]] = team_name
    return tokens


def _color_slot_of(doc: Any) -> Optional[int]:
    config = doc.get("config") if isinstance(doc, dict) and isinstance(doc.get("config"), dict) else None
    slot = config.get("color_slot") if config else None
    return slot if isinstance(slot, int) and not isinstance(slot, bool) and 1 <= slot <= len(COLOR_SLOT_KEYS) else None


def _roster_clear_tokens() -> Dict[str, Any]:
    """The identity keys plus every colour slot, all cleared."""
    tokens: Dict[str, Any] = {"team": None, "team_role": None}
    tokens.update(_color_slot_tokens(None, None))
    return tokens


def _clear_tokens(api: Any, pane_id: str, log: Any) -> None:
    for source, tokens in (("herdr-synapse:roster", _roster_clear_tokens()), ("herdr-synapse:task", {"team_task": None})):
        try:
            api.request("pane.report_metadata", {"pane_id": pane_id, "source": source, "tokens": tokens}, timeout=CALL_TIMEOUT_S)
        except HerdrTeamError as err:
            log("clear tokens on {} failed: {}".format(pane_id, err.code))
            return


def _stamp_tokens(api: Any, pane_id: str, team_name: str, role: str, log: Any, color_slot: Optional[int] = None) -> None:
    tokens: Dict[str, Any] = {"team": team_name, "team_role": role}
    tokens.update(_color_slot_tokens(team_name, color_slot))
    try:
        api.request("pane.report_metadata", {"pane_id": pane_id, "source": "herdr-synapse:roster", "tokens": tokens}, timeout=CALL_TIMEOUT_S)
    except HerdrTeamError as err:
        log("token stamp on {} failed: {}".format(pane_id, err.code))


def _apply_name(api: Any, pane_id: str, name: str, log: Any) -> Optional[str]:
    """Re-apply the roster name; returns the resulting status override (``name_conflict``) or None."""
    try:
        api.request("agent.rename", {"target": pane_id, "name": name}, timeout=CALL_TIMEOUT_S)
        log("re-applied name {} on {}".format(name, pane_id))
        return None
    except HerdrTeamError as err:
        log("agent.rename {} on {} failed: {}".format(name, pane_id, err.code))
        if err.code == "agent_name_taken":
            return "name_conflict"
        return None


def _apply_label(api: Any, pane_id: str, label: str, log: Any) -> None:
    try:
        api.request("pane.rename", {"pane_id": pane_id, "label": label}, timeout=CALL_TIMEOUT_S)
    except HerdrTeamError as err:
        log("pane.rename {} failed: {}".format(pane_id, err.code))


# --------------------------------------------------------------------------
# reconcile


def _load_session_teams(layout: Layout) -> Dict[str, Dict[str, Any]]:
    teams: Dict[str, Dict[str, Any]] = {}
    for name in layout.session.list_teams():
        doc = _load_doc(layout.team(name))
        if doc is not None:
            teams[name] = doc
    return teams


def _write_pane_record(layout: Layout, terminal_id: str, team_name: str, name: str, generation: int, session: Optional[Dict[str, Any]] = None) -> None:
    """Merge into ``panes/<terminal_id>.json``; the Claude hook's ``hooks_last_seen`` keys survive."""
    try:
        path = layout.session.pane_record(terminal_id)
        ensure_dir(path.parent)
        record = store.read_json(path, default=None)
        if not isinstance(record, dict):
            record = {}
        record.update({"team": team_name, "name": name, "gen": int(generation)})
        if session_key(session) is not None:
            record["session"] = dict(session)  # type: ignore[arg-type]
        store.write_json(path, record, fsync=False)
    except HerdrTeamError:
        return


def reconcile(layout: Layout, api: Any, event: HookEvent, log: Any = None) -> Dict[str, Any]:
    """Update member status/name for the affected pane; returns what changed for the log.

    ``{"event", "pane_id", "team"|None, "changes": [{"team","member","fields"}...],
    "skipped": None|"no_team"|"no_pane_id"|"unknown_event"}``.
    """
    say = log or (lambda _m: None)
    out: Dict[str, Any] = {"event": event.event, "pane_id": event.pane_id, "team": None, "changes": [], "skipped": None}
    if event.event not in EVENTS:
        out["skipped"] = "unknown_event"
        return out
    if not event.pane_id:
        out["skipped"] = "no_pane_id"
        return out
    teams = _load_session_teams(layout)
    if not teams:
        out["skipped"] = "no_team"
        return out
    pane_id = event.pane_id
    agent, code = _agent_get(api, pane_id)
    if code in UNREACHABLE_CODES:
        # No truth to re-fetch: never change the roster on a guess.
        say("agent.get {} -> {}; Herdr unreachable, roster untouched".format(pane_id, code))
        out["skipped"] = "herdr_unreachable"
        return out
    if event.event in ("pane_closed", "pane_exited"):
        _reconcile_gone(layout, api, teams, pane_id, event.event, agent, out, say)
        return out
    _reconcile_detected(layout, api, teams, pane_id, agent, code, out, say)
    return out


def _record_change(out: Dict[str, Any], team_name: str, name: str, fields: Dict[str, Any]) -> None:
    out["changes"].append({"team": team_name, "member": name, "fields": dict(fields)})
    if out["team"] is None:
        out["team"] = team_name


def _reconcile_gone(layout: Layout, api: Any, teams: Dict[str, Dict[str, Any]], pane_id: str, why: str, agent: Optional[Dict[str, Any]], out: Dict[str, Any], say: Any) -> None:
    """``pane.closed``/``pane.exited``: members on that pane become ``missing`` unless an agent is still there."""
    # ``agent`` is the re-fetched truth: a still-present agent of the roster kind means nothing happened (idempotent).
    for team_name, doc in teams.items():
        updates: Dict[str, Dict[str, Any]] = {}
        for member in _members_on_pane(doc, pane_id):
            name = str(member.get("name"))
            if agent is not None and agent.get("terminal_id") == member.get("terminal_id") and agent.get("agent") == member.get("kind"):
                say("{}: {} still hosts {} after {}; nothing to do".format(team_name, pane_id, name, why))
                continue
            if member.get("status") == "missing":
                continue
            updates[name] = {"status": "missing", "last_seen_at": _now_iso()}
            if why == "pane_exited" and agent is None:
                _clear_tokens(api, pane_id, say)
        if updates:
            _update_members(layout.team(team_name), updates)
            for name, fields in updates.items():
                say("{}: {} -> {} ({})".format(team_name, name, fields["status"], why))
                _record_change(out, team_name, name, fields)
    if why == "pane_closed":
        # UI-05, daemon-dead path: a console pane closed while the server is up is recorded open:false.
        from herdr_team import cmd_ui as _cmd_ui

        if _cmd_ui.mark_console_closed_if_pane_gone(layout.session, api):
            say("console pane gone after {}; console.json open:false".format(why))
            out["console_closed"] = True


def _reconcile_detected(layout: Layout, api: Any, teams: Dict[str, Dict[str, Any]], pane_id: str, agent: Optional[Dict[str, Any]], code: Optional[str], out: Dict[str, Any], say: Any) -> None:
    # The no-daemon hook path observes the same restoration reservation as the notifier.
    with ExitStack() as stack:
        skipped = set()
        current = dict(teams)
        for name in sorted(teams):
            path = layout.team(name).root / "restore.lock"
            if not path.exists():
                continue
            lock = store.FileLock(path, timeout=0)
            if not lock.try_acquire():
                skipped.add(name)
                continue
            stack.callback(lock.release)
            current[name] = _load_doc(layout.team(name)) or teams[name]
        _reconcile_available(layout, api, current, pane_id, agent, code, out, say, skipped)


def _reconcile_available(layout: Layout, api: Any, teams: Dict[str, Dict[str, Any]], pane_id: str, agent: Optional[Dict[str, Any]], code: Optional[str], out: Dict[str, Any], say: Any, skipped: Any) -> None:
    """``pane.agent_detected``: bind by harness session, else terminal id, else label, pane id + kind, exact name; re-apply the name.

    A terminal that reports a different harness session than its member
    recorded hosts a fresh agent (crash and restart, ``/clear``, a resume by
    hand): the member keeps its name and pane, gets a new generation, and is
    briefed again, the same rule as the daemon's reconcile.

    A row whose ``agent`` is ``null`` (``launch_pending`` right after
    ``agent start``, or detection not yet run) is no evidence for or against
    the member's kind: it binds by terminal or exact name, adopts the live
    ids and keeps the status (``starting`` stays with ``create --new``, a
    ``missing`` member waits for a detected kind), the same rule as
    ``roster.rehydrate_match`` and the daemon's reconcile.
    """
    from herdr_team import swap
    teams = {name: dict(doc, members=[m for m in doc.get("members", []) if not swap.active(m)]) for name, doc in teams.items()}
    if agent is None:
        # The agent was released (or the pane hosts a shell): members on that pane are missing.
        say("agent.get {} -> {}; members on the pane become missing".format(pane_id, code))
        for team_name, doc in teams.items():
            if team_name in skipped:
                continue
            updates: Dict[str, Dict[str, Any]] = {}
            for member in _members_on_pane(doc, pane_id):
                if member.get("status") in ("active", "starting"):
                    updates[str(member.get("name"))] = {"status": "missing", "last_seen_at": _now_iso()}
            if updates:
                _clear_tokens(api, pane_id, say)
                _update_members(layout.team(team_name), updates)
                for name, fields in updates.items():
                    _record_change(out, team_name, name, fields)
        return
    terminal_id = str(agent["terminal_id"])
    live_kind = agent.get("agent")
    live_name = agent.get("name")
    live_session = session_of(agent)
    launch_pending = bool(agent.get("launch_pending"))
    pane_label: Optional[str] = None
    pane_fetched = False
    for team_name, doc in teams.items():
        if team_name in skipped:
            continue
        member = _member_by_session(doc, live_session)
        how = "session"
        if member is not None and member.get("terminal_id") != terminal_id and _claimed_elsewhere(teams, team_name, terminal_id):
            member = None
        if member is None:
            member = _member_by_terminal(doc, terminal_id)
            how = "terminal_id"
        if member is None:
            # (b) the pane carries the member's team label and hosts an agent of the member's kind.
            if not pane_fetched:
                pane_fetched = True
                pane, _code = _pane_get(api, pane_id)
                pane_label = pane.get("label") if isinstance(pane, dict) and isinstance(pane.get("label"), str) else None
            if pane_label:
                for candidate in doc.get("members", []):
                    if not isinstance(candidate, dict) or candidate.get("kind") == "human" or candidate.get("status") not in LIVE_STATUSES:
                        continue
                    label = candidate.get("label") or "team:{}/{}".format(team_name, candidate.get("role"))
                    if label == pane_label and candidate.get("kind") == live_kind and not _claimed_elsewhere(teams, team_name, terminal_id):
                        member, how = candidate, "label"
                        break
        if member is None:
            for candidate in _members_on_pane(doc, pane_id):
                if candidate.get("kind") == live_kind and not _claimed_elsewhere(teams, team_name, terminal_id):
                    member, how = candidate, "pane_id"
                    break
        if member is None and isinstance(live_name, str):
            for candidate in doc.get("members", []):
                if isinstance(candidate, dict) and candidate.get("name") == live_name and (live_kind is None or candidate.get("kind") == live_kind) and candidate.get("status") != "left" and not _claimed_elsewhere(teams, team_name, terminal_id):
                    member, how = candidate, "name"
                    break
        if member is None:
            continue
        name = str(member.get("name"))
        fields: Dict[str, Any] = {}
        if live_kind and live_kind != member.get("kind"):
            if member.get("status") != "kind_changed":
                fields["status"] = "kind_changed"
                _clear_tokens(api, pane_id, say)
                say("{}: {} now hosts {} (roster {}); kind_changed".format(team_name, name, live_kind, member.get("kind")))
        elif live_kind is None:
            # No detected kind yet: adopt ids only, keep the status, no generation bump, no rename.
            for key in ("terminal_id", "pane_id", "workspace_id", "tab_id"):
                if agent.get(key) and agent.get(key) != member.get(key):
                    fields[key] = agent.get(key)
            if agent.get("cwd") and not member.get("cwd"):
                fields["cwd"] = agent.get("cwd")
            if fields:
                say("{}: {} present on {} ({}) with no detected kind yet; ids adopted, status {} kept".format(team_name, name, terminal_id, pane_id, member.get("status")))
                role = str(member.get("role") or "")
                label = member.get("label") or "team:{}/{}".format(team_name, role)
                _apply_label(api, pane_id, str(label), say)
                _stamp_tokens(api, pane_id, team_name, role, say, _color_slot_of(doc))
                _write_pane_record(layout, terminal_id, team_name, name, int(member.get("generation") or 1))
        else:
            for key in ("terminal_id", "pane_id", "workspace_id", "tab_id"):
                if agent.get(key) and agent.get(key) != member.get(key):
                    fields[key] = agent.get(key)
            if agent.get("cwd") and not member.get("cwd"):
                fields["cwd"] = agent.get("cwd")
            status = member.get("status")
            if status != "active":
                fields["status"] = "active"
                if status in ("missing", "unbound", "starting", "kind_changed"):
                    fields["generation"] = int(member.get("generation") or 1) + 1
            rebrief = False
            if how == "session" and "terminal_id" in fields and "generation" not in fields:
                fields["generation"] = int(member.get("generation") or 1) + 1  # moved panes, memory intact
            if live_session is not None and not same_session(member.get("session"), live_session):
                fields["session"] = live_session
                if session_key(member.get("session")) is not None:
                    # A different session than recorded: a fresh agent, whether on the member's own
                    # terminal or on the pane a label or pane-id match found (same rule as the daemon).
                    rebrief = True
                    if "generation" not in fields:
                        fields["generation"] = int(member.get("generation") or 1) + 1
                    fields["briefed_at"] = None
                    say("{}: {} started a new session on {} ({} -> {}); re-briefing".format(
                        team_name, name, pane_id, short_session(member.get("session")), short_session(live_session)))
            if not launch_pending:
                if live_name is None:
                    conflict = _apply_name(api, pane_id, name, say)
                    if conflict:
                        fields["status"] = conflict
                elif isinstance(live_name, str) and live_name != name:
                    # A human rename is adopted; the old name stays resolvable for 10 minutes.
                    history = list(member.get("previous_names") or [])
                    history.append({"name": name, "retired_at": _now_iso()})
                    fields["name"] = live_name
                    fields["previous_names"] = history
                    say("{}: adopting rename {} -> {}".format(team_name, name, live_name))
            role = str(member.get("role") or "")
            label = member.get("label") or "team:{}/{}".format(team_name, role)
            _apply_label(api, pane_id, str(label), say)
            _stamp_tokens(api, pane_id, team_name, role, say, _color_slot_of(doc))
            _write_pane_record(layout, terminal_id, team_name, str(fields.get("name", name)), int(fields.get("generation", member.get("generation") or 1)), fields.get("session") or member.get("session"))
            if rebrief:
                try:
                    write_briefing_job(layout.team(team_name), str(fields.get("name", name)))
                except HerdrTeamError as err:
                    say("{}: could not enqueue a briefing for {}: {}".format(team_name, name, err.code))
        if fields:
            fields["last_seen_at"] = _now_iso()
            _update_members(layout.team(team_name), {name: fields})
            say("{}: {} matched by {}: {}".format(team_name, name, how, json.dumps(fields, ensure_ascii=False)))
            _record_change(out, team_name, name, fields)
        elif out["team"] is None:
            out["team"] = team_name
        break


# --------------------------------------------------------------------------
# entry point


def _install_alarm(seconds: int, on_fire: Any) -> bool:
    if threading.current_thread() is not threading.main_thread():
        return False
    try:
        signal.signal(signal.SIGALRM, on_fire)
        signal.alarm(int(seconds))
    except (ValueError, OSError, AttributeError):
        return False
    return True


def _daemon_alive(layout: Layout) -> bool:
    try:
        from herdr_team import daemon as _daemon

        return bool(_daemon.daemon_alive(layout.session))
    except Exception:  # noqa: BLE001 - a broken daemon module must not break the hook
        return False


def run_hook_event(argv: Sequence[str], env: Dict[str, str], api: Any = None, stdout: Any = None) -> int:
    """Entry for ``herdr-synapse hook-event <event>``; returns 0 in every case that is not a bug.

    Prints ``{"event","pane_id","team"|null,"changes":[...],"skipped":...}``
    as one JSON line on ``stdout``. ``api`` may be injected by tests.
    """
    out = stdout if stdout is not None else sys.stdout
    argv_event = argv[0] if argv else None
    env = dict(env)
    event = parse_event(env, argv_event)
    result: Dict[str, Any] = {"event": event.event, "pane_id": event.pane_id, "team": None, "changes": [], "skipped": None}
    layout: Optional[Layout] = None

    def finish() -> int:
        try:
            out.write(json.dumps(result, ensure_ascii=False) + "\n")
            out.flush()
        except (OSError, ValueError):
            pass
        return 0

    def fired(_signum: int, _frame: Any) -> None:
        result["skipped"] = "timeout"
        if layout is not None:
            log_line(layout, "hook-event {} {}: alarm after {}s".format(event.event, event.pane_id, ALARM_S))
        finish()
        os._exit(0)

    armed = _install_alarm(ALARM_S, fired)
    try:
        try:
            layout = resolve_layout(env)
        except HerdrTeamError as err:
            result["skipped"] = "layout:{}".format(err.code)
            return finish()
        try:
            ensure_session_dirs(layout.session)
        except HerdrTeamError:
            pass
        say = lambda m: log_line(layout, "hook-event {} {}: {}".format(event.event, event.pane_id, m))  # noqa: E731
        if not socket_allowed(layout.config_dir, layout.socket):
            result["skipped"] = "socket_not_allowed"
            return finish()
        if _daemon_alive(layout):
            result["skipped"] = "daemon_alive"
            return finish()
        if not layout.session.list_teams():
            result["skipped"] = "no_team"
            return finish()
        if event.event not in EVENTS:
            result["skipped"] = "unknown_event"
            say("unknown event")
            return finish()
        if api is None:
            # System author: never the focused pane's identity.
            api = HerdrApi(layout.socket, timeout=CALL_TIMEOUT_S, env=scrub_env(env))
        try:
            outcome = reconcile(layout, api, event, say)
        except HerdrTeamError as err:
            say("reconcile failed: {}".format(err))
            result["skipped"] = "error:{}".format(err.code)
            return finish()
        result.update(outcome)
        say("done: team={} changes={} skipped={}".format(result.get("team"), len(result.get("changes") or []), result.get("skipped")))
        return finish()
    except Exception as err:  # noqa: BLE001 - never leave a hook with a traceback
        result["skipped"] = "error:internal"
        result["error"] = "{}: {}".format(type(err).__name__, err)
        if layout is not None:
            log_line(layout, "hook-event {} crashed: {}: {}".format(event.event, type(err).__name__, err))
        return finish()
    finally:
        if armed:
            try:
                signal.alarm(0)
            except (ValueError, OSError):
                pass


# --------------------------------------------------------------------------
# Claude Stop decision


def _stop_state_path(team: TeamPaths, name: str) -> Path:
    stem = re.sub(r"[^A-Za-z0-9._@-]", "_", name)[:64] or "member"
    return team.root / "hooks" / (stem + ".last_stop_block")


def _cursor_state(team: TeamPaths, name: str) -> Tuple[int, "set[int]"]:
    try:
        state = store.Cursors(team).get(name)
    except (HerdrTeamError, ValueError, TypeError, AttributeError):
        return 0, set()
    try:
        seq = max(0, int(state.get("seq", 0) or 0))
    except (ValueError, TypeError, AttributeError):
        seq = 0
    seen = {int(value) for value in (state.get("seen") or []) if isinstance(value, int) and not isinstance(value, bool)}
    return seq, seen


def _cursor_seq(team: TeamPaths, name: str) -> int:
    return _cursor_state(team, name)[0]


def unread_for(team: TeamPaths, name: str, since_seq: int = 0) -> List[Dict[str, Any]]:
    """Unread board context for ``name``: to it or ``all``, from someone else, past its cursor (and ``since_seq``).

    Retracted posts and retract records are excluded; ``nudged``/``toast``
    system records never count, nor does a ``direct`` line the human typed
    into the member or its ``typed`` outcome (``store.is_direct_line``).
    Prompt-submit shows all remaining awareness; the Stop decision narrows it
    to authored mail with ``store.is_member_mail``.
    """
    cursor, seen = _cursor_state(team, name)
    floor = max(cursor, since_seq)
    try:
        records = store.BoardStore(team).read(since_seq=floor, include_retracted=False)
    except HerdrTeamError:
        records = []
    out: List[Dict[str, Any]] = []
    for record in records:
        if record.get("seq") in seen:
            continue
        if record.get("from") == name or store.is_direct_line(record):
            continue
        if record.get("kind") == "system" and record.get("event") in ("nudged", "toast"):
            continue
        to = record.get("to")
        if isinstance(to, str):
            to = [to]
        if isinstance(to, list) and (name in to or "all" in to):
            out.append(record)
    return out


def is_muted(team: TeamPaths, name: str, now: Optional[float] = None) -> bool:
    doc = store.read_json(team.mute_json, default={})
    if not isinstance(doc, dict):
        return False
    now = time.time() if now is None else now
    for key in ("*", name):
        if key not in doc:
            continue
        value = doc.get(key)
        if value is None or value is True:
            return True
        until = _parse_iso(value)
        if until is not None and until > now:
            return True
    return False


def stop_decision(layout: Layout, team: str, member: str, stdin_payload: Dict[str, Any], now: Optional[float] = None) -> "tuple[int, str]":
    """``(exit_code, stdout)`` for the Claude Stop hook: 2 blocks, 0 releases; capped, muted-aware."""
    payload = stdin_payload if isinstance(stdin_payload, dict) else {}
    if payload.get("stop_hook_active"):
        return 0, ""
    team_paths = layout.team(team)
    now = time.time() if now is None else now
    if is_muted(team_paths, member, now):
        return 0, ""
    state_path = _stop_state_path(team_paths, member)
    state = store.read_json(state_path, default={})
    if not isinstance(state, dict):
        state = {}
    last_seq = state.get("seq", 0)
    last_seq = int(last_seq) if isinstance(last_seq, int) and not isinstance(last_seq, bool) else 0
    # System and delivery records are board awareness, not authored mail. They
    # still reach ``board --new`` and prompt-submit context, but never send a
    # finished agent back into a turn just to acknowledge runtime bookkeeping.
    unread = [r for r in unread_for(team_paths, member, last_seq) if store.is_member_mail(r, member)]
    if not unread:
        return 0, ""
    blocks = [t for t in (_parse_iso(x) for x in (state.get("blocks") or [])) if t is not None and now - t < STOP_WINDOW_S]
    if len(blocks) >= STOP_BLOCKS_PER_WINDOW:
        return 0, ""
    seqs = sorted(int(r["seq"]) for r in unread if isinstance(r.get("seq"), int))
    top = seqs[-1] if seqs else last_seq
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    new_state = {
        "v": 1,
        "seq": top,
        "blocks": [datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z") for t in blocks] + [stamp],
        "updated": stamp,
    }
    try:
        ensure_dir(state_path.parent)
        store.write_json(state_path, new_state, fsync=False)
    except (HerdrTeamError, OSError):
        return 0, ""
    count = len(unread)
    span = "seq {}".format(seqs[0]) if len(seqs) == 1 else ("seq {}-{}".format(seqs[0], seqs[-1]) if seqs else "seq ?")
    message = "{} {} unread board post{} for {} ({}). Run: herdr-synapse board --new, then herdr-synapse ack, then finish.".format(
        STOP_MARKER, count, "" if count == 1 else "s", member, span,
    )
    return 2, message
