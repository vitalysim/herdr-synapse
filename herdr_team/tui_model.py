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

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from herdr_team import roster, sanitize
from herdr_team.errors import HerdrTeamError
from herdr_team.paths import ROLE_NAME_RE, TEAM_NAME_RE

FILTERS = ("all", "to me", "requests", "human", "system")
SLASH_COMMANDS = (
    "/all", "/human", "/kind", "/reply", "/urgent", "/ref", "/retract", "/mute", "/unmute", "/pause",
    "/nudge", "/focus", "/peek", "/who", "/filter", "/as", "/use", "/charter", "/remove", "/help", "/quit",
)
#: Directives that turn a line into a post rather than a command.
POST_DIRECTIVES = ("/all", "/human", "/kind", "/reply", "/urgent", "/ref")
POST_KINDS = ("note", "request", "handoff", "done", "blocked", "question", "answer")
REQUEST_KINDS = ("request", "question", "blocked", "handoff")

MAX_TEXT_CHARS = 2000
MAX_CHARTER_CHARS = 2000
MAX_BRIEF_CHARS = 300
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
KIND_GLYPHS = {"request": "→", "done": "✓", "blocked": "!", "question": "?"}
ASCII_KIND_GLYPHS = {"request": ">", "done": "+", "blocked": "!", "question": "?"}

MEMBER_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}\Z")
RESERVED_NAMES = frozenset({"human", "all", "me", "none", "system", "team"})
RECIPIENT_RE = re.compile(r"^(?:[a-z][a-z0-9_-]{0,31}|role:[a-z][a-z0-9_-]{0,13}|all|human)\Z")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_DIRECTIVE_RE = re.compile(
    r"^\s*(?:(?P<at>@\S+)|(?P<all>/all\b)|(?P<human>/human\b)|(?P<urgent>/urgent\b)"
    r"|/kind\s+(?P<kind>\S+)|/reply\s+(?P<reply>\S+)|/ref\s+(?P<ref>\S+))"
)
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


@dataclass
class ConsoleModel:
    team: str
    header: ConsoleHeader
    roster_lines: List[str] = field(default_factory=list)
    feed: List[Dict[str, Any]] = field(default_factory=list)
    filter_index: int = 0
    input: str = ""
    cursor: int = 0
    scroll: int = 0
    status: Optional[str] = None
    focused: bool = True
    default_recipient: Optional[str] = None
    # -- extra state owned by this module (defaults keep the scaffold shape)
    width: int = 80
    height: int = 24
    ascii_only: bool = False
    paste_mode: bool = False
    pending_confirm: Optional["Intent"] = None
    peek: Optional[List[str]] = None
    human_label: Optional[str] = None
    members: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class Intent:
    kind: str  # post | retract | mute | unmute | nudge | focus | peek | who | filter | as | use | charter | remove | help | error | quit | none | create | load_file
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PostSpec:
    """A parsed post line shared by the console and the compose popup."""

    text: str
    to: List[str] = field(default_factory=list)
    kind: str = "note"
    reply_to: Optional[int] = None
    urgent: bool = False
    refs: List[str] = field(default_factory=list)

    def to_args(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "to": list(self.to),
            "kind": self.kind,
            "reply_to": self.reply_to,
            "urgent": self.urgent,
            "refs": list(self.refs),
        }


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


def status_glyph(status: Optional[str], ascii_only: bool = False) -> str:
    table = ASCII_STATUS_GLYPHS if ascii_only else STATUS_GLYPHS
    return table.get(status or "unknown", table["unknown"])


def kind_glyph(kind: Optional[str], ascii_only: bool = False) -> str:
    table = ASCII_KIND_GLYPHS if ascii_only else KIND_GLYPHS
    return table.get(kind or "", "")


