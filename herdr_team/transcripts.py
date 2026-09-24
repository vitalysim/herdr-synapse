"""Search what members said and did in their own harness conversations.

The board holds what a member chose to post. The harness keeps everything
else: the question it was asked, the answer it gave, the tool it ran and what
came back. "What did the analyst find about X yesterday?" is usually answered
there, and nowhere else. This module is the reader behind ``search``
(``cmd_search``); it holds no authority logic, which stays with the command.

It reads the same stores ``context`` reads, through the same locators: Claude
transcripts, Codex rollout logs, OpenCode's ``opencode.db``, and Pi's session
file. What it adds is a walk over every message instead of a peek at the last
usage record. Four properties hold for every reader:

- **Only recorded conversations.** A file is opened because a member's roster
  entry names it: the current ``session``, and with ``history`` the
  references kept in ``session_history`` (restarts, clears) and
  ``agent_history`` (swaps). No directory is scanned for "recent" files, and
  a session id only reaches a filename pattern once it is shown to be a plain
  id, so a recorded value can never widen the lookup to the user's unrelated
  conversations.
- **Stream, never slurp.** Files are read line by line, capped at
  ``MAX_SESSION_BYTES`` per conversation. Over the cap the *newest* part is
  kept, since results are newest first; the cap is reported, never silent.
- **Read-only.** SQLite through a read-only URI with a short timeout, the way
  ``context.connect_readonly`` opens it.
- **Redacted.** A matching message is passed through the board's secret
  patterns before anything is excerpted from it, and the match is taken from
  the redacted text, so a query cannot be used to fish a key out of a
  transcript.

A store that is missing, unreadable, or of a kind with no reader is reported
per member as a reason, never raised.
"""
from __future__ import annotations

import heapq
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

from herdr_team import context as _context
from herdr_team import roster as _roster
from herdr_team import sanitize as _sanitize
from herdr_team.cmd_board import SECRET_PATTERNS

#: Per conversation. A long Claude session is tens of MB; this reads a very
#: long one whole and still bounds a pathological one.
MAX_SESSION_BYTES = 64 * 1024 * 1024
ROLES = ("user", "assistant", "tool")
KINDS = ("claude", "codex", "opencode", "pi")
DEFAULT_LIMIT = 20
MAX_LIMIT = 500
DEFAULT_CONTEXT = 160
MIN_CONTEXT, MAX_CONTEXT = 40, 2000
MAX_QUERY_CHARS = 1000
MAX_TERMS = 16

#: A session id that may go into a filename pattern: what Claude, Codex, and
#: Pi ids look like, and nothing that ``glob`` or a path would read as syntax.
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")

#: Codex starts a conversation with harness-written "user" messages (the
#: environment, AGENTS.md). They were never said by anyone, and a search for
#: a word in AGENTS.md would otherwise hit the first message of every session.
CODEX_INJECTED_PREFIXES = ("<environment_context>", "<user_instructions>", "# AGENTS.md instructions", "<permissions instructions>")

#: The board's patterns match a key's header, not its body; a transcript that
#: printed a private key is redacted from the header to the matching footer.
_PRIVATE_KEY_BLOCK = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
#: ``sk-ant-...`` also fits the OpenAI shape; the more specific label goes first.
_REDACTIONS = tuple(sorted(SECRET_PATTERNS, key=lambda item: item[0] == "openai_key"))
_WHITESPACE_RE = re.compile(r"\s+")
_FINE_FRACTION_RE = re.compile(r"(\.\d{6})\d+")
_RELATIVE_RE = re.compile(r"^(\d+)\s*([smhdw])\Z")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


# --------------------------------------------------------------------------
# the query


