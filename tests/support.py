"""Shared test fixtures: ``TempState``, ``FakeHerdrServer``, ``FakeApi``.

Run the suite from the plugin root::

    python3 -m unittest discover -s tests -v

``TempState`` builds an isolated state root with one session dir and one
team dir, and an ``env`` mapping that points every resolver at it. Never
uses the real HOME. Unix sockets need short paths (104 bytes on macOS), so
the root lives under ``tempfile.mkdtemp()`` rather than a deep scratch dir.

``FakeHerdrServer`` is a threaded NDJSON Unix-socket server with a
programmable response table keyed by method, a scripted
``events.subscribe`` stream, and a request log. Framing follows the real
server: one request line within 5 s under 1 MiB, ``subscription_started``
ack, then one event line per tick.

``FakeApi`` is the in-process stand-in with ``HerdrApi``'s surface
(``request``, ``request_raw``, ``ping``, ``subscribe``, ``run``,
``run_json``) for subprocess-free tests.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Union

from herdr_team import paths
from herdr_team.api import RunResult, parse_cli_json
from herdr_team.errors import HerdrTeamError, exit_code_for

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

FAKE_TEAM = "alpha"
FAKE_SLUG = "default"
def identity_tokens(team: Optional[str], role: Optional[str], slot: Optional[int] = None) -> Dict[str, Any]:
    """The roster-source token patch: the two identity keys plus every colour slot.

    ``slot=None`` clears every slot, which is what a stamp looks like before the daemon has
    assigned the team a colour. ``team=None`` is the full clear.
    """
    from herdr_team import roster as _roster

    tokens: Dict[str, Any] = {"team": team, "team_role": role}
    tokens.update(_roster.color_slot_tokens(team, slot))
    return tokens


FAKE_MEMBERS: List[Dict[str, Any]] = [
    {
        "name": "alpha-reviewer", "role": "reviewer", "kind": "codex", "terminal_id": "term_r1",
        "pane_id": "w2:p1", "workspace_id": "w2", "tab_id": "w2:t1", "label": "team:alpha/reviewer",
        "cwd": "/tmp/work", "managed": False, "session": None, "status": "active", "generation": 1,
        "delivery": "nudge", "joined_at": "2026-09-04T10:00:00Z", "last_seen_at": None,
        "briefed_at": None, "briefing_seq": None, "charter_seq_acked": None, "brief": None,
    },
    {
        "name": "alpha-worker", "role": "worker", "kind": "claude", "terminal_id": "term_w1",
        "pane_id": "w2:p2", "workspace_id": "w2", "tab_id": "w2:t1", "label": "team:alpha/worker",
        "cwd": "/tmp/work", "managed": False, "session": None, "status": "active", "generation": 1,
        "delivery": "nudge", "joined_at": "2026-09-04T10:00:00Z", "last_seen_at": None,
        "briefed_at": None, "briefing_seq": None, "charter_seq_acked": None, "brief": None,
    },
    {"name": "human", "role": "operator", "kind": "human", "terminal_id": None, "status": "active"},
]


def fake_agent(pane_id: str, terminal_id: str, kind: Optional[str], name: Optional[str], status: str = "idle", **extra: Any) -> Dict[str, Any]:
    """An ``AgentInfo`` object as ``agent.get``/``agent.list`` return it (schema fixture, protocol 20).

    Every optional field the server emits is present with its zero value so
    tests see the real width of the object; ``extra`` overrides any of them.
    """
    agent = {
        "terminal_id": terminal_id, "agent": kind, "display_agent": kind, "name": name, "agent_status": status,
        "workspace_id": pane_id.split(":")[0], "tab_id": pane_id.split(":")[0] + ":t1", "pane_id": pane_id,
        "focused": False, "revision": 1, "state_change_seq": 1, "launch_pending": False,
        "interactive_ready": False, "screen_detection_skipped": False, "tokens": {}, "state_labels": {},
        "title": None, "terminal_title": None, "terminal_title_stripped": None, "cwd": "/tmp/work",
        "foreground_cwd": None, "agent_session": None,
    }
    agent.update(extra)
    return agent


def fake_pane(pane_id: str, terminal_id: str, kind: Optional[str] = None, label: Optional[str] = None, status: str = "unknown", **extra: Any) -> Dict[str, Any]:
    """A ``PaneInfo`` object as ``pane.get``/``pane.list``/``pane.rename`` return it.

    Differs from ``AgentInfo``: ``label`` and ``scroll`` exist, ``name`` /
    ``launch_pending`` / ``interactive_ready`` / ``state_change_seq`` do not.
    """
    pane = {
        "pane_id": pane_id, "terminal_id": terminal_id, "workspace_id": pane_id.split(":")[0], "tab_id": pane_id.split(":")[0] + ":t1",
        "focused": False, "agent": kind, "display_agent": kind, "agent_status": status, "revision": 1, "label": label,
        "tokens": {}, "state_labels": {}, "title": None, "terminal_title": None, "terminal_title_stripped": None,
        "cwd": "/tmp/work", "foreground_cwd": None, "agent_session": None,
        "scroll": {"offset_from_bottom": 0, "max_offset_from_bottom": 0, "viewport_rows": 40},
    }
    pane.update(extra)
    return pane


def pane_from_agent(agent: Dict[str, Any], label: Optional[str] = None) -> Dict[str, Any]:
    """The ``PaneInfo`` row for an ``AgentInfo`` row (same terminal, pane, status, tokens)."""
    return fake_pane(
        agent["pane_id"], agent["terminal_id"], agent.get("agent"), label, str(agent.get("agent_status") or "unknown"),
        focused=bool(agent.get("focused")), tokens=dict(agent.get("tokens") or {}), state_labels=dict(agent.get("state_labels") or {}),
        cwd=agent.get("cwd"), revision=int(agent.get("revision") or 1),
    )


def fake_read(pane_id: str, text: str, source: str = "visible", fmt: str = "text", truncated: bool = False) -> Dict[str, Any]:
    """The ``pane_read`` result of ``agent.read`` / ``pane.read``: the screen is ``read.text``."""
    return {
        "type": "pane_read",
        "read": {
            "pane_id": pane_id, "workspace_id": pane_id.split(":")[0], "tab_id": pane_id.split(":")[0] + ":t1",
            "source": source, "format": fmt, "text": text, "revision": 1, "truncated": truncated,
        },
    }


def fake_explain(agent: str = "codex", state: str = "idle", rule_id: Optional[str] = "codex.idle", **extra: Any) -> Dict[str, Any]:
    """The ``agent.explain`` result: ``{"type":"agent_explain","explain":{...}}`` (``src/detect/manifest.rs`` ``explain_to_json_value``)."""
    explain: Dict[str, Any] = {
        "agent": agent, "state": state, "manifest_source": "bundled", "manifest_version": "1", "cached_remote_version": None,
        "local_override_shadowing_remote": False, "remote_update_status": None, "remote_update_error": None,
        "matched_rule": {"id": rule_id, "region": "after_last_prompt_marker"} if rule_id else None,
        "visible_idle": state == "idle", "visible_blocker": state == "blocked", "visible_working": state == "working",
        "screen_detection_skipped": False, "screen_detection_skip_reason": None,
        "skip_state_update": False, "skipped_update_reason": None, "fallback_reason": None, "warning": None,
        "evaluated_rules": [],
    }
    explain.update(extra)
    return {"type": "agent_explain", "explain": explain}


def fake_process_info(pane_id: str, shell_pid: Optional[int] = 4000, fg_pgid: Optional[int] = 4001, processes: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The ``pane_process_info`` result: ``process_info.{shell_pid, foreground_process_group_id, foreground_processes}``."""
    procs = processes if processes is not None else [{"pid": 4001, "name": "zsh", "argv": ["-zsh"], "argv0": "-zsh", "cmdline": "-zsh", "cwd": "/tmp/work"}]
    return {
        "type": "pane_process_info",
        "process_info": {"pane_id": pane_id, "shell_pid": shell_pid, "foreground_process_group_id": fg_pgid, "foreground_processes": procs, "tty": "/dev/ttys001"},
    }


