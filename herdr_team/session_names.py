"""Native conversation titles for plugin-created sessions, not roster identity.

Never rename an attached/resumed conversation. Pending work belongs to one
terminal/generation and, once reported, one exact native session ID.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import roster, store


def path(paths: Any) -> Path:
    return paths.root / "session-names.json"


def load(paths: Any) -> Dict[str, Any]:
    value = store.read_json(path(paths), default={})
    return value if isinstance(value, dict) else {}


def prepare(kind: str, name: str, args: Sequence[str]) -> Tuple[List[str], Dict[str, Any]]:
    """Only fresh launches call this. No network discovery or native DB writes."""
    extra = list(args)
    entry: Dict[str, Any] = {"kind": kind, "title": name, "status": "pending", "created": time.time(), "attempts": 0}
    if kind in ("claude", "pi"):
        extra += ["--name", name]
    elif kind == "opencode":
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        # OpenCode's TUI already owns a server. Pin it to a private loopback
        # endpoint so the plugin can PATCH the reported session, not another UI.
        extra += ["--hostname", "127.0.0.1", "--port", str(port)]
        entry["port"] = port
    return extra, entry


def arm(paths: Any, member: Any, entry: Dict[str, Any], session: Optional[Dict[str, Any]] = None) -> None:
    if entry["kind"] not in ("claude", "codex", "opencode", "pi"):
        return
    entry = dict(entry, terminal_id=member.terminal_id, generation=member.generation,
                 session_id=(session or member.session or {}).get("value"), awaiting_first_report=not bool(member.session))
    if entry["kind"] in ("claude", "pi"):
        entry["status"] = "launch-option"
    save(paths, member.name, entry)


def save(paths: Any, name: str, entry: Dict[str, Any]) -> None:
    with store.FileLock(paths.root / "session-names.lock"):
        state = load(paths)
        state[name] = entry
        store.write_json(path(paths), state)


def current(entry: Dict[str, Any], member: Dict[str, Any], agent: Dict[str, Any]) -> bool:
    if (member.get("status") != "active" or entry.get("terminal_id") != member.get("terminal_id")
            or agent.get("terminal_id") != member.get("terminal_id") or agent.get("pane_id") != member.get("pane_id")
            or agent.get("agent") != entry.get("kind")):
        return False
    session = (roster.session_of(agent) or {}).get("value")
    # The first session report can increment the generation. It is allowed
    # only until we've bound the first ID; thereafter both identities match.
    if entry.get("session_id"):
        generations = (entry.get("generation"), (entry.get("generation") or 0) + 1) if entry.get("awaiting_first_report") else (entry.get("generation"),)
        return session == entry["session_id"] and member.get("generation") in generations
    return member.get("generation") == entry.get("generation")


def before_brief(daemon: Any, team: Any, member: Dict[str, Any]) -> bool:
    """Called only after the normal delivery gates pass. True defers the brief."""
    name = member["name"]
    entry = load(team.paths).get(name)
    if not isinstance(entry, dict) or entry.get("kind") != "codex" or entry.get("status") != "pending":
        return False
    if time.time() - entry["created"] > 120:
        fail(daemon, team, name, entry, "timed out waiting for a safe empty composer")
        return False
    fresh = daemon.api.request("agent.get", {"target": member["pane_id"]}).get("agent")
    if not isinstance(fresh, dict) or not current(entry, member, fresh) or fresh.get("agent_status") != "idle":
        return True
    draft = daemon._prompt_line(team, name, member["pane_id"], "codex", daemon._detection_text(member["pane_id"]))
    if draft is None or draft.strip():
        return True
    # Persist an intent before sending: after a crash never blindly replay a
    # slash command into a possibly modified composer.
    entry.update(status="sent", sent_at=time.time(), generation=member.get("generation"), session_id=(roster.session_of(fresh) or {}).get("value"), awaiting_first_report=False)
    save(team.paths, name, entry)
    result, details = daemon._type_keystroke(member["pane_id"], "/rename " + entry["title"])
    if result != "landed_working":
        fail(daemon, team, name, entry, "rename submission uncertain: {}".format(details))
    return True


def codex_title(session: str) -> Optional[str]:
    root = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    try:
        with (root / "session_index.jsonl").open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 256 * 1024))
            lines = handle.read().decode("utf-8", "replace").splitlines()
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("id") == session:
                return record.get("thread_name")
    except OSError:
        pass
    return None


def opencode_title(port: int, session: str, title: str) -> None:
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("invalid managed OpenCode port")
    url = "http://127.0.0.1:{}/session/{}".format(port, urllib.parse.quote(session, safe=""))
    headers = {"Content-Type": "application/json"}
    password = os.environ.get("OPENCODE_SERVER_PASSWORD")
    if password:
        credentials = "{}:{}".format(os.environ.get("OPENCODE_SERVER_USERNAME", "opencode"), password)
        headers["Authorization"] = "Basic " + base64.b64encode(credentials.encode()).decode()
    request = urllib.request.Request(url, data=json.dumps({"title": title}).encode(), headers=headers, method="PATCH")
    # No ambient proxies for a managed local process. Redirects are not used.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=1.0) as response:
        result = json.loads(response.read(64 * 1024))
    if result.get("id") != session or result.get("title") != title:
        raise ValueError("OpenCode did not confirm the requested session title")


def fail(daemon: Any, team: Any, name: str, entry: Dict[str, Any], reason: str) -> None:
    entry.update(status="failed", error=reason)
    save(team.paths, name, entry)
    daemon._append_system(team, "session_name_failed", "{}: native session name not confirmed: {}".format(name, reason), ["human"], {"member": name})


def poll(daemon: Any, team: Any, now: float) -> None:
    if now - getattr(team, "titles_polled_at", 0) < 2:
        return
    team.titles_polled_at = now
    for name, entry in load(team.paths).items():
        if not isinstance(entry, dict) or entry.get("status") not in ("pending", "sent"):
            continue
        member = team.member(name)
        if not member:
            continue
        agent = daemon.agents.get(str(member.get("terminal_id")))
        if not agent:
            continue
        session = (roster.session_of(agent) or {}).get("value")
        pending = team.pending.get(name) if hasattr(team, "pending") else None
        # OpenCode creates its first conversation on the initial briefing.
        # Permit that one report only with positive evidence of our delivered
        # briefing, never merely because some new ID appeared in this pane.
        if (not entry.get("session_id") and session and entry.get("kind") == "opencode"
                and member.get("generation") == (entry.get("generation") or 0) + 1
                and pending is not None and pending.kind == "brief" and pending.landed_ms is not None):
            entry = dict(entry, session_id=session, generation=member["generation"], awaiting_first_report=False)
        if not current(entry, member, agent):
            fail(daemon, team, name, entry, "session or terminal changed; original naming request cancelled")
            continue
        if not session:
            if now - entry["created"] > 120:
                fail(daemon, team, name, entry, "no native session ID reported")
            continue
        if not entry.get("session_id") or entry.get("awaiting_first_report"):
            entry["session_id"] = session
            entry["generation"] = member.get("generation")
            entry["awaiting_first_report"] = False
            save(team.paths, name, entry)
        if entry["kind"] == "codex":
            if codex_title(session) == entry["title"]:
                entry["status"] = "complete"
                save(team.paths, name, entry)
            elif entry.get("status") == "sent" and now - entry.get("sent_at", now) > 30:
                fail(daemon, team, name, entry, "Codex did not confirm /rename; use /rename manually")
        elif entry["kind"] == "opencode" and now >= entry.get("retry_at", 0):
            try:
                opencode_title(entry["port"], session, entry["title"])
                entry["status"] = "complete"
                save(team.paths, name, entry)
            except Exception as err:  # noqa: BLE001 - optional naming cannot stop message delivery
                entry["attempts"] += 1
                if entry["attempts"] >= 3:
                    fail(daemon, team, name, entry, str(err))
                else:
                    entry["retry_at"] = now + 5
                    save(team.paths, name, entry)