@dataclass
class Query:
    """Case-insensitive terms that must all occur in one message.

    Whitespace separates terms; a double-quoted run is one term matched as an
    exact phrase (any whitespace between its words, so a line break in the
    transcript does not hide it). Terms are literal text, never patterns.
    """

    text: str
    terms: List[str]
    patterns: List["re.Pattern[str]"]

    @classmethod
    def parse(cls, text: str) -> "Query":
        text = (text or "").strip()
        if len(text) > MAX_QUERY_CHARS:
            raise ValueError("the query is longer than {} characters".format(MAX_QUERY_CHARS))
        if text.count('"') % 2:
            raise ValueError('unbalanced double quote; a phrase is written "like this"')
        terms: List[str] = []
        for index, part in enumerate(text.split('"')):
            if index % 2:  # inside quotes: one phrase
                phrase = " ".join(part.split())
                if phrase:
                    terms.append(phrase)
            else:
                terms.extend(part.split())
        if not terms:
            raise ValueError("search needs at least one word")
        if len(terms) > MAX_TERMS:
            raise ValueError("at most {} terms".format(MAX_TERMS))
        patterns = [re.compile(r"\s+".join(re.escape(word) for word in term.split()), re.IGNORECASE) for term in terms]
        return cls(text, terms, patterns)

    def span(self, text: str) -> Optional[Tuple[int, int]]:
        """The earliest match when every term occurs in ``text``, else None."""
        best: Optional[Tuple[int, int]] = None
        for pattern in self.patterns:
            found = pattern.search(text)
            if found is None:
                return None
            if best is None or found.start() < best[0]:
                best = (found.start(), found.end())
        return best


def parse_since(text: str, now: float) -> float:
    """Epoch seconds for ``3d``/``12h``/``30m``/``2w``, or an ISO date or time.

    A date or time without a zone is local, which is what a person typing
    ``--since 2026-09-22`` means; ``Z`` or an offset is honoured.
    """
    value = (text or "").strip()
    relative = _RELATIVE_RE.match(value.lower())
    if relative:
        return now - int(relative.group(1)) * _UNITS[relative.group(2)]
    iso = value[:-1] + "+00:00" if value[-1:] in ("Z", "z") else value
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        raise ValueError("--since expects 30m, 12h, 3d, 2w, or an ISO date such as 2026-09-22 or 2026-09-22T14:00Z")
    if when.tzinfo is None:
        when = when.astimezone()
    return when.timestamp()


# --------------------------------------------------------------------------
# text handling


def redact(text: str) -> str:
    """``text`` with every secret shape the board refuses replaced by ``[redacted:<kind>]``."""
    if "PRIVATE KEY-----" in text:
        text = _PRIVATE_KEY_BLOCK.sub("[redacted:private_key]", text)
    for label, pattern in _REDACTIONS:
        text = pattern.sub("[redacted:{}]".format(label), text)
    return text


def _clean(piece: str) -> str:
    """One line of printable text: no escapes, no controls, no invisible format characters."""
    return _WHITESPACE_RE.sub(" ", _sanitize.replace_format_chars(_sanitize.strip_controls(piece), ""))