def fake_plugin_info(plugin_id: str = "herdr-synapse", enabled: bool = True, root: str = "/x/herdr-synapse", warnings: Optional[List[str]] = None) -> Dict[str, Any]:
    """One ``InstalledPluginInfo`` row of ``plugin.list``."""
    return {
        "plugin_id": plugin_id, "name": plugin_id, "version": "0.1.0", "description": None,
        "manifest_path": root + "/herdr-plugin.toml", "plugin_root": root, "enabled": enabled, "min_herdr_version": "0.8.2",
        "platforms": None, "source": {"kind": "local"}, "startup": [], "actions": [], "events": [], "panes": [], "build": [],
        "link_handlers": [], "warnings": list(warnings or []),
    }


def fake_plugin_pane_opened(entrypoint: str, pane: Dict[str, Any], plugin_id: str = "herdr-synapse", kind: str = "opened") -> Dict[str, Any]:
    """``plugin_pane_opened`` / ``plugin_pane_focused``: the pane is at ``plugin_pane.pane``."""
    return {"type": "plugin_pane_" + kind, "plugin_pane": {"plugin_id": plugin_id, "entrypoint": entrypoint, "pane": dict(pane)}}


FAKE_AGENTS: List[Dict[str, Any]] = [
    fake_agent("w2:p1", "term_r1", "codex", "alpha-reviewer"),
    fake_agent("w2:p2", "term_w1", "claude", "alpha-worker", status="working"),
    fake_agent("wA:p6", "term_owner", "claude", None),
]

