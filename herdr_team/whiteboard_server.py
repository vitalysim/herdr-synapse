"""The whiteboard page server: a loopback-only HTTP server for the canvas, team views and activity (0.21).

Contract: ``.local/prd/canvas-contracts.md`` section 13. A stdlib
``ThreadingHTTPServer`` bound to ``127.0.0.1`` on a random free port, one
per Herdr session, recorded in ``<session>/whiteboard.json``. It serves the
prebuilt page from ``web/dist/`` (Vite, React and Excalidraw; Python users
never need Node), a small JSON API over the team's canvas, views and
activity, one Server-Sent Events stream per open page, and the sealed frames
agent ``viz`` code runs in.

Who may use it (a speed bump against the obvious routes, not a sandbox; any
process running as this user can read the state directory):

* ``whiteboard open`` mints a one-use ticket (32 random bytes, 120 s) in
  ``<session>/whiteboard-tickets/``; ``GET /?ticket=`` swaps it for an
  in-memory session and answers ``303`` with an ``HttpOnly``,
  ``SameSite=Strict`` cookie, so the ticket leaves the address bar at once.
* Every request: ``Host`` must be ``127.0.0.1:<port>`` or
  ``localhost:<port>`` (DNS rebinding); a present ``Origin`` must be this
  server's; ``Sec-Fetch-Site`` must not be ``cross-site`` or ``same-site``
  (another page on another localhost port is "same-site" and would carry the
  Strict cookie). Every write also needs ``Origin`` and the session's CSRF
  token in ``X-Synapse-CSRF``. No CORS headers except on ``/viz-lib/``.
* A page write is the operator's only when the server was started by the
  operator in person (``trusted_human``) and the ticket was minted for that
  author; otherwise every write answers ``403 read_only``.
* The page runs under a strict CSP (no inline script, no remote origins,
  ``wasm-unsafe-eval`` only). Agent ``viz`` HTML runs only in
  ``<iframe sandbox="allow-scripts">`` loaded from ``/viz/<team>/<id>``,
  whose own CSP forbids every network request and lets scripts load only
  from the vendored ``/viz-lib/``.

It is not the notifier: it does work only while a page is connected (one
file-stat poll per stream every ``POLL_S``), and exits on SIGTERM, when the
session layer is switched off, or after ``IDLE_TIMEOUT_S`` with no page.
The canvas itself belongs to ``herdr_team.canvas``: this module only turns
HTTP into its calls (the human's edits become operations authored by
``canvas.page_author``) and streams its events back.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from herdr_team import VERSION, features, store
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError
from herdr_team.paths import TEAM_NAME_RE, SessionPaths, TeamPaths, check_not_symlink, ensure_dir, plugin_root

STATE_FILE = "whiteboard.json"
LOG_FILE = "whiteboard.log"
TICKETS_DIR = "whiteboard-tickets"
COOKIE_NAME = "synapse_wb"
CSRF_HEADER = "X-Synapse-CSRF"
TICKET_TTL_S = 120.0
IDLE_TIMEOUT_S = 1800.0
POLL_S = 0.5
KEEPALIVE_S = 15.0
MAX_BODY_BYTES = 6 * 1024 * 1024
MAX_STREAMS = 8
#: Page sessions kept in memory; opening the page again past this drops the oldest session.
MAX_SESSIONS = 64
START_WAIT_S = 5.0
STOP_WAIT_S = 3.0
#: ``beat_at`` in ``whiteboard.json`` is refreshed this often.
BEAT_S = 30.0
#: The foreground loop of ``serve``: how often it checks the layer switch, idleness and a stop request.
LOOP_S = 1.0
#: How long ``serve`` lets open streams send ``bye`` before it closes the socket.
DRAIN_S = 2.0
#: SSE ``views`` at most this often; ``activity`` is recomputed at most this often.
VIEWS_MIN_S = 2.0
ACTIVITY_MIN_S = 5.0
SCHEMA = 1
BIND_HOST = "127.0.0.1"
#: What ``start`` tells the ``whiteboard-serve`` process about its starter.
ENV_WRITABLE = "HERDR_SYNAPSE_WB_WRITABLE"
ENV_STARTED_BY = "HERDR_SYNAPSE_WB_BY"

PAGE_CSP = ("default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; frame-src 'self'; "
            "worker-src 'self' blob:; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
#: JSON answers are never meant to render; this CSP makes sure they cannot.
API_CSP = "default-src 'none'; frame-ancestors 'none'"
ASSET_CSP = "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"
VIZ_CSP = ("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline' http://127.0.0.1:{port}/viz-lib/; "
           "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'none'; "
           "form-action 'none'; base-uri 'none'; frame-ancestors http://127.0.0.1:{port} http://localhost:{port}")
#: The vendored libraries a ``viz`` element may ask for, as files under ``web/dist/viz-lib/``.
VIZ_LIB_FILES = {"d3": "d3.min.js", "three": "three.module.min.js", "p5": "p5.min.js"}
VIZ_RUNTIME = "synapse-viz.js"
#: Static paths served with ``immutable`` caching: Vite's content-hashed chunks and Excalidraw's hashed fonts.
IMMUTABLE_PREFIXES = ("assets/", "fonts/")

MIME_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json", ".map": "application/json",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".ico": "image/x-icon", ".webp": "image/webp", ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
    ".otf": "font/otf", ".wasm": "application/wasm", ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8", ".tsv": "text/tab-separated-values; charset=utf-8",
}
#: ``canvas.store_asset`` names, minus ``.html`` (viz sources are never served as a document here).
ASSET_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|jpeg|svg|vl\.json|json)\Z")
ELEMENT_ID_RE = re.compile(r"^E-[1-9][0-9]{0,6}\Z")
CANONICAL_ID_RE = re.compile(r"^(E|C|K|X|G|B)-[1-9][0-9]{0,6}\Z")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
TICKET_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}\Z")
VIZ_LIB_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
MAX_SEND_IDS = 200
MAX_NOTE_CHARS = 1000

#: ``HerdrTeamError.code`` -> HTTP status (contract 13.4). Anything else is 500 ``internal``.
HTTP_STATUS = {
    "usage": 400, "op_invalid": 400, "element_unknown": 400, "svg_refused": 400, "chart_refused": 400,
    "mention_unknown": 400, "secret_detected": 400, "echo_rejected": 400,
    "not_signed_in": 401,
    "whiteboard_off": 403, "viz_off": 403, "author_mismatch": 403, "author_unverified": 403, "operator_only": 403,
    "element_not_yours": 403, "read_only": 403, "bad_host": 403, "bad_origin": 403, "bad_csrf": 403, "path_refused": 403,
    "canvas_locked": 403, "not_a_member": 403,
    "team_not_found": 404, "not_found": 404, "artifacts_unset": 404, "member_not_found": 404,
    "method_not_allowed": 405,
    "canvas_stale": 409, "canvas_refused": 409, "alias_taken": 409,
    "canvas_limit": 413, "image_refused": 413, "body_too_large": 413,
    "unsupported_media_type": 415,
    "canvas_rate": 429,
    "canvas_busy": 503, "page_not_built": 503, "too_many_streams": 503,
}

NOT_SIGNED_IN_HTML = (
    "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>Synapse whiteboard</title>"
    "<style>body{{font:16px/1.5 system-ui,sans-serif;margin:3em auto;max-width:36em;color:#1e1e1e}}"
    "code{{background:#f1f3f5;padding:.1em .3em;border-radius:4px}}</style>"
    "<h1>Open the whiteboard from Herdr</h1><p>{reason}</p>"
    "<p>Press <b>prefix+a</b> in Herdr, or run <code>herdr-synapse whiteboard open</code>. "
    "Each link opens the page once.</p></html>"
)


def _spawn(argv: List[str], env: Dict[str, str], log_path: Path) -> int:
    """Start the detached ``whiteboard-serve`` process; its pid."""
    log_fd = os.open(os.fspath(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
    try:
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd, start_new_session=True, close_fds=True)
    finally:
        os.close(log_fd)
    return proc.pid


#: Test hook: ``SPAWN(argv, env, log_path) -> pid``.
SPAWN: Callable[[List[str], Dict[str, str], Path], int] = _spawn
#: Test hook: signals the recorded server process (``os.kill``).
KILL: Callable[[int, int], None] = os.kill
#: Test hook: opens the page for ``start(open_browser=True)``.
OPEN_BROWSER: Callable[[str], bool] = webbrowser.open


def _stderr_line(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


#: Test hook: where the server process writes its log lines (stderr, which ``start`` sends to ``whiteboard.log``).
LOG: Callable[[str], None] = _stderr_line

#: Servers running inside this process, by session dir: ``stop`` never signals its own pid.
_LOCAL: Dict[str, "WhiteboardServer"] = {}


# --------------------------------------------------------------------------
# lazy neighbours: the CLI imports this module for every command, the page's
# dependencies only matter while a server runs


def _canvas() -> Any:
    from herdr_team import canvas as _module

    return _module


def _views() -> Any:
    from herdr_team import views as _module

    return _module


def _activity() -> Any:
    from herdr_team import activity as _module

    return _module


def _process_start_time(pid: int) -> Optional[str]:
    from herdr_team import daemon as _daemon

    return _daemon.process_start_time(pid)


def _pid_alive(pid: int) -> bool:
    from herdr_team import daemon as _daemon

    return _daemon.pid_alive(pid)


# --------------------------------------------------------------------------
# small helpers


def _iso(epoch: float) -> str:
    moment = datetime.fromtimestamp(epoch, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(moment.microsecond // 1000)


def _parse_iso(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _stat_sig(*paths: Path) -> Tuple[Any, ...]:
    """A cheap change signature of files: inode, size and mtime, or None per missing file."""
    out: List[Any] = []
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            out.append(None)
            continue
        out.append((st.st_ino, st.st_size, st.st_mtime_ns))
    return tuple(out)


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def web_dist() -> Path:
    """The prebuilt page: ``<plugin>/web/dist``."""
    return plugin_root() / "web" / "dist"


def state_path(session: SessionPaths) -> Path:
    """``<session>/whiteboard.json``: the running server."""
    return session.root / STATE_FILE


def tickets_dir(session: SessionPaths) -> Path:
    """``<session>/whiteboard-tickets/``: one file per unused opening ticket, named by its hash."""
    return session.root / TICKETS_DIR


def log_path(session: SessionPaths) -> Path:
    """``<session>/whiteboard.log``: the server process's stderr."""
    return session.root / LOG_FILE


