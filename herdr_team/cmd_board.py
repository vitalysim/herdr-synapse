"""Command group: post board show retract edit task ack.

This module also hosts the shared support layer (``_support`` section) that
``cmd_roster`` and ``cmd_misc`` import: author resolution, team resolution,
board append/read, cursors, roster documents, daemon liveness, job files,
and human rendering. Every helper is a thin wrapper over the real module
(``store.BoardStore``, ``sanitize``, ``render``, ``identity``, ``roster``,
``charter``, ``daemon``) so the command modules share one code path.

Contract: ``docs/cli.md`` sections 3, 7 and 10.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from herdr_team import asks as _asks
from herdr_team import charter as _charter
from herdr_team import daemon as _daemon
from herdr_team import gate as _gate
from herdr_team import identity as _identity
from herdr_team import links as _links
from herdr_team import paths as _paths
from herdr_team import render as _render
from herdr_team import roster as _roster
from herdr_team import sanitize as _sanitize
from herdr_team import store
from herdr_team import workdir as _workdir
from herdr_team.cli import api_for, emit, layout_for
from herdr_team.errors import (
    EXIT_NO_ANSWER,
    EXIT_DAEMON_DOWN,
    EXIT_ECHO_REJECTED,
    EXIT_REFUSED,
    EXIT_UNREACHABLE,
    HerdrTeamError,
    UsageError,
)
from herdr_team.identity import (
    AUTHOR_HUMAN,
    AUTHOR_SYSTEM,
    VIA_CLI,
    VIA_CONSOLE,
    VIA_CONSOLE_UNFOCUSED,
    VIA_OUTSIDE,
    VIA_POPUP,
    VIA_SYSTEM,
    Author,
)
from herdr_team.paths import Layout, SessionPaths, TeamPaths



from herdr_team.cli import Command

# --------------------------------------------------------------------------
# constants

POST_KINDS = ("note", "request", "handoff", "done", "blocked", "question", "answer")
RECORD_KINDS = POST_KINDS + ("direct", "retract", "system")
SYSTEM_EVENTS = (
    "nudged", "toast", "retracted", "expired", "abandoned", "member_gone", "member_restarted",
    "rotated", "reset_detected", "charter_updated", "renamed", "typed", "member_joined",
    "knowledge_updated", "instructions_updated", "instructions_edited", "knowledge_finding",
    "artifacts_changed", "project_set", "operator_granted", "operator_revoked",
    "context_high", "context_cleared", "context_compacted", "workdir_moved", "manager_changed",
    "model_changed", "model_applied", "restart_failed",
    "link_established", "link_broken", "link_read",
    "board_cleared",
)
HUMAN_VIAS = (VIA_CONSOLE, VIA_CONSOLE_UNFOCUSED, VIA_POPUP, VIA_OUTSIDE)
#: ``say`` (docs/cli.md section 7): only the verified team console may type into a member. A shell pane is
#: refused too, because any agent can mint a "verified cli" author by opening a pane around the command.
SAY_VIAS = (VIA_CONSOLE,)
SAY_MAX_CHARS = _sanitize.ADVISED_TEXT_CHARS
SAY_WAIT_TIMEOUT_S = 10.0
SAY_POLL_S = 0.1
#: First tokens a plain ``say`` refuses (``say_control_command``); ``--force`` (the console's ``!!``) types them.
SAY_CONTROL_WORDS = ("/exit", "/quit", "/clear", "/logout", "/login", "/resume", "exit", "quit")
BOARD_DEFAULT_LAST = 30
BOARD_NEW_LIMIT = 100
BOARD_OUTPUT_CAP_BYTES = 32 * 1024
CONTEXT_MAX_POSTS = 20
CONTEXT_MAX_BYTES = 4096
SHOW_CAT_CAP_BYTES = 64 * 1024
ATTACH_CAP_BYTES = 16 * 1024 * 1024
TAIL_SCAN_BYTES = 64 * 1024
HEARTBEAT_S = float(getattr(_daemon, "HEARTBEAT_S", 30.0))
STALE_BEAT_S = HEARTBEAT_S * 2
NAME_HISTORY_TTL_S = float(getattr(_roster, "NAME_HISTORY_TTL_S", 600.0))
TASK_MAX_AGE_S = 30 * 60
SAFE_BASENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

SECRET_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{30,}=*")),
    ("password_assignment", re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*['\"]?[^\s'\"]{8,}")),
)


# --------------------------------------------------------------------------
# _support: tiny utilities


def now_iso() -> str:
    """ISO-8601 UTC with milliseconds, e.g. ``2026-09-04T13:53:10.123Z``."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(now.microsecond // 1000)