def excerpt(text: str, span: Tuple[int, int], width: int) -> Tuple[str, int, int]:
    """About ``width`` characters around ``span``, flattened to one line.

    Returns the excerpt and where the match sits in it, so a renderer can mark
    it (``»…«`` in text mode) without searching again.
    """
    start, end = span
    room = max(0, width - (end - start))
    lo = max(0, start - room // 2)
    hi = min(len(text), end + room - (start - lo))
    lo = max(0, min(lo, hi - max(width, end - start)))
    lead = "…" if lo > 0 else ""
    tail = "…" if hi < len(text) else ""
    before = _clean(text[lo:start])
    if not lead:
        before = before.lstrip()
    middle = _clean(text[start:end])
    after = _clean(text[end:hi])
    if not tail:
        after = after.rstrip()
    first = len(lead) + len(before)
    return lead + before + middle + after + tail, first, first + len(middle)


def marked(hit: Mapping[str, Any]) -> str:
    """The excerpt with its match between ``»`` and ``«``."""
    text = str(hit.get("excerpt") or "")
    bounds = hit.get("match")
    if not isinstance(bounds, list) or len(bounds) != 2:
        return text
    start, end = int(bounds[0]), int(bounds[1])
    return text[:start] + "»" + text[start:end] + "«" + text[end:]


#: Keys whose values are identifiers, content-type tags, harness bookkeeping
#: (exit codes, durations), or inline media (base64 screenshots), none of
#: which anyone searches for.
_SKIP_KEYS = frozenset(("type", "id", "call_id", "callID", "tool_use_id", "toolCallId", "signature", "metadata",
                        "image_url", "source", "data", "mimeType", "media_type", "encrypted_content"))


def _flatten(value: Any, depth: int = 0) -> str:
    """The text inside a tool call's arguments or result, without JSON punctuation."""
    if isinstance(value, str):
        return value
    if depth > 6 or value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return "\n".join(part for part in (_flatten(v, depth + 1) for k, v in value.items() if k not in _SKIP_KEYS) if part)
    if isinstance(value, list):
        return "\n".join(part for part in (_flatten(v, depth + 1) for v in value) if part)
    return ""


def _json_text(value: Any) -> str:
    """A tool payload that may itself be a JSON document in a string (Codex arguments, outputs)."""
    if isinstance(value, str) and value[:1] in ("{", "["):
        try:
            return _flatten(json.loads(value))
        except ValueError:
            return value
    return _flatten(value)


def _tool(name: Any, body: str) -> str:
    return "{} {}".format(name, body).strip() if isinstance(name, str) and name else body


def epoch_of(value: Any) -> Optional[float]:
    """Epoch seconds from an ISO string or epoch milliseconds (OpenCode, Pi).

    Fractions finer than microseconds are trimmed first: ``strptime`` reads at
    most six digits, and a nanosecond stamp would otherwise lose the message's
    time altogether.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if value > 10**11 else float(value)
    return _roster.parse_iso(_FINE_FRACTION_RE.sub(r"\1", value.strip())) if isinstance(value, str) else None


def iso_of(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    try:
        when = datetime.fromtimestamp(epoch, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(when.microsecond // 1000)


# --------------------------------------------------------------------------
# one message


@dataclass
class Message:
    role: str  # user | assistant | tool
    text: str
    epoch: Optional[float]


def _grouped(role_texts: Iterable[Tuple[str, str]], epoch: Optional[float]) -> Iterator[Message]:
    """One message per role per record: a reply split into blocks is still one message."""
    by_role: Dict[str, List[str]] = {}
    for role, text in role_texts:
        if isinstance(text, str) and text.strip():
            by_role.setdefault(role, []).append(text)
    for role, texts in by_role.items():
        yield Message(role, "\n".join(texts), epoch)


def claude_messages(record: Dict[str, Any]) -> Iterator[Message]:
    """User and assistant text, tool calls and tool results; thinking is not what was said."""
    if record.get("type") not in ("user", "assistant"):
        return
    message = record.get("message")
    if not isinstance(message, dict):
        return
    role = message.get("role") if message.get("role") in ("user", "assistant") else record.get("type")
    content = message.get("content")
    parts: List[Tuple[str, str]] = []
    if isinstance(content, str):
        parts.append((str(role), content))
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                parts.append((str(role), block.get("text") or ""))
            elif kind == "tool_use":
                parts.append(("tool", _tool(block.get("name"), _flatten(block.get("input")))))
            elif kind == "tool_result":
                parts.append(("tool", _flatten(block.get("content"))))
    yield from _grouped(parts, epoch_of(record.get("timestamp")))


def codex_messages(record: Dict[str, Any]) -> Iterator[Message]:
    """``response_item`` records: the history Codex actually sent, once each.

    ``event_msg`` repeats some of it in forms that change between versions, so
    it is not read; ``developer`` messages and harness-injected context are
    instructions to the model, not conversation.
    """
    if record.get("type") != "response_item":
        return
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return
    kind = payload.get("type")
    parts: List[Tuple[str, str]] = []
    if kind == "message" and payload.get("role") in ("user", "assistant"):
        for block in payload.get("content") or []:
            if not isinstance(block, dict) or block.get("type") not in ("input_text", "output_text", "text"):
                continue
            text = block.get("text")
            if isinstance(text, str) and not text.lstrip().startswith(CODEX_INJECTED_PREFIXES):
                parts.append((str(payload["role"]), text))
    elif kind == "function_call":
        parts.append(("tool", _tool(payload.get("name"), _json_text(payload.get("arguments")))))
    elif kind == "custom_tool_call":
        parts.append(("tool", _tool(payload.get("name"), _json_text(payload.get("input")))))
    elif kind in ("function_call_output", "custom_tool_call_output"):
        parts.append(("tool", _json_text(payload.get("output"))))
    elif kind in ("local_shell_call", "web_search_call"):
        parts.append(("tool", _flatten(payload.get("action"))))
    yield from _grouped(parts, epoch_of(record.get("timestamp")))


def pi_messages(record: Dict[str, Any]) -> Iterator[Message]:
    """Pi's ``message`` entries: user, assistant (text and tool calls), and tool results."""
    if record.get("type") != "message":
        return
    message = record.get("message")
    if not isinstance(message, dict):
        return
    role = message.get("role")
    content = message.get("content")
    blocks = content if isinstance(content, list) else [{"type": "text", "text": content}] if isinstance(content, str) else []
    parts: List[Tuple[str, str]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if role == "toolResult":
            if block.get("type") == "text":
                parts.append(("tool", _tool(message.get("toolName"), block.get("text") or "")))
        elif block.get("type") == "text" and role in ("user", "assistant"):
            parts.append((str(role), block.get("text") or ""))
        elif block.get("type") == "toolCall":
            parts.append(("tool", _tool(block.get("name"), _json_text(block.get("arguments")))))
    epoch = epoch_of(record.get("timestamp"))
    yield from _grouped(parts, epoch if epoch is not None else epoch_of(message.get("timestamp")))


def opencode_messages(role: Optional[str], part: Dict[str, Any], created: Any) -> Iterator[Message]:
    """One ``part`` row: text keeps its message's role, a tool part is a tool call and its output."""
    kind = part.get("type")
    epoch = epoch_of(created)
    if kind == "text" and role in ("user", "assistant"):
        yield from _grouped([(role, part.get("text") or "")], epoch)
    elif kind == "tool":
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        body = "\n".join(t for t in (_flatten(state.get("input")), _flatten(state.get("output"))) if t)
        yield from _grouped([("tool", _tool(part.get("tool"), body))], epoch)


# --------------------------------------------------------------------------
# which conversations a member has


@dataclass
class SessionRef:
    kind: str
    session: Dict[str, Any]
    history: bool
    transcript_hint: Optional[str] = None

    @property
    def value(self) -> str:
        return str(self.session.get("value") or "")

    @property
    def label(self) -> str:
        return "{} {}".format(self.kind, _roster.short_session(self.session) or "?")


def harness_of(session: Mapping[str, Any], fallback: Optional[str]) -> str:
    """Which harness wrote ``session``: its reported agent, its ``herdr:<kind>`` source, else the member's kind."""
    agent = session.get("agent")
    if isinstance(agent, str) and agent:
        return agent
    source = session.get("source")
    if isinstance(source, str) and source.startswith("herdr:") and len(source) > 6:
        return source[6:]
    return str(fallback or "unknown")


def session_refs(member: Mapping[str, Any], pane_record: Optional[Mapping[str, Any]], history: bool) -> List[SessionRef]:
    """The member's recorded conversations, current first, then (with ``history``) newest earlier first."""
    refs: List[SessionRef] = []
    seen = set()

    def add(session: Any, kind: Optional[str], earlier: bool, hint: Optional[str] = None) -> None:
        key = _roster.session_key(session)
        if key is None:
            return
        ref = SessionRef(harness_of(session, kind), dict(session), earlier, hint)
        if (ref.kind, ref.value) in seen:
            return
        seen.add((ref.kind, ref.value))
        refs.append(ref)

    record = pane_record if isinstance(pane_record, Mapping) and pane_record.get("name") == member.get("name") else None
    current = member.get("session")
    if _roster.session_key(current) is None and record is not None:
        current = record.get("session")  # the Claude hook records it here first
    hint = record.get("transcript_path") if record is not None else None
    add(current, member.get("kind"), False, hint if isinstance(hint, str) else None)
    if history:
        for entry in reversed(member.get("session_history") or []):
            add(entry, member.get("kind"), True)
        for entry in reversed(member.get("agent_history") or []):
            if isinstance(entry, dict):
                add(entry.get("session"), entry.get("kind"), True)
    return refs


# --------------------------------------------------------------------------
# the scan


@dataclass
class MemberScan:
    sessions: int = 0
    bytes: int = 0
    skipped: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {"sessions": self.sessions, "bytes": self.bytes, "skipped": list(self.skipped)}


@dataclass
class Search:
    """One search: the query, its filters, and the newest ``limit`` hits kept so far."""

    query: Query
    since: Optional[float] = None
    role: Optional[str] = None
    limit: int = DEFAULT_LIMIT
    width: int = DEFAULT_CONTEXT
    max_bytes: int = MAX_SESSION_BYTES
    env: Dict[str, str] = field(default_factory=dict)
    home: Optional[Path] = None
    total: int = 0
    _heap: List[Tuple[float, int, Dict[str, Any]]] = field(default_factory=list)

    def hits(self) -> List[Dict[str, Any]]:
        """Newest first; hits without a timestamp last."""
        return [hit for _key, _n, hit in sorted(self._heap, key=lambda item: (item[0], item[1]), reverse=True)]

    # -- one message

    def consider(self, member: str, ref: SessionRef, source: str, message: Message) -> None:
        if self.role is not None and message.role != self.role:
            return
        if self.since is not None and (message.epoch is None or message.epoch < self.since):
            return
        if self.query.span(message.text) is None:
            return  # cheap test on the raw text first; most messages stop here
        text = redact(message.text)
        span = self.query.span(text)
        if span is None:
            return  # the words only matched inside a secret
        self.total += 1
        key = message.epoch if message.epoch is not None else float("-inf")
        if len(self._heap) >= self.limit and key <= self._heap[0][0]:
            return
        shown, start, end = excerpt(text, span, self.width)
        hit = {"member": member, "kind": ref.kind, "session": ref.value, "ts": iso_of(message.epoch),
               "role": message.role, "excerpt": shown, "match": [start, end], "source_path": source,
               "history": ref.history}
        entry = (key, self.total, hit)
        if len(self._heap) < self.limit:
            heapq.heappush(self._heap, entry)
        else:
            heapq.heapreplace(self._heap, entry)

    # -- one member

    def member(self, name: str, refs: List[SessionRef], scan: MemberScan) -> None:
        for ref in refs:
            reader = _READERS.get(ref.kind)
            if reader is None:
                scan.skipped.append("{}: no transcript reader for this kind".format(ref.label))
                continue
            try:
                reader(self, name, ref, scan)
            except (OSError, ValueError) as err:
                scan.skipped.append("{}: unreadable ({})".format(ref.label, type(err).__name__))

    # -- the stores

    def _jsonl(self, name: str, ref: SessionRef, path: Path, scan: MemberScan,
               parse: Callable[[Dict[str, Any]], Iterator[Message]]) -> None:
        size = path.stat().st_size
        read = 0
        with path.open("rb") as handle:
            if size > self.max_bytes:
                handle.seek(size - self.max_bytes)
                read += len(handle.readline())  # the line the seek landed inside
                scan.skipped.append("{}: only the newest {} of {} scanned".format(ref.label, _size(self.max_bytes), _size(size)))
            scan.sessions += 1
            for raw in handle:
                if read >= self.max_bytes:
                    break  # only reachable when the live file grew while we read it
                read += len(raw)
                line = raw.strip()
                if not line.startswith(b"{"):
                    continue
                try:
                    record = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue  # a half-written last line is normal on a live file
                if isinstance(record, dict):
                    for message in parse(record):
                        self.consider(name, ref, os.fspath(path), message)
        scan.bytes += read


def _size(count: int) -> str:
    if count >= 1024 * 1024:
        return "{:.0f} MiB".format(count / (1024.0 * 1024.0))
    return "{:.0f} KiB".format(count / 1024.0)


def _jsonl_file(value: Optional[str]) -> Optional[Path]:
    """A recorded path that is a regular ``.jsonl`` file; anything else is not a transcript."""
    if not value or not os.path.isabs(value) or not value.endswith(".jsonl"):
        return None
    path = Path(value)
    return path if path.is_file() else None


def _read_claude(search: Search, name: str, ref: SessionRef, scan: MemberScan) -> None:
    path: Optional[Path] = None
    hint = _jsonl_file(ref.transcript_hint)
    if hint is not None and hint.stem == ref.value:
        path = hint  # what the SessionStart hook recorded for exactly this session
    if path is None:
        if not SAFE_ID_RE.match(ref.value):
            scan.skipped.append("{}: session id is not a plain id".format(ref.label))
            return
        found = _context.claude_transcript_by_id(ref.value, search.home, search.env)
        path = Path(found) if found else None
    if path is None:
        scan.skipped.append("{}: transcript not found under {}".format(ref.label, _context.claude_projects_root(search.home, search.env)))
        return
    search._jsonl(name, ref, path, scan, claude_messages)


def _read_codex(search: Search, name: str, ref: SessionRef, scan: MemberScan) -> None:
    if not SAFE_ID_RE.match(ref.value):
        scan.skipped.append("{}: session id is not a plain id".format(ref.label))
        return
    path = _context.codex_rollout(ref.value, search.home, search.env, archived=True)
    if path is None:
        scan.skipped.append("{}: rollout log not found under {}".format(ref.label, _context.codex_home(search.home, search.env)))
        return
    search._jsonl(name, ref, path, scan, codex_messages)


def _read_pi(search: Search, name: str, ref: SessionRef, scan: MemberScan) -> None:
    path: Optional[Path] = None
    if ref.session.get("kind") == "path":
        path = _jsonl_file(ref.value)
    elif SAFE_ID_RE.match(ref.value):
        from herdr_team import pi_support

        root = pi_support.agent_dir(search.env) / "sessions"
        try:
            matches = sorted(root.glob("*/*_{}.jsonl".format(ref.value))) if root.is_dir() else []
        except OSError:
            matches = []
        path = matches[-1] if matches else None
    if path is None:
        scan.skipped.append("{}: session file not found".format(ref.label))
        return
    search._jsonl(name, ref, path, scan, pi_messages)


def _read_opencode(search: Search, name: str, ref: SessionRef, scan: MemberScan) -> None:
    path = _context.opencode_db(search.home, search.env)
    if not path.is_file():
        scan.skipped.append("{}: {} not found".format(ref.label, path))
        return
    try:
        conn = _context.connect_readonly(path)
    except sqlite3.Error as err:
        scan.skipped.append("{}: opencode.db unreadable ({})".format(ref.label, err))
        return
    read = 0
    try:
        roles: Dict[str, Optional[str]] = {}
        for message_id, blob in conn.execute("SELECT id, data FROM message WHERE session_id = ?", (ref.value,)):
            read += len(blob) if isinstance(blob, (str, bytes)) else 0
            try:
                data = json.loads(blob) if isinstance(blob, (str, bytes)) else None
            except ValueError:
                data = None
            roles[str(message_id)] = data.get("role") if isinstance(data, dict) else None
        if not roles:
            scan.skipped.append("{}: session not in {}".format(ref.label, path))
            return
        scan.sessions += 1
        sql = "SELECT message_id, time_created, data FROM part WHERE session_id = ?"
        params: Tuple[Any, ...] = (ref.value,)
        if search.since is not None:
            sql += " AND time_created >= ?"
            params += (int(search.since * 1000),)
        sql += " ORDER BY time_created DESC"
        for message_id, created, blob in conn.execute(sql, params):
            if not isinstance(blob, (str, bytes)):
                continue
            read += len(blob)
            if read > search.max_bytes:
                scan.skipped.append("{}: only the newest {} scanned".format(ref.label, _size(search.max_bytes)))
                break
            try:
                part = json.loads(blob)
            except ValueError:
                continue
            if isinstance(part, dict):
                for message in opencode_messages(roles.get(str(message_id)), part, created):
                    search.consider(name, ref, os.fspath(path), message)
    except sqlite3.Error as err:
        scan.skipped.append("{}: opencode.db unreadable ({})".format(ref.label, err))
    finally:
        scan.bytes += read
        try:
            conn.close()
        except sqlite3.Error:
            pass


_READERS: Dict[str, Callable[[Search, str, SessionRef, MemberScan], None]] = {
    "claude": _read_claude,
    "codex": _read_codex,
    "opencode": _read_opencode,
    "pi": _read_pi,
}


def run(search: Search, members: Iterable[Tuple[str, List[SessionRef], Optional[str]]]) -> Dict[str, MemberScan]:
    """Scan every ``(name, refs, why_none)``; ``why_none`` is reported when a member has no conversation."""
    scanned: Dict[str, MemberScan] = {}
    for name, refs, why_none in members:
        scan = scanned.setdefault(name, MemberScan())
        if not refs:
            scan.skipped.append(why_none or "no conversation recorded")
            continue
        search.member(name, refs, scan)
    return scanned