def page_url(port: int) -> str:
    return "http://{}:{}/".format(BIND_HOST, int(port))


# --------------------------------------------------------------------------
# the state file and the process


def _read_state(session: SessionPaths) -> Optional[Dict[str, Any]]:
    try:
        rec = store.read_json(state_path(session), default=None)
    except (HerdrTeamError, OSError):
        return None
    if not isinstance(rec, dict) or _int(rec.get("pid"), 0) <= 0 or _int(rec.get("port"), 0) <= 0:
        return None
    return rec


def _remove_state(session: SessionPaths, pid: Optional[int] = None) -> bool:
    """Remove ``whiteboard.json``, only while it still names ``pid`` when one is given."""
    path = state_path(session)
    if pid is not None:
        rec = _read_state(session)
        if rec is not None and _int(rec.get("pid")) != int(pid):
            return False
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


def _alive(rec: Dict[str, Any]) -> bool:
    """The daemon's rule: a live pid whose start time still matches the one recorded."""
    pid = _int(rec.get("pid"), 0) or 0
    if pid <= 0 or not _pid_alive(pid):
        return False
    recorded = str(rec.get("start_time") or "").strip()
    if not recorded:
        return True
    live = _process_start_time(pid)
    return live is not None and live == recorded


def status(layout: Any) -> Optional[Dict[str, Any]]:
    """The running server's record with ``alive`` and ``url`` (no ticket), or None (a stale record is removed)."""
    session = layout.session
    rec = _read_state(session)
    if rec is None:
        return None
    if not _alive(rec):
        _remove_state(session, _int(rec.get("pid")))
        return None
    out = dict(rec)
    out["alive"] = True
    out["url"] = page_url(int(rec["port"]))
    beat = _parse_iso(rec.get("beat_at"))
    out["beat_age_s"] = round(max(0.0, time.time() - beat), 1) if beat is not None else None
    return out


def _spawn_server(layout: Any, env: Mapping[str, str], writable: bool, by: Dict[str, Any]) -> Dict[str, Any]:
    session = layout.session
    ensure_dir(session.root)
    argv = [os.fspath(plugin_root() / "bin" / "herdr-synapse"), "--socket", os.fspath(layout.socket), "whiteboard-serve"]
    child_env = dict(env)
    # The child resolves the same state root as its starter, whatever flag or variable chose it.
    child_env["HERDR_TEAM_STATE_DIR"] = os.fspath(layout.state_root.path)
    child_env[ENV_WRITABLE] = "1" if writable else "0"
    child_env[ENV_STARTED_BY] = json.dumps(by, separators=(",", ":"))
    log = log_path(session)
    pid = SPAWN(argv, child_env, log)
    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        rec = status(layout)
        if rec is not None and _int(rec.get("pid")) == int(pid):
            return rec
        time.sleep(0.05)
    raise HerdrTeamError(
        "whiteboard_start_failed",
        "the whiteboard page server did not start within {:.0f} s; see {}".format(START_WAIT_S, log),
        EXIT_REFUSED, {"log": os.fspath(log), "pid": pid},
    )


def start(layout: Any, env: Mapping[str, str], author: Any, open_browser: bool = False) -> Dict[str, Any]:
    """Start the server if needed and mint an opening ticket; ``{"url", "port", "pid", "writable", "started"}``.

    The page is for people: members refuse ``author_mismatch`` (they draw
    through ``canvas``). The operator in person gets a writable page; a
    human the board cannot vouch for gets a read-only one. A running
    read-only server is restarted when the operator in person opens it, so
    the operator never has to stop it by hand to edit.
    """
    features.require_layer(layout.session)
    if not getattr(author, "is_human", False):
        raise HerdrTeamError(
            "author_mismatch",
            "the whiteboard page is for the operator; members draw with: herdr-synapse canvas draw",
            EXIT_REFUSED, {"action": "whiteboard open", "author": getattr(author, "name", None), "via": getattr(author, "via", None)},
        )
    writable = bool(getattr(author, "trusted_human", False))
    by = {"name": str(getattr(author, "name", "human")), "via": getattr(author, "via", None), "verified": bool(getattr(author, "verified", False))}
    current = status(layout)
    if current is not None and writable and not current.get("writable"):
        stop(layout, "stopped")
        current = None
    started = current is None
    if current is None:
        current = _spawn_server(layout, env, writable, by)
    port = int(current["port"])
    url = mint_ticket(layout, writable, by["name"], port=port)
    if open_browser:
        try:
            OPEN_BROWSER(url)
        except Exception:  # noqa: BLE001 - no browser is a normal case; the URL is printed anyway
            pass
    return {"url": url, "port": port, "pid": int(current["pid"]), "writable": bool(writable and current.get("writable")), "started": started}


