"""Pure state for the console, picker, and compose UIs (plan 7.3, 11).

No curses here: ``console.py``, ``picker.py``, and ``compose.py`` own the
screen; this module owns the data and the key-to-intent mapping so every
transition is unit-testable. Models are plain dataclasses; ``apply_key``
functions mutate the model in place and return the ``Intent`` the runtime
must perform (post, retract, mute, ...), or ``None`` when the key only
edited local state.

Key vocabulary shared by every runtime (strings): a single printable
character inserts itself; ``ENTER``, ``ALT_ENTER``, ``TAB``, ``BACKSPACE``,
``DELETE``, ``LEFT``, ``RIGHT``, ``HOME``, ``END``, ``UP``, ``DOWN``,
``PGUP``, ``PGDN``, ``ESC``, ``CTRL_A``, ``CTRL_C``, ``CTRL_D``, ``CTRL_E``,
``CTRL_K``, ``CTRL_O``, ``CTRL_U``, ``PASTE_START``, ``PASTE_END``,
``RESIZE``.

Fallbacks: the sanitizer and roster validators live in other modules that
may still be stubs; every call is guarded so this module works standalone.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from herdr_team import roster, sanitize
from herdr_team import topology as team_topology
from herdr_team.errors import HerdrTeamError
from herdr_team.paths import MAX_ROLE_CHARS, MAX_TEAM_CHARS, ROLE_NAME_RE, TEAM_NAME_RE

FILTERS = ("all", "to me", "requests", "human", "system", "teams", "team")
SLASH_COMMANDS = (
    "/all", "/human", "/kind", "/reply", "/urgent", "/interrupt", "/interrupts", "/ref", "/retract", "/mute", "/unmute", "/pause",
    "/nudge", "/focus", "/peek", "/who", "/context", "/compact", "/clear", "/model", "/team", "/links", "/link", "/unlink", "/wipe", "/asks", "/ask-policy", "/filter", "/as", "/use", "/charter", "/remove", "/export", "/help", "/quit",
)
#: ``/`` menu rows: command -> (placeholder, what it does). Every entry in
#: ``SLASH_COMMANDS`` must appear here; a test keeps the two in step, so a new
#: command cannot be added without telling the operator how to type it.
SLASH_USAGE = {
    "/all": ("text", "post to the whole team"),
    "/human": ("text", "a note to yourself"),
    "/kind": ("note|request|handoff|done|blocked|question|answer", "set the post kind"),
    "/reply": ("N", "reply to post #N"),
    "/urgent": ("", "nudge everyone, not just on their next read"),
    "/interrupt": ("@name text", "type into a working teammate's turn"),
    "/interrupts": ("[off|on|kind,…] [--cooldown 10m]", "which kinds interrupts may reach"),
    "/ref": ("path", "attach a file by reference"),
    "/retract": ("N", "retract post #N"),
    "/mute": ("[name] [10m]", "stop nudging a member"),
    "/unmute": ("[name]", "resume nudging a member"),
    "/pause": ("[10m]", "stop all delivery for a while"),
    "/nudge": ("name [--force]", "nudge one member now"),
    "/focus": ("name", "jump to that member's pane"),
    "/peek": ("name", "look at that member's screen"),
    "/who": ("", "the roster with roles, states and tasks"),
    "/asks": ("", "what is waiting on you"),
    "/ask-policy": ("[block|noblock] [8m]", "whether an agent waits for you, and how long"),
    "/context": ("[name]", "how full each member's context window is"),
    "/compact": ("name", "ask a member to summarise its context"),
    "/clear": ("name", "throw away a member's context and brief it again"),
    "/model": ("name model[@effort] [--restart]", "set model/effort (Claude and OpenCode effort can apply live; other changes at resume or --restart)"),
    "/team": ("other-team text", "post to a linked team (its manager is nudged)"),
    "/links": ("", "which teams this team is linked to, and their state"),
    "/link": ("other-team", "link this team to another (both need a manager)"),
    "/unlink": ("other-team", "break the link to another team"),
    "/wipe": ("[--purge] [reason]", "empty the board: posts move to the archive (--purge deletes them); asks first"),
    "/filter": ("[all|to me|requests|human|system]", "filter the feed"),
    "/as": ("label", "change the label your posts carry"),
    "/use": ("team", "switch to another team"),
    "/charter": ("[set [--urgent] text]", "show or change the team charter"),
    "/remove": ("name", "remove a member from the team"),
    "/export": ("[path] [--format md|json|jsonl|text]", "save the whole board to a file"),
    "/help": ("", "every sign, command and key"),
    "/quit": ("", "close the console"),
}

#: Directives that turn a line into a post rather than a command.
POST_DIRECTIVES = ("/all", "/human", "/team", "/kind", "/reply", "/urgent", "/interrupt", "/ref")
POST_KINDS = ("note", "request", "handoff", "done", "blocked", "question", "answer")
REQUEST_KINDS = ("request", "question", "blocked", "handoff")

MAX_TEXT_CHARS = 2000
MAX_CHARTER_CHARS = 2000
MAX_RULES_CHARS = 4000
MAX_BRIEF_CHARS = 300
#: What ``brief --set`` stores (``charter.MAX_BRIEF_TOTAL_CHARS``); only the first ``MAX_BRIEF_CHARS``
#: reach the briefing line, so the picker's goal editor uses this cap or a longer CLI-set brief
#: could not be edited at all.
MAX_BRIEF_TOTAL_CHARS = 2000
MAX_MEMBER_NAME_CHARS = 32
MAX_NAME_SUFFIX = 99
HEADLINE_COLUMNS = 24

#: Column thresholds for layout degradation (plan 7.3, task brief).
WIDE_COLUMNS = 60
NARROW_COLUMNS = 40

#: The console's own prompt marker. Deliberately none of the glyphs any
#: detection manifest keys on (``❯``, ``›``, ``>``), so the console pane is
#: never classified as an agent.
INPUT_PROMPT = "» "
#: Replacement for agent prompt markers inside the ``/peek`` box.
PEEK_MARKER_REPLACEMENT = "▸"

STATUS_GLYPHS = {"idle": "○", "working": "◐", "blocked": "●", "done": "✓", "unknown": "?"}
ASCII_STATUS_GLYPHS = {"idle": "o", "working": "*", "blocked": "!", "done": "+", "unknown": "?"}
KIND_GLYPHS = {"request": "→", "done": "✓", "blocked": "!", "question": "?", "direct": "»"}
ASCII_KIND_GLYPHS = {"request": ">", "done": "+", "blocked": "!", "question": "?", "direct": ">>"}

MEMBER_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}\Z")
RESERVED_NAMES = frozenset({"human", "all", "me", "none", "system", "team"})
RECIPIENT_RE = re.compile(r"^(?:[a-z][a-z0-9_-]{0,31}|role:[a-z][a-z0-9_-]{0,63}|team:[a-z][a-z0-9_-]{0,63}|all|human)\Z")
#: ``!name text`` types the line into one member now (docs/cli.md section 7, ``say``). Every input line
#: that starts with ``!`` is such an attempt and never falls back to a post, so a mistyped name can
#: never leak a one-member instruction to the whole team.
BANG_HINT = 'to post text that starts with "!" write /all !text or @name !text'
_BANG_RE = re.compile(r"^(!{1,2})([a-z])")
#: A ``direct`` entry with no ``typed`` outcome yet reads "typing" this long, then "no outcome".
SAY_NO_OUTCOME_S = 10.0
#: A say the console sent is watched for its outcome this long before the status line gives up.
SAY_WATCH_S = 30.0
#: Refusal reasons the console's ``!!`` bypasses (the status line says so).
FORCEABLE_REASONS = ("working", "muted")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_DIRECTIVE_RE = re.compile(
    r"^\s*(?:(?P<at>@\S+)|(?P<all>/all\b)|(?P<human>/human\b)|(?P<urgent>/urgent\b)|(?P<interrupt>/interrupt\b)"
    r"|/team\s+(?P<team>\S+)|/kind\s+(?P<kind>\S+)|/reply\s+(?P<reply>\S+)|/ref\s+(?P<ref>\S+))"
)
#: ``@@path`` anywhere in a post line attaches that file (``post --file``): a token at the start or after whitespace.
_FILE_TOKEN_RE = re.compile(r"(?:(?<=\s)|^)@@(\S+)")
#: Rows offered by the ``@@`` file menu at most (the menu window scrolls through them).
MAX_FILE_ROWS = 40
_SEQ_RANGE_RE = re.compile(r"seq\s+(\d+)(?:\s*-\s*(\d+))?")
_SEQ_HASH_RE = re.compile(r"#(\d+)")
_ID_NUM_RE = re.compile(r"(\d+)")


# --------------------------------------------------------------------------
# text helpers (sanitize fallbacks)


def display_width(text: str) -> int:
    """Terminal columns ``text`` occupies (``sanitize.display_width``)."""
    return int(sanitize.display_width(text))


def truncate_columns(text: str, columns: int, ellipsis: str = "…") -> str:
    """Cut ``text`` to at most ``columns`` display columns, ending with ``ellipsis`` when cut."""
    if columns <= 0:
        return ""
    if display_width(text) <= columns:
        return text
    ell_width = display_width(ellipsis)
    budget = max(0, columns - ell_width)
    out: List[str] = []
    used = 0
    for ch in text:
        w = display_width(ch)
        if used + w > budget:
            break
        out.append(ch)
        used += w
    return "".join(out) + (ellipsis if ell_width <= columns else "")


def pad_columns(text: str, columns: int) -> str:
    """Right-pad with spaces to exactly ``columns`` display columns (truncating first)."""
    text = truncate_columns(text, columns, ellipsis="")
    return text + " " * max(0, columns - display_width(text))


def strip_ansi(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    return "".join(ch for ch in text if ch == "\t" or unicodedata.category(ch) not in ("Cc", "Cf"))


def headline(text: str, columns: int = HEADLINE_COLUMNS) -> str:
    """First line of ``text`` with controls stripped, cut to ``columns``."""
    first = (text or "").replace("\r\n", "\n").split("\n", 1)[0]
    first = strip_ansi(first).replace("\t", " ").strip()
    return truncate_columns(first, columns)


def neutralize_prompt_markers(line: str) -> str:
    """Replace agent prompt markers so a copied screen never looks like an agent prompt."""
    line = line.replace("❯", PEEK_MARKER_REPLACEMENT).replace("›", PEEK_MARKER_REPLACEMENT)
    stripped = line.lstrip()
    if stripped.startswith(">"):
        indent = line[: len(line) - len(stripped)]
        line = indent + PEEK_MARKER_REPLACEMENT + stripped[1:]
    return line


# --------------------------------------------------------------------------
# time helpers (wall clock only for TTL-style labels)


def parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _now(now: Optional[datetime]) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def remaining_label(until: Any, now: Optional[datetime] = None) -> Optional[str]:
    """``7m`` / ``45s`` / ``2h`` left until ``until``; None when absent or expired."""
    parsed = parse_iso(until)
    if parsed is None:
        return None
    seconds = int((parsed - _now(now)).total_seconds())
    if seconds <= 0:
        return None
    if seconds < 60:
        return "{}s".format(seconds)
    if seconds < 3600:
        return "{}m".format(seconds // 60)
    return "{}h".format(seconds // 3600)


def age_label(since: Any, now: Optional[datetime] = None) -> Optional[str]:
    parsed = parse_iso(since)
    if parsed is None:
        return None
    seconds = max(0, int((_now(now) - parsed).total_seconds()))
    if seconds < 60:
        return "{}s".format(seconds)
    if seconds < 3600:
        return "{}m".format(seconds // 60)
    if seconds < 86400:
        return "{}h".format(seconds // 3600)
    return "{}d".format(seconds // 86400)


def clock_label(ts: Any) -> str:
    parsed = parse_iso(ts)
    if parsed is None:
        return "--:--"
    return parsed.astimezone().strftime("%H:%M")


# --------------------------------------------------------------------------
# console


@dataclass
class ConsoleHeader:
    team: str
    members: int
    view_on: bool
    nudges: str  # "on" | "paused (7m)"
    toasts: str  # effective delivery mode
    unread: int
    charter_seq: Optional[int]
    charter_headline: Optional[str]


@dataclass(frozen=True)
class ConsoleRuntime:
    """Installed/plugin and live-daemon compatibility shown above the feed."""

    plugin_version: str
    daemon_version: Optional[str] = None
    herdr_version: Optional[str] = None
    protocol: Optional[int] = None
    atomic_idle_prompt: Optional[bool] = None


@dataclass
class ConsoleModel:
    team: str
    header: ConsoleHeader
    roster_lines: List[str] = field(default_factory=list)
    feed: List[Dict[str, Any]] = field(default_factory=list)
    filter_index: int = 0
    input: str = ""
    cursor: int = 0
    #: Entries hidden *below* the window. Only meaningful while ``follow`` is False.
    scroll: int = 0
    #: True while the feed is pinned to the newest entry. Scrolling back clears it;
    #: reaching the bottom, End, Esc, posting and a filter change set it again.
    #: Without this, one Up press left the reader permanently N entries behind the
    #: tail with nothing on screen saying so.
    follow: bool = True
    status: Optional[str] = None
    focused: bool = True
    default_recipient: Optional[str] = None
    # -- extra state owned by this module (defaults keep the scaffold shape)
    width: int = 80
    height: int = 24
    ascii_only: bool = False
    runtime: Optional[ConsoleRuntime] = None
    paste_mode: bool = False
    pending_confirm: Optional["Intent"] = None
    peek: Optional[List[str]] = None
    human_label: Optional[str] = None
    members: List[Dict[str, Any]] = field(default_factory=list)
    #: Active links of this team (``links.summary`` shape), for the ``@`` menu and ``/links``.
    links: List[Dict[str, Any]] = field(default_factory=list)
    #: ``@`` mention menu: highlighted row, and the input snapshot Esc hid it for.
    mention_index: int = 0
    mention_hidden_for: Optional[str] = None
    #: Says this console sent and still awaits an outcome for: ``{direct seq: (member, monotonic sent)}``.
    watching_say: Dict[int, Tuple[str, float]] = field(default_factory=dict)
    #: Directory relative ``@@path`` tokens complete against (the console process cwd when None).
    file_base: Optional[str] = None
    #: Project roots the ``@@`` finder searches: the members' working directories (the roster's ``cwd``), most
    #: common first; empty means the console process cwd only.
    file_roots: List[str] = field(default_factory=list)


@dataclass
class Intent:
    kind: str  # post | say | retract | mute | unmute | nudge | focus | peek | who | filter | as | use | charter | remove | help | error | quit | none | create | load_file
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PostSpec:
    """A parsed post line shared by the console and the compose popup."""

    text: str
    to: List[str] = field(default_factory=list)
    kind: str = "note"
    reply_to: Optional[int] = None
    urgent: bool = False
    #: ``/interrupt``: urgent, and the notifier may type the nudge into a working recipient's turn.
    interrupt: bool = False
    refs: List[str] = field(default_factory=list)
    #: ``@@path`` tokens: referenced when the team can read them, copied into ``payloads/`` otherwise.
    files: List[str] = field(default_factory=list)

    def to_args(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "to": list(self.to),
            "kind": self.kind,
            "reply_to": self.reply_to,
            "urgent": self.urgent,
            "interrupt": self.interrupt,
            "refs": list(self.refs),
            "files": list(self.files),
        }


def extract_file_tokens(line: str) -> Tuple[str, List[str]]:
    """Pull every ``@@path`` token out of ``line``; returns the remaining text and the paths in order."""
    files: List[str] = []

    def take(match: "re.Match[str]") -> str:
        path = match.group(1)
        if path not in files:
            files.append(path)
        return ""

    rest = _FILE_TOKEN_RE.sub(take, line)
    return " ".join(rest.split()) if files else line, files


@dataclass
class SaySpec:
    """A parsed ``!member text`` / ``!!member|all text`` line: type ``text`` now (``!!`` forces)."""

    member: str
    text: str
    force: bool = False


def parse_bang(line: str) -> Tuple[Optional[SaySpec], Optional[str]]:
    """``(spec, None)`` for a say line, ``(None, error)`` for a malformed one, ``(None, None)`` when ``line`` is not one.

    Any line that starts with ``!`` is a say attempt and never becomes a post.
    The text after the name is passed verbatim (``!peer /compact``, ``!peer @x look``).
    """
    stripped = line.strip()
    if not stripped.startswith("!"):
        return None, None
    usage = "usage: !name text, !!name text, or !!all text ({})".format(BANG_HINT)
    match = _BANG_RE.match(stripped)
    if match is None:
        return None, usage
    sigil = match.group(1)
    parts = stripped[len(sigil):].split(None, 1)
    name = parts[0]
    text = parts[1].strip() if len(parts) > 1 else ""
    if name.startswith("role:"):
        return None, "!{} types into one member, not a role (post to the role with @{} text)".format(name, name)
    if name == "all" and sigil != "!!":
        return None, "!all does not broadcast direct typing; use !!all <text>, or post with /all <text>"
    if name in RESERVED_NAMES and not (name == "all" and sigil == "!!"):
        return None, "!{} is not a member; the sign types into one agent (post with @{} text)".format(name, name)
    if not MEMBER_NAME_RE.match(name):
        return None, "invalid member name {} ({})".format(name, usage)
    if not text:
        return None, "usage: !{} <text> (nothing to type)".format(name)
    if "\n" in text:
        return None, "!{} types one line; remove the newline or post it with @{} text".format(name, name)
    return SaySpec(member=name, text=text, force=sigil == "!!"), None


def member_file_roots(members: Iterable[Dict[str, Any]], home: Optional[str] = None) -> List[str]:
    """The project roots the ``@@`` finder searches: ``roster.member_roots`` (members' cwds, most common first)."""
    return roster.member_roots(members, home)


def agent_member_names(members: Iterable[Dict[str, Any]]) -> List[str]:
    """Names a ``!`` line may type into: agent members on the roster that have not left."""
    return [str(m.get("name")) for m in members if m.get("name") and m.get("name") != "human" and m.get("kind") != "human" and m.get("status") not in ("left",)]


def degrade_level(width: int) -> int:
    """0 = full layout, 1 = under 60 columns, 2 = under 40 columns."""
    if width >= WIDE_COLUMNS:
        return 0
    if width >= NARROW_COLUMNS:
        return 1
    return 2


def nudges_label(mutes: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> str:
    """``on`` or ``paused (7m)`` from ``mute.json``'s ``*`` entry."""
    if not mutes:
        return "on"
    until = mutes.get("*")
    if "*" in mutes and until is None:
        return "paused"
    left = remaining_label(until, now)
    return "paused ({})".format(left) if left else "on"


def member_muted(member: Dict[str, Any], mutes: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> bool:
    if remaining_label(member.get("muted_until"), now):
        return True
    if not mutes:
        return False
    for key in ("*", member.get("name")):
        if key in mutes:
            value = mutes.get(key)
            if value is None or remaining_label(value, now):
                return True
    return False


def unread_for_human(records: Iterable[Dict[str, Any]], cursor_seq: int) -> int:
    count = 0
    for rec in records:
        if rec.get("from") in ("human", "system"):
            continue
        if rec.get("kind") in ("retract", "system"):
            continue
        to = rec.get("to") or []
        if ("human" in to or "all" in to) and int(rec.get("seq") or 0) > cursor_seq:
            count += 1
    return count


def build_header(
    team: str,
    who: Optional[Dict[str, Any]],
    records: Iterable[Dict[str, Any]],
    human_cursor_seq: int = 0,
    mutes: Optional[Dict[str, Any]] = None,
    view_on: bool = False,
    toasts: Optional[str] = None,
    now: Optional[datetime] = None,
) -> ConsoleHeader:
    who = who or {}
    team_doc = (who.get("teams") or {}).get(team) or {}
    members = team_doc.get("members") or []
    charter = (who.get("charters") or {}).get(team) or {}
    seq = charter.get("seq")
    head = charter.get("headline")
    return ConsoleHeader(
        team=team,
        members=len(members),
        view_on=bool(view_on),
        nudges=nudges_label(mutes, now),
        toasts=toasts or who.get("toasts") or "unknown",
        unread=unread_for_human(records, human_cursor_seq),
        charter_seq=int(seq) if isinstance(seq, int) else None,
        charter_headline=head if isinstance(head, str) and head else None,
    )


def header_lines(header: ConsoleHeader, width: int = 80) -> List[str]:
    """The two header lines of plan 7.3, degraded under 60 and 40 columns."""
    level = degrade_level(width)
    members = "{} member{}".format(header.members, "" if header.members == 1 else "s")
    if level == 0:
        first = "team {} · {} · view:{} · nudges:{} · toasts:{} · unread(you):{}".format(
            header.team, members, "on" if header.view_on else "off", header.nudges, header.toasts, header.unread
        )
    elif level == 1:
        first = "team {} · {} · nudges:{} · unread(you):{}".format(header.team, members, header.nudges, header.unread)
    else:
        first = "team {} · {} · unread:{}".format(header.team, header.members, header.unread)
    if header.charter_seq is None:
        second = "charter: none"
    else:
        second = "charter #{}: {}".format(header.charter_seq, header.charter_headline or "")
    return [truncate_columns(first, width), truncate_columns(second, width)]


def runtime_from_daemon(daemon: Any, plugin_version: str) -> ConsoleRuntime:
    """Normalize the optional, backward-compatible ``daemon.json`` fields."""

    from herdr_team import capabilities

    doc = daemon if isinstance(daemon, dict) else {}
    protocol = doc.get("protocol")
    return ConsoleRuntime(
        plugin_version=plugin_version,
        daemon_version=doc.get("version") if isinstance(doc.get("version"), str) and doc.get("version") else None,
        herdr_version=doc.get("herdr_version") if isinstance(doc.get("herdr_version"), str) and doc.get("herdr_version") else None,
        protocol=int(protocol) if isinstance(protocol, int) and not isinstance(protocol, bool) else None,
        atomic_idle_prompt=capabilities.atomic_idle_prompt_of(doc),
    )


def runtime_line(runtime: ConsoleRuntime, width: int = 80) -> str:
    """One capability-first line; the safety result survives narrow clipping."""

    safe = "ready" if runtime.atomic_idle_prompt is True else "unavailable" if runtime.atomic_idle_prompt is False else "unknown"
    herdr = runtime.herdr_version or "?"
    protocol = "p{}".format(runtime.protocol) if runtime.protocol is not None else "p?"
    daemon = runtime.daemon_version or "down"
    if width >= WIDE_COLUMNS:
        line = "runtime: safe !:{} · Herdr {}/{} · Synapse {} · daemon {}".format(safe, herdr, protocol, runtime.plugin_version, daemon)
    elif width >= NARROW_COLUMNS:
        line = "safe !:{} · H {}/{} · S/D {}/{}".format(safe, herdr, protocol, runtime.plugin_version, daemon)
    else:
        line = "safe !:{} · H {}/{}".format(safe, herdr, protocol)
    return truncate_columns(line, width)


def status_glyph(status: Optional[str], ascii_only: bool = False) -> str:
    table = ASCII_STATUS_GLYPHS if ascii_only else STATUS_GLYPHS
    return table.get(status or "unknown", table["unknown"])


def kind_glyph(kind: Optional[str], ascii_only: bool = False) -> str:
    table = ASCII_KIND_GLYPHS if ascii_only else KIND_GLYPHS
    return table.get(kind or "", "")


def _session_tail(value: Any, tail: int = 8) -> Optional[str]:
    """The end of a member's harness session id: ``who.json`` carries the short form, ``team.json`` the record."""
    if isinstance(value, dict):
        value = value.get("value")
    if not isinstance(value, str) or not value:
        return None
    text = value if len(value) <= tail else value[-tail:]
    return "".join(ch for ch in text if ch.isalnum() or ch in "._:-") or None


def roster_line(
    member: Dict[str, Any],
    width: int = 80,
    ascii_only: bool = False,
    mutes: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
    show_role: bool = False,
) -> str:
    """One ``who`` line (plan 11): glyph name kind pane status "headline" ↪N muted gone <age> ...

    ``show_role`` adds the member's role after its name. The console's roster
    box leaves it out (roles live in the CLI ``who``), but the picker's team
    tree is where roles are chosen and changed, so it asks for them.
    """
    level = degrade_level(width)
    name = str(member.get("name") or "?")
    kind = str(member.get("kind") or "?")
    pane = str(member.get("pane_id") or "-")
    status = str(member.get("agent_status") or "unknown")
    roster_status = member.get("status") or "active"
    star = ("* " if ascii_only else "★ ") if member.get("manager") else ""
    fields = [status_glyph(status, ascii_only) + " " + star + name]
    if show_role and level < 2 and member.get("role"):
        fields.append(str(member["role"]))
    if level < 2:
        fields.append(kind)
        fields.append(pane)
    fields.append(status)
    head = member.get("last_headline")
    if level == 0 and head:
        fields.append('"{}"'.format(headline(str(head), HEADLINE_COLUMNS)))
    session = _session_tail(member.get("session"))
    if level == 0 and session:
        fields.append("sess " + session)
    pending = member.get("pending_nudges") or 0
    if pending:
        hold = member.get("hold")
        # Show why a queued nudge is waiting (daemon.log has the detail); observed live 2026-09-05:
        # a bare ↪3 next to an idle member read as "nothing happens" when the hold was `focused`.
        fields.append("{}{}{}".format("^" if ascii_only else "↪", pending, " ({})".format(hold) if hold else ""))
    if member.get("say") == "confirming":
        fields.append("{} typing".format(">>" if ascii_only else "»"))
    if member.get("interrupt"):
        fields.append("{}{}".format("!" if ascii_only else "⚡", member["interrupt"]))
    if member_muted(member, mutes, now):
        fields.append("muted")
    if roster_status in ("missing", "left", "unbound", "kind_changed", "name_conflict", "failed", "starting"):
        age = age_label(member.get("last_seen_at"), now)
        if roster_status in ("missing", "left") and age:
            fields.append("gone {}".format(age))
        else:
            fields.append(roster_status.replace("_", " "))
    if member.get("manager"):
        fields.append("manager")
    if level == 0 and isinstance(member.get("setting"), str) and member.get("setting"):
        fields.append("model " + str(member.get("setting")))
    if member.get("restarting"):
        fields.append("restarting")
    if member.get("briefed") is False and member.get("kind") != "human":
        fields.append("unbriefed")
    if member.get("charter_stale"):
        fields.append("charter: stale")
    if member.get("delivery") == "hooks" and level == 0:
        seen = member.get("hooks_last_seen")
        fields.append("hooks: silent since {}".format(clock_label(seen)) if seen else "hooks: silent")
    if member.get("verified_kind") is False and level == 0:
        fields.append("kind unverified")
    return truncate_columns("  ".join(fields), width)


def roster_lines_for(
    members: Iterable[Dict[str, Any]],
    width: int = 80,
    ascii_only: bool = False,
    mutes: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> List[str]:
    return [roster_line(m, width, ascii_only, mutes, now) for m in members]


# -- feed --------------------------------------------------------------------


def nudged_seqs(record: Dict[str, Any]) -> List[int]:
    """Post seqs a ``nudged`` system record refers to (``seqs``, ``reply_to``, or text)."""
    seqs: List[int] = []
    raw = record.get("seqs")
    if isinstance(raw, list):
        seqs.extend(int(s) for s in raw if isinstance(s, int))
    reply_to = record.get("reply_to")
    if isinstance(reply_to, int):
        seqs.append(reply_to)
    text = str(record.get("text") or "")
    m = _SEQ_RANGE_RE.search(text)
    if m:
        first = int(m.group(1))
        last = int(m.group(2)) if m.group(2) else first
        if last >= first and last - first < 1000:
            seqs.extend(range(first, last + 1))
    else:
        for h in _SEQ_HASH_RE.findall(text):
            seqs.append(int(h))
    out: List[int] = []
    for s in seqs:
        if s not in out:
            out.append(s)
    return out


def typed_outcomes(records: Iterable[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """``{direct seq: outcome}`` from ``typed`` system records (docs/cli.md section 7); the newest wins."""
    out: Dict[int, Dict[str, Any]] = {}
    for rec in records:
        if rec.get("from") != "system" or rec.get("kind") != "system" or rec.get("event") != "typed":
            continue
        outcome = {
            "result": rec.get("result"), "reason": rec.get("reason"), "detail": rec.get("detail"), "member": rec.get("member"),
            "kind": rec.get("kind_of_member"), "force": rec.get("force"), "force_verified": rec.get("force_verified"),
            "ts": rec.get("ts"), "seq": rec.get("seq"),
        }
        for s in nudged_seqs(rec):
            out[s] = outcome
    return out


_TYPED_REFUSED_LABELS = {
    "working": "not typed (working)", "muted": "not typed (muted)", "blocked": "blocked", "dialog": "not typed (dialog)",
    "draft": "not typed (draft)", "skip_state_update": "not typed (overlay open)", "unknown": "not typed (state unknown)",
    "not_ready": "not typed (not ready)", "absent": "not typed (absent)", "wrong_occupant": "not typed (absent)",
    "wrong_target": "not typed (absent)", "in_flight": "not typed (busy)", "stale": "not typed (stale job)",
    "unverified_source": "refused (unverified source)", "member_not_found": "not typed (no such member)",
    "kind_unverified": "not typed (kind not trusted)", "state_changed": "not typed (state changed; retry)",
    # ``update_required`` remains readable for board history written by 0.15.5.
    "update_required": "not typed (update Herdr)",
    "capability_unavailable": "safe ! unavailable; use @name",
}


def typed_label(outcome: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """``(ok, label)`` for a ``typed`` outcome; the vocabulary shared by the feed tag and the status line."""
    if not outcome:
        return False, "failed (unknown)"
    result = str(outcome.get("result") or "")
    reason = outcome.get("reason")
    reason_s = headline(str(reason), 24) if reason else ""
    if result == "typed":
        if reason == "in_turn":
            if outcome.get("force_verified") is False and outcome.get("kind"):
                return True, "typed (in running turn, unverified for {})".format(headline(str(outcome["kind"]), 16))
            return True, "typed (in running turn)"
        if reason == "dry":
            return True, "typed (dry run)"
        return True, "typed"
    if result == "refused":
        return False, _TYPED_REFUSED_LABELS.get(reason_s, "not typed ({})".format(reason_s or "refused"))
    if result == "not_submitted":
        return False, "not submitted"
    if result == "failed" and reason == "hung":
        return False, "failed (pane hung)"
    if result == "failed" and reason == "unconfirmed":
        return False, "unconfirmed"
    return False, "failed ({})".format(headline(str(outcome.get("detail") or reason_s or result or "unknown"), 24))


def typed_tag(outcome: Optional[Dict[str, Any]], ascii_only: bool = False) -> str:
    ok, label = typed_label(outcome)
    if ok:
        return ("+" if ascii_only else "✓") + label
    return ("x " if ascii_only else "✗ ") + label


def pending_say_tag(record: Dict[str, Any], now: Optional[datetime] = None, ascii_only: bool = False) -> str:
    """The tag of a ``direct`` entry with no outcome yet: typing, or after SAY_NO_OUTCOME_S no outcome."""
    parsed = parse_iso(record.get("ts"))
    age = (_now(now) - parsed).total_seconds() if parsed is not None else 0.0
    if age < SAY_NO_OUTCOME_S:
        return "... typing" if ascii_only else "… typing"
    return "{}no outcome (notifier?)".format("x " if ascii_only else "✗ ")


def derive_receipts(
    records: Iterable[Dict[str, Any]],
    cursors: Dict[str, Dict[str, Any]],
    members: Iterable[Dict[str, Any]],
) -> Dict[int, Dict[str, Any]]:
    """``{seq: {"nudged": [ts...], "read": [names], "read_by": "k/n"|None, "typed": outcome|None}}``.

    A ``direct`` line gets no read or nudge receipts, only its ``typed``
    outcome (docs/cli.md section 7).

    ``nudged`` comes from board ``system`` records, ``read`` from cursors,
    ``read_by k/n`` for posts to ``all`` counted over agent members other
    than the author. Hook peeks never advance cursors, so they never count.
    """
    recs = list(records)
    agent_names = [str(m.get("name")) for m in members if m.get("kind") not in ("human", None) and m.get("name")]
    cursor_seq: Dict[str, int] = {}
    for reader, doc in (cursors or {}).items():
        try:
            cursor_seq[str(reader)] = int((doc or {}).get("seq") or 0)
        except (TypeError, ValueError):
            cursor_seq[str(reader)] = 0
    nudged: Dict[int, List[str]] = {}
    interrupted = set()
    for rec in recs:
        if rec.get("from") == "system" and rec.get("kind") == "system" and rec.get("event") == "nudged":
            for s in nudged_seqs(rec):
                nudged.setdefault(s, []).append(str(rec.get("ts") or ""))
                if rec.get("interrupt"):
                    interrupted.add(s)
    typed = typed_outcomes(recs)
    receipts: Dict[int, Dict[str, Any]] = {}
    for rec in recs:
        if rec.get("kind") in ("system", "retract"):
            continue
        seq = rec.get("seq")
        if not isinstance(seq, int):
            continue
        if rec.get("kind") == "direct":
            receipts[seq] = {"nudged": [], "read": [], "read_by": None, "typed": typed.get(seq)}
            continue
        to = [str(t) for t in (rec.get("to") or [])]
        author = str(rec.get("from") or "")
        if "all" in to:
            audience = [n for n in agent_names if n != author]
        else:
            audience = [n for n in to if n not in ("human",) and n != author]
        read = [n for n in audience if cursor_seq.get(n, 0) >= seq]
        # human readers: any human@<label> cursor at or past the seq
        if "human" in to or "all" in to:
            for reader, cseq in cursor_seq.items():
                if reader.startswith("human@") and cseq >= seq:
                    read.append("human")
                    break
        entry: Dict[str, Any] = {"nudged": list(nudged.get(seq, [])), "read": read, "read_by": None, "typed": None, "interrupted": seq in interrupted}
        if "all" in to:
            entry["read_by"] = "{}/{}".format(len([n for n in read if n != "human"]), len(audience))
        receipts[seq] = entry
    # Across a link the reader is on the other board; its notifier sends a
    # ``link_read`` receipt back here, naming the message id and the reader.
    read_across: Dict[str, str] = {}
    for rec in recs:
        if rec.get("from") == "system" and rec.get("kind") == "system" and rec.get("event") == "link_read" and isinstance(rec.get("link_id"), str):
            read_across[rec["link_id"]] = str(rec.get("reader") or "?")
    if read_across:
        for rec in recs:
            link = rec.get("link")
            seq = rec.get("seq")
            if isinstance(link, dict) and link.get("mirror") and link.get("id") in read_across and isinstance(seq, int) and seq in receipts:
                receipts[seq]["read_by"] = read_across[link["id"]]
                receipts[seq]["read"] = [read_across[link["id"]]]
    return receipts


def retraction_map(records: Iterable[Dict[str, Any]]) -> Dict[int, int]:
    """``{retracted_seq: retract_record_seq}``."""
    out: Dict[int, int] = {}
    for rec in records:
        target = rec.get("retracts")
        if rec.get("kind") == "retract" and isinstance(target, int):
            out[target] = int(rec.get("seq") or 0)
    return out


def _to_label(record: Dict[str, Any]) -> str:
    to_role = record.get("to_role")
    if to_role:
        return "role:{}".format(to_role)
    to = record.get("to") or []
    return ",".join(str(t) for t in to) if to else "-"


def _author_label(record: Dict[str, Any]) -> str:
    author = str(record.get("from") or "?")
    from_team = record.get("from_team")
    if isinstance(from_team, str) and from_team and isinstance(record.get("link"), dict) and not record["link"].get("mirror"):
        author = "{}/{}".format(from_team, author)  # a linked team's manager, named with its team
    origin = record.get("origin") or {}
    via = origin.get("via")
    label = record.get("from_label")
    if author == "human" and label:
        author = "human ({})".format(label)
    if record.get("relayed_for"):
        author = "{} (relaying for {})".format(author, record.get("relayed_for"))
    unverified = False
    if author.startswith("human") and via not in ("console", "popup", "outside", "cli") and not origin.get("verified"):
        unverified = True
    if author.startswith("human") and via == "cli" and not origin.get("verified"):
        unverified = True
    if record.get("from") == "system" and record.get("kind") != "system":
        unverified = True
    if record.get("from") not in ("human", "system") and origin and origin.get("verified") is False:
        unverified = True
    return author + (" (unverified)" if unverified else "")


def feed_entry(
    record: Dict[str, Any],
    receipts: Optional[Dict[int, Dict[str, Any]]] = None,
    retractions: Optional[Dict[int, int]] = None,
    ascii_only: bool = False,
    width: int = 80,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """One rendered feed line plus the facts the filters need."""
    seq = record.get("seq")
    kind = str(record.get("kind") or "note")
    author = str(record.get("from") or "?")
    text = headline(str(record.get("text") or ""), 10_000)
    full_text = clean_text(str(record.get("text") or ""))
    struck_by = (retractions or {}).get(seq) if isinstance(seq, int) else None
    level = degrade_level(width)
    head = ""
    tail = ""
    if kind == "system":
        head = "#{} {} system {}:".format(seq, clock_label(record.get("ts")), record.get("event") or "event")
        line = "{} {}".format(head, text)
    else:
        glyph = kind_glyph(kind, ascii_only)
        parts = ["#{}".format(seq)]
        if level == 0:
            parts.append(clock_label(record.get("ts")))
        if isinstance(record.get("link"), dict):
            parts.append("<->" if ascii_only else "⇄")
        parts.append("{}{}{}".format(_author_label(record), "->" if ascii_only else "→", _to_label(record)))
        if kind != "note":
            parts.append("{}{}".format(glyph, kind) if glyph else kind)
        if record.get("reply_to") is not None:
            parts.append("re#{}".format(record.get("reply_to")))
        if record.get("interrupt"):
            parts.append("{}INTERRUPT".format("!" if ascii_only else "⚡"))
        elif record.get("urgent"):
            parts.append("URGENT")
        head = " ".join(parts)
        body = text
        if struck_by is not None:
            body = "~~{}~~ (retracted by #{})".format(text, struck_by)
            full_text = "~~{}~~ (retracted by #{})".format(full_text, struck_by)
        parts.append(body)
        line = " ".join(parts)
        rec_receipts = (receipts or {}).get(seq) if isinstance(seq, int) else None
        if rec_receipts and struck_by is None and level == 0:
            tags: List[str] = []
            if kind == "direct":
                outcome = rec_receipts.get("typed")
                tags.append(typed_tag(outcome, ascii_only) if outcome else pending_say_tag(record, now, ascii_only))
            if rec_receipts.get("interrupted"):
                tags.append("{}interrupted".format("!" if ascii_only else "⚡"))
            elif rec_receipts.get("nudged"):
                tags.append("{}nudged".format("+" if ascii_only else "✓"))
            if rec_receipts.get("read_by") is not None:
                tags.append("read by {}".format(rec_receipts["read_by"]))
            elif rec_receipts.get("read"):
                tags.append("{}read".format("+" if ascii_only else "✓"))
            if tags:
                tail = " ".join(tags)
                line = "{}  {}".format(line, tail)
    return {
        "seq": seq,
        "line": truncate_columns(line, width),
        "head": head,
        "text": full_text,
        "tail": tail,
        "struck": struck_by is not None,
        "kind": kind,
        "from": author,
        "to": [str(t) for t in (record.get("to") or [])],
        "link": record.get("link") if isinstance(record.get("link"), dict) else None,
        "from_team": record.get("from_team") if isinstance(record.get("from_team"), str) else None,
        "record": record,
    }


def clean_text(text: str) -> str:
    """Post text for display: controls and escapes stripped, tabs to spaces, newlines kept."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [strip_ansi(part).replace("\t", " ").rstrip() for part in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def wrap_columns(text: str, first_width: int, rest_width: Optional[int] = None) -> List[str]:
    """Word-aware wrap by display columns; explicit newlines start a new row; long words hard-break.

    The first row may have a different budget (the header shares it); every
    later row gets ``rest_width``. Always returns at least one row.
    """
    rest_width = first_width if rest_width is None else rest_width
    rows: List[str] = []
    for paragraph in (text or "").split("\n"):
        budget = first_width if not rows else rest_width
        budget = max(1, budget)
        if paragraph == "":
            rows.append("")
            first_width = rest_width
            continue
        words = paragraph.split(" ")
        current = ""
        for word in words:
            if not word and current == "":
                # leading or repeated spaces: keep one so indentation survives roughly
                candidate = " "
            else:
                candidate = word if current == "" else current + " " + word
            if display_width(candidate) <= budget:
                current = candidate
                continue
            if current:
                rows.append(current)
                budget = max(1, rest_width)
                current = ""
            # the word alone: hard-break if it does not fit the row budget
            while display_width(word) > budget:
                cut = ""
                for ch in word:
                    if display_width(cut + ch) > budget:
                        break
                    cut += ch
                if not cut:
                    cut = word[0]
                rows.append(cut)
                word = word[len(cut):]
                budget = max(1, rest_width)
            current = word
        rows.append(current)
        first_width = rest_width
    return rows or [""]


#: Continuation rows of a wrapped feed entry are indented by this much.
WRAP_INDENT = "    "


def entry_rows(entry: Dict[str, Any], width: int) -> List[str]:
    """Screen rows for one feed entry: header plus wrapped text, continuation rows indented, receipts last."""
    width = max(1, width)
    head = entry.get("head")
    if head is None or "text" not in entry:
        return [truncate_columns(str(entry.get("line") or ""), width)]
    head = truncate_columns(str(head), width)
    text = str(entry.get("text") or "")
    tail = str(entry.get("tail") or "")
    if not text:
        rows = [head]
    else:
        first_budget = width - display_width(head) - 1
        if first_budget < max(8, width // 4):
            # header takes the row; text starts on the next one
            rows = [head] + [WRAP_INDENT + chunk for chunk in wrap_columns(text, width - len(WRAP_INDENT))]
        else:
            chunks = wrap_columns(text, first_budget, width - len(WRAP_INDENT))
            rows = [head + " " + chunks[0]] + [WRAP_INDENT + chunk for chunk in chunks[1:]]
    if tail:
        joined = "{}  {}".format(rows[-1], tail)
        if display_width(joined) <= width:
            rows[-1] = joined
        else:
            rows.append(truncate_columns(WRAP_INDENT + tail, width))
    return [truncate_columns(row, width) for row in rows]


def _follow_tail(model: ConsoleModel) -> None:
    """Pin the feed back to the newest entry."""
    model.follow = True
    model.scroll = 0


def feed_gap_line(below: int, width: int, ascii_only: bool = False) -> str:
    """The rule that says the feed is not following, and how to get back.

    Rendered only while scrolled, so an unscrolled screen is unchanged.
    """
    dash = "-" if ascii_only else "\u2500"
    label = "{} newer below".format(below) if below != 1 else "1 newer below"
    if width >= 46:
        text = "{} {} {} End returns to the latest ".format(dash * 2, label, "-" if ascii_only else "\u00b7")
    else:
        text = "{} {} (End) ".format(dash * 2, label)
    pad = max(0, width - display_width(text))
    return truncate_columns(text + dash * pad, width)


def visible_feed_rows(model: ConsoleModel, height: int) -> List[Tuple[str, Dict[str, Any]]]:
    """The last ``height`` screen rows of the filtered feed, ``model.scroll`` entries above the tail.

    Entries wrap over several rows; the window is filled from the bottom, so
    the topmost entry may show only its last rows. ``scroll`` counts entries
    and is clamped so the first entry can always be reached.
    """
    entries = filtered_feed(model)
    if height <= 0 or not entries:
        return []
    max_scroll = max(0, len(entries) - 1)
    scroll = 0 if model.follow else min(max(0, model.scroll), max_scroll)
    model.scroll = scroll
    if scroll == 0:
        model.follow = True  # clamped to the tail means following again, never a phantom offset
    end = len(entries) - scroll
    width = max(1, model.width)
    rows: List[Tuple[str, Dict[str, Any]]] = []
    for entry in reversed(entries[:end]):
        chunk = [(row, entry) for row in entry_rows(entry, width)]
        rows = chunk + rows
        if len(rows) >= height:
            break
    return rows[-height:]


def build_feed(
    records: Iterable[Dict[str, Any]],
    cursors: Optional[Dict[str, Dict[str, Any]]] = None,
    members: Optional[Iterable[Dict[str, Any]]] = None,
    ascii_only: bool = False,
    width: int = 80,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    recs = sorted((r for r in records if isinstance(r.get("seq"), int)), key=lambda r: r["seq"])
    receipts = derive_receipts(recs, cursors or {}, list(members or []))
    retractions = retraction_map(recs)
    return [feed_entry(r, receipts, retractions, ascii_only, width, now) for r in recs if r.get("kind") != "retract"]


#: ``audit.jsonl`` events the console surfaces as warnings (plan 7.1: ``--as human`` from an agent
#: pane is refused, audited, and shown in the console; a forged ``HERDR_PANE_ID`` likewise).
AUDIT_WARNING_EVENTS = ("author_mismatch", "pane_mismatch")


def audit_warning_entry(entry: Dict[str, Any], ascii_only: bool = False, width: int = 80) -> Optional[Dict[str, Any]]:
    """A feed entry for one ``audit.jsonl`` line, None when the event is not a warning."""
    event = entry.get("event")
    if event not in AUDIT_WARNING_EVENTS:
        return None
    ts = str(entry.get("ts") or "")
    clock = clock_label(ts) if ts else "--:--"
    author = sanitize.sanitize_text(str(entry.get("author") or "?"), max_len=64)
    details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
    if event == "author_mismatch":
        what = "tried to post as {}".format(sanitize.sanitize_text(str(details.get("requested") or "human"), max_len=32))
    else:
        what = "claimed pane {}".format(sanitize.sanitize_text(str(details.get("claimed_pane_id") or entry.get("pane_id") or "?"), max_len=32))
    where = sanitize.sanitize_text(str(entry.get("pane_id") or "?"), max_len=32)
    glyph = "!" if ascii_only else "⚠"
    line = "{} {} warning: {} {} ({}, {})".format(glyph, clock, author, what, event, where)
    return {"seq": None, "ts": ts, "line": truncate_columns(line, width), "struck": False, "kind": "warning", "from": author, "to": [], "record": entry, "audit": True}


def merge_audit_warnings(feed: List[Dict[str, Any]], audit: Optional[Iterable[Dict[str, Any]]], ascii_only: bool = False, width: int = 80) -> List[Dict[str, Any]]:
    """Insert audit warnings into ``feed`` by timestamp (board entries keep their seq order)."""
    warnings = [w for w in (audit_warning_entry(e, ascii_only, width) for e in (audit or []) if isinstance(e, dict)) if w is not None]
    if not warnings:
        return feed
    out = list(feed)
    for warning in warnings:
        index = len(out)
        for i, entry in enumerate(out):
            rec_ts = str((entry.get("record") or {}).get("ts") or "")
            if rec_ts and warning["ts"] and rec_ts > warning["ts"]:
                index = i
                break
        out.insert(index, warning)
    return out


def build_console_model(
    team: str,
    who: Optional[Dict[str, Any]],
    records: Iterable[Dict[str, Any]],
    cursors: Optional[Dict[str, Dict[str, Any]]] = None,
    mutes: Optional[Dict[str, Any]] = None,
    console_json: Optional[Dict[str, Any]] = None,
    width: int = 80,
    height: int = 24,
    view_on: bool = False,
    toasts: Optional[str] = None,
    human_label: Optional[str] = None,
    ascii_only: bool = False,
    now: Optional[datetime] = None,
    previous: Optional[ConsoleModel] = None,
    audit: Optional[Iterable[Dict[str, Any]]] = None,
    runtime: Optional[ConsoleRuntime] = None,
) -> ConsoleModel:
    """Assemble the whole console state from files; ``previous`` keeps input, scroll, filter."""
    recs = list(records)
    who = who or {}
    console_json = console_json or {}
    label = human_label or console_json.get("human_label") or "human"
    cursors = cursors or {}
    human_cursor = 0
    for reader, doc in cursors.items():
        if reader == "human@{}".format(label):
            try:
                human_cursor = int((doc or {}).get("seq") or 0)
            except (TypeError, ValueError):
                human_cursor = 0
    header = build_header(team, who, recs, human_cursor, mutes, view_on, toasts, now)
    members = list(((who.get("teams") or {}).get(team) or {}).get("members") or [])
    model = ConsoleModel(
        team=team,
        header=header,
        roster_lines=roster_lines_for(members, width, ascii_only, mutes, now),
        feed=merge_audit_warnings(build_feed(recs, cursors, members, ascii_only, width, now), audit, ascii_only, width),
        width=width,
        height=height,
        ascii_only=ascii_only,
        runtime=runtime,
        human_label=label,
        members=members,
        default_recipient=(previous.default_recipient if previous else None),
    )
    if previous is not None:
        model.filter_index = previous.filter_index
        model.input = previous.input
        model.cursor = previous.cursor
        model.follow = previous.follow
        if previous.follow:
            model.scroll = 0
        else:
            # Keep what the reader is looking at still and grow the count below.
            # Copying ``scroll`` verbatim made the window advance with every new
            # record, which is how the console sat on #64 while the board was at #68.
            grew = len(filtered_feed(model)) - len(filtered_feed(previous))
            model.scroll = previous.scroll + max(0, grew)
        model.status = previous.status
        model.focused = previous.focused
        model.paste_mode = previous.paste_mode
        model.pending_confirm = previous.pending_confirm
        model.peek = previous.peek
        model.watching_say = previous.watching_say
    return model


def filter_matches(entry: Dict[str, Any], filter_name: str) -> bool:
    if filter_name == "all":
        return True
    if filter_name == "to me":
        return "human" in entry.get("to", []) or "all" in entry.get("to", [])
    if filter_name == "requests":
        return entry.get("kind") in REQUEST_KINDS
    if filter_name == "human":
        return entry.get("from") == "human"
    if filter_name == "system":
        return entry.get("kind") in ("system", "warning")
    if filter_name == "teams":
        # the inter-team lens: what crossed a link, either way
        return isinstance(entry.get("link"), dict)
    if filter_name == "team":
        return not isinstance(entry.get("link"), dict)
    return True


def filtered_feed(model: ConsoleModel) -> List[Dict[str, Any]]:
    name = FILTERS[model.filter_index % len(FILTERS)]
    return [e for e in model.feed if filter_matches(e, name)]


def visible_feed(model: ConsoleModel, height: int) -> List[Dict[str, Any]]:
    """The window of ``height`` entries ending ``model.scroll`` entries above the tail."""
    entries = filtered_feed(model)
    if height <= 0 or not entries:
        return []
    max_scroll = max(0, len(entries) - height)
    scroll = 0 if model.follow else min(max(0, model.scroll), max_scroll)
    model.scroll = scroll
    if scroll == 0:
        model.follow = True
    end = len(entries) - scroll
    start = max(0, end - height)
    return entries[start:end]


def filter_line(model: ConsoleModel) -> str:
    parts = []
    for i, name in enumerate(FILTERS):
        parts.append("[{}]".format(name) if i == model.filter_index % len(FILTERS) else name)
    return "filter: " + "  ".join(parts) + "  (Tab cycles)   ? help"


# -- input editing -------------------------------------------------------------


def _insert(model: Any, text: str) -> None:
    model.input = model.input[: model.cursor] + text + model.input[model.cursor :]
    model.cursor += len(text)


def edit_key(model: Any, key: str) -> bool:
    """Apply a line-editing key to any model with ``input``/``cursor``; True when consumed."""
    if key == "BACKSPACE":
        if model.cursor > 0:
            model.input = model.input[: model.cursor - 1] + model.input[model.cursor :]
            model.cursor -= 1
        return True
    if key == "DELETE":
        model.input = model.input[: model.cursor] + model.input[model.cursor + 1 :]
        return True
    if key == "LEFT":
        model.cursor = max(0, model.cursor - 1)
        return True
    if key == "RIGHT":
        model.cursor = min(len(model.input), model.cursor + 1)
        return True
    if key in ("HOME", "CTRL_A"):
        model.cursor = 0
        return True
    if key in ("END", "CTRL_E"):
        model.cursor = len(model.input)
        return True
    if key == "CTRL_U":
        model.input = ""
        model.cursor = 0
        return True
    if key == "CTRL_K":
        model.input = model.input[: model.cursor]
        return True
    if key == "ALT_ENTER":
        _insert(model, "\n")
        return True
    if len(key) == 1 and (key.isprintable() or key == "\t"):
        _insert(model, key)
        return True
    if key == "PASTE_START":
        model.paste_mode = True
        return True
    if key == "PASTE_END":
        model.paste_mode = False
        return True
    if key == "ENTER" and getattr(model, "paste_mode", False):
        _insert(model, "\n")
        return True
    return False


# -- @ mention menu -------------------------------------------------------------

#: Rows shown at once in the ``@`` menu; the highlight scrolls inside the window.
MENTION_MENU_ROWS = 6
MENTION_MARKER = "▸"
MENTION_MARKER_ASCII = ">"


def mention_context(text: str, cursor: int) -> Optional[Tuple[int, int, str, str]]:
    """The ``@token``, ``!token``, or ``!!token`` under the cursor as ``(start, end, prefix, sigil)``, or None.

    An ``@`` token must begin at the start of the line or after whitespace
    and contain no whitespace up to the cursor, so an e-mail address or
    ``foo@bar`` typed mid-word never opens the menu. A ``!`` or ``!!`` token
    opens it only as the head of the line (a say line, docs/cli.md section 7)
    and only when what follows could be a member name, so ``wow!``,
    ``hello !re``, ``!!!x`` and ``!Name`` never do.
    """
    cursor = max(0, min(cursor, len(text)))
    start = cursor
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    if start >= cursor:
        return None
    if start > 0 and not text[start - 1].isspace():
        return None
    token = text[start:cursor]
    if token.startswith("@@"):
        return start, cursor, token[2:], "@@"
    if token.startswith("@"):
        return start, cursor, token[1:], "@"
    if token.startswith("/") and text[:start].strip() == "":
        # Only as the head of the line: ``a/b`` and ``see /tmp`` are post text.
        return start, cursor, token[1:], "/"
    if token.startswith("!") and text[:start].strip() == "":
        sigil = "!!" if token.startswith("!!") else "!"
        prefix = token[len(sigil):]
        if prefix and not ("a" <= prefix[0] <= "z"):
            return None
        return start, cursor, prefix, sigil
    return None


def _size_label(path: Path) -> str:
    try:
        size = float(path.stat().st_size)
    except OSError:
        return "?"
    if size < 1024:
        return "{}B".format(int(size))
    for unit in ("KB", "MB", "GB"):
        size /= 1024.0
        if size < 1024 or unit == "GB":
            return "{:.1f}{}".format(size, unit)
    return "?"


#: Directories the ``@@`` finder never walks into.
FILE_INDEX_SKIP_DIRS = frozenset({"node_modules", "target", "__pycache__", ".venv", "venv", "dist", "build", ".git", ".hg", ".svn", ".tox", ".mypy_cache", ".pytest_cache", ".idea", ".vscode"})
#: Entries indexed per root at most, and how long an index is reused before it is rebuilt.
FILE_INDEX_LIMIT = 5000
FILE_INDEX_TTL_S = 15.0
_FILE_INDEX_CACHE: Dict[str, Tuple[float, List[Tuple[str, bool]]]] = {}


def file_index(root: str, now: Optional[float] = None, limit: int = FILE_INDEX_LIMIT) -> List[Tuple[str, bool]]:
    """``(relative path, is_dir)`` for everything under ``root`` (dot-entries and build dirs skipped), cached ``FILE_INDEX_TTL_S``."""
    import time as _time

    now = _time.monotonic() if now is None else now
    cached = _FILE_INDEX_CACHE.get(root)
    if cached is not None and now - cached[0] < FILE_INDEX_TTL_S:
        return cached[1]
    entries: List[Tuple[str, bool]] = []
    base = Path(root)
    try:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in FILE_INDEX_SKIP_DIRS)
            rel_dir = os.path.relpath(dirpath, base)
            prefix = "" if rel_dir == "." else rel_dir + "/"
            for name in dirnames:
                entries.append((prefix + name + "/", True))
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                entries.append((prefix + name, False))
            if len(entries) >= limit:
                break
    except OSError:
        entries = []
    entries = entries[:limit]
    _FILE_INDEX_CACHE[root] = (now, entries)
    return entries


def _match_rank(needle: str, rel: str) -> Optional[int]:
    """0 name prefix, 1 a path segment prefix, 2 substring of the path, 3 in-order letters of the name; None when unrelated.

    The in-order match looks at the file or folder name only and needs three
    letters: over a whole path it matched almost everything (``readme`` found
    ``tests/fixtures/detection/claude_model_picker.txt``).
    """
    low = rel.lower().rstrip("/")
    base = low.rsplit("/", 1)[-1]
    if base.startswith(needle):
        return 0
    if any(seg.startswith(needle) for seg in low.split("/")):
        return 1
    if needle in low or needle in rel.lower():  # the second form lets ``rules/`` match the directory entry itself
        return 2
    if len(needle) >= 3:
        it = iter(base)
        if all(ch in it for ch in needle):
            return 3
    return None


def file_candidates(prefix: str, roots: Optional[List[str]] = None, base_dir: Optional[str] = None) -> List[Dict[str, str]]:
    """Menu rows for ``@@<prefix>``: a project-wide finder over ``roots``, or plain path completion for explicit paths.

    No prefix lists the first root's top level (directories first). A bare
    word searches every root recursively (``_match_rank`` order, shorter
    paths first). Anything that looks like a path (``~``, ``/``, ``./``, a
    slash inside) completes as a path relative to the first root. Rows carry
    the path to insert: relative when it lives under the console's own cwd,
    absolute otherwise so ``post --file`` finds it (and references a file
    under a member's cwd instead of copying it).
    """
    roots = [r for r in (roots or []) if r] or [base_dir or os.getcwd()]
    here = os.path.realpath(base_dir or os.getcwd())
    anchored = prefix.startswith(("~", "/", "./", "../"))
    if anchored or not prefix:
        return path_candidates(prefix, roots[0])
    if "/" in prefix:
        rows = path_candidates(prefix, roots[0])
        if rows:
            return rows
        # ``rules/ba`` typed from deeper in the tree: the search below matches it anywhere in a path
    needle = prefix.lower()
    # The team's project (the first root) answers alone; the other roots are consulted only when it has no match,
    # so an agent that happens to sit in another directory does not mix its files into the list.
    for index, root in enumerate(roots):
        ranked: List[Tuple[int, int, str, Dict[str, str]]] = []
        for rel, is_dir in file_index(root):
            rank = _match_rank(needle, rel)
            if rank is None:
                continue
            insert = _insert_path(root, rel, here, primary=index == 0)
            label = "{}  {}".format(rel, "dir" if is_dir else _size_label(Path(root) / rel))
            if index > 0:
                label += "  in {}".format(os.path.basename(root.rstrip("/")) or root)
            ranked.append((rank, len(rel), rel.lower(), {"insert": insert, "label": label}))
        if ranked:
            ranked.sort(key=lambda item: item[:3])  # best match, then the shorter path
            return [row for *_, row in ranked[:MAX_FILE_ROWS]]
    return []


def _insert_path(root: str, rel: str, here: str, primary: bool = True) -> str:
    """The token to insert for ``rel`` under ``root``.

    Relative for the first root and for the console's own cwd (``post --file``
    resolves a relative path against its cwd, then every member's cwd, so the
    short form is enough); absolute for a secondary root, where the same
    relative path could exist in two projects.
    """
    if rel.startswith(("~", "/")):
        return rel
    try:
        if primary or os.path.realpath(root) == here:
            return rel
    except OSError:
        pass
    return os.path.join(root, rel)


def path_candidates(prefix: str, base_dir: Optional[str] = None) -> List[Dict[str, str]]:
    """Menu rows for ``@@<prefix>``: directories (ending in ``/``) then files completing the path.

    Relative paths complete against ``base_dir`` (the console process cwd by
    default); ``~`` expands. Dot-files stay hidden unless the typed name
    starts with a dot. Listing errors yield no rows.
    """
    expanded = os.path.expanduser(prefix)
    base = Path(base_dir) if base_dir else Path.cwd()
    if prefix == "" or prefix.endswith("/"):
        directory, stem = (expanded or "."), ""
        typed_dir = prefix
    else:
        directory = os.path.dirname(expanded) or "."
        stem = os.path.basename(expanded)
        typed_dir = prefix[: len(prefix) - len(os.path.basename(prefix))]
    root = Path(directory) if os.path.isabs(directory) else base / directory
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    rows: List[Tuple[int, str, Dict[str, str]]] = []
    for name in names:
        if name.startswith(".") and not stem.startswith("."):
            continue
        if not stem and name in FILE_INDEX_SKIP_DIRS:
            continue  # a bare listing hides build and dependency dirs; typing their name still finds them
        if not name.lower().startswith(stem.lower()):
            continue
        target = root / name
        is_dir = target.is_dir()
        insert = typed_dir + name + ("/" if is_dir else "")
        label = "{}  {}".format(insert, "dir" if is_dir else _size_label(target))
        rows.append((0 if is_dir else 1, name.lower(), {"insert": insert, "label": label}))
    rows.sort(key=lambda item: (item[0], item[1]))
    return [row for _, _, row in rows[:MAX_FILE_ROWS]]


def mention_sigil(model: Any) -> str:
    """The sigil of the open menu (``@``, ``!``, or ``!!``); ``@`` when none is open."""
    ctx = mention_context(model.input, model.cursor)
    return ctx[3] if ctx is not None else "@"


def mention_candidates(members: Iterable[Dict[str, Any]], prefix: str = "", members_only: bool = False, links: Optional[Iterable[Dict[str, Any]]] = None,
                       force_all: bool = False) -> List[Dict[str, str]]:
    """Menu rows for ``@<prefix>``: members, then ``role:<r>`` groups, then ``all`` and ``human``.

    Matching is case-insensitive; a prefix match on the inserted text sorts
    before a substring match on the name or role, so ``@rev`` finds
    ``red-dev-codex-reviewer`` through its role even though the name does not
    start with it. ``members_only`` (the ``!`` menu) drops the group rows.
    ``force_all`` adds the one deliberate exception used by the ``!!`` menu:
    ``!!all`` fans out into independently verified direct deliveries.
    """
    needle = prefix.lower()
    rows: List[Tuple[int, int, Dict[str, str]]] = []
    roles: Dict[str, int] = {}
    order = 0
    for member in members:
        name = str(member.get("name") or "")
        if not name or member.get("kind") == "human" or member.get("status") in ("left",):
            continue
        role = str(member.get("role") or "")
        if role:
            roles[role] = roles.get(role, 0) + 1
        status = str(member.get("agent_status") or member.get("status") or "")
        label = "  ".join(part for part in (name, " · ".join(p for p in (role, str(member.get("kind") or ""), status) if p)) if part)
        rank = _mention_rank(needle, name, role)
        if rank is not None:
            rows.append((rank, order, {"insert": name, "label": label}))
        order += 1
    if members_only and force_all:
        rank = _mention_rank(needle, "all", "")
        if rank is not None:
            rows.append((rank, order, {"insert": "all", "label": "all  every agent (forced direct typing)"}))
            order += 1
    if not members_only:
        for role, count in roles.items():
            insert = "role:" + role
            rank = _mention_rank(needle, insert, role)
            if rank is not None:
                rows.append((rank, order, {"insert": insert, "label": "{}  everyone with role {} ({})".format(insert, role, count)}))
            order += 1
        for insert, label in (("all", "all  everyone on the team"), ("human", "human  the operator (you)")):
            rank = _mention_rank(needle, insert, "")
            if rank is not None:
                rows.append((rank, order, {"insert": insert, "label": label}))
            order += 1
        for link in links or []:
            if not isinstance(link, dict) or link.get("state") != "active" or not link.get("team"):
                continue
            insert = "team:" + str(link["team"])
            rank = _mention_rank(needle, insert, str(link["team"]))
            if rank is not None:
                rows.append((rank, order, {"insert": insert, "label": "{}  the manager of team {} ({})".format(insert, link["team"], link.get("manager") or "?")}))
            order += 1
    rows.sort(key=lambda item: (item[0], item[1]))
    return [row for _, _, row in rows]


def slash_candidates(prefix: str = "", commands: Optional[Sequence[str]] = None) -> List[Dict[str, str]]:
    """Menu rows for ``/<prefix>``: every console command with its placeholder.

    A prefix match sorts before a substring match, so ``/ex`` puts ``/export``
    first while ``/port`` still finds it.
    """
    needle = prefix.lower()
    rows: List[Tuple[int, int, Dict[str, str]]] = []
    for order, command in enumerate(commands if commands is not None else SLASH_COMMANDS):
        name = command[1:]
        placeholder, description = SLASH_USAGE.get(command, ("", ""))
        if needle and not name.lower().startswith(needle):
            if needle not in name.lower():
                continue
            rank = 1
        else:
            rank = 0
        label = name + (" " + placeholder if placeholder else "")
        if description:
            label = "{}   {}".format(label, description)
        rows.append((rank, order, {"insert": name, "label": label}))
    rows.sort(key=lambda item: (item[0], item[1]))
    return [row for _rank, _order, row in rows]


def _mention_rank(needle: str, insert: str, role: str) -> Optional[int]:
    if not needle:
        return 1
    if insert.lower().startswith(needle):
        return 0
    if needle in insert.lower() or (role and needle in role.lower()):
        return 1
    return None


def mention_menu(model: Any) -> List[Dict[str, str]]:
    """The open ``@`` menu rows for any model with ``input``/``cursor``/``members``; [] when closed."""
    if getattr(model, "paste_mode", False):
        return []
    if getattr(model, "mention_hidden_for", None) == model.input:
        return []
    ctx = mention_context(model.input, model.cursor)
    if ctx is None:
        return []
    if ctx[3] == "@@":
        return file_candidates(ctx[2], getattr(model, "file_roots", None) or None, getattr(model, "file_base", None))
    if ctx[3] == "/":
        # The compose popup only understands the post directives, so it offers
        # those rather than the whole console vocabulary.
        allowed = getattr(model, "slash_commands", None)
        return slash_candidates(ctx[2], allowed) if getattr(model, "slash_menu", True) else []
    if ctx[3] != "@" and not getattr(model, "bang_menu", True):
        return []  # the compose popup refuses ! lines, so it does not offer names for them
    return mention_candidates(
        model.members,
        ctx[2],
        members_only=ctx[3] != "@",
        links=getattr(model, "links", None),
        force_all=ctx[3] == "!!",
    )


def mention_lines(model: Any, width: int, ascii_only: bool = False) -> List[str]:
    """Screen rows for the open ``@`` menu (at most MENTION_MENU_ROWS), highlight on ``mention_index``."""
    rows = mention_menu(model)
    if not rows:
        return []
    index = max(0, min(getattr(model, "mention_index", 0), len(rows) - 1))
    first = 0
    if len(rows) > MENTION_MENU_ROWS:
        first = max(0, min(index - MENTION_MENU_ROWS + 1, len(rows) - MENTION_MENU_ROWS))
    marker = MENTION_MARKER_ASCII if ascii_only else MENTION_MARKER
    sigil = mention_sigil(model)
    out: List[str] = []
    for i, row in enumerate(rows[first : first + MENTION_MENU_ROWS], start=first):
        lead = (marker if i == index else " ") + " " + sigil
        out.append(truncate_columns(lead + row["label"], width))
    if len(rows) > MENTION_MENU_ROWS:
        # Enter picks in the name/file menus, but runs the line in the command menu.
        picks = "Tab picks" if sigil == "/" else "Tab or Enter picks"
        arrows = "up/down move" if ascii_only else "↑/↓ move"
        out.append(truncate_columns("  {} of {}  ({}, {}, Esc hides)".format(index + 1, len(rows), arrows, picks), width))
    if sigil == "@@":
        roots = [r for r in (getattr(model, "file_roots", None) or []) if r] or [getattr(model, "file_base", None) or os.getcwd()]
        where = roots[0] + ("  (then {} other project{})".format(len(roots) - 1, "" if len(roots) == 2 else "s") if len(roots) > 1 else "")
        out.append(truncate_columns("  project {}  (type to search names, Tab picks, a dir/ descends; ~ or / for other paths)".format(where), width))
    return out


def accept_mention(model: Any, row: Dict[str, str]) -> None:
    """Replace the token under the cursor with ``<sigil><insert> `` (same sigil) and move the cursor after it."""
    ctx = mention_context(model.input, model.cursor)
    if ctx is None:
        return
    start, end, _, sigil = ctx
    # A completed directory keeps the menu open on its contents; a file or a name closes it with a space.
    # A command inserts its name only, never the placeholder, which the operator would have to delete.
    replacement = sigil + row["insert"] + ("" if sigil == "@@" and row["insert"].endswith("/") else " ")
    model.input = model.input[:start] + replacement + model.input[end:]
    model.cursor = start + len(replacement)
    model.mention_index = 0
    model.mention_hidden_for = None


def mention_key(model: Any, key: str) -> bool:
    """Handle a key while the ``@`` menu is open; True when the key was consumed."""
    rows = mention_menu(model)
    if not rows:
        return False
    index = max(0, min(getattr(model, "mention_index", 0), len(rows) - 1))
    if key == "UP":
        model.mention_index = (index - 1) % len(rows)
        return True
    if key == "DOWN":
        model.mention_index = (index + 1) % len(rows)
        return True
    if key == "ENTER" and mention_sigil(model) == "/":
        # The command menu is a hint, not a gate: Enter runs the line you typed,
        # Tab completes from the menu. Consuming Enter here would make every
        # fully typed command need a second press.
        return False
    if key in ("TAB", "ENTER"):
        accept_mention(model, rows[index])
        return True
    if key == "ESC":
        model.mention_hidden_for = model.input
        model.mention_index = 0
        return True
    return False


# -- command parsing -----------------------------------------------------------


def parse_post_directives(line: str, default_to: Optional[str] = None) -> Tuple[Optional[PostSpec], Optional[str]]:
    """Leading ``@name`` ``@role:r`` ``/all`` ``/human`` ``/kind k`` ``/reply N`` ``/urgent`` ``/ref p`` then text."""
    spec = PostSpec(text="")
    rest, spec.files = extract_file_tokens(line)
    while True:
        m = _DIRECTIVE_RE.match(rest)
        if not m:
            break
        if m.group("at"):
            target = m.group("at")[1:].rstrip(",")
            if not RECIPIENT_RE.match(target):
                return None, "invalid recipient @{}".format(target)
            if target not in spec.to:
                spec.to.append(target)
        elif m.group("all"):
            spec.to = ["all"]
        elif m.group("human"):
            if "human" not in spec.to:
                spec.to.append("human")
        elif m.group("team"):
            # Another team, through the link between them; it goes alone.
            name = m.group("team").lower().rstrip(",")
            if not re.match(r"^[a-z][a-z0-9_-]{0,63}\Z", name):
                return None, "invalid team name {}".format(name)
            spec.to = ["team:" + name]
        elif m.group("urgent"):
            spec.urgent = True
        elif m.group("interrupt"):
            spec.interrupt = True
            spec.urgent = True
        elif m.group("kind"):
            kind = m.group("kind").lower()
            if kind not in POST_KINDS:
                return None, "unknown kind {} (one of {})".format(kind, ", ".join(POST_KINDS))
            spec.kind = kind
        elif m.group("reply"):
            try:
                spec.reply_to = int(m.group("reply").lstrip("#"))
            except ValueError:
                return None, "/reply needs a seq number"
        elif m.group("ref"):
            spec.refs.append(m.group("ref"))
        rest = rest[m.end() :]
    text = rest.strip()
    if not text and spec.files:
        text = "file: " + ", ".join(os.path.basename(f.rstrip("/")) or f for f in spec.files)
    if not text:
        return None, "empty post"
    spec.text = text
    if not spec.to and default_to:
        spec.to = [default_to]
    return spec, None


def _split_args(text: str) -> List[str]:
    return text.split()


def parse_input_line(line: str, default_team: str) -> Intent:
    """``@name``, ``@role:qa``, ``/all``, ``/reply N``, ``/retract N``, ... -> Intent."""
    stripped = line.strip()
    if not stripped:
        return Intent("none")
    if stripped.startswith("!"):
        spec, err = parse_bang(stripped)
        if err or spec is None:
            return Intent("error", {"message": err or "usage: !name text"})
        return Intent("say", {"member": spec.member, "text": spec.text, "force": spec.force, "team": default_team})
    if stripped.startswith("/"):
        head = stripped.split(None, 1)[0].lower()
        if head not in POST_DIRECTIVES:
            return _parse_slash(head, stripped[len(head) :].strip(), default_team)
    if stripped.startswith("@") or stripped.startswith("/"):
        spec, err = parse_post_directives(stripped)
    else:
        spec, err = parse_post_directives(stripped)
    if err:
        return Intent("error", {"message": err})
    assert spec is not None
    args = spec.to_args()
    args["team"] = default_team
    return Intent("post", args)


def _parse_slash(head: str, rest: str, default_team: str) -> Intent:
    args = _split_args(rest)
    if head == "/retract":
        if len(args) != 1 or not args[0].lstrip("#").isdigit():
            return Intent("error", {"message": "usage: /retract <seq>"})
        return Intent("retract", {"seq": int(args[0].lstrip("#")), "team": default_team, "confirm": False})
    if head == "/remove":
        if len(args) != 1:
            return Intent("error", {"message": "usage: /remove <name>"})
        return Intent("remove", {"member": args[0], "team": default_team, "confirm": False})
    if head in ("/mute", "/pause"):
        member: Optional[str] = None
        duration: Optional[str] = None
        for a in args:
            if a in ("--all", "all", "*"):
                member = None
            elif re.match(r"^\d+[smh]$", a):
                duration = a
            elif member is None and RECIPIENT_RE.match(a):
                member = a
            else:
                return Intent("error", {"message": "usage: /mute [<name>|all] [10m]"})
        if head == "/pause":
            member = None
        return Intent("mute", {"member": member, "for": duration, "team": default_team})
    if head == "/unmute":
        member = None
        if args:
            if args[0] not in ("--all", "all", "*"):
                if not RECIPIENT_RE.match(args[0]):
                    return Intent("error", {"message": "usage: /unmute [<name>|all]"})
                member = args[0]
        return Intent("unmute", {"member": member, "team": default_team})
    if head == "/nudge":
        if not args or not MEMBER_NAME_RE.match(args[0]):
            return Intent("error", {"message": "usage: /nudge <name> [--force]"})
        return Intent("nudge", {"member": args[0], "force": any(a in ("--force", "force") for a in args[1:]), "team": default_team})
    if head in ("/focus", "/peek"):
        if len(args) != 1 or not MEMBER_NAME_RE.match(args[0]):
            return Intent("error", {"message": "usage: {} <name>".format(head)})
        return Intent(head[1:], {"member": args[0], "team": default_team})
    if head == "/interrupts":
        mode = None
        cooldown = None
        rest = list(args)
        while rest:
            a = rest.pop(0)
            if a == "--cooldown" and rest:
                cooldown = rest.pop(0)
            elif mode is None and a != "--cooldown":
                mode = a
            else:
                return Intent("error", {"message": "usage: /interrupts [show|off|on|kind,…] [--cooldown 10m]"})
        return Intent("interrupts", {"mode": mode, "cooldown": cooldown, "team": default_team})
    if head == "/who":
        return Intent("who", {"team": default_team})
    if head == "/asks":
        return Intent("asks", {"team": default_team})
    if head == "/ask-policy":
        block = None
        timeout = None
        for a in args:
            if a in ("block", "on", "yes"):
                block = True
            elif a in ("noblock", "off", "no"):
                block = False
            elif re.match(r"^\d+[smh]$", a):
                timeout = a
            else:
                return Intent("error", {"message": "usage: /ask-policy [block|noblock] [8m]"})
        return Intent("ask_policy", {"team": default_team, "block": block, "timeout": timeout})
    if head == "/context":
        if len(args) > 1 or (args and not MEMBER_NAME_RE.match(args[0])):
            return Intent("error", {"message": "usage: /context [<name>]"})
        return Intent("context", {"member": args[0] if args else None, "team": default_team})
    if head in ("/compact", "/clear"):
        if len(args) != 1 or not MEMBER_NAME_RE.match(args[0]):
            return Intent("error", {"message": "usage: {} <name>".format(head)})
        return Intent(head[1:], {"member": args[0], "team": default_team, "confirm": False})
    if head == "/model":
        words = [w for w in args if w]
        restart = "--restart" in words
        words = [w for w in words if w != "--restart"]
        if len(words) != 2 or not MEMBER_NAME_RE.match(words[0]):
            return Intent("error", {"message": "usage: /model <name> <model>[@<effort>] [--restart]"})
        return Intent("model_set", {"member": words[0], "setting": words[1], "restart": restart, "team": default_team})
    if head == "/export":
        # Optional path and format: ``/export``, ``/export ~/board.md``, ``/export --format json``.
        fmt = "md"
        words = [w for w in args if w]
        path_words = []
        index = 0
        while index < len(words):
            if words[index] == "--format" and index + 1 < len(words):
                fmt = words[index + 1]
                index += 2
                continue
            path_words.append(words[index])
            index += 1
        if fmt not in ("md", "json", "jsonl", "text"):
            return Intent("error", {"message": "format must be md, json, jsonl or text"})
        return Intent("export", {"team": default_team, "path": " ".join(path_words) or None, "format": fmt})
    if head == "/wipe":
        words = [w for w in args if w]
        purge = "--purge" in words
        reason = " ".join(w for w in words if w != "--purge").strip() or None
        return Intent("wipe", {"team": default_team, "purge": purge, "reason": reason, "confirm": False})
    if head == "/links":
        return Intent("links", {"team": default_team})
    if head in ("/link", "/unlink"):
        if len(args) != 1 or not re.match(r"^[a-z][a-z0-9_-]{0,63}\Z", args[0]):
            return Intent("error", {"message": "usage: {} <other-team>".format(head)})
        return Intent(head[1:], {"team": default_team, "other": args[0]})
    if head == "/filter":
        if args:
            name = " ".join(args).lower()
            if name not in FILTERS:
                return Intent("error", {"message": "filters: {}".format(", ".join(FILTERS))})
            return Intent("filter", {"name": name})
        return Intent("filter", {"name": None})
    if head == "/as":
        if len(args) != 1 or not re.match(r"^[A-Za-z0-9._-]{1,32}$", args[0]):
            return Intent("error", {"message": "usage: /as <label> ([A-Za-z0-9._-]{1,32})"})
        return Intent("as", {"label": args[0]})
    if head == "/use":
        if len(args) != 1 or not TEAM_NAME_RE.match(args[0]):
            return Intent("error", {"message": "usage: /use <team>"})
        return Intent("use", {"team": args[0]})
    if head == "/charter":
        if not args:
            return Intent("charter", {"action": "show", "team": default_team})
        if args[0] == "set":
            body = rest[len("set") :].strip()
            urgent = False
            if body.startswith("--urgent"):
                urgent = True
                body = body[len("--urgent") :].strip()
            if not body:
                return Intent("error", {"message": "usage: /charter set [--urgent] <text>"})
            return Intent("charter", {"action": "set", "text": body, "urgent": urgent, "team": default_team})
        return Intent("error", {"message": "usage: /charter | /charter set [--urgent] <text>"})
    if head == "/quit":
        return Intent("quit")
    if head == "/help":
        return Intent("help", {"commands": list(SLASH_COMMANDS)})
    return Intent("error", {"message": "unknown command {} (try /help)".format(head)})


HELP_TEXT = (
    "? or /help opens the full list | @ opens the name list (↑/↓ move, Tab or Enter picks, Esc hides) | "
    "!name text types only if that member stays idle (!!name permits working; !!all targets every agent) | @@path attaches a file (@@ lists files) | "
    "@name text | @role:r text | /all text | /human text | /kind k | /reply N | /urgent | /interrupt | /ref path | "
    "/retract N | /mute [name] [10m] | /unmute [name] | /pause | /nudge name [--force] | /focus name | "
    "/peek name | /who | /context [name] | /compact name | /clear name | /filter [name] | /as label | /use team | /charter [set [--urgent] text] | /remove name | /export | /quit"
)


HELP_LINES = (
    "post:      text (whole team)   @name text (one member)   @role:r text (a role)   /human text (yourself)",
    "           /kind k  /reply N  /urgent (nudges everyone)  /ref path  @@path attaches a file (@@ lists files)",
    "type now:  !name text types only if that same member is still idle at atomic submission",
    "           !!name permits working or muted; !!all sends to every agent; every attempt is recorded",
    "           a line that begins with ! never posts by itself; to post one, write /all !text",
    "menus:     / commands   @ names   @@ files   ! members   !! members + all   (up/down move, Tab picks, Esc hides)",
    "board:     /retract N   /filter [all|to me|requests|human|system]   Tab cycles   /who",
    "members:   /peek name   /focus name   /nudge name [--force]   /remove name   /asks",
    "context:   /context [name]   /compact name   /clear name (throws it away, asks)   /ask-policy",
    "delivery:  /mute [name] [10m]   /unmute [name]   /pause [10m]   Esc clears the status or closes a box",
    "interrupt: /interrupt @name text (into a working turn)   /interrupts [off|on|kind,…] [--cooldown 10m]",
    "team:      /charter   /charter set [--urgent] text   /use team   /as label   /export [path]   /quit",
    "keys:      Up/Down and PgUp/PgDn scroll back; End (or Esc) returns to the latest and follows again",
    "           Alt+Enter newline   Ctrl-U clear   Ctrl-K kill to end   Ctrl-A/Ctrl-E start/end   Ctrl-C quits",
)


def help_lines() -> List[str]:
    """The full command list shown in a box by ``?`` (on an empty line) and ``/help``."""
    return list(HELP_LINES)


def apply_key(model: ConsoleModel, key: str) -> Optional[Intent]:
    """Edit the input line, cycle filters on Tab, return an Intent on Enter."""
    if model.pending_confirm is not None:
        pending = model.pending_confirm
        if key in ("y", "Y", "ENTER"):
            model.pending_confirm = None
            model.status = None
            pending.args["confirm"] = True
            return pending
        if key in ("n", "N", "ESC", "CTRL_C"):
            model.pending_confirm = None
            model.status = "cancelled"
            return Intent("none")
        return None
    if model.peek is not None and key in ("ESC", "q", "CTRL_C"):
        model.peek = None
        model.status = None
        return None
    if key == "RESIZE":
        return None
    if mention_key(model, key):
        return None
    if key == "?" and not model.input and not model.paste_mode:
        # A help sign on an empty line; ``?`` inside text is just a character.
        model.peek = box(help_lines(), model.width, "help (Esc closes)")
        model.status = "Esc closes"
        return Intent("help", {"commands": list(SLASH_COMMANDS)})
    if key == "TAB" and not model.paste_mode:
        model.filter_index = (model.filter_index + 1) % len(FILTERS)
        _follow_tail(model)
        return Intent("filter", {"name": FILTERS[model.filter_index]})
    if key in ("UP", "PGUP"):
        if model.follow:
            model.follow = False
            model.status = "scrolled back; End returns to the latest"
        model.scroll += 1 if key == "UP" else 10
        return None
    if key in ("DOWN", "PGDN"):
        model.scroll = max(0, model.scroll - (1 if key == "DOWN" else 10))
        if model.scroll == 0:
            model.follow = True
            model.status = None
        return None
    if key == "END" and not model.follow:
        # Only while scrolled: otherwise End stays end-of-line (and Ctrl-E always is).
        _follow_tail(model)
        model.status = "following the latest"
        return None
    if key == "ESC":
        if not model.follow:
            _follow_tail(model)
        model.status = None
        return None
    if key == "CTRL_C":
        if model.input:
            model.input = ""
            model.cursor = 0
            model.status = None
            return None
        return Intent("quit")
    if key == "ENTER" and not model.paste_mode:
        line = model.input
        model.input = ""
        model.cursor = 0
        intent = parse_input_line(line, model.team)
        return _after_parse(model, intent)
    if edit_key(model, key):
        model.mention_index = 0
        return None
    return None


def _after_parse(model: ConsoleModel, intent: Intent) -> Intent:
    if intent.kind in ("post", "say"):
        # You post in order to watch it land, so sending returns to the tail.
        _follow_tail(model)
    if intent.kind == "post":
        if not intent.args.get("to"):
            reply_to = intent.args.get("reply_to")
            author = None
            if reply_to is not None:
                for entry in model.feed:
                    if entry.get("seq") == reply_to:
                        author = entry.get("from")
                        break
            if author and author not in ("system",):
                intent.args["to"] = [author]
            else:
                intent.args["to"] = [model.default_recipient or "all"]
        intent.args["spill"] = len(intent.args.get("text") or "") > MAX_TEXT_CHARS
        intent.args["focused"] = model.focused
        intent.args["label"] = model.human_label
        if not model.focused:
            model.status = "posted while unfocused: recorded as unverified"
        return intent
    if intent.kind == "say":
        # The roster is checked here so a typo is an error before the CLI runs; a still-empty roster defers to the CLI.
        known = agent_member_names(model.members)
        member = str(intent.args.get("member"))
        if known and member not in known and not (member == "all" and bool(intent.args.get("force"))):
            message = "no member {} ({})".format(member, BANG_HINT)
            model.status = "error: {}".format(message)
            return Intent("error", {"message": message})
        return intent
    if intent.kind in ("retract", "remove", "clear", "wipe"):
        if intent.kind == "retract":
            what = "retract #{}".format(intent.args.get("seq"))
        elif intent.kind == "remove":
            what = "remove {}".format(intent.args.get("member"))
        elif intent.kind == "wipe":
            what = ("delete every post on the board, its archive and its payloads (nothing is kept)" if intent.args.get("purge")
                    else "clear the board (every post moves to the archive; read positions are kept)")
        else:
            # Clearing is the one console action that destroys something the
            # operator cannot get back, so it asks even though it is one word.
            what = "clear {}'s context".format(intent.args.get("member"))
        model.pending_confirm = intent
        model.status = "{}? y/n".format(what)
        return Intent("none")
    if intent.kind == "filter":
        name = intent.args.get("name")
        if name is None:
            model.filter_index = (model.filter_index + 1) % len(FILTERS)
        else:
            model.filter_index = FILTERS.index(name)
        _follow_tail(model)
        intent.args["name"] = FILTERS[model.filter_index]
        return intent
    if intent.kind == "as":
        model.human_label = intent.args.get("label")
        model.status = "posting as human ({})".format(model.human_label)
        return intent
    if intent.kind == "help":
        model.peek = box(help_lines(), model.width, "help (Esc closes)")
        model.status = HELP_TEXT
        return intent
    if intent.kind == "error":
        model.status = "error: {}".format(intent.args.get("message"))
        return intent
    return intent


def settle_say_watch(model: ConsoleModel, records: Iterable[Dict[str, Any]], now_mono: float) -> Optional[str]:
    """Turn the ``typed`` outcome of a say this console sent into a status line; drop watches that gave up.

    Cheap when nothing is watched (one truthiness check); otherwise one pass
    over the records. Receipt tags need 60 columns, so the status line is the
    outcome's home on a narrow pane.
    """
    if not model.watching_say:
        return None
    outcomes = typed_outcomes(records)
    status: Optional[str] = None
    for seq in sorted(model.watching_say):
        member, sent = model.watching_say[seq]
        outcome = outcomes.get(seq)
        if outcome is not None:
            ok, label = typed_label(outcome)
            if outcome.get("reason") == "capability_unavailable":
                status = "say #{}: safe ! unavailable; post with @{} or force with !!{}".format(seq, member, member)
            else:
                status = "say #{} to {}: {}".format(seq, member, label)
            if not ok and outcome.get("reason") in FORCEABLE_REASONS:
                status += " (!!{} text forces)".format(member)
            del model.watching_say[seq]
        elif now_mono - sent >= SAY_WATCH_S:
            status = "say #{} to {}: no outcome after {:.0f}s (herdr-synapse notifier stats)".format(seq, member, SAY_WATCH_S)
            del model.watching_say[seq]
    if status is not None:
        model.status = status
    return status


# -- full-screen rendering -----------------------------------------------------


def input_lines(model: ConsoleModel, width: int) -> List[str]:
    """The input area: prompt plus buffer, one screen line per buffer line, wrapped by columns."""
    inner = max(1, width - display_width(INPUT_PROMPT))
    lines: List[str] = []
    for i, raw in enumerate(model.input.split("\n")):
        prefix = INPUT_PROMPT if i == 0 else "  "
        chunks = [raw] if raw else [""]
        if display_width(raw) > inner:
            chunks = []
            cur = ""
            for ch in raw:
                if display_width(cur + ch) > inner:
                    chunks.append(cur)
                    cur = ch
                else:
                    cur += ch
            chunks.append(cur)
        for j, chunk in enumerate(chunks):
            lines.append((prefix if j == 0 else "  ") + chunk)
    return lines


#: Style keys the runtime maps to colors. ``member:<name>`` gets a stable
#: color per roster slot; ``human`` is the operator; ``system`` records are
#: dim; ``warning`` is for audit warnings; ``menu-selected`` is the ``@``
#: menu highlight; ``input`` is the prompt line.
STYLE_HEADER = "header"
STYLE_DIM = "dim"
STYLE_HUMAN = "human"
STYLE_SYSTEM = "system"
STYLE_WARNING = "warning"
STYLE_MENU = "menu"
STYLE_MENU_SELECTED = "menu-selected"
STYLE_INPUT = "input"
STYLE_STATUS = "status"
STYLE_PEEK = "peek"
STYLE_PLAIN = ""
#: The team manager's row in the teams view: bold, and coloured where the terminal has colours.
STYLE_MANAGER = "manager"


def member_style(name: Optional[str]) -> str:
    return "member:" + str(name) if name else STYLE_PLAIN


def member_color_slot(name: str, members: Iterable[Dict[str, Any]]) -> Optional[int]:
    """Stable palette slot for a member: its position among non-human roster rows, or None."""
    slot = 0
    for member in members:
        if member.get("kind") == "human" or not member.get("name"):
            continue
        if str(member.get("name")) == name:
            return slot
        slot += 1
    return None


def entry_style(entry: Dict[str, Any]) -> str:
    """Style key for one feed entry: warning, system, human, or the author's member color."""
    if entry.get("warning"):
        return STYLE_WARNING
    kind = str(entry.get("kind") or "")
    author = str(entry.get("from") or "")
    if kind == "system" or author == "system":
        return STYLE_SYSTEM
    if author == "human" or author.startswith("human@"):
        return STYLE_HUMAN
    return member_style(author) if author else STYLE_PLAIN


def mention_rows_styled(model: Any, width: int, ascii_only: bool = False) -> List[Tuple[str, str]]:
    """``mention_lines`` with a style per row: the member's color, ``menu-selected`` on the highlight."""
    rows = mention_menu(model)
    plain = mention_lines(model, width, ascii_only)
    if not rows or not plain:
        return []
    index = max(0, min(getattr(model, "mention_index", 0), len(rows) - 1))
    first = 0
    if len(rows) > MENTION_MENU_ROWS:
        first = max(0, min(index - MENTION_MENU_ROWS + 1, len(rows) - MENTION_MENU_ROWS))
    styled: List[Tuple[str, str]] = []
    for offset, line in enumerate(plain):
        i = first + offset
        if i >= len(rows) or i >= first + MENTION_MENU_ROWS:
            styled.append((line, STYLE_DIM))  # the "n of m" footer
            continue
        insert = rows[i]["insert"]
        if i == index:
            style = STYLE_MENU_SELECTED
        elif mention_sigil(model) == "@@" or insert in ("all", "human") or insert.startswith("role:"):
            style = STYLE_MENU
        else:
            style = member_style(insert)
        styled.append((line, style))
    return styled


def render_console_styled(model: ConsoleModel, width: Optional[int] = None, height: Optional[int] = None) -> List[Tuple[str, str]]:
    """``render_console`` with a style key per line (see the STYLE_* constants)."""
    w = width if width is not None else model.width
    h = height if height is not None else model.height
    if h <= 0:
        return []
    footer: List[Tuple[str, str]] = [(filter_line(model), STYLE_DIM)]
    if model.status:
        footer.append(("• " + model.status if not model.ascii_only else "* " + model.status, STYLE_STATUS))
    footer.extend(mention_rows_styled(model, w, model.ascii_only))
    footer.extend((line, STYLE_INPUT) for line in input_lines(model, w))
    if len(footer) > h:
        footer = footer[-h:]  # the input line always wins
    budget = h - len(footer)
    header = [(line, STYLE_HEADER) for line in header_lines(model.header, w)]
    if model.runtime is not None:
        runtime_style = STYLE_WARNING if model.runtime.atomic_idle_prompt is False else STYLE_DIM
        header.append((runtime_line(model.runtime, w), runtime_style))
    lines: List[Tuple[str, str]] = header[:budget]
    budget -= len(lines)
    roster_cap = min(len(model.roster_lines), max(3, h // 4), max(0, budget - 1))
    roster_names = [str(m.get("name") or "") for m in model.members] if len(model.members) == len(model.roster_lines) else []
    for i, line in enumerate(model.roster_lines[:roster_cap]):
        name = roster_names[i] if i < len(roster_names) else ""
        style = STYLE_HUMAN if name == "human" else member_style(name)
        lines.append((line, style))
    budget -= roster_cap
    if len(model.roster_lines) > roster_cap and budget > 1:
        lines.append((truncate_columns("  … {} more (/who)".format(len(model.roster_lines) - roster_cap), w), STYLE_DIM))
        budget -= 1
    if budget > 0:
        lines.append(("─" * w if not model.ascii_only else "-" * w, STYLE_DIM))
        budget -= 1
    feed_height = max(0, budget)
    if model.peek is not None:
        body: List[Tuple[str, str]] = [(line, STYLE_PEEK) for line in fit_peek(model.peek, feed_height)]
    elif not model.follow and feed_height >= 1:
        # Scrolled back: give the bottom row to the indicator, so the reader can
        # see that the feed is not following and how to get back.
        rows = visible_feed_rows(model, feed_height - 1)
        body = [(row, entry_style(e)) for row, e in rows]
        body.append((feed_gap_line(model.scroll, w, model.ascii_only), STYLE_STATUS))
    else:
        body = [(row, entry_style(e)) for row, e in visible_feed_rows(model, feed_height)]
    body = body + [("", STYLE_PLAIN)] * (feed_height - len(body))
    lines.extend(body)
    lines.extend(footer)
    out = [(truncate_columns(line, w), style) for line, style in lines]
    if len(out) > h:
        out = out[:h]
    return out


def render_console(model: ConsoleModel, width: Optional[int] = None, height: Optional[int] = None) -> List[str]:
    """Every screen line, top to bottom, exactly ``height`` entries, each at most ``width`` columns."""
    return [line for line, _ in render_console_styled(model, width, height)]


def fit_peek(box_lines: List[str], height: int) -> List[str]:
    """A ``/peek`` box taller than the feed keeps its title border and the *last* lines.

    M7 UI-02 (rig, 2026-09-05): a 20-row console showed only blank leading rows and the top of
    a working Claude's screen because the box was cut from the head; the member's spinner,
    prompt box, and status line live at the bottom of ``agent read --source visible``.
    """
    if len(box_lines) <= height:
        return list(box_lines)
    if height <= 1:
        return list(box_lines[:height])
    return [box_lines[0]] + list(box_lines[-(height - 1):])


# --------------------------------------------------------------------------
# /peek box


def box(lines: List[str], width: int, title: Optional[str] = None) -> List[str]:
    """Bordered box for ``/peek`` with prompt markers replaced so the console is never detected as an agent."""
    width = max(8, int(width))
    inner = width - 4
    top_label = " {} ".format(truncate_columns(title, max(0, inner - 2))) if title else ""
    top = "┌─" + top_label + "─" * max(0, inner - display_width(top_label)) + "─┐"
    bottom = "└" + "─" * (width - 2) + "┘"
    out = [top]
    for raw in lines:
        cleaned = neutralize_prompt_markers(strip_ansi(raw).replace("\t", "    ").rstrip("\r\n"))
        out.append("│ " + pad_columns(cleaned, inner) + " │")
    if not lines:
        out.append("│ " + pad_columns("(empty screen)", inner) + " │")
    out.append(bottom)
    return out


# --------------------------------------------------------------------------
# picker


@dataclass
class PickerRow:
    pane_id: str
    workspace_id: str
    tab_id: str
    kind: Optional[str]
    name: Optional[str]
    agent_status: str
    launch_pending: bool
    #: Human-facing label from ``tab.list``; ``""`` falls back to ``tab_id``.
    tab_label: str = ""
    #: Stable tab number from ``tab.list``; None when that optional lookup failed.
    tab_number: Optional[int] = None
    selected: bool = False
    role: str = ""
    member_name: str = ""
    brief: str = ""
    #: ``<model>[@<effort>]`` typed at the fourth prompt; "" leaves the harness default.
    setting: str = ""
    claimed_by: str = ""  # team that already owns this agent, if any
    terminal_id: str = ""
    #: The agent's working directory, used to prefill the project stage.
    cwd: str = ""


@dataclass
class PickerModel:
    rows: List[PickerRow]
    cursor: int = 0
    scope_workspace: Optional[str] = None
    stage: str = "select"  # select | topology | target | name | charter | rules | project | members | confirm
    team_name: str = ""
    charter: str = ""
    rules: str = ""
    #: The team's project directory; "" means the team gets no working folder.
    project: str = ""
    #: ``workdir.status`` per existing team, for the tree's folder column (filled by the runtime).
    folders: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: The team whose folder the ``team_folder`` stage is setting.
    folder_team: str = ""
    #: Active links per team (``links.summary`` rows) and each team's manager, for the tree and the ``c`` chooser.
    links: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    managers: Dict[str, Optional[str]] = field(default_factory=dict)
    #: The ``link_pick`` stage: which team is being connected, and the highlighted row.
    link_team: str = ""
    link_index: int = 0
    error: Optional[str] = None
    #: ``create`` a new team, or ``add`` the selected agents to an existing team (the target stage).
    mode: str = "create"
    #: Highlighted row of the target stage: one row per existing team, then "create a new team".
    target_index: int = 0
    #: Agent members per existing team, for the target stage labels (filled by the picker runtime).
    existing_team_sizes: Dict[str, int] = field(default_factory=dict)
    #: Kinds the daemon may type into (``kinds.json``); None when unknown. The confirm screen warns about the rest.
    trusted_kinds: Optional[Set[str]] = None
    # -- extra state owned by this module
    focused_workspace: Optional[str] = None
    restore_pane: Optional[str] = None
    restore_terminal: Optional[str] = None
    restore_team: Optional[str] = None
    restore_results: List[str] = field(default_factory=list)
    restore_top: int = 0
    live_names: Set[str] = field(default_factory=set)
    existing_teams: List[str] = field(default_factory=list)
    input: str = ""
    cursor_pos: int = 0
    charter_lines: List[str] = field(default_factory=list)
    rules_lines: List[str] = field(default_factory=list)
    member_index: int = 0
    member_field: str = "role"  # role | name | brief | model
    status: Optional[str] = None
    paste_mode: bool = False
    # -- the team tree (the select stage) and the member actions on it
    #: ``{team: [member dicts]}`` straight from every ``team.json`` in the session.
    rosters: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    #: ``{team: charter}`` from the same read, so a team row can show its goal with the daemon down.
    charters: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: ``{team: {name: who member}}``: the live extras only the daemon knows (headline, holds, mutes).
    who_members: Dict[str, Dict[str, Dict[str, Any]]] = field(default_factory=dict)
    #: Team names folded away in the tree.
    collapsed: Set[str] = field(default_factory=set)
    #: Tab ids folded away in the unassigned-agent part of the tree.
    collapsed_tabs: Set[str] = field(default_factory=set)
    ascii_only: bool = False
    #: First visible tree row; owned by ``picker_lines`` the way ``feed_window`` owns the console scroll.
    top: int = 0
    #: First visible row in the read-only ASCII topology view.
    topology_top: int = 0
    #: Body height of the last render, so PgUp/PgDn can step a real page.
    page_rows: int = 10
    #: The member the action menu is about, and the highlighted action.
    action_team: str = ""
    action_member: str = ""
    action_index: int = 0
    #: A destructive action waiting for ``y``; mirrors ``ConsoleModel.pending_confirm``.
    pending_action: Optional["Intent"] = None

    # ``edit_key`` expects ``cursor``; the picker's list cursor already uses
    # that name, so the text cursor is ``cursor_pos`` and this shim maps it.
    @property
    def text_cursor(self) -> int:
        return self.cursor_pos


class _TextView:
    """Adapter so ``edit_key`` can edit a picker's input buffer."""

    def __init__(self, model: PickerModel) -> None:
        self._m = model

    @property
    def input(self) -> str:
        return self._m.input

    @input.setter
    def input(self, value: str) -> None:
        self._m.input = value

    @property
    def cursor(self) -> int:
        return self._m.cursor_pos

    @cursor.setter
    def cursor(self, value: int) -> None:
        self._m.cursor_pos = value

    @property
    def paste_mode(self) -> bool:
        return self._m.paste_mode

    @paste_mode.setter
    def paste_mode(self, value: bool) -> None:
        self._m.paste_mode = value


def _id_sort_key(value: str) -> Tuple[int, str]:
    m = _ID_NUM_RE.search(value or "")
    return (int(m.group(1)) if m else 1 << 30, value or "")


def _pane_sort_key(pane_id: str) -> Tuple[int, str]:
    tail = pane_id.split(":")[-1] if pane_id else ""
    return _id_sort_key(tail)


def picker_rows_from_agent_list(
    agents: List[Dict[str, Any]],
    focused_workspace: Optional[str],
    tabs: Optional[List[Dict[str, Any]]] = None,
) -> List[PickerRow]:
    """Sorted by workspace, tab, then pane; ``launch_pending`` rows are not selectable."""
    tab_info = {
        str(tab.get("tab_id")): tab
        for tab in (tabs or [])
        if isinstance(tab, dict) and tab.get("tab_id")
    }
    rows: List[PickerRow] = []
    for agent in agents:
        if not agent.get("agent") and not agent.get("launch_pending"):
            continue  # a pane without a detected agent is never listed
        pane_id = str(agent.get("pane_id") or "")
        tab_id = str(agent.get("tab_id") or "")
        tab = tab_info.get(tab_id) or {}
        number = tab.get("number")
        rows.append(
            PickerRow(
                pane_id=pane_id,
                workspace_id=str(agent.get("workspace_id") or pane_id.split(":")[0]),
                tab_id=tab_id,
                kind=agent.get("agent"),
                name=agent.get("name"),
                agent_status=str(agent.get("agent_status") or "unknown"),
                launch_pending=bool(agent.get("launch_pending")),
                tab_label=headline(str(tab.get("label") or ""), 80),
                tab_number=number if isinstance(number, int) and not isinstance(number, bool) else None,
                terminal_id=str(agent.get("terminal_id") or ""),
                cwd=str(agent.get("cwd") or ""),
            )
        )
    rows.sort(key=lambda r: (
        _id_sort_key(r.workspace_id),
        r.tab_number if r.tab_number is not None else _id_sort_key(r.tab_id)[0],
        r.tab_id,
        _pane_sort_key(r.pane_id),
    ))
    return rows


def mark_claimed(rows: List[PickerRow], rosters: Dict[str, List[Dict[str, Any]]]) -> None:
    """``rosters``: ``{team: [member dicts]}``; rows already in a team get ``claimed_by``."""
    by_terminal: Dict[str, str] = {}
    by_pane: Dict[str, str] = {}
    for team, members in rosters.items():
        for m in members:
            if m.get("status") in ("left",):
                continue
            if m.get("terminal_id"):
                by_terminal[str(m["terminal_id"])] = team
            if m.get("pane_id"):
                by_pane[str(m["pane_id"])] = team
    for row in rows:
        row.claimed_by = by_terminal.get(row.terminal_id) or by_pane.get(row.pane_id) or ""


@dataclass
class PickerNode:
    """One line of the team tree: a team/member, section, tab, or unassigned agent."""

    kind: str  # team | member | section | tab | agent
    key: str
    team: str = ""
    tab_id: str = ""
    label: str = ""
    count: int = 0
    member: Optional[Dict[str, Any]] = None
    row: Optional[PickerRow] = None


#: Roster statuses whose ``pane_id`` is stale, so renaming or removing would act on the wrong pane.
UNSETTLED_STATUSES = ("missing", "unbound", "kind_changed", "name_conflict", "failed", "starting")


def tree_member(member: Dict[str, Any], charter: Optional[Dict[str, Any]] = None, who: Optional[Dict[str, Any]] = None, row: Optional[PickerRow] = None) -> Dict[str, Any]:
    """A ``roster_line``-shaped dict: ``team.json`` for identity, the live row for state, ``who.json`` for extras."""
    out: Dict[str, Any] = {
        "name": member.get("name"),
        "role": member.get("role"),
        "kind": member.get("kind"),
        "status": member.get("status") or "active",
        "brief": member.get("brief"),
        "terminal_id": member.get("terminal_id"),
        "pane_id": member.get("pane_id"),
        "workspace_id": member.get("workspace_id"),
        "last_seen_at": member.get("last_seen_at"),
        "briefed": member.get("briefed_at") is not None,
        "manager": bool(member.get("manager")),
        "model": member.get("model"),
        "effort": member.get("effort"),
        "agent_status": None,
    }
    if isinstance(charter, dict) and charter.get("seq") is not None:
        try:
            out["charter_stale"] = int(member.get("charter_seq_acked") or 0) < int(charter["seq"])
        except (TypeError, ValueError):
            out["charter_stale"] = False
    if isinstance(who, dict):
        for key in ("agent_status", "pane_id", "workspace_id", "last_headline", "pending_nudges", "hold",
                    "say", "interrupt", "muted_until", "verified_kind", "delivery", "hooks_last_seen"):
            if who.get(key) is not None:
                out[key] = who[key]
    if row is not None:
        # ``agent.list`` is the freshest source for what the pane is doing right now.
        out["agent_status"] = row.agent_status
        out["pane_id"] = row.pane_id
        out["workspace_id"] = row.workspace_id
    if not out.get("agent_status"):
        out["agent_status"] = "unknown"
    return out


def live_rows_by_id(rows: List[PickerRow]) -> Tuple[Dict[str, PickerRow], Dict[str, PickerRow]]:
    """``(by terminal_id, by pane_id)``; the terminal survives a pane move, so it is tried first."""
    by_terminal = {r.terminal_id: r for r in rows if r.terminal_id}
    by_pane = {r.pane_id: r for r in rows if r.pane_id}
    return by_terminal, by_pane


def picker_tree(model: PickerModel) -> List[PickerNode]:
    """Teams with their members, then unassigned agents grouped by Herdr tab.

    ``left`` tombstones and the ``human`` member are never listed: no action
    applies to either. Tab headers use the human label when ``tab.list`` was
    available and retain the stable id so duplicate labels stay unambiguous.
    """
    nodes: List[PickerNode] = []
    by_terminal, by_pane = live_rows_by_id(model.rows)
    for team in sorted(model.rosters):
        members = [m for m in model.rosters[team] if isinstance(m, dict) and m.get("kind") != "human" and m.get("status") != "left"]
        nodes.append(PickerNode(kind="team", key="team:" + team, team=team, label=team))
        if team in model.collapsed:
            continue
        who = model.who_members.get(team) or {}
        charter = model.charters.get(team)
        for member in members:
            row = by_terminal.get(str(member.get("terminal_id") or "")) or by_pane.get(str(member.get("pane_id") or ""))
            name = str(member.get("name") or "?")
            nodes.append(PickerNode(
                kind="member",
                key="member:{}/{}".format(team, name),
                team=team,
                label=name,
                member=tree_member(member, charter, who.get(name), row),
                row=row,
            ))
    free = [r for r in visible_rows(model) if not r.claimed_by]
    if model.rosters:
        nodes.append(PickerNode(kind="section", key="section:unassigned", label="not in a team"))
    groups: Dict[str, List[PickerRow]] = {}
    for row in free:
        tab_id = row.tab_id or "{}:tab?".format(row.workspace_id or "unknown")
        groups.setdefault(tab_id, []).append(row)
    for tab_id, rows in groups.items():
        label = next((row.tab_label for row in rows if row.tab_label), "") or tab_id
        nodes.append(PickerNode(kind="tab", key="tab:" + tab_id, tab_id=tab_id, label=label, count=len(rows)))
        if tab_id in model.collapsed_tabs:
            continue
        for row in rows:
            nodes.append(PickerNode(kind="agent", key="pane:" + row.pane_id, tab_id=tab_id, label=row.name or row.pane_id, row=row))
    return nodes


def node_at(model: PickerModel, nodes: Optional[List[PickerNode]] = None) -> Optional[PickerNode]:
    nodes = picker_tree(model) if nodes is None else nodes
    if not nodes:
        return None
    return nodes[min(max(0, model.cursor), len(nodes) - 1)]


def focus_node(model: PickerModel, key: str) -> bool:
    """Put the cursor on ``key``; False when it is gone (the caller keeps its old position)."""
    for index, node in enumerate(picker_tree(model)):
        if node.key == key:
            model.cursor = index
            return True
    return False


def scroll_window(top: int, cursor: int, count: int, height: int) -> int:
    """Smallest move of ``top`` that keeps ``cursor`` on screen."""
    if height <= 0 or count <= 0:
        return 0
    top = min(max(0, top), max(0, count - height))
    if cursor < top:
        top = cursor
    elif cursor >= top + height:
        top = cursor - height + 1
    return min(max(0, top), max(0, count - height))


def _wrap_picker_text(text: str, width: int, continuation: str = "  ") -> List[str]:
    """Wrap one picker sentence without losing the part past the right edge."""
    width = max(1, width)
    if display_width(text) <= width:
        return [text]
    continuation_width = min(display_width(continuation), max(0, width - 1))
    continuation = truncate_columns(continuation, continuation_width, ellipsis="")
    chunks = wrap_columns(text, width, max(1, width - continuation_width))
    if not chunks:
        return [""]
    return [truncate_columns(chunks[0], width)] + [
        truncate_columns(continuation + chunk, width) for chunk in chunks[1:]
    ]


def _option_window(groups: List[List[str]], selected: int, height: int) -> List[str]:
    """Largest contiguous option window that keeps the selected label whole.

    An option can occupy several wrapped terminal rows.  The markers count as
    rows too, so a short popup never hides the selected choice below the crop.
    """
    if not groups or height <= 0:
        return []
    selected = min(max(0, selected), len(groups) - 1)
    best: Optional[Tuple[Tuple[int, int, int], int, int]] = None
    for low in range(selected + 1):
        for high in range(selected, len(groups)):
            used = sum(len(group) for group in groups[low: high + 1])
            used += int(low > 0) + int(high < len(groups) - 1)
            if used > height:
                continue
            score = (high - low + 1, used, -abs((low + high) - 2 * selected))
            if best is None or score > best[0]:
                best = (score, low, high)
    if best is None:
        rows = list(groups[selected][:height])
        if len(groups[selected]) > height and rows:
            rows[-1] = truncate_columns(rows[-1], max(1, display_width(rows[-1]) - 3), ellipsis="...")
        return rows
    _score, low, high = best
    rows: List[str] = []
    if low > 0:
        rows.append("  ^ {} more option{}".format(low, "" if low == 1 else "s"))
    for group in groups[low: high + 1]:
        rows.extend(group)
    below = len(groups) - high - 1
    if below:
        rows.append("  v {} more option{}".format(below, "" if below == 1 else "s"))
    return rows


def _picker_heading(head: str, keys: str, width: int) -> List[str]:
    """Keep the complete key legend visible by wrapping instead of degrading it."""
    combined = "{}    {}".format(head, keys)
    if display_width(combined) <= width:
        return [combined]
    return [truncate_columns(head, width)] + _wrap_picker_text("keys: " + keys, width, "      ")


def visible_rows(model: PickerModel) -> List[PickerRow]:
    if model.scope_workspace is None:
        return list(model.rows)
    return [r for r in model.rows if r.workspace_id == model.scope_workspace]


def selectable(row: PickerRow) -> bool:
    return not row.launch_pending and not row.claimed_by and row.agent_status != "blocked"


def selected_rows(model: PickerModel) -> List[PickerRow]:
    return [r for r in model.rows if r.selected]


def normalize_team_name(raw: str) -> str:
    text = raw.strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "-", text)
    text = re.sub(r"^[^a-z]+", "", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:15]


def validate_team_name_local(name: str) -> Optional[str]:
    if not TEAM_NAME_RE.match(name or ""):
        return "team name must match [a-z][a-z0-9_-]{{0,{}}}".format(MAX_TEAM_CHARS - 1)
    if name in RESERVED_NAMES:
        return "team name {} is reserved".format(name)
    return None


def validate_role_local(role: str) -> Optional[str]:
    """Error text or None (``roster.validate_role``)."""
    try:
        roster.validate_role(role)
        return None
    except HerdrTeamError as err:
        return err.message


def validate_member_name_local(name: str) -> Optional[str]:
    """Error text or None (``roster.validate_member_name``)."""
    try:
        roster.validate_member_name(name)
        return None
    except HerdrTeamError as err:
        return err.message


def suggest_name(base: str, taken: Iterable[str], cap: int = MAX_MEMBER_NAME_CHARS) -> Optional[str]:
    """``base`` when free, else ``base-2``, ``base-3``, ... only while the cap allows; None otherwise."""
    taken_set = set(taken)
    if base not in taken_set:
        return base if len(base) <= cap else None
    for i in range(2, MAX_NAME_SUFFIX + 1):
        candidate = "{}-{}".format(base, i)
        if len(candidate) > cap:
            return None
        if candidate not in taken_set:
            return candidate
    return None


def taken_names(model: PickerModel, exclude: Optional[PickerRow] = None) -> Set[str]:
    """Live agent names plus names already chosen for other selected rows."""
    names: Set[str] = set()
    for row in model.rows:
        if row is exclude:
            continue
        if row.name:
            names.add(row.name)
        if row.selected and row.member_name:
            names.add(row.member_name)
    names.update(n for n in model.live_names if n)
    if exclude is not None and exclude.name:
        # the agent's own current name is free for itself
        names.discard(exclude.name)
    return names


DEFAULT_ROLE_SUFFIX = "-dev"


def default_role(row: PickerRow) -> str:
    """``<kind>-dev``: the roster refuses a bare kind label as a role (plan 12), so the default carries a suffix."""
    kind = re.sub(r"[^a-z0-9_-]+", "-", (row.kind or "agent").lower()).strip("-")
    for candidate in (kind + DEFAULT_ROLE_SUFFIX, kind[: 32 - len(DEFAULT_ROLE_SUFFIX)].rstrip("-") + DEFAULT_ROLE_SUFFIX, "dev"):
        if ROLE_NAME_RE.match(candidate) and validate_role_local(candidate) is None:
            return candidate
    return "dev"


def default_member_name(model: PickerModel, row: PickerRow) -> Optional[str]:
    base = roster.fit_member_name(model.team_name, row.role, MAX_MEMBER_NAME_CHARS) if row.role else model.team_name
    return suggest_name(base, taken_names(model, exclude=row))


def create_spec(model: PickerModel) -> Dict[str, Any]:
    members = []
    for row in selected_rows(model):
        members.append(
            {
                "target": row.pane_id,
                "terminal_id": row.terminal_id,
                "kind": row.kind,
                "role": row.role,
                "name": row.member_name,
                "brief": row.brief or None,
                "setting": row.setting or None,
                "renamed": bool(row.name and row.name != row.member_name),
            }
        )
    return {"team": model.team_name, "charter": model.charter or None, "rules": model.rules or None, "project": model.project or None,
            "naming": "prefixed", "members": members, "mode": model.mode}


def _set_input(model: PickerModel, text: str) -> None:
    model.input = text
    model.cursor_pos = len(text)


def _begin_member_field(model: PickerModel) -> None:
    rows = selected_rows(model)
    if not rows:
        model.stage = "select"
        model.error = "select at least one agent"
        return
    if model.member_index >= len(rows):
        model.stage = "confirm"
        model.input = ""
        model.cursor_pos = 0
        return
    row = rows[model.member_index]
    if model.member_field == "role":
        _set_input(model, row.role or default_role(row))
    elif model.member_field == "name":
        if row.member_name:
            _set_input(model, row.member_name)
        else:
            suggestion = default_member_name(model, row)
            if suggestion is None:
                model.error = "no free name fits under {} chars for role {}; type a shorter one".format(MAX_MEMBER_NAME_CHARS, row.role)
                _set_input(model, "")
            else:
                _set_input(model, suggestion)
    elif model.member_field == "brief":
        _set_input(model, row.brief or "")
    else:
        _set_input(model, row.setting or "")


def picker_apply_key(model: PickerModel, key: str) -> Optional[Intent]:
    """Drive the picker state machine; ``create`` on the confirm screen, ``quit`` on Esc/Ctrl-C."""
    if key == "CTRL_C":
        return Intent("quit")
    if key == "RESIZE":
        return None
    if model.pending_action is not None:
        # A destructive action waiting for confirmation. Unlike the console's ``pending_confirm``,
        # ENTER is NOT yes: Enter opens the menu and picks the action, so a third Enter (or one held
        # key) would remove a member nobody meant to remove.
        pending = model.pending_action
        if key in ("y", "Y"):
            model.pending_action = None
            model.status = None
            return pending
        if key in ("n", "N", "ESC"):
            model.pending_action = None
            model.status = "cancelled"
            return None
        return None
    if model.stage == "select":
        return _select_key(model, key)
    if model.stage == "topology":
        return _topology_key(model, key)
    if model.stage == "restore_results":
        if key in ("ESC", "q", "ENTER"):
            model.stage = "select"
            model.error = None
        elif key == "g" and model.restore_pane:
            return Intent("restore_focus", {"pane_id": model.restore_pane})
        elif key in ("UP", "k", "PGUP"):
            model.restore_top = max(0, model.restore_top - (model.page_rows if key == "PGUP" else 1))
        elif key in ("DOWN", "j", "PGDN"):
            model.restore_top += model.page_rows if key == "PGDN" else 1
        elif key == "HOME":
            model.restore_top = 0
        elif key == "END":
            model.restore_top = 1 << 30
        return None
    if model.stage == "actions":
        return _actions_key(model, key)
    if model.stage == "rename":
        return _rename_key(model, key)
    if model.stage == "goal":
        return _goal_key(model, key)
    if model.stage == "model":
        return _model_key(model, key)
    if model.stage == "target":
        return _target_key(model, key)
    if model.stage == "name":
        return _name_key(model, key)
    if model.stage == "rules":
        return _rules_key(model, key)
    if model.stage == "project":
        return _project_key(model, key)
    if model.stage == "team_folder":
        return _team_folder_key(model, key)
    if model.stage == "link_pick":
        return _link_pick_key(model, key)
    if model.stage == "charter":
        return _charter_key(model, key)
    if model.stage == "members":
        return _members_key(model, key)
    if model.stage == "confirm":
        if key == "ENTER":
            return Intent("create", create_spec(model))
        if key == "ESC":
            rows = selected_rows(model)
            model.stage = "members"
            model.member_index = max(0, len(rows) - 1)
            model.member_field = "model"
            _begin_member_field(model)
            model.error = None
        return None
    return None


def _select_key(model: PickerModel, key: str) -> Optional[Intent]:
    """The team tree: move, fold, pick unassigned agents, or open a member's actions."""
    nodes = picker_tree(model)
    model.error = None
    if nodes:
        model.cursor = min(max(0, model.cursor), len(nodes) - 1)
    node = nodes[model.cursor] if nodes else None
    if key in ("PASTE_START", "PASTE_END"):
        # A paste in a list stage would otherwise arrive as keystrokes and fire actions.
        model.paste_mode = key == "PASTE_START"
        return None
    if model.paste_mode:
        return None
    if key in ("ESC", "q"):
        return Intent("quit")
    if key in ("UP", "k"):
        model.cursor = max(0, model.cursor - 1)
        return None
    if key in ("DOWN", "j"):
        model.cursor = min(max(0, len(nodes) - 1), model.cursor + 1)
        return None
    if key == "PGUP":
        model.cursor = max(0, model.cursor - max(1, model.page_rows))
        return None
    if key == "PGDN":
        model.cursor = min(max(0, len(nodes) - 1), model.cursor + max(1, model.page_rows))
        return None
    if key == "HOME":
        model.cursor = 0
        return None
    if key == "END":
        model.cursor = max(0, len(nodes) - 1)
        return None
    if key == "r":
        return Intent("refresh")
    if key == "w":
        keep = node.key if node is not None else ""
        if model.scope_workspace is None and model.focused_workspace:
            model.scope_workspace = model.focused_workspace
        else:
            model.scope_workspace = None
        if not focus_node(model, keep):
            model.cursor = 0
        return None
    if key == "a":
        candidates = [r for r in visible_rows(model) if selectable(r) and not r.claimed_by]
        all_selected = bool(candidates) and all(r.selected for r in candidates)
        for r in candidates:
            r.selected = not all_selected
        return None
    if key == "v":
        model.stage = "topology"
        model.topology_top = 0
        model.status = None
        return None
    if node is None:
        return None
    if key == "s":
        if node.kind != "team":
            model.error = "put the cursor on a team to restore it"
            return None
        return Intent("team_restore", {"team": node.team, "workspace": model.focused_workspace})
    if key == "g" and node.kind == "team" and node.team == model.restore_team and model.restore_pane:
        return Intent("restore_focus", {"pane_id": model.restore_pane})
    if key == "g":
        if node.kind not in ("member", "agent") or node.row is None:
            model.error = "put the cursor on an agent to go to its pane"
            return None
        return Intent("agent_focus", {
            "pane_id": node.row.pane_id,
            "terminal_id": node.row.terminal_id,
            "member": node.label,
        })
    if key == "b":
        # Open that team's board. A session may have one console per team, so
        # this adds a board rather than switching an existing one.
        team = node.team if node.kind in ("team", "member") else ""
        if not team:
            model.error = "put the cursor on a team to open its board"
            return None
        return Intent("team_board_open", {"team": team})
    if key == "x":
        # Dissolving is the one destructive thing in this tree, so it asks, and
        # the question says where the team goes rather than just "are you sure":
        # the directory is archived, not deleted, and the panes keep running.
        team = node.team if node.kind in ("team", "member") else ""
        if not team:
            model.error = "put the cursor on a team to dissolve it"
            return None
        members = len([m for m in (model.rosters.get(team) or []) if m.get("kind") != "human"])
        intent = Intent("team_dissolve", {"team": team, "members": members})
        model.pending_action = intent
        model.status = _confirm_question(intent)
        return None
    if key == "c":
        # Connect this team to another through their managers, or break that
        # connection: one chooser, one key, and the row says which it will do.
        team = node.team if node.kind in ("team", "member") else ""
        if not team:
            model.error = "put the cursor on a team to connect it"
            return None
        if not [t for t in model.rosters if t != team]:
            model.error = "no other team to connect {} to".format(team)
            return None
        model.link_team = team
        model.link_index = 0
        model.stage = "link_pick"
        model.error = None
        return None
    if key == "f":
        team = node.team if node.kind in ("team", "member") else ""
        if not team:
            model.error = "put the cursor on a team to set its folder"
            return None
        model.folder_team = team
        model.stage = "team_folder"
        model.error = None
        _set_input(model, str((model.folders.get(team) or {}).get("project_dir") or team_shared_dir(model, team)))
        return None
    if key in ("LEFT", "h"):
        if node.kind == "member":
            focus_node(model, "team:" + node.team)
        elif node.kind == "team":
            model.collapsed.add(node.team)
        elif node.kind == "agent":
            focus_node(model, "tab:" + node.tab_id)
        elif node.kind == "tab":
            model.collapsed_tabs.add(node.tab_id)
        return None
    if key in ("RIGHT", "l"):
        if node.kind == "team":
            model.collapsed.discard(node.team)
        elif node.kind == "tab":
            model.collapsed_tabs.discard(node.tab_id)
        return None
    if key in (" ", "SPACE"):
        if node.kind in ("team", "section", "tab"):
            return _toggle_collapse(model, node)
        if node.kind == "member":
            model.error = "{} is in team {}; Enter opens its actions".format(node.label, node.team)
            return None
        return _toggle_agent(model, node.row)
    if key == "ENTER":
        if node.kind == "member":
            return _open_actions(model, node)
        if node.kind == "team":
            if selected_rows(model):
                _enter_add_mode(model, node.team)
                return None
            return _toggle_collapse(model, node)
        if node.kind == "section":
            return _toggle_collapse(model, node)
        if node.kind == "tab":
            if selected_rows(model):
                return _advance_from_select(model)
            return _toggle_collapse(model, node)
        if node.kind == "agent" and node.row is not None and not selected_rows(model) and selectable(node.row):
            node.row.selected = True  # Enter on a single agent means "this one"
        return _advance_from_select(model)
    return None


def _topology_key(model: PickerModel, key: str) -> Optional[Intent]:
    """Scroll the read-only organization map, or return to the ordinary team tree."""
    model.error = None
    if key in ("ESC", "q", "v"):
        model.stage = "select"
        model.status = None
        return None
    if key == "r":
        return Intent("refresh")
    if key in ("UP", "k"):
        model.topology_top = max(0, model.topology_top - 1)
    elif key in ("DOWN", "j"):
        model.topology_top += 1
    elif key == "PGUP":
        model.topology_top = max(0, model.topology_top - max(1, model.page_rows))
    elif key == "PGDN":
        model.topology_top += max(1, model.page_rows)
    elif key == "HOME":
        model.topology_top = 0
    elif key == "END":
        # The renderer clamps this after wrapping for the current terminal width.
        model.topology_top = 1 << 30
    return None


def folder_summary(info: Dict[str, Any]) -> str:
    """The tree's one-phrase version of a team's knowledge-base status."""
    if not info.get("project_dir"):
        return "none"
    if info.get("issues"):
        return str(info["issues"][0])
    total = len(info.get("members") or [])
    out = "{} rules, {}/{} with Missions, {} finding{}".format(
        "has" if info.get("rules") else "no", info.get("with_missions", 0), total,
        info.get("findings", 0), "" if info.get("findings") == 1 else "s")
    missing = info.get("missing_missions") or []
    if missing:
        out += ", Mission missing: {}".format(", ".join(str(name) for name in missing))
    waiting = info.get("awaiting_adopt") or []
    if waiting:
        out += ", {} edit{} to adopt".format(len(waiting), "" if len(waiting) == 1 else "s")
    return out


def team_shared_dir(model: PickerModel, team: str) -> str:
    """The directory that team's live members share, for prefilling the folder prompt."""
    counts: Dict[str, int] = {}
    order: List[str] = []
    for member in model.rosters.get(team, []) or []:
        if not isinstance(member, dict) or member.get("kind") == "human" or member.get("status") == "left":
            continue
        cwd = str(member.get("cwd") or "").strip()
        if not cwd or not os.path.isdir(cwd):
            continue
        if cwd not in counts:
            order.append(cwd)
        counts[cwd] = counts.get(cwd, 0) + 1
    if not order:
        return ""
    position = {path: index for index, path in enumerate(order)}
    return min(order, key=lambda path: (-counts[path], position[path]))


def _team_folder_key(model: PickerModel, key: str) -> Optional[Intent]:
    if key in ("PASTE_START", "PASTE_END"):
        model.paste_mode = key == "PASTE_START"
        return None
    if key == "ESC":
        model.stage = "select"
        model.error = None
        _set_input(model, "")
        return None
    if key == "ENTER" and not model.paste_mode:
        path = model.input.strip()
        if not path:
            model.error = "type a directory, or Esc to leave it alone"
            return None
        return Intent("team_folder_set", {"team": model.folder_team, "path": path})
    edit_key(_TextView(model), key)
    return None


def link_options(model: PickerModel) -> List[Dict[str, Any]]:
    """The ``c`` chooser's rows for ``model.link_team``: every other team, and what Enter would do to it."""
    team = model.link_team
    linked = {str(r.get("team")): r for r in (model.links.get(team) or [])}
    rows: List[Dict[str, Any]] = []
    for other in sorted(t for t in model.rosters if t != team):
        manager = model.managers.get(other)
        if other in linked:
            state = str(linked[other].get("state") or "active")
            rows.append({"team": other, "action": "unlink", "manager": manager,
                         "label": "{} {}  linked{} - Enter breaks the link".format("<->" if model.ascii_only else "⇄", other, " ({})".format(state) if state != "active" else "")})
        elif manager is None:
            rows.append({"team": other, "action": None, "manager": None,
                         "label": "   {}  no manager - set one first (8 on one of its members)".format(other)})
        else:
            rows.append({"team": other, "action": "link", "manager": manager,
                         "label": "   {}  manager {} - Enter links the teams".format(other, manager)})
    return rows


def _link_pick_key(model: PickerModel, key: str) -> Optional[Intent]:
    rows = link_options(model)
    if key == "ESC":
        model.stage = "select"
        model.error = None
        return None
    if key in ("UP", "k"):
        model.link_index = max(0, model.link_index - 1)
        return None
    if key in ("DOWN", "j"):
        model.link_index = min(max(0, len(rows) - 1), model.link_index + 1)
        return None
    if key == "ENTER":
        if not rows:
            model.stage = "select"
            return None
        row = rows[min(model.link_index, len(rows) - 1)]
        if model.managers.get(model.link_team) is None:
            model.error = "{} has no manager; the link runs through the managers, so set one first (8 on one of its members)".format(model.link_team)
            return None
        if row["action"] is None:
            model.error = "{} has no manager; set one first (8 on one of its members)".format(row["team"])
            return None
        model.stage = "select"
        model.error = None
        return Intent("team_link", {"team": model.link_team, "other": row["team"], "action": row["action"], "other_manager": row.get("manager")})
    return None


def picker_styles(model: PickerModel, lines: List[str]) -> List[str]:
    """A style key per rendered line: the manager's row is ``STYLE_MANAGER`` in the tree, everything else plain."""
    if model.stage != "select":
        return [STYLE_PLAIN] * len(lines)
    marker = re.compile(r"^(?:> |  )  \S+ (\u2605|\*) ")
    return [STYLE_MANAGER if marker.match(line) else STYLE_PLAIN for line in lines]


def _toggle_collapse(model: PickerModel, node: PickerNode) -> None:
    if node.kind == "team":
        if node.team in model.collapsed:
            model.collapsed.discard(node.team)
        else:
            model.collapsed.add(node.team)
    elif node.kind == "tab":
        if node.tab_id in model.collapsed_tabs:
            model.collapsed_tabs.discard(node.tab_id)
        else:
            model.collapsed_tabs.add(node.tab_id)
    else:  # the unassigned section folds every team away, or brings them all back
        if model.collapsed >= set(model.rosters):
            model.collapsed.clear()
        else:
            model.collapsed.update(model.rosters)
    return None


def _toggle_agent(model: PickerModel, row: Optional[PickerRow]) -> None:
    if row is None:
        return None
    if row.launch_pending:
        model.error = "{} is still launching; wait for it to settle".format(row.pane_id)
    elif row.claimed_by:
        model.error = "{} is already in team {} (use the CLI with --steal)".format(row.name or row.pane_id, row.claimed_by)
    elif row.agent_status == "blocked":
        model.error = "{} is blocked at a dialog; settle it first".format(row.name or row.pane_id)
    else:
        row.selected = not row.selected
    return None


def _advance_from_select(model: PickerModel) -> Optional[Intent]:
    if not selected_rows(model):
        model.error = "select at least one agent (Space toggles, a selects all)"
        return None
    if model.existing_teams:
        model.stage = "target"
        model.target_index = 0
        model.status = None
        return None
    model.mode = "create"
    model.stage = "name"
    _set_input(model, model.team_name)
    return None


def _open_actions(model: PickerModel, node: PickerNode) -> None:
    model.stage = "actions"
    model.action_team = node.team
    model.action_member = node.label
    model.action_index = 0
    model.status = None
    model.pending_action = None
    return None


def target_options(model: PickerModel) -> List[Tuple[str, str]]:
    """Rows of the target stage: ``("add", team)`` per existing team, then ``("create", "")``."""
    return [("add", team) for team in model.existing_teams] + [("create", "")]


def _enter_add_mode(model: PickerModel, team: str) -> None:
    """The selected agents join ``team``; its charter stays as it is, so the wizard goes straight to the members."""
    count = len(selected_rows(model))
    model.team_name = team
    model.mode = "add"
    model.charter = ""
    model.charter_lines = []
    model.rules = ""
    model.rules_lines = []
    model.error = None
    model.status = "adding {} agent{} to team {}; its charter is kept".format(count, "" if count == 1 else "s", team)
    model.stage = "members"
    model.member_index = 0
    model.member_field = "role"
    _begin_member_field(model)


def _target_key(model: PickerModel, key: str) -> Optional[Intent]:
    options = target_options(model)
    model.error = None
    if key in ("ESC", "q"):
        model.stage = "select"
        return None
    if key in ("UP", "k"):
        model.target_index = max(0, model.target_index - 1)
        return None
    if key in ("DOWN", "j"):
        model.target_index = min(len(options) - 1, model.target_index + 1)
        return None
    if len(key) == 1 and key.isdigit():
        number = int(key)
        if 1 <= number <= len(options):
            model.target_index = number - 1
            return _choose_target(model, options[number - 1])
        model.error = "type a number between 1 and {}".format(len(options))
        return None
    if key == "ENTER":
        return _choose_target(model, options[min(model.target_index, len(options) - 1)])
    return None


def _choose_target(model: PickerModel, option: Tuple[str, str]) -> Optional[Intent]:
    action, team = option
    if action == "add":
        _enter_add_mode(model, team)
        return None
    model.mode = "create"
    model.stage = "name"
    model.status = None
    _set_input(model, model.team_name)
    return None


def _name_key(model: PickerModel, key: str) -> Optional[Intent]:
    if key == "ESC":
        model.stage = "target" if model.existing_teams else "select"
        model.error = None
        return None
    if key == "ENTER":
        name = normalize_team_name(model.input)
        err = validate_team_name_local(name)
        if err:
            model.error = err
            return None
        if name in model.existing_teams:
            # Typed the name of a team that exists: that is the add option of the target stage.
            _enter_add_mode(model, name)
            return None
        model.mode = "create"
        model.team_name = name
        model.error = None
        model.stage = "charter"
        model.charter_lines = model.charter.split("\n") if model.charter else []
        _set_input(model, "")
        return None
    edit_key(_TextView(model), key)
    return None


def _charter_key(model: PickerModel, key: str) -> Optional[Intent]:
    view = _TextView(model)
    if key == "ESC":
        model.stage = "name"
        model.error = None
        _set_input(model, model.team_name)
        return None
    if key == "CTRL_O":
        return Intent("load_file")
    if key == "TAB":
        model.charter = ""
        model.charter_lines = []
        return _finish_charter(model)
    if key == "ENTER" and not model.paste_mode:
        if model.input == "" and model.charter_lines:
            return _finish_charter(model)
        model.charter_lines.append(model.input)
        _set_input(model, "")
        return None
    if key in ("ALT_ENTER", "CTRL_D"):
        if model.input:
            model.charter_lines.append(model.input)
            _set_input(model, "")
        return _finish_charter(model)
    edit_key(view, key)
    return None


def _finish_charter(model: PickerModel) -> Optional[Intent]:
    text = "\n".join(model.charter_lines).strip()
    if len(text) > MAX_CHARTER_CHARS:
        model.error = "charter is {} chars; max {} (use create --charter-file for longer text)".format(len(text), MAX_CHARTER_CHARS)
        return None
    model.charter = text
    model.error = None
    model.stage = "rules"
    model.rules_lines = model.rules.split("\n") if model.rules else []
    _set_input(model, "")
    return None


def _rules_key(model: PickerModel, key: str) -> Optional[Intent]:
    view = _TextView(model)
    if key == "ESC":
        model.stage = "charter"
        model.error = None
        _set_input(model, "")
        return None
    if key == "CTRL_O":
        return Intent("load_file")
    if key == "TAB":
        model.rules = ""
        model.rules_lines = []
        return _finish_rules(model)
    if key == "ENTER" and not model.paste_mode:
        if model.input == "" and model.rules_lines:
            return _finish_rules(model)
        model.rules_lines.append(model.input)
        _set_input(model, "")
        return None
    if key in ("ALT_ENTER", "CTRL_D"):
        if model.input:
            model.rules_lines.append(model.input)
            _set_input(model, "")
        return _finish_rules(model)
    edit_key(view, key)
    return None


def _finish_rules(model: PickerModel) -> Optional[Intent]:
    text = "\n".join(model.rules_lines).strip()
    if len(text) > MAX_RULES_CHARS:
        model.error = "rules are {} chars; max {}".format(len(text), MAX_RULES_CHARS)
        return None
    model.rules = text
    model.error = None
    model.stage = "project"
    _set_input(model, suggested_project_dir(model))
    return None


def suggested_project_dir(model: PickerModel) -> str:
    """The directory most of the selected agents already sit in, or "".

    Only a suggestion: the operator still presses Enter, because the plugin
    never writes into a repository nobody named.
    """
    counts: Dict[str, int] = {}
    order: List[str] = []
    for row in selected_rows(model):
        cwd = (row.cwd or "").strip()
        if not cwd or not os.path.isdir(cwd):
            continue
        if cwd not in counts:
            order.append(cwd)
        counts[cwd] = counts.get(cwd, 0) + 1
    if not order:
        return ""
    # Positions captured first: sorting a list while indexing into it is a bug.
    position = {path: index for index, path in enumerate(order)}
    return min(order, key=lambda path: (-counts[path], position[path]))


def _project_key(model: PickerModel, key: str) -> Optional[Intent]:
    if key == "ESC":
        model.stage = "rules"
        model.error = None
        _set_input(model, "")
        return None
    if key == "TAB":
        model.project = ""
        return _finish_project(model)
    if key == "ENTER":
        model.project = model.input.strip()
        return _finish_project(model)
    edit_key(_TextView(model), key)
    return None


def _finish_project(model: PickerModel) -> Optional[Intent]:
    model.error = None
    model.stage = "members"
    model.member_index = 0
    model.member_field = "role"
    _begin_member_field(model)
    return None


def _members_key(model: PickerModel, key: str) -> Optional[Intent]:
    rows = selected_rows(model)
    if not rows:
        model.stage = "select"
        return None
    row = rows[min(model.member_index, len(rows) - 1)]
    if key == "ESC":
        if model.member_field == "model":
            model.member_field = "brief"
        elif model.member_field == "brief":
            model.member_field = "name"
        elif model.member_field == "name":
            model.member_field = "role"
        elif model.member_index > 0:
            model.member_index -= 1
            model.member_field = "model"
        elif model.mode == "add":
            model.stage = "target"
            model.status = None
            return None
        else:
            # Back one stage, which is now the folder, not the charter.
            model.stage = "project"
            _set_input(model, model.project or suggested_project_dir(model))
            return None
        model.error = None
        _begin_member_field(model)
        return None
    if key == "ENTER":
        value = model.input.strip()
        if model.member_field == "role":
            role = value.lower()
            err = validate_role_local(role)
            if err:
                model.error = err
                return None
            if role != row.role:
                row.member_name = ""
            row.role = role
            model.member_field = "name"
        elif model.member_field == "name":
            name = value.lower()
            err = validate_member_name_local(name)
            if err:
                model.error = err
                return None
            if name in taken_names(model, exclude=row):
                model.error = "name {} is taken (live agent or another selection)".format(name)
                return None
            row.member_name = name
            model.member_field = "brief"
        elif model.member_field == "brief":
            if not value:
                model.error = "Mission / brief is required before this member can join"
                return None
            if len(value) > MAX_BRIEF_CHARS:
                model.error = "brief is {} chars; max {}".format(len(value), MAX_BRIEF_CHARS)
                return None
            row.brief = value
            model.member_field = "model"
        else:
            if value:
                from herdr_team import models as _models
                from herdr_team.errors import HerdrTeamError as _Err

                try:
                    _models.validate(row.kind, *_models.parse_setting(value))
                except _Err as err:
                    model.error = err.message
                    return None
            row.setting = value
            model.member_index += 1
            model.member_field = "role"
        model.error = None
        _begin_member_field(model)
        return None
    edit_key(_TextView(model), key)
    return None


PICKER_TEXT_STAGES = ("name", "charter", "rules", "members", "rename", "goal", "model")

#: The member action menu, in the order it is shown. ``{team}`` is filled in per member.
ACTION_OPTIONS = (
    ("rename", "rename it (the team name and the Herdr agent name)"),
    ("goal", "change its goal"),
    ("send_goal", "send the goal to it now"),
    ("remove", "remove it from {team}"),
    ("remove_keep", "remove it from {team}, keep its Herdr agent name"),
    ("focus", "go to its pane (closes this popup)"),
    ("resume", "show the command that reopens its own session (herdr-synapse resume)"),
    ("manager", "make it the team manager"),
    ("model", "set its model and effort (Claude and OpenCode effort can apply live; other changes at resume)"),
)


def action_label(action: str, member: Optional[Dict[str, Any]]) -> Optional[str]:
    """A label that depends on the member, or None to use the static one."""
    if action == "manager" and member is not None and member.get("manager"):
        return "it is the team manager (this clears that)"
    return None


def action_member(model: PickerModel) -> Optional[Dict[str, Any]]:
    """The tree's dict for the member the action menu is about, or None when it is gone."""
    for node in picker_tree(model):
        if node.kind == "member" and node.team == model.action_team and node.label == model.action_member:
            return node.member
    return None


def action_options(model: PickerModel) -> List[Tuple[str, str]]:
    return [(key, label.format(team=model.action_team)) for key, label in ACTION_OPTIONS]


def _action_refusal(action: str, member: Dict[str, Any]) -> Optional[str]:
    """Why an action cannot run against this member right now, or None."""
    status = str(member.get("status") or "active")
    if status in UNSETTLED_STATUSES and action in ("rename", "send_goal", "focus"):
        if status == "missing":
            return "{} has no live agent right now; bind it first (herdr-synapse bind)".format(member.get("name"))
        return "{} is {}; settle it first".format(member.get("name"), status.replace("_", " "))
    return None


def _confirm_question(intent: "Intent") -> str:
    args = intent.args
    if intent.kind == "member_remove":
        tail = "its team tokens and pane label are cleared" if args.get("keep_name") else "its team tokens, pane label and Herdr agent name are cleared"
        return "remove {} from {}? {} - y removes, n cancels".format(args.get("member"), args.get("team"), tail)
    if intent.kind == "team_dissolve":
        members = args.get("members") or 0
        return "dissolve {}? its {} member{} are released and the board is archived, not deleted - y dissolves, n cancels".format(
            args.get("team"), members, "" if members == 1 else "s")
    return "{}? y/n".format(intent.kind)


def _start_action(model: PickerModel, action: str) -> Optional[Intent]:
    member = action_member(model)
    if member is None:
        model.stage = "select"
        model.error = "{} is not in {} any more; press r to refresh".format(model.action_member, model.action_team)
        return None
    refusal = _action_refusal(action, member)
    if refusal:
        model.error = refusal
        return None
    name = str(member.get("name"))
    base = {"team": model.action_team, "member": name, "terminal_id": member.get("terminal_id")}
    if action == "rename":
        model.stage = "rename"
        model.error = None
        _set_input(model, name)
        return None
    if action == "goal":
        model.stage = "goal"
        model.error = None
        _set_input(model, str(member.get("brief") or ""))
        return None
    if action == "model":
        model.stage = "model"
        model.error = None
        _set_input(model, str(member.get("setting") or ""))
        return None
    if action == "send_goal":
        return Intent("member_send_goal", dict(base))
    if action == "resume":
        return Intent("member_resume", dict(base))
    if action == "manager":
        # A toggle: the same row sets it and clears it, so the operator never
        # has to remember which of two commands they want.
        return Intent("member_manager", dict(base, clear=bool(member.get("manager"))))
    if action in ("remove", "remove_keep"):
        intent = Intent("member_remove", dict(base, keep_name=action == "remove_keep"))
        model.pending_action = intent
        model.status = _confirm_question(intent)
        return None
    return Intent("member_focus", dict(base))


def _actions_key(model: PickerModel, key: str) -> Optional[Intent]:
    options = action_options(model)
    model.error = None
    if key in ("PASTE_START", "PASTE_END"):
        model.paste_mode = key == "PASTE_START"
        return None
    if model.paste_mode:
        return None
    if key in ("ESC", "q"):
        model.stage = "select"
        model.status = None
        return None
    if key in ("UP", "k"):
        model.action_index = max(0, model.action_index - 1)
        return None
    if key in ("DOWN", "j"):
        model.action_index = min(len(options) - 1, model.action_index + 1)
        return None
    if len(key) == 1 and key.isdigit():
        number = int(key)
        if 1 <= number <= len(options):
            model.action_index = number - 1
            return _start_action(model, options[number - 1][0])
        model.error = "type a number between 1 and {}".format(len(options))
        return None
    if key == "ENTER":
        return _start_action(model, options[min(model.action_index, len(options) - 1)][0])
    return None


def _rename_key(model: PickerModel, key: str) -> Optional[Intent]:
    if key == "ESC":
        model.stage = "actions"
        model.error = None
        return None
    if key == "ENTER":
        member = action_member(model)
        if member is None:
            model.stage = "select"
            model.error = "{} is not in {} any more; press r to refresh".format(model.action_member, model.action_team)
            return None
        new = model.input.strip().lower()
        current = str(member.get("name"))
        if new == current:
            model.stage = "actions"
            model.status = "name unchanged"
            return None
        err = validate_member_name_local(new)
        if err:
            model.error = err
            return None
        if new in rename_taken_names(model, current):
            model.error = "{} is taken (a member or a live agent already answers to it)".format(new)
            return None
        return Intent("member_rename", {"team": model.action_team, "member": current, "new": new, "terminal_id": member.get("terminal_id")})
    edit_key(_TextView(model), key)
    return None


def rename_taken_names(model: PickerModel, current: str) -> Set[str]:
    """Every member name in the session plus every live agent name, minus the member's own."""
    taken = set(model.live_names)
    for members in model.rosters.values():
        for member in members:
            if isinstance(member, dict) and member.get("status") != "left" and member.get("name"):
                taken.add(str(member["name"]))
    taken.discard(current)
    return taken


def _goal_key(model: PickerModel, key: str) -> Optional[Intent]:
    if key == "ESC":
        model.stage = "actions"
        model.error = None
        return None
    if key == "ENTER":
        member = action_member(model)
        if member is None:
            model.stage = "select"
            model.error = "{} is not in {} any more; press r to refresh".format(model.action_member, model.action_team)
            return None
        text = model.input.strip()
        if len(text) > MAX_BRIEF_TOTAL_CHARS:
            model.error = "goal is {} chars; max {}".format(len(text), MAX_BRIEF_TOTAL_CHARS)
            return None
        if text == str(member.get("brief") or ""):
            model.stage = "actions"
            model.status = "goal unchanged"
            return None
        return Intent("member_goal", {"team": model.action_team, "member": str(member.get("name")), "text": text, "terminal_id": member.get("terminal_id")})
    edit_key(_TextView(model), key)
    return None



def _model_key(model: PickerModel, key: str) -> Optional[Intent]:
    if key == "ESC":
        model.stage = "actions"
        model.error = None
        return None
    if key == "ENTER":
        member = action_member(model)
        if member is None:
            model.stage = "select"
            model.error = "{} is not in {} any more; press r to refresh".format(model.action_member, model.action_team)
            return None
        text = model.input.strip()
        from herdr_team import models as _models
        from herdr_team.errors import HerdrTeamError as _Err

        try:
            parsed = _models.parse_setting(text)
            _models.validate(member.get("kind"), *parsed)
        except _Err as err:
            model.error = err.message
            return None
        return Intent("member_model", {"team": model.action_team, "member": str(member.get("name")), "setting": text, "terminal_id": member.get("terminal_id")})
    edit_key(_TextView(model), key)
    return None


def picker_cursor(model: PickerModel, lines: List[str], width: int) -> Optional[Tuple[int, int]]:
    """Screen (row, col) of the text cursor: on the input line of a typing stage, None (hidden) for the list stages.

    The input line follows its prompt and any error or status comes after
    it, so the row is found by its prompt marker rather than assumed last.
    """
    if model.stage not in PICKER_TEXT_STAGES or not lines:
        return None
    rows = [i for i, line in enumerate(lines) if line.startswith(INPUT_PROMPT)]
    if not rows:
        return None
    col = display_width(INPUT_PROMPT) + display_width(model.input[: model.cursor_pos])
    return rows[-1], min(max(0, width - 1), col)


def _team_header(model: PickerModel, node: PickerNode, width: int) -> str:
    size = len([m for m in model.rosters.get(node.team, []) if isinstance(m, dict) and m.get("kind") != "human" and m.get("status") != "left"])
    if model.ascii_only:
        glyph = "+" if node.team in model.collapsed else "-"
    else:
        glyph = "▸" if node.team in model.collapsed else "▾"
    text = "{} {}  ({} member{})".format(glyph, node.team, size, "" if size == 1 else "s")
    info = model.folders.get(node.team)
    if info is not None and degrade_level(width) < 2:
        # A team with no shared folder is the thing worth spotting from the list.
        if not info.get("project_dir"):
            text += "  " + ("[no folder]" if model.ascii_only else "▫ no folder")
        elif info.get("issues"):
            text += "  " + ("[!]" if model.ascii_only else "⚠")
        missing = info.get("missing_missions") or []
        if missing:
            label = "Mission missing: {}".format(", ".join(str(name) for name in missing))
            text += "  " + ("[! {}]".format(label) if model.ascii_only else "⚠ " + label)
    if degrade_level(width) < 2:
        manager = model.managers.get(node.team)
        text += "  " + (("manager: " + str(manager)) if manager else "no manager")
        for row in model.links.get(node.team) or []:
            state = str(row.get("state") or "")
            text += "  {} {}{}".format("<->" if model.ascii_only else "⇄", row.get("team"), " ({})".format(state) if state and state != "active" else "")
    charter = model.charters.get(node.team) or {}
    if degrade_level(width) == 0 and charter.get("text"):
        text += '  "{}"'.format(headline(str(charter["text"]), 40))
    return text


def _tab_header(model: PickerModel, node: PickerNode) -> str:
    """One unassigned-agent group, named by Herdr and disambiguated by stable id."""
    if model.ascii_only:
        glyph = "+" if node.tab_id in model.collapsed_tabs else "-"
    else:
        glyph = "▸" if node.tab_id in model.collapsed_tabs else "▾"
    label = headline(node.label, 80) or node.tab_id
    return "{} tab {}  ({} · {} agent{})".format(
        glyph,
        label,
        node.tab_id,
        node.count,
        "" if node.count == 1 else "s",
    )


def _topology_lines(model: PickerModel, width: int, height: int) -> List[str]:
    """A scrollable ASCII map of every team and manager-to-manager link."""
    teams = len(model.rosters)
    link_total = team_topology.link_count(model.links, model.managers)
    head = "Team topology | {} team{} | {} link{}".format(
        teams, "" if teams == 1 else "s", link_total, "" if link_total == 1 else "s")
    header = _picker_heading(head, "v/Esc tree | r refresh | Up/Down scroll | PgUp/PgDn page", width)
    body: List[str] = []
    for logical in team_topology.lines(model.rosters, model.managers, model.links, model.who_members):
        if not logical:
            body.append("")
            continue
        continuation = "|       " if logical.startswith("|") else ("      " if logical.startswith("  ") else "    ")
        body.extend(_wrap_picker_text(logical, width, continuation))
    status_rows = int(bool(model.error or model.status))
    capacity = max(1, height - len(header) - status_rows)
    scrolling = len(body) > capacity
    if scrolling:
        capacity = max(1, capacity - 1)
    model.page_rows = capacity
    model.topology_top = min(max(0, model.topology_top), max(0, len(body) - capacity))
    visible = body[model.topology_top: model.topology_top + capacity]
    lines = list(header) + visible
    if scrolling:
        first = model.topology_top + 1 if body else 0
        last = min(len(body), model.topology_top + capacity)
        lines.append(truncate_columns("rows {}-{}/{} | Up/Down scroll".format(first, last, len(body)), width))
    return lines


def _tree_lines(model: PickerModel, width: int, height: int) -> List[str]:
    """The team tree: a pinned header, a scrolled body, and a detail line for the row under the cursor."""
    nodes = picker_tree(model)
    if nodes:
        model.cursor = min(max(0, model.cursor), len(nodes) - 1)
    teams = len(model.rosters)
    agents = len([n for n in nodes if n.kind in ("member", "agent")])
    scope = ""
    if model.scope_workspace:
        scope = "  unassigned: {}".format(model.scope_workspace)
    picked = len(selected_rows(model))
    keys = "Enter acts | Space picks | s restore team | g go to pane | b board | c connect | v map | f folder | x dissolve | w scope | a all | r refresh | Esc quit"
    head = "{} team{} · {} agent{}{}".format(teams, "" if teams == 1 else "s", agents, "" if agents == 1 else "s", scope)
    if picked:
        head += " · {} selected".format(picked)
    lines = _picker_heading(head, keys, width)
    # Always leave room for the detail line and for the error or status the caller appends.
    body = max(1, height - len(lines) - 2)
    if len(nodes) > body:
        body = max(1, body - 1)  # the "more" footer
    model.page_rows = body
    model.top = scroll_window(model.top, model.cursor, len(nodes), body)
    for index in range(model.top, min(len(nodes), model.top + body)):
        node = nodes[index]
        pointer = "> " if index == model.cursor else "  "
        if node.kind == "team":
            lines.append(truncate_columns(pointer + _team_header(model, node, width - 2), width))
        elif node.kind == "member":
            lines.append(truncate_columns(pointer + "  " + roster_line(node.member or {}, max(20, width - 4), model.ascii_only, None, None, show_role=True), width))
        elif node.kind == "section":
            free = len([r for r in visible_rows(model) if not r.claimed_by])
            lines.append(truncate_columns("{}{} ({})".format(pointer, node.label, free), width))
        elif node.kind == "tab":
            lines.append(truncate_columns(pointer + "  " + _tab_header(model, node), width))
        else:
            row = node.row
            mark = "[x]" if row is not None and row.selected else "[ ]"
            note = ""
            if row is not None and row.launch_pending:
                note = "  (launching)"
            elif row is not None and row.agent_status == "blocked":
                note = "  (blocked)"
            lines.append(truncate_columns("{}{} {:<8} {:<10} {:<20} {}{}".format(
                pointer, mark, row.pane_id if row else "?", (row.kind if row else None) or "?",
                (row.name if row else None) or "(unnamed)", row.agent_status if row else "unknown", note), width))
    if not nodes:
        lines.append("  no agents in scope")
    if len(nodes) > body:
        above, below = model.top, max(0, len(nodes) - model.top - body)
        arrows = "^{} v{}".format(above, below) if model.ascii_only else "↑{} ↓{}".format(above, below)
        lines.append(truncate_columns("  {}  (PgUp/PgDn)".format(arrows), width))
    lines.append(truncate_columns(_tree_detail(model, nodes), width))
    return lines


def _tree_detail(model: PickerModel, nodes: List[PickerNode]) -> str:
    """The line under the list: what Enter would do to the row under the cursor."""
    node = nodes[model.cursor] if nodes else None
    if node is None:
        return "no agents; start one in a pane, then press r"
    if node.kind == "team":
        if selected_rows(model):
            return "Enter adds the selected agents to {}".format(node.team)
        info = model.folders.get(node.team)
        if info is not None and not info.get("project_dir"):
            missing = info.get("missing_missions") or []
            mission = " · Mission missing: {}".format(", ".join(str(name) for name in missing)) if missing else ""
            return "f creates one for {} · s restores agents · x dissolves it · no shared rules or member documents{}".format(node.team, mission)
        if info is not None:
            return "s restores agents · Enter folds {} · f changes its folder · x dissolves it · folder: {}".format(node.team, folder_summary(info))
        return "Enter folds {} · s restores missing agents · x dissolves it".format(node.team)
    if node.kind == "member":
        goal = str((node.member or {}).get("brief") or "")
        location = " · g goes to pane {}".format(node.row.pane_id) if node.row is not None else ""
        return "Enter opens actions for {}{} · goal: {}".format(node.label, location, headline(goal, 40) if goal else "(none yet)")
    if node.kind == "section":
        return "agents that belong to no team; Space picks them, Enter continues"
    if node.kind == "tab":
        if selected_rows(model):
            return "Enter continues with the agents you picked · Space folds tab {}".format(node.label)
        action = "expands" if node.tab_id in model.collapsed_tabs else "folds"
        return "Enter {} tab {} · Left/Right folds or expands it".format(action, node.label)
    row = node.row
    if row is not None:
        tab = row.tab_label or row.tab_id or "unknown"
        return "pane {} in tab {} · g goes there · Space picks it, Enter continues".format(row.pane_id, tab)
    return "Space picks it, Enter continues with the agents you picked"


def picker_lines(model: PickerModel, width: int = 70, height: int = 24) -> List[str]:
    """Screen lines for the picker popup at its current stage: prompt, then the input line, then any error or status."""
    lines: List[str] = []
    has_input = False
    if model.stage == "select":
        lines.extend(_tree_lines(model, width, height))
    elif model.stage == "topology":
        lines.extend(_topology_lines(model, width, height))
    elif model.stage == "restore_results":
        header = _picker_heading("Restore {}".format(model.restore_team or "team"), "g go to tab | Esc tree | Up/Down scroll | PgUp/PgDn page", width)
        body = [line for result in model.restore_results for line in _wrap_picker_text(result, width)]
        capacity = max(1, height - len(header) - 1 - int(bool(model.error or model.status)))
        model.page_rows = capacity
        model.restore_top = min(max(0, model.restore_top), max(0, len(body) - capacity))
        lines.extend(header)
        lines.extend(body[model.restore_top:model.restore_top + capacity])
        lines.append(truncate_columns("rows {}-{}/{}".format(model.restore_top + 1, min(len(body), model.restore_top + capacity), len(body)), width))
    elif model.stage == "actions":
        member = action_member(model)
        if member is None:
            lines.append("{} is not in {} any more (Esc back, r refreshes)".format(model.action_member, model.action_team))
        else:
            groups: List[List[str]] = []
            for i, (_key, label) in enumerate(action_options(model)):
                pointer = ">" if i == model.action_index else " "
                lead = "{} {}  ".format(pointer, i + 1)
                groups.append(_wrap_picker_text(lead + label, width, " " * display_width(lead)))
            identity = " · ".join(str(p) for p in (
                member.get("name"), member.get("role"), member.get("kind"), member.get("pane_id") or "-", member.get("agent_status") or "unknown") if p)
            header = _wrap_picker_text(identity if height >= 10 else "Actions: {}".format(member.get("name") or model.action_member), width, "  ")
            if height >= 12:
                goal = str(member.get("brief") or "")
                header.extend(_wrap_picker_text("goal: {}".format(headline(goal, max(20, width - 8))) if goal else "goal: (none yet)", width, "      "))
                header.append("")
            footer = _wrap_picker_text("type 1-9 | Up/Down move | Enter acts | Esc back", width, "  ")
            reserved = int(bool(model.error or model.status))
            selected = min(model.action_index, len(groups) - 1)
            if len(header) + len(footer) + len(groups[selected]) + reserved > height:
                header = _wrap_picker_text("Actions: {}".format(member.get("name") or model.action_member), width, "  ")
                footer = _wrap_picker_text("1-9, arrows, Enter, Esc", width, "  ")
            option_height = max(1, height - len(header) - len(footer) - reserved)
            lines.extend(header)
            lines.extend(_option_window(groups, selected, option_height))
            lines.extend(footer)
    elif model.stage == "rename":
        lines.append("Rename {} (lowercase letters, digits, - and _, up to {} characters)".format(model.action_member, MAX_MEMBER_NAME_CHARS))
        lines.append("Enter renames the member and its Herdr agent; the old name still resolves for 10 min")
        lines.append("name:")
        lines.append(INPUT_PROMPT + model.input)
        has_input = True
    elif model.stage == "goal":
        lines.append("Goal for {} (its brief; up to {} characters, Ctrl-U clears, Esc goes back)".format(model.action_member, MAX_BRIEF_TOTAL_CHARS))
        lines.append("Enter saves it to the roster; the agent only sees it when you send it (action 3)")
        lines.append("goal:")
        lines.append(INPUT_PROMPT + model.input)
        has_input = True
    elif model.stage == "model":
        lines.append("Model and effort for {}: <model>[@<effort>], e.g. opus@medium, gpt-5.6-luna@high, @xhigh (Esc goes back)".format(model.action_member))
        lines.append("Enter records it and announces it; Claude and OpenCode effort can switch live; other changes apply at resume")
        lines.append("setting:")
        lines.append(INPUT_PROMPT + model.input)
        has_input = True
    elif model.stage == "link_pick":
        team = model.link_team
        mine = model.managers.get(team)
        rows = link_options(model)
        if not rows:
            header = _wrap_picker_text("Team links: {} | manager: {} | Esc back".format(team, mine or "none yet"), width, "  ")
            lines.extend(header)
            lines.append("  no other team in this session")
        else:
            selected = min(model.link_index, len(rows) - 1)
            groups = []
            for i, row in enumerate(rows):
                lead = ("> " if i == selected else "  ")
                groups.append(_wrap_picker_text(lead + row["label"], width, "  "))
            header = _wrap_picker_text("Connect {} to another team (its manager: {}). Enter links or breaks; Esc goes back".format(team, mine or "none yet"), width, "  ")
            header.extend(_wrap_picker_text("Messages cross a link between the two managers; the sending team sees a mirror, the receiving manager is nudged", width, "  "))
            footer = _wrap_picker_text("j/k or Up/Down move | Enter acts | Esc back", width, "  ")
            reserved = int(bool(model.error or model.status))
            if len(header) + len(footer) + len(groups[selected]) + reserved > height:
                header = _wrap_picker_text("Team links: {} | manager: {}".format(team, mine or "none yet"), width, "  ")
                footer = _wrap_picker_text("arrows, Enter, Esc", width, "  ")
            option_height = max(1, height - len(header) - len(footer) - reserved)
            lines.extend(header)
            lines.extend(_option_window(groups, selected, option_height))
            lines.extend(footer)
    elif model.stage == "target":
        count = len(selected_rows(model))
        arrows = "up/down" if model.ascii_only else "↑/↓"
        header = _wrap_picker_text("{} agent{} selected. What now? (type a number, or {} and Enter; Esc back)".format(count, "" if count == 1 else "s", arrows), width, "  ")
        groups = []
        for i, (action, team) in enumerate(target_options(model)):
            pointer = ">" if i == model.target_index else " "
            if action == "add":
                size = model.existing_team_sizes.get(team)
                members = "  ({} member{})".format(size, "" if size == 1 else "s") if size is not None else ""
                label = "add {} to team {}{}".format("it" if count == 1 else "them", team, members)
            else:
                label = "create a new team"
            lead = "{} {}  ".format(pointer, i + 1)
            groups.append(_wrap_picker_text(lead + label, width, " " * display_width(lead)))
        reserved = int(bool(model.error or model.status))
        lines.extend(header)
        lines.extend(_option_window(groups, model.target_index, max(1, height - len(header) - reserved)))
    elif model.stage == "name":
        lines.append("New team name ([a-z][a-z0-9_-]{{0,{}}}; normalized on Enter, Esc back)".format(MAX_TEAM_CHARS - 1))
        lines.append(INPUT_PROMPT + model.input)
        has_input = True
    elif model.stage == "charter":
        lines.append("Charter for {} (Enter adds a line, empty line or Alt+Enter finishes, Tab skips, Ctrl-O loads a file)".format(model.team_name))
        for line in model.charter_lines:
            lines.append("  " + line)
        lines.append(INPUT_PROMPT + model.input)
        has_input = True
    elif model.stage == "rules":
        lines.append("Team rules for {} (Enter adds a line, empty line or Alt+Enter finishes, Tab skips, Ctrl-O loads a file)".format(model.team_name))
        for line in model.rules_lines:
            lines.append("  " + line)
        lines.append(INPUT_PROMPT + model.input)
        has_input = True
    elif model.stage == "team_folder":
        lines.append("Team folder for {} (Enter sets it, Esc cancels)".format(model.folder_team))
        lines.append("  rules, one instructions file per member, and artifacts/ go in")
        lines.append("  <dir>/.herdr-synapse/{}/".format(model.folder_team))
        lines.append(INPUT_PROMPT + model.input)
        has_input = True
    elif model.stage == "project":
        lines.append("Team folder for {} (Enter accepts, Tab skips, Esc back)".format(model.team_name))
        lines.append("  the team's rules, one instructions file per member, and artifacts/ go in")
        lines.append("  <dir>/.herdr-synapse/{}/ ; leave empty for no folder".format(model.team_name))
        lines.append(INPUT_PROMPT + model.input)
        has_input = True
    elif model.stage == "members":
        rows = selected_rows(model)
        row = rows[min(model.member_index, len(rows) - 1)]
        joining = " (adding to team {})".format(model.team_name) if model.mode == "add" else ""
        lines.append("Member {}/{}: {} {} {}{}".format(model.member_index + 1, len(rows), row.pane_id, row.kind or "?", row.name or "(unnamed)", joining))
        lines.append("Enter accepts the value shown, Ctrl-U clears it, Esc goes back")
        prompt = {"role": "role", "name": "name", "brief": "Mission / brief for {} (required)".format(row.member_name or "this member"),
                  "model": "model@effort for {} (optional, e.g. opus@medium or @high; Enter keeps the harness default)".format(row.member_name or "this member")}[model.member_field]
        lines.append(prompt + ":")
        lines.append(INPUT_PROMPT + model.input)
        has_input = True
    elif model.stage == "confirm":
        if model.mode == "add":
            count = len(selected_rows(model))
            lines.append("Add {} agent{} to team {}? (Enter adds, Esc back)".format(count, "" if count == 1 else "s", model.team_name))
            lines.append("charter: kept as it is; each new member is briefed once idle")
        else:
            lines.append("Create team {}? (Enter creates, Esc back)".format(model.team_name))
            lines.append("charter: {}".format(headline(model.charter, 60) if model.charter else "(none, set later with charter set)"))
            lines.append("rules: {}".format(headline(model.rules, 60) if model.rules else "(none, set later with knowledge set)"))
        for row in selected_rows(model):
            lines.append("  {:<8} {:<10} {:<14} {}{}{}".format(row.pane_id, row.kind or "?", row.role, row.member_name,
                                                              "  brief: " + headline(row.brief, 30) if row.brief else "", "  model: " + row.setting if row.setting else ""))
        if model.trusted_kinds is not None:
            untrusted = sorted({str(row.kind) for row in selected_rows(model) if row.kind and row.kind not in model.trusted_kinds})
            for kind in untrusted:
                lines.append("note: {} is not trusted for delivery yet; nothing is typed into it until you run: herdr-synapse kinds trust {}".format(kind, kind))
    if model.error:
        lines.append("error: {}".format(model.error))
    elif model.status:
        lines.append(model.status)
    if has_input and len(lines) > height:
        lines = lines[-height:]  # the input line and its message must stay on screen
    return [truncate_columns(line, width) for line in lines[:height]]


# --------------------------------------------------------------------------
# compose


@dataclass
class ComposeModel:
    default_to: Optional[str] = None
    team: Optional[str] = None
    input: str = ""
    cursor: int = 0
    status: Optional[str] = None
    paste_mode: bool = False
    roster_names: List[str] = field(default_factory=list)
    #: Full roster rows when available (richer ``@`` menu); ``roster_names`` is the fallback.
    roster_members: List[Dict[str, Any]] = field(default_factory=list)
    mention_index: int = 0
    mention_hidden_for: Optional[str] = None
    #: The popup cannot be verified as the human (no pane id on 0.8.2), so it refuses ``!`` lines and offers no names for them.
    bang_menu: bool = False
    #: The popup parses only the post directives, so the ``/`` menu offers those.
    slash_commands: Tuple[str, ...] = POST_DIRECTIVES
    #: Project roots the ``@@`` finder searches (the members' cwds), like ``ConsoleModel.file_roots``.
    file_roots: List[str] = field(default_factory=list)

    @property
    def members(self) -> List[Dict[str, Any]]:
        if self.roster_members:
            return self.roster_members
        return [{"name": n, "kind": "?", "role": "", "agent_status": ""} for n in self.roster_names]


def compose_default_recipient(context: Dict[str, Any], roster_members: List[Dict[str, Any]]) -> Optional[str]:
    """The roster member whose pane is ``focused_pane_id``; ``focused_pane_agent`` is only a kind label."""
    pane_id = (context or {}).get("focused_pane_id")
    if not pane_id:
        return None
    for member in roster_members:
        if member.get("pane_id") == pane_id and member.get("kind") != "human" and member.get("status", "active") != "left":
            name = member.get("name")
            return str(name) if name else None
    return None


def compose_apply_key(model: ComposeModel, key: str) -> Optional[Intent]:
    if key == "RESIZE":
        return None
    if mention_key(model, key):
        return None
    if key in ("ESC", "CTRL_C"):
        return Intent("quit")
    if key == "ENTER" and not model.paste_mode:
        if model.input.strip().startswith("!"):
            err = "direct typing is console-only (prefix+u opens the console; !name text works there)"
            model.status = "error: {}".format(err)
            return Intent("error", {"message": err})
        spec, err = parse_post_directives(model.input, model.default_to)
        if err:
            model.status = "error: {}".format(err)
            return Intent("error", {"message": err})
        assert spec is not None
        args = spec.to_args()
        if not args["to"]:
            args["to"] = ["all"]
        args["spill"] = len(spec.text) > MAX_TEXT_CHARS
        args["team"] = model.team
        return Intent("post", args)
    if edit_key(model, key):
        model.mention_index = 0
    return None


def compose_lines(model: ComposeModel, width: int = 80) -> List[str]:
    to = model.default_to or "all"
    lines = ["Post to team {} (default @{}; type @ for names; @role:r, /all, /kind k, /reply N, /urgent; Enter posts, Esc cancels)".format(model.team or "?", to)]
    lines.append(INPUT_PROMPT + model.input)
    lines.extend(mention_lines(model, width))
    if model.status:
        lines.append(model.status)
    return [truncate_columns(line, width) for line in lines]
