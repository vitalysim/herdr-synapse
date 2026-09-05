"""Locks, atomic file primitives, and the storage classes.

Implemented here (every module needs them):

* the three ``flock`` files of the whole plugin, through ``FileLock``:
  ``team_lock(team)`` (roster, board, seq, cursors), ``daemon_lock(session)``,
  ``claude_settings_lock(settings_path)``. Nobody else imports ``fcntl``.
* ``secure_open`` / ``atomic_write`` / ``read_json`` / ``write_json`` /
  ``append_line`` with 0600 modes, ``O_NOFOLLOW|O_CLOEXEC``, lstat symlink
  refusal, and same-directory temp files.

Board storage (plan 6.1 to 6.3): ``BoardStore`` (append, seq, rotation,
archive index, reads, attachments), ``Cursors``, ``BoardTailer``, and the
record grammar and retraction/supersedes helpers around them. ``RosterStore``
stays a stub for the roster implementer.

Lock semantics: ``FileLock.acquire(timeout)`` polls ``LOCK_EX|LOCK_NB``
every 10 ms (``time.monotonic``) and raises ``LockTimeout`` (exit 5) when the
deadline passes. ``timeout=0`` is a single try. The kernel releases a flock
when the holder dies. The daemon must close inherited fds *before* taking
``daemon_lock``: a later ``closerange`` would release it.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import itertools
import json
import logging
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, LockTimeout
from herdr_team.paths import DIR_MODE, FILE_MODE, SessionPaths, TeamPaths, check_not_symlink, ensure_dir, ensure_team_dirs

PathLike = Union[str, "os.PathLike[str]"]

LOCK_POLL_S = 0.01
TEAM_LOCK_TIMEOUT_S = 5.0
CLAUDE_SETTINGS_LOCK_SUFFIX = ".herdr-team.lock"


# --------------------------------------------------------------------------
# secure file primitives


def secure_open(path: PathLike, flags: int, mode: int = FILE_MODE) -> int:
    """``os.open`` with ``O_NOFOLLOW|O_CLOEXEC`` after an lstat symlink check."""
    check_not_symlink(path)
    flags |= os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        return os.open(path, flags, mode)
    except OSError as err:
        if err.errno in (errno.ELOOP, errno.EMLINK):
            raise HerdrTeamError("path_symlink", "refusing to open a symlink", EXIT_REFUSED, {"path": os.fspath(path)})
        raise


def atomic_write(path: PathLike, data: bytes, mode: int = FILE_MODE, fsync: bool = True) -> None:
    """Write ``data`` to a temp file in the target directory, fsync, rename."""
    target = Path(path)
    check_not_symlink(target)
    ensure_dir(target.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-" + target.name + "-", dir=os.fspath(target.parent))
    try:
        try:
            os.fchmod(fd, mode)
            view = memoryview(data)
            while len(view):
                written = os.write(fd, view)
                view = view[written:]
            if fsync:
                os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_bytes(path: PathLike, default: Optional[bytes] = None) -> Optional[bytes]:
    """Read a managed file; ``default`` when absent. Symlinks refused."""
    try:
        fd = secure_open(path, os.O_RDONLY)
    except FileNotFoundError:
        return default
    chunks: List[bytes] = []
    try:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


def read_json(path: PathLike, default: Any = None) -> Any:
    """Parse a managed JSON file; ``default`` when absent or unparseable."""
    raw = read_bytes(path)
    if raw is None:
        return default
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return default


def write_json(path: PathLike, obj: Any, mode: int = FILE_MODE, fsync: bool = True) -> None:
    """Serialize ``obj`` as one JSON line (no indent) and write it atomically."""
    data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=False) + "\n").encode("utf-8")
    atomic_write(path, data, mode=mode, fsync=fsync)


def append_line(path: PathLike, line: bytes, mode: int = FILE_MODE, fsync: bool = True) -> None:
    """Append one ``\\n``-terminated line with ``O_APPEND``; heals a missing final newline.

    The caller holds the relevant lock. A short write is truncated back to
    the pre-write size so readers never see a torn record.
    """
    if not line.endswith(b"\n"):
        line += b"\n"
    # O_RDWR so the last byte can be pread back; O_APPEND keeps every write at the tail.
    fd = secure_open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, mode)
    try:
        size = os.fstat(fd).st_size
        prefix = b""
        if size > 0:
            last = os.pread(fd, 1, size - 1)
            if last != b"\n":
                prefix = b"\n"
        payload = prefix + line
        written = os.write(fd, payload)
        if written != len(payload):
            os.ftruncate(fd, size)
            raise HerdrTeamError("board_write_failed", "short write, record not written", EXIT_REFUSED, {"path": os.fspath(path)})
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# locks


class FileLock:
    """One ``flock``-ed file. Use as a context manager or ``acquire``/``release``."""

    def __init__(self, path: PathLike, timeout: float = TEAM_LOCK_TIMEOUT_S, code: str = "lock_timeout") -> None:
        self.path = Path(path)
        self.timeout = float(timeout)
        self.code = code
        self.fd: Optional[int] = None

    @property
    def held(self) -> bool:
        return self.fd is not None

    def acquire(self, timeout: Optional[float] = None) -> "FileLock":
        if self.fd is not None:
            return self
        wait = self.timeout if timeout is None else float(timeout)
        ensure_dir(self.path.parent, DIR_MODE)
        fd = secure_open(self.path, os.O_RDWR | os.O_CREAT, FILE_MODE)
        deadline = time.monotonic() + wait
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as err:
                    if err.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                        raise
                if time.monotonic() >= deadline:
                    raise LockTimeout(os.fspath(self.path), wait, self.code)
                time.sleep(LOCK_POLL_S)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def try_acquire(self) -> bool:
        """Single non-blocking attempt; False when another process holds it."""
        try:
            self.acquire(0.0)
        except LockTimeout:
            return False
        return True

    def release(self) -> None:
        fd = self.fd
        if fd is None:
            return
        self.fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


def team_lock(team: Union[TeamPaths, PathLike], timeout: float = TEAM_LOCK_TIMEOUT_S) -> FileLock:
    """``<team>/team.lock``: roster, board, ``board.seq``, cursors. Timeout -> ``board_locked``, exit 5."""
    path = team.team_lock if isinstance(team, TeamPaths) else Path(team) / "team.lock"
    # ``FileLock.acquire`` creates the lock's directory. A team dir is created by ``create_team``
    # (``ensure_team_dirs``) before its first lock, so a missing root means the team was dissolved
    # or never existed: refuse instead of resurrecting ``teams/<team>/team.lock`` (RS-10/RS-13 live
    # finding: the daemon's reconcile raced ``dissolve`` and left a lock-only ``teams/gamma``).
    if not path.parent.is_dir():
        raise HerdrTeamError("team_not_found", "team directory {} does not exist".format(path.parent), EXIT_REFUSED, {"team_dir": os.fspath(path.parent)})
    return FileLock(path, timeout, code="board_locked")


def daemon_lock(session: Union[SessionPaths, PathLike], timeout: float = 0.0) -> FileLock:
    """``<session>/daemon.lock``: held for the daemon's lifetime. Default is one try."""
    path = session.daemon_lock if isinstance(session, SessionPaths) else Path(session) / "daemon.lock"
    return FileLock(path, timeout, code="daemon_lock_held")