def stop(layout: Any, reason: str = "stopped") -> bool:
    """Stop the recorded server after checking its start time; True when one was stopped."""
    session = layout.session
    rec = _read_state(session)
    if rec is None:
        return False
    pid = int(rec["pid"])
    if pid == os.getpid():
        key = os.fspath(session.root)
        local = _LOCAL.get(key)
        if local is not None:
            local.request_stop(reason)
            deadline = time.monotonic() + STOP_WAIT_S
            while _LOCAL.get(key) is local and time.monotonic() < deadline:
                time.sleep(0.02)
            return True
        _remove_state(session, pid)
        return False
    if not _alive(rec):
        _remove_state(session, pid)
        return False
    # Tell the server why before signalling it, so its SSE ``bye`` names the reason.
    try:
        store.write_json(state_path(session), dict(rec, stop_reason=str(reason)), fsync=False)
    except (HerdrTeamError, OSError):
        pass
    try:
        KILL(pid, signal.SIGTERM)
    except ProcessLookupError:
        _remove_state(session, pid)
        return True
    except OSError:
        return False
    deadline = time.monotonic() + STOP_WAIT_S
    while time.monotonic() < deadline:
        if not _alive(rec):
            break
        time.sleep(0.05)
    _remove_state(session, pid)
    return True


# --------------------------------------------------------------------------
# tickets


def _prune_tickets(directory: Path, now: float) -> None:
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.endswith(".json"):
            continue
        path = directory / name
        try:
            doc = store.read_json(path, default=None)
        except (HerdrTeamError, OSError):
            doc = None
        expires = _parse_iso(doc.get("expires_at")) if isinstance(doc, dict) else None
        if expires is None or expires <= now:
            try:
                os.unlink(path)
            except OSError:
                pass


def mint_ticket(layout: Any, writable: bool, by: str, port: Optional[int] = None) -> str:
    """A one-use opening URL ``http://127.0.0.1:<port>/?ticket=...`` valid for ``TICKET_TTL_S``."""
    if port is None:
        current = status(layout)
        if current is None:
            raise HerdrTeamError("whiteboard_not_running", "the whiteboard page server is not running; start it with: herdr-synapse whiteboard open",
                                 EXIT_REFUSED)
        port = int(current["port"])
    now = time.time()
    directory = tickets_dir(layout.session)
    ensure_dir(directory)
    _prune_tickets(directory, now)
    ticket = secrets.token_urlsafe(32)
    store.write_json(directory / (_digest(ticket) + ".json"),
                     {"expires_at": _iso(now + TICKET_TTL_S), "writable": bool(writable), "by": str(by)}, fsync=False)
    return "{}?ticket={}".format(page_url(int(port)), ticket)


def _consume_ticket(session: SessionPaths, ticket: str) -> Optional[Dict[str, Any]]:
    """Look a ticket up by its hash and delete it (one use); its grant, or None when unknown, used or expired."""
    if not TICKET_RE.match(ticket or ""):
        return None
    path = tickets_dir(session) / (_digest(ticket) + ".json")
    try:
        check_not_symlink(path)
        doc = store.read_json(path, default=None)
    except (HerdrTeamError, OSError):
        return None
    try:
        # Whoever unlinks first owns the ticket; a second request with it loses here.
        os.unlink(path)
    except OSError:
        return None
    if not isinstance(doc, dict):
        return None
    expires = _parse_iso(doc.get("expires_at"))
    if expires is None or expires <= time.time():
        return None
    return {"writable": doc.get("writable") is True, "by": str(doc.get("by") or "human")}


# --------------------------------------------------------------------------
# the server


