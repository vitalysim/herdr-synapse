"""Watch: what an agent is doing, read from what its harness already writes (0.21).

An activity card (``.local/prd/canvas-contracts.md`` section 14) is one
agent's terminal title, lifecycle state, plan, recent tool calls, files
touched, context, and last prompt. Nothing is asked of the agent and no model
is called: the title and state come from the ``agent.list`` row Herdr already
keeps, everything else from the harness's own conversation store, located
through the same readers ``context`` and ``transcripts`` use (Claude
transcripts, Codex rollout logs, OpenCode's ``opencode.db``, Pi's session
file). Four properties hold for every reader:

- **Only names and short arguments.** A tool call becomes its name and one
  short argument (the command's first line, the path, the search pattern, the
  domain); a tool's output and a file's contents are never read into a card.
- **Redacted.** Every string passes the board's secret patterns first and
  shows ``[redacted:<kind>]`` instead.
- **Incremental and bounded.** A JSONL reader keeps a byte offset per agent,
  the OpenCode reader a row cursor, both in ``<session>/watch-cache/``; one
  refresh reads at most ``MAX_READ_BYTES``, and the first look at an agent at
  most ``FIRST_READ_BYTES``. When more than that was written since the last
  look, the newest part wins: a card is about now. The budgets are sized for
  transcripts that carry images and attachments in single lines of 100 KB and
  more (live, 2026-09-26): a 64 KB window held only the last two actions.
- **Unknown, never an error.** A store that is missing, unreadable, or in a
  shape this module does not recognise leaves ``reader_error: "unknown"``
  and a card that still carries the title and the state.

It also owns ``<session>/watch.json`` (the agents the operator flagged, keyed
by terminal id so a pane move keeps the flag) and the sidebar ``team_doing``
token the notifier stamps for them while the whiteboard layer is on.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit

from herdr_team import context as _context
from herdr_team import roster as _roster
from herdr_team import sanitize as _sanitize
from herdr_team import store
from herdr_team import transcripts as _transcripts
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError
from herdr_team.paths import TERMINAL_ID_RE, SessionPaths, ensure_dir

WATCH_FILE = "watch.json"
WATCH_SCHEMA = 1
WATCH_CACHE_DIR = "watch-cache"
CACHE_SCHEMA = 1
DOING_TOKEN = "team_doing"
DOING_SOURCE = "herdr-synapse:watch"
DOING_TTL_MS = 60000
DOING_REFRESH_S = 15.0
DOING_MAX_COLUMNS = 32
MAX_WATCHED = 20
MAX_READ_BYTES = 512 * 1024
#: The first read of an agent, which has no offset yet and must reach back far
#: enough to fill a card's recent actions past image-sized transcript lines.
FIRST_READ_BYTES = 1024 * 1024
MAX_ACTIONS = 10
MAX_FILES = 20
LAST_PROMPT_CHARS = 160
STATES = ("working", "idle", "done", "blocked", "unknown", "not_running")
ACTION_ICONS = ("run", "edit", "read", "search", "web", "agent", "mcp", "tool")
#: Kinds with a reader; any other kind gets a card with its title and state only.
READERS = ("claude", "codex", "opencode", "pi")
PLAN_SOURCES = ("todo", "update_plan", "opencode_todo")
STEP_STATUSES = ("pending", "in_progress", "completed", "cancelled")
HEADLINE_CHARS = 120
ACTION_CHARS = 120
PATH_CHARS = 240
MAX_PLAN_STEPS = 50
#: A long argument is cut to this before the secret patterns run; what is shown is far shorter.
SCAN_CHARS = 4000
#: Cache files of agents nobody watches any more are removed after a day.
CACHE_MAX_AGE_S = 86400.0
#: A conversation file that could not be found is looked for again after this long, not on every
#: refresh: finding a Codex rollout walks every day directory.
LOOKUP_RETRY_S = 60.0
#: OpenCode rows looked at per refresh at most (the byte budget usually stops earlier).
OPENCODE_MAX_ROWS = 400
MAX_TASK_IDS = 200

_WHITESPACE_RE = re.compile(r"\s+")
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M)
_CLAUDE_COMMAND_RE = re.compile(r"<command-name>\s*(.*?)\s*</command-name>(?:.*?<command-args>\s*(.*?)\s*</command-args>)?", re.S)
_BASH_INPUT_RE = re.compile(r"^<bash-input>(.*?)</bash-input>", re.S)
#: Claude writes these into user turns itself: command output, reminders, interruptions.
_CLAUDE_NOT_PROMPTS = ("<local-command-", "<system-reminder>", "Caveat:", "[Request interrupted", "<bash-stdout>", "<bash-stderr>")
_CODEX_NOT_PROMPTS = _transcripts.CODEX_INJECTED_PREFIXES + ("<turn_aborted>", "<user_shell_command>", "<INSTRUCTIONS>")
_SHELLS = ("bash", "sh", "zsh", "dash", "fish", "ksh", "pwsh", "powershell")

_RUN_TOOLS = frozenset(("bash", "shell", "shell_command", "exec_command", "local_shell", "exec", "run_shell_command",
                        "run_terminal_cmd", "terminal", "powershell"))
_EDIT_TOOLS = frozenset(("edit", "write", "multiedit", "notebookedit", "apply_patch", "patch", "str_replace_editor",
                         "str_replace_based_edit_tool", "create_file", "edit_file", "write_file", "replace"))
_READ_TOOLS = frozenset(("read", "view", "cat", "read_file", "ls", "list", "notebookread", "view_image", "list_dir",
                         "list_directory", "read_many_files"))
_SEARCH_TOOLS = frozenset(("grep", "glob", "find", "search", "codesearch", "file_search", "grep_search",
                           "search_file_content", "search_files"))
_WEB_TOOLS = frozenset(("webfetch", "websearch", "web_search", "web_fetch", "fetch", "google_web_search"))
_AGENT_TOOLS = frozenset(("task", "agent", "subagent", "spawn_agent"))
#: Tools that only read the plan back: nothing to show.
_PLAN_READS = frozenset(("todoread", "tasklist", "taskget"))
_PATH_KEYS = ("file_path", "filePath", "path", "notebook_path", "target_file", "absolute_path", "file", "filename")
_COMMAND_KEYS = ("command", "cmd", "script")
_PATTERN_KEYS = ("pattern", "query", "regex", "q")
_STATUS_ALIASES = {"done": "completed", "complete": "completed", "finished": "completed", "active": "in_progress",
                   "running": "in_progress", "doing": "in_progress", "in-progress": "in_progress", "todo": "pending",
                   "not_started": "pending", "canceled": "cancelled", "skipped": "cancelled"}

#: Card keys that come from the reader and survive in the cache.
_ACTIVITY_KEYS = ("plan", "tasks", "actions", "files", "last_prompt", "context", "error", "note",
                  "path", "file", "offset", "mid_line", "db", "order", "cursor", "cursor_seen", "missing_at")

_UNSET: Any = object()


# --------------------------------------------------------------------------
# text


#: Key shapes the board's patterns do not know, and shorter forms of ones it does.
_KEY_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("github_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("gitlab_token", re.compile(r"\bgl(?:pat|dt|ptt|rt)-[A-Za-z0-9_-]{20,}")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}")),
    ("stripe_key", re.compile(r"\bwhsec_[A-Za-z0-9]{20,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
)
_SECRET_NAME = r"(?:passw(?:or)?d|passphrase|_pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|credentials?|auth[_-]?key)"
#: ``PGPASSWORD=…``, ``MYSQL_PWD=…``, ``--password=…``, ``client_secret: …``: the name stays, the value goes.
_ASSIGNED_RE = re.compile(r"(?i)(\b[A-Za-z0-9_.-]*?" + _SECRET_NAME + r"[A-Za-z0-9_.-]*\s*[:=]\s*['\"]?)([^\s'\"&|;,)]{4,})")
#: ``--password VALUE``, ``--api-key VALUE``, ``--with-token VALUE``.
_FLAG_RE = re.compile(r"(?i)((?:^|\s)--?[a-z0-9-]*?(?:passw(?:or)?d|passphrase|secret|token|api-?key|access-?key|private-?key|credentials?|auth-?key)"
                      r"[a-z0-9-]*\s+['\"]?)([^\s'\"-][^\s'\"]*)")
#: MySQL's attached ``-pSECRET``.
_MYSQL_RE = re.compile(r"((?<![\w-])(?i:mysql|mysqldump|mysqladmin|mysqlimport|mysqlsh|mariadb|mariadb-dump)\b[^|;&\n]*?\s-p)([^\s'\"]+)")
#: ``curl -u user:pass``, ``--user user:pass``, ``-uuser:pass``.
_USER_PASS_RE = re.compile(r"((?:^|\s)(?:-u|--user|--proxy-user)(?:\s+|=)?['\"]?[^\s:'\"]+:)([^\s'\"@]+)")
#: ``scheme://user:pass@host`` and ``scheme://TOKEN@host``.
_URL_PASS_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s/@:'\"]+:)([^\s/@'\"]+)(?=@)")
_URL_TOKEN_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)([A-Za-z0-9_-]{20,})(?=@)")
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization:\s*(?:(?:basic|bearer|token|digest|negotiate)\s+)?)([^\s'\"]+)")


def _value_redactor(label: str) -> Callable[["re.Match[str]"], str]:
    def sub(match: "re.Match[str]") -> str:
        value = match.group(2)
        if value.startswith("[redacted:") or (value.isdigit() and len(value) < 8):
            return match.group(0)  # already gone, or a count such as max_tokens=4000
        return match.group(1) + "[redacted:{}]".format(label)
    return sub


_VALUE_PATTERNS: Tuple[Tuple["re.Pattern[str]", Callable[["re.Match[str]"], str]], ...] = (
    (_URL_TOKEN_RE, _value_redactor("token")),
    (_URL_PASS_RE, _value_redactor("password")),
    (_AUTH_HEADER_RE, _value_redactor("auth")),
    (_USER_PASS_RE, _value_redactor("password")),
    (_MYSQL_RE, _value_redactor("password")),
    (_ASSIGNED_RE, _value_redactor("secret")),
    (_FLAG_RE, _value_redactor("secret")),
)


def redact(text: str) -> str:
    """``text`` with every secret shape the board knows replaced by ``[redacted:<kind>]``.

    The board's own patterns (``transcripts.redact``, which also covers a
    whole private key block) first, then the stricter shapes of
    ``sanitize.SECRET_PATTERNS`` so a shorter key form is caught as well,
    then the shapes a command line carries that the board never sees: more
    key prefixes, ``NAME_PASSWORD=value`` assignments, password flags,
    ``user:pass`` and credentials in a URL, an ``Authorization`` header.
    Over-redacting a card costs a word; under-redacting copies a secret into
    a sidebar token every agent in the session can read.
    """
    text = _transcripts.redact(text)
    for label, pattern in _sanitize.SECRET_PATTERNS.items():
        text = pattern.sub("[redacted:{}]".format(label), text)
    for label, pattern in _KEY_PATTERNS:
        text = pattern.sub("[redacted:{}]".format(label), text)
    for pattern, sub in _VALUE_PATTERNS:
        text = pattern.sub(sub, text)
    return text


def _line(value: Any, limit: int = ACTION_CHARS) -> str:
    """One display line: escapes and controls stripped, whitespace collapsed, secrets redacted, then cut."""
    if not isinstance(value, str) or not value:
        return ""
    text = _sanitize.replace_format_chars(_sanitize.strip_controls(value[:SCAN_CHARS]), "")
    text = redact(_WHITESPACE_RE.sub(" ", text).strip())
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def _first_line(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    for line in value.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _iso(value: Any) -> Optional[str]:
    return _transcripts.iso_of(_transcripts.epoch_of(value)) if value is not None else None


def _basename(path: str) -> str:
    head, _, tail = path.partition(" +")
    name = head.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1] or head
    return name + (" +" + tail if tail else "")


# --------------------------------------------------------------------------
# watch.json


def watch_path(session: SessionPaths) -> Path:
    """``<session>/watch.json``."""
    return session.root / WATCH_FILE


def _field(value: Any, limit: int) -> Optional[str]:
    return value[:limit] if isinstance(value, str) and value else None


def _entry(item: Any) -> Optional[Dict[str, Any]]:
    """A normalised ``watch.json`` row, or None when it names no usable terminal."""
    if not isinstance(item, dict):
        return None
    terminal = item.get("terminal_id")
    if not isinstance(terminal, str) or not TERMINAL_ID_RE.match(terminal):
        return None
    return {
        "terminal_id": terminal, "pane_id": _field(item.get("pane_id"), 64), "name": _field(item.get("name"), 64),
        "kind": _field(item.get("kind"), 32), "team": _field(item.get("team"), 64),
        "by": _field(item.get("by"), 64), "at": _field(item.get("at"), 40),
    }


def watched(session: SessionPaths) -> List[Dict[str, Any]]:
    """The watched agents (``watch.json`` ``agents``), oldest first."""
    raw = store.read_json(watch_path(session), default=None)
    items = raw.get("agents") if isinstance(raw, dict) else None
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for item in items if isinstance(items, list) else []:
        entry = _entry(item)
        if entry is not None and entry["terminal_id"] not in seen:
            seen.add(entry["terminal_id"])
            out.append(entry)
    return out


def _save(session: SessionPaths, agents: List[Dict[str, Any]]) -> None:
    store.write_json(watch_path(session), {"v": WATCH_SCHEMA, "agents": agents})


def is_watched(session: SessionPaths, terminal_id: str) -> bool:
    """Whether the terminal is flagged."""
    return any(entry["terminal_id"] == terminal_id for entry in watched(session))


def add_watch(session: SessionPaths, entry: Dict[str, Any], by: str) -> Dict[str, Any]:
    """Flag an agent (keyed by ``terminal_id``); the stored entry. ``watch_limit`` past ``MAX_WATCHED``.

    Flagging an agent that is already flagged refreshes its pane, name, kind
    and team and keeps who flagged it first, and when.
    """
    new = _entry(dict(entry, by=by, at=store.now_iso()))
    if new is None:
        raise HerdrTeamError("terminal_id_invalid", "an agent is watched by its terminal id, and this one is missing or not file-name safe",
                             EXIT_REFUSED, {"terminal_id": entry.get("terminal_id")})
    agents = watched(session)
    for index, current in enumerate(agents):
        if current["terminal_id"] != new["terminal_id"]:
            continue
        merged = dict(current)
        for key in ("pane_id", "name", "kind"):
            if new[key] is not None:
                merged[key] = new[key]
        if "team" in entry:
            merged["team"] = new["team"]
        if merged != current:
            agents[index] = merged
            _save(session, agents)
        return merged
    if len(agents) >= MAX_WATCHED:
        raise HerdrTeamError(
            "watch_limit", "at most {} agents can be watched at once; drop one first: herdr-synapse unwatch <target>".format(MAX_WATCHED),
            EXIT_REFUSED, {"limit": "watched", "max": MAX_WATCHED, "watched": len(agents)})
    agents.append(new)
    _save(session, agents)
    return new


def remove_watch(session: SessionPaths, terminal_id: str) -> Optional[Dict[str, Any]]:
    """Drop a flag; the removed entry, or None when it was not watched. Its read cache goes too."""
    agents = watched(session)
    removed = next((entry for entry in agents if entry["terminal_id"] == terminal_id), None)
    if removed is None:
        return None
    _save(session, [entry for entry in agents if entry["terminal_id"] != terminal_id])
    remove_cache(session, terminal_id)
    return removed


def sync_watch(session: SessionPaths, rows: Mapping[str, Mapping[str, Any]]) -> bool:
    """Refresh each flag's pane, name and kind from live ``agent.list`` rows by terminal id; True when written.

    Only a change is written, so a quiet session costs one small read.
    """
    agents = watched(session)
    changed = False
    for entry in agents:
        row = rows.get(entry["terminal_id"])
        if not isinstance(row, Mapping):
            continue
        for key, live in (("pane_id", row.get("pane_id")), ("name", row.get("name")), ("kind", row.get("agent"))):
            value = _field(live, 64)
            if value is not None and value != entry.get(key):
                entry[key] = value
                changed = True
    if changed:
        _save(session, agents)
    return changed


# --------------------------------------------------------------------------
# the read cache (<session>/watch-cache/<terminal_id>.json)


def cache_path(session: SessionPaths, terminal_id: Any) -> Optional[Path]:
    """Where one terminal's reader offsets and last activity live; None for an unsafe id."""
    if not isinstance(terminal_id, str) or not TERMINAL_ID_RE.match(terminal_id):
        return None
    return session.root / WATCH_CACHE_DIR / (terminal_id + ".json")


