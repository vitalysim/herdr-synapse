"""How full each member's context window is, read from the harness's own files.

Herdr knows nothing about tokens, and no agent kind will tell us over a socket,
but every kind we support writes exact counts to disk. One reader per kind
behind one interface; a kind with no reader reports nothing rather than a
guess, because a wrong number here would be worse than no number.

Two properties every reader keeps:

- **Tail, never slurp.** These files are hot and large (36 MB, 67 MB and 80 MB
  on the machine this was written against). A reader seeks to the end and scans
  back over ``TAIL_BYTES``; it never parses the whole file, and it never holds
  more than that slice in memory.
- **Read-only.** SQLite is opened through an immutable URI so a live agent
  writing to its own database is never blocked or corrupted by us.

The numbers mean "what the next request will carry", not "what this session has
spent in total". Codex publishes both and the distinction is stark: on a live
session its cumulative ``total_token_usage`` was 494,529 against a 258,400
window, while the context it was actually carrying was 39,195.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

#: How much of the end of a transcript to read. One assistant record with its
#: usage block is a few hundred bytes; 256 KB is a wide margin for a long tool
#: result sitting between us and the last one.
TAIL_BYTES = 256 * 1024

#: Context windows we cannot read from the file itself. A model that is not
#: listed falls back to the smallest tier that fits what we observed, so a
#: missing entry understates the window rather than inventing headroom.
WINDOW_TIERS = (200_000, 400_000, 1_000_000)
DEFAULT_WINDOW = 200_000

MODEL_WINDOWS: Dict[str, int] = {
    "claude-opus-5": 200_000,
    "claude-sonnet-5": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-fable-5-1": 200_000,
}

KINDS = ("claude", "codex", "opencode")


@dataclass
class Reading:
    """One member's context, as read from its harness."""

    used: int
    window: int
    source: str  # the file we read it from
    model: Optional[str] = None
    at: Optional[str] = None  # timestamp carried by the record, when it has one

    @property
    def percent(self) -> float:
        if self.window <= 0:
            return 0.0
        return max(0.0, min(100.0, (float(self.used) / float(self.window)) * 100.0))

    def to_json(self) -> Dict[str, Any]:
        return {
            "used": int(self.used), "window": int(self.window),
            "percent": round(self.percent, 1), "model": self.model,
            "source": self.source, "at": self.at,
        }


def home_dir(env: Optional[Dict[str, str]] = None) -> Path:
    """The home directory this process should read, ``$HOME`` first.

    Every other path in the plugin is resolved through the environment rather
    than through the passwd database, and these readers must follow: a session
    running under a different HOME writes its transcripts there.
    """
    if env:
        value = env.get("HOME") or env.get("USERPROFILE")
        if value:
            return Path(value)
    return Path.home()


def window_for(model: Optional[str], used: int) -> int:
    """The context window for ``model``, widened to fit what we already saw.

    A session that is demonstrably carrying more than the table says is on a
    larger variant (Claude's 1M mode, for one), so the tier is raised rather
    than reporting an impossible number over 100%.
    """
    window = MODEL_WINDOWS.get(str(model or "").strip(), DEFAULT_WINDOW)
    if used <= window:
        return window
    for tier in WINDOW_TIERS:
        if used <= tier:
            return tier
    return used