class WhiteboardServer(ThreadingHTTPServer):
    """The HTTP server plus its in-memory page sessions, stream count and stop state."""

    daemon_threads = True
    request_queue_size = 32

    def __init__(self, layout: Any, env: Mapping[str, str], port: int, static_dir: Path, writable: bool, api: Any) -> None:
        super().__init__((BIND_HOST, int(port)), _Handler)
        self.layout = layout
        self.env = dict(env)
        self.static_dir = Path(static_dir)
        #: Whether the starter was the operator in person; a page session writes only when this and its ticket say so.
        self.writable = bool(writable)
        self.api = api
        self.verbose = False
        self.started_by: Optional[Dict[str, Any]] = None
        self.stop_event = threading.Event()
        self.stop_reason: Optional[str] = None
        #: Set by the SIGTERM handler, which must not take locks; ``exit_reason`` turns it into a stop.
        self.pending_signal: Optional[str] = None
        self._lock = threading.Lock()
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._streams = 0
        self._last_activity = time.monotonic()
        self._last_activity_wall = time.time()

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def allowed_hosts(self) -> Tuple[str, str]:
        return ("127.0.0.1:{}".format(self.port), "localhost:{}".format(self.port))

    def allowed_origins(self) -> Tuple[str, str]:
        return ("http://127.0.0.1:{}".format(self.port), "http://localhost:{}".format(self.port))

    def log(self, message: str) -> None:
        if self.verbose:
            LOG("{} whiteboard: {}".format(store.now_iso(), message))

    # -- sessions ------------------------------------------------------------

    def new_session(self, writable: bool, by: str) -> Tuple[str, Dict[str, Any]]:
        """A fresh page session; ``(cookie value, session)``. Only the cookie's hash is kept."""
        token = secrets.token_urlsafe(32)
        session = {"writable": bool(writable) and self.writable, "csrf": secrets.token_urlsafe(32), "by": by, "created": time.time()}
        with self._lock:
            self._sessions[_digest(token)] = session
            while len(self._sessions) > MAX_SESSIONS:
                del self._sessions[next(iter(self._sessions))]
        return token, session

    def session_for(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token or not TICKET_RE.match(token):
            return None
        with self._lock:
            return self._sessions.get(_digest(token))

    # -- activity and streams --------------------------------------------------

    def touch(self) -> None:
        with self._lock:
            self._last_activity = time.monotonic()
            self._last_activity_wall = time.time()

    def open_stream(self) -> bool:
        with self._lock:
            if self._streams >= MAX_STREAMS:
                return False
            self._streams += 1
            self._last_activity = time.monotonic()
            self._last_activity_wall = time.time()
            return True

    def close_stream(self) -> None:
        with self._lock:
            self._streams = max(0, self._streams - 1)
            self._last_activity = time.monotonic()
            self._last_activity_wall = time.time()

    def last_page_at(self) -> str:
        """When a page last talked to the server (wall clock), for ``doctor``'s "no page for a day"."""
        with self._lock:
            return _iso(time.time() if self._streams > 0 else self._last_activity_wall)

    def active_streams(self) -> int:
        with self._lock:
            return self._streams

    def idle_seconds(self, now: Optional[float] = None) -> float:
        """Seconds with no request and no open stream (0 while a page is connected)."""
        with self._lock:
            if self._streams > 0:
                return 0.0
            current = time.monotonic() if now is None else now
            return max(0.0, current - self._last_activity)

    def request_stop(self, reason: str) -> None:
        with self._lock:
            if self.stop_reason is None:
                self.stop_reason = str(reason)
        self.stop_event.set()

    def exit_reason(self, idle_timeout_s: float = IDLE_TIMEOUT_S, now: Optional[float] = None) -> Optional[str]:
        """Why the server should exit now (``stopped``/..., ``disabled``, ``idle``), or None."""
        if self.pending_signal is not None and not self.stop_event.is_set():
            self.request_stop(self.pending_signal)
        if self.stop_event.is_set():
            return self.stop_reason or "stopped"
        try:
            if not features.layer_enabled(self.layout.session):
                return "disabled"
        except (HerdrTeamError, OSError):
            return "disabled"
        if self.idle_seconds(now) >= float(idle_timeout_s):
            return "idle"
        return None


def make_server(layout: Any, env: Mapping[str, str], port: int = 0, static_dir: Optional[Path] = None, writable: bool = True,
                api: Any = None) -> WhiteboardServer:
    """A bound, not yet serving ``WhiteboardServer`` (``.port``, ``.serve_forever()``, ``.shutdown()``, ``.server_close()``)."""
    return WhiteboardServer(layout, env, port, static_dir if static_dir is not None else web_dist(), writable, api)


def _default_api(layout: Any, env: Mapping[str, str]) -> Any:
    from herdr_team.api import HerdrApi

    return HerdrApi(layout.socket, env=env)


def _started_by(env: Mapping[str, str]) -> Dict[str, Any]:
    try:
        raw = json.loads(env.get(ENV_STARTED_BY) or "null")
    except ValueError:
        raw = None
    if not isinstance(raw, dict):
        return {"name": "human", "via": None, "verified": False}
    return {"name": str(raw.get("name") or "human"), "via": raw.get("via") if isinstance(raw.get("via"), str) else None,
            "verified": raw.get("verified") is True}


def _write_state(server: WhiteboardServer, start_time: str, started_at: str) -> None:
    session = server.layout.session
    ensure_dir(session.root)
    store.write_json(state_path(session), {
        "v": SCHEMA, "pid": os.getpid(), "port": server.port, "start_time": start_time, "started_at": started_at,
        "started_by": server.started_by, "writable": server.writable, "version": VERSION, "beat_at": store.now_iso(),
        "page_at": server.last_page_at(), "streams": server.active_streams(),
    }, fsync=False)


def _beat(server: WhiteboardServer) -> bool:
    """Refresh ``beat_at``; False when the record no longer names this process (someone replaced or removed it)."""
    session = server.layout.session
    rec = _read_state(session)
    if rec is None or _int(rec.get("pid")) != os.getpid():
        return False
    rec["beat_at"] = store.now_iso()
    rec["page_at"] = server.last_page_at()
    rec["streams"] = server.active_streams()
    try:
        store.write_json(state_path(session), rec, fsync=False)
    except (HerdrTeamError, OSError):
        return False
    return True


def _stop_reason_on_disk(session: SessionPaths) -> Optional[str]:
    rec = _read_state(session)
    if rec is None or _int(rec.get("pid")) != os.getpid():
        return None
    reason = rec.get("stop_reason")
    return reason if isinstance(reason, str) and reason else None


def serve(layout: Any, env: Mapping[str, str], port: int = 0, idle_timeout_s: float = IDLE_TIMEOUT_S, api: Any = None) -> int:
    """Run the server in the foreground (the ``whiteboard-serve`` command) until stopped, idle, or disabled; the exit code."""
    session = layout.session
    if not features.layer_enabled(session):
        LOG("whiteboard: the whiteboard layer is off for this session; not starting")
        return 1
    existing = status(layout)
    if existing is not None and _int(existing.get("pid")) != os.getpid():
        LOG("whiteboard: already running (pid {}, port {})".format(existing.get("pid"), existing.get("port")))
        return 1
    try:
        server = make_server(layout, env, port=port, writable=env.get(ENV_WRITABLE) == "1",
                             api=api if api is not None else _default_api(layout, env))
    except OSError as err:
        LOG("whiteboard: cannot listen on {}:{}: {}".format(BIND_HOST, port, err))
        return 1
    server.verbose = True
    server.started_by = _started_by(env)
    key = os.fspath(session.root)
    _LOCAL[key] = server
    previous: Dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        def on_signal(signum: int, frame: Any) -> None:
            # No locks in a signal handler: the main thread may hold one right now.
            server.pending_signal = _stop_reason_on_disk(session) or "stopped"

        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, on_signal)
        previous[signal.SIGHUP] = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25}, name="whiteboard-http", daemon=True)
    thread_started = False
    try:
        _write_state(server, _process_start_time(os.getpid()) or "", store.now_iso())
        thread.start()
        thread_started = True
        server.log("listening on {} (writable={}, started by {})".format(page_url(server.port), server.writable, server.started_by.get("name")))
        last_beat = time.monotonic()
        while True:
            reason = server.exit_reason(idle_timeout_s)
            if reason is not None:
                server.request_stop(reason)
                break
            server.stop_event.wait(LOOP_S)
            if time.monotonic() - last_beat >= BEAT_S:
                last_beat = time.monotonic()
                if not _beat(server):
                    server.request_stop("stopped")
        deadline = time.monotonic() + DRAIN_S
        while server.active_streams() > 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        server.log("exiting: {}".format(server.stop_reason))
    finally:
        if thread_started:
            server.shutdown()
        server.server_close()
        _remove_state(session, os.getpid())
        if _LOCAL.get(key) is server:
            del _LOCAL[key]
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (TypeError, ValueError):
                pass
    return 0


# --------------------------------------------------------------------------
# pure page helpers (tested directly)


def safe_static_path(url_path: str) -> Optional[str]:
    """A request path -> a relative file path under ``web/dist``, or None (traversal, hidden files, odd bytes)."""
    try:
        decoded = unquote(url_path, errors="strict")
    except UnicodeDecodeError:
        return None
    if "\x00" in decoded or "\\" in decoded or not decoded.startswith("/"):
        return None
    rel = decoded[1:] or "index.html"
    parts = rel.split("/")
    for part in parts:
        if not part or part in (".", "..") or part.startswith(".") or any(ord(ch) < 32 for ch in part):
            return None
    return "/".join(parts)


def resolve_static(root: Path, rel: str) -> Optional[Path]:
    """The regular file ``root/rel``, refusing symlinks on the way and anything outside ``root``."""
    base = Path(os.path.realpath(os.fspath(root)))
    current = base
    for part in rel.split("/"):
        current = current / part
        try:
            st = os.lstat(current)
        except OSError:
            return None
        if stat.S_ISLNK(st.st_mode):
            return None
    if not stat.S_ISREG(os.lstat(current).st_mode):
        return None
    real = os.path.realpath(os.fspath(current))
    if os.path.commonpath([real, os.fspath(base)]) != os.fspath(base):
        return None
    return current