def roster_line(
    member: Dict[str, Any],
    width: int = 80,
    ascii_only: bool = False,
    mutes: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> str:
    """One ``who`` line (plan 11): glyph name kind pane status "headline" ↪N muted gone <age> ..."""
    level = degrade_level(width)
    name = str(member.get("name") or "?")
    kind = str(member.get("kind") or "?")
    pane = str(member.get("pane_id") or "-")
    status = str(member.get("agent_status") or "unknown")
    roster_status = member.get("status") or "active"
    fields = [status_glyph(status, ascii_only) + " " + name]
    if level < 2:
        fields.append(kind)
        fields.append(pane)
    fields.append(status)
    head = member.get("last_headline")
    if level == 0 and head:
        fields.append('"{}"'.format(headline(str(head), HEADLINE_COLUMNS)))
    pending = member.get("pending_nudges") or 0
    if pending:
        fields.append("{}{}".format("^" if ascii_only else "↪", pending))
    if member_muted(member, mutes, now):
        fields.append("muted")
    if roster_status in ("missing", "left", "unbound", "kind_changed", "name_conflict", "failed", "starting"):
        age = age_label(member.get("last_seen_at"), now)
        if roster_status in ("missing", "left") and age:
            fields.append("gone {}".format(age))
        else:
            fields.append(roster_status.replace("_", " "))
    if member.get("briefed") is False:
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


def derive_receipts(
    records: Iterable[Dict[str, Any]],
    cursors: Dict[str, Dict[str, Any]],
    members: Iterable[Dict[str, Any]],
) -> Dict[int, Dict[str, Any]]:
    """``{seq: {"nudged": [ts...], "read": [names], "read_by": "k/n"|None}}``.

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
    for rec in recs:
        if rec.get("from") == "system" and rec.get("kind") == "system" and rec.get("event") == "nudged":
            for s in nudged_seqs(rec):
                nudged.setdefault(s, []).append(str(rec.get("ts") or ""))
    receipts: Dict[int, Dict[str, Any]] = {}
    for rec in recs:
        if rec.get("kind") in ("system", "retract"):
            continue
        seq = rec.get("seq")
        if not isinstance(seq, int):
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
        entry: Dict[str, Any] = {"nudged": list(nudged.get(seq, [])), "read": read, "read_by": None}
        if "all" in to:
            entry["read_by"] = "{}/{}".format(len([n for n in read if n != "human"]), len(audience))
        receipts[seq] = entry
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
) -> Dict[str, Any]:
    """One rendered feed line plus the facts the filters need."""
    seq = record.get("seq")
    kind = str(record.get("kind") or "note")
    author = str(record.get("from") or "?")
    text = headline(str(record.get("text") or ""), 10_000)
    struck_by = (retractions or {}).get(seq) if isinstance(seq, int) else None
    level = degrade_level(width)
    if kind == "system":
        line = "#{} {} system {}: {}".format(seq, clock_label(record.get("ts")), record.get("event") or "event", text)
    else:
        glyph = kind_glyph(kind, ascii_only)
        parts = ["#{}".format(seq)]
        if level == 0:
            parts.append(clock_label(record.get("ts")))
        parts.append("{}{}{}".format(_author_label(record), "->" if ascii_only else "→", _to_label(record)))
        if kind != "note":
            parts.append("{}{}".format(glyph, kind) if glyph else kind)
        if record.get("reply_to") is not None:
            parts.append("re#{}".format(record.get("reply_to")))
        if record.get("urgent"):
            parts.append("URGENT")
        body = text
        if struck_by is not None:
            body = "~~{}~~ (retracted by #{})".format(text, struck_by)
        parts.append(body)
        line = " ".join(parts)
        rec_receipts = (receipts or {}).get(seq) if isinstance(seq, int) else None
        if rec_receipts and struck_by is None and level == 0:
            tags: List[str] = []
            if rec_receipts.get("nudged"):
                tags.append("{}nudged".format("+" if ascii_only else "✓"))
            if rec_receipts.get("read_by") is not None:
                tags.append("read by {}".format(rec_receipts["read_by"]))
            elif rec_receipts.get("read"):
                tags.append("{}read".format("+" if ascii_only else "✓"))
            if tags:
                line = "{}  {}".format(line, " ".join(tags))
    return {
        "seq": seq,
        "line": truncate_columns(line, width),
        "struck": struck_by is not None,
        "kind": kind,
        "from": author,
        "to": [str(t) for t in (record.get("to") or [])],
        "record": record,
    }


def build_feed(
    records: Iterable[Dict[str, Any]],
    cursors: Optional[Dict[str, Dict[str, Any]]] = None,
    members: Optional[Iterable[Dict[str, Any]]] = None,
    ascii_only: bool = False,
    width: int = 80,
) -> List[Dict[str, Any]]:
    recs = sorted((r for r in records if isinstance(r.get("seq"), int)), key=lambda r: r["seq"])
    receipts = derive_receipts(recs, cursors or {}, list(members or []))
    retractions = retraction_map(recs)
    return [feed_entry(r, receipts, retractions, ascii_only, width) for r in recs if r.get("kind") != "retract"]


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
        feed=build_feed(recs, cursors, members, ascii_only, width),
        width=width,
        height=height,
        ascii_only=ascii_only,
        human_label=label,
        members=members,
        default_recipient=(previous.default_recipient if previous else None),
    )
    if previous is not None:
        model.filter_index = previous.filter_index
        model.input = previous.input
        model.cursor = previous.cursor
        model.scroll = previous.scroll
        model.status = previous.status
        model.focused = previous.focused
        model.paste_mode = previous.paste_mode
        model.pending_confirm = previous.pending_confirm
        model.peek = previous.peek
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
        return entry.get("kind") == "system"
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
    scroll = min(max(0, model.scroll), max_scroll)
    model.scroll = scroll
    end = len(entries) - scroll
    start = max(0, end - height)
    return entries[start:end]


def filter_line(model: ConsoleModel) -> str:
    parts = []
    for i, name in enumerate(FILTERS):
        parts.append("[{}]".format(name) if i == model.filter_index % len(FILTERS) else name)
    return "filter: " + "  ".join(parts) + "  (Tab cycles)"


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


# -- command parsing -----------------------------------------------------------


def parse_post_directives(line: str, default_to: Optional[str] = None) -> Tuple[Optional[PostSpec], Optional[str]]:
    """Leading ``@name`` ``@role:r`` ``/all`` ``/human`` ``/kind k`` ``/reply N`` ``/urgent`` ``/ref p`` then text."""
    spec = PostSpec(text="")
    rest = line
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
        elif m.group("urgent"):
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
    if head == "/who":
        return Intent("who", {"team": default_team})
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
    "@name text | @role:r text | /all text | /human text | /kind k | /reply N | /urgent | /ref path | "
    "/retract N | /mute [name] [10m] | /unmute [name] | /pause | /nudge name [--force] | /focus name | "
    "/peek name | /who | /filter [name] | /as label | /use team | /charter [set [--urgent] text] | /remove name | /quit"
)


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
    if key == "TAB" and not model.paste_mode:
        model.filter_index = (model.filter_index + 1) % len(FILTERS)
        model.scroll = 0
        return Intent("filter", {"name": FILTERS[model.filter_index]})
    if key == "UP":
        model.scroll += 1
        return None
    if key == "DOWN":
        model.scroll = max(0, model.scroll - 1)
        return None
    if key == "PGUP":
        model.scroll += 10
        return None
    if key == "PGDN":
        model.scroll = max(0, model.scroll - 10)
        return None
    if key == "ESC":
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
        return None
    return None


def _after_parse(model: ConsoleModel, intent: Intent) -> Intent:
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
    if intent.kind in ("retract", "remove"):
        what = "retract #{}".format(intent.args.get("seq")) if intent.kind == "retract" else "remove {}".format(intent.args.get("member"))
        model.pending_confirm = intent
        model.status = "{}? y/n".format(what)
        return Intent("none")
    if intent.kind == "filter":
        name = intent.args.get("name")
        if name is None:
            model.filter_index = (model.filter_index + 1) % len(FILTERS)
        else:
            model.filter_index = FILTERS.index(name)
        model.scroll = 0
        intent.args["name"] = FILTERS[model.filter_index]
        return intent
    if intent.kind == "as":
        model.human_label = intent.args.get("label")
        model.status = "posting as human ({})".format(model.human_label)
        return intent
    if intent.kind == "help":
        model.status = HELP_TEXT
        return intent
    if intent.kind == "error":
        model.status = "error: {}".format(intent.args.get("message"))
        return intent
    return intent


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


def render_console(model: ConsoleModel, width: Optional[int] = None, height: Optional[int] = None) -> List[str]:
    """Every screen line, top to bottom, exactly ``height`` entries, each at most ``width`` columns."""
    w = width if width is not None else model.width
    h = height if height is not None else model.height
    if h <= 0:
        return []
    footer: List[str] = [filter_line(model)]
    if model.status:
        footer.append("• " + model.status if not model.ascii_only else "* " + model.status)
    footer.extend(input_lines(model, w))
    if len(footer) > h:
        footer = footer[-h:]  # the input line always wins
    budget = h - len(footer)
    lines: List[str] = header_lines(model.header, w)[:budget]
    budget -= len(lines)
    roster_cap = min(len(model.roster_lines), max(3, h // 4), max(0, budget - 1))
    lines.extend(model.roster_lines[:roster_cap])
    budget -= roster_cap
    if len(model.roster_lines) > roster_cap and budget > 1:
        lines.append(truncate_columns("  … {} more (/who)".format(len(model.roster_lines) - roster_cap), w))
        budget -= 1
    if budget > 0:
        lines.append("─" * w if not model.ascii_only else "-" * w)
        budget -= 1
    feed_height = max(0, budget)
    if model.peek is not None:
        body = model.peek[:feed_height]
    else:
        body = [e["line"] for e in visible_feed(model, feed_height)]
    body = body + [""] * (feed_height - len(body))
    lines.extend(body)
    lines.extend(footer)
    out = [truncate_columns(line, w) for line in lines]
    if len(out) > h:
        out = out[:h]
    return out


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
    kind: Optional[str]
    name: Optional[str]
    agent_status: str
    launch_pending: bool
    selected: bool = False
    role: str = ""
    member_name: str = ""
    brief: str = ""
    claimed_by: str = ""  # team that already owns this agent, if any
    terminal_id: str = ""


@dataclass
class PickerModel:
    rows: List[PickerRow]
    cursor: int = 0
    scope_workspace: Optional[str] = None
    stage: str = "select"  # select | name | charter | members | confirm
    team_name: str = ""
    charter: str = ""
    error: Optional[str] = None
    # -- extra state owned by this module
    focused_workspace: Optional[str] = None
    live_names: Set[str] = field(default_factory=set)
    existing_teams: List[str] = field(default_factory=list)
    input: str = ""
    cursor_pos: int = 0
    charter_lines: List[str] = field(default_factory=list)
    member_index: int = 0
    member_field: str = "role"  # role | name | brief
    status: Optional[str] = None
    paste_mode: bool = False

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


def picker_rows_from_agent_list(agents: List[Dict[str, Any]], focused_workspace: Optional[str]) -> List[PickerRow]:
    """Sorted by workspace then pane; ``launch_pending`` rows greyed (not selectable)."""
    rows: List[PickerRow] = []
    for agent in agents:
        if not agent.get("agent") and not agent.get("launch_pending"):
            continue  # a pane without a detected agent is never listed
        pane_id = str(agent.get("pane_id") or "")
        rows.append(
            PickerRow(
                pane_id=pane_id,
                workspace_id=str(agent.get("workspace_id") or pane_id.split(":")[0]),
                kind=agent.get("agent"),
                name=agent.get("name"),
                agent_status=str(agent.get("agent_status") or "unknown"),
                launch_pending=bool(agent.get("launch_pending")),
                terminal_id=str(agent.get("terminal_id") or ""),
            )
        )
    rows.sort(key=lambda r: (_id_sort_key(r.workspace_id), _pane_sort_key(r.pane_id)))
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
        return "team name must match [a-z][a-z0-9_-]{0,14}"
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
    for candidate in (kind + DEFAULT_ROLE_SUFFIX, kind[: 14 - len(DEFAULT_ROLE_SUFFIX)].rstrip("-") + DEFAULT_ROLE_SUFFIX, "dev"):
        if ROLE_NAME_RE.match(candidate) and validate_role_local(candidate) is None:
            return candidate
    return "dev"


def default_member_name(model: PickerModel, row: PickerRow) -> Optional[str]:
    base = "{}-{}".format(model.team_name, row.role) if row.role else model.team_name
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
                "renamed": bool(row.name and row.name != row.member_name),
            }
        )
    return {"team": model.team_name, "charter": model.charter or None, "naming": "prefixed", "members": members}


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
    else:
        _set_input(model, row.brief or "")


def picker_apply_key(model: PickerModel, key: str) -> Optional[Intent]:
    """Drive the picker state machine; ``create`` on the confirm screen, ``quit`` on Esc/Ctrl-C."""
    if key == "CTRL_C":
        return Intent("quit")
    if key == "RESIZE":
        return None
    if model.stage == "select":
        return _select_key(model, key)
    if model.stage == "name":
        return _name_key(model, key)
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
            model.member_field = "brief"
            _begin_member_field(model)
            model.error = None
        return None
    return None


def _select_key(model: PickerModel, key: str) -> Optional[Intent]:
    rows = visible_rows(model)
    model.error = None
    if key in ("ESC", "q"):
        return Intent("quit")
    if key in ("UP", "k"):
        model.cursor = max(0, model.cursor - 1)
        return None
    if key in ("DOWN", "j"):
        model.cursor = min(max(0, len(rows) - 1), model.cursor + 1)
        return None
    if key == "r":
        return Intent("refresh")
    if key == "w":
        if model.scope_workspace is None and model.focused_workspace:
            model.scope_workspace = model.focused_workspace
        else:
            model.scope_workspace = None
        model.cursor = 0
        return None
    if key == "a":
        candidates = [r for r in rows if selectable(r)]
        all_selected = bool(candidates) and all(r.selected for r in candidates)
        for r in candidates:
            r.selected = not all_selected
        return None
    if key in (" ", "SPACE"):
        if not rows:
            return None
        row = rows[min(model.cursor, len(rows) - 1)]
        if row.launch_pending:
            model.error = "{} is still launching; wait for it to settle".format(row.pane_id)
        elif row.claimed_by:
            model.error = "{} is already in team {} (use the CLI with --steal)".format(row.name or row.pane_id, row.claimed_by)
        elif row.agent_status == "blocked":
            model.error = "{} is blocked at a dialog; settle it first".format(row.name or row.pane_id)
        else:
            row.selected = not row.selected
        return None
    if key == "ENTER":
        if not selected_rows(model):
            model.error = "select at least one agent (Space toggles, a selects all)"
            return None
        model.stage = "name"
        _set_input(model, model.team_name)
        return None
    return None


def _name_key(model: PickerModel, key: str) -> Optional[Intent]:
    if key == "ESC":
        model.stage = "select"
        model.error = None
        return None
    if key == "ENTER":
        name = normalize_team_name(model.input)
        err = validate_team_name_local(name)
        if err:
            model.error = err
            return None
        if name in model.existing_teams:
            model.error = "team {} already exists".format(name)
            return None
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
        if model.member_field == "brief":
            model.member_field = "name"
        elif model.member_field == "name":
            model.member_field = "role"
        elif model.member_index > 0:
            model.member_index -= 1
            model.member_field = "brief"
        else:
            model.stage = "charter"
            _set_input(model, "")
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
        else:
            if len(value) > MAX_BRIEF_CHARS:
                model.error = "brief is {} chars; max {}".format(len(value), MAX_BRIEF_CHARS)
                return None
            row.brief = value
            model.member_index += 1
            model.member_field = "role"
        model.error = None
        _begin_member_field(model)
        return None
    edit_key(_TextView(model), key)
    return None


def picker_lines(model: PickerModel, width: int = 70, height: int = 24) -> List[str]:
    """Screen lines for the picker popup at its current stage."""
    lines: List[str] = []
    if model.stage == "select":
        scope = "Space {}".format(model.scope_workspace) if model.scope_workspace else "all Spaces"
        lines.append("Team up: pick agents  ({}; w scope, a all, Space toggle, Enter next, Esc quit)".format(scope))
        rows = visible_rows(model)
        for i, row in enumerate(rows):
            mark = "[x]" if row.selected else "[ ]"
            note = ""
            if row.launch_pending:
                note = "  (launching)"
            elif row.claimed_by:
                note = "  in team {}".format(row.claimed_by)
            elif row.agent_status == "blocked":
                note = "  (blocked)"
            pointer = ">" if i == model.cursor else " "
            lines.append("{} {} {:<8} {:<10} {:<24} {}{}".format(pointer, mark, row.pane_id, row.kind or "?", row.name or "(unnamed)", row.agent_status, note))
        if not rows:
            lines.append("  no agents in scope")
    elif model.stage == "name":
        lines.append("Team name ([a-z][a-z0-9_-]{0,14}; normalized on Enter, Esc back)")
        lines.append(INPUT_PROMPT + model.input)
    elif model.stage == "charter":
        lines.append("Charter for {} (Enter adds a line, empty line or Alt+Enter finishes, Tab skips, Ctrl-O loads a file)".format(model.team_name))
        for line in model.charter_lines:
            lines.append("  " + line)
        lines.append(INPUT_PROMPT + model.input)
    elif model.stage == "members":
        rows = selected_rows(model)
        row = rows[min(model.member_index, len(rows) - 1)]
        lines.append("Member {}/{}: {} {} {}".format(model.member_index + 1, len(rows), row.pane_id, row.kind or "?", row.name or "(unnamed)"))
        prompt = {"role": "role", "name": "name (default {}-<role>)".format(model.team_name), "brief": "brief (optional, Enter to skip)"}[model.member_field]
        lines.append(prompt + ":")
        lines.append(INPUT_PROMPT + model.input)
    elif model.stage == "confirm":
        lines.append("Create team {}? (Enter creates, Esc back)".format(model.team_name))
        lines.append("charter: {}".format(headline(model.charter, 60) if model.charter else "(none, set later with charter set)"))
        for row in selected_rows(model):
            lines.append("  {:<8} {:<10} {:<14} {}{}".format(row.pane_id, row.kind or "?", row.role, row.member_name, "  brief: " + headline(row.brief, 30) if row.brief else ""))
    if model.error:
        lines.append("error: {}".format(model.error))
    elif model.status:
        lines.append(model.status)
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
    if key in ("ESC", "CTRL_C"):
        return Intent("quit")
    if key == "RESIZE":
        return None
    if key == "ENTER" and not model.paste_mode:
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
    edit_key(model, key)
    return None


def compose_lines(model: ComposeModel, width: int = 80) -> List[str]:
    to = model.default_to or "all"
    lines = ["Post to team {} (default @{}; @name, @role:r, /all, /kind k, /reply N, /urgent; Enter posts, Esc cancels)".format(model.team or "?", to)]
    lines.append(INPUT_PROMPT + model.input)
    if model.status:
        lines.append(model.status)
    return [truncate_columns(line, width) for line in lines]