def _tail_lines(path: Path, limit: int = TAIL_BYTES) -> List[str]:
    """The last whole lines of a file, newest last, without reading the rest."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    try:
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(size - limit)
                handle.readline()  # drop the partial line the seek landed inside
            raw = handle.read()
    except OSError:
        return []
    return raw.decode("utf-8", "replace").splitlines()


def _records(path: Path) -> Iterator[Dict[str, Any]]:
    """Parsed JSON objects from the tail of a JSONL file, newest first."""
    for line in reversed(_tail_lines(path)):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a half-written last line is normal on a live file
        if isinstance(record, dict):
            yield record


# --------------------------------------------------------------------------
# per kind


def read_claude(transcript: Optional[str]) -> Optional[Reading]:
    """The newest assistant message's usage in a Claude Code transcript.

    ``cache_read_input_tokens`` dominates and is the bulk of the context, so a
    reader that only looked at ``input_tokens`` would report near zero on a
    nearly full session.
    """
    if not transcript:
        return None
    path = Path(transcript)
    for record in _records(path):
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        used = 0
        for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and value > 0:
                used += value
        if used <= 0:
            continue
        model = message.get("model") if isinstance(message.get("model"), str) else None
        at = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
        return Reading(used=used, window=window_for(model, used), source=os.fspath(path), model=model, at=at)
    return None


def codex_rollout(session_value: Optional[str], home: Optional[Path] = None) -> Optional[Path]:
    """The rollout file whose name ends in this session id."""
    if not session_value:
        return None
    root = (home or Path.home()) / ".codex" / "sessions"
    if not root.is_dir():
        return None
    try:
        matches = sorted(root.glob("**/rollout-*-{}.jsonl".format(session_value)))
    except OSError:
        return None
    return matches[-1] if matches else None


def read_codex(session_value: Optional[str], home: Optional[Path] = None) -> Optional[Reading]:
    """The newest ``token_count`` event in a Codex rollout.

    Codex publishes the window itself, so nothing is assumed. ``last_token_usage``
    is the context; ``total_token_usage`` is cumulative for the session and
    routinely exceeds the window.
    """
    path = codex_rollout(session_value, home)
    if path is None:
        return None
    for record in _records(path):
        if record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        last = info.get("last_token_usage")
        total = info.get("total_token_usage")
        chosen = last if isinstance(last, dict) else total
        if not isinstance(chosen, dict):
            continue
        used = chosen.get("total_tokens")
        if not isinstance(used, int) or used <= 0:
            continue
        window = info.get("model_context_window")
        window = int(window) if isinstance(window, int) and window > 0 else window_for(None, used)
        at = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
        return Reading(used=used, window=window, source=os.fspath(path), at=at)
    return None


def opencode_db(home: Optional[Path] = None) -> Path:
    return (home or Path.home()) / ".local" / "share" / "opencode" / "opencode.db"


def read_opencode(session_value: Optional[str], home: Optional[Path] = None) -> Optional[Reading]:
    """The newest assistant message's ``tokens.total`` for this OpenCode session.

    The ``session`` row's own counters are lifetime totals, not context, so they
    are deliberately not used.
    """
    if not session_value:
        return None
    path = opencode_db(home)
    if not path.is_file():
        return None
    try:
        # immutable: never block or disturb an agent writing its own database
        conn = sqlite3.connect("file:{}?immutable=1".format(path), uri=True, timeout=1.0)
    except sqlite3.Error:
        return None
    try:
        conn.row_factory = None
        rows = conn.execute(
            "SELECT data FROM message WHERE session_id = ? ORDER BY time_created DESC LIMIT 8",
            (session_value,),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    for (blob,) in rows or []:
        if not isinstance(blob, (str, bytes)):
            continue
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("role") != "assistant":
            continue
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            continue
        used = tokens.get("total")
        if not isinstance(used, int) or used <= 0:
            continue
        model = data.get("modelID") if isinstance(data.get("modelID"), str) else None
        return Reading(used=used, window=window_for(model, used), source=os.fspath(path), model=model)
    return None


# --------------------------------------------------------------------------
# the one entry point


def read_member(kind: Optional[str], session: Any, pane_record: Optional[Dict[str, Any]] = None, home: Optional[Path] = None) -> Optional[Reading]:
    """One member's context reading, or None when this kind cannot be read.

    ``session`` is the member's recorded ``agent_session`` and ``pane_record``
    the ``panes/<terminal_id>.json`` document, which is where the Claude hook
    already writes the transcript path.
    """
    kind = str(kind or "").strip()
    value = session.get("value") if isinstance(session, dict) else None
    try:
        if kind == "claude":
            transcript = (pane_record or {}).get("transcript_path")
            reading = read_claude(transcript if isinstance(transcript, str) else None)
            if reading is None and value:
                reading = read_claude(_claude_transcript_by_id(str(value), home))
            return reading
        if kind == "codex":
            return read_codex(str(value) if value else None, home)
        if kind == "opencode":
            return read_opencode(str(value) if value else None, home)
    except (OSError, ValueError):
        return None
    return None


def _claude_transcript_by_id(session_value: str, home: Optional[Path] = None) -> Optional[str]:
    """Find a Claude transcript by session id when no hook recorded its path."""
    root = (home or Path.home()) / ".claude" / "projects"
    if not root.is_dir():
        return None
    try:
        matches = sorted(root.glob("*/{}.jsonl".format(session_value)))
    except OSError:
        return None
    return os.fspath(matches[-1]) if matches else None