def mime_type(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".vl.json"):
        return "application/json"
    return MIME_TYPES.get(os.path.splitext(lower)[1], "application/octet-stream")


def viz_document(source: Mapping[str, Any], port: int) -> str:
    """The sealed ``viz`` frame's document (contract 13.6): runtime, requested libraries, then the agent's HTML."""
    base = "http://127.0.0.1:{}/viz-lib/".format(int(port))
    libs = [lib for lib in (source.get("libs") or []) if isinstance(lib, str) and lib in VIZ_LIB_FILES]
    # The runtime marks a lone top-level canvas or svg data-synapse-fit: drawn at a fixed size,
    # it is scaled to the frame with its aspect kept (a stylesheet !important outranks the inline
    # sizes p5 and three.js set).
    parts = ['<!doctype html><meta charset="utf-8">',
             "<style>html,body{margin:0;height:100%;overflow:hidden;background:#fff}"
             "[data-synapse-fit]{display:block!important;width:100vw!important;height:100vh!important;"
             "object-fit:contain!important}</style>"]
    if "three" in libs:
        imports = {"imports": {"three": base + VIZ_LIB_FILES["three"]}}
        parts.append('<script type="importmap">{}</script>'.format(json.dumps(imports, separators=(",", ":")).replace("</", "<\\/")))
    parts.append('<script src="{}{}"></script>'.format(base, VIZ_RUNTIME))
    for lib in ("d3", "p5"):
        if lib in libs:
            parts.append('<script src="{}{}"></script>'.format(base, VIZ_LIB_FILES[lib]))
    parts.append(str(source.get("html") or ""))
    return "\n".join(parts) + "\n"


def sse_frame(event: str, data: Any, event_id: Optional[int] = None) -> bytes:
    """One Server-Sent Events frame: ``event:``, optional ``id:``, one ``data:`` line of compact JSON."""
    lines = ["event: {}".format(event)]
    if event_id is not None:
        lines.append("id: {}".format(int(event_id)))
    lines.append("data: {}".format(json.dumps(data, ensure_ascii=False, separators=(",", ":"))))
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def http_status_for(code: str) -> int:
    return HTTP_STATUS.get(code, 500)


# --------------------------------------------------------------------------
# requests


