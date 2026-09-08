"""Starting an agent in a pane through ``herdr agent start``.

Shared by ``create --new`` (``cmd_roster``) and by the notifier's restart of a
member with new model flags (``daemon``), so both spawn an agent with the same
argv and the same ``agent_pane_busy`` retry. The names keep their leading
underscore because ``cmd_roster`` re-exports them and the tests reach them
there.
"""
from __future__ import annotations

import json
import subprocess
import time as _time
from typing import Any, Dict, List, Optional, Sequence

from herdr_team import api as _api
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError

AGENT_START_TIMEOUT_MS = 60000
AGENT_START_RETRY_S = 0.5
AGENT_START_RETRY_WINDOW_S = 10.0


def agent_start_argv(name: str, kind: str, pane_id: str, timeout_ms: int = AGENT_START_TIMEOUT_MS, args: Sequence[str] = ()) -> List[str]:
    """``herdr agent start NAME --kind K --pane P --timeout T [-- ARG...]``; ``args`` go to the harness untouched."""
    argv = ["agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", str(int(timeout_ms))]
    extra = [str(a) for a in args if str(a)]
    if extra:
        argv += ["--"] + extra
    return argv


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


def _start_agent(api: Any, name: str, kind: str, pane_id: str, sleep: Any = None, args: Sequence[str] = ()) -> Dict[str, Any]:
    """``herdr agent start`` with the plan's ``agent_pane_busy`` retry (500 ms for 10 s).

    Returns ``{"pane_id", "started", "terminal_id", "agent"}``; ``terminal_id``
    and ``agent`` come from the CLI's ``agent_started`` result when it printed
    one (``_started_agent``) and are None otherwise.
    """
    sleeper = sleep if sleep is not None else _time.sleep
    deadline = _time.monotonic() + AGENT_START_RETRY_WINDOW_S
    while True:
        result = api.run(agent_start_argv(name, kind, pane_id, args=args), timeout=(AGENT_START_TIMEOUT_MS / 1000.0) + 5.0)
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


class StartHandle:
    """A ``herdr agent start`` in flight, so a daemon tick never waits on it.

    ``herdr agent start`` blocks until the agent is ready (up to a minute), which
    is fine for the CLI and not for a notifier tick. With a real ``HerdrApi``
    the command runs as a child process polled each tick; a fake API without
    ``spawn`` runs it synchronously and the handle is complete at once.
    """

    def __init__(self, argv: List[str], proc: Optional[Any] = None, result: Optional[Any] = None) -> None:
        self.argv = argv
        self.proc = proc
        self._result = result
        self.started_at = _time.monotonic()

    def done(self) -> bool:
        if self.proc is None:
            return True
        return self.proc.poll() is not None

    def result(self) -> Any:
        """``(ok, text)`` once ``done()``; the text is stderr+stdout for a failure message."""
        if self.proc is None:
            res = self._result
            ok = bool(getattr(res, "ok", False))
            text = ((getattr(res, "stderr", "") or "") + (getattr(res, "stdout", "") or "")).strip()
            return ok, text
        out, err = self.proc.communicate(timeout=5)
        text = ((err or b"").decode("utf-8", "replace") + (out or b"").decode("utf-8", "replace")).strip()
        return self.proc.returncode == 0, text


def start_agent_async(api: Any, name: str, kind: str, pane_id: str, args: Sequence[str] = ()) -> StartHandle:
    """Launch ``herdr agent start`` without waiting for the agent to become ready."""
    argv = agent_start_argv(name, kind, pane_id, args=args)
    spawn = getattr(api, "spawn", None)
    if callable(spawn):
        return StartHandle(argv, proc=spawn(argv))
    return StartHandle(argv, result=api.run(argv, timeout=(AGENT_START_TIMEOUT_MS / 1000.0) + 5.0))