def parse_iso(value: Any) -> Optional[float]:
    """Epoch seconds for an ISO-8601 UTC timestamp, None when unparseable."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1]
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=datetime.timezone.utc).timestamp()
    return None


def warn(args: argparse.Namespace, message: str) -> None:
    stream = getattr(args, "stderr", None) or sys.stderr
    stream.write("warning: {}\n".format(message))
    stream.flush()


def env_of(args: argparse.Namespace) -> Dict[str, str]:
    return dict(getattr(args, "env", None) or {})


def cli_path() -> str:
    return os.fspath(_paths.plugin_root() / "bin" / "herdr-synapse")


# --------------------------------------------------------------------------
# _support: sanitization fallbacks

def validate_utf8(data: bytes) -> str:
    return _sanitize.validate_utf8(data)


def strip_controls(text: str) -> str:
    return _sanitize.strip_controls(text)


def sanitize_text(text: str, max_len: int = _sanitize.MAX_TEXT_CHARS) -> str:
    return _sanitize.sanitize_text(text, max_len)


def sanitize_label(label: Optional[str]) -> Optional[str]:
    return _sanitize.sanitize_label(label)


def is_marker_text(text: str) -> bool:
    return _sanitize.is_marker_text(text)


def display_width(text: str) -> int:
    return _sanitize.display_width(text)


def truncate_columns(text: str, columns: int, ellipsis: str = "…") -> str:
    return _sanitize.truncate_columns(text, columns, ellipsis)


def headline(text: str, columns: int = _sanitize.HEADLINE_COLUMNS) -> str:
    return _sanitize.headline(text, columns)


def find_secret(text: str) -> Optional[str]:
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return label
    return None


# --------------------------------------------------------------------------
# _support: roster documents


def load_doc(team: TeamPaths) -> Dict[str, Any]:
    """Parsed ``team.json`` (raises ``team_not_found``)."""
    return store.RosterStore(team).load()


def save_doc(team: TeamPaths, doc: Dict[str, Any], expected_revision: Optional[int] = None) -> Dict[str, Any]:
    return store.RosterStore(team).save(doc, expected_revision)


def update_doc(team: TeamPaths, mutate: Callable[[Dict[str, Any]], Any]) -> Dict[str, Any]:
    """Lock, load, ``mutate(doc)``, save with a revision check; retried on conflict."""
    return store.RosterStore(team).update(mutate)


def members_of(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    members = doc.get("members")
    return [m for m in members if isinstance(m, dict)] if isinstance(members, list) else []


def agent_members(doc: Dict[str, Any], include_left: bool = False) -> List[Dict[str, Any]]:
    out = []
    for member in members_of(doc):
        if member.get("kind") == "human" or member.get("name") == AUTHOR_HUMAN:
            continue
        if not include_left and member.get("status") == "left":
            continue
        out.append(member)
    return out


def member_or_raise(doc: Dict[str, Any], name: str, team_name: str) -> Dict[str, Any]:
    """The agent member called ``name`` (not human, not left), else ``member_not_found`` (exit 1)."""
    for member in agent_members(doc):
        if member.get("name") == name:
            return member
    details: Dict[str, Any] = {"name": name, "roster": [m.get("name") for m in agent_members(doc)]}
    if name in ("all", AUTHOR_HUMAN, "me") or name.startswith("role:"):
        details["hint"] = "one agent member at a time; post --to {} addresses a group".format(name)
    raise HerdrTeamError("member_not_found", "{!r} is not an agent member of {!r}".format(name, team_name), EXIT_REFUSED, details)


def find_member(doc: Dict[str, Any], name: str, allow_retired: bool = True) -> Optional[Dict[str, Any]]:
    """By current name, ``terminal_id``, or a name retired under ``NAME_HISTORY_TTL_S``."""
    for member in members_of(doc):
        if member.get("name") == name or (name and member.get("terminal_id") == name):
            return member
    if allow_retired:
        now = time.time()
        for member in members_of(doc):
            for old in member.get("previous_names") or []:
                if not isinstance(old, dict) or old.get("name") != name:
                    continue
                retired = parse_iso(old.get("retired_at"))
                if retired is None or now - retired <= NAME_HISTORY_TTL_S:
                    return member
    return None


def role_holders(doc: Dict[str, Any], role: str) -> List[Dict[str, Any]]:
    return [m for m in agent_members(doc) if m.get("role") == role]


def charter_of(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    charter = doc.get("charter")
    if isinstance(charter, dict) and charter.get("text"):
        return charter
    return None


def charter_headline(charter: Optional[Dict[str, Any]], max_chars: int = _charter.HEADLINE_CHARS) -> Optional[str]:
    if not charter:
        return None
    first = str(charter.get("text") or "").strip().split("\n", 1)[0].strip()
    if len(first) > max_chars:
        first = first[: max_chars - 1].rstrip() + "…"
    return first


def charter_summary(charter: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not charter:
        return None
    return {"seq": int(charter.get("seq", 0)), "headline": charter_headline(charter)}


# --------------------------------------------------------------------------
# _support: session-level files


def read_console_json(session: SessionPaths) -> Dict[str, Any]:
    doc = store.read_json(session.console_json)
    return doc if isinstance(doc, dict) else {}


def write_console_json(session: SessionPaths, doc: Dict[str, Any]) -> None:
    _paths.ensure_session_dirs(session)
    store.write_json(session.console_json, doc)


def default_team_of(session: SessionPaths) -> Optional[str]:
    value = read_console_json(session).get("default_team")
    return value if isinstance(value, str) and value else None


def workspace_team(session: SessionPaths, workspace_id: Optional[str]) -> Optional[str]:
    """The one team whose agents live in ``workspace_id``, else None.

    A Herdr space is where a team physically *is*, so it answers "which team
    do I mean" better than a session-wide ``default_team`` can: the operator
    pressing the console action in the GitLab space means the GitLab team,
    whatever they last ran ``use`` on. Before this existed, the second team of
    a session was unreachable from its own space (the console action opened
    the default team's board instead).

    Nothing is guessed. A space with no team, or with two, returns None and
    the caller keeps its own fallback; only ``status: "left"`` members and the
    ``human`` row are ignored, because the human sits in no space in
    particular. Sessions with fewer than two teams skip the reads entirely --
    there is nothing to disambiguate and the caller's fallbacks are cheaper.
    """
    if not workspace_id:
        return None
    known = session.list_teams()
    if len(known) < 2:
        return None
    found: Optional[str] = None
    for name in known:
        try:
            doc = store.read_json(session.team(name).team_json, default=None)
        except (HerdrTeamError, OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        if not any(m.get("workspace_id") == workspace_id for m in agent_members(doc)):
            continue
        if found is not None:
            return None  # two teams share the space; the caller must be told, not guessed at
        found = name
    return found


def inferred_team(layout: Layout, env: Mapping[str, str]) -> Optional[str]:
    """``workspace_team`` for this invocation's space, else the session ``default_team``."""
    return workspace_team(layout.session, _paths.env_workspace(env)) or default_team_of(layout.session)


# --------------------------------------------------------------------------
# the console registry (docs/cli.md section 7)
#
# ``console.json`` used to be one flat record with a single slot for
# ``terminal_id``/``pane_id``/``pid``/``human_label``, which is what limited a
# session to one console: ``identity._tier_console`` proves a caller is the
# console by comparing its terminal against that one value, so a second console
# overwrote the record and silently demoted the first to ``cli-unverified``
# (HP-07, 2026-09-05). The registry keys those fields by ``terminal_id`` so a
# team can have its own board open without breaking anybody else's.
#
# ``default_team`` stays a single session-wide field: it is what every CLI
# command in the session infers its team from, not a per-console value.


CONSOLE_SCHEMA = 2
#: Fields that belong to one console rather than to the session.
#: ``launched_at`` is deliberately NOT here: it is the session-wide boot grace
#: ``reconcile_console`` reads, and moving it into an entry silently disabled it.
CONSOLE_ENTRY_KEYS = ("pane_id", "terminal_id", "team", "pid", "open", "human_label", "opened_at", "closed_at")


def console_doc(session: SessionPaths) -> Dict[str, Any]:
    """``console.json`` normalised to the registry shape.

    A pre-registry document (one flat record, no ``consoles``) is read as a
    one-entry registry, so an upgrade needs no migration pass and an older
    plugin still finds the fields it expects where it left them.
    """
    doc = read_console_json(session)
    consoles = doc.get("consoles")
    if isinstance(consoles, dict):
        entries = {str(k): dict(v) for k, v in consoles.items() if isinstance(v, dict)}
    else:
        entries = {}
        terminal = doc.get("terminal_id")
        if isinstance(terminal, str) and terminal:
            entries[terminal] = {key: doc[key] for key in CONSOLE_ENTRY_KEYS if key in doc}
            entries[terminal].setdefault("team", doc.get("default_team"))
    out = {k: v for k, v in doc.items() if k not in CONSOLE_ENTRY_KEYS and k != "consoles"}
    out["schema"] = CONSOLE_SCHEMA
    out["consoles"] = entries
    return out


def console_entries(session: SessionPaths) -> Dict[str, Dict[str, Any]]:
    """Every recorded console, keyed by ``terminal_id``."""
    return console_doc(session).get("consoles") or {}


def console_entry(session: SessionPaths, terminal_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not terminal_id:
        return None
    return console_entries(session).get(str(terminal_id))


def live_console_entries(session: SessionPaths, alive: Any = None) -> Dict[str, Dict[str, Any]]:
    """Recorded consoles that say ``open`` and whose pid is still running."""
    check = alive if alive is not None else _pid_is_alive
    out: Dict[str, Dict[str, Any]] = {}
    for terminal, entry in console_entries(session).items():
        if entry.get("open") and check(entry.get("pid")):
            out[terminal] = entry
    return out


def _pid_is_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def update_console_doc(session: SessionPaths, mutate: Any) -> Dict[str, Any]:
    """Read-modify-write ``console.json`` under the session lock.

    Several console processes now write this file, and every writer replaces
    the whole document, so an unlocked read-modify-write would silently drop a
    concurrent console's entry.
    """
    _paths.ensure_session_dirs(session)
    with store.FileLock(session.console_lock):
        doc = console_doc(session)
        mutate(doc)
        doc["schema"] = CONSOLE_SCHEMA
        store.write_json(session.console_json, doc)
        return doc


def upsert_console(session: SessionPaths, terminal_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Create or update one console's entry, leaving every other entry alone."""
    def mutate(doc: Dict[str, Any]) -> None:
        entry = dict(doc["consoles"].get(terminal_id) or {})
        entry.update(fields)
        entry["terminal_id"] = terminal_id
        doc["consoles"][terminal_id] = entry

    return update_console_doc(session, mutate)


def close_console(session: SessionPaths, terminal_id: str, closed_at: Optional[str] = None) -> Dict[str, Any]:
    """Mark one console closed. The entry is kept: it records which team that pane held."""
    def mutate(doc: Dict[str, Any]) -> None:
        entry = dict(doc["consoles"].get(terminal_id) or {})
        if not entry:
            return
        entry.update({"open": False, "pid": None, "closed_at": closed_at or now_iso()})
        doc["consoles"][terminal_id] = entry

    return update_console_doc(session, mutate)


def console_for_team(session: SessionPaths, team: str, alive: Any = None) -> Optional[Dict[str, Any]]:
    """The live console showing ``team``, if one is open."""
    for entry in live_console_entries(session, alive).values():
        if entry.get("team") == team:
            return entry
    return None


def mute_state(team: TeamPaths) -> Dict[str, Any]:
    doc = store.read_json(team.mute_json)
    return doc if isinstance(doc, dict) else {}


def nudges_state(team: TeamPaths) -> str:
    until = mute_state(team).get("*")
    if until is None and "*" not in mute_state(team):
        return "on"
    if until is None:
        return "paused"
    ts = parse_iso(until)
    return "paused" if ts is None or ts > time.time() else "on"


def view_state(session: SessionPaths) -> str:
    doc = store.read_json(session.view_json)
    if isinstance(doc, dict):
        value = doc.get("view", doc.get("on"))
        if value in (True, "on"):
            return "on"
    return "off"




def toast_delivery(config_dir: Path) -> str:
    """``[ui.toast] delivery`` from ``<config_dir>/config.toml`` (the daemon owns the parser)."""
    return _daemon.read_toast_delivery(config_dir)


# --------------------------------------------------------------------------
# _support: daemon liveness


def process_start_time(pid: int) -> Optional[str]:
    return _daemon.process_start_time(pid)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def daemon_status(session: SessionPaths) -> Dict[str, Any]:
    """``daemon.json`` interpreted: pid alive, start time matching, beat age."""
    info = store.read_json(session.daemon_json)
    status: Dict[str, Any] = {
        "alive": False, "pid": None, "start_time": None, "beat_at": None, "beat_age_s": None,
        "socket": None, "herdr_version": None, "protocol": None, "version": None, "reason": "no daemon.json",
    }
    if not isinstance(info, dict):
        return status
    for key in ("pid", "start_time", "beat_at", "socket", "herdr_version", "protocol", "version"):
        status[key] = info.get(key)
    beat = parse_iso(info.get("beat_at"))
    if beat is not None:
        status["beat_age_s"] = round(max(0.0, time.time() - beat), 1)
    try:
        pid = int(info.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if not pid_alive(pid):
        status["reason"] = "pid {} is not running".format(pid)
        return status
    recorded = info.get("start_time")
    if isinstance(recorded, str) and recorded.strip():
        actual = process_start_time(pid)
        if actual is not None and actual != recorded.strip():
            status["reason"] = "pid {} start time differs".format(pid)
            return status
    if status["beat_age_s"] is not None and status["beat_age_s"] > STALE_BEAT_S:
        status["reason"] = "heartbeat stale ({}s)".format(status["beat_age_s"])
        return status
    status["alive"] = True
    status["reason"] = None
    return status


def notifier_state(session: SessionPaths) -> str:
    return "alive" if daemon_status(session)["alive"] else "offline"


def require_daemon(session: SessionPaths) -> None:
    status = daemon_status(session)
    if not status["alive"]:
        raise HerdrTeamError("daemon_down", "the team notifier is not running ({}); run: herdr-synapse daemon start".format(status.get("reason")), EXIT_DAEMON_DOWN)


def enqueue_job(team: TeamPaths, kind: str, member: Optional[str], author: Author, force: bool = False, extra: Optional[Dict[str, Any]] = None) -> str:
    """Write ``notifier/jobs/<ts>-<id>.json`` and return the job id."""
    _paths.ensure_dir(team.jobs_dir)
    ts = now_iso()
    job_id = hashlib.sha1("{}:{}:{}:{}:{}".format(ts, kind, member, os.getpid(), time.monotonic()).encode()).hexdigest()[:10]
    job: Dict[str, Any] = {
        "v": 1, "kind": kind, "member": member, "force": bool(force),
        "requested_by": {"name": author.name, "via": author.via, "verified": author.verified},
        "requested_at": ts,
    }
    if extra:
        job.update(extra)
    store.write_json(team.jobs_dir / "{}-{}.json".format(ts.replace(":", "").replace(".", ""), job_id), job)
    return job_id


# --------------------------------------------------------------------------
# _support: board records


def _grammar_ok(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and isinstance(record.get("seq"), int)
        and isinstance(record.get("from"), str)
        and isinstance(record.get("to"), list)
        and isinstance(record.get("kind"), str)
    )


def _parse_lines(raw: bytes) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not raw:
        return out
    lines = raw.split(b"\n")
    if not raw.endswith(b"\n"):
        lines = lines[:-1]  # drop the torn fragment
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if _grammar_ok(record):
            out.append(record)
    return out


def _archive_segments(team: TeamPaths) -> List[Tuple[int, int, Path]]:
    segments: List[Tuple[int, int, Path]] = []
    try:
        names = os.listdir(team.archive_dir)
    except OSError:
        return segments
    for name in names:
        match = re.match(r"^board\.(\d+)-(\d+)\.jsonl$", name)
        if match:
            segments.append((int(match.group(1)), int(match.group(2)), team.archive_dir / name))
    segments.sort()
    return segments


def board_read_all(team: TeamPaths, include_archive: bool = False) -> List[Dict[str, Any]]:
    """Every parseable record, sorted and deduped by seq (filters are applied by the caller)."""
    return board_read_all_detailed(team, include_archive=include_archive)[0]


def board_read_all_detailed(team: TeamPaths, include_archive: bool = False) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """``board_read_all`` plus what the store skipped: ``{"corrupt", "fragment", "duplicates", "grammar"}`` line counts.

    ``corrupt`` lines are unparseable JSON, ``fragment`` a torn last line,
    ``duplicates`` repeated seqs, ``grammar`` parseable records that fail the
    plan 6.1 grammar (M5 F-03: ``board`` used to hide all of them).
    """
    records, stats = store.BoardStore(team).read_detailed(include_archive=include_archive)
    kept = [r for r in records if _grammar_ok(r)]
    skipped = {
        "corrupt": int(stats.get("corrupt") or 0),
        "fragment": int(stats.get("fragment") or 0),
        "duplicates": int(stats.get("duplicates") or 0),
        "grammar": len(records) - len(kept),
    }
    return sorted(kept, key=lambda r: r["seq"]), skipped


def board_get(team: TeamPaths, seq: int) -> Optional[Dict[str, Any]]:
    return store.BoardStore(team).get(seq)


def board_max_seq(team: TeamPaths) -> int:
    records = board_read_all(team, include_archive=False)
    tail = records[-1]["seq"] if records else 0
    seq_doc = store.read_json(team.board_seq)
    nxt = int(seq_doc.get("next", 1)) - 1 if isinstance(seq_doc, dict) and isinstance(seq_doc.get("next"), int) else 0
    archive_max = max([last for _f, last, _p in _archive_segments(team)] or [0])
    return max(tail, nxt, archive_max, 0)


def board_append(team: TeamPaths, record: Dict[str, Any], prepare: Optional[Callable[[int], None]] = None, spill_text: Optional[str] = None, attachments: Optional[Sequence[store.StagedAttachment]] = None) -> int:
    """Append under ``team.lock``; ``spill_text`` lands in ``payloads/<seq>-body.md`` inside the same lock.

    ``attachments`` are ``BoardStore.stage_attachment`` results (copied
    before the lock) committed as ``payloads/<seq>-<basename>`` under it
    (plan 6.1). ``prepare(seq)`` is kept for callers that stage extra files
    keyed by the seq; the store assigns the seq, so it runs after the append.
    """
    seq = store.BoardStore(team).append(record, attachments=attachments, spill_text=spill_text)
    if prepare is not None:
        prepare(seq)
    return int(seq)


def build_record(
    author: Author,
    to: List[str],
    kind: str,
    text: str,
    to_role: Optional[str] = None,
    refs: Optional[List[str]] = None,
    reply_to: Optional[int] = None,
    retracts: Optional[int] = None,
    supersedes: Optional[int] = None,
    urgent: bool = False,
    relayed_for: Optional[str] = None,
    socket_path: Optional[str] = None,
    truncated: bool = False,
    event: Optional[str] = None,
    from_gen: int = 1,
) -> Dict[str, Any]:
    origin = dict(author.origin or {})
    origin.setdefault("via", author.via)
    origin.setdefault("verified", bool(author.verified))
    origin.setdefault("pid", os.getpid())
    origin.setdefault("ppid", os.getppid())
    origin.setdefault("workspace_id", author.workspace_id)
    origin.setdefault("tab_id", author.tab_id)
    origin.setdefault("socket", socket_path)
    return {
        "v": 1, "seq": 0, "ts": None,
        "from": author.name, "from_label": author.from_label, "from_kind": author.kind,
        "from_pane": author.pane_id, "from_terminal": author.terminal_id, "from_gen": from_gen,
        "origin": origin,
        "to": list(to), "to_role": to_role, "kind": kind, "text": text, "refs": list(refs or []),
        "reply_to": reply_to, "retracts": retracts, "supersedes": supersedes,
        "urgent": bool(urgent), "ttl_ms": None, "truncated": bool(truncated), "event": event,
        "relayed_for": relayed_for,
    }


def system_author(layout: Layout) -> Author:
    return Author(AUTHOR_SYSTEM, None, VIA_SYSTEM, True, origin={"via": VIA_SYSTEM, "verified": True, "socket": os.fspath(layout.socket)})


def append_system_record(layout: Layout, team: TeamPaths, event: str, text: str, to: Optional[List[str]] = None, refs: Optional[List[str]] = None, urgent: bool = False, extra: Optional[Dict[str, Any]] = None, reply_to: Optional[int] = None) -> int:
    record = build_record(system_author(layout), to or ["all"], "system", text, refs=refs, urgent=urgent, socket_path=os.fspath(layout.socket), event=event, reply_to=reply_to)
    if extra:
        record.update(extra)
    return board_append(team, record)


def record_is_unverified(record: Dict[str, Any]) -> bool:
    """Plan 6.1 reader rules: forged ``human`` or ``system`` records render ``(unverified)``."""
    origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
    sender = record.get("from")
    if sender == AUTHOR_SYSTEM:
        return record.get("kind") != "system" or not record.get("event")
    if sender == AUTHOR_HUMAN:
        via = origin.get("via")
        if via in HUMAN_VIAS:
            return False
        return not (via == VIA_CLI and origin.get("verified") is True)
    return not origin.get("verified", False)


# --------------------------------------------------------------------------
# _support: cursors


def cursor_get(team: TeamPaths, reader: str) -> Dict[str, Any]:
    return store.Cursors(team).get(reader)


def cursor_advance(team: TeamPaths, reader: str, seq: int, terminal_id: Optional[str], surfaced_by: str = "cli", seen: Optional[Sequence[int]] = None, touch: bool = False) -> Dict[str, Any]:
    return store.Cursors(team).advance(reader, seq, terminal_id, surfaced_by, seen=seen, touch=touch)


def cursors_all(team: TeamPaths) -> Dict[str, Dict[str, Any]]:
    return store.Cursors(team).all()


def addressed_to(record: Dict[str, Any], reader: str, is_human: bool) -> bool:
    to = record.get("to") or []
    if "all" in to or reader in to:
        return True
    return is_human and AUTHOR_HUMAN in to


def inbox_record(record: Dict[str, Any], reader: str, is_human: bool) -> bool:
    """The reader's mail: addressed to it, not its own, and (for a member) not a line the human typed into it.

    A ``direct`` record is already in the member's input box and the daemon's
    ``typed`` outcome is addressed to the human, so neither is unread mail for
    a member (docs/cli.md section 7, ``say``); the human's inbox lists both.
    """
    if record.get("from") == reader or not addressed_to(record, reader, is_human):
        return False
    return is_human or not store.is_direct_line(record)


def unread_for(team: TeamPaths, records: Sequence[Dict[str, Any]], reader: str, is_human: bool = False) -> int:
    state = cursor_get(team, reader)
    cursor = int(state.get("seq", 0))
    seen = set(state.get("seen") or [])
    return sum(1 for r in records if r["seq"] > cursor and r["seq"] not in seen and inbox_record(r, reader, is_human))


# --------------------------------------------------------------------------
# _support: audit


def audit(layout: Layout, team: str, event: str, author: Author, details: Optional[Dict[str, Any]] = None) -> None:
    _identity.audit(layout, team, event, author, details)


# --------------------------------------------------------------------------
# _support: author and team resolution


def verify_shell_ancestry(api: Any, pane_id: str, own_pgrp: int) -> Tuple[bool, Optional[str]]:
    return tuple(_identity.verify_shell_ancestry(api, pane_id, own_pgrp))  # type: ignore[return-value]


def resolve_author(args: argparse.Namespace, layout: Layout, api: Any, team: Optional[str] = None, as_human: bool = False, relayed_for: Optional[str] = None, label: Optional[str] = None, require_server: bool = True) -> Author:
    """Plan 4.3 tiers through ``identity.resolve_author`` with the local fallback."""
    env = env_of(args)
    # ``team`` is explicit when --team / HERDR_TEAM / HERDR_TEAM_DIR named it; a hint that came only
    # from ``default_team`` must not hide a member pane of another team (plan 4.3, M5 rig finding).
    explicit = team is None or _paths.team_name_from_arg(getattr(args, "team", None), env) is not None
    author = _identity.resolve_author(env, layout, api, team=team, as_human=as_human, relayed_for=relayed_for, label=label, require_server=require_server, team_explicit=explicit)
    if author.reason and not author.verified and author.via != VIA_OUTSIDE:
        warn(args, "author unverified: {}".format(author.reason))
    return author


def resolve_team(args: argparse.Namespace, layout: Layout, author: Optional[Author] = None, required: bool = True) -> Optional[str]:
    """Team name from ``--team``, env, the author's roster, this space's team, ``default_team``, or the only team."""
    env = env_of(args)
    name = _paths.team_name_from_arg(getattr(args, "team", None), env)
    if name is None and author is not None and author.team:
        name = author.team
    if name is None:
        # The space outranks ``default_team`` because it is the more local
        # fact: a session-wide default cannot be right in two spaces at once.
        name = inferred_team(layout, env)
    known = layout.session.list_teams()
    if name is None:
        if len(known) == 1:
            name = known[0]
        elif len(known) > 1:
            raise HerdrTeamError("team_ambiguous", "several teams in this session; pass --team or run: herdr-synapse use <team>", EXIT_REFUSED, {"teams": known})
        elif required:
            raise HerdrTeamError("team_not_found", "no team exists in this session", EXIT_REFUSED, {"teams": []})
        else:
            return None
    if name not in known:
        if not required:
            return name
        raise HerdrTeamError("team_not_found", "team {} does not exist in this session".format(name), EXIT_REFUSED, {"team": name, "teams": known})
    return name


def reader_id(author: Author) -> str:
    if author.is_member:
        return author.name
    if author.name == AUTHOR_SYSTEM:
        return "human@system"
    return "human@" + (author.from_label or "human")


def member_generation(doc: Dict[str, Any], author: Author) -> int:
    member = find_member(doc, author.name, allow_retired=False) if author.is_member else None
    try:
        return int(member.get("generation", 1)) if member else 1
    except (TypeError, ValueError):
        return 1


# --------------------------------------------------------------------------
# _support: refs and attachments


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def roster_roots(layout: Layout, team_name: str, doc: Optional[Dict[str, Any]], env: Optional[Dict[str, str]] = None) -> List[Path]:
    """The team dir plus every member cwd that is a safe root (``charter.safe_roots``: never HOME or ``/``)."""
    roots = [layout.team(team_name).root.resolve()]
    for member in members_of(doc or {}):
        cwd = member.get("cwd")
        if isinstance(cwd, str) and cwd:
            roots.append(Path(cwd).resolve())
    return _charter.safe_roots(roots, env if env is not None else {})


def validate_refs(layout: Layout, team_name: str, refs: List[str], doc: Optional[Dict[str, Any]] = None, env: Optional[Dict[str, str]] = None) -> List[str]:
    if not refs:
        return []
    return list(_charter.validate_refs(layout, team_name, refs, env=env))


def resolve_files(layout: Layout, team_name: str, files: Sequence[str], doc: Optional[Dict[str, Any]] = None, env: Optional[Dict[str, str]] = None) -> Tuple[List[str], List[str]]:
    """``--file`` (the console's ``@@path``): ``(refs, sources to attach)``.

    A file the team can already read (under the team dir or a member's cwd)
    becomes a ``--ref``; anything else is copied into ``payloads/`` like
    ``--attach``. A relative path is looked up under the caller's cwd first,
    then under every member's cwd (the console's ``@@`` finder inserts
    project-relative paths). A missing file is ``ref_invalid`` and a file
    under a dot-directory (``.ssh``, ``.aws``, ``.config`` ...) is refused
    either way.
    """
    refs: List[str] = []
    attach: List[str] = []
    roots = [Path.cwd()] + [Path(r) for r in _roster.member_roots(members_of(doc) if doc else [])]
    for raw in files:
        if not isinstance(raw, str) or not raw.strip():
            raise HerdrTeamError("ref_invalid", "empty --file", EXIT_REFUSED, {"ref": raw})
        source = Path(os.path.expanduser(raw))
        if not source.is_absolute():
            source = next((root / source for root in roots if (root / source).is_file()), roots[0] / source)
        if not source.is_file():
            raise HerdrTeamError("ref_invalid", "file does not exist under your directory or any member's: {}".format(raw), EXIT_REFUSED, {"ref": raw, "searched": [os.fspath(r) for r in roots]})
        project_dir = _workdir.project_dir_of(doc)
        if any(part.startswith(".") and part not in (".", "..") for part in source.resolve().parts[1:]) and not _workdir.is_inside(source, project_dir):
            # The team's own ``.herdr-synapse/artifacts/`` is the exception: it is
            # where members are told to leave work products, and it is ours.
            raise HerdrTeamError("ref_invalid", "file sits under a dot-directory (.ssh, .aws, .config ...): {}".format(raw), EXIT_REFUSED, {"ref": raw, "path": os.fspath(source)})
        try:
            for ref in validate_refs(layout, team_name, [os.fspath(source)], doc, env=env):
                if ref not in refs:
                    refs.append(ref)
        except HerdrTeamError as err:
            if err.code != "ref_invalid":
                raise
            attach.append(os.fspath(source))
    return refs, attach


def stage_attachments(team: TeamPaths, sources: Sequence[str]) -> List[store.StagedAttachment]:
    """Copy every ``--attach`` into ``payloads/.tmp-*`` before the lock (``BoardStore.stage_attachment``)."""
    board = store.BoardStore(team)
    staged: List[store.StagedAttachment] = []
    try:
        for source in sources:
            staged.append(board.stage_attachment(os.path.expanduser(source)))
    except BaseException:
        board.discard_staged(staged)
        raise
    return staged


def _hidden_parts(path: Path, root: Path) -> bool:
    """True when ``path`` sits under a dot-directory (or is a dot-file) relative to ``root``."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return any(part.startswith(".") for part in relative.parts)


# --------------------------------------------------------------------------
# _support: rendering fallbacks


def glyph_for_kind(kind: str, ascii_only: bool = False) -> str:
    return _render.glyph_for_kind(kind, ascii_only)


def render_post(record: Dict[str, Any], ascii_only: bool = False, receipts: Optional[Dict[str, Any]] = None) -> str:
    return _render.render_post(record, ascii_only, receipts)


def render_board(records: Iterable[Dict[str, Any]], ascii_only: bool = False, receipts: Optional[Dict[int, Dict[str, Any]]] = None) -> str:
    return _render.render_board(records, ascii_only, receipts)


def render_context(records: Iterable[Dict[str, Any]], max_bytes: int = CONTEXT_MAX_BYTES, max_posts: int = CONTEXT_MAX_POSTS) -> str:
    return _render.render_context(records, max_bytes, max_posts)


def render_charter(charter: Dict[str, Any]) -> str:
    return _render.render_charter(charter)


# --------------------------------------------------------------------------
# shared command scaffolding


def check_write_session(args: argparse.Namespace, layout: Layout, team_name: str) -> None:
    """Plan 12 (RS-08): a write against a team whose ``team.json`` socket differs from the resolved socket is refused.

    ``team_session_mismatch`` (exit 1) unless ``--session-mismatch-ok`` or an
    explicit ``--socket`` says the caller means it. The check is skipped when
    the socket came from the *default* fallback (nothing configured): that is
    the outside-Herdr ``--team <path>`` case of plan 7 / HP-05, where the
    append must work offline.
    """
    if getattr(args, "session_mismatch_ok", False) or getattr(args, "socket", None):
        return
    if layout.env_socket.source == _paths.SOCKET_SOURCE_DEFAULT:
        return
    team = _roster.load_team(layout.team(team_name))
    _roster.check_session(layout, team)


def _open_team(args: argparse.Namespace, require_server: bool, as_human: bool = False, relayed_for: Optional[str] = None, label: Optional[str] = None, write: bool = False) -> Tuple[Layout, Any, Author, str, TeamPaths, Dict[str, Any]]:
    layout = layout_for(args)
    api = api_for(args, layout)
    hinted = resolve_team(args, layout, None, required=False)
    author = resolve_author(args, layout, api, team=hinted, as_human=as_human, relayed_for=relayed_for, label=label, require_server=require_server)
    team_name = resolve_team(args, layout, author)
    if write:
        check_write_session(args, layout, team_name)
    team = layout.team(team_name)
    doc = load_doc(team)
    return layout, api, author, team_name, team, doc


def require_member(author: Author, command: str) -> None:
    if not author.is_member:
        raise HerdrTeamError("not_a_member", "{} runs from a member pane; you are {}".format(command, author.name), EXIT_UNREACHABLE, {"author": author.name, "via": author.via})


def parse_seq(value: Any, what: str = "seq") -> int:
    try:
        seq = int(value)
    except (TypeError, ValueError):
        raise UsageError("{} must be an integer".format(what))
    if seq <= 0:
        raise UsageError("{} must be positive".format(what))
    return seq


# --------------------------------------------------------------------------
# post


def _add_post_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("text", help="post text (<= 2000 chars; 500 advised)")
    parser.add_argument("--to", metavar="TARGET", action="append", help="name[,name...] | all | human | role:<r> (repeatable)")
    wait = parser.add_mutually_exclusive_group()
    wait.add_argument("--wait", dest="wait", action="store_true", default=None, help="block until the operator replies (default for asking kinds; see ask-policy)")
    wait.add_argument("--no-wait", dest="wait", action="store_false", default=None, help="never block, whatever the team policy says")
    parser.add_argument("--timeout", metavar="DURATION", help="how long to wait before giving up (default from ask-policy)")
    parser.add_argument("--kind", choices=POST_KINDS, default="note")
    parser.add_argument("--ref", metavar="PATH", action="append", default=[], help="reference a file under the team dir, payloads/, or a member cwd")
    parser.add_argument("--attach", metavar="PATH", action="append", default=[], help="copy a file into payloads/ and reference it")
    parser.add_argument("--file", metavar="PATH", action="append", default=[], help="reference the file when it lives under the team dir or a member's cwd, else copy it into payloads/ (the console's @@path)")
    parser.add_argument("--reply-to", metavar="SEQ", type=int)
    parser.add_argument("--urgent", action="store_true")
    parser.add_argument("--interrupt", action="store_true", help="urgent, and the notifier may type the nudge into the recipient's running turn (named recipients only; one per recipient per cooldown)")
    parser.add_argument("--spill", action="store_true", help="write over-length text to payloads/<seq>-body.md")
    parser.add_argument("--as", dest="as_who", choices=("human",), help="human authorship (refused from an agent pane)")
    parser.add_argument("--name", metavar="LABEL", help="human label, honoured only when verified")
    parser.add_argument("--relayed-for", dest="relayed_for", choices=("human",), help="a member relaying the operator")
    parser.add_argument("--to-any", dest="to_any", action="store_true", help="skip roster validation of --to")
    parser.add_argument("--force", action="store_true", help="post even when the text looks like a secret")


def resolve_recipients(doc: Dict[str, Any], to_args: Optional[List[str]], author: Author, to_any: bool = False) -> Tuple[List[str], Optional[str]]:
    """Expand ``--to`` against the roster; ``role:<r>`` records ``to_role``."""
    roster_names = [m.get("name") for m in agent_members(doc)]
    if not to_args:
        # A post with no recipient goes to the whole team, whoever wrote it
        # (owner decision 2026-09-05; previously a member's post defaulted to human).
        return ["all"], None
    entries: List[str] = []
    for arg in to_args:
        entries.extend(part.strip() for part in arg.split(",") if part.strip())
    if not entries:
        raise UsageError("--to needs at least one recipient")
    out: List[str] = []
    to_role: Optional[str] = None
    for entry in entries:
        if entry in ("all", AUTHOR_HUMAN):
            target = [entry]
        elif entry.startswith("role:"):
            role = entry[len("role:"):]
            holders = [str(m.get("name")) for m in role_holders(doc, role)]
            if not holders and not to_any:
                raise HerdrTeamError("recipient_unknown", "no member holds role {}".format(role), EXIT_REFUSED, {"role": role, "roster": roster_names})
            to_role = role
            target = holders
        elif entry == "me":
            target = [author.name] if author.is_member else [AUTHOR_HUMAN]
        elif _links.is_team_recipient(entry):
            # Another team, through the link between them. Kept as the token;
            # ``_run_post`` checks the link and writes the delivered copy.
            _links.team_of_recipient(entry)
            target = [entry]
        else:
            member = find_member(doc, entry)
            if member is not None and member.get("kind") != "human":
                target = [str(member.get("name"))]
            elif to_any:
                target = [entry]
            else:
                details: Dict[str, Any] = {"recipient": entry, "roster": roster_names}
                if entry in _roster.KIND_LABELS or entry in _roster.RESERVED_NAMES:
                    details["hint"] = "{} is an agent kind or reserved word, not a member name".format(entry)
                raise HerdrTeamError("recipient_unknown", "{} is not on the roster ({})".format(entry, ", ".join(roster_names) or "empty"), EXIT_REFUSED, details)
        for name in target:
            if name not in out:
                out.append(name)
    if not out:
        raise HerdrTeamError("recipient_unknown", "--to expanded to nobody", EXIT_REFUSED, {"roster": roster_names})
    return out, to_role


def prepare_text(raw: str, spill: bool, force: bool) -> Tuple[str, bool, Optional[str]]:
    """Validate, sanitize, then marker- and secret-check; returns ``(text, truncated, spill_body)``.

    The echo check runs on the *sanitized* text (and on the spill body), so
    a control character or escape sequence hidden inside ``[herdr-team`` or
    ``[n17]`` cannot smuggle a nudge marker past it (plan 8.3, 6.1).
    """
    text = validate_utf8(raw.encode("utf-8", "surrogateescape"))
    body: Optional[str] = None
    truncated = False
    try:
        clean = sanitize_text(text)
    except HerdrTeamError as err:
        if err.code != "text_too_long" or not spill:
            raise
        body = _sanitize.replace_format_chars(strip_controls(text).replace("\r\n", "\n").replace("\r", "\n"), "")
        clean = sanitize_text(body[: _sanitize.ADVISED_TEXT_CHARS].rstrip() + " …")
        truncated = True
    if is_marker_text(clean) or (body is not None and is_marker_text(body)):
        raise HerdrTeamError("echo_rejected", "text starts with [herdr-team or carries a [n<digits>] nonce; that is a nudge echo, not a post", EXIT_ECHO_REJECTED)
    if not clean.strip():
        raise HerdrTeamError("text_empty", "post text is empty after sanitization", EXIT_REFUSED)
    if not force:
        hit = find_secret(clean) or (find_secret(body) if body else None)
        if hit:
            raise HerdrTeamError("secret_detected", "text looks like it contains a secret ({}); never post credentials, or pass --force".format(hit), EXIT_REFUSED, {"pattern": hit})
    return clean, truncated, body


INTERRUPT_SCAN = 400


def interrupt_cooldown_ms(doc: Dict[str, Any]) -> int:
    """``config.gate.interrupt_cooldown_ms`` of the roster, else the gate default."""
    config = doc.get("config") if isinstance(doc.get("config"), dict) else {}
    gate_cfg = config.get("gate") if isinstance(config.get("gate"), dict) else {}
    value = gate_cfg.get("interrupt_cooldown_ms")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return int(value)
    return _gate.INTERRUPT_COOLDOWN_MS


def check_interrupt(team: TeamPaths, doc: Dict[str, Any], author: Author, to: List[str]) -> None:
    """``--interrupt`` needs named agent recipients, and a member gets one per recipient per cooldown."""
    if "all" in to:
        raise HerdrTeamError("interrupt_needs_recipient", "--interrupt needs named recipients (--to <name>); it is never a broadcast", EXIT_REFUSED, {"to": to})
    agents = [t for t in to if t != AUTHOR_HUMAN]
    if not agents:
        raise HerdrTeamError("interrupt_needs_recipient", "--interrupt reaches agent members; the human sees every post --to human anyway", EXIT_REFUSED, {"to": to})
    if author.is_human:
        return
    cooldown_ms = interrupt_cooldown_ms(doc)
    cutoff = time.time() - cooldown_ms / 1000.0
    top = board_max_seq(team)
    records = store.BoardStore(team).read(since_seq=max(0, top - INTERRUPT_SCAN), include_retracted=False)
    for rec in reversed(records):
        if not rec.get("interrupt") or rec.get("from") != author.name:
            continue
        hit = [t for t in (rec.get("to") or []) if t in agents]
        if not hit:
            continue
        sent = _daemon._parse_iso(rec.get("ts")) if isinstance(rec.get("ts"), str) else None
        if sent is None or sent < cutoff:
            continue
        retry_in = int(sent - cutoff) + 1
        raise HerdrTeamError(
            "interrupt_cooldown",
            "you interrupted {} {} s ago (#{}); one interrupt per teammate per {} min. Post --urgent instead, or wait {} s".format(
                ",".join(hit), int(time.time() - sent), rec.get("seq"), max(1, cooldown_ms // 60000), retry_in),
            EXIT_REFUSED,
            {"member": hit, "last_seq": rec.get("seq"), "retry_in_s": retry_in, "cooldown_ms": cooldown_ms},
        )


def _run_post(args: argparse.Namespace) -> int:
    as_human = args.as_who == "human"
    layout, api, author, team_name, team, doc = _open_team(args, require_server=False, as_human=as_human, relayed_for=args.relayed_for, label=args.name, write=True)
    if author.name == AUTHOR_SYSTEM:
        raise HerdrTeamError("author_mismatch", "hooks and startup processes cannot post as themselves", EXIT_REFUSED)
    if args.relayed_for and not author.is_member:
        raise HerdrTeamError("author_mismatch", "--relayed-for human is for member panes; you are already human", EXIT_REFUSED)
    if args.name and not author.verified and author.is_human:
        warn(args, "--name ignored: author is not verified")
    to, to_role = resolve_recipients(doc, args.to, author, args.to_any)
    link_target = next((t for t in to if _links.is_team_recipient(t)), None)
    interrupt = bool(getattr(args, "interrupt", False))
    if link_target is not None:
        if len(to) != 1:
            raise UsageError("a team recipient goes alone: --to team:<name>")
        if interrupt or args.spill or args.attach or getattr(args, "file", None):
            raise UsageError("--interrupt, --spill, --attach and --file do not cross a link; put a path in --ref instead")
    if interrupt:
        check_interrupt(team, doc, author, to)
    urgent = bool(args.urgent or interrupt)
    text, truncated, body = prepare_text(args.text, args.spill, args.force)
    reply_to: Optional[int] = None
    if args.reply_to is not None:
        reply_to = parse_seq(args.reply_to, "--reply-to")
        if board_get(team, reply_to) is None:
            raise HerdrTeamError("reply_to_unknown", "no post #{} on this board".format(reply_to), EXIT_REFUSED, {"reply_to": reply_to})
    refs = validate_refs(layout, team_name, list(args.ref), doc, env=env_of(args))
    file_refs, file_attach = resolve_files(layout, team_name, list(getattr(args, "file", None) or []), doc, env=env_of(args))
    refs = refs + [r for r in file_refs if r not in refs]
    staged = stage_attachments(team, list(args.attach) + file_attach)
    record = build_record(author, to, args.kind, text, to_role=to_role, refs=refs, reply_to=reply_to, urgent=urgent, relayed_for=args.relayed_for, socket_path=os.fspath(layout.socket), truncated=truncated, from_gen=member_generation(doc, author))
    if interrupt:
        record["interrupt"] = True
    timeout_s = _wait_seconds(args, doc, to, author)  # before the append: a usage error must leave nothing on the board
    if link_target is not None:
        return _post_across_link(args, layout, author, team_name, team, doc, record, link_target, reply_to)

    try:
        seq = board_append(team, record, spill_text=body, attachments=staged)
    except BaseException:
        store.BoardStore(team).discard_staged(staged)
        raise
    attached = [s.committed for s in staged if s.committed]
    notifier = notifier_state(layout.session)
    if notifier == "offline":
        warn(args, "notifier offline: nudges are queued until the daemon runs (herdr-synapse daemon start)")
    payload = {
        "seq": seq, "team": team_name, "notifier": notifier, "to": to, "to_role": to_role, "kind": args.kind,
        "author": {"name": author.name, "via": author.via, "verified": bool(author.verified)},
        "spilled": body is not None, "attached": attached, "refs": refs, "urgent": urgent, "interrupt": interrupt,
        "waited": False, "answer": None,
    }
    # The append is done and the team lock is released. Waiting here rather
    # than anywhere earlier is deliberate: holding the lock across a wait of
    # minutes would fail every other member's post with ``board_locked``.
    if timeout_s is not None:
        def heartbeat(elapsed: float) -> None:
            warn(args, "still waiting for the operator ({:.0f}s of {:.0f}s)".format(elapsed, timeout_s))

        answer = wait_for_answer(team, seq, timeout_s, heartbeat=heartbeat)
        if answer is None:
            raise HerdrTeamError(
                "wait_no_answer",
                "#{} is on the board and nobody answered in {:.0f}s. Do not guess: post what you would have done, or work on something else.".format(seq, timeout_s),
                EXIT_NO_ANSWER, {"seq": seq, "team": team_name, "timeout_s": timeout_s})
        payload["waited"] = True
        payload["answer"] = {"seq": answer.get("seq"), "from": answer.get("from"), "text": answer.get("text")}
        return emit(args, payload, "#{} answered by {}: {}".format(seq, answer.get("from"), _one_line_text(answer.get("text"))))
    return emit(args, payload, "#{} posted to {} as {} (notifier {}){}".format(seq, ",".join(to), author.name, notifier, " (interrupt)" if interrupt else ""))


LINK_THREAD_LOOKBACK = 400


def _post_across_link(args: argparse.Namespace, layout: Layout, author: Author, team_name: str, team: TeamPaths, doc: Dict[str, Any],
                      record: Dict[str, Any], link_target: str, reply_to: Optional[int]) -> int:
    """One message, two boards: the delivered copy for the other team's manager, the mirror for this team.

    Authority is the team manager, the operator, or a delegate; a plain member
    is pointed at its manager. Both appends take their own team lock in turn
    (``board_append`` never nests), so two managers posting to each other at
    once cannot deadlock.
    """
    other = _links.team_of_recipient(link_target)
    if other == team_name:
        raise UsageError("that is this team; a link is to another team")
    link, mine, theirs = _links.require_endpoint(layout.session, team_name, other)
    if not (author.trusted_human or getattr(author, "operator", False) or (author.is_member and author.name == mine)):
        _identity.audit(layout, team_name, "author_mismatch", author, {"action": "post --to team:", "other": other, "manager": mine})
        raise HerdrTeamError(
            "author_mismatch",
            "only the manager speaks for the team across a link; post to {} and ask it to relay".format(mine),
            EXIT_REFUSED, {"manager": mine, "other": other, "author": author.name},
        )
    other_paths = layout.team(other)
    message = {"id": _links.new_message_id(), "from_team": team_name, "to_team": other, "reply_to_id": None}
    remote_reply: Optional[int] = None
    if reply_to is not None:
        local = board_get(team, reply_to)
        local_link = (local or {}).get("link") if isinstance(local, dict) else None
        if isinstance(local_link, dict) and isinstance(local_link.get("id"), str):
            message["reply_to_id"] = local_link["id"]
            remote_reply = _seq_of_link_message(other_paths, local_link["id"])
    delivered = dict(record)
    delivered.update({"to": [theirs], "to_role": None, "from_team": team_name, "reply_to": remote_reply, "link": dict(message)})
    mirror = dict(record)
    mirror.update({"to": [link_target], "to_role": None, "from_team": team_name, "link": dict(message, mirror=True)})
    # In team-name order, so every writer takes the two locks the same way round.
    order = sorted(((team_name, team, mirror), (other, other_paths, delivered)), key=lambda item: item[0])
    seqs: Dict[str, int] = {}
    for name, paths, rec in order:
        seqs[name] = board_append(paths, rec)
    _identity.audit(layout, team_name, "link_post", author, {"link": link.id, "message": message["id"], "other": other, "seqs": seqs})
    notifier = notifier_state(layout.session)
    if notifier == "offline":
        warn(args, "notifier offline: {}'s manager is nudged once the daemon runs (herdr-synapse daemon start)".format(other))
    payload = {
        "seq": seqs[team_name], "team": team_name, "notifier": notifier, "to": [link_target], "to_role": None, "kind": args.kind,
        "author": {"name": author.name, "via": author.via, "verified": bool(author.verified)},
        "spilled": False, "attached": [], "refs": list(record.get("refs") or []), "urgent": bool(record.get("urgent")), "interrupt": False,
        "waited": False, "answer": None,
        "link": {"id": message["id"], "other_team": other, "other_manager": theirs, "delivered_seq": seqs[other], "mirror_seq": seqs[team_name], "reply_to_id": message["reply_to_id"]},
    }
    return emit(args, payload, "#{} posted to {} (its manager {} is nudged; their copy is #{})".format(seqs[team_name], other, theirs, seqs[other]))


def _seq_of_link_message(team: TeamPaths, message_id: str, lookback: int = LINK_THREAD_LOOKBACK) -> Optional[int]:
    """The local seq of the copy carrying ``message_id`` on this board, from a bounded read."""
    try:
        records = store.BoardStore(team).read(last=lookback, include_retracted=True)
    except HerdrTeamError:
        return None
    for rec in reversed(records):
        link = rec.get("link")
        if isinstance(link, dict) and link.get("id") == message_id and isinstance(rec.get("seq"), int):
            return int(rec["seq"])
    return None


def _one_line_text(text: Any, limit: int = 300) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "\u2026"


def _wait_seconds(args: argparse.Namespace, doc: Dict[str, Any], to: List[str], author: Author) -> Optional[float]:
    """How long this post should wait for the operator, or None to return now.

    ``--wait`` and ``--no-wait`` are the author's word and win. Otherwise the
    team policy decides, and only for a member asking the operator something:
    the operator's own posts never wait, and neither does a post nobody is
    expected to answer.
    """
    from herdr_team.cmd_misc import ask_view

    explicit = getattr(args, "wait", None) is True
    if getattr(args, "wait", None) is False:
        return None
    if author.is_human:
        if explicit:
            raise UsageError("--wait is for members: you are the operator, and nobody else answers a wait")
        return None
    if "human" not in to:
        if explicit:
            raise UsageError("--wait needs --to human: only the operator's reply ends a wait")
        return None
    policy = ask_view(doc)
    wanted = bool(getattr(args, "wait", None)) or _asks.blocks(args.kind, policy)
    if not wanted:
        return None
    from herdr_team.cmd_misc import parse_duration_s

    seconds = parse_duration_s(args.timeout) if getattr(args, "timeout", None) else float(policy["timeout_s"])
    if seconds <= 0:
        return None
    return min(float(seconds), MAX_WAIT_S)


# --------------------------------------------------------------------------
# board


def _add_board_arguments(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--new", action="store_true", help="posts to me or all since my cursor; advances the cursor")
    mode.add_argument("--peek", action="store_true", help="same selection as --new, never advances")
    parser.add_argument("--to", choices=("me",), help="only posts addressed to me or all")
    parser.add_argument("--teams", action="store_true", help="only posts that crossed a link to or from another team")
    parser.add_argument("--from", dest="from_name", metavar="NAME")
    parser.add_argument("--kind", choices=RECORD_KINDS)
    parser.add_argument("--thread", metavar="SEQ", type=int, help="a post and its replies")
    parser.add_argument("--since", metavar="SEQ", type=int)
    parser.add_argument("--last", metavar="N", type=int)
    parser.add_argument("--limit", metavar="N", type=int)
    parser.add_argument("--receipts", action="store_true")
    parser.add_argument("--format", choices=("text", "json", "context"), default=None)
    parser.add_argument("--max-bytes", dest="max_bytes", type=int)
    parser.add_argument("--max", dest="max_posts", type=int)
    parser.add_argument("--ascii", action="store_true")
    parser.add_argument("--name", metavar="LABEL", help="human label for the cursor file")


def _thread_of(records: List[Dict[str, Any]], root: int) -> List[Dict[str, Any]]:
    wanted = {root}
    changed = True
    while changed:
        changed = False
        for record in records:
            if record["seq"] in wanted:
                continue
            if record.get("reply_to") in wanted or record.get("retracts") in wanted or record.get("supersedes") in wanted:
                wanted.add(record["seq"])
                changed = True
    return [r for r in records if r["seq"] in wanted]


def compute_receipts(team: TeamPaths, doc: Dict[str, Any], records: List[Dict[str, Any]], all_records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    cursors = cursors_all(team)
    member_names = [str(m.get("name")) for m in agent_members(doc)]
    out: Dict[str, Dict[str, Any]] = {}
    for record in records:
        if store.is_direct_line(record):
            continue  # a typed line has a ``typed`` outcome, not read receipts
        seq = record["seq"]
        nudged = []
        for other in all_records:
            if other.get("kind") != "system" or other.get("event") != "nudged":
                continue
            seqs = other.get("seqs") if isinstance(other.get("seqs"), list) else []
            if other.get("reply_to") == seq or seq in seqs:
                nudged.append(other.get("ts"))
        recipients = [n for n in record.get("to") or [] if n != AUTHOR_HUMAN and n != "all"]
        if "all" in (record.get("to") or []):
            recipients = [n for n in member_names if n != record.get("from")]
        read = [name for name in recipients if int(cursors.get(name, {}).get("seq", 0)) >= seq]
        entry: Dict[str, Any] = {"nudged": nudged, "read": read}
        if recipients:
            entry["read_by"] = "{}/{}".format(len(read), len(recipients))
        out[str(seq)] = entry
    return out


def copy_payloads_for_sandboxed(layout: Layout, team: TeamPaths, doc: Dict[str, Any], author: Author, records: List[Dict[str, Any]]) -> None:
    """Kinds with ``payload_readable:false`` get refs copied into ``<cwd>/.herdr-synapse/``."""
    kinds = store.read_json(layout.session.kinds_json)
    if not isinstance(kinds, dict) or not author.is_member:
        return
    info = kinds.get(author.kind or "")
    if not isinstance(info, dict) or info.get("payload_readable", True) is not False:
        return
    member = find_member(doc, author.name, allow_retired=False) or {}
    cwd = member.get("cwd")
    if not cwd:
        return
    target = Path(cwd) / _workdir.DIR_NAME
    for record in records:
        for ref in record.get("refs") or []:
            if not isinstance(ref, str) or not ref.startswith("payloads/"):
                continue
            src = team.root / ref
            try:
                _paths.ensure_dir(target)
                shutil.copyfile(src, target / Path(ref).name)
            except OSError:
                continue


def _run_board(args: argparse.Namespace) -> int:
    layout, api, author, team_name, team, doc = _open_team(args, require_server=False, label=args.name)
    fmt = args.format or ("json" if args.json else "text")
    reader = reader_id(author)
    is_human = author.is_human or author.name == AUTHOR_SYSTEM
    hook_mode = env_of(args).get("HERDR_TEAM_HOOK") == "1"
    all_records, skipped = board_read_all_detailed(team, include_archive=bool(args.since or args.thread))
    selection = all_records
    cursor_state = cursor_get(team, reader)
    cursor_before = int(cursor_state.get("seq", 0))
    seen_before = set(cursor_state.get("seen") or [])
    inbox = args.new or args.peek or args.to == "me"
    if inbox:
        selection = [r for r in selection if inbox_record(r, reader, is_human)]
    if args.new or args.peek:
        selection = [r for r in selection if r["seq"] > cursor_before and r["seq"] not in seen_before]
    if args.since is not None:
        selection = [r for r in selection if r["seq"] > args.since]
    if args.from_name:
        selection = [r for r in selection if r.get("from") == args.from_name]
    if args.kind:
        selection = [r for r in selection if r.get("kind") == args.kind]
    if getattr(args, "teams", False):
        selection = [r for r in selection if isinstance(r.get("link"), dict)]
    if args.thread is not None:
        selection = _thread_of(selection, args.thread)
    if args.last is not None:
        selection = selection[-max(0, args.last):] if args.last > 0 else []
    elif not (args.new or args.peek or args.since is not None or args.thread is not None):
        selection = selection[-BOARD_DEFAULT_LAST:]
    limit = args.limit if args.limit is not None else (BOARD_NEW_LIMIT if (args.new or args.peek) else None)
    if fmt == "context":
        max_posts = args.max_posts if args.max_posts is not None else CONTEXT_MAX_POSTS
        max_bytes = args.max_bytes if args.max_bytes is not None else CONTEXT_MAX_BYTES
    else:
        max_posts = args.max_posts
        max_bytes = args.max_bytes if args.max_bytes is not None else BOARD_OUTPUT_CAP_BYTES
    truncated = False
    if limit is not None and len(selection) > limit:
        selection = selection[:limit]
        truncated = True
    if max_posts is not None and len(selection) > max_posts:
        selection = selection[:max_posts]
        truncated = True
    # byte cap: keep whole records, oldest first, so the cursor stops at the last printed seq
    shown: List[Dict[str, Any]] = []
    used = 0
    for record in selection:
        size = len(json.dumps(record, ensure_ascii=False).encode("utf-8")) + 1
        if shown and used + size > max_bytes:
            truncated = True
            break
        shown.append(record)
        used += size
    receipts = compute_receipts(team, doc, shown, all_records) if args.receipts else None
    highest = shown[-1]["seq"] if shown else cursor_before
    shown_seqs = {r["seq"] for r in shown}
    seen_now: List[int] = []
    if args.new and (args.from_name or args.kind or args.thread is not None):
        # Plan 6.2: the cursor advances only to the highest seq printed *without skipping anything unread*.
        # A content filter hides addressed posts, so the cursor stops right before the first unread one
        # not shown; the printed seqs above it are remembered individually (``seen``) and never shown twice.
        unread = [r["seq"] for r in all_records if r["seq"] > cursor_before and r["seq"] not in seen_before and inbox_record(r, reader, is_human) and r["seq"] not in shown_seqs]
        if unread:
            highest = min(highest, min(unread) - 1)
        seen_now = sorted(s for s in shown_seqs if s > highest)
    advance = bool(args.new) and not hook_mode and (highest > cursor_before or bool(seen_now))
    payload: Dict[str, Any] = {
        "team": team_name, "reader": reader,
        "cursor": {"before": cursor_before, "after": highest if advance else cursor_before, "advanced": advance},
        "posts": shown, "truncated": truncated, "count": len(shown),
    }
    if receipts is not None:
        payload["receipts"] = receipts
    if any(skipped.values()):
        # F-03: a torn or garbage line is reported, never silently dropped (JSON key, stderr warning otherwise).
        payload["skipped"] = skipped
        if not (fmt == "json" or args.json):
            warn(args, "board: skipped {} line(s): {}".format(sum(skipped.values()), ", ".join("{} {}".format(v, k) for k, v in skipped.items() if v)))
    out = getattr(args, "stdout", None) or sys.stdout
    if fmt == "json" or args.json:
        out.write(json.dumps(payload, ensure_ascii=False) + "\n")
    elif fmt == "context":
        out.write(render_context(shown, max_bytes=max_bytes, max_posts=max_posts or CONTEXT_MAX_POSTS) + "\n")
    else:
        int_receipts = {int(k): v for k, v in receipts.items()} if receipts else None
        text = render_board(shown, ascii_only=args.ascii, receipts=int_receipts)
        footer = "-- {} post{}".format(len(shown), "" if len(shown) == 1 else "s")
        if args.new or args.peek:
            footer += " for {} (cursor {} -> {})".format(reader, cursor_before, highest if advance else cursor_before)
        if truncated:
            footer += "; more pending, run: herdr-synapse board --new"
        out.write(text + "\n" + footer + "\n")
    out.flush()
    if advance:
        cursor_advance(team, reader, highest, author.terminal_id, "hook" if hook_mode else "cli", seen=seen_now)
    if args.new and shown:
        copy_payloads_for_sandboxed(layout, team, doc, author, shown)
    return 0


# --------------------------------------------------------------------------
# say: type one line into a member now (human only, from the verified console)


def require_say_author(layout: Layout, team_name: str, author: Author) -> None:
    """``say`` is for the verified console only; members, hooks, popups, shells, and unfocused consoles are refused and audited."""
    if not author.is_human:
        # Members and hooks get the ordinary authority refusal (a delegate
        # passes it and is refused just below: typing into a pane is not
        # delegable). A human is checked against ``say``'s own, stricter
        # rule first, so an unverified shell hears why *say* refused it.
        _charter.require_human(layout, team_name, author, "say")
    ancestry = (author.origin or {}).get("ancestry")
    if author.verified and author.via in SAY_VIAS and ancestry == "confirmed":
        return
    details = {"via": author.via, "verified": bool(author.verified), "ancestry": ancestry, "reason": author.reason}
    _identity.audit(layout, team_name, "say_unverified", author, details)
    raise HerdrTeamError(
        "say_unverified",
        "say types into a member and needs the focused team console (you are human via {}{}); open it with prefix+u and type !<name> <text>".format(
            author.via, ": " + author.reason if author.reason else ""),
        EXIT_REFUSED,
        details,
    )


def prepare_say_text(raw: str, force: bool) -> str:
    """One typed line: post sanitization, secrets never allowed, tabs to spaces, single line, <= 500 chars.

    ``force`` (the console's ``!!``) lifts only the control-word refusal;
    ``echo_rejected`` and ``secret_detected`` stand in both modes because the
    text goes into a live terminal and onto the board.
    """
    clean, _truncated, _body = prepare_text(raw, spill=False, force=False)
    clean = clean.replace("\t", " ").strip()
    if "\n" in clean:
        raise HerdrTeamError("say_multiline", "say types one line; a newline would submit several prompts (post multi-line text instead)", EXIT_REFUSED)
    if len(clean) > SAY_MAX_CHARS:
        raise HerdrTeamError("say_too_long", "say text is {} characters; the limit is {} (post longer text and nudge instead)".format(len(clean), SAY_MAX_CHARS), EXIT_REFUSED, {"length": len(clean), "max": SAY_MAX_CHARS})
    head = clean.split(None, 1)[0].lower() if clean else ""
    if head in SAY_CONTROL_WORDS and not force:
        raise HerdrTeamError("say_control_command", "{!r} would end, clear, or switch the member's session; --force (the console's !!name) types it anyway".format(head), EXIT_REFUSED, {"word": head})
    return clean


def say_outcome_of(record: Dict[str, Any]) -> Dict[str, Any]:
    return {"seq": record.get("seq"), "result": record.get("result"), "reason": record.get("reason"), "detail": record.get("detail"), "elapsed_ms": record.get("elapsed_ms")}


#: How often a waiting post looks for its answer. Slower than ``say``'s 0.1 s
#: because this wait is measured in minutes, not seconds, and because the
#: tailer below reads only new bytes rather than re-parsing the board.
ASK_POLL_S = 0.5
#: Ceiling on ``--timeout``. Claude Code kills a shell command at ten minutes,
#: so a longer wait would end as a killed process with no error the agent could
#: read. Nine leaves room for the process to report its own timeout first.
MAX_WAIT_S = 540.0
#: A line on stderr this often while waiting. Claude Code returns a shell
#: command's output only at the end, but Codex runs it in an exec cell that
#: yields to the model after ten seconds with the process still running: a
#: silent wait looks hung there, and a model that thinks so moves on.
WAIT_HEARTBEAT_S = 30.0


def wait_for_answer(team: TeamPaths, seq: int, timeout_s: float, poll_s: float = ASK_POLL_S,
                    sleep=time.sleep, heartbeat: Optional[Callable[[float], None]] = None,
                    heartbeat_s: float = WAIT_HEARTBEAT_S, clock: Callable[[], float] = time.monotonic) -> Optional[Dict[str, Any]]:
    """The operator's reply to the post at ``seq``, or None once ``timeout_s`` is up.

    Uses a non-persisting ``BoardTailer`` rather than ``BoardStore.read``: read
    re-parses the whole active file, up to four megabytes, on every call, and a
    wait measured in minutes would do that hundreds of times. The tailer
    ``pread``s only the new bytes from a fd it holds open.

    Only the operator's reply counts. A teammate answering a question is a peer
    note on the same thread, and taking it as the answer is exactly how a
    genuine pre-submission halt got overridden on the team this was built for.
    """
    tailer = store.BoardTailer(team, start_seq=seq, persist=False)
    started = clock()
    deadline = started + max(0.0, timeout_s)
    next_beat = started + heartbeat_s
    while True:
        for record in tailer.poll():
            if _asks.answered_by(record) == seq:
                return record
        now = clock()
        if now >= deadline:
            return None
        if heartbeat is not None and now >= next_beat:
            heartbeat(now - started)
            next_beat = now + heartbeat_s
        sleep(poll_s)


def wait_for_typed(team: TeamPaths, seq: int, timeout_s: float, poll_s: float = SAY_POLL_S) -> Optional[Dict[str, Any]]:
    """The daemon's ``typed`` record for the ``direct`` record ``seq``, polled until ``timeout_s``; None on timeout."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        for rec in store.BoardStore(team).read(since_seq=seq, kind="system"):
            if rec.get("event") == "typed" and seq in (rec.get("seqs") or []):
                return rec
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_s)


def say_human_text(payload: Dict[str, Any]) -> str:
    seq, name = payload["seq"], payload["member"]
    outcome = payload.get("outcome")
    if not outcome:
        return "#{} queued as job {} for {} (not waiting)".format(seq, payload["job"], name)
    result, reason = outcome.get("result"), outcome.get("reason")
    if result == "typed":
        suffix = "'s running turn" if reason == "in_turn" else (" (dry run)" if reason == "dry" else "")
        return "#{} typed into {}{}".format(seq, name, suffix)
    if result == "refused":
        if reason in ("working", "muted"):
            hint = "; --force types anyway"
        elif reason == "state_changed":
            hint = "; retry after checking the member"
        elif reason == "update_required":
            hint = "; update Herdr to 0.9.0 or newer"
        elif reason == "capability_unavailable":
            hint = "; post with @{} text for board delivery, or use --force only intentionally".format(name)
        else:
            hint = ""
        return "#{} not typed into {}: {}{}".format(seq, name, reason, hint)
    if result == "not_submitted":
        return "#{} is on {}'s prompt line but was not submitted".format(seq, name)
    return "typing #{} into {} failed: {}".format(seq, name, outcome.get("detail") or reason or result)


def _add_say_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("member", help="one agent member (human only; type it from the team console)")
    parser.add_argument("text", help="one line typed into the member's input box as-is (<= 500 chars)")
    parser.add_argument("--force", action="store_true", help="also type into a working or muted member and allow control words like /clear (the console's !!name)")
    wait = parser.add_mutually_exclusive_group()
    wait.add_argument("--wait", dest="wait", action="store_true", default=True, help="wait for the daemon's typed outcome (default)")
    wait.add_argument("--no-wait", dest="wait", action="store_false", help="return as soon as the record and the job are written")
    parser.add_argument("--timeout", type=float, default=SAY_WAIT_TIMEOUT_S, metavar="S", help="--wait deadline in seconds (default 10)")


def _run_say(args: argparse.Namespace) -> int:
    # require_server: identity needs pane.get; with Herdr down the user must see server_not_running, not say_unverified.
    layout, api, author, team_name, team, doc = _open_team(args, require_server=True, write=True)
    require_say_author(layout, team_name, author)
    member = member_or_raise(doc, args.member, team_name)
    name = str(member["name"])
    kind = str(member.get("kind") or "")
    if not (bool(member.get("verified_kind")) or _roster.kind_trusted(store.read_json(layout.session.kinds_json, default=None), kind)):
        raise HerdrTeamError("kind_unverified", "{} is a {} agent and that kind is not trusted for delivery yet; run: herdr-synapse kinds trust {}".format(name, kind, kind), EXIT_REFUSED, {"member": name, "kind": kind})
    text = prepare_say_text(args.text, args.force)
    require_daemon(layout.session)
    record = build_record(author, [name], "direct", text, urgent=False, socket_path=os.fspath(layout.socket), from_gen=member_generation(doc, author))
    record["force"] = bool(args.force)
    seq = board_append(team, record)
    job = enqueue_job(team, "say", name, author, force=args.force, extra={"seq": seq})
    outcome_record = wait_for_typed(team, seq, args.timeout) if args.wait else None
    if args.wait and outcome_record is None:
        raise HerdrTeamError("say_timeout", "#{} was recorded and job {} queued for {}, but no typed outcome arrived within {:g}s (see herdr-synapse notifier stats)".format(seq, job, name, args.timeout), EXIT_REFUSED, {"seq": seq, "job": job, "member": name, "timeout_s": args.timeout})
    payload: Dict[str, Any] = {
        "seq": seq, "team": team_name, "member": name, "job": job, "force": bool(args.force), "text": text,
        "author": {"name": author.name, "via": author.via, "verified": bool(author.verified)},
        "waited": bool(args.wait), "outcome": say_outcome_of(outcome_record) if outcome_record else None,
    }
    return emit(args, payload, lambda: say_human_text(payload))


# --------------------------------------------------------------------------
# show / retract / edit / task / ack


def _add_show_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("seq", type=int)
    parser.add_argument("--cat", action="store_true", help="inline refs under payloads/ or roster roots (64 KiB each)")
    parser.add_argument("--force", action="store_true", help="--cat: also inline refs under dot-directories (.ssh, .aws, .config ...)")


def _ref_entry(layout: Layout, team_name: str, team: TeamPaths, doc: Dict[str, Any], ref: str, cat: bool, force: bool = False, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"path": ref, "bytes": None, "content": None, "truncated": False}
    candidate = Path(ref) if os.path.isabs(ref) else team.root / ref
    try:
        resolved = candidate.resolve()
        size = os.stat(resolved).st_size
    except OSError:
        entry["missing"] = True
        return entry
    entry["bytes"] = size
    if not cat:
        return entry
    roots = roster_roots(layout, team_name, doc, env)
    root = next((r for r in roots if _under(resolved, r)), None)
    if root is None:
        entry["skipped"] = "outside payloads/ and roster roots"
        return entry
    if _hidden_parts(resolved, root) and not force and not _workdir.is_inside(resolved, _workdir.project_dir_of(doc)):
        # A cwd recorded as HOME after a restart is never a root; a dot-directory under a real root still is not cat-able by default.
        # The team's own .herdr-synapse/ is the exception: the plugin created it and tells members to write there.
        entry["skipped"] = "under a dot-directory (pass --force)"
        return entry
    raw = store.read_bytes(resolved, b"") or b""
    if len(raw) > SHOW_CAT_CAP_BYTES:
        raw = raw[:SHOW_CAT_CAP_BYTES]
        entry["truncated"] = True
    entry["content"] = raw.decode("utf-8", "replace")
    return entry


def _run_show(args: argparse.Namespace) -> int:
    layout, api, author, team_name, team, doc = _open_team(args, require_server=False)
    seq = parse_seq(args.seq)
    record = board_get(team, seq)
    if record is None:
        raise HerdrTeamError("post_not_found", "no post #{}".format(seq), EXIT_REFUSED, {"seq": seq})
    refs = list(record.get("refs") or [])
    body_ref = "payloads/{}-body.md".format(seq)
    if record.get("truncated") and (team.root / body_ref).is_file() and body_ref not in refs:
        refs.append(body_ref)
    entries = [_ref_entry(layout, team_name, team, doc, ref, args.cat, force=bool(getattr(args, "force", False)), env=env_of(args)) for ref in refs]
    payload = {"team": team_name, "post": record, "refs": entries}

    def human() -> str:
        lines = [render_post(record)]
        for entry in entries:
            if entry.get("content") is not None:
                lines.append("")
                lines.append("---- {} ({} bytes{}) ----".format(entry["path"], entry["bytes"], ", truncated" if entry["truncated"] else ""))
                lines.append(entry["content"].rstrip("\n"))
        return "\n".join(lines)

    return emit(args, payload, human)


def _own_or_human(record: Dict[str, Any], author: Author, verb: str) -> None:
    if author.is_human:
        return
    if record.get("from") != author.name:
        raise HerdrTeamError("author_mismatch", "only the author or the human may {} #{}".format(verb, record.get("seq")), EXIT_REFUSED, {"from": record.get("from"), "author": author.name})


def _run_retract(args: argparse.Namespace) -> int:
    layout, api, author, team_name, team, doc = _open_team(args, require_server=False, write=True)
    seq = parse_seq(args.seq)
    original = board_get(team, seq)
    if original is None:
        raise HerdrTeamError("post_not_found", "no post #{}".format(seq), EXIT_REFUSED, {"seq": seq})
    _own_or_human(original, author, "retract")
    if original.get("kind") == "direct":
        raise HerdrTeamError("retract_invalid", "#{} was typed into {}; send a correction with !<name> instead".format(seq, ",".join(original.get("to") or [])), EXIT_REFUSED)
    if original.get("kind") in ("retract", "system"):
        raise HerdrTeamError("retract_invalid", "#{} is a {} record".format(seq, original.get("kind")), EXIT_REFUSED)
    record = build_record(author, list(original.get("to") or ["all"]), "retract", "retracted #{}".format(seq), to_role=original.get("to_role"), retracts=seq, socket_path=os.fspath(layout.socket), from_gen=member_generation(doc, author))
    new_seq = board_append(team, record)
    return emit(args, {"team": team_name, "seq": new_seq, "retracts": seq}, "#{} retracts #{}".format(new_seq, seq))


def _add_edit_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("seq", type=int)
    parser.add_argument("text")
    parser.add_argument("--force", action="store_true")


def _run_edit(args: argparse.Namespace) -> int:
    layout, api, author, team_name, team, doc = _open_team(args, require_server=False, write=True)
    seq = parse_seq(args.seq)
    original = board_get(team, seq)
    if original is None:
        raise HerdrTeamError("post_not_found", "no post #{}".format(seq), EXIT_REFUSED, {"seq": seq})
    _own_or_human(original, author, "edit")
    if original.get("kind") == "direct":
        raise HerdrTeamError("edit_invalid", "#{} was typed into {}; send a correction with !<name> instead".format(seq, ",".join(original.get("to") or [])), EXIT_REFUSED)
    if original.get("kind") in ("retract", "system"):
        raise HerdrTeamError("edit_invalid", "#{} is a {} record".format(seq, original.get("kind")), EXIT_REFUSED)
    text, truncated, body = prepare_text(args.text, False, args.force)
    record = build_record(author, list(original.get("to") or ["all"]), str(original.get("kind")), text, to_role=original.get("to_role"), refs=list(original.get("refs") or []), reply_to=original.get("reply_to"), supersedes=seq, urgent=bool(original.get("urgent")), relayed_for=original.get("relayed_for"), socket_path=os.fspath(layout.socket), from_gen=member_generation(doc, author))
    new_seq = board_append(team, record)
    return emit(args, {"team": team_name, "seq": new_seq, "supersedes": seq}, "#{} supersedes #{}".format(new_seq, seq))


def task_file(team: TeamPaths, member_name: str) -> Path:
    if not _paths.FILE_STEM_RE.match(member_name or ""):
        raise HerdrTeamError("name_invalid", "member name is not file-name safe", EXIT_REFUSED, {"name": member_name})
    return team.root / "tasks" / (member_name + ".json")


def read_task(team: TeamPaths, member_name: str) -> Optional[Dict[str, Any]]:
    """The member's current task headline when younger than 30 minutes."""
    try:
        doc = store.read_json(task_file(team, member_name))
    except HerdrTeamError:
        return None
    if not isinstance(doc, dict) or not doc.get("headline"):
        return None
    set_at = parse_iso(doc.get("set_at"))
    if set_at is not None and time.time() - set_at > TASK_MAX_AGE_S:
        return None
    return doc


def _run_task(args: argparse.Namespace) -> int:
    layout, api, author, team_name, team, doc = _open_team(args, require_server=False, write=True)
    require_member(author, "task")
    text = sanitize_text(validate_utf8(args.text.encode("utf-8", "surrogateescape")), _sanitize.ADVISED_TEXT_CHARS)
    if is_marker_text(text):
        raise HerdrTeamError("echo_rejected", "task text carries a nudge marker", EXIT_ECHO_REJECTED)
    head = headline(text, _sanitize.HEADLINE_COLUMNS)
    set_at = now_iso()
    path = task_file(team, author.name)
    _paths.ensure_dir(path.parent)
    store.write_json(path, {"v": 1, "member": author.name, "text": text, "headline": head, "set_at": set_at})
    return emit(args, {"team": team_name, "member": author.name, "task": head, "set_at": set_at}, "task for {}: {}".format(author.name, head))


def _run_ack(args: argparse.Namespace) -> int:
    if env_of(args).get("HERDR_TEAM_HOOK") == "1":
        raise HerdrTeamError("ack_from_hook", "ack must be run by the member itself, not from a hook", EXIT_REFUSED)
    layout, api, author, team_name, team, doc = _open_team(args, require_server=False, write=True)
    require_member(author, "ack")
    member = find_member(doc, author.name, allow_retired=False)
    if member is None:
        raise HerdrTeamError("not_a_member", "{} is not on the roster of {}".format(author.name, team_name), EXIT_UNREACHABLE)
    if member.get("terminal_id") and author.terminal_id and member.get("terminal_id") != author.terminal_id:
        raise HerdrTeamError("author_mismatch", "ack must come from {}'s own pane".format(author.name), EXIT_REFUSED, {"expected_terminal": member.get("terminal_id"), "actual_terminal": author.terminal_id})
    max_seq = board_max_seq(team)
    # touch=True: an ack at an unchanged seq must still be a visible cursor write (see store.Cursors.advance)
    cursor = cursor_advance(team, author.name, max_seq, author.terminal_id, "cli", touch=True)
    charter = charter_of(doc)
    charter_seq = int(charter.get("seq", 0)) if charter else None
    # The operator's two other documents are acknowledged here too, on the same
    # evidence: the member ran ``ack`` from its own pane after reading. This is
    # what stops the hook re-injecting an instructions document every turn.
    instructions_seq = int(member.get("instructions_seq") or 0)
    rules_seq = _charter.rules_seq(doc)
    now = now_iso()

    def mutate(d: Dict[str, Any]) -> None:
        for m in members_of(d):
            if m.get("name") == author.name:
                m["charter_seq_acked"] = charter_seq
                m["instructions_seq_acked"] = instructions_seq
                m["rules_seq_acked"] = rules_seq
                m["briefed_at"] = m.get("briefed_at") or now
                m["last_seen_at"] = now
                if m.get("briefing_seq") is None:
                    m["briefing_seq"] = max_seq

    update_doc(team, mutate)
    payload = {"team": team_name, "member": author.name, "cursor": int(cursor.get("seq", max_seq)), "charter_seq_acked": charter_seq,
               "instructions_seq_acked": instructions_seq, "rules_seq_acked": rules_seq}
    return emit(args, payload, "{} acknowledged: cursor {}, charter #{}".format(author.name, payload["cursor"], charter_seq if charter_seq is not None else "none"))


#: What each kind is told to do. ``clear`` starts a fresh context in the same
#: session; ``compact`` summarises in place. Codex deliberately gets ``/new``
#: rather than ``/clear``: its ``/clear`` also wipes the terminal scrollback,
#: which is the surface the detection layer reads to tell idle from working.
CONTROL_KEYSTROKES: Dict[str, Dict[str, str]] = {
    "claude": {"compact": "/compact", "clear": "/clear"},
    "codex": {"compact": "/compact", "clear": "/new"},
    "opencode": {"compact": "/compact", "clear": "/new"},
}
CONTROL_ACTIONS = ("compact", "clear")


def control_keystroke(kind: Optional[str], action: str) -> str:
    """The line to type, or a refusal naming the kinds we have verified."""
    table = CONTROL_KEYSTROKES.get(str(kind or "").strip())
    if not table or action not in table:
        raise HerdrTeamError(
            "control_unsupported",
            "no verified {} command for a {} agent".format(action, kind or "?"),
            EXIT_REFUSED,
            {"action": action, "kind": kind, "supported": sorted(CONTROL_KEYSTROKES)},
        )
    return table[action]


def _add_control_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("member", nargs="?", help="the member to act on (default with --self: you)")
    parser.add_argument("--self", dest="on_self", action="store_true", help="act on your own pane (members only)")
    parser.add_argument("--yes", action="store_true", help="do not ask before clearing")
    parser.add_argument("--reason", metavar="TEXT", help="why, for the board record")


def _run_compact(args: argparse.Namespace) -> int:
    return _run_control(args, "compact")


def _run_clear(args: argparse.Namespace) -> int:
    return _run_control(args, "clear")


def _run_control(args: argparse.Namespace, action: str) -> int:
    """Ask the notifier to compact or clear a member's context.

    Only the notifier ever types, so this appends a board record and enqueues a
    job; the daemon re-reads the record and validates its origin before acting,
    the same way ``say`` does, because a job file on its own proves nothing.

    Authority: the operator may act on anyone, and a member may act on itself
    with ``--self``. A member never acts on a peer; it posts a request and the
    peer or the operator decides.
    """
    layout, api, author, team_name, team, doc = _open_team(args, require_server=True, write=True)
    name = args.member
    if args.on_self:
        if not author.is_member:
            raise HerdrTeamError("not_a_member", "--self runs from a member's own pane; you are {}".format(author.name), EXIT_UNREACHABLE, {"author": author.name})
        if name and name != author.name:
            raise UsageError("--self acts on your own pane; drop the name or the flag")
        name = author.name
    if not name:
        raise UsageError("which member? herdr-synapse {} <name> (or --self)".format(action))
    member = member_or_raise(doc, name, team_name)
    name = str(member.get("name"))
    if not args.on_self:
        _charter.require_human(layout, team_name, author, "{} <member>".format(action))
    elif action == "clear":
        # Clearing throws away the member's working memory. A member may ask,
        # but the operator decides, so self-service stops at compact.
        raise HerdrTeamError("author_mismatch", "clearing is the operator's; --self covers compact only", EXIT_REFUSED,
                             {"action": action, "author": author.name})
    keystroke = control_keystroke(member.get("kind"), action)
    if action == "clear" and not args.yes:
        # ``--json`` is not a way around this: a caller that cannot be asked
        # must say ``--yes``, so throwing away a member's memory is always
        # something someone wrote down rather than something that happened.
        if not _confirm_control(args, name, member.get("kind")):
            return emit(args, {"team": team_name, "member": name, "action": action, "requested": False}, "not cleared")
    require_daemon(layout.session)
    text = "{} {}{}".format(action, name, ": {}".format(args.reason) if args.reason else "")
    record = build_record(author, [name], "direct", text, urgent=False, socket_path=os.fspath(layout.socket),
                          from_gen=member_generation(doc, author))
    record["control"] = {"action": action, "keystroke": keystroke, "kind": member.get("kind")}
    seq = board_append(team, record)
    job = enqueue_job(team, "control", name, author, extra={"seq": seq, "action": action})
    payload = {"team": team_name, "member": name, "action": action, "keystroke": keystroke,
               "kind": member.get("kind"), "record_seq": seq, "job": job, "requested": True}
    return emit(args, payload, "{} queued for {}; the notifier types {!r} when it is idle".format(action, name, keystroke))


def _add_wipe_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--yes", action="store_true", help="do not ask")
    parser.add_argument("--purge", action="store_true", help="delete the archive and the payloads too; nothing is kept")
    parser.add_argument("--reason", metavar="TEXT", help="why, for the board note")


def _run_wipe(args: argparse.Namespace) -> int:
    """Empty a team's board. Human only, asks first, archives unless told to purge.

    Every post moves to ``archive/`` and the fresh board opens with a
    ``board_cleared`` note, so a member's next read says why the board is
    short. Seqs and cursors are untouched. ``--purge`` deletes the archive and
    the payloads as well; that is the one thing here nobody can get back.
    """
    layout, api, author, team_name, team, doc = _open_team(args, require_server=False, write=True)
    _charter.require_human(layout, team_name, author, "wipe")
    check_write_session(args, layout, team_name)
    if not args.yes and not _confirm_wipe(args, team_name, bool(args.purge)):
        return emit(args, {"team": team_name, "wiped": False}, "not wiped")
    result = store.BoardStore(team).clear(author.name, reason=args.reason, purge=bool(args.purge))
    _identity.audit(layout, team_name, "board_purged" if args.purge else "board_wiped", author, dict(result, reason=args.reason))
    payload = dict(result, team=team_name, wiped=True)
    if result["records"] == 0 and not result["purged_segments"]:
        return emit(args, payload, "{}'s board was already empty".format(team_name))
    if args.purge:
        return emit(args, payload, "{}'s board purged: {} post{} deleted, {} archive segment{} and {} payload{} removed".format(
            team_name, result["records"], "" if result["records"] == 1 else "s", result["purged_segments"], "" if result["purged_segments"] == 1 else "s",
            result["purged_payloads"], "" if result["purged_payloads"] == 1 else "s"))
    return emit(args, payload, "{}'s board cleared: {} post{} moved to {} (herdr-synapse board --since 1 still reads them)".format(
        team_name, result["records"], "" if result["records"] == 1 else "s", result["archived_to"]))


def _confirm_wipe(args: argparse.Namespace, team_name: str, purge: bool) -> bool:
    if purge:
        question = "delete every post on {}'s board, its archive and its payloads? nothing is kept".format(team_name)
    else:
        question = "clear {}'s board? every post moves to archive/ (herdr-synapse board --since 1 still reads them); members keep their read positions".format(team_name)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise HerdrTeamError("confirmation_required", "{} (pass --yes)".format(question), EXIT_REFUSED, {"team": team_name, "purge": purge})
    args.stdout.write("{} [y/N] ".format(question))
    args.stdout.flush()
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def _confirm_control(args: argparse.Namespace, name: str, kind: Optional[str]) -> bool:
    question = "clear {}'s context? its memory of this conversation goes, and it is briefed again".format(name)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise HerdrTeamError("confirmation_required", "{} (pass --yes)".format(question), EXIT_REFUSED, {"member": name, "kind": kind})
    args.stdout.write("{} [y/N] ".format(question))
    args.stdout.flush()
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def _seq_only(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("seq", type=int)


def _text_only(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("text")


def _no_arguments(parser: argparse.ArgumentParser) -> None:
    pass


# --------------------------------------------------------------------------
# export


#: Formats ``export`` can write. ``jsonl`` is the on-disk shape, so an export
#: round-trips back into any tool that reads the board file.
EXPORT_FORMATS = ("md", "json", "jsonl", "text")
EXPORT_MAX_BYTES = 64 * 1024 * 1024


def export_filename(team_name: str, fmt: str, now: Optional[str] = None) -> str:
    """``board-<team>-<YYYYmmdd-HHMMSS>.<ext>``, safe to use as a file name."""
    stamp = (now or now_iso()).replace("-", "").replace(":", "").replace("T", "-")[:15]
    stem = re.sub(r"[^A-Za-z0-9._-]", "-", str(team_name))[:40] or "team"
    ext = "md" if fmt == "md" else ("txt" if fmt == "text" else fmt)
    return "board-{}-{}.{}".format(stem, stamp, ext)


def render_export(fmt: str, records: List[Dict[str, Any]], team_name: str, doc: Dict[str, Any], author: Any) -> str:
    """One board in the requested format."""
    charter = charter_of(doc)
    members = [m for m in members_of(doc) if isinstance(m, dict)]
    stamp = now_iso()
    if fmt == "jsonl":
        return "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in records)
    if fmt == "json":
        payload = {
            "schema": 1, "team": team_name, "exported_at": stamp,
            "exported_by": getattr(author, "name", None),
            "charter": charter, "members": members, "records": records,
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if fmt == "text":
        return _render.render_board(records) + "\n"
    return _render.render_export_markdown(
        records, team_name, charter=charter, members=members,
        exported_at=stamp, exported_by=getattr(author, "name", None),
    )


def _add_export_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("out", nargs="?", metavar="PATH", help="file to write (default: ./board-<team>-<timestamp>.<ext>)")
    parser.add_argument("--format", dest="fmt", choices=EXPORT_FORMATS, default="md", help="md (default), json, jsonl, or text")
    parser.add_argument("--since", metavar="SEQ", type=int, help="only posts after this seq")
    parser.add_argument("--last", metavar="N", type=int, help="only the newest N posts")
    parser.add_argument("--kind", choices=RECORD_KINDS, help="only this post kind")
    parser.add_argument("--from", dest="from_name", metavar="NAME", help="only posts from this member")
    parser.add_argument("--no-archive", dest="no_archive", action="store_true", help="skip rotated segments; the active file only")
    # dest is not ``stdout``: ``cli.main`` puts the output *stream* on ``args.stdout``,
    # so that name is always truthy and every export would take this branch.
    parser.add_argument("--stdout", dest="to_stdout", action="store_true", help="write to stdout instead of a file")
    parser.add_argument("--force", action="store_true", help="overwrite an existing file")


def _run_export(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False)
    records = store.BoardStore(team).read(
        since_seq=int(args.since or 0),
        from_name=args.from_name,
        kind=args.kind,
        last=args.last,
        include_archive=not args.no_archive,
        include_retracted=True,
    )
    body = render_export(args.fmt, records, team_name, doc, author)

    if args.to_stdout:
        payload = {"team": team_name, "records": len(records), "format": args.fmt, "path": None}
        return emit(args, payload, body.rstrip("\n"))

    if args.out:
        # Checked before canonicalize, which would resolve the link away and
        # leave the guard below inspecting the real file instead.
        _paths.check_not_symlink(Path(os.path.expanduser(str(args.out))))
        target = _paths.canonicalize(args.out)
    else:
        target = Path(os.getcwd()) / export_filename(team_name, args.fmt)
    if target.is_dir():
        target = target / export_filename(team_name, args.fmt)
    if target.exists() and not args.force:
        raise HerdrTeamError(
            "path_exists", "{} already exists; pass --force to overwrite".format(target),
            EXIT_REFUSED, {"path": os.fspath(target)},
        )
    encoded = body.encode("utf-8")
    if len(encoded) > EXPORT_MAX_BYTES:
        raise HerdrTeamError("export_too_large", "the export is {} bytes; narrow it with --last or --since".format(len(encoded)), EXIT_REFUSED, {"bytes": len(encoded)})
    try:
        _paths.check_not_symlink(target)
        _paths.ensure_dir(target.parent)
        store.atomic_write(target, encoded, mode=0o600)
    except OSError as err:
        raise HerdrTeamError("write_failed", "cannot write {}: {}".format(target, err), EXIT_REFUSED, {"path": os.fspath(target)}) from err

    payload = {"team": team_name, "records": len(records), "format": args.fmt, "path": os.fspath(target), "bytes": len(encoded)}
    return emit(args, payload, "exported {} post{} to {} ({} bytes)".format(
        len(records), "" if len(records) == 1 else "s", target, len(encoded)))


COMMANDS: List[Command] = [
    Command("post", "append a post to the team board", _add_post_arguments, _run_post),
    Command("board", "read the board (--new advances your cursor)", _add_board_arguments, _run_board),
    Command("show", "show one post and its refs", _add_show_arguments, _run_show),
    Command("retract", "retract one of your posts", _seq_only, _run_retract),
    Command("edit", "supersede one of your posts with new text", _add_edit_arguments, _run_edit),
    Command("task", "set your current task headline", _text_only, _run_task),
    Command("export", "save the whole board to a file (md, json, jsonl, text)", _add_export_arguments, _run_export),
    Command("ack", "acknowledge the briefing and charter, move your cursor to the end", _no_arguments, _run_ack),
    Command("wipe", "empty the board: every post moves to the archive (--purge deletes it all); operator only, asks first", _add_wipe_arguments, _run_wipe),
    Command("compact", "ask the notifier to compact a member's context (operator, or --self)", _add_control_arguments, _run_compact),
    Command("clear", "ask the notifier to clear a member's context (operator only)", _add_control_arguments, _run_clear),
    Command("say", "type one line into a member's input box now (human only, from the team console)", _add_say_arguments, _run_say),
]
