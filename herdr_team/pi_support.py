"""Pi's optional observability extension; never a source of delivery authority.

Snapshots are separate from hook/roster state and bound to the exact live
terminal and native session. No model catalogue or transcript parsing needed.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import context, paths, roster, store
from .cli import Command, api_for, emit, layout_for
from .errors import EXIT_REFUSED, HerdrTeamError

VERSION = 1
MAX_BYTES = 8192
TTL = 20.0
MARKER = "// herdr-synapse pi extension v1"


def extension_path(env: Dict[str, str]) -> Path:
    value = env.get("PI_CODING_AGENT_DIR")
    if value == "~" or (value and value.startswith("~/")):
        value = str(paths.home_dir(env)) + value[1:]
    root = Path(value or (paths.home_dir(env) / ".pi" / "agent"))
    return root / "extensions" / "herdr-synapse.ts"


def extension_text(cli: Path) -> str:
    source = paths.plugin_root() / "assets" / "pi-synapse.ts"
    return source.read_text(encoding="utf-8").replace("__SYNAPSE_CLI__", json.dumps(os.fspath(cli)))


def install(args: Any, cli: Path) -> Dict[str, Any]:
    target = extension_path(args.env)
    expected = extension_text(cli)
    existing = target.read_text(encoding="utf-8") if target.is_file() and not target.is_symlink() else None
    foreign = target.is_symlink() or (target.exists() and (existing is None or not existing.startswith(MARKER)))
    payload = {"kind": "pi", "action": args.action, "hook": os.fspath(target), "ok": True,
               "installed": existing == expected, "warnings": [], "added": [], "removed": [], "already": []}
    if foreign:
        raise HerdrTeamError("foreign_extension", "not replacing an unmanaged Pi extension: {}".format(target), EXIT_REFUSED)
    if args.action == "check":
        payload["ok"] = existing == expected
    elif args.action == "install":
        if existing == expected:
            payload["already"] = [os.fspath(target)]
        elif getattr(args, "dry_run", False):
            payload["would_write"] = True
        else:
            paths.ensure_dir(target.parent, 0o700)
            store.atomic_write(target, expected.encode("utf-8"))
            payload["added"] = [os.fspath(target)]
            payload["installed"] = True
    elif args.action == "uninstall" and existing is not None:
        if getattr(args, "dry_run", False):
            payload["would_remove"] = True
        else:
            target.unlink()
            payload["removed"] = [os.fspath(target)]
            payload["installed"] = False
    payload["warnings"] = ["Pi uses typed delivery, not Claude stop hooks. Run /reload in existing Pi sessions after installation or removal."]
    if not payload["installed"]:
        payload["warnings"].append("Runtime context requires `herdr-synapse hooks install pi`, Herdr's Pi integration, and a Pi reload.")
    return payload


def snapshot_path(session: Any, terminal: str) -> Path:
    # Reuse the terminal-id validation rather than permitting arbitrary paths.
    return session.root / "pi" / session.pane_record(terminal).name


def _number(value: Any, minimum: float = 0) -> bool:
    return type(value) in (int, float) and minimum <= value <= 10**12 and math.isfinite(value)


def validate(payload: Any, now: float) -> Dict[str, Any]:
    def fail() -> None:
        raise HerdrTeamError("pi_report_invalid", "invalid or stale Pi runtime report", EXIT_REFUSED)

    if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload.get("version") != VERSION or payload.get("mode") != "tui":
        fail()
    at = payload.get("at")
    if not _number(at) or not -5 <= now - at <= TTL:
        fail()
    for key in ("session", "provider", "model", "instance"):
        value = payload.get(key)
        if not isinstance(value, str) or not value or len(value) > (4096 if key == "session" else 256) or any(ord(c) < 32 for c in value):
            fail()
    if not os.path.isabs(payload["session"]):
        fail()
    used, window = payload.get("used"), payload.get("window")
    if used is not None and (type(used) is not int or not 0 <= used <= 10**12):
        fail()
    if window is not None and (type(window) is not int or not 0 < window <= 10**12):
        fail()
    effort = payload.get("effort")
    if effort is not None and (not isinstance(effort, str) or len(effort) > 32):
        fail()
    compact = payload.get("compact")
    if compact is not None:
        if (not isinstance(compact, dict) or not isinstance(compact.get("id"), str)
                or not 0 < len(compact["id"]) <= 128 or not _number(compact.get("at"))
                or compact["at"] > at or compact.get("status") not in ("success", "failed")):
            fail()
        compact = {key: compact[key] for key in ("id", "at", "status")}
    return {"version": VERSION, "at": at, "session": payload["session"], "instance": payload["instance"],
            "provider": payload["provider"], "model": payload["model"], "effort": effort,
            "used": used, "window": window, "compact": compact}


def receive(layout: Any, api: Any, env: Dict[str, str], payload: Any, now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    data = validate(payload, now)
    pane = env.get("HERDR_PANE_ID")
    if env.get("HERDR_ENV") != "1" or not pane:
        raise HerdrTeamError("pi_report_identity", "Pi reporting requires a Herdr pane", EXIT_REFUSED)
    agent = api.request("agent.get", {"target": pane}, timeout=3).get("agent") or {}
    session = roster.session_of(agent)
    if (agent.get("agent") != "pi" or agent.get("pane_id") != pane or not agent.get("terminal_id")
            or not session or session.get("source") != "herdr:pi" or session.get("kind") != "path"
            or session.get("value") != data["session"]):
        raise HerdrTeamError("pi_report_identity", "Pi report does not match the live native session", EXIT_REFUSED)
    data.update(pane_id=pane, terminal_id=agent["terminal_id"])
    target = snapshot_path(layout.session, agent["terminal_id"])
    paths.ensure_dir(target.parent, 0o700)
    with store.FileLock(target.with_suffix(".lock")):
        previous = store.read_json(target, default={})
        if isinstance(previous, dict) and _number(previous.get("at")) and previous["at"] >= data["at"]:
            return
        store.write_json(target, data, fsync=False)  # ephemeral, refreshed every five seconds


def read_snapshot(session: Any, member: Dict[str, Any], now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    now = time.time() if now is None else now
    native = roster.session_of(member.get("session"))
    if (member.get("kind") != "pi" or not member.get("terminal_id") or not native
            or native.get("kind") != "path" or native.get("source") != "herdr:pi"):
        return None
    try:
        target = snapshot_path(session, str(member["terminal_id"]))
        if target.is_symlink() or target.stat().st_size > MAX_BYTES:
            return None
        data = store.read_json(target, default={})
        # Stored reports intentionally have no mode; only the validated receiver writes them.
        clean = validate(dict(data, mode="tui"), now)
        if (data.get("terminal_id") != member["terminal_id"] or data.get("pane_id") != member.get("pane_id")
                or clean["session"] != native.get("value")):
            return None
        return dict(clean, source=os.fspath(target))
    except (OSError, ValueError, TypeError, HerdrTeamError):
        return None


def reading(data: Dict[str, Any]) -> context.Reading:
    return context.Reading(data["used"], data["window"], data["source"],
                           model=data["provider"] + "/" + data["model"], estimated=True)


def matches_control(data: Dict[str, Any], member: Dict[str, Any], control: Dict[str, Any]) -> bool:
    return (control.get("terminal_id") == member.get("terminal_id")
            and control.get("native_session") == data.get("session"))


def run_report(args: Any) -> int:
    raw = getattr(args, "stdin", sys.stdin).read(MAX_BYTES + 1)
    if len(raw.encode("utf-8")) > MAX_BYTES:
        raise HerdrTeamError("pi_report_invalid", "Pi report exceeds size limit", EXIT_REFUSED)
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HerdrTeamError("pi_report_invalid", "Pi report must be JSON", EXIT_REFUSED)
    receive(layout_for(args), api_for(args), args.env, payload)
    return 0


COMMANDS = [Command("pi-report", "internal: Pi runtime telemetry", lambda parser: None, run_report, hidden=True)]