#: ``pane.list`` rows: the three agent panes (labelled per the roster) plus one plain shell pane.
FAKE_PANES: List[Dict[str, Any]] = [
    pane_from_agent(FAKE_AGENTS[0], "team:alpha/reviewer"),
    pane_from_agent(FAKE_AGENTS[1], "team:alpha/worker"),
    pane_from_agent(FAKE_AGENTS[2], None),
    fake_pane("w1:p1", "term_shell", None, None, "unknown"),
]


def default_responses() -> Dict[str, Any]:
    """Canned results for the methods the plugin uses most (``FakeHerdrServer`` default table).

    Deliberately small: a method missing here answers ``unknown_method``,
    which several tests rely on for fallback paths. ``canned_responses``
    covers every method the plugin calls.
    """
    return {method: _server_spec(spec) for method, spec in _DEFAULT_METHODS()}


def _DEFAULT_METHODS() -> List[Tuple[str, Any]]:
    catalogue = canned_responses()
    return [(m, catalogue[m]) for m in ("ping", "agent.list", "agent.get", "agent.prompt", "notification.show", "pane.report_metadata")]


def _server_spec(spec: Any) -> Any:
    """Adapt a ``canned_responses`` entry (``callable(params)``) to the server's ``callable(params, request)``."""
    if callable(spec):
        return lambda params, _req, _spec=spec: _spec(params)
    return spec


def canned_responses() -> Dict[str, Any]:
    """Schema-shaped results for every socket method the plugin calls (method -> result | callable(params)).

    Shapes follow ``tests/fixtures/herdr-api-0.8.2.schema.json`` and the
    handler code in upstream ``src/app/api``: ``agent.rename`` and
    ``agent.focus`` answer ``agent_info``, ``pane.rename`` answers
    ``pane_info``, ``agent.view.*`` answer ``agent_view``, ``agent.read``
    answers ``pane_read``, ``layout.apply`` answers ``layout_apply``,
    ``plugin.pane.open`` answers ``plugin_pane_opened`` (or a bare ``ok`` for
    a popup), ``popup.close`` / ``pane.close`` / ``pane.report_metadata``
    answer ``ok``. ``tests/test_schema_conformance.py`` validates every entry.
    Pass ``FakeApi(responses=canned_responses())`` to opt in.
    """
    return {
        "ping": {"type": "pong", "version": "0.8.2", "protocol": 20, "capabilities": {"live_handoff": True, "detached_server_daemon": False}},
        "agent.list": {"type": "agent_list", "agents": [dict(a) for a in FAKE_AGENTS]},
        "agent.get": _agent_get,
        "agent.prompt": lambda params: {"type": "agent_prompted", "agent": _prompted(params)},
        "agent.read": lambda params: fake_read(_resolve_pane_id(params.get("target")), "$ \n", str(params.get("source") or "recent"), str(params.get("format") or "text")),
        "agent.explain": lambda params: fake_explain(str(_agent_get(params)["agent"].get("agent") or "codex"), str(_agent_get(params)["agent"].get("agent_status") or "idle")),
        "agent.rename": lambda params: {"type": "agent_info", "agent": dict(_agent_get(params)["agent"], name=params.get("name"))},
        "agent.focus": lambda params: {"type": "agent_info", "agent": dict(_agent_get(params)["agent"], focused=True)},
        "agent.start": lambda params: {"type": "agent_started", "agent": fake_agent(str(params["pane_id"]), "term_started", str(params["kind"]), str(params["name"]), launch_pending=False, interactive_ready=True), "argv": [str(params["kind"])] + list(params.get("args") or [])},
        "agent.view.set": lambda params: {"type": "agent_view", "active": True, "source": params.get("source"), "label": params.get("label")},
        "agent.view.clear": {"type": "agent_view", "active": False, "source": None, "label": None},
        "pane.get": _pane_get,
        "pane.list": lambda params: {"type": "pane_list", "panes": [dict(p) for p in FAKE_PANES if not params.get("workspace_id") or p["workspace_id"] == params["workspace_id"]]},
        "pane.rename": lambda params: {"type": "pane_info", "pane": dict(_pane_get(params)["pane"], label=params.get("label"))},
        "pane.close": {"type": "ok"},
        "pane.report_metadata": {"type": "ok"},
        "pane.process_info": lambda params: fake_process_info(str(params.get("pane_id") or "w1:p1")),
        "layout.apply": _layout_apply,
        "plugin.list": {"type": "plugin_list", "plugins": [fake_plugin_info()]},
        "plugin.pane.open": _plugin_pane_open,
        "plugin.pane.focus": lambda params: fake_plugin_pane_opened("console", _pane_get(params)["pane"], kind="focused"),
        "plugin.pane.close": lambda params: {"type": "plugin_pane_closed", "pane_id": str(params.get("pane_id"))},
        "popup.close": {"type": "ok"},
        "notification.show": {"type": "notification_show", "shown": True, "reason": "shown"},
    }


