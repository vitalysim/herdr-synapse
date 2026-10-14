"""Bounded, owned native metadata connections; never attach to a user's session."""
from __future__ import annotations

import json
import os
import queue
import selectors
import signal
import subprocess
import threading
import time
import base64
import secrets
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Sequence


class NativeError(Exception):
    """A stable classification, deliberately excluding native output and secrets."""


def _stop_owned(proc: subprocess.Popen) -> None:
    """Reap our private process group even when its original parent already exited."""
    def send(sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError as denied:
            # Darwin can report EPERM for an already-exited, unreaped group.
            # Reap it, but never suppress denied cleanup of a still-live parent.
            if proc.poll() is None:
                # Exit notification and waitpid can race on Darwin. Observe our
                # child's settled exit, rather than treating one poll as final.
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    raise denied from None
                if proc.poll() is None:
                    raise
    proc.poll()
    send(signal.SIGTERM)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # A surviving grandchild may hold stdout open or ignore TERM after its parent
    # exits. This group was created by us; no pre-existing session is targeted.
    send(signal.SIGKILL)
    proc.wait(timeout=2)


def metadata_command(argv: Sequence[str], cwd: str, env: Dict[str, str], deadline: float) -> str:
    """One native metadata command with an owned process group and a shared deadline."""
    proc = subprocess.Popen(list(argv), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise NativeError("native_timeout")
                chunk = os.read(proc.stdout.fileno(), min(65536, 8 * 1024 * 1024 + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > 8 * 1024 * 1024:
                    raise NativeError("native_frame_too_large")
        try:
            proc.wait(timeout=max(.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise NativeError("native_timeout") from None
        if proc.returncode:
            raise NativeError("native_method_rejected")
        return output.decode("utf-8", "strict")
    finally:
        _stop_owned(proc)
        proc.stdout.close()


class JsonLines:
    """One owned stdio process, with a connection-wide deadline and bounded frames."""

    def __init__(self, argv: Sequence[str], cwd: str, env: Dict[str, str], timeout: float = 20) -> None:
        self.deadline = time.monotonic() + timeout
        self.frames: queue.Queue = queue.Queue(maxsize=64)
        self.seq = 0
        self.proc = subprocess.Popen(list(argv), cwd=cwd, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        self.closed = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        try:
            while not self.closed.is_set():
                line = self.proc.stdout.readline(1024 * 1024 + 1)
                if not line:
                    self._put(NativeError("native_eof"))
                    return
                if len(line) > 1024 * 1024:
                    self._put(NativeError("native_frame_too_large"))
                    return
                try:
                    frame = json.loads(line)
                except (ValueError, UnicodeError):
                    self._put(NativeError("native_malformed"))
                    return
                if not isinstance(frame, dict):
                    self._put(NativeError("native_malformed"))
                    return
                self._put(frame)
        except (OSError, ValueError):
            self._put(NativeError("native_io"))

    def _put(self, value: Any) -> None:
        while not self.closed.is_set():
            try:
                self.frames.put(value, timeout=.1)
                return
            except queue.Full:
                continue

    def send(self, frame: Dict[str, Any]) -> None:
        if time.monotonic() >= self.deadline:
            raise NativeError("native_timeout")
        try:
            self.proc.stdin.write(json.dumps(frame).encode("utf-8") + b"\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            raise NativeError("native_io") from None

    def receive(self) -> Dict[str, Any]:
        try:
            frame = self.frames.get(timeout=max(0, self.deadline - time.monotonic()))
        except queue.Empty:
            raise NativeError("native_timeout") from None
        if isinstance(frame, Exception):
            raise frame
        return frame

    def rpc(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self.seq += 1
        request_id = self.seq
        self.send({"id": request_id, "method": method, "params": params})
        for _ in range(1024):
            frame = self.receive()
            if frame.get("id") == request_id and "method" not in frame:
                if "error" in frame:
                    raise NativeError("native_method_rejected")
                result = frame.get("result")
                if not isinstance(result, dict):
                    raise NativeError("native_malformed")
                return result
            # Never authorize tools, auth flows, commands or elicitation requested by a child.
            if "id" in frame and "method" in frame:
                self.send({"id": frame["id"], "error": {"code": -32601, "message": "metadata only"}})
        raise NativeError("native_frame_limit")

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        _stop_owned(self.proc)
        self.reader.join(timeout=1)
        for stream in (self.proc.stdin, self.proc.stdout):
            if stream is not None:
                stream.close()

    def __enter__(self) -> "JsonLines":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class OpenCodeServer:
    """A private, authenticated loopback metadata helper, never a user's server."""

    def __init__(self, path: str, cwd: str, env: Dict[str, str], timeout: float = 20) -> None:
        self.deadline = time.monotonic() + timeout
        password = secrets.token_urlsafe(32)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        self.base = "http://127.0.0.1:{}".format(port)
        self.authorization = "Basic " + base64.b64encode(("opencode:" + password).encode()).decode()
        native_env = dict(env, OPENCODE_SERVER_USERNAME="opencode", OPENCODE_SERVER_PASSWORD=password,
                          OPENCODE_DISABLE_EXTERNAL_PLUGINS="true", OPENCODE_DISABLE_AUTOUPDATE="true")
        self.proc = subprocess.Popen([path, "serve", "--hostname", "127.0.0.1", "--port", str(port)],
                                     cwd=cwd, env=native_env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        # Do not honor system proxy settings for this owned loopback connection.
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.authenticated = False

    def request(self, route: str) -> Dict[str, Any]:
        if route not in ("/provider", "/config/providers", "/api/provider", "/api/model", "/api/integration"):
            raise NativeError("metadata_method_refused")
        while time.monotonic() < self.deadline:
            if self.proc.poll() is not None:
                raise NativeError("native_eof")
            request = urllib.request.Request(self.base + route, headers={"Authorization": self.authorization, "Accept": "application/json"})
            try:
                if not self.authenticated:
                    # A flag being accepted is not proof of authentication enforcement.
                    try:
                        with self.http.open(self.base + "/", timeout=max(.01, min(2, self.deadline - time.monotonic()))):
                            pass
                    except urllib.error.HTTPError as error:
                        if error.code not in (401, 403):
                            raise NativeError("native_authentication_unsupported") from None
                    else:
                        raise NativeError("native_authentication_unsupported")
                    self.authenticated = True
                with self.http.open(request, timeout=max(.01, min(2, self.deadline - time.monotonic()))) as response:
                    body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise NativeError("native_frame_too_large")
                obj = json.loads(body)
                if not isinstance(obj, dict):
                    raise NativeError("native_malformed")
                return obj
            except urllib.error.HTTPError:
                raise NativeError("native_method_rejected") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(min(.05, max(0, self.deadline - time.monotonic())))
            except (ValueError, UnicodeError):
                raise NativeError("native_malformed") from None
        raise NativeError("native_timeout")

    def __enter__(self) -> "OpenCodeServer":
        return self

    def __exit__(self, *args: Any) -> None:
        _stop_owned(self.proc)