def remove_cache(session: SessionPaths, terminal_id: str) -> bool:
    path = cache_path(session, terminal_id)
    if path is None:
        return False
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def prune_cache(session: SessionPaths, keep: Iterable[str], max_age_s: float = CACHE_MAX_AGE_S, now: Optional[float] = None) -> int:
    """Remove cache files older than ``max_age_s`` whose terminal is not in ``keep``; how many went."""
    root = session.root / WATCH_CACHE_DIR
    wanted = set(keep)
    now = time.time() if now is None else now
    removed = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        if not name.endswith(".json") or name[:-5] in wanted:
            continue
        path = root / name
        try:
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode) and now - info.st_mtime > max_age_s:
                os.unlink(path)
                removed += 1
        except OSError:
            continue
    return removed


def _load_cache(path: Optional[Path]) -> Dict[str, Any]:
    raw = store.read_json(path, default=None) if path is not None else None
    return dict(raw) if isinstance(raw, dict) and raw.get("v") == CACHE_SCHEMA else {"v": CACHE_SCHEMA}


def _save_cache(path: Optional[Path], state: Dict[str, Any], before: Dict[str, Any]) -> None:
    if path is None or state == before:
        return
    try:
        ensure_dir(path.parent)
        store.write_json(path, state, fsync=False)  # ephemeral: a lost write costs one re-read
    except (OSError, HerdrTeamError):
        pass


