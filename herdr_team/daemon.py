"""The notifier daemon: sole caller of ``agent.prompt`` and ``notification.show`` (plan 8).

Process model (plan 8.1): fork, ``setsid``, fork again, ``chdir`` to the
session dir, ``dup2`` ``/dev/null`` onto 0 and ``daemon.log`` onto 1 and 2,
close every other inherited fd, **then** open and flock ``daemon.lock``
(``store.daemon_lock``). ``daemon.json`` is written before the original
parent returns so ``bin/hook`` short-circuits from the first event. The
identity variables ``HERDR_PANE_ID``, ``HERDR_TAB_ID``, ``HERDR_WORKSPACE_ID``
are unset at start (``api.scrub_env``); the daemon never uses ``--current``.

``daemon.json`` (one line, read by the sh gate with parameter expansion)::

    {"pid":N,"start_time":"<ps -o lstart= trimmed>","beat_at":"<iso>",
     "socket":"...","socket_inode":N,"version":"0.1.0","herdr_version":"0.8.2",
     "protocol":20,"manifest_version":"0.1.0","last_ping_at":"<iso>",...}

Main loop: one ``events.subscribe`` with the six global kinds; every event
only wakes the evaluator; ``agent.list`` is polled every 2 s (500 ms while
a directed post is pending); boards are tailed every 250 ms; jobs under
``notifier/jobs/`` are consumed; tokens are restamped every 30 s;
``who.json`` is rewritten at most once per second; toasts go through one
queue that honours the server's ``reason``. Reconnect backoff 1, 2, 4 ... 30 s
for 60 s, then exit releasing the lock. Every (re)connect is a cold start.

Hard rule: never ``agent.prompt`` a terminal that is not in a roster. The
guard is ``Daemon._assert_roster_terminal``; a trip is counted as
``wrong_target`` in the ledger and must stay zero.

Delivery decisions are delegated to ``herdr_team.gate`` and the texts to
``herdr_team.nudge``; ``herdr_team.store`` owns the board, cursors, and the
roster file.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from herdr_team import PLUGIN_ID, VERSION, capabilities, gate, nudge, render, roster, sanitize, store
from herdr_team import charter as _charter
from herdr_team import identity as _identity
from herdr_team import launch as _launch
from herdr_team import links as _links
from herdr_team import models as _models
from herdr_team import context as _context
from herdr_team import operator as _operator
from herdr_team import usage as _usage
from herdr_team import workdir as _workdir
from herdr_team.api import HerdrApi, IDENTITY_ENV_VARS, PROMPT_TIMEOUT_S, read_text, scrub_env
from herdr_team.errors import EXIT_DAEMON_DOWN, EXIT_OK, EXIT_REFUSED, EXIT_UNREACHABLE, HerdrTeamError, LockTimeout
from herdr_team.ledger import (
    RESULT_DRY,
    RESULT_HUNG,
    RESULT_LANDED_IN_TURN,
    RESULT_LANDED_WORKING,
    RESULT_NOT_SUBMITTED,
    RESULT_REFUSED,
    RESULT_TRANSIENT,
    RESULT_WRONG_OCCUPANT,
    Attempt,
    Ledger,
    now_iso,
)
from herdr_team.paths import FILE_STEM_RE as _FILE_STEM_RE
from herdr_team.paths import Layout, SessionPaths, TeamPaths, ensure_session_dirs, ensure_team_dirs, plugin_root, resolve_layout, socket_allowed

HEARTBEAT_S = 30.0
TASK_TTL_MS = 120000
POLL_IDLE_S = 2.0
POLL_PENDING_S = 0.5
TAIL_TICK_S = 0.25
RECONNECT_BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
RECONNECT_GIVE_UP_S = 60.0
#: ``plugin.list`` poll interval. PK-08 needs a disabled plugin to clear its tokens and view and
#: stop the daemon within 15 s; the plan's 60 s (measured live: 54 s) missed that by 4x.
REGISTRY_POLL_S = 10.0
#: Env the daemon must not inherit (plan S-07): the identity trio plus the session name and the
#: private TUI client socket. ``HERDR_SOCKET_PATH`` is pinned instead, so nothing else selects a server.
DAEMON_DROPPED_ENV_VARS = tuple(IDENTITY_ENV_VARS) + ("HERDR_SESSION", "HERDR_CLIENT_SOCKET_PATH")
GRACE_WINDOW_S = 30.0
#: After a (re)connect the daemon re-runs ``cmd_ui.reconcile_console`` this many seconds later, and again while a
#: labelled ``Team console`` pane stays unresolved (foreground unknown or a launch in its grace), bounded (RT-05).
CONSOLE_RECONCILE_DELAY_S = 3.0
CONSOLE_RECONCILE_ATTEMPTS = 12
LOCK_TAKEOVER_WAIT_S = 10.0
SUBSCRIPTIONS = ("pane.agent_detected", "pane.closed", "pane.exited", "pane.moved", "pane.focused", "pane.updated")

SUPPORTED_HERDR_MAJOR_MINORS = ("0.8", "0.9")
VERSION_WATCH_S = 5.0
RECONCILE_POLL_S = 10.0
#: The team artifacts walk is filesystem work on a per-team loop, so it runs on
#: its own slow cadence rather than with every ``scan_teams`` (every 2 s).
ARTIFACTS_POLL_S = 10.0
#: How often the unread sweep looks for an idle member holding mail nothing woke it for.
IDLE_SWEEP_POLL_S = 10.0
#: How often a member's context is re-read from its harness's own files. The
#: read is a tail of one file, skipped entirely when that file has not moved.
CONTEXT_POLL_S = 15.0
#: How often to look for asks waiting on the operator, and how long to leave
#: the popup slot alone after a failed open. The slot is shared with compose,
#: the picker and the popups the operator opens themselves, so ``ui_busy`` is
#: an ordinary answer here, not an error: back off and try again.
ASK_POLL_S = 5.0
ASK_POPUP_RETRY_S = 30.0
#: The ``team_context`` sidebar token fades if the notifier stops reading, the
#: same health signal ``team_task`` uses.
CONTEXT_TTL_MS = 120000
#: How long a typed ``/compact`` or ``/clear`` may go unobserved before the
#: daemon gives up waiting for its effect. Not a completion timer: the job is
#: closed by what actually happens (a new session, a new phase, a context that
#: fell), and this only bounds how long it stays open when nothing does.
#: Claude's compaction runs two to three minutes here, so the bound is well
#: past that rather than near it.
CONTROL_OBSERVE_S = 360.0
#: A context reading this much below the one taken when the keystroke landed
#: counts as the compaction having happened. Compaction rewrites the history
#: into a summary, so the drop is large; a normal turn only ever adds.
CONTROL_DROP_RATIO = 0.7
#: Between two control lines typed in one job (``/model`` then ``/effort``):
#: Claude answers each at once, but a second line typed on top of the first
#: would land while the first is still being read.
CONTROL_LINE_GAP_S = 1.0
#: How often a team's manager cursor is checked against the messages another
#: team's manager sent it, so the sender's console can show ``read by``.
LINK_RECEIPT_POLL_S = 5.0
#: A restart (``model --apply restart``) exits the agent and resumes its
#: session with new flags. These bound each half so a member cannot sit in
#: limbo: the exit keystroke must empty the pane, and the resumed agent must
#: come back and re-report its session.
RESTART_EXIT_S = 30.0
RESTART_START_S = 90.0
#: Minimum gap between two swept nudges for one member. A broadcast never
#: interrupts on its own, so this is the pace at which a chatty team's
#: announcements reach an idle teammate: one nudge, not one per post.
IDLE_SWEEP_AFTER_S = 180.0
#: Floor between two rewrites of a team's ``board.md`` snapshot. The board can
#: move many times a minute and the file is the whole archive, so it is written
#: on change but no more often than this.
BOARD_SNAPSHOT_MIN_INTERVAL_S = 60.0
#: How each system event is proactively delivered. Every system record remains
#: visible board awareness; this table alone may turn one into a wake or toast:
#: ``wake: all`` nudges every member when the record is ``urgent``, ``wake:
#: named`` gives each named member an ordinary (gated) nudge, ``toast`` reaches
#: the operator's toast queue. One table, because the two lists this replaced
#: were maintained by hand and the manager announcement shipped on neither --
#: written to the board, delivered to no one.
SYSTEM_EVENT_DELIVERY: Dict[str, Dict[str, Any]] = {
    "charter_updated": {"wake": "all"},
    "member_joined": {"wake": "all"},
    "knowledge_updated": {"wake": "all"},
    "instructions_updated": {"wake": "all"},
    "manager_changed": {"wake": "all", "toast": True},
    "context_high": {"wake": "named"},
    "model_changed": {"wake": "named"},
    "link_established": {"wake": "named"},
    "link_broken": {"wake": "named"},
    "link_read": {},  # a receipt for the sending console; nobody is woken
    "board_cleared": {},  # visible on the board and in hook context; nobody is woken
}
URGENT_SYSTEM_EVENTS = tuple(e for e, d in SYSTEM_EVENT_DELIVERY.items() if d.get("wake") == "all")
NAMED_SYSTEM_EVENTS = tuple(e for e, d in SYSTEM_EVENT_DELIVERY.items() if d.get("wake") == "named")
TOAST_SYSTEM_EVENTS = tuple(e for e, d in SYSTEM_EVENT_DELIVERY.items() if d.get("toast"))
#: Unanswered asks the daemon keeps in memory per team (``TeamState.open_asks``).
OPEN_ASKS_MAX = 400
#: A change is announced only once the tree has stopped moving for one whole
#: poll, so a build or a data dump yields one record instead of one every ten
#: seconds. That costs a single drop up to ``2 * ARTIFACTS_POLL_S`` of latency,
#: which is free in practice: a member reads the board at its next turn anyway.
#: A tree that never settles is still announced this often.
ARTIFACTS_MAX_WAIT_S = 300.0
#: Floor between two records for one team, applied only after one has posted,
#: so the first drop is never delayed.
ARTIFACTS_MIN_INTERVAL_S = 60.0
RECONCILE_POLL_BOUND_S = 300.0
JOBS_POLL_S = 0.5
WHO_COALESCE_S = 1.0
HOLD_REEVALUATE_S = 5.0
TRANSIENT_BACKOFF_MIN_S = 3.0
TRANSIENT_BACKOFF_MAX_S = 60.0
PANE_STUCK_S = 60.0
STALLED_READ_DELAY_S = 1.5
LANDED_FAST_MS = 250.0
#: ``say`` (a human line typed into a member now, docs/cli.md section 7): the socket timeout of its one
#: ``agent.prompt`` (sent without ``wait``, so a stall costs at most this per tick), the asynchronous
#: confirmation window, and the age past which a job left over from a dead daemon is refused as stale.
SAY_PROMPT_TIMEOUT_S = 5.0
SAY_CONFIRM_S = 5.0
SAY_MAX_AGE_S = 10.0
#: Origins whose ``direct`` records the daemon types: the verified console only (``cmd_board.SAY_VIAS``).
SAY_VIAS = ("console",)
#: Kinds verified live to queue text typed into a running turn (``!!``); other kinds are typed but flagged.
FORCE_VERIFIED_KINDS = ("claude",)
RENUDGE_AFTER_S = (120.0, 300.0, 600.0)
# The post TTL (plan 8.3, 30 min of target-active time) is ``gate.POST_TTL_MS``, per team via ``config.gate.post_ttl_ms``.
BRIEF_ACK_S = 90.0
DIALOG_TOAST_AFTER_S = 600.0
TOAST_RETRY_BUSY_S = 1.1
TOAST_GIVE_UP_S = 30.0
#: How long the ``disabled`` verdict is trusted before one more ``notification.show`` probe.
TOAST_DISABLED_RECHECK_S = 60.0
TOAST_RETRY_NO_CLIENT_S = 15.0
TOAST_GLOBAL_INTERVAL_S = 1.0
KIND_UNVERIFIED_TOAST_S = 3600.0
LOG_ROTATE_BYTES = 1024 * 1024
LOG_ROTATE_KEEP = 3
PING_FAILURES_BEFORE_RECONNECT = 3
STATUS_PIPE_TIMEOUT_S = LOCK_TAKEOVER_WAIT_S * 2 + 10.0
#: A Claude Stop-hook block for seqs at or above the pending ones suppresses nudges this long (plan 12, SK-08).
STOP_BLOCK_SUPPRESS_S = 600.0
#: ``team_task`` is restamped when its value changed or this long after the last stamp (TTL 120 s; plan 5.3, register).
TASK_RESTAMP_S = 90.0
#: A kind becomes ``verified`` in kinds.json after this many round trips at ``KIND_VERIFY_CLEAN_RATE`` (plan 8.3).
KIND_VERIFY_WINDOW = 20
KIND_VERIFY_CLEAN_RATE = 0.9

_MANIFEST_VERSION_RE = re.compile(r'^version\s*=\s*"([^"\n]*)"', re.M)


# --------------------------------------------------------------------------
# small helpers


def _now_ms(clock: Callable[[], float]) -> float:
    return clock() * 1000.0


def _parse_iso(value: Optional[str]) -> Optional[float]:
    """Seconds since the epoch for an ISO-8601 UTC timestamp (``Z`` or an offset), else None.

    Timezone-aware throughout: a ``Z`` suffix is UTC and a naive timestamp
    is taken as UTC, never as local time (the daemon's TTLs, heartbeat age,
    and mute expiries all go through here, so this must not depend on the
    host zone).
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None:
        # ``fromisoformat`` on 3.9 accepts only 3 or 6 fractional digits; fall back to a strict parse.
        match = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(?:([+-]\d{2}):?(\d{2})|\+00:00)?$", text)
        if not match:
            return None
        try:
            base = calendar.timegm(time.strptime(match.group(1) + "T" + match.group(2), "%Y-%m-%dT%H:%M:%S"))
        except (ValueError, OverflowError):
            return None
        frac = float("0." + match.group(3)) if match.group(3) else 0.0
        offset = 0
        if match.group(4) is not None:
            sign = -1 if match.group(4).startswith("-") else 1
            offset = sign * (abs(int(match.group(4))) * 3600 + int(match.group(5)) * 60)
        return base + frac - offset
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _short_text(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def read_manifest_version(root: Optional[Path] = None) -> Optional[str]:
    """``version = "..."`` from ``herdr-plugin.toml``; None when unreadable or unparseable."""
    path = (root or plugin_root()) / "herdr-plugin.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _MANIFEST_VERSION_RE.search(text)
    if not match:
        return None
    return match.group(1)


def socket_inode(path: Path) -> Optional[int]:
    try:
        return os.stat(path).st_ino
    except OSError:
        return None


# --------------------------------------------------------------------------
# daemon.json and process identity


@dataclass
class DaemonInfo:
    pid: int
    start_time: str
    beat_at: str
    socket: str
    socket_inode: Optional[int]
    version: str
    herdr_version: Optional[str]
    protocol: Optional[int]
    manifest_version: Optional[str] = None
    last_ping_at: Optional[str] = None
    started_at: Optional[str] = None
    identity_env_unset: Optional[bool] = None
    capabilities: Optional[Dict[str, Optional[bool]]] = None

    def to_json(self) -> Dict[str, Any]:
        obj: Dict[str, Any] = {
            "pid": int(self.pid),
            "start_time": self.start_time,
            "beat_at": self.beat_at,
            "socket": self.socket,
            "socket_inode": self.socket_inode,
            "version": self.version,
            "herdr_version": self.herdr_version,
            "protocol": self.protocol,
        }
        if self.manifest_version is not None:
            obj["manifest_version"] = self.manifest_version
        if self.last_ping_at is not None:
            obj["last_ping_at"] = self.last_ping_at
        if self.started_at is not None:
            obj["started_at"] = self.started_at
        if self.identity_env_unset is not None:
            obj["identity_env_unset"] = self.identity_env_unset
        if self.capabilities is not None:
            obj["capabilities"] = dict(self.capabilities)
        return obj

    @classmethod
    def from_json(cls, obj: Dict[str, Any]) -> "DaemonInfo":
        def _int(value: Any) -> Optional[int]:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        pid = _int(obj.get("pid"))
        if pid is None:
            raise ValueError("daemon.json has no pid")
        return cls(
            pid=pid,
            start_time=str(obj.get("start_time") or "").strip(),
            beat_at=str(obj.get("beat_at") or ""),
            socket=str(obj.get("socket") or ""),
            socket_inode=_int(obj.get("socket_inode")),
            version=str(obj.get("version") or ""),
            herdr_version=obj.get("herdr_version") if isinstance(obj.get("herdr_version"), str) else None,
            protocol=_int(obj.get("protocol")),
            manifest_version=obj.get("manifest_version") if isinstance(obj.get("manifest_version"), str) else None,
            last_ping_at=obj.get("last_ping_at") if isinstance(obj.get("last_ping_at"), str) else None,
            started_at=obj.get("started_at") if isinstance(obj.get("started_at"), str) else None,
            identity_env_unset=obj.get("identity_env_unset") if isinstance(obj.get("identity_env_unset"), bool) else None,
            capabilities=dict(obj["capabilities"]) if isinstance(obj.get("capabilities"), dict) else None,
        )


def process_start_time(pid: int) -> Optional[str]:
    """``ps -o lstart= -p <pid>`` trimmed; None when the pid is gone."""
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    text = completed.stdout.decode("utf-8", "replace").strip()
    if completed.returncode != 0 or not text:
        return None
    return text


def pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True


def read_daemon_info(session: SessionPaths) -> Optional[DaemonInfo]:
    obj = store.read_json(session.daemon_json, default=None)
    if not isinstance(obj, dict):
        return None
    try:
        return DaemonInfo.from_json(obj)
    except ValueError:
        return None


def write_daemon_info(session: SessionPaths, info: DaemonInfo) -> None:
    store.write_json(session.daemon_json, info.to_json(), fsync=False)


def info_alive(info: Optional[DaemonInfo]) -> bool:
    if info is None or info.pid <= 0 or not info.start_time:
        return False
    if not pid_alive(info.pid):
        return False
    live = process_start_time(info.pid)
    return live is not None and live == info.start_time


def daemon_alive(session: SessionPaths) -> bool:
    """Live pid whose start time matches ``daemon.json`` (same rule as ``bin/hook``)."""
    return info_alive(read_daemon_info(session))


def beat_age_s(session: SessionPaths) -> Optional[float]:
    info = read_daemon_info(session)
    if info is None:
        return None
    beat = _parse_iso(info.beat_at)
    if beat is None:
        return None
    return max(0.0, time.time() - beat)


def stop_daemon(session: SessionPaths, timeout_s: float = 10.0) -> bool:
    """SIGTERM the pid in ``daemon.json`` only after the start time matches; True when it is gone."""
    info = read_daemon_info(session)
    if not info_alive(info):
        return False
    assert info is not None
    try:
        os.kill(info.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        if not pid_alive(info.pid) or process_start_time(info.pid) != info.start_time:
            return True
        time.sleep(0.05)
    return not pid_alive(info.pid)


# --------------------------------------------------------------------------
# detach


def _close_fds_except(keep: Sequence[int]) -> None:
    keep_set = set(int(fd) for fd in keep)
    try:
        limit = os.sysconf("SC_OPEN_MAX")
    except (ValueError, OSError, AttributeError):
        limit = 1024
    if limit <= 0 or limit > 65536:
        limit = 65536
    start = 3
    for fd in sorted(keep_set):
        if fd >= start:
            if fd > start:
                os.closerange(start, fd)
            start = fd + 1
    if start < limit:
        os.closerange(start, limit)


def _open_log_fd(session: SessionPaths) -> int:
    return store.secure_open(session.daemon_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT)


def rotate_log(path: Path, keep: int = LOG_ROTATE_KEEP, limit: int = LOG_ROTATE_BYTES) -> bool:
    """Rename ``path`` to ``.1`` (``.1`` to ``.2`` ...) when it exceeds ``limit``; True when rotated."""
    try:
        size = os.lstat(path).st_size
    except OSError:
        return False
    if size <= limit:
        return False
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
        except FileNotFoundError:
            continue
        except OSError:
            return False
    return True


def _identity_env_unset() -> bool:
    return not any(name in os.environ for name in IDENTITY_ENV_VARS)


def _session_env_unset() -> bool:
    """``HERDR_SESSION`` and ``HERDR_CLIENT_SOCKET_PATH`` gone: only the pinned socket selects a server."""
    return not any(name in os.environ for name in DAEMON_DROPPED_ENV_VARS if name not in IDENTITY_ENV_VARS)


def _stdio_description(session: SessionPaths) -> str:
    """Where fds 0-2 point, by device/inode comparison (no ``fcntl`` outside ``store``)."""
    names = []
    try:
        null_st = os.stat("/dev/null")
    except OSError:
        null_st = None
    try:
        log_st = os.stat(session.daemon_log)
    except OSError:
        log_st = None
    for fd in (0, 1, 2):
        try:
            st = os.fstat(fd)
        except OSError:
            names.append("fd{}=closed".format(fd))
            continue
        if null_st is not None and (st.st_dev, st.st_ino) == (null_st.st_dev, null_st.st_ino):
            names.append("fd{}=/dev/null".format(fd))
        elif log_st is not None and (st.st_dev, st.st_ino) == (log_st.st_dev, log_st.st_ino):
            names.append("fd{}=daemon.log".format(fd))
        else:
            names.append("fd{}=other".format(fd))
    return " ".join(names)


def _write_status(fd: Optional[int], obj: Dict[str, Any]) -> None:
    if fd is None:
        return
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        while data:
            written = os.write(fd, data)
            data = data[written:]
    except OSError:
        pass


def acquire_daemon_lock(session: SessionPaths, socket_path: Path, replace: bool, wait_s: float = LOCK_TAKEOVER_WAIT_S, log: Optional[Callable[[str], None]] = None) -> Tuple[Optional[store.FileLock], Dict[str, Any]]:
    """Take ``daemon.lock`` with the plan 8.1 takeover rules.

    Returns ``(lock, info)``; ``lock`` is None when another daemon keeps it and
    no takeover applies (``info['already_running']``). Takeover happens with
    ``replace`` or when the holder's recorded identity (socket path or socket
    inode) differs from ours; the holder is SIGTERMed only when its start
    time matches ``daemon.json``.
    """
    say = log or (lambda _m: None)
    lock = store.daemon_lock(session)
    if lock.try_acquire():
        return lock, {"replaced": False}
    say("daemon.lock held; waiting up to {:g}s".format(wait_s))
    try:
        lock.acquire(wait_s)
        return lock, {"replaced": False}
    except LockTimeout:
        pass
    holder = read_daemon_info(session)
    identity_differs = False
    if holder is not None:
        current_inode = socket_inode(socket_path)
        if holder.socket and os.path.realpath(holder.socket) != os.path.realpath(os.fspath(socket_path)):
            identity_differs = True
        elif holder.socket_inode is not None and current_inode is not None and holder.socket_inode != current_inode:
            identity_differs = True
    if not replace and not identity_differs:
        return None, {"already_running": True, "replaced": False, "holder": holder.to_json() if holder else None}
    if holder is not None and info_alive(holder):
        say("taking over from pid {} (replace={}, identity_differs={})".format(holder.pid, replace, identity_differs))
        try:
            os.kill(holder.pid, signal.SIGTERM)
        except OSError:
            pass
    try:
        lock.acquire(wait_s)
    except LockTimeout:
        if holder is not None and info_alive(holder):
            say("holder ignored SIGTERM; sending SIGKILL to pid {}".format(holder.pid))
            try:
                os.kill(holder.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                lock.acquire(2.0)
            except LockTimeout:
                return None, {"already_running": True, "replaced": False, "error": "daemon_lock_held"}
        else:
            return None, {"already_running": True, "replaced": False, "error": "daemon_lock_held"}
    return lock, {"replaced": True, "holder": holder.to_json() if holder else None}


def _grandchild_main(layout: Layout, env: Dict[str, str], replace: bool, status_fd: Optional[int], allow_version: bool, dry_nudge: bool) -> None:
    """Runs in the detached grandchild; never returns."""
    session = layout.session
    exit_code = 1
    try:
        os.chdir(os.fspath(session.root))
        null_fd = os.open("/dev/null", os.O_RDWR)
        rotate_log(session.daemon_log)
        log_fd = _open_log_fd(session)
        os.dup2(null_fd, 0)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        keep = [0, 1, 2]
        if status_fd is not None:
            keep.append(status_fd)
        _close_fds_except(keep)
        # 3..n are closed now; the lock fd is opened *after* this point.
        # ``env`` is the scrubbed copy from ``detach_and_run``; overlaying it on ``os.environ``
        # keeps whatever the caller inherited, so the dropped names are removed here as well
        # (observed live in S-07: ``HERDR_SESSION`` survived into the daemon before this pop).
        for name in DAEMON_DROPPED_ENV_VARS:
            os.environ.pop(name, None)
        for key, value in env.items():
            if key not in DAEMON_DROPPED_ENV_VARS:
                os.environ[key] = value
        sys.stdout = os.fdopen(1, "w", buffering=1, encoding="utf-8", errors="replace", closefd=False)
        sys.stderr = os.fdopen(2, "w", buffering=1, encoding="utf-8", errors="replace", closefd=False)

        def log(message: str) -> None:
            sys.stderr.write("{} [{}] {}\n".format(now_iso(), os.getpid(), message))
            sys.stderr.flush()

        try:
            wait_s = float(os.environ.get("HERDR_TEAM_LOCK_WAIT_S") or LOCK_TAKEOVER_WAIT_S)
        except ValueError:
            wait_s = LOCK_TAKEOVER_WAIT_S
        lock, lock_info = acquire_daemon_lock(session, layout.socket, replace, wait_s=wait_s, log=log)
        if lock is None:
            lock_info.setdefault("status", "already_running")
            _write_status(status_fd, {"status": "already_running", "daemon": lock_info.get("holder")})
            log("another daemon holds the lock; exiting")
            exit_code = 0
            return
        api = HerdrApi(layout.socket, env=os.environ)
        daemon = Daemon(layout, dict(os.environ), api, dry_nudge=dry_nudge, allow_version=allow_version)
        daemon.lock = lock
        daemon.detached = True
        try:
            daemon.connect_server()
        except HerdrTeamError as err:
            if err.code == "herdr_version_mismatch":
                _write_status(status_fd, {"status": "error", "error": err.to_json()})
                log("refusing to run: {}".format(err))
                lock.release()
                return
            log("server not reachable at start ({}); will retry".format(err))
        info = daemon.write_info(started=True)
        log("started pid {} start_time {!r} socket {} {}".format(info.pid, info.start_time, layout.socket, _stdio_description(session)))
        log("identity env unset: {}; session env unset: {}".format(_identity_env_unset(), _session_env_unset()))
        _write_status(status_fd, {"status": "started", "replaced": bool(lock_info.get("replaced")), "daemon": info.to_json()})
        if status_fd is not None:
            os.close(status_fd)
            status_fd = None
        exit_code = daemon.run()
    except BaseException as err:  # noqa: BLE001 - the grandchild must never unwind into the parent's code
        try:
            _write_status(status_fd, {"status": "error", "error": {"code": "daemon_start_failed", "message": "{}: {}".format(type(err).__name__, err)}})
            sys.stderr.write("{} daemon crashed: {}: {}\n".format(now_iso(), type(err).__name__, err))
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        exit_code = 1
    finally:
        if status_fd is not None:
            try:
                os.close(status_fd)
            except OSError:
                pass
        os._exit(exit_code)


def detach_and_run(layout: Layout, env: Mapping[str, str], replace: bool = False, allow_version: bool = False, dry_nudge: bool = False) -> Dict[str, Any]:
    """The double fork; returns in the original parent once the grandchild reported.

    The result is ``{"status": "started"|"already_running"|"error", "daemon": {...},
    "replaced": bool, "intermediate_exit_ms": float, "error": {...}}``.
    """
    session = layout.session
    ensure_session_dirs(session)
    scrubbed = scrub_env(env, DAEMON_DROPPED_ENV_VARS)
    scrubbed["HERDR_SOCKET_PATH"] = os.fspath(layout.socket)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    read_fd, write_fd = os.pipe()
    t0 = time.monotonic()
    pid = os.fork()
    if pid == 0:
        # intermediate child
        try:
            os.close(read_fd)
            os.setsid()
            grandchild = os.fork()
            if grandchild == 0:
                _grandchild_main(layout, scrubbed, replace, write_fd, allow_version, dry_nudge)
        except BaseException:  # noqa: BLE001
            _write_status(write_fd, {"status": "error", "error": {"code": "daemon_start_failed", "message": "fork failed"}})
        finally:
            os._exit(0)
    os.close(write_fd)
    _, _status = os.waitpid(pid, 0)
    intermediate_exit_ms = (time.monotonic() - t0) * 1000.0
    result: Dict[str, Any] = {"status": "error", "error": {"code": "daemon_start_failed", "message": "daemon reported nothing"}}
    buffer = b""
    deadline = time.monotonic() + STATUS_PIPE_TIMEOUT_S
    try:
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([read_fd], [], [], remaining)
            if not ready:
                result = {"status": "error", "error": {"code": "daemon_start_failed", "message": "timed out waiting for the daemon to report"}}
                break
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            buffer += chunk
            if b"\n" in buffer:
                break
    finally:
        os.close(read_fd)
    line = buffer.split(b"\n", 1)[0].strip()
    if line:
        try:
            parsed = json.loads(line.decode("utf-8", "replace"))
            if isinstance(parsed, dict):
                result = parsed
        except ValueError:
            result = {"status": "error", "error": {"code": "daemon_start_failed", "message": "unparseable daemon report"}}
    result["intermediate_exit_ms"] = intermediate_exit_ms
    result.setdefault("replaced", False)
    return result


def ensure_daemon(layout: Layout, env: Mapping[str, str]) -> bool:
    """Start the daemon when none is alive; True when one is running afterwards.

    Honours ``allowed-sockets`` exactly like ``daemon start`` (plan 4.1 /
    PK-07): a socket the rig has not listed never gets a daemon forked
    against it, whichever command asked (``create``, ``add``, ``bind``,
    the team-up action).
    """
    if daemon_alive(layout.session):
        return True
    try:
        if not socket_allowed(layout.config_dir, layout.socket):
            return False
    except HerdrTeamError:
        return False
    try:
        result = detach_and_run(layout, env)
    except OSError:
        return False
    if result.get("status") not in ("started", "already_running"):
        return False
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if daemon_alive(layout.session):
            return True
        time.sleep(0.05)
    return daemon_alive(layout.session)


# --------------------------------------------------------------------------
# fallbacks for modules owned by other implementers


def _artifacts_watch_enabled(doc: Dict[str, Any]) -> bool:
    """``config.artifacts.watch``: an operator can turn the watcher off per team."""
    config = doc.get("config") if isinstance(doc, dict) else None
    artifacts = config.get("artifacts") if isinstance(config, dict) else None
    if not isinstance(artifacts, dict):
        return True
    return artifacts.get("watch") is not False


def _load_roster_doc(team: TeamPaths) -> Optional[Dict[str, Any]]:
    doc = store.read_json(team.team_json, default=None)
    if not isinstance(doc, dict) or not isinstance(doc.get("members"), list):
        return None
    return doc


def update_roster(team: TeamPaths, mutate: Callable[[Dict[str, Any]], Any]) -> Optional[Dict[str, Any]]:
    """Lock, load, ``mutate(doc)`` in place, bump ``revision``, write; None when the team is gone."""
    try:
        return store.RosterStore(team).update(mutate)
    except HerdrTeamError as err:
        if err.code == "team_not_found":
            return None
        raise


def read_board_records(team: TeamPaths, since_seq: int = 0) -> List[Dict[str, Any]]:
    """Records with ``seq > since_seq`` from the active board, sorted and deduped; never locks."""
    return list(store.BoardStore(team).read(since_seq=since_seq))


def _board_max_seq(team: TeamPaths) -> int:
    """Highest seq known to the store (board.seq hint, tail scan, archives)."""
    return int(store.BoardStore(team).max_seq())


#: Fields ``system_record`` owns outright: who wrote it, who it is for, what it
#: says, and what the appender assigns. ``extra`` may not name one of these.
#: The rest of the schema (``reply_to``, ``refs``, ``urgent``, ``ttl_ms`` and
#: the like) is detail a caller legitimately sets, so it stays open: a ``typed``
#: outcome, for one, replies to the record it reports on.
RESERVED_RECORD_FIELDS = frozenset({
    "v", "seq", "ts", "from", "from_label", "from_kind", "from_pane", "from_terminal", "from_gen",
    "origin", "to", "kind", "text", "event", "team",
})


def system_record(team_name: str, event: str, text: str, to: Sequence[str], socket: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A plan 6.1 ``system`` record without ``seq``/``ts`` (the appender assigns them)."""
    rec: Dict[str, Any] = {
        "v": 1, "seq": None, "ts": None, "from": "system", "from_label": None, "from_kind": None,
        "from_pane": None, "from_terminal": None, "from_gen": None,
        "origin": {"via": "system", "verified": True, "pid": os.getpid(), "ppid": os.getppid(), "workspace_id": None, "tab_id": None, "socket": socket},
        "to": list(to) or ["all"], "to_role": None, "kind": "system", "text": text, "refs": [], "reply_to": None,
        "retracts": None, "supersedes": None, "urgent": False, "ttl_ms": None, "truncated": False, "event": event, "relayed_for": None,
    }
    for key, value in (extra or {}).items():
        # ``extra`` carries a record's own detail, never its structure. A key
        # like ``from`` or ``to`` used to overwrite the field of that name:
        # here it turned a system record into one claiming a filesystem path
        # as its author, which the store then refused. A silent collision on
        # ``text`` would not have been refused at all.
        if key in RESERVED_RECORD_FIELDS:
            raise HerdrTeamError("record_invalid", "{!r} is a record field, not extra detail".format(key), EXIT_REFUSED,
                                 {"field": key, "event": event})
        rec[key] = value
    rec["team"] = team_name
    return rec


def say_source_problem(rec: Any, member: str, console_terminal: Any) -> Optional[str]:
    """Why the daemon must not type ``rec`` for a ``say`` job, or None when it is the console's own ``direct`` record.

    The job file carries only a seq; the text comes from the board record,
    and the record must be a verified console author's ``direct`` line to
    exactly this member (plan: agents must never gain this power).
    """
    if not isinstance(rec, dict):
        return "no board record at that seq"
    if rec.get("kind") != "direct":
        return "record #{} is a {} record, not direct".format(rec.get("seq"), rec.get("kind"))
    if rec.get("from") != "human":
        return "record is from {!r}, not the human".format(rec.get("from"))
    if list(rec.get("to") or []) != [member]:
        return "record is addressed to {}, not {}".format(rec.get("to"), member)
    origin = rec.get("origin") if isinstance(rec.get("origin"), dict) else {}
    if origin.get("verified") is not True or origin.get("via") not in SAY_VIAS:
        return "record origin is {} {}".format(origin.get("via"), "verified" if origin.get("verified") else "unverified")
    # ``console_terminal`` is every live console terminal, not one: a session may
    # have a console open per team, and any of them may have typed this line.
    # A single string is still accepted so the signature stays usable directly.
    known = {console_terminal} if isinstance(console_terminal, str) else set(console_terminal or ())
    if not known or rec.get("from_terminal") not in known:
        return "record terminal {!r} is not a live console's {}".format(rec.get("from_terminal"), sorted(known) or "(none open)")
    if not isinstance(rec.get("text"), str) or not rec["text"].strip():
        return "record has no text"
    return None


def append_board_record(team: TeamPaths, record: Dict[str, Any]) -> int:
    """Append one record through ``store.BoardStore`` (plan 6.2 sequence under ``team.lock``)."""
    record = dict(record)
    record.pop("team", None)
    return int(store.BoardStore(team).append(record))


def read_cursor_state(team: TeamPaths, reader: str) -> Tuple[int, Set[int]]:
    """``(cursor seq, seqs above it read individually by a filtered board --new)``."""
    try:
        obj = store.Cursors(team).get(reader)
    except HerdrTeamError:
        return 0, set()
    try:
        seq = max(0, int(obj.get("seq") or 0))
    except (TypeError, ValueError, AttributeError):
        seq = 0
    seen = {int(s) for s in (obj.get("seen") or []) if isinstance(s, int) and not isinstance(s, bool)}
    return seq, seen


def read_cursor_seq(team: TeamPaths, reader: str) -> int:
    return read_cursor_state(team, reader)[0]


_TOAST_SECTION_RE = re.compile(r"^\s*\[ui\.toast\]\s*$")
_TOML_SECTION_RE = re.compile(r"^\s*\[")
_TOAST_DELIVERY_RE = re.compile(r"^\s*delivery\s*=\s*[\"']([A-Za-z_]+)[\"']")


def read_toast_delivery(config_dir: Any) -> str:
    """``[ui.toast] delivery`` from ``<config_dir>/config.toml`` (compiled default ``off``; plan 7.2)."""
    try:
        text = (Path(config_dir) / "config.toml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "off"
    in_section = False
    for line in text.splitlines():
        if _TOAST_SECTION_RE.match(line):
            in_section = True
            continue
        if _TOML_SECTION_RE.match(line):
            in_section = False
            continue
        if in_section:
            match = _TOAST_DELIVERY_RE.match(line)
            if match:
                return match.group(1)
    return "off"


def read_mute(team: TeamPaths) -> Dict[str, Optional[float]]:
    """``{"*"|name: until_epoch_s | None}``; a key present with null means muted indefinitely."""
    obj = store.read_json(team.mute_json, default=None)
    out: Dict[str, Optional[float]] = {}
    if not isinstance(obj, dict):
        return out
    for key, value in obj.items():
        if not isinstance(key, str):
            continue
        if value is None:
            out[key] = None
        elif isinstance(value, str):
            parsed = _parse_iso(value)
            if parsed is not None:
                out[key] = parsed
    return out


def muted_until(mute: Dict[str, Optional[float]], name: str, now_s: float) -> Optional[float]:
    """Epoch seconds until which ``name`` is muted, ``float('inf')`` for indefinitely, None when not muted."""
    best: Optional[float] = None
    for key in ("*", name):
        if key not in mute:
            continue
        until = mute[key]
        value = float("inf") if until is None else until
        if value > now_s and (best is None or value > best):
            best = value
    return best


def headline_for(text: str, columns: int = 24) -> str:
    return sanitize.headline(text, columns)


_KIND_GLYPH = {"request": "→", "done": "✓", "blocked": "!", "question": "?"}


TASK_MAX_AGE_S = 1800.0


def read_task_file(team_paths: TeamPaths, member_name: str) -> Optional[Dict[str, Any]]:
    """The member's ``tasks/<name>.json`` written by ``herdr-synapse task`` (plan 5.3), or None.

    RS-01/RS-04 regression: the CLI stores the task in the team dir, not on
    the roster member, so the heartbeat must read this file to stamp
    ``team_task``. Unsafe names and unreadable files yield None.
    """
    if not isinstance(member_name, str) or not _FILE_STEM_RE.match(member_name):
        return None
    try:
        doc = store.read_json(team_paths.root / "tasks" / (member_name + ".json"))
    except HerdrTeamError:
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("text"), str):
        return None
    return doc


def task_headline(member: Dict[str, Any], last_post: Optional[Dict[str, Any]], now_s: float, task: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """``team_task`` value: the member's ``task`` if younger than 30 min, else its last post headline.

    ``task`` is the ``tasks/<name>.json`` document (``read_task_file``);
    a ``task`` key on the member dict is honoured as a fallback.
    """
    if task is None:
        task = member.get("task")
    if isinstance(task, dict) and isinstance(task.get("text"), str):
        set_at = _parse_iso(task.get("set_at")) if isinstance(task.get("set_at"), str) else None
        if set_at is None or now_s - set_at < TASK_MAX_AGE_S:
            return headline_for(task["text"], 24) or None
    if last_post is not None:
        glyph = _KIND_GLYPH.get(str(last_post.get("kind") or ""), "")
        text = headline_for(str(last_post.get("text") or ""), 24 - (2 if glyph else 0))
        if text:
            return (glyph + " " + text) if glyph else text
    return None


def nudge_text_for(name: str, seqs: Sequence[int], nonce: int) -> str:
    return nudge.nudge_text(name, list(seqs), nonce)


def interrupt_text_for(name: str, seqs: Sequence[int], nonce: int, sender: str) -> str:
    return nudge.interrupt_text(name, list(seqs), nonce, sender)


def interrupt_sender(pending: "Pending") -> str:
    """Who the interrupt line names: the human when the operator joined it, else the first interrupting agent."""
    if "human" in pending.interrupt_authors:
        return "human"
    agents = sorted(pending.interrupt_authors)
    return agents[0] if agents else "human"


def briefing_lines_for(name: str, role: str, team: str, charter_headline: Optional[str], teammates: List[Tuple[str, str]], brief: Optional[str], cli_path: str) -> List[str]:
    return list(nudge.briefing_lines(name, role, team, charter_headline, teammates, brief, cli_path))


def new_nonce() -> int:
    return int(nudge.new_nonce())


def probe_text_for(nonce: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]", "", str(nonce))[:32] or "0"
    return nudge.probe_text(safe)


# -- gate wrappers ------------------------------------------------------------


def gate_is_weak_idle(explain: Optional[Dict[str, Any]]) -> bool:
    return bool(gate.is_weak_idle(explain))


def gate_dialog_line(detection_text: Optional[str], kind: str) -> Optional[str]:
    return gate.dialog_line(detection_text, kind)


def gate_prompt_line_empty(detection_text: Optional[str], kind: str) -> bool:
    return bool(gate.prompt_line_empty(detection_text, kind))


def gate_evaluate(snapshot: Any, pending: Any, now_ms: float, global_last_nudge_ms: Optional[float], pair_exchanges: int, config: Any = None) -> Any:
    """Plan 8.2 gates through ``gate.evaluate``; ``config`` is a ``gate.GateConfig`` or an override mapping."""
    return gate.evaluate(snapshot, pending, now_ms, global_last_nudge_ms, pair_exchanges, config=config)


def gate_config_from_roster(doc: Optional[Dict[str, Any]]) -> Tuple[gate.GateConfig, Optional[Dict[str, Any]], Optional[str]]:
    """``(config, overrides, error)`` from ``team.json`` ``config.gate`` (docs/cli.md section 10).

    A non-numeric ``*_ms`` / ``pair_budget`` value or a ``nudge_focused`` that
    is not a string yields ``DEFAULT_CONFIG`` plus the error text, so one bad
    roster field can never change every gate. An *unknown* key is skipped and
    reported by ``gate_config_from_roster_detailed``; it used to reject the
    whole mapping, which is how a typo switched every override off at once.
    """
    config, applied, error, _skipped = gate_config_from_roster_detailed(doc)
    return config, applied, error


def gate_config_from_roster_detailed(doc: Optional[Dict[str, Any]]) -> Tuple[gate.GateConfig, Optional[Dict[str, Any]], Optional[str], List[str]]:
    """``gate_config_from_roster`` plus the unknown keys that were skipped."""
    config = doc.get("config") if isinstance(doc, dict) and isinstance(doc.get("config"), dict) else None
    overrides = config.get("gate") if config is not None else None
    if overrides is None:
        return gate.DEFAULT_CONFIG, None, None, []
    if not isinstance(overrides, dict):
        return gate.DEFAULT_CONFIG, None, "config.gate is not an object", []
    try:
        built, skipped = gate.GateConfig.from_mapping_lenient(overrides)
    except ValueError as err:
        return gate.DEFAULT_CONFIG, None, str(err), []
    for key, value in overrides.items():
        if key in skipped:
            continue
        if key == "nudge_focused":
            if not isinstance(value, str):
                return gate.DEFAULT_CONFIG, None, "nudge_focused must be a string", []
            if value not in gate.NUDGE_FOCUSED_VALUES:
                # Gate 10 compares against the exact word: "Never" or "no" would silently drop the focus hold.
                return gate.DEFAULT_CONFIG, None, "nudge_focused must be one of {}".format("|".join(gate.NUDGE_FOCUSED_VALUES)), []
        elif key == "interrupt_kinds":
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                return gate.DEFAULT_CONFIG, None, "interrupt_kinds must be a list of kind names", []
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return gate.DEFAULT_CONFIG, None, "{} must be a non-negative number".format(key), []
    return built, {k: v for k, v in overrides.items() if k not in skipped}, None, skipped


# --------------------------------------------------------------------------
# daemon state


@dataclass
class Pending:
    """Undelivered work for one member: a nudge for board seqs or a briefing."""

    seqs: List[int] = field(default_factory=list)
    urgent: bool = False
    authors: Set[str] = field(default_factory=set)
    first_ms: float = 0.0
    kind: str = "nudge"  # nudge | brief | probe | control
    lines: Optional[List[str]] = None
    probe: Optional[Dict[str, Any]] = None
    #: For a ``control`` pending: the action, the keystroke, and who asked.
    control: Optional[Dict[str, Any]] = None
    force: bool = False
    attempts: int = 0
    landed_ms: Optional[float] = None
    landed_seq_max: int = 0
    next_eligible_ms: float = 0.0
    active_ms: float = 0.0
    last_tick_ms: Optional[float] = None
    attempt_id: Optional[str] = None
    gate_seq: Optional[int] = None
    hold: Optional[str] = None
    hold_since_ms: Optional[float] = None
    hold_toasted: bool = False
    transient_failures: int = 0
    renudges: int = 0
    turn_completed_since_landing: bool = False
    #: First ``focused`` hold for this work (gate 10 max-hold clock); reset by any other hold or a landing.
    focus_hold_since_ms: Optional[float] = None
    #: Posts arrived after a landing: one immediate follow-up nudge is allowed (plan 8.2 gate 11).
    follow_up_due: bool = False
    #: The one immediate follow-up of this landing schedule was spent (gate 11); a regular landing resets it.
    follow_up_used: bool = False
    #: Board seqs addressed to the member while this briefing was pending (HP-10, 2026-09-05: they were
    #: dropped and never nudged); they become a nudge when the briefing is acknowledged or given up.
    deferred_seqs: List[int] = field(default_factory=list)
    deferred_authors: Set[str] = field(default_factory=set)
    deferred_urgent: bool = False
    #: The member's cursor as re-read right before the last ``agent.prompt`` (becomes ``last_nudge_cursor_seq``).
    gate_cursor: Optional[int] = None
    #: When the newest seq joined this nudge; the send waits ``burst_window_ms`` after it so a same-second
    #: burst becomes one nudge covering the range (plan 12; M5 ND-03: five posts used to yield "1 new post").
    last_added_ms: Optional[float] = None
    #: Cursor file state at the briefing's landing; an ack is a *later* cursor write at or past ``brief_seq``.
    brief_cursor_updated: Optional[str] = None
    #: A probe that jumped the queue keeps the briefing or nudge it displaced here and puts it back when done.
    resume: Optional["Pending"] = None
    #: A teammate's ``post --interrupt`` is among the seqs: the daemon may type the nudge into a running
    #: turn when the kind allows it and the sender is out of cooldown (``interrupt_state`` says which).
    interrupt: bool = False
    interrupt_authors: Set[str] = field(default_factory=set)
    interrupt_state: Optional[str] = None  # armed | cooldown | kind_not_allowed
    interrupt_sent: bool = False  # the last attempt was typed into a running turn
    deferred_interrupt: bool = False


@dataclass
class MemberRuntime:
    last_nudge_ms: Optional[float] = None
    in_flight: bool = False
    pane_stuck_until_ms: Optional[float] = None
    last_seen_present_ms: Optional[float] = None
    last_headline: Optional[str] = None
    last_post: Optional[Dict[str, Any]] = None
    kind_unverified_toast_ms: Optional[float] = None
    #: Last toast for a gate 3 / gate 11 hold (``kind_mismatch``, ``pair_budget``): one per (member, reason) per hour.
    hold_toast_ms: Dict[str, float] = field(default_factory=dict)
    brief_landed_ms: Optional[float] = None
    brief_seq: Optional[int] = None
    rebriefed: bool = False
    #: Cursor seq at the last landed nudge; gate 11 allows one immediate follow-up once the cursor moved past it.
    last_nudge_cursor_seq: Optional[int] = None
    #: Terminal id of the fingerprint candidate last logged for an unbound member (rehydration step e).
    unbound_candidate: Optional[str] = None
    #: Detection-read digest and when it last changed (gate 10: unchanged for 3 s after the max-hold).
    detection_hash: Optional[str] = None
    detection_stable_since_ms: Optional[float] = None
    #: Last ``team_task`` value and stamp time (restamp only on change or TTL refresh).
    last_task_value: Optional[str] = None
    last_task_stamp_ms: Optional[float] = None
    #: Last context reading and the severity already announced for it. The
    #: severity is what gives the warning hysteresis: a crossing is news once,
    #: not every fifteen seconds for as long as the member stays full.
    context: Optional[Dict[str, Any]] = None
    context_read_ms: Optional[float] = None
    context_mtime: Optional[float] = None
    context_severity: Optional[str] = None
    context_stamp_value: Optional[str] = None
    context_stamp_ms: Optional[float] = None
    #: An operator-requested ``compact``/``clear`` that has been typed and whose
    #: effect has not been seen yet: ``{action, requested_by, typed_ms, used}``.
    control_pending: Optional[Dict[str, Any]] = None
    #: An open restart: ``{"phase": exiting|starting|started, "since_ms", "argv", "kind", "pane_id", ...}``.
    #: While set, a pane with no agent is a member on its way back, not one gone missing.
    restart: Optional[Dict[str, Any]] = None
    #: Harness session the last context reading belonged to. A reading from a
    #: different session is a different history, so it is a new baseline rather
    #: than a fall in the old one.
    context_session: Optional[Any] = None


@dataclass
class Stability:
    state_change_seq: int = -1
    since_ms: Optional[float] = None
    status: Optional[str] = None
    idle_since_ms: Optional[float] = None
    turns: int = 0  # increments on every idle/done -> working transition


@dataclass
class SayState:
    """A human line typed into a member, waiting for the next agent poll to confirm it landed."""

    seq: int
    attempt_id: str
    sent_ms: float
    gate_seq: int
    status_before: str
    force: bool
    text: str
    kind: str
    pane_id: str
    requested_by: Any = None
    prompt_ms: float = 0.0


@dataclass
class TeamState:
    name: str
    paths: TeamPaths
    ledger: Ledger
    roster: Dict[str, Any] = field(default_factory=dict)
    tailer: Optional[store.BoardTailer] = None  # plan 6.3 contract, state in notifier/state.json
    watermark: int = 0
    pending: Dict[str, Pending] = field(default_factory=dict)
    runtime: Dict[str, MemberRuntime] = field(default_factory=dict)
    human_queue: List[Dict[str, Any]] = field(default_factory=list)
    retracted: Set[int] = field(default_factory=set)
    open_intents: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: Asks still waiting on the operator, by seq; seeded from the board once,
    #: then kept current by ``_track_ask`` as records are ingested.
    open_asks: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    #: ``(mtime, seqs)`` of ``dismissed-asks.json`` at the last read.
    dismissed_cache: Optional[Tuple[Optional[float], List[int]]] = None
    #: Messages delivered here across a link and not yet read by this team's
    #: manager, by seq; ``_track_link`` fills it, ``poll_link_receipts`` drains it.
    link_inbox: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    link_receipt_ms: Optional[float] = None
    #: Last seen ``artifacts/`` fingerprint, or None before the first scan (which only seeds it).
    artifacts_seen: Optional[Dict[str, Tuple[int, int, int]]] = None
    #: When that scan last ran; the walk is throttled to ``ARTIFACTS_POLL_S``.
    artifacts_scanned_ms: Optional[float] = None
    #: The state the board was last told about; the diff baseline, so deferring is lossless.
    artifacts_announced: Optional[Dict[str, Tuple[int, int, int]]] = None
    #: When the currently pending change was first seen.
    artifacts_pending_since_ms: Optional[float] = None
    #: When a record was last posted for this team.
    artifacts_posted_ms: Optional[float] = None
    #: Member file -> digest of the edit already announced, so one edit is
    #: reported once however many times the mirror is re-rendered.
    adopt_announced: Dict[str, str] = field(default_factory=dict)
    #: When the unread sweep last created a pending for each member.
    swept_ms: Dict[str, float] = field(default_factory=dict)
    #: Unread seqs whose automatic delivery expired or was abandoned, by
    #: member. They remain readable; only automatic retry is terminal.
    delivery_terminal: Dict[str, Set[int]] = field(default_factory=dict)
    #: When the sweep last ran for this team.
    sweep_scanned_ms: Optional[float] = None
    #: Board watermark last written to ``board.md``, and when.
    snapshot_seq: Optional[int] = None
    snapshot_ms: Optional[float] = None
    #: ``say`` lines typed and awaiting confirmation, by member name (``confirm_says``).
    say_inflight: Dict[str, SayState] = field(default_factory=dict)
    #: Monotonic ms of the last interrupt typed per ``(sender, target)``: the ``interrupt_cooldown_ms`` clock.
    interrupts_sent: Dict[Tuple[str, str], float] = field(default_factory=dict)
    #: Plan 8.2 tunables from ``team.json`` ``config.gate`` (``gate.DEFAULT_CONFIG`` without overrides).
    gate_config: gate.GateConfig = gate.DEFAULT_CONFIG
    #: The raw ``config.gate`` value the current ``gate_config`` was built from (change detection).
    gate_overrides: Any = None
    gate_loaded: bool = False

    def members(self) -> List[Dict[str, Any]]:
        return [m for m in self.roster.get("members", []) if isinstance(m, dict)]

    def member(self, name: str) -> Optional[Dict[str, Any]]:
        for m in self.members():
            if m.get("name") == name:
                return m
        return None

    def manager_name(self) -> Optional[str]:
        """The member designated to coordinate this team, when there is one."""
        for m in self.members():
            if m.get("manager") and m.get("kind") != "human" and m.get("status") != "left":
                name = m.get("name")
                return str(name) if isinstance(name, str) else None
        return None

    def member_by_terminal(self, terminal_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not terminal_id:
            return None
        for m in self.members():
            if m.get("terminal_id") == terminal_id:
                return m
        return None

    def member_by_retired_name(self, name: str) -> Optional[Dict[str, Any]]:
        """The member that carried ``name`` before an adopted rename (plan 5.5: old names resolve for 10 min)."""
        now = time.time()
        for m in self.members():
            for old in m.get("previous_names") or []:
                if isinstance(old, dict) and old.get("name") == name:
                    retired = _parse_iso(old.get("retired_at")) if isinstance(old.get("retired_at"), str) else None
                    if retired is None or now - retired <= 600.0:
                        return m
        return None

    def rt(self, name: str) -> MemberRuntime:
        return self.runtime.setdefault(name, MemberRuntime())


@dataclass
class Notification:
    team: str
    seqs: List[int]
    title: str
    body: str
    sound: str
    created_ms: float
    next_ms: float
    reason: Optional[str] = None
    kind: str = "post"  # post | outcome | roster


class Daemon:
    """Event loop: subscribe, poll ``agent.list``, tail boards, run the gate, deliver."""

    def __init__(self, layout: Layout, env: Dict[str, str], api: Any, dry_nudge: bool = False, allow_version: bool = False, clock: Optional[Callable[[], float]] = None, sleep: Optional[Callable[[float], None]] = None) -> None:
        self.layout = layout
        self.env = scrub_env(env)
        self.api = api
        self.dry_nudge = bool(dry_nudge) or self.env.get("HERDR_TEAM_DRY_NUDGE") == "1"
        self.allow_version = allow_version
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.session = layout.session
        self.lock: Optional[store.FileLock] = None
        self.detached = False
        self.stop_requested = False
        self.stop_reason: Optional[str] = None
        self.exit_code = EXIT_OK
        self.backoff = RECONNECT_BACKOFF_S
        self.give_up_s = RECONNECT_GIVE_UP_S
        self.registry_poll_s = REGISTRY_POLL_S
        self.version_watch_s = VERSION_WATCH_S
        self.tick_s = TAIL_TICK_S
        self.server_version: Optional[str] = None
        self.server_protocol: Optional[int] = None
        #: ``None`` until the live server has answered the zero-write probe.
        self.atomic_idle_prompt: Optional[bool] = None
        self.socket_inode: Optional[int] = socket_inode(layout.socket)
        self.started_at = now_iso()
        self.start_time = process_start_time(os.getpid()) or ""
        self.manifest_version = read_manifest_version()
        self.manifest_seen: Optional[str] = None
        self.teams: Dict[str, TeamState] = {}
        self.agents: Dict[str, Dict[str, Any]] = {}
        self.agents_by_pane: Dict[str, Dict[str, Any]] = {}
        #: ``pane.list`` rows by terminal id, fetched lazily once per reconcile (rehydration step b).
        self.panes_by_terminal: Dict[str, Dict[str, Any]] = {}
        self._panes_fetched_ms: Optional[float] = None
        self.reconnect_requested = False
        self.last_agent_list_ms: Optional[float] = None
        self.stability: Dict[str, Stability] = {}
        self.global_last_nudge_ms: Optional[float] = None
        self.pair_exchanges: Dict[Tuple[str, str], List[float]] = {}
        self.notifications: List[Notification] = []
        self.toasts_disabled = False
        #: When ``notification.show`` answered ``disabled``: the latch time and the ``[ui.toast] delivery``
        #: read then. HP-14 (2026-09-05): the latch was permanent, so ``herdr server reload-config`` that
        #: turned toasts back on was never noticed; now a changed delivery or 60 s lifts it for one probe.
        self.toasts_disabled_ms: Optional[float] = None
        self.toasts_disabled_delivery: Optional[str] = None
        self.next_toast_ms = 0.0
        #: When the asks phase may next look; also the popup back-off.
        self.ask_next_ms: Optional[float] = None
        self.who_dirty = True
        self.last_who_ms: Optional[float] = None
        self.last_heartbeat_ms: Optional[float] = None
        self.last_ping_at: Optional[str] = None
        self.connected_ms: Optional[float] = None
        self.reconcile_due = True
        self.console_check_due = False  # a pane.closed event: is the console's terminal still listed?
        self.console_reconcile_at_ms: Optional[float] = None  # deferred RT-05 pass after a connect
        self.console_reconcile_attempts = 0
        self.last_reconcile_ms: Optional[float] = None
        self.last_registry_ms: Optional[float] = None
        self.last_version_ms: Optional[float] = None
        self.last_jobs_ms: Optional[float] = None
        self.last_teams_scan_ms: Optional[float] = None
        self.ping_failures = 0
        self.counters: Dict[str, int] = {"events": 0, "polls": 0, "reconnects": 0, "nudges": 0, "toasts": 0, "wrong_target": 0, "jobs": 0, "unverified_skipped": 0, "phase_errors": 0, "says": 0}
        self.iterations = 0
        self.max_iterations: Optional[int] = None
        self.cli_path = os.fspath(plugin_root() / "bin" / "herdr-synapse")
        self._signals_installed = False
        self._previous_signals: Dict[int, Any] = {}
        #: An ``agent.prompt`` whose wait returns faster than this was already inside a turn (plan 8.3).
        self.landed_fast_ms = LANDED_FAST_MS
        #: Tests may append a callable here to capture log lines.
        self.log_sinks: List[Callable[[str], None]] = []
        #: Set False to keep log lines out of stderr (tests with a sink).
        self.log_stderr = True

    # -- logging ---------------------------------------------------------------

    def log(self, message: str) -> None:
        for sink in self.log_sinks:
            try:
                sink(message)
            except Exception:  # noqa: BLE001 - a test sink must not break the loop
                pass
        if not self.log_stderr:
            return
        try:
            sys.stderr.write("{} [{}] {}\n".format(now_iso(), os.getpid(), message))
            sys.stderr.flush()
        except (OSError, ValueError):
            pass

    def now_ms(self) -> float:
        return _now_ms(self.clock)

    # -- daemon.json -------------------------------------------------------------

    def info(self) -> DaemonInfo:
        return DaemonInfo(
            pid=os.getpid(),
            start_time=self.start_time,
            beat_at=now_iso(),
            socket=os.fspath(self.layout.socket),
            socket_inode=self.socket_inode,
            version=VERSION,
            herdr_version=self.server_version,
            protocol=self.server_protocol,
            manifest_version=self.manifest_version,
            last_ping_at=self.last_ping_at,
            started_at=self.started_at,
            identity_env_unset=_identity_env_unset() and not any(name in self.env for name in IDENTITY_ENV_VARS),
            capabilities=capabilities.capability_map(self.atomic_idle_prompt),
        )

    def write_info(self, started: bool = False) -> DaemonInfo:
        info = self.info()
        write_daemon_info(self.session, info)
        return info

    # -- server connection ---------------------------------------------------------

    def connect_server(self) -> Dict[str, Any]:
        """Ping, record version and protocol, refuse an unsupported major.minor without ``allow_version``."""
        pong = self.api.ping(timeout=5.0)
        version = str(pong.get("version") or "")
        protocol = pong.get("protocol")
        self.server_version = version or None
        self.server_protocol = int(protocol) if isinstance(protocol, int) else None
        self.last_ping_at = now_iso()
        self.ping_failures = 0
        self.reconnect_requested = False
        major_minor = ".".join(version.split(".")[:2])
        if version and major_minor not in SUPPORTED_HERDR_MAJOR_MINORS and not self.allow_version:
            supported = ", ".join("{}.x".format(item) for item in SUPPORTED_HERDR_MAJOR_MINORS)
            raise HerdrTeamError(
                "herdr_version_mismatch",
                "Herdr {} is not supported ({}); pass --allow-version to run anyway".format(version, supported),
                EXIT_REFUSED,
                {"herdr_version": version, "supported": list(SUPPORTED_HERDR_MAJOR_MINORS)},
            )
        self.atomic_idle_prompt = capabilities.probe_atomic_idle_prompt(self.api)
        self.socket_inode = socket_inode(self.layout.socket)
        return pong

    def _install_signals(self) -> None:
        if self._signals_installed or threading.current_thread() is not threading.main_thread():
            return

        def _stop(signum: int, _frame: Any) -> None:
            self.request_stop("signal {}".format(signum))

        previous: Dict[int, Any] = {}
        try:
            for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                previous[signum] = signal.signal(signum, _stop)
        except (ValueError, OSError):
            self._restore_signals(previous)
            return
        self._previous_signals = previous
        self._signals_installed = True

    def _restore_signals(self, previous: Optional[Dict[int, Any]] = None) -> None:
        """Put back the handlers ``_install_signals`` replaced.

        The daemon owns its process, so this only matters when ``run`` is
        called in-process (tests): on Linux a forked child inherits Python-level
        handlers, and a leftover ``_stop`` would turn its SIGTERM into a no-op.
        """
        handlers = self._previous_signals if previous is None else previous
        for signum, handler in handlers.items():
            try:
                signal.signal(signum, signal.SIG_DFL if handler is None else handler)
            except (ValueError, OSError, TypeError):
                pass
        self._previous_signals = {}
        self._signals_installed = False

    def request_stop(self, reason: str) -> None:
        self.stop_requested = True
        if self.stop_reason is None:
            self.stop_reason = reason

    def _interruptible_sleep(self, seconds: float) -> None:
        deadline = self.clock() + seconds
        while not self.stop_requested:
            remaining = deadline - self.clock()
            if remaining <= 0:
                return
            self.sleep(min(0.1, remaining))

    # -- main loop --------------------------------------------------------------------

    def run(self) -> int:
        self._install_signals()
        self.log("run: socket {} inode {} dry_nudge={}".format(self.layout.socket, self.socket_inode, self.dry_nudge))
        try:
            while not self.stop_requested:
                if not self._connect_with_backoff():
                    break
                self._serve_subscription()
                if self.stop_requested:
                    break
                self.counters["reconnects"] += 1
                inode = socket_inode(self.layout.socket)
                if inode is not None and self.socket_inode is not None and inode != self.socket_inode:
                    self.log("subscription ended and the socket inode changed ({} -> {}): server replaced".format(self.socket_inode, inode))
                else:
                    self.log("subscription ended; reconnecting")
        finally:
            try:
                self._shutdown()
            finally:
                self._restore_signals()
        return self.exit_code

    def _connect_with_backoff(self) -> bool:
        """Ping until the server answers; give up after ``give_up_s`` of failures."""
        first_failure: Optional[float] = None
        attempt = 0
        while not self.stop_requested:
            try:
                self.connect_server()
                return True
            except HerdrTeamError as err:
                if err.code == "herdr_version_mismatch":
                    self.log("exiting: {}".format(err))
                    self.exit_code = EXIT_REFUSED
                    self.request_stop("version mismatch")
                    return False
                now = self.clock()
                if first_failure is None:
                    first_failure = now
                if now - first_failure >= self.give_up_s:
                    self.log("server unreachable for {:g}s; exiting and releasing the lock".format(self.give_up_s))
                    self.exit_code = EXIT_UNREACHABLE
                    self.request_stop("server unreachable")
                    return False
                if not self._session_dir_present():
                    self.log("session state dir removed; exiting and releasing the lock")
                    self.exit_code = EXIT_UNREACHABLE
                    self.request_stop("session dir removed")
                    return False
                delay = self.backoff[min(attempt, len(self.backoff) - 1)]
                attempt += 1
                self.log("ping failed ({}); retrying in {:g}s".format(err.code, delay))
                self.write_info()
                self._interruptible_sleep(delay)
        return False

    def _session_dir_present(self) -> bool:
        """False once ``<session>/`` was removed under us (teardown, gc, a test's temp dir).

        ``write_info`` would otherwise recreate the directory just to drop a
        ``daemon.json`` nobody reads; the right move is to stop.
        """
        try:
            return os.path.isdir(self.session.root)
        except OSError:
            return False

    def _serve_subscription(self) -> None:
        """One subscription lifetime: cold start, then events and ticks until EOF or stop."""
        try:
            stream = self.api.subscribe(list(SUBSCRIPTIONS), tick_timeout=self.tick_s, connect_timeout=5.0)
        except HerdrTeamError as err:
            self.log("subscribe failed: {}".format(err))
            self._interruptible_sleep(self.backoff[0])
            return
        try:
            self.on_connected()
            while not self.stop_requested:
                try:
                    event = next(stream)
                except StopIteration:
                    return
                except HerdrTeamError as err:
                    self.log("subscription error: {}".format(err))
                    return
                if event is not None:
                    self._phase("event", lambda: self.handle_event(event))
                self.tick()
                self.iterations += 1
                if self.max_iterations is not None and self.iterations >= self.max_iterations:
                    self.request_stop("max iterations")
                if self.reconnect_requested:
                    # Plan 8.1: three failed pings end the subscription; ``run`` reconnects with backoff.
                    self.log("{} failed API calls in a row; closing the subscription to reconnect".format(self.ping_failures))
                    return
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def on_connected(self) -> None:
        """Every (re)connect is a cold start: drop caches, reconcile, restart the grace window."""
        now = self.now_ms()
        self.agents = {}
        self.agents_by_pane = {}
        self.panes_by_terminal = {}
        self._panes_fetched_ms = None
        self.stability = {}
        self.reconnect_requested = False
        self.last_agent_list_ms = None
        self.connected_ms = now
        self.console_reconcile_at_ms = self.now_ms() + CONSOLE_RECONCILE_DELAY_S * 1000.0
        self.console_reconcile_attempts = 0
        self.reconcile_due = True
        self.last_reconcile_ms = None
        self.socket_inode = socket_inode(self.layout.socket)
        # The same boundary as ``tick`` (plan 10, F-07): a ``board_locked`` from a rebind while an
        # ``add``/``rename`` holds ``team.lock``, or one bad roster file, must not unwind ``run`` at
        # connect time. A failed reconcile is retried by the 10 s reconcile poll of the grace window.
        self._phase("scan_teams", lambda: self.scan_teams(force=True))
        self._phase("poll_agents", lambda: self.poll_agents(force=True))
        self._phase("reconcile", self.reconcile)
        self.write_info()
        self.who_dirty = True

    def _shutdown(self) -> None:
        self.log("stopping: {}".format(self.stop_reason or "unknown"))
        if self._session_dir_present():
            try:
                self.write_info()
            except Exception as err:  # noqa: BLE001
                self.log("final daemon.json write failed: {}".format(err))
        else:
            self.log("session state dir removed; skipping the final daemon.json")
        if self.lock is not None:
            try:
                self.lock.release()
            except OSError:
                pass
            self.lock = None

    # -- events ---------------------------------------------------------------------

    def handle_event(self, envelope: Dict[str, Any]) -> None:
        """Global events only wake the evaluator; every decision re-reads the API."""
        self.counters["events"] += 1
        kind = str(envelope.get("event") or "")
        data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
        if kind in ("pane_agent_detected", "pane_closed", "pane_exited", "pane_moved"):
            self.reconcile_due = True
            self.last_agent_list_ms = None  # poll on the next tick
            if kind in ("pane_closed", "pane_exited"):
                self.console_check_due = True  # the event has no terminal id; the tick asks pane.list (cmd_ui.mark_console_closed_if_pane_gone)
                pane_id = data.get("pane_id")
                if isinstance(pane_id, str):
                    self._mark_pane_gone(pane_id, kind)
        elif kind == "pane_focused":
            now = self.now_ms()
            for note in self.notifications:
                if note.reason == "no_foreground_client":
                    note.next_ms = now
        elif kind == "pane_updated":
            pane = data.get("pane") if isinstance(data.get("pane"), dict) else None
            if pane and isinstance(pane.get("terminal_id"), str):
                cached = self.agents.get(pane["terminal_id"])
                if cached is not None:
                    for key in ("agent_status", "focused", "tokens", "terminal_title_stripped", "agent"):
                        if key in pane:
                            cached[key] = pane[key]
                    self.who_dirty = True

    def _mark_pane_gone(self, pane_id: str, why: str) -> None:
        for team in self.teams.values():
            for member in team.members():
                if member.get("pane_id") == pane_id and member.get("terminal_id") and member.get("status") in ("active", "starting"):
                    name = str(member.get("name"))
                    if self._restarting(team, name):
                        continue
                    self.log("{}: member {} pane {} {}".format(team.name, name, pane_id, why))
                    # Plan 5.3: the three tokens are cleared on agent exit, including a pane that no longer hosts one.
                    self._set_member_status(team, name, "missing", clear_tokens=True)

    def _reconcile_console_after_connect(self) -> None:
        """RT-05: close the dead ``Team console`` shell a cold restart left behind, reopen when ``open:true``.

        The startup hook does the same pass but runs before restored panes
        have spawned their shells (``pane.process_info`` foreground empty), so
        the dead shell survives it; this pass runs ``CONSOLE_RECONCILE_DELAY_S``
        after connect and repeats while ``reconcile_console`` reports an
        unresolved labelled pane, up to ``CONSOLE_RECONCILE_ATTEMPTS`` times.
        """
        from herdr_team import cmd_ui as _cmd_ui

        self.console_reconcile_attempts += 1
        result = _cmd_ui.reconcile_console(self.layout, self.api, dict(self.env), reopen=True)
        for pane_id in result.get("closed") or []:
            self.log("closed dead console shell {} left by a restart".format(pane_id))
        if result.get("reopened"):
            self.log("reopened the console ({}): console.json says open".format(result["reopened"]))
        if result.get("unresolved") and self.console_reconcile_attempts < CONSOLE_RECONCILE_ATTEMPTS:
            self.console_reconcile_at_ms = self.now_ms() + CONSOLE_RECONCILE_DELAY_S * 1000.0
        else:
            self.console_reconcile_at_ms = None

    def _check_console_pane(self) -> None:
        """UI-05: a console pane closed while the server is up is recorded ``open:false`` so it is not reopened."""
        self.console_check_due = False
        from herdr_team import cmd_ui as _cmd_ui

        if _cmd_ui.mark_console_closed_if_pane_gone(self.session, self.api):
            self.log("console pane closed while the server is up; console.json open:false")

    # -- tick --------------------------------------------------------------------------

    def _phase(self, name: str, fn: Callable[[], None]) -> None:
        """Run one tick phase or event; any ``Exception`` is logged and survived (plan 10, F-07).

        ``HerdrTeamError`` covers the API and store (a held ``team.lock`` is
        ``board_locked``), ``ValueError`` a bad job or roster field reaching
        ``nudge``, ``OSError`` a full disk; the rest is a bug in one phase.
        The daemon is the sole deliverer for every team in the session, so
        a single bad file or event must never unwind ``run``. Only
        ``SystemExit`` and ``KeyboardInterrupt`` pass (``BaseException``).
        """
        try:
            fn()
        except Exception as err:  # noqa: BLE001 - F-07: the daemon must outlive one bad file/event
            self.counters["phase_errors"] += 1
            self.log("tick phase {} failed: {}: {}".format(name, type(err).__name__, err))

    def tick(self) -> None:
        now = self.now_ms()
        if self.ping_failures >= PING_FAILURES_BEFORE_RECONNECT and not self.reconnect_requested:
            self.reconnect_requested = True
            return
        self._phase("scan_teams", self.scan_teams)
        self._phase("watch_version", lambda: self.watch_version(now))
        self._phase("poll_registry", lambda: self.poll_registry(now))
        if self.stop_requested:
            return
        self._phase("poll_agents", self.poll_agents)
        self._phase("confirm_says", lambda: self.confirm_says(now))
        if self.console_check_due:
            self._phase("console", self._check_console_pane)
        if self.console_reconcile_at_ms is not None and now >= self.console_reconcile_at_ms:
            self._phase("console_reconcile", self._reconcile_console_after_connect)
        if self.reconcile_due or self._reconcile_poll_due(now):
            self._phase("reconcile", self.reconcile)
        self._phase("tail_boards", self.tail_boards)
        self._phase("consume_jobs", lambda: self.consume_jobs(now))
        self._phase("restarts", lambda: self.advance_restarts(now))
        # After the tail, so a post ingested this tick is already counted, and
        # before the evaluator, so a swept pending is acted on in the same pass.
        self._phase("poll_context", lambda: self.poll_all_context(now))
        self._phase("asks", lambda: self.raise_asks(now))
        self._phase("sweep_unread", lambda: self.sweep_all_unread(now))
        self._phase("link_receipts", lambda: self.poll_link_receipts(now))
        self._phase("board_snapshot", lambda: self.snapshot_all_boards(now))
        self._phase("evaluate_pending", self.evaluate_pending)
        self._phase("heartbeat", lambda: self.heartbeat_if_due(now))
        self._phase("notifications", self.process_notifications)
        self._phase("who", lambda: self.write_who_if_due(now))

    def _reconcile_poll_due(self, now: float) -> bool:
        if self.connected_ms is None or now - self.connected_ms > RECONCILE_POLL_BOUND_S * 1000.0:
            return False
        return self.last_reconcile_ms is None or now - self.last_reconcile_ms >= RECONCILE_POLL_S * 1000.0

    # -- version and registry watch ---------------------------------------------------------

    def watch_version(self, now: float) -> None:
        if self.last_version_ms is not None and now - self.last_version_ms < self.version_watch_s * 1000.0:
            return
        self.last_version_ms = now
        current = read_manifest_version()
        if current is None:
            return  # unparseable or missing: ignore
        if current == self.manifest_version:
            self.manifest_seen = None
            return
        if self.manifest_seen == current:
            self.log("manifest version changed {} -> {} (two identical reads); exiting after the current delivery".format(self.manifest_version, current))
            self.request_stop("manifest version changed")
        else:
            self.manifest_seen = current

    def poll_registry(self, now: float) -> None:
        if self.last_registry_ms is None:
            self.last_registry_ms = now  # first poll one interval after start
            return
        if now - self.last_registry_ms < self.registry_poll_s * 1000.0:
            return
        self.last_registry_ms = now
        try:
            result = self.api.request("plugin.list", {}, timeout=5.0)
        except HerdrTeamError as err:
            self.log("plugin.list failed ({}); keeping state".format(err.code))
            return
        plugins = result.get("plugins") if isinstance(result, dict) else None
        if not isinstance(plugins, list):
            return
        entry = None
        for plugin in plugins:
            if isinstance(plugin, dict) and plugin.get("plugin_id") == PLUGIN_ID:
                entry = plugin
                break
        if entry is None or not entry.get("enabled", True):
            self.log("plugin {} {}; clearing tokens and view, exiting".format(PLUGIN_ID, "missing from the registry" if entry is None else "disabled"))
            self.teardown_projections()
            self.request_stop("plugin disabled")

    def teardown_projections(self) -> None:
        for team in self.teams.values():
            for member in team.members():
                pane_id = member.get("pane_id")
                if isinstance(pane_id, str) and member.get("terminal_id"):
                    self._clear_tokens(pane_id)
        try:
            self.api.request("agent.view.clear", {"source": "plugin:" + PLUGIN_ID}, timeout=5.0)
        except HerdrTeamError:
            pass

    # -- teams ----------------------------------------------------------------------

    def scan_teams(self, force: bool = False) -> None:
        now = self.now_ms()
        if not force and self.last_teams_scan_ms is not None and now - self.last_teams_scan_ms < 2000.0:
            return
        self.last_teams_scan_ms = now
        names = set(self.session.list_teams())
        for name in list(self.teams):
            if name not in names:
                self.log("team {} gone; dropping state".format(name))
                del self.teams[name]
                self.who_dirty = True
        for name in sorted(names):
            if name in self.teams:
                self._reload_roster(self.teams[name])
                continue
            paths = self.session.team(name)
            ensure_team_dirs(paths)
            team = TeamState(name, paths, Ledger(paths))
            self._reload_roster(team)
            self._load_tail_state(team)
            self._replay_ledger(team)
            self.teams[name] = team
            self.who_dirty = True
            self.log("team {} loaded: {} members, watermark {}".format(name, len(team.members()), team.watermark))

    def _reload_roster(self, team: TeamState) -> None:
        doc = _load_roster_doc(team.paths)
        if doc is not None:
            team.roster = doc
            self._reload_gate_config(team)
            self._assign_color_slot(team)
            self._refresh_workdir(team)
            self._watch_artifacts(team)

    def _watch_artifacts(self, team: TeamState) -> None:  # noqa: C901 - one linear gate sequence
        """Post one board record when the team's ``artifacts/`` tree changes.

        This is what makes the folder a shared surface rather than a drop box:
        a file any member writes, or the operator drops in by hand, becomes
        something the whole team can see. The record is a ``system`` record
        addressed to everyone, so it is picked up on the next board read (and
        by Claude on its next prompt) without waking anyone mid-turn.

        The first scan after the daemon sees a folder only seeds the
        fingerprint. Without that a restart would re-announce every file.
        """
        project = _workdir.project_dir_of(team.roster)
        if not project or not _artifacts_watch_enabled(team.roster):
            team.artifacts_seen = None
            team.artifacts_announced = None
            team.artifacts_pending_since_ms = None
            return
        now = self.now_ms()
        if team.artifacts_scanned_ms is not None and now - team.artifacts_scanned_ms < ARTIFACTS_POLL_S * 1000.0:
            return
        team.artifacts_scanned_ms = now
        try:
            current = _workdir.fingerprint_artifacts(project, team.name)
        except OSError as err:
            self.log("{}: cannot read the team artifacts: {}".format(team.name, err))
            return
        previous_scan = team.artifacts_seen
        team.artifacts_seen = current
        if previous_scan is None or team.artifacts_announced is None:
            # First sight of this folder: seed both baselines, announce nothing,
            # so a daemon restart never re-announces a folder full of files.
            team.artifacts_announced = current
            team.artifacts_pending_since_ms = None
            return

        # The diff is against what the board was last told, not the last scan,
        # so deferring an announcement can never lose a change.
        diff = _workdir.diff_artifacts(team.artifacts_announced, current)
        line = _workdir.describe_change(team.name, diff)
        if line is None:
            team.artifacts_pending_since_ms = None
            return
        if team.artifacts_pending_since_ms is None:
            team.artifacts_pending_since_ms = now

        settled = current == previous_scan
        waited_ms = now - team.artifacts_pending_since_ms
        if not settled and waited_ms < ARTIFACTS_MAX_WAIT_S * 1000.0:
            # A tree still being written: wait for it, so one dump is one record.
            return
        if team.artifacts_posted_ms is not None and now - team.artifacts_posted_ms < ARTIFACTS_MIN_INTERVAL_S * 1000.0:
            return

        self.log("{}: {}".format(team.name, line))
        self._append_system(team, "artifacts_changed", line, ["all"], extra={
            "added": len(diff.get("added") or []), "changed": len(diff.get("changed") or []),
            "removed": len(diff.get("removed") or []),
            "root": "{}/{}/artifacts/".format(_workdir.DIR_NAME, team.name),
        })
        team.artifacts_announced = current
        team.artifacts_pending_since_ms = None
        team.artifacts_posted_ms = now

    def snapshot_board(self, team: TeamState, now: float) -> None:
        """Keep ``<project>/.herdr-synapse/<team>/board.md`` current.

        The board is the team's record of what happened and it lived only in
        the plugin's state dir. This mirrors it beside the team's rules and
        per-member instructions, so the folder carries the conversation too.
        Written only when the board actually moved, and at most once per
        ``BOARD_SNAPSHOT_MIN_INTERVAL_S``: it is the whole archive, and a busy
        team moves the watermark many times a minute.
        """
        if not _workdir.project_dir_of(team.roster):
            return
        watermark = team.watermark
        if team.snapshot_seq == watermark:
            return
        if team.snapshot_ms is not None and now - team.snapshot_ms < BOARD_SNAPSHOT_MIN_INTERVAL_S * 1000.0:
            return
        try:
            result = _workdir.render_board_snapshot(self.layout, team.name)
        except Exception as err:  # noqa: BLE001 - F-07: the daemon must outlive one bad file
            self.log("{}: board snapshot failed: {}: {}".format(team.name, type(err).__name__, err))
            team.snapshot_ms = now
            return
        team.snapshot_ms = now
        if result.get("reason"):
            self.log("{}: board snapshot skipped: {}".format(team.name, result["reason"]))
            return
        team.snapshot_seq = watermark
        if result.get("written"):
            self.log("{}: board snapshot updated ({} posts)".format(team.name, result.get("records")))

    def snapshot_all_boards(self, now: float) -> None:
        for name, team in list(self.teams.items()):
            try:
                self.snapshot_board(team, now)
            except Exception as err:  # noqa: BLE001
                self.log("{}: board snapshot failed: {}: {}".format(name, type(err).__name__, err))

    def _refresh_workdir(self, team: TeamState) -> None:
        """Keep the project mirror current after a roster change.

        Only ever runs once a human has set ``project_dir``, and never fails a
        tick: the folder is a convenience, and the checkout may be read-only,
        on a full disk, or hold a file somebody else wrote.
        """
        if not _workdir.project_dir_of(team.roster):
            return
        try:
            result = _workdir.render(self.layout, team.name)
        except Exception as err:  # noqa: BLE001 - F-07: the daemon must outlive one bad file
            self.log("{}: team folder not refreshed: {}: {}".format(team.name, type(err).__name__, err))
            return
        for path in result.get("skipped") or []:
            self.log("{}: team folder left {} alone; it is not ours".format(team.name, path))
        self._announce_move(team, result.get("moved"))
        self._announce_edits(team, result.get("awaiting_adopt") or [])

    def _announce_move(self, team: TeamState, moved: Optional[Dict[str, Any]]) -> None:
        """Tell every member the team folder is somewhere else now.

        Each member is named as well as ``all``: a record addressed only to the
        team is a broadcast the gate holds, and every agent here is carrying the
        old path in its context, so this is the one thing each of them has to
        be nudged about rather than told eventually.
        """
        if not isinstance(moved, dict) or not moved.get("to"):
            return
        names = [str(m.get("name")) for m in team.members()
                 if m.get("kind") != "human" and m.get("status") in ("active", "starting") and m.get("name")]
        self.log("{}: team folder moved {} -> {}".format(team.name, moved.get("from"), moved["to"]))
        self._append_system(
            team, "workdir_moved",
            "The team folder moved to {}. Its knowledge base is now {}/{}/knowledge.md and your document is "
            "{}/{}/members/<you>.md; artifacts are {}/{}/artifacts/. The old path is gone: use these, or run "
            "herdr-synapse me, which prints them.".format(
                moved["to"], moved["to"], team.name, moved["to"], team.name, moved["to"], team.name),
            names + ["all"], {"moved_from": moved.get("from"), "moved_to": moved["to"]},
        )

    def _announce_edits(self, team: TeamState, paths_: Sequence[str]) -> None:
        """Tell the operator once about each member document edited in the checkout.

        The edit is kept, not imported: the project folder is writable by the
        agents themselves, so ``instructions --adopt`` is what makes an edit
        the operator's word. Keyed by content digest, so re-rendering the
        mirror does not re-announce, and a second edit does.
        """
        live = set()
        for path in paths_:
            live.add(path)
            try:
                current = _workdir.digest(Path(path).read_text(encoding="utf-8"))
            except OSError:
                continue
            if team.adopt_announced.get(path) == current:
                continue
            team.adopt_announced[path] = current
            name = Path(path).stem
            self.log("{}: {} was edited; waiting for adopt".format(team.name, path))
            self._append_system(
                team, "instructions_edited",
                "{} was edited. Review and apply it: herdr-synapse instructions {} --adopt".format(path, name),
                ["human"], {"member": name, "path": path},
            )
        for path in [p for p in team.adopt_announced if p not in live]:
            del team.adopt_announced[path]  # adopted or discarded; the next edit is news again

    def _assign_color_slot(self, team: TeamState) -> None:
        """Give a team the lowest sidebar colour slot no other team holds, once, and persist it.

        The daemon is the single writer so two teams cannot race for one slot. A team created
        while the daemon is down simply has no colour until this runs, the same grace the other
        metadata tokens already have.
        """
        if roster.color_slot_of(team.roster) is not None:
            return
        taken = [roster.color_slot_of(other.roster) for other in self.teams.values() if other is not team]
        slot = roster.free_color_slot(taken, len(self.teams))

        def mutate(doc: Dict[str, Any]) -> None:
            config = doc.get("config")
            if not isinstance(config, dict):
                config = {}
                doc["config"] = config
            config.setdefault("color_slot", slot)

        try:
            doc = update_roster(team.paths, mutate)
        except HerdrTeamError as err:
            # Cosmetic: a busy roster lock must never fail the scan phase. The next scan retries.
            self.log("{}: sidebar colour slot deferred ({})".format(team.name, err.code))
            return
        if doc is not None:
            team.roster = doc
            self.log("{}: sidebar colour slot {} ({})".format(team.name, slot, roster.TEAM_COLOR_NAMES[slot - 1]))

    def _reload_gate_config(self, team: TeamState) -> None:
        """Rebuild ``team.gate_config`` from ``config.gate`` when the mapping changed; log once per change."""
        raw = team.roster.get("config")
        overrides = raw.get("gate") if isinstance(raw, dict) else None
        if team.gate_loaded and overrides == team.gate_overrides:
            return
        team.gate_loaded = True
        team.gate_overrides = overrides
        config, applied, error, skipped = gate_config_from_roster_detailed(team.roster)
        if error is not None:
            self.log("{}: config.gate ignored ({}); using the default gate".format(team.name, error))
        elif applied:
            self.log("{}: gate config {}".format(team.name, json.dumps(applied, sort_keys=True)))
        if skipped:
            self.log("{}: config.gate: ignoring unknown key(s) {}; the rest applies".format(team.name, ", ".join(skipped)))
        team.gate_config = config

    def _load_tail_state(self, team: TeamState) -> None:
        """``store.BoardTailer`` resumes from ``notifier/state.json``, else from the lowest member cursor."""
        if team.tailer is not None:
            team.tailer.close()
        team.delivery_terminal = team.ledger.terminal_seqs()
        cursors = [read_cursor_seq(team.paths, str(m.get("name"))) for m in team.members() if m.get("terminal_id")]
        team.tailer = store.BoardTailer(team.paths, start_seq=min(cursors) if cursors else 0)
        team.watermark = team.tailer.watermark_seq
        self._seed_open_asks(team)
        self._seed_link_inbox(team)
        self._rebuild_pending(team)

    def _seed_open_asks(self, team: TeamState) -> None:
        """One bounded board read at start; ``_track_ask`` carries it from there."""
        from herdr_team import asks as _asks

        try:
            team.open_asks = {int(r["seq"]): r for r in _asks.pending(team.paths) if isinstance(r.get("seq"), int)}
        except (HerdrTeamError, OSError, ValueError):
            team.open_asks = {}

    def _rebuild_pending(self, team: TeamState) -> None:
        """Cold start: unread posts the previous daemon had already tailed become pending again.

        ``notifier/state.json`` resumes the tail *after* the last ingested seq,
        so work the old process held only in memory (a post inside its stable
        window, a landed-but-unread nudge) would never be nudged again (M5
        ND-12: a post made 0.2 s before SIGTERM was lost). Records the ledger
        shows as landed keep that landing, so the 2/5/10 min re-nudge schedule
        applies instead of an immediate duplicate. Human toasts are not
        replayed: the attention mirror already has them.
        """
        now = self.now_ms()
        cursors: Dict[str, Tuple[int, Set[int]]] = {}
        for member in team.members():
            if member.get("kind") != "human" and member.get("terminal_id"):
                cursors[str(member["name"])] = read_cursor_state(team.paths, str(member["name"]))
        if not cursors or team.watermark <= 0:
            return
        floor = min(c[0] for c in cursors.values())
        if floor >= team.watermark:
            return
        try:
            records = store.BoardStore(team.paths).read(since_seq=floor, include_retracted=True)
        except HerdrTeamError as err:
            self.log("{}: pending rebuild skipped: {}".format(team.name, err))
            return
        records = [r for r in records if isinstance(r.get("seq"), int) and r["seq"] <= team.watermark]
        retracted = {r["retracts"] for r in records if isinstance(r.get("retracts"), int)}
        team.retracted.update(retracted)
        added: Dict[str, List[int]] = {}
        for rec in records:
            seq = int(rec["seq"])
            if seq in retracted or rec.get("kind") == "retract" or isinstance(rec.get("retracts"), int) or store.is_direct_line(rec) or not self._counts_for_nudges(rec):
                continue
            author = str(rec.get("from"))
            urgent = bool(rec.get("urgent"))
            targets: List[str] = []
            if author == "system":
                if rec.get("event") in URGENT_SYSTEM_EVENTS and urgent:
                    newcomer = rec.get("member") if rec.get("event") == "member_joined" else None
                    targets = [n for n in cursors if n != newcomer]
            else:
                for target in rec.get("to", []) or []:
                    if not isinstance(target, str) or target == author or target == "human":
                        continue
                    if target == "all":
                        if urgent or author == "human":
                            targets.extend(cursors)
                        continue
                    recipient = team.member(target) or team.member_by_retired_name(target)
                    if recipient is not None:
                        targets.append(str(recipient.get("name")))
            for name in targets:
                if name == author or name not in cursors:
                    continue
                cursor, seen = cursors[name]
                if seq <= cursor or seq in seen or seq in team.delivery_terminal.get(name, set()):
                    continue
                self._add_pending(team, name, seq, urgent, author, now, interrupt=bool(rec.get("interrupt")))
                added.setdefault(name, []).append(seq)
        if not added:
            return
        landed_results = (RESULT_LANDED_WORKING, RESULT_DRY)
        attempts = [a for a in team.ledger.attempts().values() if a.get("result") in landed_results and a.get("member") in added]
        for name, seqs in added.items():
            pending = team.pending.get(name)
            if pending is None or pending.kind != "nudge":
                continue
            hits = [a for a in attempts if a.get("member") == name and any(s in (a.get("seqs") or []) for s in seqs)]
            if hits:
                last = hits[-1]
                landed_at = _parse_iso(last.get("result_ts") or last.get("ts"))
                age_ms = max(0.0, (time.time() - landed_at) * 1000.0) if landed_at is not None else 0.0
                pending.landed_ms = now - age_ms
                pending.landed_seq_max = max([int(s) for s in (last.get("seqs") or []) if isinstance(s, int)] or [0])
                pending.attempts = max(1, int(last.get("attempts") or 1))
                pending.attempt_id = str(last.get("id"))
                if pending.attempts == 1 and any(s > pending.landed_seq_max for s in seqs):
                    pending.follow_up_due = True  # posts arrived after that landing: the one follow-up still applies
                self.log("{}: rebuilt pending for {}: {} (nudged {:.0f} s ago as {}, unread)".format(team.name, name, sorted(seqs), age_ms / 1000.0, last.get("id")))
            else:
                self.log("{}: rebuilt pending for {}: {} (never nudged)".format(team.name, name, sorted(seqs)))
        self.who_dirty = True

    def _replay_ledger(self, team: TeamState) -> None:
        for entry in team.ledger.open_intents():
            member = entry.get("member")
            if isinstance(member, str):
                team.open_intents[member] = entry
        if team.open_intents:
            self.log("team {}: {} open intents count as sent".format(team.name, len(team.open_intents)))

    # -- agents ---------------------------------------------------------------------

    def _has_pending(self) -> bool:
        for team in self.teams.values():
            if team.say_inflight:
                return True  # a typed line waits for the next poll to confirm it landed
            for name, pending in team.pending.items():
                member = team.member(name)
                if member is not None and member.get("terminal_id") and pending.landed_ms is None:
                    return True
        return False

    def poll_agents(self, force: bool = False) -> None:
        now = self.now_ms()
        interval = (POLL_PENDING_S if self._has_pending() else POLL_IDLE_S) * 1000.0
        if not force and self.last_agent_list_ms is not None and now - self.last_agent_list_ms < interval:
            return
        try:
            result = self.api.request("agent.list", {}, timeout=5.0)
        except HerdrTeamError as err:
            self.ping_failures += 1
            self.log("agent.list failed: {}".format(err.code))
            self.last_agent_list_ms = now
            return
        self.counters["polls"] += 1
        agents = result.get("agents") if isinstance(result, dict) else None
        if not isinstance(agents, list):
            return
        if self.last_agent_list_ms is not None and self.stability:
            self._reset_stability_after_gap(now - self.last_agent_list_ms)
        self.last_agent_list_ms = now
        fresh: Dict[str, Dict[str, Any]] = {}
        by_pane: Dict[str, Dict[str, Any]] = {}
        for agent in agents:
            if not isinstance(agent, dict) or not isinstance(agent.get("terminal_id"), str):
                continue
            fresh[agent["terminal_id"]] = agent
            if isinstance(agent.get("pane_id"), str):
                by_pane[agent["pane_id"]] = agent
            self._track_stability(agent, now)
        for terminal_id in list(self.stability):
            if terminal_id not in fresh:
                del self.stability[terminal_id]
        identity_changed = set(fresh) != set(self.agents) or any(fresh[t].get("name") != self.agents.get(t, {}).get("name") for t in fresh)
        # A new harness session on a terminal we already knew is a fresh agent wearing a member's
        # pane (crash and restart, ``/clear``, a resume by hand): reconcile now, not in 10 s.
        identity_changed = identity_changed or any(
            roster.session_key(roster.session_of(fresh[t])) != roster.session_key(roster.session_of(self.agents.get(t, {}))) for t in fresh
        )
        changed = identity_changed or any(fresh[t].get("agent_status") != self.agents.get(t, {}).get("agent_status") for t in fresh)
        self.agents = fresh
        self.agents_by_pane = by_pane
        if changed:
            self.who_dirty = True
        if identity_changed and self.last_agent_list_ms is not None:
            self.reconcile_due = True  # a new terminal or a changed name is a roster fact, not a status blip

    def _track_stability(self, agent: Dict[str, Any], now: float) -> None:
        terminal_id = agent["terminal_id"]
        seq = agent.get("state_change_seq")
        seq = int(seq) if isinstance(seq, int) else 0
        status = str(agent.get("agent_status") or "unknown")
        entry = self.stability.get(terminal_id)
        if entry is None:
            self.stability[terminal_id] = Stability(seq, now, status, now if status in ("idle", "done") else None, 0)
            return
        if seq != entry.state_change_seq or status != entry.status:
            if entry.status not in ("idle", "done") and status in ("idle", "done"):
                entry.idle_since_ms = now
            elif status not in ("idle", "done"):
                entry.idle_since_ms = None
                if entry.status in ("idle", "done") and status == "working":
                    entry.turns += 1
            entry.state_change_seq = seq
            entry.status = status
            entry.since_ms = now

    def _reset_stability_after_gap(self, gap_ms: float) -> None:
        """Plan 8.2 gate 6: a sample gap over ``sample_gap_reset_ms`` voids the stable window.

        The threshold is per team (``config.gate``): a roster terminal uses its
        team's value, every other terminal the default. Cheap when no gap can
        matter: nothing is walked unless the gap exceeds the smallest threshold.
        """
        floor = min([gate.DEFAULT_CONFIG.sample_gap_reset_ms] + [t.gate_config.sample_gap_reset_ms for t in self.teams.values()])
        if gap_ms <= floor:
            return
        thresholds: Dict[str, int] = {}
        for team in self.teams.values():
            for member in team.members():
                terminal_id = member.get("terminal_id")
                if isinstance(terminal_id, str) and terminal_id:
                    thresholds[terminal_id] = team.gate_config.sample_gap_reset_ms
        reset = [t for t in self.stability if gap_ms > thresholds.get(t, gate.DEFAULT_CONFIG.sample_gap_reset_ms)]
        if reset:
            self.log("sample gap of {:.0f} ms; resetting {} of {} stable window(s)".format(gap_ms, len(reset), len(self.stability)))
            for terminal_id in reset:
                del self.stability[terminal_id]

    def fresh_agent(self, target: str) -> Optional[Dict[str, Any]]:
        try:
            result = self.api.request("agent.get", {"target": target}, timeout=5.0)
        except HerdrTeamError as err:
            if err.code in ("server_not_running", "herdr_timeout"):
                self.ping_failures += 1
            return None
        agent = result.get("agent") if isinstance(result, dict) else None
        if isinstance(agent, dict) and isinstance(agent.get("terminal_id"), str):
            self.agents[agent["terminal_id"]] = agent
            if isinstance(agent.get("pane_id"), str):
                self.agents_by_pane[agent["pane_id"]] = agent
            self._track_stability(agent, self.now_ms())
            return agent
        return None

    # -- reconcile ---------------------------------------------------------------------

    def reconcile(self) -> None:
        """Rehydrate every team by the plan 4.2 order (``roster.rehydrate_match``); rewrite ``who.json``.

        One matcher for the daemon and the roster tests: (0) the harness
        session the member recorded, (a) ``terminal_id``, (b) a ``pane.list``
        row with the member's label and a live agent of its kind, (c)
        ``pane_id`` plus kind, (d) exact name, (e) a unique ``(kind, cwd,
        workspace)`` fingerprint, which stays owner-gated
        (``roster.AUTO_BIND_FINGERPRINT``): it is logged and waits for
        ``bind``. Rows claimed by another team's live member never bind here.
        A pass that had to fetch ``pane.list`` also drops pane records whose
        terminal is gone, so a restart does not leave one orphan per member.
        """
        now = self.now_ms()
        self.reconcile_due = False
        self.last_reconcile_ms = now
        in_grace = self.connected_ms is not None and now - self.connected_ms < GRACE_WINDOW_S * 1000.0
        self._panes_fetched_ms = None  # one pane.list per reconcile, fetched only when step (b) needs it
        for team in self.teams.values():
            self._reload_roster(team)
            members, by_name = self._rehydration_members(team)
            changes: List[Tuple[str, Dict[str, Any]]] = []
            if members:
                own_terminals = {m.terminal_id for m in members if m.terminal_id}
                rows = [a for a in self.agents.values() if a.get("terminal_id") in own_terminals or self._unclaimed(team, a)]
                live = {str(a.get("terminal_id")) for a in rows}
                # Step (b) needs pane.list only when some member's terminal is gone.
                panes = list(self._fetch_panes().values()) if any(m.terminal_id not in live for m in members) else []
                result = roster.rehydrate_match(members, rows, panes)
                for binding in result.bindings:
                    member = by_name.get(binding.member)
                    if member is None:
                        continue
                    team.rt(binding.member).last_seen_present_ms = now
                    if binding.agent.get("agent") is None:
                        # ``agent: null`` (launch pending, detection not yet run): the terminal is present
                        # but there is no evidence for or against the kind. Adopt ids only; the status
                        # (``starting`` from ``create --new``, or ``missing``) waits for a detected kind.
                        update = self._ids_update(team, member, binding.agent)
                    elif not binding.kind_matches:
                        update = self._kind_changed_update(team, member, binding.agent)
                    else:
                        update = self._rebind_update(team, member, binding.agent, binding.how)
                    if update:
                        update["last_seen_at"] = now_iso()
                        changes.append((binding.member, update))
                        if "terminal_id" in update or "pane_id" in update or update.get("status") == "active":
                            self._apply_label(team, member, str(binding.agent.get("pane_id")))
                            old_pane = member.get("pane_id")
                            if binding.how != roster.MATCH_TERMINAL and isinstance(old_pane, str) and update.get("pane_id") and update["pane_id"] != old_pane:
                                # RT-02: a rebind to another terminal leaves the old pane behind as a plain shell; it must not keep the team label.
                                self._clear_stale_label(team, member, old_pane)
                            self._stamp_tokens(team, dict(member, **update), self.clock())
                for entry in result.kind_changed:
                    member = by_name.get(str(entry.get("member")))
                    if member is None or not isinstance(entry.get("agent"), dict) or not entry["agent"].get("agent"):
                        continue  # the matcher never reports a null kind here; the guard mirrors the bindings loop
                    team.rt(str(entry["member"])).last_seen_present_ms = now
                    update = self._kind_changed_update(team, member, entry["agent"])
                    if update:
                        update["last_seen_at"] = now_iso()
                        changes.append((str(entry["member"]), update))
                for entry in result.unbound:
                    name = str(entry.get("member"))
                    candidate = entry.get("candidate") if isinstance(entry.get("candidate"), dict) else {}
                    rt = team.rt(name)
                    if rt.unbound_candidate != candidate.get("terminal_id"):
                        rt.unbound_candidate = candidate.get("terminal_id")
                        self.log("{}: {} matches only by fingerprint ({} on {}, cwd {}); not bound, waiting for `bind`".format(
                            team.name, name, candidate.get("terminal_id"), candidate.get("pane_id"), candidate.get("cwd")))
                    self._missing_update(team, name, by_name.get(name), in_grace, changes)
                for name in result.missing:
                    team.rt(name).unbound_candidate = None
                    self._missing_update(team, name, by_name.get(name), in_grace, changes)
            if changes:
                self._apply_changes(team, changes)
            self.who_dirty = True
        if self._panes_fetched_ms is not None:
            self._gc_pane_records()

    def _gc_pane_records(self) -> None:
        """Remove ``panes/<terminal_id>.json`` for terminals that are in neither ``pane.list`` nor any roster.

        Terminal ids are reallocated on every Herdr restart, so each restart
        orphaned one record per member; identity resolves through the record
        first, so a stale one could point a hook at a member that has moved.
        Only runs on a pass that fetched ``pane.list`` (no extra socket call).
        """
        try:
            entries = list(self.session.panes_dir.iterdir())
        except OSError:
            return
        held = {str(m.get("terminal_id")) for team in self.teams.values() for m in team.members() if m.get("terminal_id")}
        for entry in entries:
            if entry.suffix != ".json":
                continue
            terminal_id = entry.stem
            if terminal_id in self.panes_by_terminal or terminal_id in self.agents or terminal_id in held:
                continue
            try:
                entry.unlink()
                self.log("dropped pane record {} (terminal gone)".format(terminal_id))
            except OSError:
                continue

    def _rehydration_members(self, team: TeamState) -> Tuple[List[roster.Member], Dict[str, Dict[str, Any]]]:
        """Roster members as ``roster.Member`` (label derived for pre-label rosters) plus the raw dicts by name."""
        members: List[roster.Member] = []
        by_name: Dict[str, Dict[str, Any]] = {}
        for raw in team.members():
            name = raw.get("name")
            if not isinstance(name, str) or not name or raw.get("kind") == "human" or raw.get("status") == "left":
                continue
            try:
                member = roster.Member.from_json(raw)
            except HerdrTeamError as err:
                self.log("{}: member {!r} skipped during rehydration: {}".format(team.name, name, err.code))
                continue
            if member.label is None and member.role:
                member.label = roster.label_for(team.name, member.role)
            members.append(member)
            by_name[name] = raw
        return members, by_name

    def _kind_changed_update(self, team: TeamState, member: Dict[str, Any], match: Dict[str, Any]) -> Dict[str, Any]:
        """The member's terminal now hosts another kind: ``kind_changed`` once, tokens cleared."""
        update: Dict[str, Any] = {}
        if member.get("status") != "kind_changed":
            update["status"] = "kind_changed"
            self._clear_tokens(str(match.get("pane_id")))
            self.log("{}: {} now hosts {} (was {}); kind_changed".format(team.name, member.get("name"), match.get("agent"), member.get("kind")))
        return update

    @staticmethod
    def _live_ids(member: Dict[str, Any], match: Dict[str, Any]) -> Dict[str, Any]:
        """The live row's ids (and a first ``cwd``) that differ from the roster record."""
        update: Dict[str, Any] = {}
        for key in ("terminal_id", "pane_id", "workspace_id", "tab_id"):
            if match.get(key) and match.get(key) != member.get(key):
                update[key] = match.get(key)
        if match.get("cwd") and not member.get("cwd"):
            update["cwd"] = match.get("cwd")
        live_session = roster.session_of(match)
        if live_session is not None and not roster.same_session(member.get("session"), live_session):
            update["session"] = live_session
        return update

    def _ids_update(self, team: TeamState, member: Dict[str, Any], match: Dict[str, Any]) -> Dict[str, Any]:
        """A terminal match without a detected kind: adopt ids, keep the status, no name or generation change."""
        update = self._live_ids(member, match)
        if update:
            self.log("{}: {} present on {} ({}) with no detected kind yet; ids adopted, status {} kept".format(
                team.name, member.get("name"), match.get("terminal_id"), match.get("pane_id"), member.get("status")))
        return update

    def _rebind_update(self, team: TeamState, member: Dict[str, Any], match: Dict[str, Any], how: str) -> Dict[str, Any]:
        """Adopt the live row's ids, reactivate, bump the generation after an absence, re-apply or adopt the name."""
        name = str(member.get("name"))
        update = self._live_ids(member, match)
        if member.get("status") not in ("active",):
            update["status"] = "active"
            if member.get("status") in ("missing", "unbound", "starting"):
                update["generation"] = int(member.get("generation") or 1) + 1
        if "terminal_id" in update:
            self.log("{}: {} rebound by {} to {} ({})".format(team.name, name, how, update["terminal_id"], match.get("pane_id")))
            if how == roster.MATCH_SESSION and "generation" not in update:
                update["generation"] = int(member.get("generation") or 1) + 1  # moved panes with its memory intact
        if "session" in update and roster.session_key(member.get("session")) is not None:
            if roster.same_session_value(member.get("session"), update["session"]):
                # Same session id, new phase. The harness rewrote the session in
                # place instead of starting one: Claude reports ``source=compact``
                # against the same id once a ``/compact`` finishes. The process,
                # the pane and the agent's sense of who it is all survive, so this
                # is not a restart and takes no new generation. The briefing does
                # go again, because it was in the history that was just summarized.
                update["briefed_at"] = None
                self.log("{}: {} stayed in session {} and changed phase to {!r}; briefing again".format(
                    team.name, name, roster.short_session(update["session"]), roster.session_source(update["session"])))
            else:
                # A different harness session than the one recorded, on the member's own terminal (a
                # crash and restart, a Claude ``/clear``, the operator resuming something else there) or
                # on the pane a label or pane-id match found: the agent has no memory of who it is, so it
                # gets a new generation and a fresh briefing; the name and read position stay.
                if "generation" not in update:
                    update["generation"] = int(member.get("generation") or 1) + 1
                update["briefed_at"] = None
                self.log("{}: {} started a new session on {} ({} -> {}); re-briefing".format(
                    team.name, name, match.get("pane_id"), roster.short_session(member.get("session")), roster.short_session(update["session"])))
        live_name = match.get("name")
        if not match.get("launch_pending"):
            if live_name is None:
                self._apply_name(team, member, str(match.get("pane_id")))
            elif live_name != name and isinstance(live_name, str):
                update["name"] = live_name
                history = list(member.get("previous_names") or [])
                history.append({"name": name, "retired_at": now_iso()})
                update["previous_names"] = history
                self.log("{}: adopting rename {} -> {}".format(team.name, name, live_name))
                self._append_system(team, "renamed", "{} was renamed to {}".format(name, live_name), ["all"])
        return update

    def _missing_update(self, team: TeamState, name: str, member: Optional[Dict[str, Any]], in_grace: bool, changes: List[Tuple[str, Dict[str, Any]]]) -> None:
        """No live row for the member: ``missing`` after the grace window, tokens cleared (plan 5.3)."""
        if member is None or in_grace or member.get("status") not in ("active", "starting"):
            return
        if self._restarting(team, name):
            return  # on its way back with new flags; the restart bounds say when to give up
        changes.append((name, {"status": "missing"}))
        pane_id = member.get("pane_id")
        if isinstance(pane_id, str):
            self._clear_tokens(pane_id)

    def _fetch_panes(self) -> Dict[str, Dict[str, Any]]:
        """``pane.list`` rows by terminal id, cached for the current reconcile pass."""
        now = self.now_ms()
        if self._panes_fetched_ms is not None and now - self._panes_fetched_ms < 1000.0:
            return self.panes_by_terminal
        self._panes_fetched_ms = now
        try:
            result = self.api.request("pane.list", {}, timeout=5.0)
        except HerdrTeamError as err:
            if err.code in ("server_not_running", "herdr_timeout"):
                self.ping_failures += 1
            self.panes_by_terminal = {}
            return self.panes_by_terminal
        panes = result.get("panes") if isinstance(result, dict) else None
        out: Dict[str, Dict[str, Any]] = {}
        for pane in panes or []:
            if isinstance(pane, dict) and isinstance(pane.get("terminal_id"), str):
                out[pane["terminal_id"]] = pane
        self.panes_by_terminal = out
        return out

    def _unclaimed(self, team: TeamState, agent: Dict[str, Any]) -> bool:
        """Cross-team row filter: False when another team's live member holds the row's terminal."""
        terminal_id = agent.get("terminal_id")
        for other in self.teams.values():
            if other is team:
                continue
            for member in other.members():
                if member.get("terminal_id") == terminal_id and member.get("status") in ("active", "starting"):
                    return False
        return True

    def _apply_changes(self, team: TeamState, changes: List[Tuple[str, Dict[str, Any]]]) -> None:
        def mutate(doc: Dict[str, Any]) -> None:
            for name, update in changes:
                for member in doc.get("members", []):
                    if isinstance(member, dict) and member.get("name") == name:
                        member.update(update)
                        break

        before = {str(m.get("name")): m.get("session") for m in team.members() if isinstance(m, dict)}
        doc = update_roster(team.paths, mutate)
        if doc is not None:
            team.roster = doc
            for name, update in changes:
                self.log("{}: {} {}".format(team.name, name, json.dumps(update, ensure_ascii=False)))
                if self._restarting(team, name) and ("session" in update or "generation" in update):
                    # The same session id with a new phase is what a resumed
                    # agent reports -- the very shape the branch below reads as
                    # a compaction. The open restart says which it is.
                    self._note_restart_done(team, name, update, self.now_ms())
                    continue
                new_name = update.get("name")
                if isinstance(new_name, str) and new_name != name and roster.migrate_cursor(team.paths, name, new_name):
                    self.log("{}: carried {}'s read position to {}".format(team.name, name, new_name))
                if update.get("status") == "missing":
                    self._append_system(team, "member_gone", "{} is missing".format(name), ["human"])
                elif "generation" in update:
                    shown = update.get("name", name)
                    text = "{} restarted{} (generation {})".format(shown, " on {}".format(update["pane_id"]) if update.get("pane_id") else "", update["generation"])
                    extra: Dict[str, Any] = {}
                    if "session" in update:
                        extra["session"] = roster.short_session(update["session"])
                        extra["previous_session"] = roster.short_session(before.get(name))
                    if "briefed_at" in update and update["briefed_at"] is None:
                        text += "; new session, briefing again"
                    self._append_system(team, "member_restarted", text, ["all"], extra or None)
                    self._note_cleared(team, name, shown)
                    if "briefed_at" in update and update["briefed_at"] is None:
                        try:
                            roster.write_briefing_job(team.paths, str(shown))
                        except HerdrTeamError as err:
                            self.log("{}: could not enqueue a briefing for {}: {}".format(team.name, shown, err.code))
                elif "session" in update and update.get("briefed_at", False) is None:
                    # Same id, new phase (see ``_rebind_update``): a compaction, not a restart.
                    self._note_compacted(team, name, str(roster.session_source(update["session"]) or "?"))
                    try:
                        roster.write_briefing_job(team.paths, str(update.get("name", name)))
                    except HerdrTeamError as err:
                        self.log("{}: could not enqueue a briefing for {}: {}".format(team.name, name, err.code))
            for member in team.members():
                terminal_id = member.get("terminal_id")
                if isinstance(terminal_id, str) and member.get("status") == "active":
                    self._write_pane_record(team, member)

    def _set_member_status(self, team: TeamState, name: str, status: str, clear_tokens: bool = False) -> None:
        member = team.member(name)
        if member is None:
            return
        pane_id = member.get("pane_id")
        if clear_tokens and isinstance(pane_id, str):
            self._clear_tokens(pane_id)
        self._apply_changes(team, [(name, {"status": status, "last_seen_at": now_iso()})])
        self.who_dirty = True

    def _write_pane_record(self, team: TeamState, member: Dict[str, Any]) -> None:
        """``panes/<terminal_id>.json``: merge, never clobber the hook-written keys (``hooks_last_seen`` ...)."""
        try:
            path = self.session.pane_record(str(member.get("terminal_id")))
        except HerdrTeamError:
            return
        record = store.read_json(path, default=None)
        if not isinstance(record, dict):
            record = {}
        record.update({"team": team.name, "name": member.get("name"), "gen": int(member.get("generation") or 1)})
        if roster.session_key(member.get("session")) is not None:
            record["session"] = dict(member["session"])
        store.write_json(path, record, fsync=False)

    def _hooks_last_seen(self, member: Dict[str, Any]) -> Optional[str]:
        terminal_id = member.get("terminal_id")
        if not isinstance(terminal_id, str):
            return None
        try:
            record = store.read_json(self.session.pane_record(terminal_id), default=None)
        except HerdrTeamError:
            return None
        if isinstance(record, dict) and isinstance(record.get("hooks_last_seen"), str):
            return record["hooks_last_seen"]
        return None

    def _apply_name(self, team: TeamState, member: Dict[str, Any], pane_id: str) -> None:
        name = str(member.get("name"))
        try:
            self.api.request("agent.rename", {"target": pane_id, "name": name}, timeout=5.0)
            self.log("{}: re-applied name {} on {}".format(team.name, name, pane_id))
        except HerdrTeamError as err:
            if err.code == "agent_name_taken":
                self.log("{}: name {} taken during re-application; name_conflict".format(team.name, name))
                self._apply_changes(team, [(name, {"status": "name_conflict"})])
                self.enqueue_toast(team.name, [], "herdr-synapse {}: name conflict".format(team.name), "{} could not be renamed: {}".format(name, err.message), "none", kind="roster")
            else:
                self.log("{}: agent.rename {} failed: {}".format(team.name, name, err.code))

    def _clear_stale_label(self, team: TeamState, member: Dict[str, Any], old_pane: str) -> None:
        """Drop the member's team label from its previous pane when that pane still carries it and hosts no agent.

        Uses the pane rows fetched for this reconcile (step (b) already needed
        them for any non-terminal match), so no extra ``pane.list`` is spent;
        ``roster.clear_stale_label`` is the same rule for the CLI's ``bind``.
        """
        label = member.get("label") or roster.label_for(team.name, str(member.get("role")))
        for pane in self._fetch_panes().values():
            if pane.get("pane_id") == old_pane:
                if pane.get("label") == label and not pane.get("agent"):
                    self.log("{}: clearing stale label {} on {} (member {} rebound elsewhere)".format(team.name, label, old_pane, member.get("name")))
                    roster.label_pane(self.api, old_pane, None)
                return

    def _apply_label(self, team: TeamState, member: Dict[str, Any], pane_id: str) -> None:
        label = member.get("label") or "team:{}/{}".format(team.name, member.get("role"))
        try:
            self.api.request("pane.rename", {"pane_id": pane_id, "label": label}, timeout=5.0)
        except HerdrTeamError as err:
            self.log("{}: pane.rename {} failed: {}".format(team.name, pane_id, err.code))

    # -- tokens ------------------------------------------------------------------------

    def _stamp_tokens(self, team: TeamState, member: Dict[str, Any], now_s: float) -> None:
        pane_id = member.get("pane_id")
        if not isinstance(pane_id, str) or not member.get("terminal_id"):
            return
        role = str(member.get("role") or "")
        identity: Dict[str, Any] = {"team": team.name, "team_role": role}
        identity.update(roster.color_slot_tokens(team.name, roster.color_slot_of(team.roster)))
        try:
            self.api.request("pane.report_metadata", {"pane_id": pane_id, "source": "herdr-synapse:roster", "tokens": identity}, timeout=5.0)
        except HerdrTeamError as err:
            self.log("{}: token stamp on {} failed: {}".format(team.name, pane_id, err.code))
            return
        rt = team.rt(str(member.get("name")))
        head = task_headline(member, rt.last_post, time.time(), task=read_task_file(team.paths, str(member.get("name"))))
        if head:
            rt.last_headline = head
            value = head[:80]
            now = self.now_ms()
            # Register (plan 12): another reporter may write team_task; overwrite only when our headline
            # changed, or when the TTL needs a refresh (the token is the heartbeat and fades in 120 s).
            unchanged = rt.last_task_value == value and rt.last_task_stamp_ms is not None and now - rt.last_task_stamp_ms < TASK_RESTAMP_S * 1000.0
            if unchanged:
                return
            try:
                self.api.request("pane.report_metadata", {"pane_id": pane_id, "source": "herdr-synapse:task", "tokens": {"team_task": value}, "ttl_ms": TASK_TTL_MS}, timeout=5.0)
                rt.last_task_value = value
                rt.last_task_stamp_ms = now
            except HerdrTeamError as err:
                self.log("{}: task token on {} failed: {}".format(team.name, pane_id, err.code))

    def _clear_tokens(self, pane_id: str) -> None:
        cleared: Dict[str, Any] = {"team": None, "team_role": None}
        cleared.update(roster.color_slot_tokens(None, None))
        try:
            self.api.request("pane.report_metadata", {"pane_id": pane_id, "source": roster.TOKEN_SOURCE_ROSTER, "tokens": cleared}, timeout=5.0)
            self.api.request("pane.report_metadata", {"pane_id": pane_id, "source": roster.TOKEN_SOURCE_TASK, "tokens": {"team_task": None}}, timeout=5.0)
            self.api.request("pane.report_metadata", {"pane_id": pane_id, "source": roster.TOKEN_SOURCE_CONTEXT, "tokens": roster.context_slot_tokens(None)}, timeout=5.0)
        except HerdrTeamError as err:
            self.log("clear tokens on {} failed: {}".format(pane_id, err.code))

    def heartbeat_if_due(self, now: float) -> None:
        if self.last_heartbeat_ms is not None and now - self.last_heartbeat_ms < HEARTBEAT_S * 1000.0:
            return
        self.last_heartbeat_ms = now
        self.heartbeat()

    def heartbeat(self) -> None:
        """Restamp ``team``/``team_role``, refresh ``team_task`` with TTL, update ``daemon.json``."""
        now_s = time.time()
        for team in self.teams.values():
            for member in team.members():
                if member.get("status") == "active" and member.get("terminal_id") in self.agents:
                    self._stamp_tokens(team, member, now_s)
        if not self._session_dir_present():
            self.log("session state dir removed; stopping")
            self.request_stop("session dir removed")
            return
        self.write_info()
        if self.detached and rotate_log(self.session.daemon_log):
            try:
                fd = _open_log_fd(self.session)
                os.dup2(fd, 1)
                os.dup2(fd, 2)
                os.close(fd)
            except OSError:
                pass
            self.log("log rotated")

    # -- board tail ----------------------------------------------------------------------

    def tail_boards(self) -> None:
        for team in self.teams.values():
            try:
                self._tail_board(team)
            except HerdrTeamError as err:
                self.log("{}: tail error {}".format(team.name, err))

    def _tail_board(self, team: TeamState) -> None:
        if team.tailer is None:
            self._load_tail_state(team)
        assert team.tailer is not None
        records = team.tailer.poll()
        for warning in team.tailer.warnings:
            self.log("{}: tailer: {}".format(team.name, warning))
        team.tailer.warnings.clear()
        for rec in records:
            if rec.get("synthetic"):
                # BoardTailer saw the file restart at or below the watermark with no archive explanation.
                self.log("{}: reset detected: {}".format(team.name, rec.get("text")))
                self._append_system(team, "reset_detected", str(rec.get("text") or "board reset detected"), ["human"])
                team.watermark = 0
                continue
            self._ingest_record(team, rec)
        team.watermark = team.tailer.watermark_seq

    @staticmethod
    def _counts_for_nudges(rec: Dict[str, Any]) -> bool:
        """Plan 6.1 reader rule: a record that renders ``(unverified)`` never counts for nudges.

        ``from: system`` needs ``kind: system`` plus an event; ``from: human``
        needs a console, popup, outside, or verified shell origin (an
        unfocused console is still the console); a member record needs a
        verified origin. Everything else could be a raw append.
        """
        if not render.is_unverified(rec):
            return True
        origin = rec.get("origin") if isinstance(rec.get("origin"), dict) else {}
        return rec.get("from") == "human" and _identity.human_origin_ok(origin)

    def _unread_mail_seqs(self, team: TeamState, name: str, retry_terminal: bool = False) -> List[int]:
        """Authored unread mail eligible for automatic delivery to ``name``.

        The board also carries system history and sparse filtered-read state.
        Neither may be flattened into mail: system records have their own
        explicit ingest policy, and ``seen`` seqs were already shown even when
        an older unread record keeps the contiguous cursor behind them.
        """
        cursor, seen = read_cursor_state(team.paths, name)
        terminal = set() if retry_terminal else team.delivery_terminal.get(name, set())
        seqs: List[int] = []
        for rec in read_board_records(team.paths, cursor):
            seq = rec.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool):
                continue
            if seq in seen or seq in terminal or seq in team.retracted:
                continue
            if store.is_member_mail(rec, name) and self._counts_for_nudges(rec):
                seqs.append(seq)
        return seqs

    def _ingest_record(self, team: TeamState, rec: Dict[str, Any]) -> None:
        seq = rec["seq"]
        author = rec["from"]
        kind = rec["kind"]
        now = self.now_ms()
        self.who_dirty = True
        if not self._counts_for_nudges(rec):
            self.counters["unverified_skipped"] += 1
            self.log("{}: #{} from {!r} is unverified (origin {}); it renders but never counts for nudges".format(
                team.name, seq, author, json.dumps(rec.get("origin"), ensure_ascii=False)[:120]))
            return
        self._track_ask(team, rec)
        self._track_link(team, rec)
        if author == "system":
            event = rec.get("event")
            if event == "board_cleared":
                # Everything the daemon held about the old board points at
                # records that are now in the archive: asks, linked messages
                # awaiting a read, and the nudges for them.
                team.open_asks.clear()
                team.link_inbox.clear()
                for name in [n for n, p in team.pending.items() if p.kind == "nudge"]:
                    del team.pending[name]
                self.log("{}: board cleared at #{}; asks, link inbox and pending nudges dropped".format(team.name, seq))
            if event in TOAST_SYSTEM_EVENTS and "human" in [t for t in (rec.get("to") or []) if isinstance(t, str)]:
                team.human_queue.append(rec)
            if event in NAMED_SYSTEM_EVENTS:
                # Addressed to particular members: each gets an ordinary nudge,
                # every gate applying, so "you are at 90%, finish and compact"
                # arrives at that member's next idle rather than never.
                for target in [t for t in (rec.get("to") or []) if isinstance(t, str) and t not in ("all", "human")]:
                    named = team.member(target)
                    if named is not None and named.get("kind") != "human" and named.get("terminal_id"):
                        self._add_pending(team, str(named["name"]), seq, False, "system", now)
            if rec.get("urgent") and event in URGENT_SYSTEM_EVENTS:
                # An urgent system broadcast nudges every member; a join spares the newcomer (its briefing covers it).
                newcomer = rec.get("member") if event == "member_joined" else None
                for member in team.members():
                    if member.get("kind") != "human" and member.get("terminal_id") and member.get("name") != newcomer:
                        self._add_pending(team, str(member["name"]), seq, True, "system", now)
            return
        if kind == "direct":
            # A line the human typed into one member (``say``): already in its input box, never a nudge.
            self.log("{}: #{} is a line the human typed into {}; recorded, not nudged".format(team.name, seq, ",".join(str(t) for t in rec.get("to") or [])))
            return
        retracts = rec.get("retracts")
        if kind == "retract" or isinstance(retracts, int):
            if isinstance(retracts, int):
                team.retracted.add(retracts)
                for name, pending in list(team.pending.items()):
                    if retracts in pending.deferred_seqs:
                        pending.deferred_seqs = [s for s in pending.deferred_seqs if s != retracts]
                    if retracts in pending.seqs:
                        already_landed = pending.landed_ms is not None and retracts <= pending.landed_seq_max
                        pending.seqs = [s for s in pending.seqs if s != retracts]
                        if not pending.seqs:
                            del team.pending[name]
                            self.log("{}: retract of #{} cancelled the pending nudge for {}".format(team.name, retracts, name))
                        if already_landed:
                            self._add_pending(team, name, seq, False, author, now)
                self._append_system(team, "retracted", "#{} was retracted by {}".format(retracts, author), ["all"])
            return
        member = team.member(author)
        if member is not None:
            rt = team.rt(author)
            rt.last_post = rec
            rt.last_headline = task_headline(member, rec, time.time())
        recipients = [str(t) for t in rec.get("to", []) if isinstance(t, str)]
        urgent = bool(rec.get("urgent"))
        interrupt = bool(rec.get("interrupt"))  # ``post --interrupt``: named recipients only (the CLI refuses ``all``)
        for target in recipients:
            if target == "human":
                if author != "human":
                    team.human_queue.append(rec)
                continue
            if target == "all":
                # The operator addressing the whole team is heard by every member (normal holds apply),
                # and so is the manager, whose whole job is splitting and sequencing the work; any other
                # agent's broadcast waits for the next board read unless it is urgent. Measured on a live
                # team, that wait ran to a median of 42 minutes, which is not a way to hand out scope.
                if urgent or author == "human" or author == team.manager_name():
                    for m in team.members():
                        if m.get("kind") != "human" and m.get("name") != author and m.get("terminal_id"):
                            self._add_pending(team, str(m["name"]), seq, urgent, author, now)
                continue
            if target == author:
                continue
            recipient = team.member(target) or team.member_by_retired_name(target)  # old names resolve for 10 min
            if recipient is None:
                continue
            self._add_pending(team, str(recipient.get("name")), seq, urgent, author, now, interrupt=interrupt)

    def _track_ask(self, team: TeamState, rec: Dict[str, Any]) -> None:
        """Keep ``team.open_asks`` current from the records already flowing past.

        ``raise_asks`` used to answer "is anything waiting" by re-parsing the
        whole active board every five seconds per team -- the exact cost
        ``wait_for_answer`` names as the reason not to poll with ``read``.
        Every record already passes through here once, so the set is
        maintained for free and the tick reads nothing.
        """
        from herdr_team import asks as _asks

        seq = rec.get("seq")
        if _asks.is_ask(rec) and isinstance(seq, int):
            team.open_asks[seq] = rec
            while len(team.open_asks) > OPEN_ASKS_MAX:
                team.open_asks.pop(min(team.open_asks))
        answered = _asks.answered_by(rec)
        if answered is not None:
            team.open_asks.pop(answered, None)
        retracts = rec.get("retracts")
        if isinstance(retracts, int) and not isinstance(retracts, bool):
            team.open_asks.pop(retracts, None)

    def _track_link(self, team: TeamState, rec: Dict[str, Any]) -> None:
        """A copy delivered here across a link waits for this team's manager to read it."""
        link = rec.get("link")
        seq = rec.get("seq")
        if isinstance(link, dict) and not link.get("mirror") and link.get("from_team") not in (None, team.name) and isinstance(seq, int):
            team.link_inbox[seq] = rec
            while len(team.link_inbox) > OPEN_ASKS_MAX:
                team.link_inbox.pop(min(team.link_inbox))

    def _seed_link_inbox(self, team: TeamState) -> None:
        """Delivered copies the manager has not read yet, from one bounded read at start."""
        manager = team.manager_name()
        cursor = read_cursor_seq(team.paths, manager) if manager else 0
        try:
            records = store.BoardStore(team.paths).read(last=OPEN_ASKS_MAX, include_retracted=False)
        except HerdrTeamError:
            return
        team.link_inbox = {}
        for rec in records:
            if isinstance(rec.get("seq"), int) and rec["seq"] > cursor:
                self._track_link(team, rec)

    def poll_link_receipts(self, now: float) -> None:
        """Tell the sending team's board when this team's manager has read a linked message."""
        for team in list(self.teams.values()):
            if not team.link_inbox:
                continue
            if team.link_receipt_ms is not None and now - team.link_receipt_ms < LINK_RECEIPT_POLL_S * 1000.0:
                continue
            team.link_receipt_ms = now
            manager = team.manager_name()
            if not manager:
                continue
            try:
                cursor = read_cursor_seq(team.paths, manager)
            except (HerdrTeamError, OSError):
                continue
            for seq in sorted(team.link_inbox):
                if seq > cursor:
                    break
                rec = team.link_inbox.pop(seq)
                link = rec.get("link") or {}
                origin = self.teams.get(str(link.get("from_team") or ""))
                if origin is None:
                    continue  # the sender was dissolved; nobody to tell
                self._append_system(origin, "link_read", "{} ({}) read the message to it (their #{})".format(manager, team.name, seq),
                                    ["team:" + team.name], {"link_id": link.get("id"), "reader": manager, "reader_team": team.name, "read_seq": seq})

    def _requeue_deferred(self, team: TeamState, name: str, brief: Pending, cursor: int, now: float) -> None:
        """Posts that arrived while ``brief`` was pending become a nudge once the member is past the briefing."""
        seqs = [s for s in brief.deferred_seqs if s > cursor and s not in team.retracted]
        if not seqs:
            return
        pending = Pending(first_ms=now, seqs=sorted(seqs), urgent=brief.deferred_urgent, authors=set(brief.deferred_authors),
                          interrupt=brief.deferred_interrupt, interrupt_authors=set(brief.deferred_authors) if brief.deferred_interrupt else set())
        team.pending[name] = pending
        self.who_dirty = True
        self.log("{}: {} briefing done; nudging for {} deferred during the briefing".format(team.name, name, ", ".join("#{}".format(s) for s in pending.seqs)))

    def raise_asks(self, now: float) -> None:
        """Open the popup when something is waiting on the operator.

        One popup for every team's queue is impossible — Herdr allows one popup
        at a time and it has no pane id to address — so this opens the popup for
        the first team that has an unanswered ask, and the popup itself lists
        that team's queue. ``popup.close`` is never called: it closes whatever
        the operator has open, which is not ours to take.
        """
        if self.ask_next_ms is not None and now < self.ask_next_ms:
            return
        self.ask_next_ms = now + ASK_POLL_S * 1000.0
        for team in list(self.teams.values()):
            try:
                if not self._ask_popup_wanted(team):
                    continue
            except Exception as err:  # noqa: BLE001 - one unreadable board must not stop the tick
                self.log("{}: ask scan failed: {}: {}".format(team.name, type(err).__name__, err))
                continue
            self._open_ask_popup(team, now)
            return

    def _ask_popup_wanted(self, team: TeamState) -> bool:
        from herdr_team.cmd_misc import ask_view

        if not ask_view(team.roster).get("popup"):
            return False
        if not team.open_asks:
            return False
        return bool(set(team.open_asks) - set(self._dismissed_asks(team)))

    def _dismissed_asks(self, team: TeamState) -> List[int]:
        """``asks.dismissed`` re-read only when its file changed."""
        from herdr_team import asks as _asks

        mtime = self._file_mtime(os.fspath(_asks.dismissed_path(team.paths)))
        cached = team.dismissed_cache
        if cached is not None and cached[0] == mtime:
            return cached[1]
        seqs = _asks.dismissed(team.paths)
        team.dismissed_cache = (mtime, seqs)
        return seqs

    def _revalidate_asks_for_popup(self, team: TeamState) -> bool:
        """Refresh the cheap tracker before taking focus with a popup.

        ``open_asks`` follows every ingested record without re-reading the
        board, so a long-running daemon can retain an ask after it falls out of
        the bounded window that the popup itself reads. Re-read only here, at
        the focus-stealing side-effect boundary, and use the popup's exact
        view as the authority.
        """
        from herdr_team import asks as _asks

        current = _asks.pending(team.paths)
        team.open_asks = {int(record["seq"]): record for record in current if isinstance(record.get("seq"), int)}
        return bool(set(team.open_asks) - set(self._dismissed_asks(team)))

    def _open_ask_popup(self, team: TeamState, now: float) -> None:
        """One ``plugin.pane.open``; a busy slot is a retry, never an error."""
        if not self._revalidate_asks_for_popup(team):
            return
        try:
            self.api.request("plugin.pane.open", {"plugin_id": PLUGIN_ID, "entrypoint": "asks", "focus": True,
                                                  "env": {"HERDR_TEAM": team.name}}, timeout=5.0)
        except HerdrTeamError as err:
            # ``ui_busy`` means the operator (or another popup) has the slot.
            # That is normal and frequent; wait rather than fight for it.
            self.ask_next_ms = now + ASK_POPUP_RETRY_S * 1000.0
            if err.code not in ("ui_busy", "plugin_pane_open_failed", "popup_open", "popup_already_open"):
                self.log("{}: could not open the asks popup: {}".format(team.name, err.code))
            return
        self.ask_next_ms = now + ASK_POPUP_RETRY_S * 1000.0
        self.log("{}: opened the asks popup".format(team.name))

    def poll_all_context(self, now: float) -> None:
        for team in list(self.teams.values()):
            try:
                self.poll_context(team, now)
            except Exception as err:  # noqa: BLE001 - one unreadable transcript must not stop the tick
                self.log("{}: context poll failed: {}: {}".format(team.name, type(err).__name__, err))

    def poll_context(self, team: TeamState, now: float) -> None:
        """Re-read how full each member is, and say so once when it crosses a line.

        The reading comes from the harness's own files (``herdr_team.context``);
        nothing is typed and nothing is asked of the agent. A member whose file
        has not changed since the last read is skipped, so a quiet team costs
        one ``stat`` per member per interval.
        """
        for member in team.members():
            if member.get("kind") == "human" or member.get("status") == "left":
                continue
            name = str(member.get("name") or "")
            terminal = member.get("terminal_id")
            if not name or not terminal:
                continue
            rt = team.rt(name)
            if rt.context_read_ms is not None and now - rt.context_read_ms < CONTEXT_POLL_S * 1000.0:
                continue
            rt.context_read_ms = now
            record = roster.read_pane_record(self.session, str(terminal)) or {}
            configured_model = _models.effective_setting(team.roster.get("config"), member)[0]
            try:
                reading = _context.read_member(member.get("kind"), member.get("session"), record,
                                               home=_context.home_dir(self.env), configured_model=configured_model)
            except Exception as err:  # noqa: BLE001 - a malformed transcript is not fatal
                self.log("{}: cannot read {}'s context: {}".format(team.name, name, err))
                continue
            if reading is None:
                continue
            mtime = self._file_mtime(reading.source)
            encoded = reading.to_json()
            if mtime is not None and rt.context_mtime == mtime and rt.context == encoded:
                continue  # neither the harness reading nor its resolved window changed
            rt.context_mtime = mtime
            previous = rt.context
            session_key = roster.session_key(member.get("session"))
            same_history = previous is not None and rt.context_session == session_key
            rt.context_session = session_key
            rt.context = encoded
            self.who_dirty = True
            open_control = rt.control_pending
            if isinstance(open_control, dict) and open_control.get("action") == "model" \
                    and _models.observed_matches(member.get("kind"), open_control.get("model"), reading.model):
                self._note_model_applied(team, name, reading.model, now)
            if same_history:
                self._check_context_drop(team, name, reading, previous, now)
            self._stamp_context(team, member, reading, now)
            self._announce_context(team, name, reading)

    def _check_context_drop(self, team: TeamState, name: str, reading: "_context.Reading", previous: Optional[Dict[str, Any]], now: float) -> None:
        """A large fall in a member's token count is a compaction happening.

        This is the only completion signal that works for every kind: a turn
        can only ever add to the context, so a reading well below the one taken
        when ``/compact`` was typed means the summary replaced the history.
        Claude also changes its session phase, which ``_note_compacted`` sees
        first; Codex and OpenCode say nothing, and are seen here.
        """
        rt = team.rt(name)
        open_control = rt.control_pending
        before = (open_control or {}).get("used")
        if not isinstance(before, int) or before <= 0:
            before = (previous or {}).get("used") if isinstance(previous, dict) else None
        if not isinstance(before, int) or before <= 0 or reading.used >= before * CONTROL_DROP_RATIO:
            return
        if isinstance(open_control, dict):
            # A ``clear`` drops the count too, but a clear is proven by the new
            # session, not by the fall; reporting it here would name the wrong
            # thing. Leave it to ``_note_cleared`` or to the observation bound.
            if open_control.get("action") == "compact":
                self._note_compacted(team, name, "{:,} -> {:,} tokens".format(before, reading.used))
            return
        # Nobody on this team asked: the harness compacted on its own, or the
        # operator did it by hand. Worth one line, because a teammate reading
        # the board needs to know the summary is what this member now knows.
        rt.context_severity = None
        self._append_system(team, "context_compacted", "{} compacted its context on its own ({:,} -> {:,} tokens)".format(name, before, reading.used),
                            [name, "all"], {"member": name, "requested_by": None, "detected_by": "token count fell"})
        # Requested or not, the summary is what this member now knows. This
        # branch used to stop at the record, so the harness's own compaction
        # at 90% -- the case the warnings exist to pre-empt -- left a Codex or
        # OpenCode member with nothing typed back.
        self._rebrief_after_compaction(team, name)

    @staticmethod
    def _file_mtime(path: str) -> Optional[float]:
        try:
            return os.stat(path).st_mtime
        except OSError:
            return None

    def _announce_context(self, team: TeamState, name: str, reading: "_context.Reading") -> None:
        """One board record per crossing, addressed to the member and to the team.

        Addressed to the member as well as ``all`` on purpose: a record naming
        nobody is a broadcast the delivery gate holds, so the one agent that
        needs to act on this would never be nudged about it.
        """
        rt = team.rt(name)
        severity = _usage.severity_for(reading.percent)
        if severity not in (_usage.WARNING, _usage.CRITICAL):
            rt.context_severity = None  # dropped back down: the next crossing is news again
            return
        if rt.context_severity == severity or (rt.context_severity == _usage.CRITICAL and severity == _usage.WARNING):
            return
        rt.context_severity = severity
        self._append_system(
            team, "context_high",
            "{} is at {:.0f}% of its context window ({} of {} tokens); finish or hand off, then compact.".format(
                name, reading.percent, "{:,}".format(reading.used), "{:,}".format(reading.window)),
            [name, "all"],
            {"member": name, "percent": round(reading.percent, 1), "used": reading.used, "window": reading.window, "severity": severity},
        )

    def _stamp_context(self, team: TeamState, member: Dict[str, Any], reading: "_context.Reading", now: float) -> None:
        """Publish the reading as a pane token so the Herdr sidebar can show it."""
        pane_id = member.get("pane_id")
        if not isinstance(pane_id, str) or not pane_id:
            return
        rt = team.rt(str(member.get("name")))
        percent = reading.percent
        value = "{:.0f}%".format(percent) if percent is not None else None
        severity = _usage.severity_for(percent)
        stamp = "{}:{}".format(severity or _usage.NORMAL, value or "unknown")
        unchanged = rt.context_stamp_value == stamp and rt.context_stamp_ms is not None and now - rt.context_stamp_ms < TASK_RESTAMP_S * 1000.0
        if unchanged:
            return
        try:
            self.api.request("pane.report_metadata", {
                "pane_id": pane_id, "source": roster.TOKEN_SOURCE_CONTEXT,
                "tokens": roster.context_slot_tokens(value, severity), "ttl_ms": CONTEXT_TTL_MS,
            }, timeout=5.0)
            rt.context_stamp_value = stamp
            rt.context_stamp_ms = now
        except HerdrTeamError as err:
            self.log("{}: context token for {} failed: {}".format(team.name, member.get("name"), err.code))

    @staticmethod
    def _keystroke_for(kind: Any, action: str) -> str:
        from herdr_team.cmd_board import control_keystroke

        return control_keystroke(kind, action)

    def _start_control(self, team: TeamState, job: Dict[str, Any], now: float) -> None:
        """Type ``/compact`` or ``/clear`` into a member, once it is safe to.

        The job is only a pointer: the text and the authority come from the
        board record it names, re-validated here, because anything that can
        write the jobs directory could otherwise mint one.
        """
        name = str(job.get("member") or "")
        action = str(job.get("action") or "")
        member = team.member(name)
        if member is None or action not in ("compact", "clear", "model", "restart"):
            self.log("{}: control job for {!r} refused: unknown member or action".format(team.name, name))
            return
        record = self._board_record(team, job.get("seq"))
        problem = self._control_source_problem(record, name)
        if problem is not None:
            self.log("{}: control job for {} refused: {}".format(team.name, name, problem))
            self._append_system(team, "typed", "{} of {} refused: {}".format(action, name, problem), ["human"], {"member": name})
            return
        control_doc = (record or {}).get("control") if isinstance((record or {}).get("control"), dict) else {}
        extra: Dict[str, Any] = {}
        if action == "model":
            # The lines come from the record, which the origin check above vouched for.
            lines = [str(k).strip() for k in (control_doc.get("keystrokes") or []) if str(k).strip().startswith("/")]
            if not lines:
                self._append_system(team, "typed", "model change of {} refused: nothing to type".format(name), ["human"], {"member": name})
                return
            extra = {"model": control_doc.get("model"), "effort": control_doc.get("effort")}
        elif action == "restart":
            exit_key = str(control_doc.get("exit") or "").strip()
            argv = [str(a) for a in (control_doc.get("argv") or []) if str(a)]
            if not exit_key.startswith("/") or not argv:
                self._append_system(team, "typed", "restart of {} refused: no exit command or resume argv on the record".format(name), ["human"], {"member": name})
                return
            lines = [exit_key]
            extra = {"model": control_doc.get("model"), "effort": control_doc.get("effort"), "argv": argv}
        else:
            try:
                lines = [self._keystroke_for(member.get("kind"), action)]
            except HerdrTeamError as err:
                self.log("{}: {}".format(team.name, err.message))
                self._append_system(team, "typed", "{} of {} refused: {}".format(action, name, err.code), ["human"], {"member": name})
                return
        pending = Pending(first_ms=now, kind="control", lines=list(lines), force=True, seqs=[])
        pending.control = dict({"action": action, "keystroke": lines[0], "keystrokes": list(lines), "requested_by": (record or {}).get("from"), "kind": member.get("kind")}, **extra)
        team.pending[name] = pending
        self.log("{}: {} queued for {} ({})".format(team.name, action, name, "; ".join(repr(k) for k in lines)))

    @staticmethod
    def _control_source_problem(record: Optional[Dict[str, Any]], member: str) -> Optional[str]:
        """Why this control record is not trustworthy, or None.

        Mirrors ``say_source_problem``: the record must be a direct line to this
        member carrying a control block, from a verified origin.
        """
        if not isinstance(record, dict):
            return "no board record"
        if record.get("kind") != "direct":
            return "record is not a direct line"
        if list(record.get("to") or []) != [member]:
            return "record is not addressed to {}".format(member)
        if not isinstance(record.get("control"), dict):
            return "record carries no control block"
        origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
        if origin.get("verified") is not True:
            return "record origin is unverified"
        return None

    def _send_control(self, team: TeamState, member: Dict[str, Any], pending: Pending, snapshot: Any, now: float) -> None:
        """Type one control keystroke into an idle member: the text, then Enter.

        Not ``agent.prompt``. That call sends a bracketed paste, and a TUI
        agent reads a pasted ``/compact`` as literal text rather than as its
        own command, so the slash command would arrive as a prompt saying
        "/compact" and be answered instead of run. ``pane.send_text`` writes
        the bytes a keyboard would and ``pane.send_keys`` submits them, which
        is the only faithful way to press a key on another agent's behalf.

        The pane id comes from the snapshot the gate just validated, never
        from the roster: the two are re-checked against each other in
        ``_assert_roster_terminal`` above, and this is the copy that was
        proven to still hold the member.
        """
        name = str(member["name"])
        rt = team.rt(name)
        control = dict(pending.control or {})
        action = str(control.get("action") or "")
        keystrokes = [str(k) for k in (control.get("keystrokes") or []) if str(k)] or ([str(control.get("keystroke"))] if control.get("keystroke") else [])
        keystroke = " then ".join(keystrokes)
        pane_id = str(snapshot.pane_id or member.get("pane_id") or "")
        if not action or not keystrokes or not pane_id:
            self._finish_pending(team, name, pending, "typed", "{} of {} refused: nothing to type".format(action or "control", name))
            return
        pending.attempts += 1
        pending.gate_seq = snapshot.state_change_seq
        attempt_id = "{}-{}-{}".format(name, int(time.time() * 1000), pending.attempts)
        team.ledger.record_intent(Attempt(
            id=attempt_id, member=name, kind=str(member.get("kind")), seqs=[],
            hook_authority=bool(snapshot.screen_detection_skipped), weak_idle=False, focused=bool(snapshot.focused),
            prompt_line_empty=not snapshot.prompt_line, gate_ms=max(0.0, now - (snapshot.stable_since_ms or now)),
            queue_ms=max(0.0, now - pending.first_ms), attempts=pending.attempts,
            manifest_source=(snapshot.explain or {}).get("manifest_source") if isinstance(snapshot.explain, dict) else None,
            extra={"delivery": "control", "action": action, "keystroke": keystroke, "pane_id": pane_id, "terminal_id": snapshot.terminal_id},
        ))
        pending.attempt_id = attempt_id
        rt.in_flight = True
        try:
            if self.dry_nudge:
                self.log("{}: DRY {} of {} ({}): {!r}".format(team.name, action, name, pane_id, keystroke))
                result, details = RESULT_DRY, {"text": keystroke}
            else:
                result, details = RESULT_LANDED_WORKING, {}
                for index, line in enumerate(keystrokes):
                    if index:
                        self.control_gap_sleep(CONTROL_LINE_GAP_S)
                    result, details = self._type_keystroke(pane_id, line)
                    if result != RESULT_LANDED_WORKING:
                        break
        finally:
            rt.in_flight = False
        team.ledger.record_result(attempt_id, result, details)
        if result not in (RESULT_LANDED_WORKING, RESULT_DRY):
            pending.transient_failures += 1
            backoff = min(TRANSIENT_BACKOFF_MAX_S, TRANSIENT_BACKOFF_MIN_S * (2 ** (pending.transient_failures - 1)))
            pending.next_eligible_ms = now + backoff * 1000.0
            self.log("{}: {} of {} failed: {} {}".format(team.name, action, name, result, json.dumps(details, ensure_ascii=False)[:200]))
            if pending.attempts >= 3:
                self._finish_pending(team, name, pending, "typed", "{} of {} failed: {}".format(action, name, details.get("code") or result))
            return
        pending.landed_ms = now
        rt.last_nudge_ms = now
        self.global_last_nudge_ms = now
        used = (rt.context or {}).get("used")
        rt.control_pending = {
            "action": action, "requested_by": control.get("requested_by") or "human",
            "typed_ms": now, "used": used if isinstance(used, int) else None,
            "session": roster.short_session(member.get("session")),
            "model": control.get("model"), "effort": control.get("effort"),
        }
        self.log("{}: typed {!r} into {} ({}); waiting for the effect".format(team.name, keystroke, name, pane_id))
        self._append_system(team, "typed", "{} typed into {}, asked by {}".format(keystroke, name, rt.control_pending["requested_by"]),
                            [name], {"member": name, "action": action, "keystroke": keystroke, "keystrokes": keystrokes})
        self.who_dirty = True
        if action == "restart":
            rt.restart = {"phase": "exiting", "since_ms": now, "argv": list(control.get("argv") or []), "kind": str(member.get("kind") or ""),
                          "pane_id": pane_id, "model": control.get("model"), "effort": control.get("effort"),
                          "requested_by": rt.control_pending["requested_by"]}
        elif action == "model" and not control.get("model"):
            # Effort alone has no observable: the transcript records the model,
            # not the thinking budget. Typed is as far as this can be proven.
            self._note_model_applied(team, name, None, now)

    def _type_keystroke(self, pane_id: str, keystroke: str) -> Tuple[str, Dict[str, Any]]:
        """``pane.send_text`` then Enter; a failure on either half is one failure."""
        t0 = time.monotonic()
        try:
            self.api.request("pane.send_text", {"pane_id": pane_id, "text": keystroke}, timeout=PROMPT_TIMEOUT_S)
            self.api.request("pane.send_keys", {"pane_id": pane_id, "keys": ["enter"]}, timeout=PROMPT_TIMEOUT_S)
        except HerdrTeamError as err:
            return RESULT_TRANSIENT, {"elapsed_ms": (time.monotonic() - t0) * 1000.0, "code": err.code, "message": err.message}
        return RESULT_LANDED_WORKING, {"elapsed_ms": (time.monotonic() - t0) * 1000.0}

    def _control_observed(self, team: TeamState, name: str, action: str, note: str, now: float) -> bool:
        """Close the open ``compact``/``clear`` job for ``name``: its effect is visible.

        Returns True when there was one, so the caller can say who asked for
        what just happened rather than reporting it as something the agent did
        to itself.
        """
        rt = team.rt(name)
        open_control = rt.control_pending
        if not isinstance(open_control, dict) or open_control.get("action") != action:
            return False
        rt.control_pending = None
        pending = team.pending.get(name)
        if pending is not None and pending.kind == "control":
            if pending.attempt_id:
                team.ledger.record_outcome(pending.attempt_id, True, max(0.0, now - (pending.landed_ms or now)))
            del team.pending[name]
        self.log("{}: {} of {} took effect ({})".format(team.name, action, name, note))
        self.who_dirty = True
        return True

    def _note_cleared(self, team: TeamState, name: str, shown: Any) -> None:
        """Say a restart was a requested ``clear``, when it was one.

        A deliberate clear and a crash look identical from outside: both mint a
        session and lose the agent's memory. Only the open job tells them
        apart, so the record is written here rather than left to read as an
        unexplained restart.
        """
        now = self.now_ms()
        rt = team.rt(name)
        by = str((rt.control_pending or {}).get("requested_by") or "human")
        if not self._control_observed(team, name, "clear", "new session", now):
            return
        self._append_system(team, "context_cleared", "{}'s context was cleared, asked by {}".format(shown, by), [str(shown), "all"],
                            {"member": str(shown), "requested_by": by})

    def _note_compacted(self, team: TeamState, name: str, note: str) -> None:
        """One record for a compaction, whoever asked for it."""
        now = self.now_ms()
        rt = team.rt(name)
        by = str((rt.control_pending or {}).get("requested_by") or "")
        requested = self._control_observed(team, name, "compact", note, now)
        rt.context_severity = None  # whatever it was full of is gone; the next crossing is news again
        text = "{} compacted its context{}; what it knows is now a summary".format(name, ", asked by {}".format(by) if requested and by else "")
        self._append_system(team, "context_compacted", text, [name, "all"],
                            {"member": name, "requested_by": by if requested else None, "detected_by": note})
        self._rebrief_after_compaction(team, name)

    def _rebrief_after_compaction(self, team: TeamState, name: str) -> None:
        """Brief a member again once its context has been summarised away.

        Claude reaches this through its session phase change, which
        ``_apply_changes`` handles and which clears ``briefed_at`` before this
        runs, so the guard below makes that path a no-op. Codex and OpenCode
        report no phase at all and are seen only by their token count falling:
        until this, they were never re-briefed, so the two kinds with no hooks
        -- the ones for which the typed line is the only channel there is --
        were exactly the ones that got nothing back after a compaction.
        """
        member = team.member(name)
        if member is None or member.get("kind") == "human" or not member.get("terminal_id"):
            return
        if member.get("briefed_at") is None:
            return  # a briefing is already pending; do not queue a second
        self._apply_changes(team, [(name, {"briefed_at": None})])
        try:
            roster.write_briefing_job(team.paths, name)
        except HerdrTeamError as err:
            self.log("{}: could not brief {} again after its compaction: {}".format(team.name, name, err.code))

    #: Patched in tests; the pause between two control lines.
    control_gap_sleep = staticmethod(time.sleep)

    def _note_model_applied(self, team: TeamState, name: str, observed: Optional[str], now: float) -> None:
        """Close an open ``model`` job: the transcript reports the model, or only the effort was changed."""
        rt = team.rt(name)
        wanted = _models.label((rt.control_pending or {}).get("model"), (rt.control_pending or {}).get("effort"))
        if not self._control_observed(team, name, "model", "transcript reports {}".format(observed) if observed else "effort typed", now):
            return
        text = "{} now runs {}".format(name, observed) if observed else "{} now runs {} (effort typed; not observable in the transcript)".format(name, wanted)
        self._append_system(team, "model_applied", text, [name, "all"], {"member": name, "observed": observed, "setting": wanted})

    def advance_restarts(self, now: float) -> None:
        for team in list(self.teams.values()):
            for name, rt in list(team.runtime.items()):
                if isinstance(rt.restart, dict):
                    try:
                        self._advance_restart(team, name, rt, now)
                    except Exception as err:  # noqa: BLE001 - one member's restart must not stop the tick
                        self.log("{}: restart of {} failed: {}: {}".format(team.name, name, type(err).__name__, err))
                        self._restart_failed(team, name, rt, "{}: {}".format(type(err).__name__, err), now)

    def _advance_restart(self, team: TeamState, name: str, rt: MemberRuntime, now: float) -> None:
        """One step of exit -> start -> wait for the session to come back."""
        state = rt.restart or {}
        phase = state.get("phase")
        pane_id = str(state.get("pane_id") or "")
        if phase == "exiting":
            if now - float(state.get("since_ms") or now) > RESTART_EXIT_S * 1000.0:
                self._restart_failed(team, name, rt, "did not exit within {:.0f}s".format(RESTART_EXIT_S), now)
                return
            pane = self._pane_row(pane_id)
            if pane is None or pane.get("agent"):
                return  # still up, or not readable yet
            argv = [str(a) for a in state.get("argv") or []]
            # ``agent start`` runs the kind's own binary; the resume argv's first word is that binary.
            handle = _launch.start_agent_async(self.api, name, str(state.get("kind") or ""), pane_id, args=argv[1:])
            state.update({"phase": "starting", "started_ms": now, "handle": handle})
            phase = "starting"
            self.log("{}: {} exited; starting again: {}".format(team.name, name, " ".join(handle.argv)))
            # fall through: a start that already finished (a synchronous API) is judged now, not next tick
        if phase == "starting":
            handle = state.get("handle")
            if handle is not None and handle.done():
                ok, text = handle.result()
                state["handle"] = None
                if not ok:
                    self._restart_failed(team, name, rt, "agent start failed: {}".format(text[:200] or "unknown error"), now)
                    return
                state["phase"] = "started"
                self.log("{}: {} is back; waiting for it to report its session".format(team.name, name))
        if phase in ("starting", "started") and now - float(state.get("started_ms") or now) > RESTART_START_S * 1000.0:
            self._restart_failed(team, name, rt, "did not come back within {:.0f}s".format(RESTART_START_S), now)

    def _pane_row(self, pane_id: str) -> Optional[Dict[str, Any]]:
        try:
            result = self.api.request("pane.get", {"pane_id": pane_id}, timeout=5.0)
        except HerdrTeamError:
            return None
        pane = result.get("pane") if isinstance(result, dict) else None
        return pane if isinstance(pane, dict) else None

    def _restart_failed(self, team: TeamState, name: str, rt: MemberRuntime, why: str, now: float) -> None:
        state = rt.restart or {}
        rt.restart = None
        rt.control_pending = None
        pending = team.pending.get(name)
        if pending is not None and pending.kind == "control":
            self._finish_pending(team, name, pending, "typed", "restart of {} failed: {}".format(name, why))
        self._append_system(team, "restart_failed", "{}'s restart failed: {}. Bring it back with: herdr-synapse resume {}".format(name, why, name),
                            ["human", "all"], {"member": name, "why": why, "setting": _models.label(state.get("model"), state.get("effort"))})
        self.who_dirty = True

    def _note_restart_done(self, team: TeamState, name: str, update: Dict[str, Any], now: float) -> None:
        """The resumed agent reported its session: the restart is complete, and it was not a compaction."""
        rt = team.rt(name)
        state = rt.restart or {}
        rt.restart = None
        setting = _models.label(state.get("model"), state.get("effort"))
        self._control_observed(team, name, "restart", "session resumed", now)
        self._append_system(team, "model_applied", "{} restarted with {} (its session was resumed, asked by {})".format(name, setting or "its recorded setting", state.get("requested_by") or "human"),
                            [name, "all"], {"member": name, "setting": setting, "restarted": True})
        if update.get("briefed_at", False) is None:
            try:
                roster.write_briefing_job(team.paths, str(update.get("name", name)))
            except HerdrTeamError as err:
                self.log("{}: could not enqueue a briefing for {}: {}".format(team.name, name, err.code))

    def _restarting(self, team: TeamState, name: str) -> bool:
        rt = team.runtime.get(name)
        return rt is not None and isinstance(rt.restart, dict)

    def _control_unobserved(self, team: TeamState, name: str, pending: Pending, now: float) -> None:
        """The keystroke landed but nothing changed within ``CONTROL_OBSERVE_S``."""
        rt = team.rt(name)
        action = str((pending.control or {}).get("action") or "control")
        rt.control_pending = None
        if action == "model":
            self._finish_pending(team, name, pending, "typed", "model change of {} was typed but the transcript has not shown the new model in {:.0f}s".format(name, CONTROL_OBSERVE_S))
            return
        self._finish_pending(team, name, pending, "typed", "{} of {} was typed but no effect was seen in {:.0f}s".format(action, name, CONTROL_OBSERVE_S))

    def _board_record(self, team: TeamState, seq: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(seq, int):
            return None
        try:
            for record in store.BoardStore(team.paths).read(since_seq=seq - 1):
                if record.get("seq") == seq:
                    return record
        except HerdrTeamError:
            return None
        return None

    def sweep_all_unread(self, now: float) -> None:
        """Run the unread sweep for every team; one bad team never stops the rest."""
        for name, team in list(self.teams.items()):
            try:
                self.sweep_unread(team, now)
            except Exception as err:  # noqa: BLE001 - F-07: the daemon must outlive one bad file/event
                # The dict key, not ``team.name``: a team object broken enough to
                # raise here is broken enough to have no usable name either.
                self.log("{}: unread sweep failed: {}: {}".format(name, type(err).__name__, err))

    def sweep_unread(self, team: TeamState, now: float) -> None:
        """Nudge an idle member holding mail that nothing else will ever wake it for.

        A post addressed to ``all`` creates no pending entry unless it is urgent
        or the operator wrote it, and every agent-side read path is
        turn-triggered: the Claude hooks need a session event, a submitted
        prompt, or a turn ending, and other kinds have no hooks at all. So an
        idle agent whose only unread mail was a teammate's broadcast stayed
        asleep indefinitely. Measured on a live team: a member's broadcast took
        a median of 42 minutes to reach everyone, and 11 of 47 never did.

        The sweep does not make broadcasts interrupt. It creates an ordinary
        non-urgent pending, so every gate still applies, and it does so at most
        once per ``IDLE_SWEEP_AFTER_S`` per member, so a chatty team costs one
        nudge per member per interval rather than one per post.
        """
        if team.sweep_scanned_ms is not None and now - team.sweep_scanned_ms < IDLE_SWEEP_POLL_S * 1000.0:
            return
        team.sweep_scanned_ms = now
        for member in team.members():
            if member.get("kind") == "human" or member.get("status") == "left":
                continue
            name = str(member.get("name") or "")
            terminal = member.get("terminal_id")
            if not name or not terminal or name in team.pending:
                continue  # the normal path owns a member that already has work
            last = team.swept_ms.get(name)
            if last is not None and now - last < IDLE_SWEEP_AFTER_S * 1000.0:
                continue
            if not member.get("briefed_at"):
                continue  # a newcomer's briefing covers the backlog; do not double up
            runtime = team.runtime.get(name)
            last_nudge = getattr(runtime, "last_nudge_ms", None) if runtime is not None else None
            if last_nudge is not None and now - last_nudge < IDLE_SWEEP_AFTER_S * 1000.0:
                continue  # it heard from us recently through the normal path
            agent = self.agents.get(str(terminal)) or {}
            if agent.get("agent_status") not in ("idle", "done"):
                continue  # a working member reads the board at its own turn boundary
            try:
                unread = self._unread_mail_seqs(team, name)
            except (HerdrTeamError, OSError):
                continue
            if not unread:
                continue
            team.swept_ms[name] = now
            team.pending[name] = Pending(seqs=unread, first_ms=now)
            self.who_dirty = True
            self.log("{}: {} is idle with {} unread post(s) nothing woke it for; sweeping {}".format(
                team.name, name, len(unread), unread[:6]))

    def _add_pending(self, team: TeamState, name: str, seq: int, urgent: bool, author: str, now: float, interrupt: bool = False) -> None:
        pending = team.pending.get(name)
        if pending is None or pending.kind != "nudge":
            if pending is not None and pending.kind == "brief":
                # The briefing lands first; the seq is kept so the nudge follows once the briefing is
                # acknowledged or given up (``_requeue_deferred``), never dropped.
                if seq not in pending.deferred_seqs:
                    pending.deferred_seqs.append(seq)
                pending.deferred_authors.add(author)
                pending.deferred_urgent = pending.deferred_urgent or urgent
                pending.deferred_interrupt = pending.deferred_interrupt or interrupt
                return
            pending = Pending(first_ms=now)
            team.pending[name] = pending
        if seq not in pending.seqs:
            pending.seqs.append(seq)
            pending.last_added_ms = now
        pending.urgent = pending.urgent or urgent
        pending.authors.add(author)
        if interrupt:
            pending.interrupt = True
            pending.interrupt_authors.add(author)
        if pending.landed_ms is not None and seq > pending.landed_seq_max and pending.attempts == 1 and not pending.renudges:
            # One immediate follow-up is allowed when posts arrived after a landing.
            pending.follow_up_due = True
            pending.next_eligible_ms = now

    # -- jobs -------------------------------------------------------------------------

    def consume_jobs(self, now: float) -> None:
        if self.last_jobs_ms is not None and now - self.last_jobs_ms < JOBS_POLL_S * 1000.0:
            return
        self.last_jobs_ms = now
        for team in self.teams.values():
            try:
                names = sorted(os.listdir(team.paths.jobs_dir))
            except OSError:
                continue
            for name in names:
                if not name.endswith(".json") or name.startswith("."):
                    continue
                path = team.paths.jobs_dir / name
                job = store.read_json(path, default=None)
                try:
                    os.unlink(path)
                except OSError:
                    pass
                if isinstance(job, dict):
                    self.counters["jobs"] += 1
                    try:
                        self._run_job(team, job, now)
                    except Exception as err:  # noqa: BLE001 - F-07: the daemon must outlive one bad file/event
                        # A bad job file (reserved member, blank role, disk full) is logged, never fatal.
                        self.counters["phase_errors"] += 1
                        self.log("{}: job {} failed: {}: {}".format(team.name, name, type(err).__name__, err))

    def _run_job(self, team: TeamState, job: Dict[str, Any], now: float) -> None:
        kind = str(job.get("kind") or "")
        member_name = job.get("member")
        member = team.member(str(member_name)) if isinstance(member_name, str) else None
        self.log("{}: job {} for {}".format(team.name, kind, member_name))
        if kind == "brief":
            if member is None:
                return
            if member.get("kind") == "human" or not member.get("terminal_id"):
                self.log("{}: brief job for {} refused: not an agent member with a terminal".format(team.name, member_name))
                return
            self._enqueue_briefing(team, member, now)
        elif kind == "toast":
            # The CLI never calls notification.show; it asks the daemon (create --new failures, plan 12).
            title = str(job.get("title") or "herdr-synapse {}".format(team.name))
            body = str(job.get("body") or "")
            seqs = [int(s) for s in job.get("seqs") or [] if isinstance(s, int)]
            self.enqueue_toast(team.name, seqs, title, body, str(job.get("sound") or "none"), kind=str(job.get("toast_kind") or "roster"))
        elif kind == "nudge":
            if member is None:
                return
            name = str(member["name"])
            pending = team.pending.get(name)
            if pending is None:
                unread = self._unread_mail_seqs(team, name, retry_terminal=bool(job.get("force")))
                if not unread:
                    self.log("{}: nothing unread for {}; nudge job dropped".format(team.name, name))
                    return
                pending = Pending(seqs=unread, first_ms=now)
                team.pending[name] = pending
            pending.force = pending.force or bool(job.get("force"))
            pending.urgent = pending.urgent or bool(job.get("force"))
            pending.next_eligible_ms = now
            pending.landed_ms = None
        elif kind == "probe":
            if member is None:
                return
            nonce = str(job.get("nonce") or new_nonce())
            agent_kind = str(job.get("agent_kind") or member.get("kind") or "")
            pending = Pending(first_ms=now, kind="probe", lines=[probe_text_for(nonce)], force=True, urgent=True)
            pending.probe = {"nonce": job.get("nonce"), "agent_kind": agent_kind, "requested_ms": now}
            existing = team.pending.get(str(member["name"]))
            if existing is not None and existing.kind in ("brief", "nudge") and existing.landed_ms is None:
                # The probe jumps the queue but must not lose the briefing or nudge already waiting
                # (observed live in M0: a probe sent while the briefing was held dropped the briefing).
                pending.resume = existing
            team.pending[str(member["name"])] = pending
        elif kind == "focus":
            if member is None or not isinstance(member.get("pane_id"), str):
                return
            self.api.request("agent.focus", {"target": member["pane_id"]}, timeout=5.0)
        elif kind == "control":
            self._start_control(team, job, now)
        elif kind == "say":
            self._run_say(team, job, now)
        elif kind == "mute":
            mute = store.read_json(team.paths.mute_json, default=None)
            if not isinstance(mute, dict):
                mute = {}
            key = str(job.get("member") or "*")
            if job.get("unmute"):
                mute.pop(key, None)
            else:
                mute[key] = job.get("until")
            store.write_json(team.paths.mute_json, mute, fsync=False)
        else:
            self.log("{}: unknown job kind {!r}".format(team.name, kind))

    def _enqueue_briefing(self, team: TeamState, member: Dict[str, Any], now: float) -> None:
        name = str(member["name"])
        charter = team.roster.get("charter") if isinstance(team.roster.get("charter"), dict) else None
        headline = None
        if charter and isinstance(charter.get("text"), str):
            headline = " ".join(charter["text"].split())
        teammates = [(str(m.get("name")), str(m.get("role") or ""), bool(m.get("manager")))
                     for m in team.members() if m.get("name") != name and m.get("kind") != "human"]
        brief = member.get("brief") if isinstance(member.get("brief"), str) else None
        try:
            lines = briefing_lines_for(name, str(member.get("role") or ""), team.name, headline, teammates, brief, self.cli_path)
        except (HerdrTeamError, ValueError) as err:
            # ``NudgeTextError`` is a ``ValueError``, not a ``HerdrTeamError``, and the
            # 400-char budget can genuinely overflow on long names with many teammates.
            # Dropping the optional parts still gets the member briefed; raising here
            # left it silently unbriefed forever.
            self.log("{}: full briefing for {} did not fit ({}); falling back to the minimal one".format(team.name, name, err))
            try:
                lines = briefing_lines_for(name, str(member.get("role") or ""), team.name, None, [], None, self.cli_path)
            except (HerdrTeamError, ValueError) as inner:
                self.log("{}: cannot brief {} at all: {}".format(team.name, name, inner))
                return
        pending = Pending(first_ms=now, kind="brief", lines=lines)
        existing = team.pending.get(name)
        if existing is not None and existing.kind == "nudge":
            pending.seqs = list(existing.seqs)
            pending.urgent = existing.urgent
            pending.authors = set(existing.authors)
        team.pending[name] = pending
        try:
            store.atomic_write(team.paths.briefing(name), ("\n".join(lines) + "\n").encode("utf-8"), fsync=False)
        except HerdrTeamError:
            pass

    # -- evaluation ---------------------------------------------------------------------

    def evaluate_pending(self) -> None:
        now = self.now_ms()
        for team in self.teams.values():
            for name in list(team.pending):
                pending = team.pending.get(name)
                if pending is None:
                    continue
                member = team.member(name)
                if member is None:
                    # An adopted rename re-keys the pending work to the new name (old names resolve for 10 min).
                    renamed = team.member_by_retired_name(name)
                    if renamed is not None and str(renamed.get("name")) not in team.pending:
                        new_name = str(renamed.get("name"))
                        team.pending[new_name] = team.pending.pop(name)
                        self.log("{}: pending work for {} follows the rename to {}".format(team.name, name, new_name))
                        member, name = renamed, new_name
                    else:
                        del team.pending[name]
                        continue
                if member.get("status") == "left":
                    # ``remove`` tombstones the member and clears its tokens, label and Herdr name.
                    # ``_evaluate_member``'s ``present`` check only pauses the TTL, so pending work
                    # for a tombstone never expires and could still be delivered (M8).
                    del team.pending[name]
                    team.runtime.pop(name, None)
                    self.log("{}: {} left the team; dropping its pending work".format(team.name, name))
                    self.who_dirty = True
                    continue
                try:
                    self._evaluate_member(team, member, pending, now)
                except Exception as err:  # noqa: BLE001 - F-07: the daemon must outlive one bad file/event
                    self.counters["phase_errors"] += 1
                    self.log("{}: evaluate {} failed: {}: {}".format(team.name, name, type(err).__name__, err))

    def _refresh_pending_seqs(self, team: TeamState, name: str, pending: Pending) -> int:
        """Gate 1: re-read the cursor and drop read (cursor or seen) and retracted seqs; returns the cursor."""
        cursor, seen = read_cursor_state(team.paths, name)
        if pending.kind == "nudge":
            pending.seqs = [s for s in pending.seqs if s > cursor and s not in seen and s not in team.retracted]
        return cursor

    def _stop_block_hold(self, team: TeamState, name: str, pending: Pending) -> Optional[str]:
        """Plan 12 / SK-08: a Claude Stop-hook block covering the pending seqs suppresses nudges for 10 min."""
        if pending.kind != "nudge" or not pending.seqs:
            return None
        from herdr_team import hooks as _hooks

        state = store.read_json(_hooks._stop_state_path(team.paths, name), default=None)
        if not isinstance(state, dict):
            return None
        blocked_seq = state.get("seq")
        if not isinstance(blocked_seq, int) or isinstance(blocked_seq, bool) or max(pending.seqs) > blocked_seq:
            return None
        stamps = [t for t in (_parse_iso(x) for x in (state.get("blocks") or [])) if t is not None]
        if not stamps:
            return None
        age = time.time() - max(stamps)
        if age < STOP_BLOCK_SUPPRESS_S:
            return "stop hook blocked at #{} {:.0f}s ago".format(blocked_seq, age)
        return None

    def _evaluate_member(self, team: TeamState, member: Dict[str, Any], pending: Pending, now: float) -> None:
        name = str(member["name"])
        rt = team.rt(name)
        # 1. cursor re-read
        cursor, seen = read_cursor_state(team.paths, name)
        if pending.kind == "nudge":
            before = list(pending.seqs)
            pending.seqs = [s for s in pending.seqs if s > cursor and s not in seen and s not in team.retracted]
            if pending.landed_ms is not None and any(s <= cursor or s in seen for s in before):
                latency = now - pending.landed_ms
                if pending.attempt_id:
                    team.ledger.record_outcome(pending.attempt_id, True, latency)
                    pending.attempt_id = None
                    self._maybe_verify_kind(team, str(member.get("kind")))
                self.log("{}: {} read up to #{} ({:.0f} ms after the nudge)".format(team.name, name, cursor, latency))
            if not pending.seqs:
                del team.pending[name]
                self.who_dirty = True
                return
            stop_block = self._stop_block_hold(team, name, pending)
            if stop_block is not None:
                self._note_hold(team, name, pending, gate.HOLD_STOP_BLOCKED, stop_block, now)
                return
        elif pending.kind == "control" and pending.landed_ms is not None:
            # The keystroke is in. Nothing is re-typed: a second ``/compact``
            # would land inside the compaction it is waiting for. The job is
            # closed by ``_control_observed`` when the effect shows up, and
            # only bounded here so it cannot sit open forever.
            if now - pending.landed_ms > CONTROL_OBSERVE_S * 1000.0:
                self._control_unobserved(team, name, pending, now)
            return
        elif pending.kind == "brief" and pending.landed_ms is not None:
            # Plan 9.2: an ack is a cursor write (``surfaced_by: cli``, or ``herdr-synapse ack``) made after
            # the landing with seq >= briefing_seq; an untouched cursor on an empty board is not one.
            try:
                cursor_doc = store.Cursors(team.paths).get(name)
            except HerdrTeamError:
                cursor_doc = {}
            written_since = cursor_doc.get("updated") is not None and cursor_doc.get("updated") != pending.brief_cursor_updated and cursor_doc.get("surfaced_by") != "hook"
            if rt.brief_seq is not None and cursor >= rt.brief_seq and written_since:
                del team.pending[name]
                self.log("{}: {} acknowledged the briefing".format(team.name, name))
                self._requeue_deferred(team, name, pending, cursor, now)
                return
            if now - pending.landed_ms > BRIEF_ACK_S * 1000.0:
                if not rt.rebriefed:
                    rt.rebriefed = True
                    pending.landed_ms = None
                    pending.next_eligible_ms = now
                    self.log("{}: {} did not ack the briefing; re-briefing once".format(team.name, name))
                else:
                    del team.pending[name]
                    self.enqueue_toast(team.name, [], "herdr-synapse {}: {} unbriefed".format(team.name, name), "{} never acknowledged its briefing".format(name), "none", kind="outcome")
                    self._requeue_deferred(team, name, pending, cursor, now)
                    return
            else:
                return
        agent = self.agents.get(str(member.get("terminal_id") or ""))
        present = agent is not None and member.get("status") == "active"
        # TTL: ``post_ttl_ms`` (30 min default, ``config.gate`` per team) of target-active time, paused while missing.
        if pending.last_tick_ms is not None and present:
            pending.active_ms += now - pending.last_tick_ms
        pending.last_tick_ms = now
        if pending.kind == "nudge" and pending.active_ms >= team.gate_config.post_ttl_ms:
            self._finish_pending(team, name, pending, "expired", "posts {} to {} expired unread".format(pending.seqs, name))
            return
        # landed but unread: re-nudge only after a completed turn and the schedule
        if pending.landed_ms is not None:
            stability = self.stability.get(str(member.get("terminal_id") or ""))
            if stability is not None and stability.idle_since_ms is not None and stability.idle_since_ms > pending.landed_ms:
                pending.turn_completed_since_landing = True
            if pending.renudges >= len(RENUDGE_AFTER_S):
                self._finish_pending(team, name, pending, "abandoned", "gave up nudging {} for {}".format(name, pending.seqs))
                return
            wait_s = RENUDGE_AFTER_S[pending.renudges]
            if now - pending.landed_ms < wait_s * 1000.0 or not pending.turn_completed_since_landing:
                # Only the one explicit follow-up (posts that arrived after the landing) may go earlier.
                if not (pending.kind == "nudge" and pending.follow_up_due and pending.attempts == 1):
                    return
        if now < pending.next_eligible_ms:
            return
        if pending.kind == "nudge" and pending.landed_ms is None and pending.last_added_ms is not None and now - pending.last_added_ms < team.gate_config.burst_window_ms:
            return  # let a same-second burst settle so one nudge covers the whole range (plan 12)
        if name in team.open_intents:
            # A prior daemon sent this without recording a result: count it as sent once.
            entry = team.open_intents.pop(name)
            pending.landed_ms = now
            pending.attempts = max(pending.attempts, int(entry.get("attempts") or 1))
            pending.landed_seq_max = max(pending.seqs) if pending.seqs else 0
            self.log("{}: open intent {} for {} counts as sent".format(team.name, entry.get("id"), name))
            return
        self._update_interrupt_state(team, name, str(member.get("kind")), pending, now)
        snapshot = self._snapshot(team, member, agent, rt, now, pending)
        pending_work = self._pending_work(team, pending, cursor)
        decision = gate_evaluate(snapshot, pending_work, now, self.global_last_nudge_ms, self._pair_exchanges(team, pending, name, now), config=team.gate_config)
        if not decision.deliver:
            # Gate 10 after the 5 min max-hold needs a detection read to judge the 3 s snapshot
            # stability; only then may the expensive phase run for a focused pane.
            max_hold_elapsed = decision.hold == gate.HOLD_FOCUSED and bool(decision.details.get("focus_max_hold_elapsed"))
            if not max_hold_elapsed:
                self._note_hold(team, name, pending, decision.hold, decision.detail, now)
                return
        # Cheap gates passed: fresh agent.get, explain, detection read, then the full gate once more.
        fresh = self.fresh_agent(str(member.get("pane_id")))
        if fresh is None:
            self._note_hold(team, name, pending, "absent", "agent.get failed", now)
            self.reconcile_due = True
            return
        from herdr_team import gate as gate_mod

        if not self._same_occupant(member, fresh):
            self._note_hold(team, name, pending, gate_mod.HOLD_KIND_MISMATCH, "occupant changed", now)
            self.reconcile_due = True
            return
        snapshot = self._snapshot(team, member, fresh, rt, now, pending)
        # Gate 6 (plan 8.2): the stable window is confirmed by this fresh ``agent.get``; log the seq
        # it saw against the polled one so a delivery can be audited from daemon.log alone (M5 ND-01).
        polled_seq = int(agent.get("state_change_seq") or 0) if agent is not None else None
        self.log("{}: {} agent.get re-check: state_change_seq {} -> {} ({}), stable for {:.0f} ms".format(
            team.name, name, polled_seq, snapshot.state_change_seq, snapshot.agent_status, now - (snapshot.stable_since_ms if snapshot.stable_since_ms is not None else now)))
        snapshot.explain = self._explain(str(member.get("pane_id")))
        snapshot.detection_text = self._read_detection(rt, str(member.get("pane_id")), self.now_ms())
        snapshot.detection_stable_since_ms = rt.detection_stable_since_ms
        snapshot.prompt_line = self._prompt_line(team, name, str(member.get("pane_id")), str(member.get("kind")), snapshot.detection_text)
        # Gate 1 again: three socket round trips passed; the member may have run board --new meanwhile
        # (register: "Board read between gate and send -> cursor re-read immediately before the prompt").
        cursor = self._refresh_pending_seqs(team, name, pending)
        if pending.kind == "nudge" and not pending.seqs:
            self.log("{}: {} read the pending posts during the gate; nothing to nudge".format(team.name, name))
            team.pending.pop(name, None)
            self.who_dirty = True
            return
        pending_work = self._pending_work(team, pending, cursor)
        decision = gate_evaluate(snapshot, pending_work, now, self.global_last_nudge_ms, self._pair_exchanges(team, pending, name, now), config=team.gate_config)
        if not decision.deliver:
            self._note_hold(team, name, pending, decision.hold, decision.detail or decision.matched_dialog_line, now)
            return
        pending.hold = None
        pending.hold_since_ms = None
        self._send(team, member, pending, snapshot, decision, now)

    def _update_interrupt_state(self, team: TeamState, name: str, kind: str, pending: Pending, now: float) -> None:
        """``armed`` when the interrupt may go into a running turn now; else why it waits for idle like an urgent nudge."""
        if not pending.interrupt or pending.kind != "nudge":
            pending.interrupt_state = None
            return
        cfg = team.gate_config
        if kind not in cfg.interrupt_kinds:
            state = "kind_not_allowed"
        elif "human" in pending.interrupt_authors:
            state = "armed"  # the operator is never in cooldown
        else:
            senders = sorted(pending.interrupt_authors)
            cooling = [a for a in senders if (a, name) in team.interrupts_sent and now - team.interrupts_sent[(a, name)] < float(cfg.interrupt_cooldown_ms)]
            state = "cooldown" if senders and len(cooling) == len(senders) else "armed"
        if state != pending.interrupt_state:
            why = "may be typed into the running turn" if state == "armed" else "{}; delivered as an urgent nudge once idle".format(state)
            self.log("{}: interrupt of {} by {}: {}".format(team.name, name, ",".join(sorted(pending.interrupt_authors)) or "?", why))
            pending.interrupt_state = state
            self.who_dirty = True

    def _pending_work(self, team: TeamState, pending: Pending, cursor: int) -> Any:
        from herdr_team import gate as gate_mod

        seqs = list(pending.seqs)
        force = bool(pending.force)
        if pending.kind in ("brief", "probe", "control"):
            # A briefing, probe or control keystroke has no board seq of its own; the gate still
            # needs one above the cursor, and plan 9.2 gates these on the stable window only
            # (no done_hold, no interval). Every other gate, idle included, still applies.
            force = True
            if not seqs:
                seqs = [cursor + 1]
        # Gate 1 sees the same target-active age the TTL check above uses (``config.gate.post_ttl_ms``).
        active_ms = pending.active_ms if pending.kind == "nudge" else None
        interrupt = bool(pending.interrupt and pending.kind == "nudge")
        manager = team.manager_name()
        return gate_mod.PendingWork(seqs, bool(pending.urgent or force), cursor, sorted(pending.authors), force=force, active_ms=active_ms,
                                    interrupt=interrupt, interrupt_ok=interrupt and pending.interrupt_state == "armed",
                                    from_manager=bool(manager and manager in pending.authors))

    def _pair_exchanges(self, team: TeamState, pending: Pending, name: str, now: float) -> int:
        """Exchanges between ``name`` and each author inside the team's ``pair_window_ms`` (gate 11 pair budget)."""
        best = 0
        window_ms = team.gate_config.pair_window_ms
        for author in pending.authors:
            key = (min(author, name), max(author, name))
            stamps = [t for t in self.pair_exchanges.get(key, []) if now - t < window_ms]
            self.pair_exchanges[key] = stamps
            best = max(best, len(stamps))
        return best

    def _note_hold(self, team: TeamState, name: str, pending: Pending, hold: Optional[str], detail: Optional[str], now: float) -> None:
        from herdr_team import gate as gate_mod

        if hold == gate_mod.HOLD_FOCUSED:
            if pending.focus_hold_since_ms is None:
                pending.focus_hold_since_ms = now  # gate 10: the 5 min max-hold clock starts here
        else:
            pending.focus_hold_since_ms = None
        if hold in (gate_mod.HOLD_NAME_MISMATCH, gate_mod.HOLD_KIND_MISMATCH, gate_mod.HOLD_ABSENT):
            self.reconcile_due = True  # adopt a rename, rebind, or mark missing before the next attempt
        if pending.hold != hold:
            pending.hold = hold
            pending.hold_since_ms = now
            pending.hold_toasted = False
            self.log("{}: {} held: {}{}".format(team.name, name, hold, (" ({})".format(detail) if detail else "")))
            self.who_dirty = True
        elif pending.hold_since_ms is not None and now - pending.hold_since_ms > DIALOG_TOAST_AFTER_S * 1000.0 and not pending.hold_toasted:
            if hold in (gate_mod.HOLD_DIALOG, gate_mod.HOLD_FOCUSED, gate_mod.HOLD_DRAFT_PRESENT, gate_mod.HOLD_BLOCKED):
                pending.hold_toasted = True
                self.enqueue_toast(team.name, pending.seqs, "herdr-synapse {}: {} waiting".format(team.name, name), "{} has been held for 10 min: {}".format(name, hold), "none", kind="outcome")
        if hold in (gate_mod.HOLD_PAIR_BUDGET, gate_mod.HOLD_KIND_MISMATCH):
            # Plan 8.2 gates 3 and 11 (``gate.TOAST_HOLDS``): the human learns about a ping-pong pause or a
            # kind mismatch once, coalesced per (member, reason) per hour (M5 ND-08: no toast was ever sent).
            rt = team.rt(name)
            last = rt.hold_toast_ms.get(hold)
            if last is None or now - last > KIND_UNVERIFIED_TOAST_S * 1000.0:
                rt.hold_toast_ms[hold] = now
                if hold == gate_mod.HOLD_PAIR_BUDGET:
                    title = "herdr-synapse {}: {} ping-pong paused".format(team.name, name)
                    body = "{} and {} hit the pair budget ({}); posts wait for the window".format(name, ", ".join(sorted(pending.authors)) or "a teammate", detail or "")
                else:
                    title = "herdr-synapse {}: {} not nudged".format(team.name, name)
                    body = "{}'s pane hosts another agent kind ({}); posts wait for a rebind".format(name, detail or "kind_mismatch")
                self.enqueue_toast(team.name, pending.seqs, title, body, "none", kind="outcome")
        if hold == gate_mod.HOLD_KIND_UNVERIFIED:
            rt = team.rt(name)
            if rt.kind_unverified_toast_ms is None or now - rt.kind_unverified_toast_ms > KIND_UNVERIFIED_TOAST_S * 1000.0:
                rt.kind_unverified_toast_ms = now
                self.enqueue_toast(team.name, pending.seqs, "herdr-synapse {}: {} not nudged".format(team.name, name), "kind {} is unverified; posts wait for the next read".format(team.member(name).get("kind") if team.member(name) else "?"), "none", kind="outcome")
        pending.next_eligible_ms = max(pending.next_eligible_ms, now + HOLD_REEVALUATE_S * 1000.0 if hold in (gate_mod.HOLD_DIALOG, gate_mod.HOLD_DRAFT_PRESENT, gate_mod.HOLD_FOCUSED, gate_mod.HOLD_SKIP_STATE_UPDATE, gate_mod.HOLD_VISIBLE_BLOCKER, gate_mod.HOLD_STOP_BLOCKED) else now)

    def _snapshot(self, team: TeamState, member: Dict[str, Any], agent: Optional[Dict[str, Any]], rt: MemberRuntime, now: float, pending: Optional[Pending] = None) -> Any:
        from herdr_team import gate as gate_mod

        terminal_id = member.get("terminal_id") if isinstance(member.get("terminal_id"), str) else None
        stability = self.stability.get(terminal_id or "")
        mute = read_mute(team.paths)
        until = muted_until(mute, str(member.get("name")), time.time())
        muted_ms: Optional[float] = None
        if until is not None:
            muted_ms = float("inf") if until == float("inf") else now + max(0.0, (until - time.time()) * 1000.0)
        # A probe is the human-requested verification round trip itself, so gate 4 does not apply to it.
        verified = bool(member.get("verified_kind")) or self._kind_trusted(str(member.get("kind"))) or bool(pending is not None and pending.kind == "probe")
        return gate_mod.MemberSnapshot(
            name=str(member.get("name")),
            kind=str(member.get("kind")),
            terminal_id=terminal_id,
            pane_id=agent.get("pane_id") if agent else None,
            agent_kind=agent.get("agent") if agent else None,
            live_name=agent.get("name") if agent and isinstance(agent.get("name"), str) else None,
            focus_hold_since_ms=pending.focus_hold_since_ms if pending is not None else None,
            detection_stable_since_ms=rt.detection_stable_since_ms,
            dialog_hold_since_ms=pending.hold_since_ms if pending is not None and pending.hold == gate_mod.HOLD_DIALOG else None,
            agent_status=str(agent.get("agent_status") or "unknown") if agent else "unknown",
            state_change_seq=int(agent.get("state_change_seq") or 0) if agent else -1,
            launch_pending=bool(agent.get("launch_pending")) if agent else False,
            focused=bool(agent.get("focused")) if agent else False,
            screen_detection_skipped=bool(agent.get("screen_detection_skipped")) if agent else False,
            delivery=str(member.get("delivery") or "nudge"),
            verified_kind=verified,
            stable_since_ms=stability.since_ms if stability else None,
            idle_since_ms=stability.idle_since_ms if stability else None,
            explain=None,
            detection_text=None,
            prompt_line=None,
            muted_until=muted_ms,
            in_flight=rt.in_flight,
            last_nudge_ms=rt.last_nudge_ms,
            pane_stuck_until_ms=rt.pane_stuck_until_ms,
            last_nudge_cursor_seq=rt.last_nudge_cursor_seq,
            follow_up_used=bool(pending.follow_up_used) if pending is not None else False,
        )

    def _kind_trusted(self, kind: str) -> bool:
        """``kinds.json[kind]``: ``verified`` (20 clean round trips), ``trusted`` (owner override), or a passed probe."""
        return roster.kind_trusted(store.read_json(self.session.kinds_json, default=None), kind)

    def _maybe_verify_kind(self, team: TeamState, kind: str) -> None:
        """Plan 8.3: a kind becomes verified after 20 round trips at >= 90 % clean; recorded in kinds.json."""
        if not kind or self._kind_trusted(kind):
            return
        try:
            rate = team.ledger.clean_rate(kind, KIND_VERIFY_WINDOW)
        except HerdrTeamError:
            return
        if rate is None or rate < KIND_VERIFY_CLEAN_RATE:
            return
        doc = store.read_json(self.session.kinds_json, default=None)
        if not isinstance(doc, dict):
            doc = {}
        entry = doc.get(kind) if isinstance(doc.get(kind), dict) else {}
        entry["verified"] = True
        entry["verified_by"] = "ledger"
        entry["clean_rate"] = round(rate, 3)
        entry["verified_at"] = now_iso()
        doc[kind] = entry
        try:
            store.write_json(self.session.kinds_json, doc, fsync=False)
        except HerdrTeamError as err:
            self.log("kinds.json write failed: {}".format(err))
            return
        self.log("kind {} verified: {:.0%} clean over the last {} round trips".format(kind, rate, KIND_VERIFY_WINDOW))
        self.who_dirty = True

    def _same_occupant(self, member: Dict[str, Any], agent: Dict[str, Any]) -> bool:
        return agent.get("terminal_id") == member.get("terminal_id") and agent.get("agent") == member.get("kind")

    def _explain(self, pane_id: str) -> Optional[Dict[str, Any]]:
        try:
            result = self.api.request("agent.explain", {"target": pane_id}, timeout=5.0)
        except HerdrTeamError:
            return None
        explain = result.get("explain") if isinstance(result, dict) else None
        return explain if isinstance(explain, dict) else None

    def _detection_text(self, pane_id: str) -> Optional[str]:
        try:
            result = self.api.request("agent.read", {"target": pane_id, "source": "detection", "format": "text"}, timeout=5.0)
        except HerdrTeamError:
            return None
        return read_text(result)  # ``pane_read``: ``read.text`` (a legacy top-level ``text`` still works)

    def _visible_ansi(self, pane_id: str) -> Optional[str]:
        """``agent.read --source visible --format ansi``: the only read that carries styling (the detection source is plain)."""
        try:
            result = self.api.request("agent.read", {"target": pane_id, "source": "visible", "format": "ansi"}, timeout=5.0)
        except HerdrTeamError:
            return None
        return read_text(result)

    def _prompt_line(self, team: TeamState, name: str, pane_id: str, kind: str, detection_text: Optional[str]) -> Optional[str]:
        """Gate 9 input: the typed draft, with Claude's faint prompt suggestion (ghost text) excluded.

        The plain detection text cannot tell a suggestion from a draft; when
        it shows one, a second read of the visible viewport with styling
        decides (``gate.styled_prompt_line_text``). Only that case costs the
        extra round trip; an empty or unknown prompt line never does.
        """
        draft = gate.prompt_line_text(detection_text, kind)
        if not draft or not draft.strip() or kind != "claude":
            return draft
        typed = gate.styled_prompt_line_text(self._visible_ansi(pane_id), kind)
        if typed is None:
            return draft
        if not typed.strip():
            self.log("{}: {} prompt suggestion (faint ghost text) is not a draft: {!r}".format(team.name, name, draft.strip().splitlines()[0][:40]))
        return typed

    def _read_detection(self, rt: MemberRuntime, pane_id: str, now: float) -> Optional[str]:
        """Detection read plus the gate 10 stability clock: the digest's unchanged-since time."""
        text = self._detection_text(pane_id)
        digest = gate.detection_hash(text) if text is not None else None
        if digest != rt.detection_hash:
            rt.detection_hash = digest
            rt.detection_stable_since_ms = now if digest is not None else None
        elif rt.detection_stable_since_ms is None and digest is not None:
            rt.detection_stable_since_ms = now
        return text

    # -- delivery ---------------------------------------------------------------------------

    def _assert_roster_terminal(self, team: TeamState, member: Dict[str, Any], agent: Dict[str, Any]) -> bool:
        """The hard rule: the target must be this member's roster terminal, in the pane the roster knows.

        A terminal outside the roster counts as ``wrong_target`` (must stay
        zero). A roster terminal that moved to another pane is refused too,
        without the counter: reconcile adopts the new pane id first.
        """
        terminal_id = agent.get("terminal_id")
        roster_terminal = member.get("terminal_id")
        ok = isinstance(terminal_id, str) and terminal_id == roster_terminal and team.member_by_terminal(terminal_id) is not None
        if not ok:
            self.counters["wrong_target"] += 1
            team.ledger.record_wrong_target(str(member.get("name")), terminal_id if isinstance(terminal_id, str) else None, agent.get("pane_id"), "terminal not in roster")
            self.log("{}: REFUSED to prompt {} (terminal {} is not {}'s roster terminal)".format(team.name, agent.get("pane_id"), terminal_id, member.get("name")))
            return False
        if agent.get("pane_id") != member.get("pane_id"):
            self.log("{}: REFUSED to prompt {}: {} now hosts {} but the roster says {}; reconciling first".format(team.name, member.get("name"), agent.get("pane_id"), terminal_id, member.get("pane_id")))
            self.reconcile_due = True
            return False
        return True

    def _send(self, team: TeamState, member: Dict[str, Any], pending: Pending, snapshot: Any, decision: Any, now: float) -> None:
        name = str(member["name"])
        rt = team.rt(name)
        agent = self.agents.get(str(member.get("terminal_id") or ""))
        if agent is None or not self._assert_roster_terminal(team, member, agent):
            return
        # Last guard before the prompt: the cursor may have moved since the gate read it.
        pending.gate_cursor = self._refresh_pending_seqs(team, name, pending)
        if pending.kind == "nudge" and not pending.seqs:
            self.log("{}: {} read the pending posts right before the prompt; nothing to nudge".format(team.name, name))
            team.pending.pop(name, None)
            self.who_dirty = True
            return
        if pending.kind == "control":
            self._send_control(team, member, pending, snapshot, now)
            return
        interrupting = pending.kind == "nudge" and bool(decision.details.get("interrupt"))
        if pending.kind in ("brief", "probe"):
            lines = list(pending.lines or [])
        elif interrupting:
            lines = [interrupt_text_for(name, pending.seqs, new_nonce(), interrupt_sender(pending))]
        else:
            lines = [nudge_text_for(name, pending.seqs, new_nonce())]
        pending.interrupt_sent = interrupting
        pending.attempts += 1
        pending.gate_seq = snapshot.state_change_seq
        attempt_id = "{}-{}-{}".format(name, int(time.time() * 1000), pending.attempts)
        attempt = Attempt(
            id=attempt_id, member=name, kind=str(member.get("kind")), seqs=list(pending.seqs),
            hook_authority=bool(snapshot.screen_detection_skipped), weak_idle=bool(decision.weak_idle), focused=bool(snapshot.focused),
            prompt_line_empty=not snapshot.prompt_line, gate_ms=max(0.0, now - (snapshot.stable_since_ms or now)),
            queue_ms=max(0.0, now - pending.first_ms), attempts=pending.attempts,
            manifest_source=(snapshot.explain or {}).get("manifest_source") if isinstance(snapshot.explain, dict) else None,
            extra={"delivery": "interrupt" if interrupting else pending.kind, "interrupt_by": sorted(pending.interrupt_authors) if interrupting else None,
                   "stable_ms_required": decision.stable_ms_required, "pane_id": snapshot.pane_id, "terminal_id": snapshot.terminal_id},
        )
        team.ledger.record_intent(attempt)
        pending.attempt_id = attempt_id
        rt.in_flight = True
        result = RESULT_TRANSIENT
        details: Dict[str, Any] = {}
        try:
            for index, line in enumerate(lines):
                result, details = self.deliver_line(team, member, line, snapshot.state_change_seq, follow_on=index > 0, pane_id=str(agent.get("pane_id")), in_turn=interrupting)
                if result not in (RESULT_LANDED_WORKING, RESULT_DRY):
                    break
        finally:
            rt.in_flight = False
        team.ledger.record_result(attempt_id, result, details)
        self._apply_result(team, member, pending, result, details, now, follow_up=bool(decision.details.get("follow_up")))

    def deliver(self, member: str, text_lines: List[str], seqs: List[int]) -> str:
        """``agent.prompt`` each line for a roster member (by name, any team); returns the last result."""
        for team in self.teams.values():
            doc = team.member(member)
            if doc is None:
                continue
            agent = self.agents.get(str(doc.get("terminal_id") or "")) or self.fresh_agent(str(doc.get("pane_id")))
            if agent is None or not self._assert_roster_terminal(team, doc, agent):
                return RESULT_WRONG_OCCUPANT
            seq = int(agent.get("state_change_seq") or 0)
            result = RESULT_TRANSIENT
            for index, line in enumerate(text_lines):
                result, _details = self.deliver_line(team, doc, line, seq, follow_on=index > 0, pane_id=str(agent.get("pane_id")))
                if result not in (RESULT_LANDED_WORKING, RESULT_DRY):
                    break
            return result
        raise HerdrTeamError("member_not_found", "{} is not in any roster".format(member), EXIT_REFUSED)

    def deliver_line(self, team: TeamState, member: Dict[str, Any], text: str, gate_seq: int, follow_on: bool = False, pane_id: Optional[str] = None, in_turn: bool = False) -> Tuple[str, Dict[str, Any]]:
        """One ``agent.prompt``; classify the result per plan 8.3.

        The first line of a job waits for ``working``/``blocked``. A
        ``follow_on`` line (the second briefing line, plan 9.2) is typed right
        after the first one started the turn: the PTY actor queues it behind
        that Enter, so it is sent without ``wait`` and counts as landed when
        the server accepted it for the same occupant. An ``in_turn`` line (an
        allowed interrupt) goes into a member that is already working, so it
        is sent without ``wait`` too: Herdr's wait needs a state *change*,
        and a working member makes none (live 2026-09-06: the wait timed out
        after 8 s and the retry typed the interrupt a second time).
        """
        name = str(member["name"])
        pane_id = pane_id or str(member.get("pane_id"))
        if self.dry_nudge:
            self.log("{}: DRY nudge to {} ({}): {}".format(team.name, name, pane_id, text))
            self.counters["nudges"] += 1
            return RESULT_DRY, {"text": text}
        params: Dict[str, Any] = {"target": pane_id, "text": text}
        if not follow_on and not in_turn:
            params["wait"] = {"until": ["working", "blocked"], "timeout_ms": 8000}
        # The call duration is real I/O time, not a scheduling window: measure it on the wall monotonic clock.
        t0 = time.monotonic()
        try:
            response = self.api.request("agent.prompt", params, timeout=PROMPT_TIMEOUT_S)
        except HerdrTeamError as err:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            return self._classify_error(team, member, err, elapsed_ms, text)
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        self.counters["nudges"] += 1
        agent = response.get("agent") if isinstance(response, dict) else None
        if not isinstance(agent, dict):
            return RESULT_LANDED_WORKING, {"elapsed_ms": elapsed_ms, "note": "no agent snapshot"}
        if agent.get("terminal_id") != member.get("terminal_id") or agent.get("agent") != member.get("kind"):
            return RESULT_WRONG_OCCUPANT, {"elapsed_ms": elapsed_ms, "terminal_id": agent.get("terminal_id"), "agent": agent.get("agent")}
        status = str(agent.get("agent_status") or "")
        seq = int(agent.get("state_change_seq") or 0)
        self._track_stability(agent, self.now_ms())
        if follow_on:
            return RESULT_LANDED_WORKING, {"elapsed_ms": elapsed_ms, "status": status, "seq": seq, "gate_seq": gate_seq, "note": "follow-on line queued behind the first"}
        if in_turn:
            return RESULT_LANDED_WORKING, {"elapsed_ms": elapsed_ms, "status": status, "seq": seq, "gate_seq": gate_seq, "note": "typed into the running turn"}
        if status in ("idle", "done"):
            return RESULT_TRANSIENT, {"elapsed_ms": elapsed_ms, "status": status, "note": "no transition observed"}
        if status == "blocked":
            # The text landed and the agent went straight to a dialog: delivered, the human sees the dialog.
            return RESULT_LANDED_WORKING, {"elapsed_ms": elapsed_ms, "status": status, "seq": seq, "gate_seq": gate_seq, "note": "blocked after landing"}
        if seq == gate_seq or elapsed_ms < self.landed_fast_ms:
            # A working status with the gate's seq, or a wait that returned before the 300 ms
            # text-to-Enter window could even elapse, means the turn was already running.
            return RESULT_LANDED_IN_TURN, {"elapsed_ms": elapsed_ms, "status": status, "seq": seq, "gate_seq": gate_seq}
        return RESULT_LANDED_WORKING, {"elapsed_ms": elapsed_ms, "status": status, "seq": seq, "gate_seq": gate_seq}

    def _classify_error(self, team: TeamState, member: Dict[str, Any], err: HerdrTeamError, elapsed_ms: float, text: str) -> Tuple[str, Dict[str, Any]]:
        code = err.code
        message = (err.message or "").lower()
        details: Dict[str, Any] = {"elapsed_ms": elapsed_ms, "code": code, "message": err.message}
        if code == "herdr_timeout":
            return RESULT_HUNG, details
        if code == "agent_prompt_stalled":
            self._interruptible_sleep(STALLED_READ_DELAY_S)
            detection = self._detection_text(str(member.get("pane_id")))
            line = gate.prompt_line_text(detection, str(member.get("kind")))
            if line and line.strip() and line.strip()[:20] in text:
                details["note"] = "text still on the prompt line"
                return RESULT_NOT_SUBMITTED, details
            details["note"] = "fast turn"
            return RESULT_LANDED_WORKING, details
        if code == "agent_prompt_failed":
            if "full" in message and "not accepting" not in message:
                details["pane_stuck"] = True
                return RESULT_HUNG, details
            return RESULT_TRANSIENT, details
        if code == "agent_not_ready":
            details["busy"] = "foreground" in message
            return RESULT_TRANSIENT, details
        if code == "agent_blocked":
            return RESULT_REFUSED, details
        if code in ("agent_not_found", "agent_not_running"):
            self.reconcile_due = True
            return RESULT_TRANSIENT, details
        if code in ("server_not_running", "herdr_protocol"):
            self.ping_failures += 1
            return RESULT_TRANSIENT, details
        details["unknown_code"] = True
        return RESULT_TRANSIENT, details

    def _apply_result(self, team: TeamState, member: Dict[str, Any], pending: Pending, result: str, details: Dict[str, Any], now: float, follow_up: bool = False) -> None:
        name = str(member["name"])
        rt = team.rt(name)
        self.log("{}: {} -> {} {}".format(team.name, name, result, json.dumps(details, ensure_ascii=False)[:300]))
        pending.follow_up_due = False
        if result in (RESULT_LANDED_WORKING, RESULT_DRY):
            rt.last_nudge_ms = now
            self.global_last_nudge_ms = now
            pending.landed_ms = now
            # Gate 11: the cursor at this landing; a follow-up spends the one exception, a regular landing
            # starts a new schedule and restores it.
            previous_cursor = rt.last_nudge_cursor_seq
            rt.last_nudge_cursor_seq = pending.gate_cursor
            pending.follow_up_used = bool(follow_up)
            if follow_up:
                self.log("{}: {} follow-up nudge inside the interval (cursor {} -> {}); the exception is spent".format(team.name, name, previous_cursor, pending.gate_cursor))
            pending.landed_seq_max = max(pending.seqs) if pending.seqs else 0
            pending.turn_completed_since_landing = False
            pending.transient_failures = 0
            pending.focus_hold_since_ms = None  # a re-nudge on a focused pane waits the full max-hold again
            if pending.attempts > 1 and not follow_up:
                pending.renudges += 1  # the one immediate follow-up is not a re-nudge on the schedule
            for author in pending.authors:
                key = (min(author, name), max(author, name))
                self.pair_exchanges.setdefault(key, []).append(now)
            if pending.kind == "brief":
                rt.brief_landed_ms = now
                rt.brief_seq = _board_max_seq(team.paths)
                # Per briefing, not per daemon. Nothing ever put this back, so
                # the second clear or compaction of a member's life got one
                # attempt at an unacknowledged briefing and then gave up.
                rt.rebriefed = False
                try:
                    pending.brief_cursor_updated = store.Cursors(team.paths).get(name).get("updated")
                except HerdrTeamError:
                    pending.brief_cursor_updated = None
                self._apply_changes(team, [(name, {"briefed_at": now_iso(), "briefing_seq": rt.brief_seq})])
                self.log("{}: briefing landed for {}".format(team.name, name))
            elif pending.kind == "probe":
                self._record_probe(team, member, pending, result, details, now)
                self._finish_probe(team, name, pending, now)
            else:
                seq_list = ", ".join("#{}".format(s) for s in pending.seqs)
                nudged_extra: Dict[str, Any] = {"seqs": list(pending.seqs)}
                text = "nudged {} for {}".format(name, seq_list)
                if pending.interrupt_sent:
                    senders = sorted(pending.interrupt_authors)
                    nudged_extra.update({"interrupt": True, "interrupt_by": senders})
                    text = "interrupted {} for {} (by {})".format(name, seq_list, ", ".join(senders) or "?")
                    for sender in senders:
                        if sender != "human":
                            team.interrupts_sent[(sender, name)] = now
                    pending.interrupt = False  # a re-nudge, if one is needed, follows the ordinary schedule
                    pending.interrupt_state = None
                self._append_system(team, "nudged", text, [name], nudged_extra)
            if result == RESULT_DRY and pending.kind == "nudge":
                pass
            self.who_dirty = True
            return
        if pending.kind == "probe" and (result in (RESULT_HUNG, RESULT_REFUSED, RESULT_WRONG_OCCUPANT) or pending.attempts >= 3):
            self._record_probe(team, member, pending, result, details, now)
            self._finish_probe(team, name, pending, now)
            return
        if result == RESULT_HUNG:
            rt.pane_stuck_until_ms = now + PANE_STUCK_S * 1000.0
            pending.next_eligible_ms = now + PANE_STUCK_S * 1000.0
            return
        if result == RESULT_REFUSED:
            pending.next_eligible_ms = now + HOLD_REEVALUATE_S * 1000.0
            return
        if result == RESULT_WRONG_OCCUPANT:
            # Re-resolve the member before any retry, and back off like a transient failure.
            self.reconcile_due = True
        # transient, landed_in_turn, not_submitted, wrong_occupant: back off 3 .. 60 s
        pending.transient_failures += 1
        backoff = min(TRANSIENT_BACKOFF_MAX_S, TRANSIENT_BACKOFF_MIN_S * (2 ** (pending.transient_failures - 1)))
        pending.next_eligible_ms = now + backoff * 1000.0
        if result == RESULT_LANDED_IN_TURN:
            # The text landed inside a turn: treat as sent but unread; re-nudge on the schedule.
            rt.last_nudge_ms = now
            self.global_last_nudge_ms = now

    # -- say: a human line typed into a member now (docs/cli.md section 7) ------------------------

    def _run_say(self, team: TeamState, job: Dict[str, Any], now: float) -> None:
        """Type the ``direct`` record named by the job into its member right now, or record why not.

        Checks kept in both modes: the record's origin, the roster terminal and
        occupant, kind trust, a blocked or unknown state, an open dialog or
        overlay, a draft on the prompt line. ``force`` (the console's ``!!``)
        bypasses exactly ``working`` and ``muted``. Every refusal is a
        ``typed`` board record for the human; nothing is dropped silently.
        The prompt is sent without ``wait`` and confirmed by ``confirm_says``.
        """
        force = bool(job.get("force"))
        requested_name = str(job.get("member") or "")
        seq = job.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
            self.log("{}: say job for {} names no board seq; dropped".format(team.name, requested_name))
            return
        member = team.member(requested_name) or team.member_by_retired_name(requested_name)
        name = str(member.get("name")) if member is not None else requested_name

        def refuse(reason: str, detail: Optional[str] = None) -> None:
            self._say_outcome(team, seq, name, "refused", reason, detail, force, requested_by=job.get("requested_by"))

        requested_at = _parse_iso(job.get("requested_at")) if isinstance(job.get("requested_at"), str) else None
        if requested_at is None or time.time() - requested_at > SAY_MAX_AGE_S:
            return refuse("stale", "job requested at {}; older than {:.0f} s".format(job.get("requested_at"), SAY_MAX_AGE_S))
        try:
            rec = store.BoardStore(team.paths).get(seq)
        except HerdrTeamError:
            rec = None
        from herdr_team import cmd_board as _console_registry

        console_terminal = set(_console_registry.live_console_entries(self.session))
        problem = say_source_problem(rec, requested_name, console_terminal)
        if problem is not None and name != requested_name:
            problem = say_source_problem(rec, name, console_terminal)  # the member was renamed since the CLI wrote both
        if problem is not None:
            self.counters["unverified_skipped"] += 1
            return refuse("unverified_source", problem)
        assert isinstance(rec, dict)
        text = str(rec["text"])
        if member is None or member.get("kind") == "human":
            return refuse("member_not_found", "{} is not an agent member of {}".format(requested_name, team.name))
        pane_id = member.get("pane_id")
        terminal_id = member.get("terminal_id")
        if not isinstance(pane_id, str) or not terminal_id:
            return refuse("absent", "{} has no pane or terminal on the roster".format(name))
        kind = str(member.get("kind") or "")
        if not (bool(member.get("verified_kind")) or self._kind_trusted(kind)):
            return refuse("kind_unverified", "kind {} is not trusted; run: herdr-synapse kinds trust {}".format(kind, kind))
        rt = team.rt(name)
        if rt.in_flight or name in team.say_inflight:
            return refuse("in_flight", "another line is being typed into {}".format(name))
        fresh = self.fresh_agent(pane_id)
        if fresh is None:
            self.reconcile_due = True
            return refuse("absent", "agent.get found no agent at {}".format(pane_id))
        if not self._same_occupant(member, fresh):
            self.reconcile_due = True
            return refuse("wrong_occupant", "{} now hosts {} {}".format(pane_id, fresh.get("agent"), fresh.get("terminal_id")))
        if not self._assert_roster_terminal(team, member, fresh):
            return refuse("wrong_target", "terminal {} is not {}'s roster terminal".format(fresh.get("terminal_id"), name))
        until = muted_until(read_mute(team.paths), name, time.time())
        if until is not None and not force:
            return refuse("muted", "{} is muted{}".format(name, "" if until == float("inf") else " until " + time.strftime("%H:%M:%SZ", time.gmtime(until))))
        status = str(fresh.get("agent_status") or "unknown")
        if status == "blocked":
            return refuse("blocked", "agent status blocked")
        if status == "unknown":
            return refuse("unknown", "agent status unknown: detection cannot tell what is on screen")
        if bool(fresh.get("launch_pending")):
            return refuse("not_ready", "launch pending")
        if status not in gate.IDLE_STATUSES and not force:
            return refuse("working", "agent status {}".format(status))
        explain = self._explain(pane_id)
        if isinstance(explain, dict):
            rule = gate._rule_id(explain) if hasattr(gate, "_rule_id") else (explain.get("rule_id") or explain.get("state"))
            if explain.get("state") == "blocked" or explain.get("visible_blocker"):
                return refuse("blocked", "explain: {}".format(rule))
            skipped_reason = explain.get("skipped_update_reason")
            if explain.get("skip_state_update") or skipped_reason:
                return refuse("skip_state_update", str(skipped_reason or rule or "an overlay is open"))
        detection = self._read_detection(rt, pane_id, now)
        line = gate_dialog_line(detection, kind)
        if line is not None:
            return refuse("dialog", line)
        draft = self._prompt_line(team, name, pane_id, kind, detection)
        if draft and draft.strip():
            return refuse("draft", draft.strip().splitlines()[0][:40])
        gate_seq = int(fresh.get("state_change_seq") or 0)
        if not force and self.atomic_idle_prompt is False:
            return refuse(
                "capability_unavailable",
                capabilities.unavailable_detail(name),
            )
        force_verified: Optional[bool] = (kind in FORCE_VERIFIED_KINDS) if force else None
        attempt_id = "say-{}-{}".format(name, int(time.time() * 1000))
        attempt = Attempt(
            id=attempt_id, member=name, kind=kind, seqs=[seq],
            hook_authority=bool(fresh.get("screen_detection_skipped")), weak_idle=gate_is_weak_idle(explain), focused=bool(fresh.get("focused")),
            prompt_line_empty=True, gate_ms=0.0, queue_ms=max(0.0, (time.time() - requested_at) * 1000.0), attempts=1,
            manifest_source=explain.get("manifest_source") if isinstance(explain, dict) else None,
            extra={"delivery": "say", "force": force, "requested_by": job.get("requested_by"), "pane_id": pane_id, "terminal_id": terminal_id, "status_before": status},
        )
        team.ledger.record_intent(attempt)
        if self.dry_nudge:
            self.log("{}: DRY say to {} ({}): {}".format(team.name, name, pane_id, text))
            self.counters["says"] += 1
            team.ledger.record_result(attempt_id, RESULT_DRY, {"text": text})
            self._say_outcome(team, seq, name, "typed", "dry", None, force, attempt_id=attempt_id, kind=kind, force_verified=force_verified, requested_by=job.get("requested_by"))
            return
        t0 = time.monotonic()
        try:
            if force:
                self.api.request("agent.prompt", {"target": pane_id, "text": text}, timeout=SAY_PROMPT_TIMEOUT_S)
            else:
                self.api.request(
                    "agent.prompt_if_idle",
                    {
                        "target": pane_id,
                        "text": text,
                        "expected_terminal_id": str(fresh.get("terminal_id") or ""),
                        "expected_state_change_seq": gate_seq,
                    },
                    timeout=SAY_PROMPT_TIMEOUT_S,
                )
        except HerdrTeamError as err:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            result, reason, detail, ledger_result = self._classify_say_error(rt, err, now, guarded=not force, member=name)
            team.ledger.record_result(attempt_id, ledger_result, {"elapsed_ms": elapsed_ms, "code": err.code, "message": err.message})
            self._say_outcome(team, seq, name, result, reason, detail, force, attempt_id=attempt_id, elapsed_ms=elapsed_ms, kind=kind, force_verified=force_verified, requested_by=job.get("requested_by"))
            return
        if not force:
            self.atomic_idle_prompt = True
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        self.counters["says"] += 1
        self.global_last_nudge_ms = now
        rt.in_flight = True
        team.say_inflight[name] = SayState(seq=seq, attempt_id=attempt_id, sent_ms=now, gate_seq=gate_seq, status_before=status, force=force, text=text, kind=kind, pane_id=pane_id, requested_by=job.get("requested_by"), prompt_ms=elapsed_ms)
        self.who_dirty = True
        self.log("{}: say #{} typed into {} ({}) in {:.0f} ms; confirming".format(team.name, seq, name, pane_id, elapsed_ms))

    def _classify_say_error(self, rt: MemberRuntime, err: HerdrTeamError, now: float, guarded: bool = False, member: Optional[str] = None) -> Tuple[str, str, str, str]:
        """``(result, reason, detail, ledger_result)`` for a prompt error on a say (no wait, so no stall code)."""
        code = err.code
        message = (err.message or "").lower()
        detail = "{}: {}".format(code, err.message)
        method_missing = code in ("unknown_method", "unsupported_method", "invalid_request", "method_not_found")
        # An old server answers an unrecognised method with id "".  The normal
        # request path reports that as an id mismatch; the connect-time raw
        # probe normally prevents this fallback from being needed.
        method_missing = method_missing or (
            code == "herdr_protocol" and self.atomic_idle_prompt is not True and "response id mismatch" in message
        )
        if guarded and method_missing:
            self.atomic_idle_prompt = False
            return (
                "refused",
                "capability_unavailable",
                capabilities.unavailable_detail(member, detail),
                RESULT_REFUSED,
            )
        if code == "agent_not_idle":
            return "refused", "working", detail, RESULT_REFUSED
        if code == "agent_state_changed":
            return "refused", "state_changed", detail, RESULT_TRANSIENT
        if code == "agent_changed":
            self.reconcile_due = True
            return "refused", "wrong_occupant", detail, RESULT_WRONG_OCCUPANT
        if code == "agent_blocked":
            return "refused", "blocked", detail, RESULT_REFUSED
        if code == "agent_not_ready":
            return "refused", "not_ready", detail, RESULT_TRANSIENT
        if code in ("agent_not_found", "agent_not_running"):
            self.reconcile_due = True
            return "refused", "absent", detail, RESULT_TRANSIENT
        if code == "herdr_timeout" or (code == "agent_prompt_failed" and "full" in message and "not accepting" not in message):
            rt.pane_stuck_until_ms = now + PANE_STUCK_S * 1000.0
            return "failed", "hung", detail, RESULT_HUNG
        if code in ("server_not_running", "herdr_protocol"):
            self.ping_failures += 1
        return "failed", "transient", detail, RESULT_TRANSIENT

    def confirm_says(self, now: float) -> None:
        """Classify typed lines from the agent poll: working or a new state seq is typed, blocked is typed into a dialog, idle after the window is not."""
        for team in self.teams.values():
            for name in list(team.say_inflight):
                state = team.say_inflight[name]
                agent = self.agents_by_pane.get(state.pane_id)
                status = str(agent.get("agent_status") or "unknown") if agent else "unknown"
                seq_now = int(agent.get("state_change_seq") or 0) if agent else -1
                if agent is not None and (status == "working" or seq_now > state.gate_seq):
                    in_turn = state.status_before not in gate.IDLE_STATUSES
                    self._finish_say(team, name, state, "typed", "in_turn" if in_turn else None, None, RESULT_LANDED_IN_TURN if in_turn else RESULT_LANDED_WORKING, now)
                elif agent is not None and status == "blocked":
                    self._finish_say(team, name, state, "typed", None, "{} now shows a dialog".format(name), RESULT_LANDED_WORKING, now)
                elif now - state.sent_ms >= SAY_CONFIRM_S * 1000.0:
                    detection = self._detection_text(state.pane_id)
                    line = gate.prompt_line_text(detection, state.kind)
                    if line and line.strip() and line.strip()[:20] in state.text:
                        self._finish_say(team, name, state, "not_submitted", "not_submitted", "the text is on the prompt line; press Enter in the pane", RESULT_NOT_SUBMITTED, now)
                    else:
                        self._finish_say(team, name, state, "failed", "unconfirmed", "no transition observed within {:.0f} s".format(SAY_CONFIRM_S), RESULT_TRANSIENT, now)

    def _finish_say(self, team: TeamState, name: str, state: SayState, result: str, reason: Optional[str], detail: Optional[str], ledger_result: str, now: float) -> None:
        elapsed_ms = max(0.0, now - state.sent_ms) + state.prompt_ms
        team.ledger.record_result(state.attempt_id, ledger_result, {"elapsed_ms": elapsed_ms, "status_before": state.status_before, "reason": reason, "detail": detail})
        force_verified: Optional[bool] = (state.kind in FORCE_VERIFIED_KINDS) if state.force else None
        self._say_outcome(team, state.seq, name, result, reason, detail, state.force, attempt_id=state.attempt_id, elapsed_ms=elapsed_ms, kind=state.kind, force_verified=force_verified, requested_by=state.requested_by)
        team.rt(name).in_flight = False
        team.say_inflight.pop(name, None)
        self.who_dirty = True

    def _say_outcome(self, team: TeamState, seq: int, name: str, result: str, reason: Optional[str], detail: Optional[str], force: bool, attempt_id: Optional[str] = None, elapsed_ms: Optional[float] = None, kind: Optional[str] = None, force_verified: Optional[bool] = None, requested_by: Any = None) -> None:
        """Append the ``typed`` record (to the human) that the console and ``say --wait`` read as the outcome."""
        if force and force_verified is False and kind:
            detail = "{}{}queue behaviour unverified for {}".format(detail or "", "; " if detail else "", kind)
        if result == "typed":
            text = "typed #{} into {}{}".format(seq, name, "'s running turn" if reason == "in_turn" else (" (dry run)" if reason == "dry" else ""))
        elif result == "refused":
            text = "#{} not typed into {}: {}".format(seq, name, reason)
        elif result == "not_submitted":
            text = "#{} is on {}'s prompt line but was not submitted".format(seq, name)
        else:
            text = "typing #{} into {} failed: {}".format(seq, name, reason)
        if detail:
            text = "{} ({})".format(text, _short_text(detail, 120))
        extra = {
            "seqs": [seq], "reply_to": seq, "member": name, "kind_of_member": kind, "result": result, "reason": reason,
            "detail": detail, "force": bool(force), "force_verified": force_verified, "requested_by": requested_by,
            "attempt_id": attempt_id, "elapsed_ms": elapsed_ms,
        }
        self._append_system(team, "typed", text, ["human"], extra)
        self.log("{}: say #{} -> {}: {} {}".format(team.name, seq, name, result, reason or ""))
        self.who_dirty = True

    def _finish_pending(self, team: TeamState, name: str, pending: Pending, event: str, text: str) -> None:
        if name in team.pending:
            del team.pending[name]
        if pending.attempt_id:
            team.ledger.record_outcome(pending.attempt_id, False, None)
        if pending.kind == "nudge" and event in ("expired", "abandoned") and pending.seqs:
            team.ledger.record_terminal(name, pending.seqs, event)
            team.delivery_terminal.setdefault(name, set()).update(pending.seqs)
        self._append_system(team, event, text, ["human"], {"seqs": list(pending.seqs), "member": name})
        self.enqueue_toast(team.name, pending.seqs, "herdr-synapse {}: {}".format(team.name, event), text, "none", kind="outcome")
        self.log("{}: {} {}".format(team.name, event, text))
        self.who_dirty = True

    def _append_system(self, team: TeamState, event: str, text: str, to: Sequence[str], extra: Optional[Dict[str, Any]] = None) -> Optional[int]:
        rec = system_record(team.name, event, text, to, os.fspath(self.layout.socket), extra)
        try:
            seq = append_board_record(team.paths, rec)
        except HerdrTeamError as err:
            self.log("{}: could not append {} record: {}".format(team.name, event, err))
            return None
        # Our own record will come back through the tail; the ingest ignores ``system`` authors.
        return seq

    def _finish_probe(self, team: TeamState, name: str, pending: Pending, now: float) -> None:
        """Drop the finished probe and put back the briefing or nudge it displaced, gate-fresh."""
        team.pending.pop(name, None)
        resumed = pending.resume
        if resumed is None or resumed.landed_ms is not None:
            return
        resumed.next_eligible_ms = now
        resumed.hold = None
        resumed.hold_since_ms = None
        resumed.hold_toasted = False
        team.pending[name] = resumed
        self.log("{}: {} {} resumed after the probe".format(team.name, name, resumed.kind))
        self.who_dirty = True

    def _record_probe(self, team: TeamState, member: Dict[str, Any], pending: Pending, result: str, details: Dict[str, Any], now: float) -> None:
        """``kinds.json[kind].probe``: the round trip ``hooks probe <kind>`` waits for."""
        probe = pending.probe or {}
        kind = str(probe.get("agent_kind") or member.get("kind") or "")
        doc = store.read_json(self.session.kinds_json, default=None)
        if not isinstance(doc, dict):
            doc = {}
        entry = doc.get(kind) if isinstance(doc.get(kind), dict) else {}
        ok = result in (RESULT_LANDED_WORKING, RESULT_DRY)
        # ``probe.ok`` is one round trip; ``verified`` needs 20 at >= 90 % clean (``_maybe_verify_kind``).
        entry["probe"] = {
            "nonce": probe.get("nonce"),
            "round_trip_ms": round(float(details.get("elapsed_ms") or 0.0), 1),
            "paste_multiline": None,
            "ok": ok,
            "result": result,
            "member": str(member.get("name")),
            "recorded_at": now_iso(),
            "source": "daemon",
        }
        doc[kind] = entry
        try:
            store.write_json(self.session.kinds_json, doc, fsync=False)
        except HerdrTeamError as err:
            self.log("kinds.json write failed: {}".format(err))
        self.log("{}: probe for {} ({}) -> {} in {:.0f} ms".format(team.name, member.get("name"), kind, result, float(details.get("elapsed_ms") or 0.0)))

    # -- toasts -------------------------------------------------------------------------------

    def enqueue_toast(self, team: str, seqs: Sequence[int], title: str, body: str, sound: str = "none", kind: str = "post") -> None:
        now = self.now_ms()
        self.notifications.append(Notification(team, list(seqs), title[:80], body, sound, now, now, None, kind))

    def _drain_human_queue(self) -> None:
        for team in self.teams.values():
            if not team.human_queue:
                continue
            posts = team.human_queue
            team.human_queue = []
            seqs = [int(p["seq"]) for p in posts]
            if len(posts) == 1:
                post = posts[0]
                prefix = "#{} {} from {}: ".format(post["seq"], post.get("kind"), post.get("from"))
                title = (prefix + _short_text(post.get("text"), max(0, 80 - len(prefix))))[:80]
                body = "#{} {}".format(post["seq"], _short_text(post.get("text"), 200))
                sound = "request" if post.get("kind") in ("request", "question", "blocked") else ("done" if post.get("kind") == "done" else "none")
            else:
                title = "{} new posts for you #{}-#{}".format(len(posts), min(seqs), max(seqs))[:80]
                body = "\n".join("#{} {}: {}".format(p["seq"], p.get("from"), _short_text(p.get("text"), 60)) for p in posts[:5])
                sound = "request" if any(p.get("kind") in ("request", "question", "blocked") for p in posts) else "none"
            self.enqueue_toast(team.name, seqs, title, body, sound, kind="post")

    def process_notifications(self) -> None:
        self._drain_human_queue()
        if not self.notifications:
            return
        now = self.now_ms()
        if now < self.next_toast_ms:
            return
        # Coalesce queued post toasts per team into one before sending.
        due = [n for n in self.notifications if n.next_ms <= now]
        if not due:
            return
        note = due[0]
        siblings = [n for n in due if n is not note and n.team == note.team and n.kind == "post" and note.kind == "post"]
        if siblings:
            for sibling in siblings:
                self.notifications.remove(sibling)
                note.seqs.extend(sibling.seqs)
            seqs = sorted(set(note.seqs))
            note.seqs = seqs
            note.title = "{} new posts for you #{}-#{}".format(len(seqs), seqs[0], seqs[-1])[:80]
        if self.toasts_disabled and self._toasts_still_disabled(now):
            self.notifications.remove(note)
            self._mirror_toast(note, "disabled", False)
            return
        try:
            result = self.api.request("notification.show", {"title": note.title, "body": note.body, "sound": note.sound}, timeout=5.0)
        except HerdrTeamError as err:
            self.log("notification.show failed: {}".format(err.code))
            note.reason = err.code
            note.next_ms = now + TOAST_RETRY_NO_CLIENT_S * 1000.0
            if now - note.created_ms > TOAST_GIVE_UP_S * 1000.0:
                self.notifications.remove(note)
                self._mirror_toast(note, err.code, False)
            return
        self.next_toast_ms = now + TOAST_GLOBAL_INTERVAL_S * 1000.0
        reason = str(result.get("reason") or ("shown" if result.get("shown") else "unknown"))
        note.reason = reason
        if reason == "shown" or result.get("shown"):
            self.counters["toasts"] += 1
            self.notifications.remove(note)
            self._mirror_toast(note, reason, True)
        elif reason in ("busy", "rate_limited"):
            if now - note.created_ms > TOAST_GIVE_UP_S * 1000.0:
                self.notifications.remove(note)
                self._mirror_toast(note, reason, False)
            else:
                note.next_ms = now + TOAST_RETRY_BUSY_S * 1000.0
        elif reason == "disabled":
            self.toasts_disabled = True
            self.toasts_disabled_ms = now
            self.toasts_disabled_delivery = read_toast_delivery(self.layout.config_dir)
            self.log("toasts disabled on this server; mirroring to human-attention only")
            for pending_note in list(self.notifications):
                self.notifications.remove(pending_note)
                self._mirror_toast(pending_note, reason, False)
        elif reason == "no_foreground_client":
            note.next_ms = now + TOAST_RETRY_NO_CLIENT_S * 1000.0
        else:
            self.notifications.remove(note)
            self._mirror_toast(note, reason, bool(result.get("shown")))

    def _toasts_still_disabled(self, now: float) -> bool:
        """Lift the ``disabled`` latch when ``[ui.toast] delivery`` changed (a config reload) or after
        ``TOAST_DISABLED_RECHECK_S``; the next ``notification.show`` re-latches if it still says disabled."""
        delivery = read_toast_delivery(self.layout.config_dir)
        if delivery != "off" and delivery != (self.toasts_disabled_delivery or "off"):
            self.log("toast delivery changed to {!r}; probing notification.show again".format(delivery))
        elif self.toasts_disabled_ms is not None and now - self.toasts_disabled_ms < TOAST_DISABLED_RECHECK_S * 1000.0:
            return True
        else:
            self.log("toasts disabled for {:.0f} s; probing notification.show again".format(TOAST_DISABLED_RECHECK_S))
        self.toasts_disabled = False
        self.toasts_disabled_ms = None
        self.toasts_disabled_delivery = None
        return False

    def _mirror_toast(self, note: Notification, reason: str, shown: bool) -> None:
        entry = {"ts": now_iso(), "team": note.team, "seqs": note.seqs, "title": note.title, "body": note.body, "reason": reason, "shown": shown, "kind": note.kind}
        team = self.teams.get(note.team)
        if team is not None:
            try:
                store.append_line(team.paths.human_attention, json.dumps(entry, ensure_ascii=False).encode("utf-8"), fsync=False)
            except HerdrTeamError as err:
                self.log("human-attention write failed: {}".format(err))
            self._append_system(team, "toast", "toast {}: {}".format(reason, note.title), ["human"], {"seqs": note.seqs, "shown": shown})
        self.who_dirty = True

    # -- who.json ------------------------------------------------------------------------------

    def write_who_if_due(self, now: float) -> None:
        if not self.who_dirty:
            return
        if self.last_who_ms is not None and now - self.last_who_ms < WHO_COALESCE_S * 1000.0:
            return
        self.last_who_ms = now
        self.who_dirty = False
        self.write_who()

    def build_who(self) -> Dict[str, Any]:
        console = store.read_json(self.session.console_json, default=None)
        default_team = console.get("default_team") if isinstance(console, dict) else None
        if default_team is None and len(self.teams) == 1:
            default_team = next(iter(self.teams))
        charters: Dict[str, Any] = {}
        teams: Dict[str, Any] = {}
        now_ms = self.now_ms()
        for team in self.teams.values():
            charter = team.roster.get("charter") if isinstance(team.roster.get("charter"), dict) else None
            if charter is not None:
                charters[team.name] = {"seq": charter.get("seq"), "headline": _short_text(charter.get("text"), 120), "refs": list(charter.get("refs") or [])}
            mute = read_mute(team.paths)
            members = []
            for member in team.members():
                name = str(member.get("name"))
                agent = self.agents.get(str(member.get("terminal_id") or "")) if member.get("terminal_id") else None
                rt = team.rt(name)
                pending = team.pending.get(name)
                until = muted_until(mute, name, time.time())
                members.append({
                    "name": name,
                    "role": member.get("role"),
                    "kind": member.get("kind"),
                    "status": member.get("status"),
                    "agent_status": agent.get("agent_status") if agent else None,
                    "pane_id": agent.get("pane_id") if agent else member.get("pane_id"),
                    "terminal_id": member.get("terminal_id"),
                    "workspace_id": agent.get("workspace_id") if agent else member.get("workspace_id"),
                    "terminal_title_stripped": agent.get("terminal_title_stripped") if agent else None,
                    "last_headline": rt.last_headline,
                    "pending_nudges": len(pending.seqs) if pending and pending.kind == "nudge" else (1 if pending else 0),
                    "hold": pending.hold if pending else None,
                    "say": "confirming" if name in team.say_inflight else None,
                    "interrupt": pending.interrupt_state if pending is not None and pending.interrupt else None,
                    "muted_until": None if until is None else ("indefinite" if until == float("inf") else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until))),
                    "verified_kind": bool(member.get("verified_kind")) or self._kind_trusted(str(member.get("kind"))),
                    "delivery": member.get("delivery", "nudge"),
                    "hooks_last_seen": self._hooks_last_seen(member),
                    "last_seen_at": member.get("last_seen_at"),
                    "briefed": member.get("briefed_at") is not None,
                    "charter_stale": bool(charter and (member.get("charter_seq_acked") or 0) < int(charter.get("seq") or 0)) if member.get("kind") != "human" else False,
                    "instructions_stale": _charter.instructions_stale(member) if member.get("kind") != "human" else False,
                    "operator": bool(_operator.active(self.session, team.name, name)) if member.get("kind") != "human" else False,
                    "brief": member.get("brief"),
                    "session": roster.short_session(member.get("session")),
                    "manager": bool(member.get("manager")),
                    "model": member.get("model"),
                    "effort": member.get("effort"),
                    "model_effective": _models.effective_setting(team.roster.get("config"), member)[0],
                    "setting": _models.label(*_models.effective_setting(team.roster.get("config"), member)),
                    "restarting": isinstance(rt.restart, dict),
                "context": rt.context,
                })
            try:
                links = _links.summary(self.session, team.name)
            except (HerdrTeamError, OSError):
                links = []
            teams[team.name] = {"members": members, "pending": sum(len(p.seqs) for p in team.pending.values()), "watermark": team.watermark, "links": links}
        return {
            "v": 1,
            "daemon_beat_at": now_iso(),
            "daemon_pid": os.getpid(),
            "socket": os.fspath(self.layout.socket),
            "herdr_version": self.server_version,
            "default_team": default_team,
            "toasts": "disabled" if self.toasts_disabled else read_toast_delivery(self.layout.config_dir),
            "charters": charters,
            "teams": teams,
            "counters": dict(self.counters),
            "uptime_ms": now_ms - (self.connected_ms or now_ms),
        }

    def write_who(self) -> None:
        try:
            store.write_json(self.session.who_json, self.build_who(), fsync=False)
        except HerdrTeamError as err:
            self.log("who.json write failed: {}".format(err))


# --------------------------------------------------------------------------
# entry point for a foreground daemon (tests, debugging)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python -m herdr_team.daemon --socket PATH [--state-root DIR] [--dry-nudge] [--allow-version]``.

    Runs the daemon in the foreground (no double fork) holding ``daemon.lock``;
    exit 5 when another daemon holds it. Used by tests and for debugging.
    """
    parser = argparse.ArgumentParser(prog="herdr_team.daemon")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--state-root")
    parser.add_argument("--dry-nudge", action="store_true")
    parser.add_argument("--allow-version", action="store_true")
    parser.add_argument("--lock-timeout", type=float, default=0.0)
    parser.add_argument("--max-iterations", type=int)
    parser.add_argument("--backoff", help="comma separated reconnect backoff seconds (tests)")
    parser.add_argument("--give-up", type=float, help="seconds before giving up on reconnects (tests)")
    parser.add_argument("--tick", type=float, help="subscription tick seconds (tests)")
    args = parser.parse_args(list(argv) if argv is not None else None)
    env = scrub_env(os.environ)
    for name in IDENTITY_ENV_VARS:
        os.environ.pop(name, None)
    if args.state_root:
        env["HERDR_TEAM_STATE_DIR"] = args.state_root
        os.environ["HERDR_TEAM_STATE_DIR"] = args.state_root
    layout = resolve_layout(env, socket=args.socket)
    ensure_session_dirs(layout.session)
    lock = store.daemon_lock(layout.session, timeout=args.lock_timeout)
    try:
        lock.acquire()
    except LockTimeout as err:
        sys.stderr.write(json.dumps(err.to_json()) + "\n")
        return EXIT_DAEMON_DOWN
    api = HerdrApi(layout.socket, env=env)
    daemon = Daemon(layout, env, api, dry_nudge=args.dry_nudge, allow_version=args.allow_version)
    daemon.lock = lock
    if args.backoff:
        daemon.backoff = tuple(float(x) for x in args.backoff.split(","))
    if args.give_up is not None:
        daemon.give_up_s = args.give_up
    if args.tick is not None:
        daemon.tick_s = args.tick
    daemon.max_iterations = args.max_iterations
    try:
        daemon.connect_server()
    except HerdrTeamError as err:
        if err.code == "herdr_version_mismatch":
            sys.stderr.write(json.dumps(err.to_json()) + "\n")
            lock.release()
            return err.exit_code
    daemon.write_info(started=True)
    daemon.log("foreground start pid {} {}".format(os.getpid(), _stdio_description(layout.session)))
    return daemon.run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