def _resolve_pane_id(target: Any) -> str:
    for agent in FAKE_AGENTS:
        if target in (agent["pane_id"], agent["name"]):
            return str(agent["pane_id"])
    return str(target)


def _agent_get(params: Dict[str, Any]) -> Dict[str, Any]:
    target = params.get("target")
    for agent in FAKE_AGENTS:
        if target in (agent["pane_id"], agent["name"]):
            return {"type": "agent_info", "agent": dict(agent)}
    raise FakeError("agent_not_found", "agent target {} not found".format(target))


def _pane_get(params: Dict[str, Any]) -> Dict[str, Any]:
    pane_id = params.get("pane_id")
    for pane in FAKE_PANES:
        if pane_id == pane["pane_id"]:
            return {"type": "pane_info", "pane": dict(pane)}
    raise FakeError("pane_not_found", "pane {} not found".format(pane_id))


def _prompted(params: Dict[str, Any]) -> Dict[str, Any]:
    info = _agent_get(params)["agent"]
    info = dict(info)
    info["agent_status"] = "working"
    info["state_change_seq"] = int(info.get("state_change_seq", 0)) + 1
    return info


def _layout_apply(params: Dict[str, Any]) -> Dict[str, Any]:
    """``layout_apply``: the request tree echoed with a fresh ``pane_id`` on every leaf."""
    workspace_id = str(params.get("workspace_id") or "w9")
    counter = [0]

    def fill(node: Any) -> Dict[str, Any]:
        if not isinstance(node, dict):
            return {"type": "pane", "pane_id": None}
        if node.get("type") == "pane":
            counter[0] += 1
            out = dict(node)
            out["pane_id"] = "{}:p{}".format(workspace_id, counter[0])
            return out
        return {"type": "split", "direction": node.get("direction", "right"), "ratio": float(node.get("ratio", 0.5)), "first": fill(node.get("first")), "second": fill(node.get("second"))}

    root = fill(params.get("root"))
    return {"type": "layout_apply", "layout": {"workspace_id": workspace_id, "tab_id": workspace_id + ":t2", "zoomed": False, "focused_pane_id": workspace_id + ":p1", "root": root}}


def _plugin_pane_open(params: Dict[str, Any]) -> Dict[str, Any]:
    """Popup placement answers a bare ``ok`` (no pane id); every other placement answers ``plugin_pane_opened``."""
    if params.get("placement") == "popup":
        return {"type": "ok"}
    pane = fake_pane("w1:p9", "term_plugin", None, "Team console", "unknown")
    return fake_plugin_pane_opened(str(params.get("entrypoint") or "console"), pane, str(params.get("plugin_id") or "herdr-synapse"))