def _reset(state: Dict[str, Any]) -> None:
    for key in _ACTIVITY_KEYS:
        state.pop(key, None)


# --------------------------------------------------------------------------
# one tool call


def _first(args: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list) and value:
            return value
    return None


def _command_text(value: Any) -> str:
    """The script a command runs: ``bash -lc SCRIPT`` is ``SCRIPT``, an argv list is joined."""
    if isinstance(value, list):
        parts = [part for part in value if isinstance(part, str)]
        flag = parts[1] if len(parts) >= 3 else ""
        if flag[:1] == "-" and flag[1:] and set(flag[1:]) <= set("lic") and os.path.basename(parts[0]).lower() in _SHELLS:
            return parts[2]
        return " ".join(parts)
    return value if isinstance(value, str) else ""


def patch_files(text: Any) -> List[str]:
    """The files an ``apply_patch`` envelope adds, updates, deletes or moves to, in order."""
    if not isinstance(text, str):
        return []
    out: List[str] = []
    for match in _PATCH_FILE_RE.finditer(text[: MAX_READ_BYTES]):
        path = (match.group(1) or match.group(2) or "").strip()
        if path and path not in out:
            out.append(path)
    return out


def _edit_files(lname: str, args: Mapping[str, Any]) -> List[str]:
    if lname in ("apply_patch", "patch"):
        return patch_files(_first(args, ("input", "patch", "patchText", "patch_text")))
    path = _first(args, _PATH_KEYS)
    return [path] if isinstance(path, str) else []


def _host(url: Any) -> str:
    if not isinstance(url, str):
        return ""
    try:
        host = urlsplit(url.strip()).hostname  # never the user:password@ part
    except ValueError:
        return ""
    return host or ""


def describe_tool(name: Any, args: Any) -> Optional[Tuple[str, str, List[str]]]:
    """``(icon, text, files touched)`` for one tool call; its name and one short argument, never its output."""
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    lname = name.lower()
    args = args if isinstance(args, Mapping) else {}
    if "__" in name:  # ``mcp__server__tool`` (Claude), ``server__tool`` (Codex)
        parts = [part for part in name.split("__") if part]
        if parts and parts[0].lower() == "mcp":
            parts = parts[1:]
        return "mcp", _line(" ".join(parts) or name), []
    if lname in _RUN_TOOLS:
        script = _command_text(_first(args, _COMMAND_KEYS))
        stripped = script.lstrip()
        if stripped.startswith("apply_patch") or "*** Begin Patch" in script[:SCAN_CHARS]:
            files = patch_files(script)
            if files:
                return "edit", _line(files[0] + (" +{}".format(len(files) - 1) if len(files) > 1 else "")), [_line(f, PATH_CHARS) for f in files]
        return "run", _line(_first_line(script)) or _line(name), []
    if lname in _EDIT_TOOLS:
        files = [f for f in (_line(path, PATH_CHARS) for path in _edit_files(lname, args)) if f]
        if not files:
            return "edit", _line(name), []
        return "edit", _line(files[0] + (" +{}".format(len(files) - 1) if len(files) > 1 else "")), files
    if lname in _READ_TOOLS:
        return "read", _line(_first(args, _PATH_KEYS)) or _line(name), []
    if lname in _SEARCH_TOOLS:
        pattern = _first(args, _PATTERN_KEYS)
        where = _first(args, ("path", "include", "glob", "directory"))
        text = '"{}"'.format(pattern) if isinstance(pattern, str) else ""
        if isinstance(where, str) and where != pattern:
            text = "{} in {}".format(text, where) if text else where
        return "search", _line(text) or _line(name), []
    if lname in _WEB_TOOLS:
        host = _host(args.get("url"))
        query = _first(args, ("query", "q"))
        return "web", _line(host) or _line(query) or _line(name), []
    if lname in _AGENT_TOOLS:
        kind = _first(args, ("subagent_type", "agent_type", "agent"))
        about = _first(args, ("description",))
        text = " ".join(part for part in (kind, about) if isinstance(part, str))
        return "agent", _line(text) or _line(name), []
    return "tool", _line(name), []


# --------------------------------------------------------------------------
# plans


