"""How full each member's context window is, read from the harness's own files.

The file-backed readers report native counts; Pi's optional Synapse extension
reports its native context estimate and active model limit in a private snapshot.
One reader per kind
behind one interface; a kind with no reader reports nothing rather than a
guess, because a wrong number here would be worse than no number.

Two properties every reader keeps:

- **Tail, never slurp.** These files are hot and large (36 MB, 67 MB and 80 MB
  on the machine this was written against). A reader seeks to the end and scans
  back over ``TAIL_BYTES``; it never parses the whole file, and it never holds
  more than that slice in memory.
- **Read-only.** SQLite is opened through a read-only URI so a live agent
  cannot be modified by an observability feature while committed WAL updates
  remain visible.

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
from typing import Any, Dict, Iterator, List, Optional, Tuple

#: How much of the end of a transcript to read. One assistant record with its
#: usage block is a few hundred bytes; 256 KB is a wide margin for a long tool
#: result sitting between us and the last one.
TAIL_BYTES = 256 * 1024

#: Claude does not write the window beside ``message.usage``. Keep the
#: families whose limits Anthropic publishes explicit: an unfamiliar future
#: model stays unknown instead of silently inheriting an obsolete default.
CLAUDE_1M_MODELS = (
    "claude-fable-5-1", "claude-fable-5", "claude-mythos-5-1", "claude-mythos-5",
    "claude-mythos-preview", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
    "claude-opus-4-6", "claude-sonnet-5", "claude-sonnet-4-6",
)
CLAUDE_200K_MODELS = (
    "claude-haiku-4-5", "claude-opus-4-5", "claude-opus-4-1", "claude-opus-4",
    "claude-sonnet-4-5", "claude-sonnet-4", "claude-3-7-sonnet",
    "claude-3-5-sonnet", "claude-3-5-haiku", "claude-3-opus", "claude-3-haiku",
)
CLAUDE_ALIAS_WINDOWS = {"fable": 1_000_000, "mythos": 1_000_000, "opus": 1_000_000,
                        "sonnet": 1_000_000, "haiku": 200_000}

#: ``~/.cache/opencode/models.json`` is several MB. Cache only the compact
#: provider/model -> context projection, invalidated by the file identity.
_OPENCODE_WINDOWS_CACHE: Dict[str, Tuple[int, int, Dict[Tuple[str, str], int]]] = {}

KINDS = ("claude", "codex", "opencode", "pi")


@dataclass
class Reading:
    """One member's context, as read from its harness."""

    used: Optional[int]
    window: Optional[int]
    source: str  # the file we read it from
    model: Optional[str] = None
    at: Optional[str] = None  # timestamp carried by the record, when it has one
    estimated: bool = False

    @property
    def percent(self) -> Optional[float]:
        if self.used is None or not isinstance(self.window, int) or self.window <= 0:
            return None
        return max(0.0, min(100.0, (float(self.used) / float(self.window)) * 100.0))

    def to_json(self) -> Dict[str, Any]:
        percent = self.percent
        result = {
            "used": int(self.used) if self.used is not None else None, "window": int(self.window) if isinstance(self.window, int) else None,
            "percent": round(percent, 1) if percent is not None else None, "model": self.model,
            "source": self.source, "at": self.at,
        }
        if self.estimated:
            result["estimated"] = True
        return result


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


def claude_window_for(model: Optional[str]) -> Optional[int]:
    """Published context size for a Claude model, or ``None`` when unknown."""
    value = str(model or "").strip().lower().rsplit("/", 1)[-1]
    if value in CLAUDE_ALIAS_WINDOWS:
        return CLAUDE_ALIAS_WINDOWS[value]
    for prefix in CLAUDE_1M_MODELS:
        if value == prefix or value.startswith(prefix + "-"):
            return 1_000_000
    for prefix in CLAUDE_200K_MODELS:
        if value == prefix or value.startswith(prefix + "-"):
            return 200_000
    return None


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