def claude_settings_lock(settings_path: PathLike, timeout: float = TEAM_LOCK_TIMEOUT_S) -> FileLock:
    """``~/.claude/settings.json.herdr-team.lock`` for hook installs."""
    return FileLock(os.fspath(settings_path) + CLAUDE_SETTINGS_LOCK_SUFFIX, timeout, code="settings_locked")


# --------------------------------------------------------------------------
# board (plan 6.1 to 6.3)
#
# Record grammar, seq assignment, torn-tail healing, rotation, the archive
# index, cursors, the tailer contract, retraction/supersedes helpers, and
# attachment staging. Only ``append``, ``Cursors.advance``, and the
# attachment commit take ``team.lock``; every read path is lock-free.

#: Wrapped so tests can simulate ENOSPC without patching ``os`` globally.
_os_write = os.write

BOARD_SCHEMA_VERSION = 1
TAIL_SCAN_BYTES = 64 * 1024
MAX_ATTACHMENT_BYTES = 16 * 1024 * 1024
ATTACHMENT_COPY_CHUNK = 256 * 1024
STAGING_PREFIX = ".tmp-"
SAFE_BASENAME_RE = re.compile(r"[^A-Za-z0-9._-]")
MAX_BASENAME_CHARS = 64
ARCHIVE_SEGMENT_RE = re.compile(r"^board\.([0-9]+)-([0-9]+)\.jsonl$")
ARCHIVE_INDEX_VERSION = 1
TAILER_STATE_VERSION = 1
TAILER_ENOENT_LOG_S = 5.0

RECORD_KINDS = ("note", "request", "handoff", "done", "blocked", "question", "answer", "direct", "retract", "system")
SYSTEM_EVENTS = (
    "nudged", "toast", "retracted", "expired", "abandoned", "member_gone", "member_restarted",
    "rotated", "reset_detected", "charter_updated", "renamed", "typed", "member_joined",
)
#: Every key of a stored record in file order (docs/cli.md section 10).
RECORD_KEYS = (
    "v", "seq", "ts", "from", "from_label", "from_kind", "from_pane", "from_terminal", "from_gen", "origin",
    "to", "to_role", "kind", "text", "refs", "reply_to", "retracts", "supersedes", "urgent", "ttl_ms",
    "truncated", "event", "relayed_for",
)
_RECORD_DEFAULTS: Dict[str, Any] = {
    "from_label": None, "from_kind": None, "from_pane": None, "from_terminal": None, "from_gen": None,
    "to_role": None, "reply_to": None, "retracts": None, "supersedes": None, "urgent": False,
    "ttl_ms": None, "truncated": False, "event": None, "relayed_for": None,
}

_log = logging.getLogger("herdr_team.store")
_log.addHandler(logging.NullHandler())


def now_iso() -> str:
    """ISO-8601 UTC with milliseconds, e.g. ``2026-09-04T13:53:10.123Z``."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(now.microsecond // 1000)


def _record_invalid(message: str, **details: Any) -> HerdrTeamError:
    return HerdrTeamError("record_invalid", message, EXIT_REFUSED, details)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_utf8_bytes(data: bytes) -> str:
    """Strict decode through ``sanitize.validate_utf8`` (``invalid_utf8``, exit 1)."""
    from herdr_team import sanitize as _sanitize

    return _sanitize.validate_utf8(data)


def _check_utf8(value: Any, path: str = "record") -> Any:
    """Walk a record; every ``str`` must round-trip as strict UTF-8, ``bytes`` are decoded."""
    if isinstance(value, bytes):
        return _validate_utf8_bytes(value)
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as err:
            raise HerdrTeamError("invalid_utf8", "{} is not valid UTF-8".format(path), EXIT_REFUSED, {"field": path, "reason": str(err)})
        return value
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            key_s = _check_utf8(key, path + ".key")
            if not isinstance(key_s, str):
                raise _record_invalid("object keys must be strings", field=path)
            out[key_s] = _check_utf8(item, path + "." + key_s)
        return out
    if isinstance(value, (list, tuple)):
        return [_check_utf8(item, "{}[{}]".format(path, index)) for index, item in enumerate(value)]
    return value


def valid_record(obj: Any) -> bool:
    """Reader grammar: the minimum a stored line must satisfy to be a record."""
    if not isinstance(obj, dict):
        return False
    if obj.get("v") != BOARD_SCHEMA_VERSION:
        return False
    seq = obj.get("seq")
    if not _is_int(seq) or seq < 1:
        return False
    if not isinstance(obj.get("from"), str) or not isinstance(obj.get("kind"), str):
        return False
    if not isinstance(obj.get("text"), str) or not isinstance(obj.get("to"), list):
        return False
    return True


def is_direct_line(record: Any) -> bool:
    """A line the human typed straight into one member (``direct``) or the daemon's ``typed`` outcome for it.

    Neither is mail (docs/cli.md section 7, ``say``): readers never count them
    as unread, the daemon never nudges for them, and a Stop hook never blocks
    on them. Both stay on the board for everyone to read.
    """
    if not isinstance(record, dict):
        return False
    kind = record.get("kind")
    if kind == "direct":
        return True
    return kind == "system" and record.get("event") == "typed"


def normalize_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a caller-built record and return it with every key present, in file order.

    ``seq`` and ``ts`` are assigned by ``BoardStore.append``; ``v`` is forced.
    Raises ``record_invalid`` (exit 1) or ``invalid_utf8`` (exit 1).
    """
    if not isinstance(record, dict):
        raise _record_invalid("record must be an object")
    src = _check_utf8(dict(record))
    sender = src.get("from")
    if not isinstance(sender, str) or not sender:
        raise _record_invalid("record needs a non-empty 'from'", field="from")
    kind = src.get("kind")
    if kind not in RECORD_KINDS:
        raise _record_invalid("unknown kind", field="kind", kind=kind, allowed=list(RECORD_KINDS))
    text = src.get("text", "")
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise _record_invalid("'text' must be a string", field="text")
    to = src.get("to")
    if isinstance(to, str):
        to = [to]
    if not isinstance(to, list) or not to or not all(isinstance(t, str) and t for t in to):
        raise _record_invalid("'to' must be a non-empty list of names", field="to")
    refs = src.get("refs") or []
    if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
        raise _record_invalid("'refs' must be a list of paths", field="refs")
    origin = src.get("origin")
    if origin is None:
        origin = {}
    if not isinstance(origin, dict):
        raise _record_invalid("'origin' must be an object", field="origin")
    for key in ("reply_to", "retracts", "supersedes", "from_gen", "ttl_ms"):
        value = src.get(key)
        if value is not None and not _is_int(value):
            raise _record_invalid("'{}' must be an integer or null".format(key), field=key)
    event = src.get("event")
    if kind == "system":
        if sender != "system":
            raise _record_invalid("kind 'system' requires from 'system'", field="from")
        if event not in SYSTEM_EVENTS:
            raise _record_invalid("system record needs a known event", field="event", allowed=list(SYSTEM_EVENTS))
    else:
        if sender == "system":
            raise _record_invalid("from 'system' requires kind 'system'", field="kind")
        if event is not None:
            raise _record_invalid("'event' is only valid on system records", field="event")
    if kind == "retract" and not _is_int(src.get("retracts")):
        raise _record_invalid("retract record needs 'retracts'", field="retracts")
    for key in ("from_label", "from_kind", "from_pane", "from_terminal", "to_role", "relayed_for"):
        value = src.get(key)
        if value is not None and not isinstance(value, str):
            raise _record_invalid("'{}' must be a string or null".format(key), field=key)

    out: Dict[str, Any] = {"v": BOARD_SCHEMA_VERSION, "seq": src.get("seq"), "ts": src.get("ts")}
    for key in RECORD_KEYS[3:]:
        if key == "from":
            out[key] = sender
        elif key == "to":
            out[key] = list(to)
        elif key == "kind":
            out[key] = kind
        elif key == "text":
            out[key] = text
        elif key == "refs":
            out[key] = list(refs)
        elif key == "origin":
            out[key] = origin
        elif key in ("urgent", "truncated"):
            out[key] = bool(src.get(key, _RECORD_DEFAULTS[key]))
        else:
            out[key] = src.get(key, _RECORD_DEFAULTS[key])
    for key, value in src.items():
        if key not in out:
            out[key] = value
    return out