def _status(value: Any) -> str:
    text = str(value or "").strip().lower().replace(" ", "_")
    text = _STATUS_ALIASES.get(text, text)
    return text if text in STEP_STATUSES else "pending"


def todo_steps(items: Any) -> List[Dict[str, str]]:
    """Steps from a to-do list in any harness's shape: ``content``/``step``/``subject``/``text`` and ``status``."""
    steps: List[Dict[str, str]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping):
            continue
        text = _first(item, ("content", "step", "subject", "text", "title", "description"))
        if isinstance(text, str):
            steps.append({"text": _line(text), "status": _status(item.get("status"))})
    return steps


def make_plan(steps: List[Dict[str, str]], source: str) -> Optional[Dict[str, Any]]:
    """``{"steps", "current", "total", "source"}``: ``current`` is the 1-based step in progress, else None."""
    if not steps:
        return None
    shown = steps[:MAX_PLAN_STEPS]
    current = next((index + 1 for index, step in enumerate(shown) if step.get("status") == "in_progress"), None)
    return {"steps": shown, "current": current, "total": len(steps), "source": source}


# --------------------------------------------------------------------------
# what one read adds


class _Activity:
    """Folds records, oldest first, into an agent's cached activity (``state``, the cache document)."""

    def __init__(self, state: Dict[str, Any], plan_source: str, configured_model: Optional[str] = None) -> None:
        self.state = state
        self.plan_source = plan_source
        self.configured_model = configured_model

    def action(self, key: str, icon: str, text: str, at: Optional[str], files: Sequence[str] = ()) -> None:
        actions = [a for a in self.state.get("actions") or [] if isinstance(a, dict) and a.get("id") != key]
        actions.insert(0, {"id": key, "icon": icon, "text": text, "at": at})
        self.state["actions"] = actions[:MAX_ACTIONS]
        if files:
            current = [f for f in self.state.get("files") or [] if isinstance(f, str)]
            for path in files:
                current = [f for f in current if f != path]
                current.insert(0, path)
            self.state["files"] = current[:MAX_FILES]

    def prompt(self, text: Any) -> None:
        line = _line(text, LAST_PROMPT_CHARS)
        if line:
            self.state["last_prompt"] = line

    def usage(self, used: Any, window: Any) -> None:
        if not isinstance(used, int) or isinstance(used, bool) or used <= 0:
            return
        window = window if isinstance(window, int) and not isinstance(window, bool) and window > 0 else None
        percent = round(max(0.0, min(100.0, used * 100.0 / window)), 1) if window else None
        self.state["context"] = {"percent": percent, "used": used, "window": window}

    def plan(self, steps: List[Dict[str, str]], source: str) -> None:
        self.state["plan"] = make_plan(steps, source)

    def tool(self, key: str, name: Any, args: Any, at: Optional[str]) -> None:
        if not isinstance(name, str) or not name.strip():
            return
        lname = name.strip().lower()
        args = args if isinstance(args, Mapping) else {}
        if lname == "todowrite":
            self.plan(todo_steps(args.get("todos")), self.plan_source)
            return
        if lname == "update_plan":
            self.plan(todo_steps(args.get("plan")), "update_plan")
            return
        if lname in ("taskcreate", "taskupdate"):
            self._task(key, lname, args)
            return
        if lname in _PLAN_READS:
            return
        shown = describe_tool(name, args)
        if shown is not None:
            icon, text, files = shown
            self.action(key, icon, text, at, files)

    def _task(self, key: str, lname: str, args: Mapping[str, Any]) -> None:
        """Claude's task tools: ``TaskCreate`` numbers tasks 1, 2, 3 in order; ``TaskUpdate`` names one by ``taskId``.

        The number is inferred from the order of creation (the tool's own
        answer is output, which is never read), so it is best effort; each
        call is counted once however often its line is read.
        """
        tasks = self.state.get("tasks") if isinstance(self.state.get("tasks"), dict) else {}
        items = [list(item) for item in tasks.get("items") or [] if isinstance(item, list) and len(item) == 3]
        seen = [s for s in tasks.get("seen") or [] if isinstance(s, str)]
        if key in seen:
            return
        seen.append(key)
        if lname == "taskcreate":
            subject = _first(args, ("subject", "content", "description"))
            if isinstance(subject, str):
                number = tasks["next"] if isinstance(tasks.get("next"), int) and tasks["next"] > 0 else 1
                items.append([str(number), _line(subject), "pending"])
                tasks["next"] = number + 1
        else:
            task_id = str(args.get("taskId") or args.get("task_id") or args.get("id") or "")
            for item in items:
                if item[0] == task_id:
                    if isinstance(args.get("subject"), str):
                        item[1] = _line(args["subject"])
                    if args.get("status") is not None:
                        item[2] = "deleted" if str(args["status"]).lower() == "deleted" else _status(args["status"])
            items = [item for item in items if item[2] != "deleted"]
        tasks.update(items=items, seen=seen[-MAX_TASK_IDS:])
        self.state["tasks"] = tasks
        self.plan([{"text": item[1], "status": item[2]} for item in items], "todo")


# --------------------------------------------------------------------------
# per-harness records (each returns True when the record had the harness's envelope)


def _text_blocks(content: Any, types: Sequence[str] = ("text",)) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") in types and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _claude_prompt(text: str) -> Optional[str]:
    stripped = text.lstrip()
    if not stripped or stripped.startswith(_CLAUDE_NOT_PROMPTS):
        return None
    if stripped.startswith("<command-"):
        match = _CLAUDE_COMMAND_RE.search(stripped)
        return " ".join(part for part in (match.group(1), match.group(2)) if part) if match else None
    shell = _BASH_INPUT_RE.match(stripped)
    if shell:
        return "! " + shell.group(1)
    return text