class FakeError(Exception):
    """Raise from a response callable to answer with ``{"error": {code, message}}``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# TempState


class TempState:
    """Isolated state root + env. Use as a context manager or call ``cleanup``."""

    def __init__(self, team: str = FAKE_TEAM, slug: str = FAKE_SLUG, members: Optional[List[Dict[str, Any]]] = None, write_team: bool = True) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ht-"))
        self.home = self.tmp / "home"
        self.config_home = self.tmp / "cfg"
        self.state_home = self.tmp / "st"
        self.config_dir = self.config_home / "herdr"
        self.state_root = self.tmp / "state"
        self.team_name = team
        self.slug = slug
        for d in (self.home, self.config_dir, self.state_home, self.state_root):
            d.mkdir(parents=True, exist_ok=True)
        if slug == "default":
            self.socket_path = self.config_dir / "herdr.sock"
        else:
            self.socket_path = self.config_dir / "sessions" / slug / "herdr.sock"
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.session = paths.session_paths(self.state_root, slug)
        paths.ensure_session_dirs(self.session)
        self.team = self.session.team(team)
        paths.ensure_team_dirs(self.team)
        self.members = [dict(m) for m in (members if members is not None else FAKE_MEMBERS)]
        if write_team:
            self.write_team_json()
        self.env: Dict[str, str] = {
            "HOME": os.fspath(self.home),
            "XDG_CONFIG_HOME": os.fspath(self.config_home),
            "XDG_STATE_HOME": os.fspath(self.state_home),
            "HERDR_TEAM_STATE_DIR": os.fspath(self.state_root),
            "HERDR_SOCKET_PATH": os.fspath(self.socket_path),
            "HERDR_ENV": "1",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }

    def write_team_json(self, charter: Optional[Dict[str, Any]] = None, revision: int = 1) -> Dict[str, Any]:
        from herdr_team import store

        doc = {
            "schema": 1, "team": self.team_name, "created_at": "2026-09-04T10:00:00Z",
            "socket": os.fspath(self.socket_path), "state_dir": os.fspath(self.state_root),
            "naming": "prefixed", "revision": revision,
            "charter": charter or {"seq": 1, "text": "Find and fix the bug.", "refs": [], "updated_at": "2026-09-04T10:00:00Z", "updated_by": "human"},
            "members": self.members,
        }
        store.write_json(self.team.team_json, doc)
        return doc

    def env_with(self, **overrides: Optional[str]) -> Dict[str, str]:
        """Copy of ``env`` with keys added, or removed when the value is None."""
        env = dict(self.env)
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    @property
    def layout(self) -> paths.Layout:
        return paths.resolve_layout(self.env)

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def __enter__(self) -> "TempState":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.cleanup()


# --------------------------------------------------------------------------
# FakeHerdrServer

ResponseSpec = Union[Dict[str, Any], Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]]


class FakeHerdrServer:
    """Threaded NDJSON Unix-socket server replaying canned responses."""

    REQUEST_TIMEOUT_S = 5.0
    MAX_REQUEST_BYTES = 1024 * 1024

    def __init__(self, socket_path: Optional[Union[str, Path]] = None, responses: Optional[Dict[str, ResponseSpec]] = None) -> None:
        self._own_dir: Optional[Path] = None
        if socket_path is None:
            self._own_dir = Path(tempfile.mkdtemp(prefix="hts-"))
            socket_path = self._own_dir / "herdr.sock"
        self.socket_path = Path(socket_path)
        self.responses: Dict[str, ResponseSpec] = default_responses()
        if responses:
            self.responses.update(responses)
        self.delays: Dict[str, float] = {}
        self.events: List[Dict[str, Any]] = []
        self.event_interval_s = 0.01
        self.hold_open = True
        self.requests: List[Dict[str, Any]] = []
        self.connections = 0
        self._stop = threading.Event()
        self._listener: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._workers: List[threading.Thread] = []
        self._lock = threading.Lock()

    # configuration -----------------------------------------------------------

    def set_response(self, method: str, result: ResponseSpec) -> None:
        self.responses[method] = result

    def set_error(self, method: str, code: str, message: str) -> None:
        def fail(_params: Dict[str, Any], _req: Dict[str, Any]) -> Dict[str, Any]:
            raise FakeError(code, message)

        self.responses[method] = fail

    def set_delay(self, method: str, seconds: float) -> None:
        self.delays[method] = seconds

    def script_events(self, events: Iterable[Dict[str, Any]], interval_s: float = 0.01, hold_open: bool = True) -> None:
        """Events pushed after the ack: ``{"event": "pane_updated", "data": {...}}`` each."""
        self.events = list(events)
        self.event_interval_s = interval_s
        self.hold_open = hold_open

    def requests_for(self, method: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [r for r in self.requests if r.get("method") == method]

    # lifecycle ---------------------------------------------------------------

    def start(self) -> "FakeHerdrServer":
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(os.fspath(self.socket_path))
        listener.listen(16)
        listener.settimeout(0.05)
        self._listener = listener
        self._thread = threading.Thread(target=self._accept_loop, name="fake-herdr-accept", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        for worker in list(self._workers):
            worker.join(timeout=2.0)
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        if self._own_dir is not None:
            shutil.rmtree(self._own_dir, ignore_errors=True)

    def __enter__(self) -> "FakeHerdrServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # internals ---------------------------------------------------------------

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            worker = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            self._workers.append(worker)
            worker.start()

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(self.REQUEST_TIMEOUT_S)
        try:
            reader = conn.makefile("rb")
            try:
                raw = reader.readline(self.MAX_REQUEST_BYTES + 1)
            except (socket.timeout, OSError):
                return
            if not raw or len(raw) > self.MAX_REQUEST_BYTES:
                return
            try:
                request = json.loads(raw.decode("utf-8"))
            except ValueError:
                self._write(conn, {"id": None, "error": {"code": "invalid_request", "message": "bad json"}})
                return
            with self._lock:
                self.requests.append(request)
            method = request.get("method")
            request_id = request.get("id")
            params = request.get("params") or {}
            delay = self.delays.get(method or "")
            if delay:
                if self._stop.wait(delay):
                    return
            if method == "events.subscribe":
                self._write(conn, {"id": request_id, "result": {"type": "subscription_started"}})
                self._stream_events(conn)
                return
            spec = self.responses.get(method or "")
            if spec is None:
                self._write(conn, {"id": request_id, "error": {"code": "unknown_method", "message": "unknown method {}".format(method)}})
                return
            try:
                result = spec(params, request) if callable(spec) else spec
            except FakeError as err:
                self._write(conn, {"id": request_id, "error": {"code": err.code, "message": err.message}})
                return
            self._write(conn, {"id": request_id, "result": result})
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _stream_events(self, conn: socket.socket) -> None:
        for event in self.events:
            if self._stop.wait(self.event_interval_s):
                return
            if not self._write(conn, event):
                return
        if not self.hold_open:
            return
        # Keep the connection open until stop or the client goes away.
        conn.settimeout(0.05)
        while not self._stop.is_set():
            try:
                data = conn.recv(1)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return

    @staticmethod
    def _write(conn: socket.socket, obj: Dict[str, Any]) -> bool:
        try:
            conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))
            return True
        except OSError:
            return False


# --------------------------------------------------------------------------
# FakeApi


class FakeApi:
    """In-process stand-in for ``herdr_team.api.HerdrApi``.

    ``responses`` maps method -> result dict or callable(params) -> result
    (raise ``FakeError`` for a server error). ``cli`` maps a tuple of CLI
    args -> ``(returncode, stdout, stderr)`` for ``run``/``run_json``.
    ``events`` is the scripted subscription stream. ``calls`` and
    ``runs`` log every invocation.
    """

    def __init__(self, socket_path: Union[str, Path] = "/nonexistent/herdr.sock", responses: Optional[Dict[str, Any]] = None) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = 5.0
        self.env: Dict[str, str] = {}
        # The same six defaults as ``FakeHerdrServer``; ``canned_responses()`` has the rest.
        self.responses: Dict[str, Any] = dict(_DEFAULT_METHODS())
        if responses:
            self.responses.update(responses)
        self.cli: Dict[Tuple[str, ...], Tuple[int, str, str]] = {}
        self.events: List[Dict[str, Any]] = []
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.runs: List[List[str]] = []
        self.unreachable = False

    def set_response(self, method: str, result: Any) -> None:
        self.responses[method] = result

    def set_error(self, method: str, code: str, message: str) -> None:
        def fail(_params: Dict[str, Any]) -> Dict[str, Any]:
            raise FakeError(code, message)

        self.responses[method] = fail

    def set_cli(self, args: Sequence[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.cli[tuple(args)] = (returncode, stdout, stderr)

    def set_cli_json(self, args: Sequence[str], obj: Any) -> None:
        """Canned stdout as given (no envelope); use ``set_cli_result`` for what the real CLI prints."""
        self.cli[tuple(args)] = (0, json.dumps(obj), "")

    def set_cli_result(self, args: Sequence[str], result: Any, request_id: str = "cli:fake") -> None:
        """Canned stdout in the real CLI shape: the socket envelope ``{"id":..,"result":result}``."""
        self.cli[tuple(args)] = (0, json.dumps({"id": request_id, "result": result}), "")

    def set_cli_error(self, args: Sequence[str], code: str, message: str, returncode: int = 1, request_id: str = "cli:fake") -> None:
        """Canned failure in the real CLI shape: ``{"id","error":{code,message}}`` on stderr, exit 1."""
        self.cli[tuple(args)] = (returncode, "", json.dumps({"id": request_id, "error": {"code": code, "message": message}}) + "\n")

    def request_raw(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        try:
            return {"id": "fake", "result": self.request(method, params, timeout)}
        except HerdrTeamError as err:
            return {"id": "fake", "error": {"code": err.code, "message": err.message}}

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        params = params or {}
        self.calls.append((method, params))
        if self.unreachable:
            raise HerdrTeamError("server_not_running", "fake server down", 3, {"socket": os.fspath(self.socket_path)})
        spec = self.responses.get(method)
        if spec is None:
            raise HerdrTeamError("unknown_method", "unknown method {}".format(method), 1, {"method": method})
        try:
            return spec(params) if callable(spec) else spec
        except FakeError as err:
            raise HerdrTeamError(err.code, err.message, exit_code_for(err.code), {"method": method})

    def ping(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        return self.request("ping", {}, timeout)

    def subscribe(self, subscriptions: Iterable[Union[str, Dict[str, Any]]], tick_timeout: Optional[float] = None, connect_timeout: Optional[float] = None) -> Iterator[Optional[Dict[str, Any]]]:
        self.calls.append(("events.subscribe", {"subscriptions": list(subscriptions)}))
        if self.unreachable:
            raise HerdrTeamError("server_not_running", "fake server down", 3)
        for event in list(self.events):
            yield event

    def run(self, args: Sequence[str], timeout: float = 3.0, pin_socket: bool = True) -> RunResult:
        argv = [str(a) for a in args]
        self.runs.append(argv)
        spec = self.cli.get(tuple(argv))
        if spec is None:
            for key, value in self.cli.items():
                if list(key) == argv[: len(key)]:
                    spec = value
                    break
        if spec is None:
            return RunResult(["herdr"] + argv, 1, "", json.dumps({"error": {"code": "unknown_command", "message": "fake: no canned result for {}".format(" ".join(argv))}}))
        code, out, err = spec
        return RunResult(["herdr"] + argv, code, out, err)

    def run_json(self, args: Sequence[str], timeout: float = 3.0, pin_socket: bool = True) -> Any:
        return parse_cli_json(self.run(args, timeout, pin_socket))


# --------------------------------------------------------------------------
# fake herdr binary for subprocess tests


FAKE_HERDR_SCRIPT = """#!/bin/sh
# Fake herdr binary: echoes args and selected env as JSON, honours a few verbs.
case "$1" in
  --version) echo "herdr 0.8.2-fake"; exit 0 ;;
  fail) printf '%s\\n' '{"error":{"code":"agent_not_found","message":"agent target nope not found"}}' >&2; exit 1 ;;
  sleep) exec sleep "$2" ;;
  stdin) if read -r line; then echo "got:$line"; else echo "stdin-closed"; fi; exit 0 ;;
esac
printf '{"argv":["%s"],"socket":"%s","session":"%s","pane":"%s"}\\n' "$*" "${HERDR_SOCKET_PATH:-}" "${HERDR_SESSION:-}" "${HERDR_PANE_ID:-}"
"""


def write_fake_herdr(directory: Path) -> Path:
    """Write an executable fake ``herdr`` and return its path (for ``HERDR_BIN_PATH``)."""
    path = directory / "herdr"
    path.write_text(FAKE_HERDR_SCRIPT, encoding="utf-8")
    os.chmod(path, 0o700)
    return path


def wait_until(predicate: Callable[[], bool], timeout_s: float = 2.0, interval_s: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()