def encode_record(record: Dict[str, Any]) -> bytes:
    """One compact JSON line. ``json.dumps`` escapes every newline, so lines split cleanly."""
    try:
        text = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as err:
        raise _record_invalid("record is not JSON-serialisable: {}".format(err))
    return text.encode("utf-8") + b"\n"


def parse_lines(data: bytes) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Split on ``\\n`` bytes only, skip the final fragment, drop invalid lines.

    Returns ``(records in file order, {"corrupt": n, "fragment": 0|1})``.
    """
    records: List[Dict[str, Any]] = []
    stats = {"corrupt": 0, "fragment": 0}
    if not data:
        return records, stats
    pieces = data.split(b"\n")
    if pieces[-1]:
        stats["fragment"] = 1
    for line in pieces[:-1]:
        if not line.strip():
            continue
        try:
            obj = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            stats["corrupt"] += 1
            continue
        if not valid_record(obj):
            stats["corrupt"] += 1
            continue
        records.append(obj)
    return records, stats


def safe_basename(name: str) -> str:
    """``[A-Za-z0-9._-]`` basename, capped, with an 8-char hash suffix when rewritten."""
    original = os.path.basename(name.rstrip("/")) or "file"
    cleaned = SAFE_BASENAME_RE.sub("_", original).lstrip(".") or "file"
    stem, ext = os.path.splitext(cleaned)
    if len(ext) > 16:
        stem, ext = cleaned, ""
    if len(stem) + len(ext) > MAX_BASENAME_CHARS:
        stem = stem[: max(1, MAX_BASENAME_CHARS - len(ext))]
    if stem + ext == original:
        return original
    digest = hashlib.sha1(original.encode("utf-8", "surrogateescape")).hexdigest()[:8]
    stem = stem[: max(1, MAX_BASENAME_CHARS - len(ext) - 9)]
    return "{}-{}{}".format(stem, digest, ext)


def retracted_map(records: Iterable[Dict[str, Any]]) -> Dict[int, int]:
    """``{retracted seq: retracting seq}`` from ``retract`` records and ``retracted`` system events."""
    out: Dict[int, int] = {}
    for rec in records:
        target = rec.get("retracts")
        if not _is_int(target):
            continue
        if rec.get("kind") == "retract" or (rec.get("kind") == "system" and rec.get("event") == "retracted"):
            if target not in out or rec["seq"] < out[target]:
                out[target] = rec["seq"]
    return out


def supersedes_map(records: Iterable[Dict[str, Any]]) -> Dict[int, int]:
    """``{old seq: newest seq that supersedes it}`` (chains resolve to the newest)."""
    direct: Dict[int, int] = {}
    for rec in records:
        old = rec.get("supersedes")
        if _is_int(old) and rec.get("kind") != "system":
            if old not in direct or rec["seq"] > direct[old]:
                direct[old] = rec["seq"]
    out: Dict[int, int] = {}
    for old in direct:
        newest = direct[old]
        seen = {old}
        while newest in direct and newest not in seen:
            seen.add(newest)
            newest = direct[newest]
        out[old] = newest
    return out


def filter_retracted(records: Iterable[Dict[str, Any]], include_retracted: bool = False) -> List[Dict[str, Any]]:
    """Drop retracted originals (and the ``retract`` records themselves) unless asked to keep them."""
    items = list(records)
    if include_retracted:
        return items
    gone = retracted_map(items)
    return [r for r in items if r["seq"] not in gone and r.get("kind") != "retract"]


def make_retract_record(original: Dict[str, Any], author: Dict[str, Any]) -> Dict[str, Any]:
    """A ``retract`` record for ``original``; ``author`` carries the ``from*``/``origin`` fields."""
    rec = dict(author)
    rec.update({
        "kind": "retract", "retracts": int(original["seq"]), "to": list(original.get("to") or ["all"]),
        "to_role": original.get("to_role"), "text": "retracted #{}".format(original["seq"]),
        "reply_to": original.get("reply_to"),
    })
    return rec


def make_supersede_record(original: Dict[str, Any], author: Dict[str, Any], text: str) -> Dict[str, Any]:
    """An edit: a new record of the original's kind carrying ``supersedes``."""
    rec = dict(author)
    rec.update({
        "kind": original.get("kind", "note"), "supersedes": int(original["seq"]), "to": list(original.get("to") or ["all"]),
        "to_role": original.get("to_role"), "text": text, "refs": list(original.get("refs") or []),
        "reply_to": original.get("reply_to"), "urgent": bool(original.get("urgent", False)),
    })
    if rec["kind"] in ("retract", "system"):
        rec["kind"] = "note"
    return rec


@dataclass
class StagedAttachment:
    """A payload copied into ``payloads/.tmp-<pid>-<n>`` before the lock."""

    tmp_path: Path
    basename: str
    size: int
    source: str
    committed: Optional[str] = None