def claude_record(record: Dict[str, Any], activity: _Activity, offset: int) -> bool:
    """One Claude Code transcript record: user prompts, ``tool_use`` blocks, the newest usage."""
    kind = record.get("type")
    if not isinstance(kind, str):
        return False
    if kind not in ("user", "assistant"):
        return True  # summaries, snapshots, system notes: known shape, nothing to show
    message = record.get("message")
    if not isinstance(message, dict):
        return False
    if record.get("isSidechain") is True:
        return True  # a subagent's own turn, not this agent's
    content = message.get("content")
    if kind == "user":
        if record.get("isMeta") or record.get("isCompactSummary"):
            return True
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return True  # a tool's answer, not a prompt
        prompt = _claude_prompt(_text_blocks(content))
        if prompt:
            activity.prompt(prompt)
        return True
    at = _iso(record.get("timestamp"))
    for index, block in enumerate(content if isinstance(content, list) else []):
        if isinstance(block, dict) and block.get("type") == "tool_use":
            key = block.get("id") if isinstance(block.get("id"), str) and block.get("id") else "{}:{}".format(offset, index)
            activity.tool(key, block.get("name"), block.get("input"), at)
    usage = message.get("usage")
    if isinstance(usage, dict):
        used = sum(v for v in (usage.get(k) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
                   if isinstance(v, int) and not isinstance(v, bool) and v > 0)
        model = message.get("model") if isinstance(message.get("model"), str) else None
        if used > 0:
            activity.usage(used, _context.claude_window_for(model or activity.configured_model))
    return True


def _json_args(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in ("{", "["):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value


def codex_record(record: Dict[str, Any], activity: _Activity, offset: int) -> bool:
    """One Codex rollout record: ``response_item`` calls and user messages, ``token_count`` events."""
    kind = record.get("type")
    payload = record.get("payload")
    if not isinstance(kind, str) or not isinstance(payload, dict):
        return False
    if kind == "event_msg":
        if payload.get("type") == "token_count" and isinstance(payload.get("info"), dict):
            info = payload["info"]
            chosen = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else info.get("total_token_usage")
            if isinstance(chosen, dict):
                activity.usage(chosen.get("total_tokens"), info.get("model_context_window"))
        return True
    if kind != "response_item":
        return True
    at = _iso(record.get("timestamp"))
    ptype = payload.get("type")
    key = payload.get("call_id") or payload.get("id")
    key = key if isinstance(key, str) and key else "o{}".format(offset)
    if ptype == "message":
        if payload.get("role") == "user":
            text = _text_blocks(payload.get("content"), ("input_text", "text"))
            if text.strip() and not text.lstrip().startswith(_CODEX_NOT_PROMPTS):
                activity.prompt(text)
    elif ptype == "function_call":
        activity.tool(key, payload.get("name"), _json_args(payload.get("arguments")), at)
    elif ptype == "custom_tool_call":
        raw = payload.get("input")
        args = _json_args(raw)
        activity.tool(key, payload.get("name"), args if isinstance(args, dict) and args else {"input": raw}, at)
    elif ptype == "local_shell_call":
        action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
        activity.tool(key, "local_shell", {"command": action.get("command")}, at)
    elif ptype == "web_search_call":
        action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
        activity.tool(key, "web_search", {"query": action.get("query"), "url": action.get("url")}, at)
    return True


def pi_record(record: Dict[str, Any], activity: _Activity, offset: int) -> bool:
    """One Pi session entry: user text and assistant ``toolCall`` blocks."""
    kind = record.get("type")
    if not isinstance(kind, str):
        return False
    if kind != "message":
        return True
    message = record.get("message")
    if not isinstance(message, dict):
        return False
    role = message.get("role")
    at = _iso(record.get("timestamp")) or _iso(message.get("timestamp"))
    content = message.get("content")
    if role == "user":
        text = _text_blocks(content)
        if text.strip():
            activity.prompt(text)
    elif role == "assistant":
        for index, block in enumerate(content if isinstance(content, list) else []):
            if isinstance(block, dict) and block.get("type") == "toolCall":
                key = block.get("id") if isinstance(block.get("id"), str) and block.get("id") else "{}:{}".format(offset, index)
                activity.tool(key, block.get("name"), _json_args(block.get("arguments")), at)
    return True


RecordParser = Callable[[Dict[str, Any], _Activity, int], bool]


# --------------------------------------------------------------------------
# incremental JSONL


def read_jsonl(state: Dict[str, Any], path: Path, parse: RecordParser, activity: _Activity, budget: int = MAX_READ_BYTES) -> int:
    """Fold what was appended to ``path`` since ``state["offset"]`` into ``activity``; bytes read.

    At most ``budget`` bytes per call. A file that was replaced or shrank
    starts over from its newest ``budget`` bytes; so does a backlog longer
    than the budget, because a card is about now. A line is consumed only
    once it is complete, so a half-written last line is read next time; a
    single line longer than the budget is skipped. When more than half the
    lines read carried no record envelope this harness writes, the format
    has drifted and ``state["error"]`` becomes ``"unknown"``.
    """
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode):
        raise OSError("not a regular file")
    ident = [int(info.st_dev), int(info.st_ino)]
    offset = state.get("offset")
    if state.get("path") != os.fspath(path) or state.get("file") != ident or not isinstance(offset, int) or offset > info.st_size:
        _reset(state)
        state.update(path=os.fspath(path), file=ident)
        offset = None
    size = info.st_size
    if offset == size:
        return 0
    mid_line = bool(state.get("mid_line")) if offset is not None else False
    if offset is None:
        budget = max(budget, FIRST_READ_BYTES)
    start = offset if offset is not None else max(0, size - budget)
    if size - start > budget:
        start = size - budget
        mid_line = True
    if offset is None and start > 0:
        mid_line = True
    with path.open("rb") as handle:
        if mid_line and start > 0:
            handle.seek(start - 1)
            mid_line = handle.read(1) != b"\n"
        handle.seek(start)
        chunk = handle.read(min(budget, size - start))
    consumed = 0
    if mid_line:
        cut = chunk.find(b"\n")
        if cut < 0:
            state.update(offset=start + len(chunk), mid_line=True)
            return len(chunk)
        consumed = cut + 1
    end = chunk.rfind(b"\n")
    if end < consumed:
        if len(chunk) >= budget:  # one line longer than the budget: skip it
            state.update(offset=start + len(chunk), mid_line=True)
        else:  # the rest of the line has not been written yet
            state.update(offset=start + consumed, mid_line=False)
        return len(chunk)
    total = shaped = 0
    position = start + consumed
    for raw in chunk[consumed:end + 1].split(b"\n"):
        at = position
        position += len(raw) + 1
        text = raw.strip()
        if not text:
            continue
        total += 1
        record: Any = None
        if text.startswith(b"{"):
            try:
                record = json.loads(text.decode("utf-8", "replace"))
            except ValueError:
                record = None
        if not isinstance(record, dict):
            continue
        try:
            if parse(record, activity, at):
                shaped += 1
        except (TypeError, ValueError, AttributeError, KeyError):
            continue  # an odd record is skipped, never fatal
    state.update(offset=start + end + 1, mid_line=False)
    if total:
        state["error"] = None if shaped * 2 >= total else "unknown"
    return len(chunk)


# --------------------------------------------------------------------------
# locating each harness's store


def _jsonl(value: Any) -> Optional[Path]:
    """A recorded path that is an absolute regular ``.jsonl`` file; anything else is not a transcript."""
    if not isinstance(value, str) or not os.path.isabs(value) or not value.endswith(".jsonl"):
        return None
    path = Path(value)
    try:
        return path if stat.S_ISREG(os.stat(path).st_mode) else None
    except OSError:
        return None


def _claude_path(state: Dict[str, Any], value: str, hint: Any, env: Mapping[str, str]) -> Optional[Path]:
    hinted = _jsonl(hint)
    if hinted is not None and hinted.stem == value:
        return hinted  # what the SessionStart hook recorded for exactly this session
    cached = _jsonl(state.get("path"))
    if cached is not None and cached.stem == value:
        return cached
    if not _transcripts.SAFE_ID_RE.match(value):
        return None
    found = _context.claude_transcript_by_id(value, _context.home_dir(dict(env)), dict(env))
    return Path(found) if found else None


def _codex_path(state: Dict[str, Any], value: str, env: Mapping[str, str]) -> Optional[Path]:
    cached = _jsonl(state.get("path"))
    if cached is not None and cached.name.endswith("-{}.jsonl".format(value)):
        return cached  # the rollout glob walks every day directory: once per conversation is enough
    if not _transcripts.SAFE_ID_RE.match(value):
        return None
    return _context.codex_rollout(value, _context.home_dir(dict(env)), dict(env))


def _pi_path(state: Dict[str, Any], ref: Mapping[str, Any], env: Mapping[str, str]) -> Optional[Path]:
    value = str(ref.get("value") or "")
    if ref.get("kind") == "path":
        return _jsonl(value)
    cached = _jsonl(state.get("path"))
    if cached is not None and cached.name.endswith("_{}.jsonl".format(value)):
        return cached
    if not _transcripts.SAFE_ID_RE.match(value):
        return None
    from herdr_team import pi_support

    root = pi_support.agent_dir(dict(env)) / "sessions"
    try:
        matches = sorted(root.glob("*/*_{}.jsonl".format(value))) if root.is_dir() else []
    except OSError:
        matches = []
    return matches[-1] if matches else None


# --------------------------------------------------------------------------
# OpenCode (opencode.db, read-only)

#: ``(column alias, JSON path, characters kept)``: only the fields a card shows, extracted in SQLite so a
#: tool's output and a reply's text never cross into Python.
_OPENCODE_INPUTS = (
    ("filePath", "$.state.input.filePath", 1000), ("path", "$.state.input.path", 1000),
    ("command", "$.state.input.command", 2000), ("pattern", "$.state.input.pattern", 500),
    ("include", "$.state.input.include", 500), ("url", "$.state.input.url", 1000),
    ("query", "$.state.input.query", 500), ("description", "$.state.input.description", 500),
    ("subagent_type", "$.state.input.subagent_type", 100), ("todos", "$.state.input.todos", 16384),
    ("patchText", "$.state.input.patchText", 8192),
)


def _columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    return {str(row[1]) for row in conn.execute("PRAGMA table_info({})".format(table))}


def _opencode_todo(conn: sqlite3.Connection, value: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """``(table exists, plan)`` from OpenCode's ``todo`` table for this session."""
    columns = _columns(conn, "todo")
    text = "content" if "content" in columns else ("text" if "text" in columns else None)
    if "session_id" not in columns or text is None or "status" not in columns:
        return False, None
    order = "position" if "position" in columns else "rowid"
    rows = conn.execute("SELECT substr({}, 1, 1000), status FROM todo WHERE session_id = ? ORDER BY {} LIMIT ?".format(text, order),
                        (value, MAX_PLAN_STEPS)).fetchall()
    return True, make_plan(todo_steps([{"content": row[0], "status": row[1]} for row in rows]), "opencode_todo")


def read_opencode(state: Dict[str, Any], value: str, env: Mapping[str, str], activity: _Activity, budget: int = MAX_READ_BYTES) -> int:
    """Fold the session's ``part`` rows changed since ``state["cursor"]`` into ``activity``; bytes read.

    Rows are read newest first up to ``budget`` bytes of extracted fields,
    then folded oldest first. The cursor is the newest ``time_updated``
    seen, and rows at that instant are read again (a tool part gains its
    input after it is first written), which is harmless: each part is one
    action keyed by its id.
    """
    home = _context.home_dir(dict(env))
    path = _context.opencode_db(home, dict(env))
    if not path.is_file():
        raise OSError("{} not found".format(path))
    conn = _context.connect_readonly(path)
    read = 0
    try:
        columns = _columns(conn, "part")
        if not {"session_id", "data", "message_id"} <= columns:
            state["error"] = "unknown"
            return 0
        order = "time_updated" if "time_updated" in columns else ("time_created" if "time_created" in columns else "rowid")
        if state.get("db") != os.fspath(path) or state.get("order") != order:
            _reset(state)
            state.update(db=os.fspath(path), order=order)
        cursor = state.get("cursor")
        fields = ", ".join("substr(json_extract(p.data, '{}'), 1, {})".format(json_path, limit) for _alias, json_path, limit in _OPENCODE_INPUTS)
        sql = ("SELECT p.id, p.{order}, json_extract(p.data, '$.type'), json_extract(p.data, '$.tool'), "
               "json_extract(p.data, '$.synthetic'), json_extract(p.data, '$.ignored'), json_extract(m.data, '$.role'), "
               "CASE WHEN json_extract(m.data, '$.role') = 'user' THEN substr(json_extract(p.data, '$.text'), 1, 2000) END, {fields} "
               "FROM part p LEFT JOIN message m ON m.id = p.message_id "
               "WHERE p.session_id = ? AND json_extract(p.data, '$.type') IN ('tool', 'text'){since} "
               "ORDER BY p.{order} DESC, p.rowid DESC LIMIT ?").format(
                   order=order, fields=fields, since=" AND p.{} >= ?".format(order) if isinstance(cursor, (int, float)) else "")
        params: Tuple[Any, ...] = (value,) + ((cursor,) if isinstance(cursor, (int, float)) else ()) + (OPENCODE_MAX_ROWS,)
        rows: List[Tuple[Any, ...]] = []
        for row in conn.execute(sql, params):
            read += sum(len(v) for v in row if isinstance(v, str))
            if rows and read > budget:
                break
            rows.append(row)
        newest = cursor
        planned = fresh = False
        for row in reversed(rows):
            part_id, stamp, ptype, tool, synthetic, ignored, role, text = row[:8]
            inputs = {alias: row[8 + index] for index, (alias, _p, _l) in enumerate(_OPENCODE_INPUTS) if row[8 + index] is not None}
            stamped = isinstance(stamp, (int, float)) and not isinstance(stamp, bool)
            # Rows at the cursor itself are read again every time; only a later one is news.
            new_row = cursor is None or (stamped and stamp > cursor)
            fresh = fresh or new_row
            if stamped and (newest is None or stamp > newest):
                newest = stamp
            at = _iso(stamp) if order != "rowid" else None
            if ptype == "text":
                if role == "user" and not synthetic and not ignored and isinstance(text, str):
                    activity.prompt(text)
                continue
            if isinstance(inputs.get("todos"), str):
                try:
                    inputs["todos"] = json.loads(inputs["todos"])
                except ValueError:
                    inputs["todos"] = []
            if new_row and isinstance(tool, str) and tool.lower() == "todowrite":
                planned = True
            activity.tool("p:" + str(part_id), tool, inputs, at)
        if newest is not None:
            state["cursor"] = newest
        if planned or "cursor_seen" not in state:
            exists, plan = _opencode_todo(conn, value)
            if exists:
                state["plan"] = plan  # the table is the plan when OpenCode keeps one
            state["cursor_seen"] = True
        state["error"] = None
        state.pop("note", None)
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    if fresh or "context" not in state:
        reading = _context.read_opencode(value, home, activity.configured_model)
        if reading is not None:
            activity.usage(reading.used, reading.window)
    return read


# --------------------------------------------------------------------------
# a card


def _session_ref(target: Mapping[str, Any], row: Optional[Mapping[str, Any]], record: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """The harness session to read: the live row's report first, then the roster's, then the hook's pane record."""
    for candidate in (row, target.get("session"), (record or {}).get("session")):
        if candidate is None:
            continue
        ref = _roster.session_of(candidate) if isinstance(candidate, Mapping) and "agent_session" in candidate else candidate
        if _roster.session_key(ref) is not None:
            return dict(ref)  # type: ignore[arg-type]
    return None


def _live_row(api: Any, target: Mapping[str, Any]) -> Any:
    """The ``agent.list`` row for the target's terminal; None when it is not listed, ``_UNSET`` when Herdr cannot say."""
    try:
        result = api.request("agent.list", {}, timeout=5.0)
    except HerdrTeamError:
        return _UNSET
    agents = result.get("agents") if isinstance(result, dict) else None
    if not isinstance(agents, list):
        return _UNSET
    terminal = target.get("terminal_id")
    for agent in agents:
        if isinstance(agent, dict) and terminal and agent.get("terminal_id") == terminal:
            return agent
    return None


def live_rows(api: Any) -> Optional[Dict[str, Dict[str, Any]]]:
    """``{terminal_id: agent.list row}``, or None when Herdr cannot be asked."""
    if api is None:
        return None
    try:
        result = api.request("agent.list", {}, timeout=5.0)
    except HerdrTeamError:
        return None
    agents = result.get("agents") if isinstance(result, dict) else None
    if not isinstance(agents, list):
        return None
    return {str(a["terminal_id"]): a for a in agents if isinstance(a, dict) and isinstance(a.get("terminal_id"), str)}


def _state_of(row: Optional[Mapping[str, Any]], known: bool) -> str:
    if not known:
        return "unknown"
    if row is None or (not row.get("agent") and not row.get("launch_pending")):
        return "not_running"
    status = str(row.get("agent_status") or "unknown")
    return status if status in STATES else "unknown"


def _refresh(session: SessionPaths, state: Dict[str, Any], reader: str, ref: Dict[str, Any], target: Mapping[str, Any],
             record: Optional[Mapping[str, Any]], env: Mapping[str, str]) -> None:
    """Bring the cached activity up to date from the harness's store (at most ``MAX_READ_BYTES``)."""
    value = str(ref.get("value") or "")
    if state.get("reader") != reader or state.get("session") != value:
        _reset(state)  # a new conversation (a restart, a clear, a swap): start over
        state.update(reader=reader, session=value)
    configured = target.get("model") if isinstance(target.get("model"), str) else None
    source = {"claude": "todo", "opencode": "opencode_todo"}.get(reader, "todo")
    activity = _Activity(state, source, configured)
    if reader == "opencode":
        read_opencode(state, value, env, activity)
        return
    missing_at = state.get("missing_at")
    if isinstance(missing_at, (int, float)) and 0 <= time.time() - missing_at < LOOKUP_RETRY_S:
        return  # looked a moment ago and found nothing; the note stays
    if reader == "claude":
        path = _claude_path(state, value, (record or {}).get("transcript_path"), env)
        parse: RecordParser = claude_record
    elif reader == "codex":
        path = _codex_path(state, value, env)
        parse = codex_record
    else:
        path = _pi_path(state, ref, env)
        parse = pi_record
    if path is None:
        state.update(error="unknown", note="the {} conversation file was not found".format(reader), missing_at=time.time())
        return
    state.pop("missing_at", None)
    state.pop("note", None)
    read_jsonl(state, path, parse, activity)
    if reader == "pi":
        from herdr_team import pi_support

        snapshot = pi_support.read_snapshot(session, {"kind": "pi", "terminal_id": target.get("terminal_id"),
                                                      "pane_id": target.get("pane_id"), "session": ref})
        if snapshot:
            activity.usage(snapshot.get("used"), snapshot.get("window"))


def card(layout: Any, target: Dict[str, Any], api: Any = None, now: Optional[float] = None, live: Any = _UNSET,
         env: Optional[Mapping[str, str]] = None, watched_ids: Optional[Set[str]] = None) -> Dict[str, Any]:
    """One activity card (contract 14.2) for a watch entry or a roster member row.

    ``live`` is the terminal's ``agent.list`` row when the caller already
    holds it (the notifier does), or None for "not listed"; left out, it is
    looked up through ``api`` when given. ``env`` locates the harness stores
    (``HOME``, ``CLAUDE_CONFIG_DIR``, ``CODEX_HOME``, ``XDG_DATA_HOME``,
    ``PI_CODING_AGENT_DIR``); ``os.environ`` when left out.
    """
    env = os.environ if env is None else env
    now_s = time.time() if now is None else float(now)
    session: SessionPaths = layout.session
    if live is _UNSET and api is not None:
        live = _live_row(api, target)
    known = live is not _UNSET
    row = live if isinstance(live, dict) else None
    state_name = _state_of(row, known)
    terminal = target.get("terminal_id") if isinstance(target.get("terminal_id"), str) else None
    pane = (row or {}).get("pane_id") or target.get("pane_id")
    kind = (row or {}).get("agent") or target.get("kind")
    kind = kind if isinstance(kind, str) and kind else None
    record = _roster.read_pane_record(session, terminal) if terminal else None
    record = record if isinstance(record, dict) and record.get("name") == target.get("name") else None
    ref = _session_ref(target, row, record)
    reader = _transcripts.harness_of(ref, kind) if ref is not None else kind
    reader = reader if reader in READERS else None
    path = cache_path(session, terminal)
    state = _load_cache(path)
    before = json.loads(json.dumps(state))
    headline = _line((row or {}).get("terminal_title_stripped") or (row or {}).get("terminal_title") or (row or {}).get("title"), HEADLINE_CHARS) if row else ""
    if headline:
        state["headline"] = headline
    if reader is not None and state_name != "not_running":
        if ref is None:
            state.update(error="unknown", note="no harness session reported yet; Herdr reports it once the {} integration is installed (herdr integration install {})".format(kind, kind))
        else:
            try:
                _refresh(session, state, reader, ref, dict(target, pane_id=pane), record, env)
            except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, HerdrTeamError):
                state["error"] = "unknown"
    _save_cache(path, state, before)
    is_watched_now = (terminal in watched_ids) if watched_ids is not None else bool(terminal and is_watched(session, terminal))
    out: Dict[str, Any] = {
        "key": terminal or target.get("pane_id") or "{}/{}".format(target.get("team") or "-", target.get("name") or "?"),
        "terminal_id": terminal,
        "pane_id": pane,
        "name": target.get("name") or (row or {}).get("name"),
        "team": target.get("team"), "role": target.get("role"), "kind": kind, "profile": target.get("profile"),
        "watched": is_watched_now,
        "state": state_name,
        "headline": headline or (state.get("headline") if state_name == "not_running" else None) or None,
        "plan": state.get("plan") if reader is not None else None,
        "actions": [{"icon": a.get("icon"), "text": a.get("text"), "at": a.get("at")} for a in state.get("actions") or []
                    if isinstance(a, dict) and a.get("icon") in ACTION_ICONS] if reader is not None else [],
        "files": [f for f in state.get("files") or [] if isinstance(f, str)] if reader is not None else [],
        "context": state.get("context") if reader is not None else None,
        "last_prompt": state.get("last_prompt") if reader is not None else None,
        "doing": None,
        "reader": reader,
        "reader_error": state.get("error") if reader is not None and state.get("error") else None,
        "reader_note": state.get("note") if reader is not None else None,
        "updated_at": _transcripts.iso_of(now_s),
    }
    out["doing"] = doing_line(out)
    return out


def memberships(layout: Any) -> Dict[str, Tuple[str, Dict[str, Any]]]:
    """``{terminal_id: (team, member row)}`` for every agent member of every team (one read of each ``team.json``)."""
    out: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    for name in layout.session.list_teams():
        doc = store.read_json(layout.team(name).team_json, default=None)
        members = doc.get("members") if isinstance(doc, dict) else None
        for member in members if isinstance(members, list) else []:
            if (isinstance(member, dict) and member.get("kind") != "human" and member.get("status") != "left"
                    and isinstance(member.get("terminal_id"), str) and member["terminal_id"] not in out):
                out[member["terminal_id"]] = (name, member)
    return out


def target_for(entry: Mapping[str, Any], membership: Optional[Tuple[str, Mapping[str, Any]]] = None) -> Dict[str, Any]:
    """A card target from a watch entry or a member row, with the roster's team, role, profile and session when it is a member."""
    target: Dict[str, Any] = {key: entry.get(key) for key in ("terminal_id", "pane_id", "name", "kind", "team", "role", "profile", "session", "model")}
    if membership is not None:
        team, member = membership
        target.update(team=team, name=member.get("name") or target.get("name"), role=member.get("role"),
                      profile=member.get("profile"), session=member.get("session") or target.get("session"),
                      model=member.get("model") or target.get("model"), kind=member.get("kind") or target.get("kind"))
        if not target.get("pane_id"):
            target["pane_id"] = member.get("pane_id")
    return target


def cards(layout: Any, api: Any = None, team: Optional[str] = None, now: Optional[float] = None,
          env: Optional[Mapping[str, str]] = None, include_watched: bool = True) -> List[Dict[str, Any]]:
    """Cards for every watched agent, plus the members of ``team`` when given (one ``agent.list`` for all).

    ``include_watched=False`` leaves out watched agents that are not members
    of ``team``: what a team's manager may see.
    """
    entries = watched(layout.session)
    ids = {entry["terminal_id"] for entry in entries}
    rows = live_rows(api)
    members = memberships(layout)
    targets: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    if include_watched:
        for entry in entries:
            seen.add(entry["terminal_id"])
            targets.append(target_for(entry, members.get(entry["terminal_id"])))
    if team:
        doc = store.read_json(layout.team(team).team_json, default=None)
        for member in (doc.get("members") if isinstance(doc, dict) else None) or []:
            if not isinstance(member, dict) or member.get("kind") == "human" or member.get("status") == "left":
                continue
            terminal = member.get("terminal_id")
            if isinstance(terminal, str) and terminal in seen:
                continue
            if isinstance(terminal, str):
                seen.add(terminal)
            targets.append(target_for(member, (team, member)))
    out = []
    for target in targets:
        terminal = target.get("terminal_id")
        live = (rows.get(terminal) if isinstance(terminal, str) else None) if rows is not None else _UNSET
        out.append(card(layout, target, now=now, live=live, env=env, watched_ids=ids))
    return out


def plan_of(layout: Any, target: Dict[str, Any], api: Any = None, env: Optional[Mapping[str, str]] = None) -> Optional[Dict[str, Any]]:
    """The agent's current plan ``{"steps", "current", "total", "source"}``, or None (used by ``canvas portrait --from-todo``)."""
    membership = memberships(layout).get(str(target.get("terminal_id") or ""))
    return card(layout, target_for(target, membership), api=api, env=env).get("plan")


# --------------------------------------------------------------------------
# the sidebar token


def doing_line(card: Dict[str, Any]) -> Optional[str]:
    """The sidebar line: plan step, else last edit, else last command, else the headline (<= 32 columns)."""
    text: Optional[str] = None
    plan = card.get("plan")
    if isinstance(plan, dict) and isinstance(plan.get("current"), int):
        steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
        current = plan["current"]
        if 1 <= current <= len(steps) and isinstance(steps[current - 1], dict):
            text = "▶ {}/{} {}".format(current, plan.get("total") or len(steps), steps[current - 1].get("text") or "")
    actions = [a for a in card.get("actions") or [] if isinstance(a, dict) and isinstance(a.get("text"), str) and a.get("text")]
    if text is None:
        edit = next((a for a in actions if a.get("icon") == "edit"), None)
        if edit is not None:
            text = "✎ " + _basename(edit["text"])
    if text is None:
        run = next((a for a in actions if a.get("icon") == "run"), None)
        program = _program(run["text"]) if run is not None else ""
        if program:
            # The token is readable by every agent in the session: the program, never its arguments.
            text = "$ " + program
    if text is None and isinstance(card.get("headline"), str) and card["headline"]:
        text = card["headline"]
    if not text:
        return None
    return _sanitize.truncate_columns(_line(text, 200), DOING_MAX_COLUMNS) or None


_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SUBCOMMAND_RE = re.compile(r"^[a-z][a-z0-9-]{0,24}\Z")
#: Words that run the next word: skipped, with their own flags, to find the program.
_WRAPPERS = frozenset(("sudo", "env", "time", "nohup", "exec", "command", "nice", "caffeinate", "xargs", "timeout"))
#: Words that only prepare a shell: the program is the next command after them.
_SETUP_WORDS = frozenset(("cd", "pushd", "popd", "export", "source", ".", "set", "unset", "true", "sleep"))
#: Programs whose first plain word is a subcommand worth showing (``git commit``, ``npm test``).
_SUBCOMMAND_TOOLS = frozenset(("git", "npm", "pnpm", "yarn", "bun", "npx", "cargo", "go", "just", "make", "docker", "kubectl",
                               "gh", "uv", "pip", "poetry", "brew", "herdr", "herdr-synapse", "dotnet", "mvn", "gradle"))


def _program(command: str) -> str:
    """What a command line runs, without its arguments: ``psql``, ``git commit``; ``""`` when nothing reads as one."""
    fallback = ""
    for segment in re.split(r"&&|\|\||[;|]", command or ""):
        tokens = segment.split()
        index = 0
        while index < len(tokens) and (_ENV_ASSIGNMENT_RE.match(tokens[index]) or tokens[index] in _WRAPPERS or tokens[index].startswith("-")):
            if tokens[index] == "timeout" and index + 1 < len(tokens) and tokens[index + 1][:1].isdigit():
                index += 1  # timeout's duration
            index += 1
        if index >= len(tokens):
            continue
        program = _basename(tokens[index].strip("'\"()")) or ""
        if not program or program.startswith("[redacted"):
            continue
        if program in _SETUP_WORDS:
            fallback = fallback or program
            continue
        nxt = tokens[index + 1] if index + 1 < len(tokens) else ""
        if program in _SUBCOMMAND_TOOLS and _SUBCOMMAND_RE.match(nxt):
            return "{} {}".format(program, nxt)
        return program
    return fallback


def doing_token_commands(cards: Iterable[Dict[str, Any]]) -> List[Any]:
    """``roster.TokenCommand`` stamps of ``team_doing`` for these cards (TTL ``DOING_TTL_MS``); a clear when there is nothing to say."""
    out: List[Any] = []
    for item in cards:
        pane = item.get("pane_id")
        if not isinstance(pane, str) or not pane:
            continue
        value = item.get("doing") if item.get("state") != "not_running" else None
        if isinstance(value, str) and value:
            out.append(_roster.TokenCommand(pane, DOING_SOURCE, {DOING_TOKEN: value}, DOING_TTL_MS))
        else:
            out.append(_roster.TokenCommand(pane, DOING_SOURCE, {DOING_TOKEN: None}))
    return out


def clear_token(api: Any, pane_id: Any) -> bool:
    """Clear ``team_doing`` on one pane; False when Herdr refused or cannot be reached."""
    if not isinstance(pane_id, str) or not pane_id:
        return False
    try:
        api.request("pane.report_metadata", {"pane_id": pane_id, "source": DOING_SOURCE, "tokens": {DOING_TOKEN: None}}, timeout=5.0)
        return True
    except HerdrTeamError:
        return False


def clear_doing_tokens(layout: Any, api: Any) -> int:
    """Clear ``team_doing`` on every watched agent's pane; how many panes were cleared.

    The flags themselves stay (switching the layer back on brings the
    tokens back); a pane that moved is cleared where it is now as well.
    """
    entries = watched(layout.session)
    if not entries:
        return 0
    panes: List[str] = [e["pane_id"] for e in entries if e.get("pane_id")]
    rows = live_rows(api) or {}
    for entry in entries:
        row = rows.get(entry["terminal_id"])
        pane = row.get("pane_id") if isinstance(row, dict) else None
        if isinstance(pane, str) and pane and pane not in panes:
            panes.append(pane)
    return sum(1 for pane in panes if clear_token(api, pane))