def read_claude(transcript: Optional[str], configured_model: Optional[str] = None) -> Optional[Reading]:
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
        return Reading(used=used, window=claude_window_for(model or configured_model),
                       source=os.fspath(path), model=model, at=at)
    return None


# --------------------------------------------------------------------------
# where each harness keeps its conversations
#
# ``env`` is optional so the context readers keep resolving from ``home``
# alone, exactly as before. Transcript search passes it and so also follows
# the harnesses' own relocation variables, the ones ``usage`` already honours
# for their login files: ``CLAUDE_CONFIG_DIR``, ``CODEX_HOME`` and
# ``XDG_DATA_HOME``.


def claude_projects_root(home: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> Path:
    """``<claude config>/projects``: one directory per project, one JSONL per session."""
    configured = (env or {}).get("CLAUDE_CONFIG_DIR")
    return (Path(configured) if configured else (home or Path.home()) / ".claude") / "projects"


def codex_home(home: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> Path:
    configured = (env or {}).get("CODEX_HOME")
    return Path(configured) if configured else (home or Path.home()) / ".codex"


def codex_rollout(session_value: Optional[str], home: Optional[Path] = None,
                  env: Optional[Dict[str, str]] = None, archived: bool = False) -> Optional[Path]:
    """The rollout file whose name ends in this session id.

    ``archived`` also looks in ``archived_sessions/``, where Codex moves a
    conversation the user archived; only search asks for that.
    """
    if not session_value:
        return None
    base = codex_home(home, env)
    found = _codex_rollout_in(base / "sessions", session_value)
    if found is None and archived:
        found = _codex_rollout_in(base / "archived_sessions", session_value)
    return found


def _codex_rollout_in(root: Path, session_value: str) -> Optional[Path]:
    if not root.is_dir():
        return None
    try:
        matches = sorted(root.glob("**/rollout-*-{}.jsonl".format(session_value)))
    except OSError:
        return None
    return matches[-1] if matches else None


def read_codex(session_value: Optional[str], home: Optional[Path] = None) -> Optional[Reading]:
    """The newest ``token_count`` event and active model in a Codex rollout.

    Codex publishes the window itself, so nothing is assumed. ``last_token_usage``
    is the context; ``total_token_usage`` is cumulative for the session and
    routinely exceeds the window. The model is published separately on
    ``turn_context`` and ``thread_settings_applied`` records, so keep scanning
    the same bounded tail after finding the token count instead of returning
    early and losing which model owns that window.
    """
    path = codex_rollout(session_value, home)
    if path is None:
        return None
    model: Optional[str] = None
    reading: Optional[Tuple[int, Optional[int], Optional[str]]] = None
    for record in _records(path):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue

        if model is None:
            candidate: Any = None
            if record.get("type") == "turn_context":
                candidate = payload.get("model")
            elif record.get("type") == "event_msg" and payload.get("type") == "thread_settings_applied":
                settings = payload.get("thread_settings")
                candidate = settings.get("model") if isinstance(settings, dict) else None
            if isinstance(candidate, str) and candidate.strip():
                model = candidate.strip()

        if reading is not None or record.get("type") != "event_msg" or payload.get("type") != "token_count":
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
        window = int(window) if isinstance(window, int) and not isinstance(window, bool) and window > 0 else None
        at = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
        reading = (used, window, at)
        if model is not None:
            break
    if reading is None:
        return None
    used, window, at = reading
    return Reading(used=used, window=window, source=os.fspath(path), model=model, at=at)


def opencode_db(home: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> Path:
    data_home = (env or {}).get("XDG_DATA_HOME")
    return (Path(data_home) if data_home else (home or Path.home()) / ".local" / "share") / "opencode" / "opencode.db"


def connect_readonly(path: Path, timeout: float = 1.0) -> sqlite3.Connection:
    """A read-only connection to a live harness database (raises ``sqlite3.Error``).

    Read-only mode participates in SQLite's WAL snapshot, so it sees the live
    agent's committed messages without ever writing. ``immutable`` looks
    attractive here but deliberately ignores WAL updates, leaving readings
    stale until the harness checkpoints.
    """
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout)


def opencode_models(home: Optional[Path] = None) -> Path:
    return (home or Path.home()) / ".cache" / "opencode" / "models.json"


def _opencode_windows(home: Optional[Path] = None) -> Dict[Tuple[str, str], int]:
    """OpenCode's resolved model catalogue, projected and cached by file identity."""
    path = opencode_models(home)
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = os.fspath(path)
    cached = _OPENCODE_WINDOWS_CACHE.get(key)
    if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    windows: Dict[Tuple[str, str], int] = {}
    if isinstance(raw, dict):
        for provider_key, provider in raw.items():
            if not isinstance(provider, dict):
                continue
            provider_id = provider.get("id") if isinstance(provider.get("id"), str) else provider_key
            models = provider.get("models")
            if not isinstance(provider_id, str) or not isinstance(models, dict):
                continue
            for model_key, model in models.items():
                if not isinstance(model, dict):
                    continue
                limit = model.get("limit")
                window = limit.get("context") if isinstance(limit, dict) else None
                if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
                    continue
                model_id = model.get("id") if isinstance(model.get("id"), str) else model_key
                if isinstance(model_key, str):
                    windows[(provider_id.lower(), model_key.lower())] = window
                if isinstance(model_id, str):
                    windows[(provider_id.lower(), model_id.lower())] = window
    _OPENCODE_WINDOWS_CACHE[key] = (stat.st_mtime_ns, stat.st_size, windows)
    return windows


def opencode_window_for(provider: Optional[str], model: Optional[str], configured_model: Optional[str] = None,
                        home: Optional[Path] = None) -> Optional[int]:
    """Resolve the model's context from OpenCode's own local catalogue."""
    observed = str(model or "").strip().lower()
    provider_id = str(provider or "").strip().lower()
    if "/" in observed:
        embedded_provider, observed = observed.rsplit("/", 1)
        provider_id = provider_id or embedded_provider

    configured = str(configured_model or "").strip().lower()
    configured_provider = ""
    configured_id = configured
    if "/" in configured:
        configured_provider, configured_id = configured.rsplit("/", 1)
    if not observed:
        observed = configured_id
    if not provider_id and configured_provider and configured_id == observed:
        provider_id = configured_provider
    if not observed:
        return None

    windows = _opencode_windows(home)
    if provider_id:
        return windows.get((provider_id, observed))
    candidates = {window for (candidate_provider, candidate_model), window in windows.items()
                  if candidate_provider and candidate_model == observed}
    return next(iter(candidates)) if len(candidates) == 1 else None


def read_opencode(session_value: Optional[str], home: Optional[Path] = None,
                  configured_model: Optional[str] = None) -> Optional[Reading]:
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
        conn = connect_readonly(path)
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
        provider = data.get("providerID") if isinstance(data.get("providerID"), str) else None
        return Reading(used=used, window=opencode_window_for(provider, model, configured_model, home),
                       source=os.fspath(path), model=model)
    return None


# --------------------------------------------------------------------------
# the one entry point


def read_member(kind: Optional[str], session: Any, pane_record: Optional[Dict[str, Any]] = None,
                home: Optional[Path] = None, configured_model: Optional[str] = None) -> Optional[Reading]:
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
            reading = read_claude(transcript if isinstance(transcript, str) else None, configured_model)
            if reading is None and value:
                reading = read_claude(_claude_transcript_by_id(str(value), home), configured_model)
            return reading
        if kind == "codex":
            return read_codex(str(value) if value else None, home)
        if kind == "opencode":
            return read_opencode(str(value) if value else None, home, configured_model)
    except (OSError, ValueError):
        return None
    return None


def _claude_transcript_by_id(session_value: str, home: Optional[Path] = None,
                             env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Find a Claude transcript by session id when no hook recorded its path."""
    root = claude_projects_root(home, env)
    if not root.is_dir():
        return None
    try:
        matches = sorted(root.glob("*/{}.jsonl".format(session_value)))
    except OSError:
        return None
    return os.fspath(matches[-1]) if matches else None


claude_transcript_by_id = _claude_transcript_by_id