class BoardStore:
    """Append-only board under ``team.lock`` (plan 6.1 to 6.3).

    ``append`` validates, takes the lock, computes ``seq``, writes
    ``board.seq`` then the record, rotates at 4 MiB. ``read`` never locks.
    """

    ROTATE_BYTES = 4 * 1024 * 1024

    def __init__(self, team: TeamPaths, lock_timeout: float = TEAM_LOCK_TIMEOUT_S, rotate_bytes: Optional[int] = None) -> None:
        self.team = team
        self.lock_timeout = float(lock_timeout)
        self.rotate_bytes = int(rotate_bytes) if rotate_bytes is not None else self.ROTATE_BYTES
        self.last_stats: Dict[str, Any] = {"corrupt": 0, "fragment": 0, "duplicates": 0, "files": []}
        self._staging_counter = itertools.count(1)

    # -- locking -------------------------------------------------------------

    def _lock(self) -> FileLock:
        lock = team_lock(self.team, self.lock_timeout)
        try:
            lock.acquire()
        except LockTimeout as err:
            failed = LockTimeout(os.fspath(self.team.team_lock), self.lock_timeout, "board_locked")
            failed.message = "post NOT written: could not acquire team.lock within {:g}s".format(self.lock_timeout)
            failed.args = (failed.message,)
            raise failed from err
        return lock

    # -- seq -----------------------------------------------------------------

    def _seq_hint(self) -> int:
        """``board.seq``'s ``next - 1``; 0 when the file is missing or garbage."""
        doc = read_json(self.team.board_seq)
        if isinstance(doc, dict) and _is_int(doc.get("next")) and doc["next"] >= 1:
            return doc["next"] - 1
        return 0

    def _tail_scan(self) -> Tuple[int, int, int]:
        """``(max seq in the last 64 KiB, first seq in the first 64 KiB, size)``; zeros when absent."""
        try:
            fd = secure_open(self.team.board_jsonl, os.O_RDONLY)
        except FileNotFoundError:
            return 0, 0, 0
        try:
            size = os.fstat(fd).st_size
            if size == 0:
                return 0, 0, 0
            head = os.pread(fd, min(size, TAIL_SCAN_BYTES), 0)
            start = max(0, size - TAIL_SCAN_BYTES)
            tail = head if start == 0 else os.pread(fd, size - start, start)
        finally:
            os.close(fd)
        first_seq = 0
        head_records, _ = parse_lines(head + (b"\n" if len(head) < size and not head.endswith(b"\n") else b""))
        if head_records:
            first_seq = head_records[0]["seq"]
        if start > 0:
            # drop the leading partial line of the tail window
            cut = tail.find(b"\n")
            tail = tail[cut + 1:] if cut >= 0 else b""
        tail_records, _ = parse_lines(tail)
        max_seq = max((r["seq"] for r in tail_records), default=0)
        return max_seq, first_seq, size

    def next_seq_unlocked(self) -> int:
        """``max(board.seq, tail seq via pread of the last 64 KiB, archive max) + 1``."""
        tail_max, _, _ = self._tail_scan()
        return max(self._seq_hint(), tail_max, self._archive_max()) + 1

    def max_seq(self) -> int:
        return self.next_seq_unlocked() - 1

    def active_first_seq(self) -> int:
        """First seq of the active file, or the seq the next record would get when it is empty."""
        _, first, _ = self._tail_scan()
        if first:
            return first
        doc = read_json(self.team.board_seq)
        if isinstance(doc, dict) and _is_int(doc.get("active_first_seq")) and doc["active_first_seq"] >= 1:
            return doc["active_first_seq"]
        return self.next_seq_unlocked()

    # -- append --------------------------------------------------------------

    def append(
        self,
        record: Dict[str, Any],
        attachments: Optional[Sequence[StagedAttachment]] = None,
        spill_text: Optional[str] = None,
    ) -> int:
        """Assign ``seq`` and ``ts``, write, return the seq. Raises ``board_locked`` on timeout.

        ``attachments`` are ``stage_attachment`` results committed under the
        lock as ``payloads/<seq>-<basename>``; ``spill_text`` lands in
        ``payloads/<seq>-body.md``. Both are added to ``refs``.
        """
        prepared = normalize_record(record)
        if spill_text is not None:
            _check_utf8(spill_text, "spill_text")
        encode_record(prepared)  # serialisability before the lock
        ensure_dir(self.team.root)
        lock = self._lock()
        try:
            # one tail scan under the lock: max seq for the assignment, first seq for the hint
            tail_max, first, _ = self._tail_scan()
            seq = max(self._seq_hint(), tail_max, self._archive_max()) + 1
            first_seq = first or seq
            write_json(self.team.board_seq, {"next": seq + 1, "active_first_seq": first_seq})
            committed: List[Path] = []
            try:
                refs = list(prepared["refs"])
                if spill_text is not None:
                    body = self.team.payloads_dir / "{}-body.md".format(seq)
                    ensure_dir(self.team.payloads_dir)
                    atomic_write(body, spill_text.encode("utf-8"))
                    committed.append(body)
                    refs.append("payloads/" + body.name)
                for staged in attachments or ():
                    target = self._commit_attachment(seq, staged)
                    committed.append(target)
                    refs.append("payloads/" + target.name)
                prepared["refs"] = refs
                prepared["seq"] = seq
                prepared["ts"] = now_iso()
                self._write_record(encode_record(prepared))
            except BaseException:
                for path in committed:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                raise
            self._rotate_locked(seq, first_seq)
        finally:
            lock.release()
        return seq

    def _write_record(self, line: bytes) -> None:
        """Open ``O_APPEND`` under the lock, heal a torn tail, one write, truncate on failure, fsync."""
        # O_RDWR rather than O_WRONLY so the last byte can be pread through the same fd.
        fd = secure_open(self.team.board_jsonl, os.O_RDWR | os.O_APPEND | os.O_CREAT, FILE_MODE)
        try:
            size = os.fstat(fd).st_size
            payload = line
            if size > 0 and os.pread(fd, 1, size - 1) != b"\n":
                payload = b"\n" + line
            try:
                written = _os_write(fd, payload)
            except OSError as err:
                self._truncate_back(fd, size)
                raise HerdrTeamError(
                    "board_write_failed", "record not written: {}".format(err.strerror or err),
                    EXIT_REFUSED, {"path": os.fspath(self.team.board_jsonl), "errno": err.errno},
                )
            if written != len(payload):
                self._truncate_back(fd, size)
                raise HerdrTeamError(
                    "board_write_failed", "short write, record not written", EXIT_REFUSED,
                    {"path": os.fspath(self.team.board_jsonl), "written": written, "expected": len(payload)},
                )
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _truncate_back(fd: int, size: int) -> None:
        try:
            os.ftruncate(fd, size)
        except OSError:
            _log.warning("board: ftruncate after a failed write did not succeed")

    # -- rotation and archive ------------------------------------------------

    def rotate_if_needed(self) -> Optional[Path]:
        """Under the lock: rename to ``archive/board.<a>-<b>.jsonl`` when over ``ROTATE_BYTES``."""
        lock = self._lock()
        try:
            tail_max, first, _ = self._tail_scan()
            if not tail_max:
                return None
            return self._rotate_locked(tail_max, first or tail_max)
        finally:
            lock.release()

    def _rotate_locked(self, last_seq: int, first_seq: int) -> Optional[Path]:
        try:
            size = os.stat(self.team.board_jsonl).st_size
        except FileNotFoundError:
            return None
        if size < self.rotate_bytes:
            return None
        ensure_dir(self.team.archive_dir)
        target = self.team.archive_segment(first_seq, last_seq)
        check_not_symlink(target)
        os.rename(self.team.board_jsonl, target)
        try:
            dir_fd = os.open(self.team.root, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        self.rebuild_archive_index()
        next_seq = last_seq + 1
        write_json(self.team.board_seq, {"next": next_seq + 1, "active_first_seq": next_seq})
        note = normalize_record({
            "from": "system", "kind": "system", "event": "rotated", "to": ["all"],
            "text": "board rotated: archive/{} ({} through {})".format(target.name, first_seq, last_seq),
        })
        note["seq"] = next_seq
        note["ts"] = now_iso()
        self._write_record(encode_record(note))
        return target

    def archive_segments(self) -> List[Dict[str, Any]]:
        """Segments from ``index.json``, rebuilt from filenames when missing or inconsistent."""
        names = self._archive_filenames()
        doc = read_json(self.team.archive_index)
        if self._index_consistent(doc, names):
            return list(doc["segments"])
        return list(self.rebuild_archive_index()["segments"])

    def _archive_filenames(self) -> List[str]:
        try:
            entries = os.listdir(self.team.archive_dir)
        except FileNotFoundError:
            return []
        return sorted(n for n in entries if ARCHIVE_SEGMENT_RE.match(n))

    @staticmethod
    def _index_consistent(doc: Any, names: List[str]) -> bool:
        if not isinstance(doc, dict) or doc.get("v") != ARCHIVE_INDEX_VERSION:
            return False
        segments = doc.get("segments")
        if not isinstance(segments, list):
            return False
        listed: List[str] = []
        for seg in segments:
            if not isinstance(seg, dict) or not isinstance(seg.get("file"), str):
                return False
            match = ARCHIVE_SEGMENT_RE.match(seg["file"])
            if not match or seg.get("first_seq") != int(match.group(1)) or seg.get("last_seq") != int(match.group(2)):
                return False
            listed.append(seg["file"])
        return sorted(listed) == sorted(names)

    def rebuild_archive_index(self) -> Dict[str, Any]:
        segments: List[Dict[str, Any]] = []
        for name in self._archive_filenames():
            match = ARCHIVE_SEGMENT_RE.match(name)
            if not match:
                continue
            try:
                size = os.lstat(self.team.archive_dir / name).st_size
            except OSError:
                size = 0
            segments.append({"file": name, "first_seq": int(match.group(1)), "last_seq": int(match.group(2)), "bytes": size})
        segments.sort(key=lambda s: (s["first_seq"], s["last_seq"]))
        doc = {
            "v": ARCHIVE_INDEX_VERSION, "segments": segments,
            "max_seq": max((s["last_seq"] for s in segments), default=0), "updated": now_iso(),
        }
        if segments or self.team.archive_dir.is_dir():
            ensure_dir(self.team.archive_dir)
            write_json(self.team.archive_index, doc, fsync=False)
        return doc

    def _archive_max(self) -> int:
        return max((s["last_seq"] for s in self.archive_segments()), default=0)

    def _read_segment(self, name: str) -> List[Dict[str, Any]]:
        data = read_bytes(self.team.archive_dir / name)
        if data is None:
            return []
        records, stats = parse_lines(data)
        self.last_stats["corrupt"] += stats["corrupt"]
        self.last_stats["fragment"] += stats["fragment"]
        self.last_stats["files"].append("archive/" + name)
        return records

    def read_archive_range(self, low_exclusive: int, high_exclusive: int) -> List[Dict[str, Any]]:
        """Archived records with ``low < seq < high``, in seq order."""
        out: List[Dict[str, Any]] = []
        for seg in self.archive_segments():
            if seg["last_seq"] <= low_exclusive or seg["first_seq"] >= high_exclusive:
                continue
            out.extend(r for r in self._read_segment(seg["file"]) if low_exclusive < r["seq"] < high_exclusive)
        out.sort(key=lambda r: r["seq"])
        return out

    # -- read ----------------------------------------------------------------

    def _read_active(self) -> List[Dict[str, Any]]:
        data = read_bytes(self.team.board_jsonl)
        if data is None:
            return []
        records, stats = parse_lines(data)
        self.last_stats["corrupt"] += stats["corrupt"]
        self.last_stats["fragment"] += stats["fragment"]
        self.last_stats["files"].append("board.jsonl")
        return records

    def _reset_stats(self) -> None:
        self.last_stats = {"corrupt": 0, "fragment": 0, "duplicates": 0, "files": []}

    def _dedupe_sorted(self, records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen: Dict[int, Dict[str, Any]] = {}
        for rec in records:
            if rec["seq"] in seen:
                self.last_stats["duplicates"] += 1
                continue
            seen[rec["seq"]] = rec
        return [seen[k] for k in sorted(seen)]

    def read(
        self,
        since_seq: int = 0,
        to: Optional[Iterable[str]] = None,
        from_name: Optional[str] = None,
        kind: Optional[str] = None,
        thread: Optional[int] = None,
        last: Optional[int] = None,
        limit: Optional[int] = None,
        include_archive: bool = False,
        include_retracted: bool = True,
    ) -> List[Dict[str, Any]]:
        """Records with ``seq > since_seq`` matching the filters, sorted, deduped by seq.

        Archive segments are consulted when ``include_archive`` is set, when
        ``since_seq`` or ``thread`` falls below the active file's first seq,
        or when ``last`` asks for more records than the active file holds.
        ``self.last_stats`` carries corrupt-line, fragment, and duplicate
        counts for the call; ``read_detailed`` returns them alongside.

        This is the storage view: ``retract`` records and the posts they
        retract are returned unless ``include_retracted=False`` (the
        ``board`` command's default view); see ``filter_retracted``.
        """
        return self.read_detailed(since_seq, to, from_name, kind, thread, last, limit, include_archive, include_retracted)[0]

    def read_detailed(
        self,
        since_seq: int = 0,
        to: Optional[Iterable[str]] = None,
        from_name: Optional[str] = None,
        kind: Optional[str] = None,
        thread: Optional[int] = None,
        last: Optional[int] = None,
        limit: Optional[int] = None,
        include_archive: bool = False,
        include_retracted: bool = True,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        self._reset_stats()
        since = max(0, int(since_seq or 0))
        active = self._read_active()
        active_first = active[0]["seq"] if active else 0
        wants_archive = include_archive
        if not wants_archive and active_first > 1:
            if 0 < since < active_first - 1:
                wants_archive = True
            elif thread is not None and int(thread) < active_first:
                wants_archive = True
            elif last is not None and len([r for r in active if r["seq"] > since]) < int(last):
                wants_archive = True
        if not wants_archive and not active and (last is not None or since > 0):
            wants_archive = bool(self._archive_filenames())
        records = list(active)
        if wants_archive:
            high = active_first if active_first else 1 << 62
            records = self.read_archive_range(since, high) + records
        records = self._dedupe_sorted(records)
        records = [r for r in records if r["seq"] > since]
        records = self._apply_filters(records, to, from_name, kind, thread)
        if not include_retracted:
            records = filter_retracted(records, False)
        if last is not None and int(last) >= 0:
            records = records[-int(last):] if int(last) > 0 else []
        if limit is not None and int(limit) >= 0:
            records = records[: int(limit)]
        return records, dict(self.last_stats)

    @staticmethod
    def _apply_filters(
        records: List[Dict[str, Any]],
        to: Optional[Iterable[str]],
        from_name: Optional[str],
        kind: Optional[str],
        thread: Optional[int],
    ) -> List[Dict[str, Any]]:
        out = records
        if to is not None:
            wanted = set(to)
            out = [r for r in out if wanted.intersection(r.get("to") or [])]
        if from_name is not None:
            out = [r for r in out if r.get("from") == from_name]
        if kind is not None:
            out = [r for r in out if r.get("kind") == kind]
        if thread is not None:
            root = int(thread)
            out = [r for r in out if r["seq"] == root or r.get("reply_to") == root or r.get("supersedes") == root or r.get("retracts") == root]
        return out

    def get(self, seq: int) -> Optional[Dict[str, Any]]:
        target = int(seq)
        self._reset_stats()
        for rec in self._read_active():
            if rec["seq"] == target:
                return rec
        for seg in self.archive_segments():
            if seg["first_seq"] <= target <= seg["last_seq"]:
                for rec in self._read_segment(seg["file"]):
                    if rec["seq"] == target:
                        return rec
        return None

    def tail_state(self) -> Dict[str, Any]:
        """``{"inode": int, "size": int, "max_seq": int, "active_first_seq": int}`` for tailers."""
        try:
            st = os.stat(self.team.board_jsonl)
            inode, size = st.st_ino, st.st_size
        except FileNotFoundError:
            inode, size = 0, 0
        tail_max, first, _ = self._tail_scan()
        max_seq = max(tail_max, self._seq_hint(), self._archive_max())
        return {"inode": inode, "size": size, "max_seq": max_seq, "active_first_seq": first or self.active_first_seq()}

    # -- attachments ---------------------------------------------------------

    def stage_attachment(self, path: PathLike) -> StagedAttachment:
        """Copy ``path`` to ``payloads/.tmp-<pid>-<n>`` before the lock (16 MiB cap)."""
        source = Path(path)
        try:
            st = os.lstat(source)
        except FileNotFoundError:
            raise HerdrTeamError("ref_invalid", "attachment does not exist", EXIT_REFUSED, {"path": os.fspath(source)})
        if stat.S_ISLNK(st.st_mode):
            raise HerdrTeamError("path_symlink", "refusing to attach a symlink", EXIT_REFUSED, {"path": os.fspath(source)})
        if not stat.S_ISREG(st.st_mode):
            raise HerdrTeamError("ref_invalid", "attachment is not a regular file", EXIT_REFUSED, {"path": os.fspath(source)})
        if st.st_size > MAX_ATTACHMENT_BYTES:
            raise HerdrTeamError(
                "attachment_too_large", "attachment exceeds 16 MiB", EXIT_REFUSED,
                {"path": os.fspath(source), "bytes": st.st_size, "max_bytes": MAX_ATTACHMENT_BYTES},
            )
        ensure_dir(self.team.payloads_dir)
        tmp = self.team.payloads_dir / "{}{}-{}".format(STAGING_PREFIX, os.getpid(), next(self._staging_counter))
        src_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            dst_fd = secure_open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
            try:
                copied = 0
                while True:
                    chunk = os.read(src_fd, ATTACHMENT_COPY_CHUNK)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > MAX_ATTACHMENT_BYTES:
                        raise HerdrTeamError("attachment_too_large", "attachment grew past 16 MiB while copying", EXIT_REFUSED, {"path": os.fspath(source)})
                    view = memoryview(chunk)
                    while len(view):
                        view = view[os.write(dst_fd, view):]
                os.fsync(dst_fd)
            except BaseException:
                os.close(dst_fd)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            os.close(dst_fd)
        finally:
            os.close(src_fd)
        return StagedAttachment(tmp_path=tmp, basename=safe_basename(os.fsdecode(source.name)), size=copied, source=os.fspath(source))

    def _commit_attachment(self, seq: int, staged: StagedAttachment) -> Path:
        """Under the lock: ``payloads/<seq>-<basename>`` via ``link`` (exclusive), then drop the temp."""
        ensure_dir(self.team.payloads_dir)
        stem, ext = os.path.splitext(staged.basename)
        attempt = 0
        while True:
            name = "{}-{}{}{}".format(seq, stem, "" if attempt == 0 else "-{}".format(attempt + 1), ext)
            target = self.team.payloads_dir / name
            check_not_symlink(target)
            try:
                os.link(staged.tmp_path, target)
                break
            except FileExistsError:
                attempt += 1
                if attempt > 99:
                    raise HerdrTeamError("board_write_failed", "could not place attachment", EXIT_REFUSED, {"path": os.fspath(target)})
        try:
            os.unlink(staged.tmp_path)
        except OSError:
            pass
        staged.committed = "payloads/" + name
        return target

    def discard_staged(self, staged: Iterable[StagedAttachment]) -> None:
        for item in staged:
            if item.committed is None:
                try:
                    os.unlink(item.tmp_path)
                except OSError:
                    pass

    def cleanup_stale_staging(self, max_age_s: float = 3600.0) -> List[Path]:
        """Remove ``payloads/.tmp-*`` older than ``max_age_s`` (abandoned by a dead poster)."""
        removed: List[Path] = []
        try:
            names = os.listdir(self.team.payloads_dir)
        except FileNotFoundError:
            return removed
        cutoff = time.time() - max_age_s
        for name in names:
            if not name.startswith(STAGING_PREFIX):
                continue
            path = self.team.payloads_dir / name
            try:
                st = os.lstat(path)
                if stat.S_ISREG(st.st_mode) and st.st_mtime < cutoff:
                    os.unlink(path)
                    removed.append(path)
            except OSError:
                continue
        return removed


class Cursors:
    """Per-reader read cursors under ``team.lock`` (plan 6.2)."""

    def __init__(self, team: TeamPaths, board: Optional[BoardStore] = None) -> None:
        self.team = team
        self.board = board if board is not None else BoardStore(team)

    def path_for(self, reader: str) -> Path:
        if reader.startswith("human@"):
            return self.team.human_cursor(reader[len("human@"):])
        return self.team.cursor(reader)

    @staticmethod
    def _empty(warning: Optional[str]) -> Dict[str, Any]:
        return {"v": 1, "seq": 0, "terminal_id": None, "surfaced_by": None, "updated": None, "warning": warning}

    def get(self, reader: str) -> Dict[str, Any]:
        """``{"v":1,"seq":N,"terminal_id":...,"surfaced_by":"cli|hook","updated":ts}``; seq 0 when absent or corrupt.

        Adds ``warning`` (``None`` when clean); a seq beyond the board max is clamped.
        """
        path = self.path_for(reader)
        raw = read_bytes(path)
        if raw is None:
            return self._empty("cursor missing for {}: starting at 0".format(reader))
        try:
            doc = json.loads(raw.decode("utf-8"))
        except ValueError:
            _log.warning("cursor %s is corrupt; using 0", path)
            return self._empty("cursor corrupt for {}: using 0".format(reader))
        if not isinstance(doc, dict) or not _is_int(doc.get("seq")) or doc["seq"] < 0:
            _log.warning("cursor %s has no valid seq; using 0", path)
            return self._empty("cursor corrupt for {}: using 0".format(reader))
        seen_raw = doc.get("seen")
        seen = sorted({s for s in seen_raw if _is_int(s) and s > doc["seq"]}) if isinstance(seen_raw, list) else []
        out = {
            "v": 1, "seq": doc["seq"], "terminal_id": doc.get("terminal_id"),
            "surfaced_by": doc.get("surfaced_by"), "updated": doc.get("updated"), "warning": None, "seen": seen,
        }
        max_seq = self.board.max_seq()
        if out["seq"] > max_seq:
            out["warning"] = "cursor {} beyond board max {}: clamped".format(out["seq"], max_seq)
            out["seq"] = max_seq
        return out

    def advance(self, reader: str, seq: int, terminal_id: Optional[str], surfaced_by: str, seen: Optional[Iterable[int]] = None, touch: bool = False) -> Dict[str, Any]:
        """Move forward only; never backwards. ``reader`` is a member name or ``human@<label>``.

        ``seq`` is the highest seq the caller actually printed; it is clamped
        to the board max and ignored when it does not exceed the stored seq.
        ``seen`` names seqs above ``seq`` that were printed individually (a
        filtered ``board --new``); they are excluded from later reads, and the
        cursor slides over them once everything below is read, so nothing
        unread is ever skipped (plan 6.2).

        ``touch`` rewrites the file (fresh ``updated``, ``surfaced_by``,
        ``terminal_id``) even when nothing moves. ``herdr-team ack`` uses it:
        the daemon recognises an acknowledgement by a cursor write after the
        briefing landed, and a member whose cursor was already at the board
        max (the join sets it there) would otherwise never produce one.
        Observed in the sandbox on 2026-09-05: a Claude acked at seq 1 = 1,
        the file kept its join timestamp, and the daemon re-briefed it.
        """
        path = self.path_for(reader)
        target = max(0, int(seq))
        extra = {int(s) for s in (seen or []) if _is_int(s)}
        with team_lock(self.team, self.board.lock_timeout):
            max_seq = self.board.max_seq()
            if target > max_seq:
                target = max_seq
            current = self.get(reader)
            new_seq = max(target, current["seq"])
            new_seen = {s for s in set(current.get("seen") or []) | extra if s > new_seq}
            while new_seq + 1 in new_seen:
                new_seq += 1
                new_seen.discard(new_seq)
            if new_seq <= current["seq"] and new_seen == set(current.get("seen") or []) and not touch:
                current["advanced"] = False
                return current
            doc: Dict[str, Any] = {"v": 1, "seq": new_seq, "terminal_id": terminal_id, "surfaced_by": surfaced_by, "updated": now_iso()}
            if new_seen:
                doc["seen"] = sorted(new_seen)
            ensure_dir(self.team.cursors_dir)
            write_json(path, doc)
        doc.setdefault("seen", [])
        doc["warning"] = None
        doc["advanced"] = new_seq > current["seq"]
        return doc

    def all(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        try:
            names = os.listdir(self.team.cursors_dir)
        except FileNotFoundError:
            return out
        for name in sorted(names):
            if not name.endswith(".json") or name.startswith(STAGING_PREFIX):
                continue
            reader = name[:-5]
            try:
                out[reader] = self.get(reader)
            except HerdrTeamError:
                continue
        return out


class BoardTailer:
    """The tailer contract (plan 6.3) for the daemon and the console.

    State ``(inode, offset, watermark_seq)`` persists in ``notifier/state.json``
    (or stays in memory with ``persist=False``). ``poll()`` returns complete
    new records in seq order, reads archive segments across a rotation gap,
    tolerates ENOENT between a rotation and the next create, and returns a
    synthetic ``reset_detected`` system record (``seq`` ``None``,
    ``synthetic`` ``True``) when a reopened file restarts at or below the
    watermark with no archive explanation. A first open with a watermark
    that came from ``start_seq`` or persisted state simply skips older seqs.
    The tailer never writes the board;
    the daemon appends the reset note if it wants it recorded.
    """

    ENOENT_LOG_S = TAILER_ENOENT_LOG_S

    def __init__(
        self,
        team: TeamPaths,
        state_path: Optional[PathLike] = None,
        start_seq: Optional[int] = None,
        persist: bool = True,
        board: Optional[BoardStore] = None,
    ) -> None:
        self.team = team
        self.board = board if board is not None else BoardStore(team)
        self.state_path = Path(state_path) if state_path is not None else team.notifier_state
        self.persist = persist
        self.inode: Optional[int] = None
        self.offset = 0
        self.watermark_seq = 0
        self.fd: Optional[int] = None
        self.warnings: List[str] = []
        self.resets = 0
        self._missing_since: Optional[float] = None
        self._missing_logged = False
        self._dirty = False
        if not self.load_state() and start_seq is not None:
            self.watermark_seq = max(0, int(start_seq))

    # -- state -------------------------------------------------------------

    def load_state(self) -> bool:
        if not self.persist:
            return False
        doc = read_json(self.state_path)
        if not isinstance(doc, dict) or doc.get("v") != TAILER_STATE_VERSION:
            return False
        if not (_is_int(doc.get("offset")) and _is_int(doc.get("watermark_seq"))):
            return False
        inode = doc.get("inode")
        self.inode = inode if _is_int(inode) and inode > 0 else None
        self.offset = max(0, doc["offset"])
        self.watermark_seq = max(0, doc["watermark_seq"])
        return True

    def save_state(self) -> None:
        self._dirty = False
        if not self.persist:
            return
        doc = {"v": TAILER_STATE_VERSION, "inode": self.inode or 0, "offset": self.offset, "watermark_seq": self.watermark_seq, "updated": now_iso()}
        ensure_dir(self.state_path.parent)
        write_json(self.state_path, doc, fsync=False)

    @property
    def state(self) -> Dict[str, Any]:
        return {"inode": self.inode, "offset": self.offset, "watermark_seq": self.watermark_seq}

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None

    def __enter__(self) -> "BoardTailer":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- polling -----------------------------------------------------------

    def poll(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        path = self.team.board_jsonl
        try:
            check_not_symlink(path)
            st: Optional[os.stat_result] = os.stat(path)
        except FileNotFoundError:
            st = None
        if st is None:
            if self.fd is not None:
                out.extend(self._drain())
                self.close()
            now = time.monotonic()
            if self._missing_since is None:
                self._missing_since = now
            elif not self._missing_logged and now - self._missing_since >= self.ENOENT_LOG_S:
                self._missing_logged = True
                self._warn("board.jsonl missing for {:.0f}s".format(now - self._missing_since))
        else:
            self._missing_since = None
            self._missing_logged = False
            if self.fd is not None and st.st_ino != self.inode:
                out.extend(self._drain())
                self.close()
            if self.fd is None:
                out.extend(self._open(st))
            else:
                size = os.fstat(self.fd).st_size
                if size < self.offset:
                    self._warn("board.jsonl shrank from {} to {}: reopening".format(self.offset, size))
                    self.close()
                    self.offset = 0
                    out.extend(self._open(st, resume=False))
                elif size != self.offset and not self._boundary_ok(self.fd, self.offset):
                    # Plan 6.3: a same-inode truncate-and-rewrite that already grew past the
                    # stale offset (``cp backup board.jsonl``) is caught by the byte before
                    # the offset no longer being a newline; the appender leaves a complete
                    # line there, so a mismatch can only mean the file was replaced.
                    self._warn("board.jsonl rewritten under offset {} (no line boundary): reopening".format(self.offset))
                    self.close()
                    self.offset = 0
                    out.extend(self._open(st, resume=False))
                else:
                    out.extend(self._read_new())
        if self._dirty:
            self.save_state()
        return out

    def _warn(self, message: str) -> None:
        self.warnings.append(message)
        _log.warning("tailer %s: %s", self.team.name, message)

    def _drain(self) -> List[Dict[str, Any]]:
        """Read the old fd to EOF before letting go of it (rotation or replacement)."""
        if self.fd is None:
            return []
        return self._read_new()

    def _open(self, st: os.stat_result, resume: bool = True) -> List[Dict[str, Any]]:
        fd = secure_open(self.team.board_jsonl, os.O_RDONLY)
        fst = os.fstat(fd)
        self.fd = fd
        same_inode = self.inode == fst.st_ino
        if resume and same_inode and 0 < self.offset <= fst.st_size and self._boundary_ok(fd, self.offset):
            return self._read_new()
        # A new file, a replaced one, or the same inode truncated or rewritten
        # under a stale offset: start from zero and reason about its first seq.
        # Only a file we had already made progress on can be a "reopen".
        reopened = (not resume) or (self.inode is not None and (not same_inode or self.offset > 0))
        self.inode = fst.st_ino
        self.offset = 0
        self._dirty = True
        records = self._read_complete_lines()
        out: List[Dict[str, Any]] = []
        if records:
            first_seq = records[0]["seq"]
            if first_seq > self.watermark_seq + 1:
                out.extend(self.board.read_archive_range(self.watermark_seq, first_seq))
            elif reopened and first_seq <= self.watermark_seq and self.watermark_seq > 0 and not self._archive_explains(first_seq):
                self.resets += 1
                self._warn("reset detected: first seq {} at or below watermark {}".format(first_seq, self.watermark_seq))
                out.append(self._reset_record(first_seq))
                self.watermark_seq = 0
            for rec in out:
                if rec.get("seq") is not None:
                    self.watermark_seq = max(self.watermark_seq, rec["seq"])
            out.extend(self._accept(records))
        return out

    @staticmethod
    def _boundary_ok(fd: int, offset: int) -> bool:
        return offset == 0 or os.pread(fd, 1, offset - 1) == b"\n"

    def _archive_explains(self, seq: int) -> bool:
        for seg in self.board.archive_segments():
            if seg["first_seq"] <= seq <= seg["last_seq"]:
                return True
        return False

    def _reset_record(self, first_seq: int) -> Dict[str, Any]:
        rec = normalize_record({
            "from": "system", "kind": "system", "event": "reset_detected", "to": ["all"],
            "text": "board reset detected: file restarts at seq {} while the watermark was {}".format(first_seq, self.watermark_seq),
        })
        rec["seq"] = None
        rec["ts"] = now_iso()
        rec["synthetic"] = True
        return rec

    def _read_complete_lines(self) -> List[Dict[str, Any]]:
        """Read from ``offset`` to EOF, keep complete lines, advance ``offset`` past them."""
        assert self.fd is not None
        chunks: List[bytes] = []
        pos = self.offset
        while True:
            chunk = os.pread(self.fd, 1 << 20, pos)
            if not chunk:
                break
            chunks.append(chunk)
            pos += len(chunk)
        data = b"".join(chunks)
        if not data:
            return []
        cut = data.rfind(b"\n")
        if cut < 0:
            return []
        complete = data[: cut + 1]
        records, stats = parse_lines(complete)
        if stats["corrupt"]:
            self._warn("{} corrupt line(s) skipped".format(stats["corrupt"]))
        self.offset += len(complete)
        self._dirty = True
        return records

    def _accept(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for rec in records:
            if rec["seq"] <= self.watermark_seq:
                continue
            self.watermark_seq = rec["seq"]
            self._dirty = True
            out.append(rec)
        return out

    def _read_new(self) -> List[Dict[str, Any]]:
        """Read past ``offset`` on the open fd; a same-inode rewrite restarting at or below the watermark is a reset.

        ``cp backup board.jsonl`` truncates and rewrites the *same* inode;
        when the restored file already exceeds the stale offset by the next
        tick, neither the inode nor the size gives it away. The appender
        always writes ``max + 1``, so bytes beyond our offset that carry a
        seq at or below the watermark with no archive explanation can only
        come from a replaced file (plan 6.3).
        """
        records = self._read_complete_lines()
        if records and self.watermark_seq > 0:
            first_seq = records[0]["seq"]
            if first_seq <= self.watermark_seq and not self._archive_explains(first_seq):
                self.resets += 1
                self._warn("reset detected: seq {} appeared after offset while the watermark was {}".format(first_seq, self.watermark_seq))
                reset = self._reset_record(first_seq)
                self.watermark_seq = 0
                self._dirty = True
                return [reset] + self._accept(records)
        return self._accept(records)


# --------------------------------------------------------------------------
# roster (roster implementer)


class RosterStore:
    """``team.json`` reads and revision-checked writes under ``team.lock`` (plan 5.1).

    Dict-level primitives shared by ``roster`` (which wraps them in ``Team``),
    ``cmd_board``, the daemon, and the hooks: temp + rename under ``team.lock``,
    a ``revision`` check, ``MAX_RETRIES`` retries on conflict.
    """

    MAX_RETRIES = 3

    def __init__(self, team: TeamPaths) -> None:
        self.team = team

    def load(self) -> Dict[str, Any]:
        """Parsed ``team.json`` or raise ``team_not_found``."""
        doc = read_json(self.team.team_json, default=None)
        if not isinstance(doc, dict):
            raise HerdrTeamError(
                "team_not_found", "team {!r} does not exist".format(self.team.name), EXIT_REFUSED,
                {"team": self.team.name, "path": os.fspath(self.team.team_json)},
            )
        return doc

    def current_revision(self) -> Optional[int]:
        """The stored revision, 0 when unreadable, None when the file is missing."""
        doc = read_json(self.team.team_json, default=None)
        if not isinstance(doc, dict):
            return None
        try:
            return int(doc.get("revision") or 0)
        except (TypeError, ValueError):
            return 0

    def _save_locked(self, doc: Dict[str, Any], expected_revision: Optional[int]) -> Dict[str, Any]:
        current = self.current_revision()
        if expected_revision is not None and current is not None and current != expected_revision:
            raise HerdrTeamError(
                "roster_conflict",
                "team.json moved from revision {} to {} under us".format(expected_revision, current),
                EXIT_REFUSED,
                {"team": self.team.name, "expected": expected_revision, "current": current},
            )
        try:
            own = int(doc.get("revision") or 0)
        except (TypeError, ValueError):
            own = 0
        doc["revision"] = (current if current is not None else own) + 1
        write_json(self.team.team_json, doc)
        return doc

    def save(self, doc: Dict[str, Any], expected_revision: Optional[int] = None) -> Dict[str, Any]:
        """Bump ``revision``; refuse with ``roster_conflict`` when it moved under us."""
        ensure_team_dirs(self.team)
        with team_lock(self.team):
            return self._save_locked(doc, expected_revision)

    def update(self, mutate: Callable[[Dict[str, Any]], Any], retries: Optional[int] = None) -> Dict[str, Any]:
        """Lock, load, ``mutate(doc)`` in place, save; retries ``MAX_RETRIES`` on conflict."""
        attempts = max(1, int(retries if retries is not None else self.MAX_RETRIES))
        last: Optional[HerdrTeamError] = None
        for _attempt in range(attempts):
            with team_lock(self.team):
                doc = self.load()
                try:
                    expected = int(doc.get("revision") or 0)
                except (TypeError, ValueError):
                    expected = 0
                mutate(doc)
                try:
                    return self._save_locked(doc, expected)
                except HerdrTeamError as err:
                    if err.code != "roster_conflict":
                        raise
                    last = err
            time.sleep(0.01)
        assert last is not None
        raise last
