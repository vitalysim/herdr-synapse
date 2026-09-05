"""The only door to Herdr: NDJSON socket client and ``herdr`` subprocess wrappers.

Wire contract (verified against ``herdr api schema --json``, protocol 20):

* request line  ``{"id":"<id>","method":"agent.get","params":{...}}``
* success line  ``{"id":"<id>","result":{"type":"agent_info",...}}``
* error line    ``{"id":"<id>","error":{"code":"agent_not_found","message":"..."}}``
* ``events.subscribe`` answers ``{"id":..,"result":{"type":"subscription_started"}}``
  and then pushes ``{"event":"pane_agent_detected","data":{...}}`` lines until
  the client closes the connection. Subscription params are
  ``{"subscriptions":[{"type":"pane.updated"}, ...]}``.

CLI wrappers differ from the raw socket (verified in ``src/cli/*.rs`` at
v0.8.2; ``tests/test_schema_conformance.py`` pins the socket shapes against
``tests/fixtures/herdr-api-0.8.2.schema.json``):

* ``herdr pane get <id>``, ``agent get <target>``, ``agent list``,
  ``pane list`` take no ``--json`` flag (``--json`` is a usage error, exit
  2) and always print the whole response envelope
  ``{"id":"cli:pane:get","result":{"type":"pane_info","pane":{...}}}`` on
  stdout. ``plugin list --json`` prints the same envelope. ``run_json``
  strips that envelope, so callers get the ``result`` object exactly as
  ``request`` would return it.
* A failing CLI call prints ``{"id":..,"error":{"code","message"}}`` on
  stderr and exits 1; ``parse_cli_json`` maps that to the server code.
* ``agent read`` and ``pane read`` print the text itself, not JSON; use
  ``run`` and read ``stdout``.
* Result accessors below (``read_text``, ``plugin_pane_id``, ``explain_of``,
  ``agent_of``, ``pane_of``) know the nested result shapes so call sites do
  not guess: ``agent.read`` answers ``pane_read`` with ``read.text``;
  ``plugin.pane.open`` answers ``plugin_pane_opened`` with
  ``plugin_pane.pane.pane_id`` for split/tab/overlay/zoomed placements and a
  bare ``{"type":"ok"}`` for ``popup``; ``agent.explain`` answers
  ``{"type":"agent_explain","explain":{...}}``.

Rules every caller relies on:

* Every socket call has a client-side timeout (the server has none). A
  timeout is ``herdr_timeout`` (exit 3); a missing or refusing socket is
  ``server_not_running`` (exit 3); a server error is a ``HerdrTeamError``
  whose ``code`` is the server's code (exit 1 unless ``errors.EXIT_CODE_FOR``
  says otherwise) and whose ``details`` carry ``method``.
* Every subprocess uses ``HERDR_BIN_PATH`` when set, never a bare ``herdr``
  in that case; ``stdin=DEVNULL``, captured output, a timeout.
* Nothing here reads ``os.environ`` implicitly except as the default ``env``
  of ``HerdrApi``; tests inject their own.
"""

from __future__ import annotations

import itertools
import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Union

from herdr_team.errors import (
    EXIT_REFUSED,
    EXIT_UNREACHABLE,
    HerdrTeamError,
    exit_code_for,
)

DEFAULT_TIMEOUT_S = 5.0
PROMPT_TIMEOUT_S = 15.0
SUBPROCESS_TIMEOUT_S = 3.0
MAX_LINE_BYTES = 16 * 1024 * 1024

#: Pane-identity variables the daemon must never inherit (plan 4.3 step 1).
IDENTITY_ENV_VARS = ("HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID")

_ids = itertools.count(1)


def herdr_bin(env: Mapping[str, str]) -> str:
    """``HERDR_BIN_PATH`` when set, else ``herdr`` from PATH."""
    return env.get("HERDR_BIN_PATH") or "herdr"


def scrub_env(env: Mapping[str, str], drop: Iterable[str] = IDENTITY_ENV_VARS) -> Dict[str, str]:
    """Copy ``env`` without the named variables."""
    dropped = set(drop)
    return {k: v for k, v in env.items() if k not in dropped}