class _Handler(BaseHTTPRequestHandler):
    server: WhiteboardServer
    server_version = "synapse-whiteboard"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    #: A client that stops talking mid-request is dropped after this long.
    timeout = 60

    # -- plumbing ------------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class's name
        # Never log a query string: the opening URL carries the ticket.
        self.server.log("{} {}".format(self.command, urlsplit(self.path).path) + (" " + " ".join(str(a) for a in args[1:2]) if len(args) > 1 else ""))

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        text = format % args if args else format
        if text.startswith("Request timed out"):
            return  # an idle keep-alive connection closing, not an error
        self.server.log("error: " + text)

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _method_not_allowed(self) -> None:
        self._responded = False
        self.close_connection = True
        self._send_error("method_not_allowed", "only GET and POST are served here")

    do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed  # noqa: N815

    def _base_headers(self, cache: str = "no-store") -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cache-Control", cache)

    def _send_bytes(self, status_code: int, body: bytes, content_type: str, csp: Optional[str] = API_CSP, frame_deny: bool = True,
                    cache: str = "no-store", extra: Optional[Dict[str, str]] = None) -> None:
        self._responded = True
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._base_headers(cache)
        if csp:
            self.send_header("Content-Security-Policy", csp)
        if frame_deny:
            self.send_header("X-Frame-Options", "DENY")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status_code: int, obj: Any) -> None:
        self._send_bytes(status_code, _json_bytes(obj), "application/json")

    def _send_error(self, code: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        body: Dict[str, Any] = {}
        for key, value in (details or {}).items():
            body[key] = value
        body["code"] = code
        body["message"] = message
        self._send_json(http_status_for(code), body)

    def _send_html(self, status_code: int, reason: str) -> None:
        text = NOT_SIGNED_IN_HTML.format(reason=reason)
        self._send_bytes(status_code, text.encode("utf-8"), "text/html; charset=utf-8", csp=PAGE_CSP)

    # -- the pipeline ----------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        self._responded = False
        server = self.server
        server.touch()
        try:
            split = urlsplit(self.path)
            path = split.path or "/"
            query = parse_qs(split.query, keep_blank_values=True)
            if not self._host_ok():
                self.close_connection = True
                return self._send_error("bad_host", "this page answers only as 127.0.0.1:{0} or localhost:{0}".format(server.port))
            if method == "GET" and path.startswith("/viz-lib/"):
                return self._viz_lib(path[len("/viz-lib/"):])
            if not self._origin_ok(method):
                self.close_connection = True
                return self._send_error("bad_origin", "requests from other pages are refused")
            if method == "GET" and path == "/favicon.ico":
                return self._favicon()
            if method == "GET" and path in ("/", "/index.html") and "ticket" in query:
                return self._exchange_ticket(query)
            session = server.session_for(self._cookie())
            if session is None:
                if method == "GET" and not path.startswith("/api/"):
                    return self._send_html(401, "This page needs a fresh link.")
                return self._send_error("not_signed_in", "open the whiteboard from Herdr: prefix+a or herdr-synapse whiteboard open")
            if method == "POST":
                if not hmac.compare_digest(str(self.headers.get(CSRF_HEADER) or ""), str(session["csrf"])):
                    self.close_connection = True
                    return self._send_error("bad_csrf", "the page's request token is missing or wrong; reload the page")
                if not session["writable"]:
                    self.close_connection = True
                    return self._send_error("read_only", "this page is read-only: it was not opened by the operator in person")
            self._route(method, path, query, session)
        except HerdrTeamError as err:
            if self._responded:
                self.close_connection = True
                return
            if method == "POST":
                # The body may be unread; never parse its bytes as the next request.
                self.close_connection = True
            if http_status_for(err.code) == 500:
                server.log("error {}: {}".format(err.code, err.message))
                self._send_error("internal", "the server could not do that; see whiteboard.log")
                return
            self._send_error(err.code, err.message, err.details)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception:  # noqa: BLE001 - a bug must not take the server down or leak a traceback to the page
            server.log("internal error on {} {}:\n{}".format(method, urlsplit(self.path).path, traceback.format_exc()))
            self.close_connection = True
            if not self._responded:
                try:
                    self._send_error("internal", "the server could not do that; see whiteboard.log")
                except OSError:
                    pass

    def _host_ok(self) -> bool:
        host = str(self.headers.get("Host") or "").strip().lower()
        return host in self.server.allowed_hosts()

    def _origin_ok(self, method: str) -> bool:
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.server.allowed_origins():
            return False
        if method == "POST" and origin is None:
            return False
        site = str(self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        return site not in ("cross-site", "same-site")

    def _cookie(self) -> Optional[str]:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME and value:
                return value.strip()
        return None

    def _exchange_ticket(self, query: Dict[str, List[str]]) -> None:
        grant = _consume_ticket(self.server.layout.session, (query.get("ticket") or [""])[0])
        if grant is None:
            return self._send_html(401, "That link was already used or has expired.")
        token, _session = self.server.new_session(grant["writable"], grant["by"])
        self._responded = True
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", "{}={}; HttpOnly; SameSite=Strict; Path=/".format(COOKIE_NAME, token))
        self.send_header("Content-Length", "0")
        self._base_headers()
        self.send_header("Content-Security-Policy", PAGE_CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()

    def _favicon(self) -> None:
        path = resolve_static(self.server.static_dir, "favicon.ico")
        if path is None:
            self._responded = True
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self._base_headers()
            self.end_headers()
            return
        self._static_file(path, "favicon.ico")

    def _route(self, method: str, path: str, query: Dict[str, List[str]], session: Dict[str, Any]) -> None:
        if path == "/api/session":
            self._need(method, "GET")
            return self._api_session(session)
        if path == "/api/teams":
            self._need(method, "GET")
            return self._send_json(200, {"teams": self._team_rows()})
        if path == "/api/activity":
            self._need(method, "GET")
            return self._api_activity(query)
        if path == "/api/stream":
            self._need(method, "GET")
            return self._api_stream(query, session)
        match = re.match(r"^/api/teams/([^/]+)/(.+)\Z", path)
        if match:
            return self._team_route(method, match.group(1), match.group(2), query, session)
        match = re.match(r"^/viz/([^/]+)/([^/]+)\Z", path)
        if match:
            self._need(method, "GET")
            return self._viz(match.group(1), match.group(2))
        if path.startswith("/api/"):
            raise HerdrTeamError("not_found", "no such API route", EXIT_REFUSED)
        self._need(method, "GET")
        return self._static(path)

    @staticmethod
    def _need(method: str, wanted: str) -> None:
        if method != wanted:
            raise HerdrTeamError("method_not_allowed", "use {} here".format(wanted), EXIT_REFUSED)

    # -- request bodies ------------------------------------------------------------

    def _read_body(self, limit: int = MAX_BODY_BYTES) -> bytes:
        if "chunked" in str(self.headers.get("Transfer-Encoding") or "").lower():
            self.close_connection = True
            raise HerdrTeamError("usage", "send a Content-Length, not a chunked body", EXIT_REFUSED)
        length = _int(self.headers.get("Content-Length"))
        if length is None or length < 0:
            self.close_connection = True
            raise HerdrTeamError("usage", "a Content-Length is required", EXIT_REFUSED)
        if length > min(int(limit), MAX_BODY_BYTES):
            self.close_connection = True
            raise HerdrTeamError("body_too_large", "the body is larger than {} bytes".format(min(int(limit), MAX_BODY_BYTES)), EXIT_REFUSED,
                                 {"max": min(int(limit), MAX_BODY_BYTES)})
        chunks: List[bytes] = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                self.close_connection = True
                raise HerdrTeamError("usage", "the body ended early", EXIT_REFUSED)
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_json(self) -> Any:
        raw = self._read_body()
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HerdrTeamError("usage", "the body is not JSON", EXIT_REFUSED)

    def _content_type(self) -> str:
        return str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()

    # -- teams -----------------------------------------------------------------------

    def _team(self, name: str, viz: bool = False) -> Tuple[TeamPaths, Dict[str, Any], Any]:
        session = self.server.layout.session
        if not TEAM_NAME_RE.match(name or ""):
            raise HerdrTeamError("team_not_found", "no such team", EXIT_REFUSED, {"team": name})
        team = session.team(name)
        doc = store.RosterStore(team).load()
        switch = features.require_viz(session, team, doc) if viz else features.require_on(session, team, doc)
        return team, doc, switch

    def _team_rows(self) -> List[Dict[str, Any]]:
        session = self.server.layout.session
        rows: List[Dict[str, Any]] = []
        for name in session.list_teams():
            team = session.team(name)
            try:
                doc = store.RosterStore(team).load()
                switch = features.team_switch(session, team, doc)
            except (HerdrTeamError, OSError, ValueError):
                continue
            if not switch.on:
                continue
            rows.append(team_row(team, doc, switch))
        return rows

    def _api_session(self, session: Dict[str, Any]) -> None:
        self._send_json(200, {
            "server_version": VERSION, "writable": bool(session["writable"]), "csrf": session["csrf"],
            "switches": features.status(self.server.layout.session), "teams": self._team_rows(),
            "limits": {"max_body_bytes": MAX_BODY_BYTES},
        })

    def _api_activity(self, query: Dict[str, List[str]]) -> None:
        session = self.server.layout.session
        features.require_layer(session)
        team_name = (query.get("team") or [""])[0] or None
        if team_name is not None:
            try:
                self._team(team_name)
            except HerdrTeamError:
                team_name = None
        cards = _activity().cards(self.server.layout, api=self.server.api, team=team_name)
        self._send_json(200, {"cards": cards})

    def _team_route(self, method: str, name: str, rest: str, query: Dict[str, List[str]], session: Dict[str, Any]) -> None:
        team, doc, switch = self._team(name)
        layout = self.server.layout
        cv = _canvas()
        if rest == "scene":
            self._need(method, "GET")
            return self._send_json(200, cv.load_scene(team))
        if rest == "changes":
            self._need(method, "GET")
            since = _int((query.get("since") or ["0"])[0])
            if since is None or since < 0:
                raise HerdrTeamError("usage", "since must be a version number", EXIT_REFUSED)
            return self._send_json(200, cv.changes_since(team, since))
        if rest == "views":
            self._need(method, "GET")
            return self._send_json(200, _views().team_views(layout, team.name, api=self.server.api))
        if rest == "text":
            self._need(method, "GET")
            ids = [part for part in (query.get("ids") or [""])[0].split(",") if part]
            if not ids or len(ids) > MAX_SEND_IDS or any(not CANONICAL_ID_RE.match(i) for i in ids):
                raise HerdrTeamError("usage", "ids must be a comma-separated list of element ids", EXIT_REFUSED)
            return self._send_json(200, {"text": cv.text_form(cv.load_scene(team), ids, reader="human")})
        if rest == "ops":
            self._need(method, "POST")
            ops, atomic = cv.parse_batch(self._read_json())
            result = cv.apply_ops(layout, team, ops, cv.page_author(bool(session["writable"])), atomic=atomic, doc=doc)
            return self._send_json(200, result)
        if rest == "send":
            self._need(method, "POST")
            body = self._read_json()
            to, ids, note = _send_request(body)
            return self._send_json(200, cv.send_to_member(layout, team, ids, to, note, cv.page_author(bool(session["writable"]))))
        if rest == "uploads":
            self._need(method, "POST")
            kind = {"image/png": "png", "image/jpeg": "jpeg"}.get(self._content_type())
            if kind is None:
                self.close_connection = True
                raise HerdrTeamError("unsupported_media_type", "upload a PNG or JPEG image", EXIT_REFUSED)
            stored = cv.store_asset(team, self._read_body(), kind)
            return self._send_json(200, {"asset": stored.get("asset"), "mime": stored.get("mime"), "px_w": stored.get("px_w"), "px_h": stored.get("px_h")})
        match = re.match(r"^stills/([^/]+)\Z", rest)
        if match:
            self._need(method, "POST")
            element_id = match.group(1)
            version = _int((query.get("v") or [""])[0])
            if not ELEMENT_ID_RE.match(element_id) or version is None or version < 0:
                raise HerdrTeamError("usage", "POST /stills/<E-n>?v=<updated_seq> with a PNG body", EXIT_REFUSED)
            cv.store_still(team, element_id, version, self._read_body(getattr(cv, "MAX_STILL_BYTES", MAX_BODY_BYTES)))
            return self._send_json(200, {"ok": True})
        match = re.match(r"^exports/([^/]+)\Z", rest)
        if match:
            self._need(method, "POST")
            request_id = match.group(1)
            if not REQUEST_ID_RE.match(request_id):
                raise HerdrTeamError("usage", "not an export request id", EXIT_REFUSED)
            cv.complete_export(team, request_id, self._read_body(getattr(cv, "MAX_EXPORT_BYTES", MAX_BODY_BYTES)))
            return self._send_json(200, {"ok": True})
        match = re.match(r"^assets/([^/]+)\Z", rest)
        if match:
            self._need(method, "GET")
            name = match.group(1)
            if not ASSET_NAME_RE.match(name):
                raise HerdrTeamError("not_found", "no such asset", EXIT_REFUSED)
            data = store.read_bytes(cv.asset_path(team, name))
            if data is None:
                raise HerdrTeamError("not_found", "no such asset", EXIT_REFUSED)
            return self._send_bytes(200, data, mime_type(name), csp=ASSET_CSP)
        if rest == "artifact":
            self._need(method, "GET")
            rel = (query.get("path") or [""])[0]
            if not rel:
                raise HerdrTeamError("usage", "artifact?path=<file under the team's artifacts>", EXIT_REFUSED)
            path = cv.artifact_file(layout, team, rel, doc=doc)
            data = store.read_bytes(path)
            if data is None:
                raise HerdrTeamError("not_found", "no such artifact", EXIT_REFUSED)
            return self._send_bytes(200, data, mime_type(Path(path).name), csp=ASSET_CSP)
        raise HerdrTeamError("not_found", "no such team route", EXIT_REFUSED)

    # -- viz ----------------------------------------------------------------------------

    def _viz(self, name: str, element_id: str) -> None:
        team, _doc, _switch = self._team(name, viz=True)
        if not ELEMENT_ID_RE.match(element_id):
            raise HerdrTeamError("not_found", "no such live visual", EXIT_REFUSED)
        source = _canvas().viz_source(team, element_id)
        body = viz_document(source, self.server.port).encode("utf-8")
        self._send_bytes(200, body, "text/html; charset=utf-8", csp=VIZ_CSP.format(port=self.server.port), frame_deny=False)

    def _viz_lib(self, name: str) -> None:
        if not VIZ_LIB_NAME_RE.match(name or "") or ".." in name:
            return self._send_error("not_found", "no such library file")
        path = resolve_static(self.server.static_dir, "viz-lib/" + name)
        if path is None:
            return self._send_error("not_found", "no such library file")
        self._static_file(path, name, public=True)

    # -- static files -----------------------------------------------------------------

    def _static(self, url_path: str) -> None:
        root = self.server.static_dir
        if resolve_static(root, "index.html") is None:
            raise HerdrTeamError("page_not_built", "the page is not built: web/dist/index.html is missing (npm run build in web/)", EXIT_REFUSED)
        rel = safe_static_path(url_path)
        path = resolve_static(root, rel) if rel is not None else None
        if path is None or rel is None:
            raise HerdrTeamError("not_found", "no such file", EXIT_REFUSED)
        self._static_file(path, rel)

    def _static_file(self, path: Path, rel: str, public: bool = False) -> None:
        immutable = rel.startswith(IMMUTABLE_PREFIXES)
        try:
            fd = os.open(os.fspath(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            raise HerdrTeamError("not_found", "no such file", EXIT_REFUSED)
        with os.fdopen(fd, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            self._responded = True
            self.send_response(200)
            self.send_header("Content-Type", mime_type(rel))
            self.send_header("Content-Length", str(size))
            self._base_headers("public, max-age=31536000, immutable" if immutable else "no-store")
            if public:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cross-Origin-Resource-Policy", "cross-origin")
            else:
                self.send_header("Content-Security-Policy", PAGE_CSP)
                self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # -- the event stream -------------------------------------------------------------

    def _api_stream(self, query: Dict[str, List[str]], session: Dict[str, Any]) -> None:
        server = self.server
        team_name = (query.get("team") or [""])[0] or None
        team: Optional[TeamPaths] = None
        switch = None
        if team_name is not None:
            team, _doc, switch = self._team(team_name)
        since = _int(self.headers.get("Last-Event-ID"))
        if since is None:
            since = _int((query.get("since") or [""])[0])
        if since is not None and since < 0:
            since = None
        if not server.open_stream():
            raise HerdrTeamError("too_many_streams", "too many pages are open; close one", EXIT_REFUSED, {"max": MAX_STREAMS})
        try:
            self._responded = True
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self._base_headers()
            self.send_header("Content-Security-Policy", API_CSP)
            self.end_headers()
            _Stream(self, team, switch, since, bool(session["writable"])).run()
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
            pass
        finally:
            server.close_stream()


def team_row(team: TeamPaths, doc: Dict[str, Any], switch: Any) -> Dict[str, Any]:
    """``TEAM_ROW`` (contract 13.4): the team, its switches, its canvas version and members with their canvas colours."""
    cv = _canvas()
    version = 0
    authors: Dict[str, Any] = {}
    try:
        version = int(cv.current_version(team))
        scene = cv.load_scene(team)
        authors = scene.get("authors") if isinstance(scene.get("authors"), dict) else {}
    except Exception:  # noqa: BLE001 - a canvas that cannot be read still lists its team
        pass
    members: List[Dict[str, Any]] = []
    for member in doc.get("members") or []:
        if not isinstance(member, dict) or member.get("kind") == "human" or not isinstance(member.get("name"), str):
            continue
        name = member["name"]
        author = authors.get(name) if isinstance(authors.get(name), dict) else {}
        members.append({"name": name, "role": member.get("role"), "kind": member.get("kind"), "status": member.get("status"),
                        "manager": bool(member.get("manager")), "color": author.get("color")})
    return {"name": team.name, "on": bool(switch.on), "viz": bool(switch.viz), "version": version, "members": members}


def _send_request(body: Any) -> Tuple[str, List[str], Optional[str]]:
    """``{"to", "elements", "note"}`` of ``POST /send``, checked for shape."""
    if not isinstance(body, dict):
        raise HerdrTeamError("usage", "send {\"to\": name, \"elements\": [ids], \"note\": text}", EXIT_REFUSED)
    to = body.get("to")
    ids = body.get("elements")
    note = body.get("note")
    if not isinstance(to, str) or not to.strip():
        raise HerdrTeamError("usage", "to must name a member", EXIT_REFUSED)
    if not isinstance(ids, list) or not ids or len(ids) > MAX_SEND_IDS or any(not isinstance(i, str) or not CANONICAL_ID_RE.match(i) for i in ids):
        raise HerdrTeamError("usage", "elements must be a list of element ids", EXIT_REFUSED)
    if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE_CHARS):
        raise HerdrTeamError("usage", "note must be text of at most {} characters".format(MAX_NOTE_CHARS), EXIT_REFUSED)
    return to.strip(), list(ids), (note or None)


class _Stream:
    """One open page's event stream: polls file signatures every ``POLL_S`` and writes SSE frames (contract 13.5)."""

    def __init__(self, handler: _Handler, team: Optional[TeamPaths], switch: Any, since: Optional[int], writable: bool) -> None:
        self.handler = handler
        self.server = handler.server
        self.layout = self.server.layout
        self.session = self.layout.session
        self.team = team
        self.switch = switch
        self.since = since
        self.writable = writable
        self.last = 0
        self.last_write = time.monotonic()
        self.sent_exports: Dict[str, float] = {}
        self.cards_key: Optional[str] = None
        self.failures: Dict[str, str] = {}

    # -- output ----------------------------------------------------------------------

    def send(self, event: str, data: Any, event_id: Optional[int] = None) -> None:
        self.handler.wfile.write(sse_frame(event, data, event_id))
        self.handler.wfile.flush()
        self.last_write = time.monotonic()

    def keepalive(self) -> None:
        self.handler.wfile.write(b": keepalive\n\n")
        self.handler.wfile.flush()
        self.last_write = time.monotonic()

    def bye(self, reason: str) -> None:
        try:
            self.send("bye", {"reason": reason})
        except OSError:
            pass

    def client_gone(self) -> bool:
        """True when the page closed the connection (readable with nothing to read)."""
        conn = self.handler.connection
        try:
            readable, _, _ = select.select([conn], [], [], 0)
            if not readable:
                return False
            return conn.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _guard(self, part: str, fn: Callable[[], None]) -> None:
        """Run one poll step; a failing step is logged once and retried next poll, never ends the stream."""
        try:
            fn()
            self.failures.pop(part, None)
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as err:  # noqa: BLE001 - see the docstring
            text = "{}: {}".format(type(err).__name__, err)
            if self.failures.get(part) != text:
                self.failures[part] = text
                self.server.log("stream {} failed: {}".format(part, text))

    # -- content ---------------------------------------------------------------------

    def version(self) -> int:
        if self.team is None:
            return 0
        try:
            return int(_canvas().current_version(self.team))
        except Exception:  # noqa: BLE001 - the hello still goes out
            return 0

    def send_scene(self) -> None:
        assert self.team is not None
        scene = _canvas().load_scene(self.team)
        version = _int(scene.get("version"), 0) or 0
        self.send("scene", {"team": self.team.name, "scene": scene}, version)
        self.last = version

    def send_changes(self) -> None:
        assert self.team is not None
        cv = _canvas()
        for _ in range(100):
            result = cv.changes_since(self.team, self.last)
            if result.get("reset"):
                self.send_scene()
                return
            events = [e for e in result.get("events") or [] if isinstance(e, dict)]
            complete = bool(result.get("complete", True))
            if events:
                last_seq = _int(events[-1].get("seq"), self.last) or self.last
                version = _int(result.get("version"), last_seq) if complete else last_seq
                self.send("ops", {"team": self.team.name, "version": version, "events": events}, version)
                self.last = int(version or last_seq)
            else:
                self.last = max(self.last, _int(result.get("version"), self.last) or 0)
            if complete or not events:
                return

    def send_exports(self) -> None:
        assert self.team is not None
        now = time.monotonic()
        for request in _canvas().pending_exports(self.team):
            request_id = request.get("id") if isinstance(request, dict) else None
            if not isinstance(request_id, str) or request_id in self.sent_exports:
                continue
            self.sent_exports[request_id] = now
            self.send("export_request", {"team": self.team.name, "request_id": request_id, "region": request.get("region"),
                                         "marks": bool(request.get("marks", True)), "grid": bool(request.get("grid", False))})
        for request_id, at in list(self.sent_exports.items()):
            if now - at > 120.0:
                del self.sent_exports[request_id]

    def send_views(self) -> None:
        assert self.team is not None
        self.send("views", {"team": self.team.name, "views": _views().team_views(self.layout, self.team.name, api=self.server.api)})

    def send_activity(self) -> None:
        cards = _activity().cards(self.layout, api=self.server.api, team=self.team.name if self.team is not None else None)
        key = json.dumps(cards, sort_keys=True, default=str)
        if key != self.cards_key:
            self.cards_key = key
            self.send("activity", {"cards": cards})

    def view_sources(self) -> Tuple[Any, ...]:
        assert self.team is not None
        team = self.team
        return _stat_sig(team.board_jsonl, team.board_seq, team.team_json, team.root / "work.jsonl", team.root / "facts.jsonl",
                         self.session.who_json, self.session.links_json)

    # -- the loop --------------------------------------------------------------------

    def run(self) -> None:
        server = self.server
        version = self.version()
        # No ``id:`` on hello: it would move the browser's Last-Event-ID past events this page has not had yet.
        self.send("hello", {"team": self.team.name if self.team is not None else None, "version": version, "writable": self.writable,
                            "switch": self.switch.to_json() if self.switch is not None else None, "server_version": VERSION})
        events_path = features.whiteboard_dir(self.team) / "events.jsonl" if self.team is not None else None
        switch_paths = [features.features_path(self.session)] + ([self.team.team_json] if self.team is not None else [])
        switch_sig = _stat_sig(*switch_paths)
        events_sig: Tuple[Any, ...] = ()
        views_sig: Tuple[Any, ...] = ()
        if self.team is not None:
            events_sig = _stat_sig(events_path)  # type: ignore[arg-type]
            views_sig = self.view_sources()
            if self.since is None:
                self._guard("scene", self.send_scene)
            else:
                self.last = int(self.since)
                self._guard("changes", self.send_changes)
        last_views = last_activity = time.monotonic()
        while True:
            if server.stop_event.wait(POLL_S):
                self.bye(server.stop_reason or "stopped")
                return
            if self.client_gone():
                return
            server.touch()
            now = time.monotonic()
            sig = _stat_sig(*switch_paths)
            if sig != switch_sig:
                switch_sig = sig
                reason = self.switch_changed()
                if reason is not None:
                    self.bye(reason)
                    return
            if self.team is not None:
                sig = _stat_sig(events_path)  # type: ignore[arg-type]
                if sig != events_sig:
                    events_sig = sig
                    self._guard("changes", self.send_changes)
                if self.writable:
                    self._guard("exports", self.send_exports)
                if now - last_views >= VIEWS_MIN_S:
                    sig = self.view_sources()
                    if sig != views_sig:
                        views_sig = sig
                        last_views = now
                        self._guard("views", self.send_views)
            if now - last_activity >= ACTIVITY_MIN_S:
                last_activity = now
                self._guard("activity", self.send_activity)
            if time.monotonic() - self.last_write >= KEEPALIVE_S:
                self.keepalive()

    def switch_changed(self) -> Optional[str]:
        """Push ``state``; the ``bye`` reason when this stream must end (layer or team off)."""
        try:
            layer = features.layer_enabled(self.session)
            self.send("state", {"switches": features.status(self.session)})
        except (HerdrTeamError, OSError):
            layer = False
        if not layer:
            return "disabled"
        if self.team is not None:
            try:
                self.switch = features.team_switch(self.session, self.team)
            except (HerdrTeamError, OSError, ValueError):
                return "team_off"
            if not self.switch.on:
                return "team_off"
        return None