def child_env(env: Mapping[str, str], socket_path: Optional[Path] = None) -> Dict[str, str]:
    """Environment for a ``herdr`` child: pin ``HERDR_SOCKET_PATH`` when a socket is given."""
    out = dict(env)
    if socket_path is not None:
        out["HERDR_SOCKET_PATH"] = os.fspath(socket_path)
        out.pop("HERDR_SESSION", None)
    return out


def next_request_id(prefix: str = "herdr-team") -> str:
    return "{}:{}:{}".format(prefix, os.getpid(), next(_ids))


def api_error(code: str, message: str, method: str, extra: Optional[Dict[str, Any]] = None) -> HerdrTeamError:
    details: Dict[str, Any] = {"method": method}
    if extra:
        details.update(extra)
    return HerdrTeamError(code, message, exit_code_for(code, EXIT_REFUSED), details)


def normalize_subscriptions(subscriptions: Iterable[Union[str, Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Accept ``"pane.updated"`` or ``{"type": "pane.updated", ...}`` entries."""
    out: List[Dict[str, Any]] = []
    for entry in subscriptions:
        if isinstance(entry, str):
            out.append({"type": entry})
        elif isinstance(entry, dict) and isinstance(entry.get("type"), str):
            out.append(dict(entry))
        else:
            raise HerdrTeamError("usage", "subscription must be a kind string or {'type': ...}", 2)
    return out


@dataclass(frozen=True)
class RunResult:
    args: List[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class _Connection:
    """One socket, one request; a subscription keeps it open."""

    def __init__(self, socket_path: Path, timeout: Optional[float]) -> None:
        self.socket_path = socket_path
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(os.fspath(socket_path))
        except (FileNotFoundError, ConnectionRefusedError, PermissionError, socket.timeout, TimeoutError) as err:
            self.sock.close()
            raise HerdrTeamError(
                "server_not_running",
                "cannot connect to Herdr at {}: {}".format(socket_path, err),
                EXIT_UNREACHABLE,
                {"socket": os.fspath(socket_path)},
            )
        except OSError as err:
            self.sock.close()
            raise HerdrTeamError(
                "server_not_running",
                "cannot connect to Herdr at {}: {}".format(socket_path, err),
                EXIT_UNREACHABLE,
                {"socket": os.fspath(socket_path)},
            )
        self.buffer = bytearray()

    def send(self, obj: Dict[str, Any]) -> None:
        data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            self.sock.sendall(data)
        except (socket.timeout, TimeoutError):
            raise HerdrTeamError("herdr_timeout", "timed out sending to Herdr", EXIT_UNREACHABLE, {"socket": os.fspath(self.socket_path)})
        except OSError as err:
            raise HerdrTeamError("server_not_running", "Herdr connection failed: {}".format(err), EXIT_UNREACHABLE)

    def _read_raw_line(self) -> bytes:
        """Buffered ``recv`` until ``\\n``; a timeout keeps the partial buffer for the next call."""
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self.buffer[: newline + 1])
                del self.buffer[: newline + 1]
                return line
            if len(self.buffer) > MAX_LINE_BYTES:
                raise HerdrTeamError("herdr_protocol", "Herdr line exceeded the {} byte cap".format(MAX_LINE_BYTES), EXIT_UNREACHABLE)
            chunk = self.sock.recv(65536)
            if not chunk:
                line = bytes(self.buffer)
                del self.buffer[:]
                return line
            self.buffer.extend(chunk)

    def read_line(self, method: str) -> Optional[Dict[str, Any]]:
        """One parsed JSON line, or None at EOF. Raises on timeout or garbage."""
        try:
            raw = self._read_raw_line()
        except (socket.timeout, TimeoutError):
            raise HerdrTeamError("herdr_timeout", "timed out waiting for Herdr ({})".format(method), EXIT_UNREACHABLE, {"method": method})
        except OSError as err:
            raise HerdrTeamError("server_not_running", "Herdr connection failed: {}".format(err), EXIT_UNREACHABLE, {"method": method})
        if not raw:
            return None
        if not raw.endswith(b"\n"):
            raise HerdrTeamError("herdr_protocol", "Herdr closed mid-line", EXIT_UNREACHABLE, {"method": method})
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise HerdrTeamError("herdr_protocol", "Herdr sent a non-JSON line", EXIT_UNREACHABLE, {"method": method})
        if not isinstance(obj, dict):
            raise HerdrTeamError("herdr_protocol", "Herdr sent a non-object line", EXIT_UNREACHABLE, {"method": method})
        return obj

    def settimeout(self, timeout: Optional[float]) -> None:
        self.sock.settimeout(timeout)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _check_response(response: Optional[Dict[str, Any]], request_id: str, method: str) -> Dict[str, Any]:
    if response is None:
        raise HerdrTeamError("server_not_running", "Herdr closed the connection without answering", EXIT_UNREACHABLE, {"method": method})
    if response.get("id") != request_id:
        raise HerdrTeamError("herdr_protocol", "response id mismatch", EXIT_UNREACHABLE, {"method": method, "id": response.get("id")})
    error = response.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "herdr_error")
        message = str(error.get("message") or code)
        raise api_error(code, message, method)
    result = response.get("result")
    if not isinstance(result, dict):
        raise HerdrTeamError("herdr_protocol", "response has no result object", EXIT_UNREACHABLE, {"method": method})
    return result


class HerdrApi:
    """Socket client plus subprocess wrappers bound to one socket path.

    ``FakeApi`` in ``tests/support.py`` mirrors this call surface:
    ``request``, ``ping``, ``subscribe``, ``run``, ``run_json``.
    """

    def __init__(
        self,
        socket_path: Union[str, Path],
        timeout: float = DEFAULT_TIMEOUT_S,
        env: Optional[Mapping[str, str]] = None,
        id_prefix: str = "herdr-team",
    ) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = float(timeout)
        self.env: Mapping[str, str] = env if env is not None else os.environ
        self.id_prefix = id_prefix

    # -- socket ---------------------------------------------------------------

    def request_raw(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Send one request, return the full response object (``id`` plus ``result`` or ``error``)."""
        request_id = next_request_id(self.id_prefix)
        conn = _Connection(self.socket_path, self.timeout if timeout is None else timeout)
        try:
            conn.send({"id": request_id, "method": method, "params": params if params is not None else {}})
            response = conn.read_line(method)
        finally:
            conn.close()
        if response is None:
            raise HerdrTeamError("server_not_running", "Herdr closed the connection without answering", EXIT_UNREACHABLE, {"method": method})
        return response

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Send one request and return ``result``; server errors raise ``HerdrTeamError``."""
        request_id = next_request_id(self.id_prefix)
        conn = _Connection(self.socket_path, self.timeout if timeout is None else timeout)
        try:
            conn.send({"id": request_id, "method": method, "params": params if params is not None else {}})
            response = conn.read_line(method)
        finally:
            conn.close()
        return _check_response(response, request_id, method)

    def ping(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """``{"type":"pong","version":"0.8.2","protocol":20,...}``."""
        return self.request("ping", {}, timeout=timeout)

    def subscribe(
        self,
        subscriptions: Iterable[Union[str, Dict[str, Any]]],
        tick_timeout: Optional[float] = None,
        connect_timeout: Optional[float] = None,
    ) -> Iterator[Optional[Dict[str, Any]]]:
        """Generator over pushed event lines ``{"event": ..., "data": {...}}``.

        The ack is consumed before the first yield. With ``tick_timeout`` set
        the generator yields ``None`` whenever that many seconds pass with no
        event, so a polling loop can tick; without it reads block. The
        generator ends at EOF (server gone); closing it closes the socket.
        """
        subs = normalize_subscriptions(subscriptions)
        request_id = next_request_id(self.id_prefix)
        conn = _Connection(self.socket_path, self.timeout if connect_timeout is None else connect_timeout)
        try:
            conn.send({"id": request_id, "method": "events.subscribe", "params": {"subscriptions": subs}})
            ack = _check_response(conn.read_line("events.subscribe"), request_id, "events.subscribe")
            if ack.get("type") != "subscription_started":
                raise HerdrTeamError("herdr_protocol", "unexpected subscription ack", EXIT_UNREACHABLE, {"result": ack})
            conn.settimeout(tick_timeout)
            while True:
                try:
                    line = conn.read_line("events.subscribe")
                except HerdrTeamError as err:
                    if err.code == "herdr_timeout" and tick_timeout is not None:
                        yield None
                        continue
                    raise
                if line is None:
                    return
                yield line
        finally:
            conn.close()

    # -- subprocess -----------------------------------------------------------

    def run(self, args: Sequence[str], timeout: float = SUBPROCESS_TIMEOUT_S, pin_socket: bool = True) -> RunResult:
        """Run ``herdr <args>``; never raises on a non-zero exit, only on timeout or a missing binary."""
        env = child_env(self.env, self.socket_path if pin_socket else None)
        return run_herdr(list(args), timeout=timeout, env=env)

    def run_json(self, args: Sequence[str], timeout: float = SUBPROCESS_TIMEOUT_S, pin_socket: bool = True) -> Any:
        """``run`` plus JSON parsing; a failing command raises with the CLI's error code when it printed one."""
        result = self.run(args, timeout=timeout, pin_socket=pin_socket)
        return parse_cli_json(result)


def run_herdr(args: Sequence[str], timeout: float = SUBPROCESS_TIMEOUT_S, env: Optional[Mapping[str, str]] = None) -> RunResult:
    """Run the Herdr CLI once: ``HERDR_BIN_PATH`` when set, stdin closed, output captured, bounded."""
    environment = dict(env) if env is not None else dict(os.environ)
    binary = herdr_bin(environment)
    argv = [binary] + [str(a) for a in args]
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=environment,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise HerdrTeamError("herdr_timeout", "herdr {} did not finish within {:g}s".format(" ".join(args[:2]), timeout), EXIT_UNREACHABLE, {"args": list(args)})
    except FileNotFoundError:
        raise HerdrTeamError("herdr_not_found", "cannot execute {}".format(binary), EXIT_UNREACHABLE, {"binary": binary})
    except PermissionError:
        raise HerdrTeamError("herdr_not_found", "cannot execute {} (permission denied)".format(binary), EXIT_UNREACHABLE, {"binary": binary})
    return RunResult(
        argv,
        completed.returncode,
        completed.stdout.decode("utf-8", "replace"),
        completed.stderr.decode("utf-8", "replace"),
    )


def parse_cli_json(result: RunResult) -> Any:
    """Parse a CLI result's stdout as JSON; map a failure to a ``HerdrTeamError``.

    The Herdr CLI prints the socket envelope (``{"id","result"}`` or
    ``{"id","error"}``) verbatim; the envelope is stripped so the caller
    gets the ``result`` object, the same shape ``HerdrApi.request`` returns.
    Output that is not an envelope (a fake binary, ``session list --json``)
    is returned as parsed.
    """
    if result.returncode != 0:
        code, message = _cli_error(result)
        raise HerdrTeamError(code, message, exit_code_for(code, EXIT_REFUSED), {"args": result.args[1:], "returncode": result.returncode})
    text = result.stdout.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        raise HerdrTeamError("herdr_protocol", "herdr printed non-JSON output", EXIT_UNREACHABLE, {"args": result.args[1:]})
    return unwrap_cli_response(parsed, list(result.args[1:]))


def unwrap_cli_response(obj: Any, args: Optional[Sequence[str]] = None) -> Any:
    """Strip the CLI's response envelope: ``{"id","result":R}`` -> ``R``; ``{"id","error":E}`` raises.

    Anything that is not an envelope is returned unchanged.
    """
    if not isinstance(obj, dict):
        return obj
    error = obj.get("error")
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        code = str(error.get("code"))
        method = " ".join(str(a) for a in args[:2]) if args else "cli"
        raise api_error(code, str(error.get("message") or code), method, {"args": list(args) if args else []})
    if "result" in obj and ("id" in obj or len(obj) == 1):
        return obj["result"]
    return obj


# -- result accessors --------------------------------------------------------------
#
# Socket results are tagged unions (``{"type": ..., ...}``); the accessors
# below encode the nesting each method really uses (schema fixture
# ``tests/fixtures/herdr-api-0.8.2.schema.json``) so call sites stop guessing.


def agent_of(result: Any) -> Optional[Dict[str, Any]]:
    """The ``AgentInfo`` inside ``agent_info`` / ``agent_prompted`` / ``agent_started`` results."""
    if isinstance(result, dict) and isinstance(result.get("agent"), dict):
        return result["agent"]
    return None


def pane_of(result: Any) -> Optional[Dict[str, Any]]:
    """The ``PaneInfo`` inside ``pane_info`` / ``pane_current`` results."""
    if isinstance(result, dict) and isinstance(result.get("pane"), dict):
        return result["pane"]
    return None


def read_text(result: Any) -> Optional[str]:
    """The screen text of an ``agent.read`` / ``pane.read`` result (``pane_read``: ``read.text``).

    A bare ``text`` at the top level is accepted for older fakes; anything
    else is None.
    """
    if not isinstance(result, dict):
        return None
    read = result.get("read")
    if isinstance(read, dict) and isinstance(read.get("text"), str):
        return read["text"]
    text = result.get("text")
    return text if isinstance(text, str) else None


def explain_of(result: Any) -> Optional[Dict[str, Any]]:
    """The explain object of an ``agent.explain`` result (``{"type":"agent_explain","explain":{...}}``)."""
    if isinstance(result, dict) and isinstance(result.get("explain"), dict):
        return result["explain"]
    return None


def plugin_pane_id(result: Any) -> Optional[str]:
    """The public pane id of a ``plugin.pane.open`` / ``plugin.pane.focus`` result.

    ``plugin_pane_opened`` and ``plugin_pane_focused`` carry
    ``plugin_pane.pane.pane_id``; ``plugin_pane_closed`` carries ``pane_id``;
    a popup placement answers a bare ``{"type":"ok"}`` (no pane id, the
    popup is not a tiled pane) and yields None.
    """
    if not isinstance(result, dict):
        return None
    plugin_pane = result.get("plugin_pane")
    if isinstance(plugin_pane, dict):
        pane = plugin_pane.get("pane")
        if isinstance(pane, dict) and isinstance(pane.get("pane_id"), str) and pane["pane_id"]:
            return pane["pane_id"]
    pane_id = result.get("pane_id")
    if isinstance(pane_id, str) and pane_id:
        return pane_id
    pane = result.get("pane")
    if isinstance(pane, dict) and isinstance(pane.get("pane_id"), str) and pane["pane_id"]:
        return pane["pane_id"]
    return None


def _cli_error(result: RunResult) -> "tuple[str, str]":
    for stream in (result.stderr, result.stdout):
        text = stream.strip()
        if not text:
            continue
        try:
            obj = json.loads(text.splitlines()[-1])
        except ValueError:
            continue
        if isinstance(obj, dict):
            error = obj.get("error") if isinstance(obj.get("error"), dict) else obj
            code = error.get("code")
            if isinstance(code, str) and code:
                return code, str(error.get("message") or code)
    message = (result.stderr.strip() or result.stdout.strip() or "herdr exited {}".format(result.returncode)).splitlines()[-1]
    lowered = message.lower()
    if "not running" in lowered or "connect" in lowered or "no such file" in lowered:
        return "server_not_running", message
    return "herdr_cli_failed", message


def default_api(socket_path: Union[str, Path], env: Optional[Mapping[str, str]] = None) -> HerdrApi:
    return HerdrApi(socket_path, env=env)


if __name__ == "__main__":  # pragma: no cover - manual probe
    api = HerdrApi(sys.argv[1])
    print(json.dumps(api.ping()))
