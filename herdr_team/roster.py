"""Roster model, names, reserved words, claims, rehydration, and the join routine.

Plan sections 5.1, 5.3, 5.4, 5.5 and 4.2.

``team.json`` schema 1: ``{"schema":1,"team","created_at","socket",
"state_dir","naming","revision","charter":{...},"members":[Member...]}``.
Member ``status`` in ``active|starting|missing|unbound|left|kind_changed|
name_conflict|failed``; ``delivery`` in ``nudge|hooks``.

Names: team ``[a-z][a-z0-9_-]{0,14}``, role ``[a-z][a-z0-9_-]{0,31}``,
derived member name ``<team>-<role>`` (29 chars max so ``-2`` fits under
Herdr's 32-char cap). Reserved: ``{human, all, me, none, system, team}``
plus every agent kind label and alias Herdr knows. Validate every name
locally before touching Herdr; ``create`` validates all names before
renaming or stamping anything so a failure leaves no half-named team.

Writes to ``team.json`` go through ``save_team``/``update_team``: temp file
plus rename under ``team.lock``, ``revision`` check, three retries.

Claims: an agent belongs to at most one team. The lock budget is three
files, so the cross-team claim check takes ``team.lock`` of every team in
the session in sorted name order (never a fourth lock). ``claims.lock`` at
the state root (plan 5.1) is therefore never created.

Pure functions the daemon and ``hook-event`` reuse: ``rehydrate_match``
(plan 4.2 order), ``classify_name_loss``, ``reconcile_live_name``
(adopt-versus-enforce), ``build_who_json`` (plan 5.4) and
``token_commands`` (plan 5.3, returned as data the caller executes).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from herdr_team import sanitize, store
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError
from herdr_team.paths import (
    MAX_ROLE_CHARS,
    ROLE_NAME_RE,
    Layout,
    SessionPaths,
    TeamPaths,
    ensure_dir,
    ensure_team_dirs,
    validate_team_name,
)

SCHEMA_VERSION = 1
MEMBER_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}\Z")

MAX_NAME_CHARS = 32
RESERVED_NAMES = frozenset({"human", "all", "me", "none", "system", "team"})
#: Canonical kind labels from ``src/detect/mod.rs::agent_label`` at ``3150bd92``
#: (22 in the installed 0.8.2 binary plus ``muse`` from HEAD).
#: ``sanitize.KIND_LABELS`` holds the 22 labels of the installed 0.8.2 binary
#: (verified against ``herdr agent start --help``); ``muse`` exists only in this
#: checkout past the tag (``src/detect/mod.rs`` at 3150bd92) and is refused too.
KIND_LABELS = frozenset(sanitize.KIND_LABELS | {"muse"})
#: Aliases ``src/detect/mod.rs::lookup_agent`` maps onto the labels above.
KIND_ALIASES = frozenset(sanitize.KIND_ALIASES - KIND_LABELS)
ALL_KIND_WORDS = KIND_LABELS | KIND_ALIASES
STATUSES = ("active", "starting", "missing", "unbound", "left", "kind_changed", "name_conflict", "failed")
DELIVERIES = ("nudge", "hooks")
NAME_HISTORY_TTL_S = 600.0
NAMING_MODES = ("prefixed", "plain")

TOKEN_SOURCE_ROSTER = "herdr-team:roster"
TOKEN_SOURCE_TASK = "herdr-team:task"
TASK_TOKEN_TTL_MS = 120000
TASK_TOKEN_MAX_COLUMNS = 24
TOKEN_VALUE_MAX_CHARS = 80

#: Herdr colours a sidebar cell from a fixed ``fg`` in the user's config; nothing lets the colour
#: depend on a token's value, and control characters are stripped from values, so ANSI cannot be
#: smuggled in. What a plugin CAN do is give each team its own token key: the recommended sidebar
#: row lists one cell per slot with its own colour, and a row's missing tokens (and their
#: separators) simply disappear, so exactly one coloured cell ever renders per member.
TEAM_COLOR_SLOTS = 6
#: The colour each slot carries in ``cmd_misc.SIDEBAR_SNIPPET``; here for docs and tests only.
TEAM_COLOR_HEX = ("#fb4934", "#b8bb26", "#83a598", "#d3869b", "#fabd2f", "#8ec07c")
TEAM_COLOR_NAMES = ("red", "green", "blue", "purple", "yellow", "aqua")


def color_slot_key(slot: int) -> str:
    """The metadata token key for a colour slot: ``team_c1`` .. ``team_c6``."""
    return "team_c{}".format(int(slot))


def color_slot_keys() -> List[str]:
    return [color_slot_key(slot) for slot in range(1, TEAM_COLOR_SLOTS + 1)]


def color_slot_tokens(team: Optional[str], slot: Optional[Any]) -> Dict[str, Optional[str]]:
    """Every slot key, with the team name in its own slot and ``None`` in the rest.

    Sending all of them on every stamp is what makes a colour change or a move between teams
    self-healing: a member can never keep a stale colour from its previous team.
    """
    if isinstance(slot, bool):
        number = 0  # a JSON ``true`` in config.color_slot is not slot 1
    else:
        try:
            number = int(slot)
        except (TypeError, ValueError):
            number = 0
    tokens: Dict[str, Optional[str]] = {key: None for key in color_slot_keys()}
    if team and 1 <= number <= TEAM_COLOR_SLOTS:
        tokens[color_slot_key(number)] = _clip_token_value(team)
    return tokens


def color_slot_of(doc: Optional[Dict[str, Any]]) -> Optional[int]:
    """``config.color_slot`` of a ``team.json`` document, when it holds a usable slot."""
    config = doc.get("config") if isinstance(doc, dict) and isinstance(doc.get("config"), dict) else None
    slot = config.get("color_slot") if config else None
    if isinstance(slot, bool) or not isinstance(slot, int):
        return None
    return slot if 1 <= slot <= TEAM_COLOR_SLOTS else None


def free_color_slot(taken: Iterable[Any], count: int = 0) -> int:
    """The lowest slot no other team holds; past ``TEAM_COLOR_SLOTS`` teams the colours repeat."""
    used = {s for s in taken if isinstance(s, int) and not isinstance(s, bool)}
    for slot in range(1, TEAM_COLOR_SLOTS + 1):
        if slot not in used:
            return slot
    return (max(0, int(count)) % TEAM_COLOR_SLOTS) + 1

NAME_POLICY_ADOPT = "adopt"
NAME_POLICY_ENFORCE = "enforce"

MATCH_SESSION = "session"
MATCH_TERMINAL = "terminal_id"
MATCH_LABEL = "label"
MATCH_PANE = "pane_id"
MATCH_NAME = "name"
MATCH_FINGERPRINT = "fingerprint"
#: Owner decision 16.5 is open: fingerprint matches wait for ``bind`` by default.
AUTO_BIND_FINGERPRINT = False

#: Herdr's own agent binary for Cursor (``src/agent_resume.rs``: ``cursor-agent.cmd`` on Windows).
CURSOR_AGENT_BIN = "cursor-agent.cmd" if os.name == "nt" else "cursor-agent"

#: ``agent_session.source`` -> ``(agent kind, session ref kinds it may carry, argv template)``,
#: copied entry for entry from Herdr's own restore table (``src/agent_resume.rs::plan`` and
#: ``is_official_agent_source`` at v0.8.2), so ``resume`` reopens a session exactly the way Herdr
#: would on a cold start. ``{id}`` is the session value, substituted inside a part so the joined
#: forms (``--resume=<id>``) stay one argument. Every integration Herdr ships is here; a source it
#: does not issue, or one paired with the wrong agent, is refused by ``resume`` rather than
#: guessed, because a wrong flag starts a *new* conversation wearing the member's name, which is
#: the confusion this table exists to prevent. ``pi`` and ``omp`` identify a session by an
#: absolute path rather than an id, which is why the ref kinds are per entry.
RESUME_COMMANDS: Dict[str, Tuple[str, Tuple[str, ...], Tuple[str, ...]]] = {
    "herdr:antigravity_cli": ("agy", ("id",), ("agy", "--conversation", "{id}")),
    "herdr:claude": ("claude", ("id",), ("claude", "--resume", "{id}")),
    "herdr:codex": ("codex", ("id",), ("codex", "resume", "{id}")),
    "herdr:copilot": ("copilot", ("id",), ("copilot", "--resume={id}")),
    "herdr:cursor": ("cursor", ("id",), (CURSOR_AGENT_BIN, "--resume", "{id}")),
    "herdr:devin": ("devin", ("id",), ("devin", "--resume", "{id}")),
    "herdr:droid": ("droid", ("id",), ("droid", "--resume", "{id}")),
    "herdr:grok": ("grok", ("id",), ("grok", "--resume", "{id}")),
    "herdr:hermes": ("hermes", ("id",), ("hermes", "--resume", "{id}")),
    "herdr:kilo": ("kilo", ("id",), ("kilo", "--session", "{id}")),
    "herdr:kimi": ("kimi", ("id",), ("kimi", "--session", "{id}")),
    "herdr:mastracode": ("mastracode", ("id",), ("mastracode", "--thread", "{id}")),
    "herdr:omp": ("omp", ("path", "id"), ("omp", "--resume={id}")),
    "herdr:opencode": ("opencode", ("id",), ("opencode", "--session", "{id}")),
    "herdr:pi": ("pi", ("path", "id"), ("pi", "--session", "{id}")),
    "herdr:qodercli": ("qodercli", ("id",), ("qodercli", "--resume", "{id}")),
    "herdr:qwen": ("qwen", ("id",), ("qwen", "--resume", "{id}")),
}

#: The two ``AgentSessionRefKind`` values Herdr issues.
SESSION_REF_KINDS = ("id", "path")
#: Herdr's ``MAX_SESSION_ID_LEN`` and ``MAX_SESSION_PATH_LEN``.
MAX_SESSION_ID_CHARS = 512
MAX_SESSION_PATH_CHARS = 4096

SYSTEM_EVENTS = (
    "nudged", "toast", "retracted", "expired", "abandoned", "member_gone",
    "member_restarted", "rotated", "reset_detected", "charter_updated", "renamed", "typed", "member_joined",
    "knowledge_updated", "instructions_updated", "instructions_edited", "knowledge_finding",
    "artifacts_changed", "project_set", "operator_granted", "operator_revoked",
)

_SAVE_RETRIES = 3


# --------------------------------------------------------------------------
# time helpers


def now_iso() -> str:
    """ISO-8601 UTC with milliseconds, ``2026-09-04T13:53:10.123Z``."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(now.microsecond // 1000)


def parse_iso(value: Optional[str]) -> Optional[float]:
    """Epoch seconds for an ISO timestamp we wrote, else None."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# harness sessions: the one key that survives a Herdr restart


def _control_free(value: str) -> bool:
    """No Unicode ``Cc`` character, the one thing Herdr's own session validators reject."""
    return not any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in value)


def session_value_problem(value: Any, ref_kind: str = "id") -> Optional[str]:
    """Why ``value`` is not a usable session reference, or None.

    Mirrors Herdr's ``valid_session_id`` and ``valid_session_path``
    (non-empty, within the length cap, no control characters, absolute for a
    path), and adds one rule Herdr does not need: a value may not start with
    ``-``. Herdr hands these to its own spawn; ``resume`` puts them in an
    ``execvp`` argv, where a leading dash would be read as a flag. No real
    id or path starts with one.
    """
    if not isinstance(value, str) or not value:
        return "empty"
    limit = MAX_SESSION_PATH_CHARS if ref_kind == "path" else MAX_SESSION_ID_CHARS
    if len(value) > limit:
        return "longer than {} characters".format(limit)
    if not _control_free(value):
        return "contains control characters"
    if value.startswith("-"):
        return "starts with a dash"
    if ref_kind == "path" and not os.path.isabs(value):
        return "is not an absolute path"
    return None


def session_of(row: Any, seen_at: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """``Member.session`` from a live row's ``agent_session`` (or a bare ``AgentSessionInfo``).

    Herdr's integrations report a session per pane (``pane.report_agent_session``)
    and ``agent.list``/``agent.get``/``pane.get`` carry it as ``{"source",
    "agent", "kind", "value"}``. Only ``source`` and ``value`` are load-bearing;
    anything without both is no session. An unusable value is refused rather
    than trimmed to fit: a truncated id is a *wrong* id, which would bind the
    member to nothing and resume nothing.
    """
    info = row.get("agent_session") if isinstance(row, dict) and "agent_session" in row else row
    if not isinstance(info, dict):
        return None
    source = info.get("source")
    value = info.get("value")
    if not isinstance(source, str) or not source or len(source) > 64 or not _control_free(source):
        return None
    ref_kind = info.get("kind") if info.get("kind") in SESSION_REF_KINDS else "id"
    if session_value_problem(value, ref_kind) is not None:
        return None
    out: Dict[str, Any] = {
        "source": source,
        "agent": info.get("agent") if isinstance(info.get("agent"), str) else None,
        "kind": ref_kind,
        "value": value,
        "seen_at": seen_at or now_iso(),
    }
    return out


def session_key(session: Any) -> Optional[Tuple[str, str]]:
    """``(source, value)`` when ``session`` names one, else None."""
    if not isinstance(session, dict):
        return None
    source, value = session.get("source"), session.get("value")
    if isinstance(source, str) and source and isinstance(value, str) and value:
        return source, value
    return None


def same_session(a: Any, b: Any) -> bool:
    key_a, key_b = session_key(a), session_key(b)
    return key_a is not None and key_a == key_b


def short_session(session: Any, tail: int = 8) -> Optional[str]:
    """The last ``tail`` characters of the session value, for ``who``/``me`` and the tree."""
    key = session_key(session)
    if key is None:
        return None
    value = key[1]
    return value if len(value) <= tail else "\u2026" + value[-tail:]


def resume_argv(session: Any) -> List[str]:
    """The exact command that reopens ``session`` (``RESUME_COMMANDS``); refuses what it cannot name."""
    key = session_key(session)
    if key is None:
        raise HerdrTeamError("session_unknown", "no harness session is recorded for this member", EXIT_REFUSED, {"hint": "the member's harness must report its session to Herdr (herdr integration install <kind>); see who"})
    source, value = key
    entry = RESUME_COMMANDS.get(source)
    if entry is None:
        raise HerdrTeamError("session_unsupported", "no resume command is known for {} sessions".format(source), EXIT_REFUSED, {"source": source, "supported": sorted(RESUME_COMMANDS)})
    agent, ref_kinds, template = entry
    reported = session.get("agent") if isinstance(session, dict) else None
    if isinstance(reported, str) and reported and reported != agent:
        # Herdr only ever pairs a source with its own agent (``is_official_agent_source``); a
        # mismatch is a report Herdr would not have issued, so resuming it would be a guess.
        raise HerdrTeamError("session_unsupported", "{} is not the agent Herdr pairs with {}".format(reported, source), EXIT_REFUSED, {"source": source, "agent": reported, "expected": agent})
    ref_kind = session.get("kind") if isinstance(session, dict) and session.get("kind") in SESSION_REF_KINDS else "id"
    if ref_kind not in ref_kinds:
        raise HerdrTeamError("session_unsupported", "{} sessions are not identified by a {}".format(source, ref_kind), EXIT_REFUSED, {"source": source, "kind": ref_kind, "expected": list(ref_kinds)})
    problem = session_value_problem(value, ref_kind)
    if problem is not None:
        raise HerdrTeamError("session_unknown", "the recorded session {} {}".format(ref_kind, problem), EXIT_REFUSED, {"source": source, "kind": ref_kind})
    return [part.replace("{id}", value) for part in template]


# --------------------------------------------------------------------------
# model


@dataclass
class Member:
    name: str
    role: str
    kind: str
    terminal_id: Optional[str]
    pane_id: Optional[str] = None
    workspace_id: Optional[str] = None
    tab_id: Optional[str] = None
    label: Optional[str] = None
    cwd: Optional[str] = None
    brief: Optional[str] = None
    managed: bool = False
    session: Optional[Dict[str, Any]] = None
    status: str = "active"
    generation: int = 1
    delivery: str = "nudge"
    verified_kind: bool = False
    joined_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    briefed_at: Optional[str] = None
    briefing_seq: Optional[int] = None
    charter_seq_acked: Optional[int] = None
    #: Revision of this member's instructions document, bumped on every write,
    #: and the revision it last acknowledged. Same shape as the charter's pair,
    #: so ``who`` can say ``instructions: stale`` the way it says ``charter: stale``.
    instructions_seq: int = 0
    instructions_seq_acked: Optional[int] = None
    rules_seq_acked: Optional[int] = None
    previous_names: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def is_human(self) -> bool:
        return self.kind == "human"

    def to_json(self) -> Dict[str, Any]:
        obj: Dict[str, Any] = {
            "name": self.name,
            "role": self.role,
            "kind": self.kind,
            "terminal_id": self.terminal_id,
            "pane_id": self.pane_id,
            "workspace_id": self.workspace_id,
            "tab_id": self.tab_id,
            "label": self.label,
            "cwd": self.cwd,
            "brief": self.brief,
            "managed": bool(self.managed),
            "session": self.session,
            "status": self.status,
            "generation": int(self.generation),
            "delivery": self.delivery,
            "verified_kind": bool(self.verified_kind),
            "joined_at": self.joined_at,
            "last_seen_at": self.last_seen_at,
            "briefed_at": self.briefed_at,
            "briefing_seq": self.briefing_seq,
            "charter_seq_acked": self.charter_seq_acked,
            "instructions_seq": int(self.instructions_seq or 0),
            "instructions_seq_acked": self.instructions_seq_acked,
            "rules_seq_acked": self.rules_seq_acked,
        }
        if self.previous_names:
            obj["previous_names"] = [dict(p) for p in self.previous_names]
        return obj

    @classmethod
    def from_json(cls, obj: Dict[str, Any]) -> "Member":
        if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
            raise HerdrTeamError("roster_corrupt", "member entry has no name", EXIT_REFUSED)
        status = obj.get("status") or "active"
        if status not in STATUSES:
            status = "unbound"
        delivery = obj.get("delivery") or "nudge"
        if delivery not in DELIVERIES:
            delivery = "nudge"
        previous = obj.get("previous_names") or []
        return cls(
            name=obj["name"],
            role=str(obj.get("role") or ""),
            kind=str(obj.get("kind") or "unknown"),
            terminal_id=obj.get("terminal_id"),
            pane_id=obj.get("pane_id"),
            workspace_id=obj.get("workspace_id"),
            tab_id=obj.get("tab_id"),
            label=obj.get("label"),
            cwd=obj.get("cwd"),
            brief=obj.get("brief"),
            managed=bool(obj.get("managed", False)),
            session=obj.get("session") if isinstance(obj.get("session"), dict) else None,
            status=status,
            generation=int(obj.get("generation") or 1),
            delivery=delivery,
            verified_kind=bool(obj.get("verified_kind", False)),
            joined_at=obj.get("joined_at"),
            last_seen_at=obj.get("last_seen_at"),
            briefed_at=obj.get("briefed_at"),
            briefing_seq=obj.get("briefing_seq"),
            charter_seq_acked=obj.get("charter_seq_acked"),
            instructions_seq=int(obj.get("instructions_seq") or 0),
            instructions_seq_acked=obj.get("instructions_seq_acked"),
            rules_seq_acked=obj.get("rules_seq_acked"),
            previous_names=[dict(p) for p in previous if isinstance(p, dict)],
        )

    def retired_name_active(self, name: str, now: Optional[float] = None) -> bool:
        """True when ``name`` was this member's name less than ``NAME_HISTORY_TTL_S`` ago."""
        current = time.time() if now is None else now
        for entry in self.previous_names:
            if entry.get("name") != name:
                continue
            retired = parse_iso(entry.get("retired_at"))
            if retired is None or current - retired <= NAME_HISTORY_TTL_S:
                return True
        return False


@dataclass
class Team:
    team: str
    socket: str
    state_dir: str
    created_at: str
    naming: str = "prefixed"  # or "plain"
    revision: int = 0
    charter: Optional[Dict[str, Any]] = None
    members: List[Member] = field(default_factory=list)
    schema: int = SCHEMA_VERSION
    #: ``{"name_policy": "adopt"|"enforce", "default_team": ...}``; optional.
    config: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        obj: Dict[str, Any] = {
            "schema": int(self.schema),
            "team": self.team,
            "created_at": self.created_at,
            "socket": self.socket,
            "state_dir": self.state_dir,
            "naming": self.naming,
            "revision": int(self.revision),
            "charter": dict(self.charter) if self.charter else None,
            "members": [m.to_json() for m in self.members],
        }
        if self.config:
            obj["config"] = dict(self.config)
        return obj

    @classmethod
    def from_json(cls, obj: Dict[str, Any]) -> "Team":
        if not isinstance(obj, dict) or not isinstance(obj.get("team"), str):
            raise HerdrTeamError("roster_corrupt", "team.json has no team name", EXIT_REFUSED)
        schema = int(obj.get("schema") or SCHEMA_VERSION)
        if schema > SCHEMA_VERSION:
            raise HerdrTeamError("roster_schema_unsupported", "team.json schema {} is newer than this plugin".format(schema), EXIT_REFUSED, {"schema": schema})
        naming = obj.get("naming") or "prefixed"
        if naming not in NAMING_MODES:
            naming = "prefixed"
        members_raw = obj.get("members") or []
        members = [Member.from_json(m) for m in members_raw if isinstance(m, dict)]
        charter = obj.get("charter") if isinstance(obj.get("charter"), dict) else None
        config = obj.get("config") if isinstance(obj.get("config"), dict) else {}
        return cls(
            team=obj["team"],
            socket=str(obj.get("socket") or ""),
            state_dir=str(obj.get("state_dir") or ""),
            created_at=str(obj.get("created_at") or ""),
            naming=naming,
            revision=int(obj.get("revision") or 0),
            charter=charter,
            members=members,
            schema=schema,
            config=dict(config),
        )

    # -- lookups ---------------------------------------------------------------

    def find(self, name_or_terminal: str) -> Optional[Member]:
        """By current name, by ``terminal_id``, or by a name retired under 10 minutes ago.

        A live member always wins over a ``left`` tombstone carrying the same
        name or terminal. ``add`` only refuses duplicates among non-left
        members (:meth:`Roster.add_member`), so a name may legitimately be
        reused after a remove; resolving to the tombstone made ``remove``,
        ``rename`` and ``brief --set`` act on the wrong entry and report
        success while the live member kept its name.
        """
        if not name_or_terminal:
            return None
        for tombstones in (False, True):
            for match in (
                lambda m: m.name == name_or_terminal,
                lambda m: bool(m.terminal_id) and m.terminal_id == name_or_terminal,
                lambda m: m.retired_name_active(name_or_terminal),
            ):
                for member in self.members:
                    if (member.status == "left") is tombstones and match(member):
                        return member
        return None

    def find_by_terminal(self, terminal_id: Optional[str], include_left: bool = False) -> Optional[Member]:
        if not terminal_id:
            return None
        for member in self.members:
            if member.terminal_id == terminal_id and (include_left or member.status != "left"):
                return member
        return None

    def holders(self, role: str) -> List[Member]:
        return [m for m in self.members if m.role == role and m.status != "left"]

    def agents(self) -> List[Member]:
        """Non-human members that have not left."""
        return [m for m in self.members if not m.is_human and m.status != "left"]

    def names(self) -> List[str]:
        return [m.name for m in self.members if m.status != "left"]

    @property
    def color_slot(self) -> Optional[int]:
        """``config.color_slot``: which sidebar colour this team's members are stamped with."""
        return color_slot_of({"config": self.config})

    @property
    def name_policy(self) -> str:
        policy = self.config.get("name_policy")
        return NAME_POLICY_ENFORCE if policy == NAME_POLICY_ENFORCE else NAME_POLICY_ADOPT


# --------------------------------------------------------------------------
# grammar


def _reserved_reason(name: str) -> Optional[str]:
    if name in RESERVED_NAMES:
        return "reserved word"
    if name in KIND_LABELS:
        return "agent kind label"
    if name in KIND_ALIASES:
        return "agent kind alias"
    return None


def validate_role(role: str, allow_kind_label: bool = False) -> str:
    """Grammar plus reserved-word refusal (``role_invalid``); kind labels only with ``allow_kind_label``.

    Plan 5.5 lets ``--from-workspace`` default a role to the kind label while
    plan 12 refuses a role equal to a kind label; the resolution (docs/cli.md):
    a kind label is an acceptable role under *prefixed* naming, where the
    member name stays ``<team>-<kind>`` and can never be mistaken for a kind.
    Reserved words (``human``, ``all``, ``me`` ...) are refused always.
    """
    if not isinstance(role, str) or not ROLE_NAME_RE.match(role):
        text = role if isinstance(role, str) else ""
        if len(text) > MAX_ROLE_CHARS:
            detail = "yours is {} characters".format(len(text))
        elif not text or not ("a" <= text[0] <= "z"):
            detail = "it must start with a lowercase letter"
        else:
            detail = "no spaces or other characters"
        raise HerdrTeamError("role_invalid", "role: lowercase letters, digits, - and _, up to {} characters ({})".format(MAX_ROLE_CHARS, detail), EXIT_REFUSED, {"role": role})
    reason = _reserved_reason(role)
    if reason and (not allow_kind_label or reason == "reserved word"):
        raise HerdrTeamError("role_invalid", "role {!r} is a {}".format(role, reason), EXIT_REFUSED, {"role": role, "reason": reason})
    return role


def validate_member_name(name: str) -> str:
    """Grammar, reserved words, kind labels (``name_invalid`` / ``name_reserved``)."""
    if not isinstance(name, str) or not MEMBER_NAME_RE.match(name):
        raise HerdrTeamError(
            "name_invalid",
            "name must start with a lowercase letter and contain only lowercase letters, digits, '-' or '_' (1-32 characters)",
            EXIT_REFUSED,
            {"name": name},
        )
    reason = _reserved_reason(name)
    if reason:
        raise HerdrTeamError("name_reserved", "name {!r} is a {}".format(name, reason), EXIT_REFUSED, {"name": name, "reason": reason})
    return name


#: Leading role segments dropped first when ``<team>-<role>`` must be shortened: kind labels and the default suffix.
GENERIC_ROLE_SEGMENTS = frozenset({"dev", "agent"})


#: Smallest role tail kept in a member name. Below this the name stops saying
#: what the member is *for*, which is the whole point of the suffix.
MIN_ROLE_TAIL = 4


def fit_member_name(team: str, role: str, cap: int = 32) -> str:
    """``<team>-<role>`` fitted to ``cap`` (Herdr caps agent names at 32 bytes).

    ``red-dev`` + ``opencode-dev-brainstormer`` is 33 characters; a cut
    mid-word gave ``red-dev-opencode-dev-brainstorme``. Dropping leading
    segments until it fits, then the generic ones (kind labels, ``dev``),
    gives ``red-dev-brainstormer``.

    The role always survives. Trimming the role alone was enough while team
    names were capped at 15, but a longer team could consume the whole budget
    and every member of that team collapsed to the same truncated team string
    — identical names for different members. So once the role is as short as
    it usefully goes, the *team* prefix gives way instead, dropping whole
    trailing segments before it resorts to a hard clip.
    """
    full = "{}-{}".format(team, role)
    if len(full) <= cap:
        return full
    segments = [s for s in role.split("-") if s]
    while len(segments) > 1 and len("{}-{}".format(team, "-".join(segments))) > cap:
        segments.pop(0)
    while len(segments) > 1 and (segments[0] in GENERIC_ROLE_SEGMENTS or segments[0] in KIND_LABELS):
        segments.pop(0)
    tail = "-".join(segments)
    fitted = "{}-{}".format(team, tail)
    if len(fitted) <= cap:
        return fitted

    # The team is too long to leave room. Shorten it, keeping leading segments.
    budget = cap - len(tail) - 1
    if budget < MIN_ROLE_TAIL:
        # Nothing readable fits either way: split the budget and clip both.
        head = max(MIN_ROLE_TAIL, (cap - 1) // 2)
        return "{}-{}".format(team[:head], tail)[:cap].rstrip("-_")
    team_segments = [s for s in team.split("-") if s]
    prefix = team_segments[0] if team_segments else team
    for extra in team_segments[1:]:
        candidate = "{}-{}".format(prefix, extra)
        if len(candidate) > budget:
            break
        prefix = candidate
    if len(prefix) > budget:
        prefix = prefix[:budget]
    return "{}-{}".format(prefix.rstrip("-_"), tail)[:cap].rstrip("-_")


def derive_name(team: str, role: str, naming: str = "prefixed") -> str:
    """``<team>-<role>`` (or ``<role>`` with plain naming), validated.

    A kind-label role is fine when prefixed (``t-claude``); with plain naming
    the derived name would *be* the kind label, which ``validate_member_name``
    refuses.
    """
    validate_team_name(team)
    validate_role(role, allow_kind_label=(naming != "plain"))
    if naming == "plain":
        return validate_member_name(role)
    # A long role still gets a valid, readable default (``fit_member_name``).
    return validate_member_name(fit_member_name(team, role))


def suffixed_name(name: str, ordinal: int) -> str:
    """``<name>-<ordinal>`` when it fits under the cap; ``name_too_long`` otherwise."""
    suffix = "-{}".format(int(ordinal))
    if len(name) + len(suffix) > MAX_NAME_CHARS:
        raise HerdrTeamError(
            "name_too_long",
            "cannot suffix {!r}: {} exceeds {} characters; pick a shorter role or use --names plain".format(name, name + suffix, MAX_NAME_CHARS),
            EXIT_REFUSED,
            {"name": name, "suffix": suffix},
        )
    return name + suffix


def unique_name(base: str, taken: Iterable[str]) -> str:
    """``base`` when free, else ``base-2`` when the cap allows; refuses beyond ``-2``."""
    used = set(taken)
    if base not in used:
        return base
    candidate = suffixed_name(base, 2)
    if candidate in used:
        raise HerdrTeamError("name_taken", "both {!r} and {!r} are in use; choose a name explicitly".format(base, candidate), EXIT_REFUSED, {"name": base})
    return candidate


def label_for(team: str, role: str) -> str:
    return "team:{}/{}".format(team, role)


_LABEL_RE = re.compile(r"^team:([a-z][a-z0-9_-]{0,14})/([a-z][a-z0-9_-]{0,31})\Z")


def parse_label(label: Optional[str]) -> Optional[Tuple[str, str]]:
    """``team:<team>/<role>`` -> ``(team, role)``."""
    if not label:
        return None
    match = _LABEL_RE.match(label)
    if not match:
        return None
    return match.group(1), match.group(2)


def parse_name_taken_candidates(message: str) -> List[Dict[str, str]]:
    """Tolerant parse of Herdr's ``agent_name_taken`` message; stops on the first malformed piece."""
    out: List[Dict[str, str]] = []
    marker = "candidates:"
    idx = message.find(marker)
    if idx < 0:
        return out
    for chunk in message[idx + len(marker):].split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        entry: Dict[str, str] = {}
        for token in chunk.split():
            if "=" not in token:
                break
            key, _, value = token.partition("=")
            entry[key] = value
        if not entry.get("terminal_id"):
            break
        out.append(entry)
    return out


# --------------------------------------------------------------------------
# persistence


def load_team(team_paths: TeamPaths) -> Team:
    return Team.from_json(store.RosterStore(team_paths).load())


def save_team(team_paths: TeamPaths, team: Team, expected_revision: Optional[int] = None) -> Team:
    """Temp + rename under ``team.lock`` with a revision check; ``roster_conflict`` when it moved."""
    doc = store.RosterStore(team_paths).save(team.to_json(), expected_revision)
    team.revision = int(doc.get("revision") or 0)
    return team


def update_team(team_paths: TeamPaths, mutate: Callable[[Team], Any], retries: int = _SAVE_RETRIES) -> Team:
    """Lock, load, ``mutate(team)``, save with the loaded revision; retries on conflict."""

    def mutate_doc(doc: Dict[str, Any]) -> None:
        team = Team.from_json(doc)
        mutate(team)
        fresh = team.to_json()
        doc.clear()
        doc.update(fresh)

    return Team.from_json(store.RosterStore(team_paths).update(mutate_doc, retries=retries))


def create_team(layout: Layout, name: str, naming: str = "prefixed", charter: Optional[Dict[str, Any]] = None, reuse: bool = False) -> Team:
    """Write a fresh ``team.json`` (with the human member); ``team_exists`` unless ``reuse``."""
    validate_team_name(name)
    if naming not in NAMING_MODES:
        raise HerdrTeamError("usage", "naming must be prefixed or plain", 2, {"naming": naming})
    team_paths = layout.team(name)
    ensure_team_dirs(team_paths)
    with store.team_lock(team_paths):
        existing = store.read_json(team_paths.team_json, default=None)
        if existing is not None:
            if not reuse:
                raise HerdrTeamError("team_exists", "team {!r} already exists (use --reuse)".format(name), EXIT_REFUSED, {"team": name})
            return Team.from_json(existing)
        team = Team(
            team=name,
            socket=os.fspath(layout.socket),
            state_dir=os.fspath(layout.state_root.path),
            created_at=now_iso(),
            naming=naming,
            revision=0,
            charter=charter,
            members=[Member(name="human", role="operator", kind="human", terminal_id=None, status="active", joined_at=now_iso())],
        )
        doc = store.RosterStore(team_paths)._save_locked(team.to_json(), None)
        team.revision = int(doc.get("revision") or 0)
        return team


def list_teams(layout: Layout) -> List[Dict[str, Any]]:
    """``[{"team", "members", "socket", "running", "team_dir", "default"}]`` for the ``teams`` command."""
    session = layout.session
    default_team = None
    console = store.read_json(session.console_json, default=None)
    if isinstance(console, dict):
        default_team = console.get("default_team")
    out: List[Dict[str, Any]] = []
    for name in session.list_teams():
        team_paths = session.team(name)
        try:
            team = load_team(team_paths)
        except HerdrTeamError:
            continue
        sock = team.socket
        running = bool(sock) and os.path.exists(sock)
        out.append({
            "team": name,
            "members": len([m for m in team.members if m.status != "left"]),
            "socket": sock,
            "running": running,
            "default": default_team == name if default_team else len(session.list_teams()) == 1,
            "team_dir": os.fspath(team_paths.root),
        })
    return out


def check_session(layout: Layout, team: Team, allow_mismatch: bool = False) -> None:
    """``team_session_mismatch`` when the resolved socket differs from ``team.json.socket``."""
    if not team.socket:
        return
    if os.path.realpath(team.socket) != os.fspath(layout.socket) and not allow_mismatch:
        raise HerdrTeamError(
            "team_session_mismatch",
            "team {!r} belongs to socket {} but this call resolved {}".format(team.team, team.socket, layout.socket),
            EXIT_REFUSED,
            {"team": team.team, "team_socket": team.socket, "socket": os.fspath(layout.socket)},
        )


# --------------------------------------------------------------------------
# pane records (panes/<terminal_id>.json = {team, name, gen})


def write_pane_record(session: SessionPaths, terminal_id: str, team: str, name: str, generation: int, agent_session: Optional[Dict[str, Any]] = None) -> Path:
    path = session.pane_record(terminal_id)
    ensure_dir(session.panes_dir)
    record: Dict[str, Any] = {"team": team, "name": name, "gen": int(generation)}
    if session_key(agent_session) is not None:
        record["session"] = dict(agent_session)  # type: ignore[arg-type]
    store.write_json(path, record)
    return path


def read_pane_record(session: SessionPaths, terminal_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not terminal_id:
        return None
    try:
        path = session.pane_record(terminal_id)
    except HerdrTeamError:
        return None
    doc = store.read_json(path, default=None)
    return doc if isinstance(doc, dict) else None


def remove_pane_record(session: SessionPaths, terminal_id: Optional[str]) -> bool:
    if not terminal_id:
        return False
    try:
        path = session.pane_record(terminal_id)
        os.unlink(path)
        return True
    except (HerdrTeamError, FileNotFoundError):
        return False


# --------------------------------------------------------------------------
# board helper (system records)


def member_roots(members: Iterable[Dict[str, Any]], home: Optional[str] = None) -> List[str]:
    """Distinct member ``cwd`` directories that exist, most common first; HOME (where a restart leaves a pane) is not a project root."""
    home_real = os.path.realpath(home or os.path.expanduser("~"))
    counts: Dict[str, int] = {}
    order: List[str] = []
    for member in members:
        cwd = member.get("cwd") if isinstance(member, dict) else None
        if not isinstance(cwd, str) or not cwd or member.get("kind") == "human" or member.get("status") in ("left",):
            continue
        try:
            if os.path.realpath(cwd) == home_real or not os.path.isdir(cwd):
                continue
        except OSError:
            continue
        if cwd not in counts:
            order.append(cwd)
        counts[cwd] = counts.get(cwd, 0) + 1
    return sorted(order, key=lambda c: (-counts[c], order.index(c)))


def system_record(event: str, text: str, to: Optional[Sequence[str]] = None, extra: Optional[Dict[str, Any]] = None, socket: Optional[str] = None) -> Dict[str, Any]:
    """A schema v1 board record with ``from:system``, ``kind:system`` (seq and ts assigned at append)."""
    record: Dict[str, Any] = {
        "v": 1, "seq": None, "ts": None, "from": "system", "from_label": None, "from_kind": None,
        "from_pane": None, "from_terminal": None, "from_gen": None,
        "origin": {"via": "system", "verified": True, "pid": os.getpid(), "ppid": os.getppid(), "workspace_id": None, "tab_id": None, "socket": socket},
        "to": list(to) if to else ["all"], "to_role": None, "kind": "system", "text": text, "refs": [],
        "reply_to": None, "retracts": None, "supersedes": None, "urgent": False, "ttl_ms": None,
        "truncated": False, "event": event, "relayed_for": None,
    }
    if extra:
        for key, value in extra.items():
            record[key] = value
    return record


def migrate_cursor(team_paths: TeamPaths, old_name: str, new_name: str) -> bool:
    """Carry a renamed member's read position to its new name.

    Every per-member artefact is keyed by name, so without this the new name
    has no cursor file, ``Cursors.get`` starts at seq 0 (``store.py``), and
    the member's next ``board --new`` replays the whole board. Best effort:
    a missing source, an existing target, or an unreadable file leaves the
    rename alone rather than failing it.
    """
    if old_name == new_name:
        return False
    try:
        source = team_paths.cursor(old_name)
        target = team_paths.cursor(new_name)
        if not source.is_file() or target.exists():
            return False
        doc = store.read_json(source, default=None)
        if not isinstance(doc, dict):
            return False
        store.write_json(target, doc)
        return True
    except (HerdrTeamError, OSError):
        return False


def append_system_record(team_paths: TeamPaths, event: str, text: str, to: Optional[Sequence[str]] = None, extra: Optional[Dict[str, Any]] = None, socket: Optional[str] = None) -> int:
    """Append a ``system`` record through ``store.BoardStore`` (plan 6.2 append sequence)."""
    record = system_record(event, text, to=to, extra=extra, socket=socket)
    return int(store.BoardStore(team_paths).append(dict(record)))


def read_board_records(team_paths: TeamPaths, event: Optional[str] = None) -> List[Dict[str, Any]]:
    """All board records (archive first), sorted by seq, optionally filtered by system ``event``."""
    records = list(store.BoardStore(team_paths).read(include_archive=True))
    if event is not None:
        records = [r for r in records if r.get("from") == "system" and r.get("kind") == "system" and r.get("event") == event]
    return records


# --------------------------------------------------------------------------
# claims


def _lock_teams(session: SessionPaths, names: Iterable[str]) -> List[store.FileLock]:
    locks: List[store.FileLock] = []
    try:
        for name in sorted(set(names)):
            team_paths = session.team(name)
            ensure_team_dirs(team_paths)
            locks.append(store.team_lock(team_paths).acquire())
    except BaseException:
        for lock in locks:
            lock.release()
        raise
    return locks


def claim_owner(layout: Layout, terminal_id: str, exclude_team: Optional[str] = None) -> Optional[Tuple[str, Member]]:
    """``(team, member)`` currently holding ``terminal_id`` in any team of the session, without locking."""
    for name in layout.session.list_teams():
        if name == exclude_team:
            continue
        try:
            team = load_team(layout.session.team(name))
        except HerdrTeamError:
            continue
        member = team.find_by_terminal(terminal_id)
        if member is not None:
            return name, member
    return None


def claim_check(layout: Layout, terminal_id: str, wanting_team: str, steal: bool = False) -> Optional[str]:
    """Return the team currently owning ``terminal_id`` (or None); raise ``member_claimed`` unless ``steal``.

    Takes ``team.lock`` of every team in the session (sorted) for the check
    so two concurrent ``create``/``add`` calls serialize on the same set.
    """
    validate_team_name(wanting_team)
    names = set(layout.session.list_teams())
    names.add(wanting_team)
    locks = _lock_teams(layout.session, names)
    try:
        owner = claim_owner(layout, terminal_id, exclude_team=wanting_team)
    finally:
        for lock in reversed(locks):
            lock.release()
    if owner is None:
        return None
    owner_team, member = owner
    if not steal:
        raise HerdrTeamError(
            "member_claimed",
            "terminal {} is already {!r} in team {!r} (use --steal to move it)".format(terminal_id, member.name, owner_team),
            EXIT_REFUSED,
            {"owner_team": owner_team, "member": member.name, "terminal_id": terminal_id},
        )
    return owner_team


def release_claim(layout: Layout, owner_team: str, terminal_id: str, reason: str, socket: Optional[str] = None) -> Optional[str]:
    """Mark the member holding ``terminal_id`` in ``owner_team`` as ``left`` and post ``member_gone``."""
    team_paths = layout.team(owner_team)
    gone: List[str] = []

    def mutate(team: Team) -> None:
        member = team.find_by_terminal(terminal_id)
        if member is not None:
            member.status = "left"
            member.last_seen_at = now_iso()
            gone.append(member.name)

    update_team(team_paths, mutate)
    if not gone:
        return None
    append_system_record(team_paths, "member_gone", "{} left team {} ({})".format(gone[0], owner_team, reason), to=["all"], socket=socket)
    return gone[0]


# --------------------------------------------------------------------------
# token projection (data only; the caller executes)


@dataclass(frozen=True)
class TokenCommand:
    """One ``pane.report_metadata`` call: ``tokens`` with ``None`` values clear keys."""

    pane_id: str
    source: str
    tokens: Dict[str, Optional[str]]
    ttl_ms: Optional[int] = None

    def params(self) -> Dict[str, Any]:
        params: Dict[str, Any] = {"pane_id": self.pane_id, "source": self.source, "tokens": dict(self.tokens)}
        if self.ttl_ms is not None:
            params["ttl_ms"] = int(self.ttl_ms)
        return params

    def argv(self) -> List[str]:
        """The equivalent ``herdr pane report-metadata`` argv (for logs and dry runs)."""
        args = ["pane", "report-metadata", self.pane_id, "--source", self.source]
        for key, value in self.tokens.items():
            args.extend(["--token", "{}={}".format(key, value if value is not None else "")])
        if self.ttl_ms is not None:
            args.extend(["--ttl-ms", str(int(self.ttl_ms))])
        return args


def _clip_token_value(value: str) -> str:
    text = value.replace("\n", " ").replace("\r", " ").strip()
    if len(text) > TOKEN_VALUE_MAX_CHARS:
        text = text[:TOKEN_VALUE_MAX_CHARS]
    return text


def token_commands(member: Member, team: str, task_headline: Optional[str] = None, clear: bool = False, pane_id: Optional[str] = None, color_slot: Optional[Any] = None) -> List[TokenCommand]:
    """What to stamp on a member's pane (plan 5.3): identity without TTL, ``team_task`` with 120 s.

    The identity command also carries the team's colour slot, so the Agents sidebar can show the
    team name in the team's own colour. ``color_slot=None`` clears every slot, which is the right
    state before the daemon has assigned one.

    ``clear=True`` yields the commands that remove every key.
    """
    target = pane_id or member.pane_id
    if not target:
        return []
    if clear:
        cleared: Dict[str, Optional[str]] = {"team": None, "team_role": None}
        cleared.update(color_slot_tokens(None, None))
        return [
            TokenCommand(target, TOKEN_SOURCE_ROSTER, cleared),
            TokenCommand(target, TOKEN_SOURCE_TASK, {"team_task": None}),
        ]
    identity: Dict[str, Optional[str]] = {"team": _clip_token_value(team), "team_role": _clip_token_value(member.role)}
    identity.update(color_slot_tokens(team, color_slot))
    out = [TokenCommand(target, TOKEN_SOURCE_ROSTER, identity)]
    if task_headline:
        out.append(TokenCommand(target, TOKEN_SOURCE_TASK, {"team_task": _clip_token_value(task_headline)}, TASK_TOKEN_TTL_MS))
    return out


def execute_token_commands(api: Any, commands: Iterable[TokenCommand]) -> List[Dict[str, Any]]:
    """Run the commands over the socket; a failure is recorded, never raised."""
    results: List[Dict[str, Any]] = []
    for command in commands:
        entry: Dict[str, Any] = {"pane_id": command.pane_id, "source": command.source, "ok": True}
        try:
            api.request("pane.report_metadata", command.params())
        except HerdrTeamError as err:
            entry["ok"] = False
            entry["error"] = err.code
        results.append(entry)
    return results


# --------------------------------------------------------------------------
# rehydration (plan 4.2), pure


@dataclass
class Binding:
    member: str
    how: str  # one of the MATCH_* constants
    agent: Dict[str, Any]
    kind_matches: bool = True


@dataclass
class RehydrationResult:
    bindings: List[Binding] = field(default_factory=list)
    unbound: List[Dict[str, Any]] = field(default_factory=list)  # {"member", "candidate", "how"}
    missing: List[str] = field(default_factory=list)
    kind_changed: List[Dict[str, Any]] = field(default_factory=list)  # {"member", "agent"}


def _agent_kind(row: Dict[str, Any]) -> Optional[str]:
    return row.get("agent")


def rehydrate_match(members: Sequence[Member], agent_list_rows: Sequence[Dict[str, Any]], pane_list_rows: Sequence[Dict[str, Any]] = ()) -> RehydrationResult:
    """Match roster members to live agents in the plan 4.2 order.

    (0) the harness session: a row whose ``agent_session`` names the same
    ``(source, value)`` the member recorded is that member, whatever its
    terminal, pane, label or name are now (the only key that survives a
    Herdr restart, since terminal ids are reallocated and names dropped);
    (a) ``terminal_id``; (b) ``pane list`` row with the member's label and a
    live agent of the member's kind on that terminal; (c) same public
    ``pane_id`` and kind; (d) exact name; (e) unique ``(kind, cwd,
    workspace)`` fingerprint. Each agent row binds at most one member. (e)
    goes to ``unbound`` with the candidate unless ``AUTO_BIND_FINGERPRINT``.
    A terminal-id match with a different kind lands in ``kind_changed``. A
    row whose ``agent`` is ``null`` (``launch_pending`` right after ``agent
    start``, or detection not yet run) is no evidence of another kind: it
    binds by terminal with ``kind_matches`` false and never flags
    ``kind_changed``, the same rule ``hooks._reconcile_detected`` applies
    (``live_kind and live_kind != member.kind``).
    """
    result = RehydrationResult()
    agents = [dict(a) for a in agent_list_rows if isinstance(a, dict) and a.get("terminal_id")]
    panes_by_terminal: Dict[str, Dict[str, Any]] = {}
    for pane in pane_list_rows:
        if isinstance(pane, dict) and pane.get("terminal_id"):
            panes_by_terminal[str(pane["terminal_id"])] = dict(pane)
    used: set = set()
    pending: List[Member] = [m for m in members if not m.is_human and m.status != "left"]

    def take(member: Member, row: Dict[str, Any], how: str) -> None:
        used.add(row["terminal_id"])
        result.bindings.append(Binding(member.name, how, row, kind_matches=(_agent_kind(row) == member.kind)))

    # (0) harness session. Several rows may carry one session for a moment (Herdr keeps a pane's
    # session until its process exits, so a resume elsewhere overlaps it): the member's own
    # terminal wins, so nothing moves until the old pane is really gone.
    sessions: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for row in agents:
        key = session_key(session_of(row))
        if key is not None:
            sessions.setdefault(key, []).append(row)
    rest: List[Member] = []
    for member in pending:
        key = session_key(member.session)
        rows = [r for r in (sessions.get(key) if key is not None else []) or [] if r["terminal_id"] not in used]
        if not rows:
            rest.append(member)
            continue
        own = next((r for r in rows if r["terminal_id"] == member.terminal_id), None)
        take(member, own or rows[0], MATCH_SESSION)
    pending = rest

    # (a) terminal id
    rest = []
    for member in pending:
        row = next((a for a in agents if a["terminal_id"] == member.terminal_id and a["terminal_id"] not in used), None)
        if row is None:
            rest.append(member)
        elif _agent_kind(row) is None or _agent_kind(row) == member.kind or member.kind == "unknown":
            take(member, row, MATCH_TERMINAL)
        else:
            used.add(row["terminal_id"])
            result.kind_changed.append({"member": member.name, "agent": row})
    pending = rest

    # (b) label + kind
    rest = []
    for member in pending:
        found = None
        for terminal_id, pane in panes_by_terminal.items():
            if terminal_id in used or pane.get("label") != member.label or not member.label:
                continue
            row = next((a for a in agents if a["terminal_id"] == terminal_id), None)
            if row is not None and _agent_kind(row) == member.kind:
                found = row
                break
        if found is None:
            rest.append(member)
        else:
            take(member, found, MATCH_LABEL)
    pending = rest

    # (c) pane id + kind
    rest = []
    for member in pending:
        row = next((a for a in agents if a["terminal_id"] not in used and a.get("pane_id") == member.pane_id and member.pane_id and _agent_kind(a) == member.kind), None)
        if row is None:
            rest.append(member)
        else:
            take(member, row, MATCH_PANE)
    pending = rest

    # (d) exact name
    rest = []
    for member in pending:
        row = next((a for a in agents if a["terminal_id"] not in used and a.get("name") == member.name), None)
        if row is None:
            rest.append(member)
        else:
            take(member, row, MATCH_NAME)
    pending = rest

    # (e) unique fingerprint
    for member in pending:
        candidates = [
            a for a in agents
            if a["terminal_id"] not in used
            and _agent_kind(a) == member.kind
            and (a.get("cwd") or None) == (member.cwd or None)
            and a.get("workspace_id") == member.workspace_id
        ]
        if len(candidates) == 1:
            if AUTO_BIND_FINGERPRINT:
                take(member, candidates[0], MATCH_FINGERPRINT)
            else:
                result.unbound.append({"member": member.name, "candidate": candidates[0], "how": MATCH_FINGERPRINT})
        else:
            result.missing.append(member.name)
    return result


# --------------------------------------------------------------------------
# name loss and adopt-versus-enforce, pure


NAME_LOSS_RELEASED = "released"
NAME_LOSS_SESSION_CHANGE = "session_change"
NAME_LOSS_LAUNCH_DEADLINE = "launch_deadline"


def classify_name_loss(event: str, data: Dict[str, Any], member: Member) -> Optional[str]:
    """Which plan 4.2 trigger (if any) an event is for ``member``.

    ``pane.agent_detected`` with ``released:true`` -> ``released`` (clear
    tokens, mark gone); ``pane.updated`` whose pane has no ``name`` while
    ``agent == member.kind`` on the member's terminal -> ``session_change``
    (re-apply once ``agent`` is set); ``launch_deadline`` when a managed
    launch ran out of time.
    """
    if not isinstance(data, dict):
        return None
    if event in ("pane.agent_detected", "pane_agent_detected"):
        if data.get("released") is True and data.get("pane_id") == member.pane_id:
            return NAME_LOSS_RELEASED
        return None
    if event in ("pane.updated", "pane_updated"):
        pane = data.get("pane") if isinstance(data.get("pane"), dict) else data
        if pane.get("terminal_id") != member.terminal_id:
            return None
        if pane.get("agent") == member.kind and not pane.get("name") and "name" in pane:
            return NAME_LOSS_SESSION_CHANGE
        if pane.get("agent") == member.kind and pane.get("name") is None and "name" not in pane and not pane.get("launch_pending"):
            # ``pane.updated`` omits ``name`` when it is unset; the same signal.
            return NAME_LOSS_SESSION_CHANGE
        return None
    if event == "launch_deadline":
        if member.status == "starting":
            return NAME_LOSS_LAUNCH_DEADLINE
        return None
    return None


def reconcile_live_name(member: Member, live_name: Optional[str], policy: str = NAME_POLICY_ADOPT, now: Optional[str] = None) -> Dict[str, Any]:
    """Decide what to do when the live agent's name differs from the roster.

    Returns ``{"action": "none"|"reapply"|"adopt", "name": ..., "old": ...}``.
    ``adopt`` (default) makes the roster follow a human rename and retires
    the old name for ``NAME_HISTORY_TTL_S``; ``enforce`` re-applies the
    roster name. A missing live name is always ``reapply``.
    """
    if live_name == member.name:
        return {"action": "none", "name": member.name, "old": None}
    if not live_name:
        return {"action": "reapply", "name": member.name, "old": None}
    if policy == NAME_POLICY_ENFORCE:
        return {"action": "reapply", "name": member.name, "old": live_name}
    try:
        validate_member_name(live_name)
    except HerdrTeamError as err:
        return {"action": "reapply", "name": member.name, "old": live_name, "refused": err.code}
    return {"action": "adopt", "name": live_name, "old": member.name, "retired_at": now or now_iso()}


def apply_adoption(member: Member, new_name: str, retired_at: Optional[str] = None) -> Member:
    """Record ``member.name`` in ``previous_names`` and switch to ``new_name`` (pure on the object)."""
    member.previous_names = [p for p in member.previous_names if p.get("name") != new_name]
    member.previous_names.append({"name": member.name, "retired_at": retired_at or now_iso()})
    member.name = new_name
    return member


# --------------------------------------------------------------------------
# who.json (plan 5.4), pure


def _headline_for(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    return sanitize.headline(text, TASK_TOKEN_MAX_COLUMNS)


def build_who_json(
    rosters: Dict[str, Team],
    agent_list: Sequence[Dict[str, Any]],
    charters: Dict[str, Any],
    mutes: Dict[str, Dict[str, Any]],
    ledger_counts: Dict[str, Dict[str, int]],
    default_team: Optional[str] = None,
    socket: Optional[str] = None,
    beat_at: Optional[str] = None,
    pending_nudges: Optional[Dict[str, Dict[str, int]]] = None,
    headlines: Optional[Dict[str, Dict[str, str]]] = None,
    hooks_last_seen: Optional[Dict[str, Dict[str, str]]] = None,
    unread: Optional[Dict[str, Dict[str, int]]] = None,
) -> Dict[str, Any]:
    """The ``who.json`` document (plan 5.4) from rosters plus one ``agent list`` snapshot.

    ``charters`` maps team -> charter dict (``seq``, ``text``/``headline``,
    ``refs``); ``mutes`` maps team -> ``mute.json``; ``ledger_counts`` maps
    team -> ``{"wrong_target": 0, ...}``; the optional maps are keyed
    ``team -> member -> value``.
    """
    by_terminal: Dict[str, Dict[str, Any]] = {}
    for row in agent_list:
        if isinstance(row, dict) and row.get("terminal_id"):
            by_terminal[str(row["terminal_id"])] = row
    doc: Dict[str, Any] = {
        "v": 1,
        "daemon_beat_at": beat_at or now_iso(),
        "socket": socket,
        "default_team": default_team,
        "charters": {},
        "teams": {},
        "ledger": {},
    }
    for team_name in sorted(rosters):
        team = rosters[team_name]
        charter = charters.get(team_name)
        if isinstance(charter, dict) and charter.get("seq") is not None:
            headline = charter.get("headline")
            if not headline:
                text = str(charter.get("text") or "")
                first = text.strip().splitlines()[0] if text.strip() else ""
                headline = first[:120]
            doc["charters"][team_name] = {"seq": charter.get("seq"), "headline": headline, "refs": list(charter.get("refs") or [])}
        mute = mutes.get(team_name) or {}
        members_out: List[Dict[str, Any]] = []
        for member in team.members:
            if member.status == "left":
                continue
            live = by_terminal.get(member.terminal_id or "") if member.terminal_id else None
            muted_until = mute.get(member.name) or mute.get("*")
            entry: Dict[str, Any] = {
                "name": member.name,
                "role": member.role,
                "kind": member.kind,
                "status": member.status,
                "agent_status": (live or {}).get("agent_status") if live else None,
                "pane_id": (live or {}).get("pane_id") or member.pane_id,
                "terminal_id": member.terminal_id,
                "workspace_id": (live or {}).get("workspace_id") or member.workspace_id,
                "terminal_title_stripped": (live or {}).get("terminal_title_stripped") if live else None,
                "last_headline": _headline_for(((headlines or {}).get(team_name) or {}).get(member.name)),
                "pending_nudges": int(((pending_nudges or {}).get(team_name) or {}).get(member.name, 0)),
                "muted_until": muted_until,
                "verified_kind": bool(member.verified_kind),
                "delivery": member.delivery,
                "hooks_last_seen": ((hooks_last_seen or {}).get(team_name) or {}).get(member.name),
                "last_seen_at": member.last_seen_at,
                "briefed": member.briefed_at is not None,
                "charter_stale": _charter_stale(member, charter),
                "instructions_stale": _instructions_stale(member),
                "unread": int(((unread or {}).get(team_name) or {}).get(member.name, 0)),
                "brief": member.brief,
                "session": short_session(member.session),
                "live_name": (live or {}).get("name") if live else None,
                "focused": bool((live or {}).get("focused")) if live else False,
                "launch_pending": bool((live or {}).get("launch_pending")) if live else False,
            }
            members_out.append(entry)
        doc["teams"][team_name] = {"members": members_out, "naming": team.naming, "revision": team.revision}
        doc["ledger"][team_name] = dict(ledger_counts.get(team_name) or {})
    return doc


def _instructions_stale(member: Member) -> bool:
    """True when the member has instructions it has not acknowledged."""
    if member.is_human:
        return False
    seq = int(member.instructions_seq or 0)
    if seq <= 0:
        return False
    acked = member.instructions_seq_acked
    try:
        return acked is None or int(acked) < seq
    except (TypeError, ValueError):
        return True


def _charter_stale(member: Member, charter: Optional[Dict[str, Any]]) -> bool:
    if member.is_human or not isinstance(charter, dict) or charter.get("seq") is None:
        return False
    acked = member.charter_seq_acked
    try:
        return acked is None or int(acked) < int(charter["seq"])
    except (TypeError, ValueError):
        return True


# --------------------------------------------------------------------------
# Roster: member CRUD over one team dir


@dataclass
class ResolvedTarget:
    pane_id: str
    terminal_id: str
    kind: Optional[str]
    name: Optional[str]
    workspace_id: Optional[str]
    tab_id: Optional[str]
    cwd: Optional[str]
    launch_pending: bool
    agent_status: Optional[str]
    raw: Dict[str, Any]
    agent_session: Optional[Dict[str, Any]] = None


def resolve_target(api: Any, target: str) -> ResolvedTarget:
    """``agent.get`` with the plan 5.2 refusals; a shell pane is ``not_an_agent`` after a ``pane.get`` cross-check."""
    try:
        result = api.request("agent.get", {"target": target})
    except HerdrTeamError as err:
        if err.code == "agent_not_found":
            pane = None
            try:
                pane = api.request("pane.get", {"pane_id": target}).get("pane")
            except HerdrTeamError:
                pane = None
            if isinstance(pane, dict):
                raise HerdrTeamError(
                    "not_an_agent",
                    "pane {} hosts no detected agent (start one, or use create --new --spawn)".format(pane.get("pane_id") or target),
                    EXIT_REFUSED,
                    {"target": target, "pane_id": pane.get("pane_id"), "terminal_id": pane.get("terminal_id")},
                )
            if target in ALL_KIND_WORDS:
                raise HerdrTeamError("agent_not_found", "{!r} is an agent kind, not a target; pass a pane id or the agent's name".format(target), EXIT_REFUSED, {"target": target, "hint": "kind_label"})
        raise
    agent = result.get("agent") if isinstance(result, dict) else None
    if not isinstance(agent, dict) or not agent.get("terminal_id"):
        raise HerdrTeamError("herdr_protocol", "agent.get returned no agent object", 3, {"target": target})
    if agent.get("launch_pending"):
        raise HerdrTeamError("launch_pending", "agent in {} is still starting; wait for it to settle".format(agent.get("pane_id")), EXIT_REFUSED, {"target": target, "pane_id": agent.get("pane_id")})
    return ResolvedTarget(
        pane_id=str(agent.get("pane_id")),
        terminal_id=str(agent.get("terminal_id")),
        kind=agent.get("agent"),
        name=agent.get("name"),
        workspace_id=agent.get("workspace_id"),
        tab_id=agent.get("tab_id"),
        cwd=agent.get("cwd"),
        launch_pending=bool(agent.get("launch_pending")),
        agent_status=agent.get("agent_status"),
        raw=agent,
        agent_session=session_of(agent),
    )


def live_names(api: Any) -> List[str]:
    """Every agent name currently in use in the session."""
    try:
        result = api.request("agent.list", {})
    except HerdrTeamError:
        return []
    names: List[str] = []
    for row in result.get("agents") or []:
        if isinstance(row, dict) and row.get("name"):
            names.append(str(row["name"]))
    return names


def rename_agent(api: Any, pane_id: str, name: Optional[str]) -> None:
    """``agent.rename``; ``agent_name_taken`` carries parsed ``candidates``."""
    try:
        api.request("agent.rename", {"target": pane_id, "name": name})
    except HerdrTeamError as err:
        if err.code == "agent_name_taken":
            err.details["candidates"] = parse_name_taken_candidates(err.message)
            err.details["name"] = name
        raise


def label_pane(api: Any, pane_id: str, label: Optional[str]) -> bool:
    """``pane.rename``; failures are tolerated (labels are a projection)."""
    try:
        api.request("pane.rename", {"pane_id": pane_id, "label": label})
        return True
    except HerdrTeamError:
        return False


def clear_stale_label(api: Any, pane_id: str, label: Optional[str]) -> bool:
    """Drop ``label`` from ``pane_id`` when that pane still carries it and hosts no agent (RT-02).

    A member bound elsewhere (``bind`` after a cold restart, or a rebind by
    name in another pane) leaves its old pane behind as a plain shell that
    would otherwise keep ``team:<team>/<role>`` forever: ``dissolve`` and
    ``remove`` only touch the member's current pane. Nothing is cleared when
    the pane is gone, carries another label, or hosts an agent (another
    member may have taken it over). Failures are tolerated.
    """
    if not label or not pane_id:
        return False
    try:
        result = api.request("pane.list", {})
    except HerdrTeamError:
        return False
    panes = result.get("panes") if isinstance(result, dict) else None
    for pane in panes or []:
        if isinstance(pane, dict) and pane.get("pane_id") == pane_id:
            if pane.get("label") == label and not pane.get("agent"):
                return label_pane(api, pane_id, None)
            return False
    return False


def write_briefing_job(team_paths: TeamPaths, member_name: str, requested_by: Optional[Dict[str, Any]] = None, kind: str = "brief", force: bool = False) -> Path:
    """``notifier/jobs/<ts>-<id>.json`` for the daemon (docs/cli.md section 8)."""
    ensure_team_dirs(team_paths)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    job_id = "{}-{}-{}".format(stamp, os.getpid(), member_name)
    path = team_paths.jobs_dir / "{}.json".format(job_id)
    store.write_json(path, {
        "v": 1, "kind": kind, "member": member_name, "force": bool(force),
        "requested_by": requested_by or {"name": "system", "via": "system"}, "requested_at": now_iso(),
    })
    return path


def _ensure_daemon(layout: Layout, env: Dict[str, str]) -> Optional[bool]:
    """Best effort: a join never fails because the notifier could not start (``HERDR_TEAM_NO_DAEMON=1`` skips it)."""
    if env.get("HERDR_TEAM_NO_DAEMON") == "1":
        return None
    try:
        from herdr_team import daemon

        return bool(daemon.ensure_daemon(layout, env))
    except (HerdrTeamError, OSError):
        return None


class Roster:
    """Member CRUD for one team (``team.json`` under ``team.lock``)."""

    def __init__(self, layout: Layout, name: str) -> None:
        self.layout = layout
        self.name = validate_team_name(name)
        self.paths = layout.team(name)

    # -- reads -------------------------------------------------------------------

    def load(self) -> Team:
        return load_team(self.paths)

    def exists(self) -> bool:
        return self.paths.team_json.is_file()

    def update(self, mutate: Callable[[Team], Any]) -> Team:
        return update_team(self.paths, mutate)

    # -- membership --------------------------------------------------------------

    def add_member(self, member: Member, steal: bool = False, socket: Optional[str] = None) -> Tuple[Team, Optional[str]]:
        """Claim check, then append; returns ``(team, previous_owner_team)``."""
        validate_member_name(member.name)
        validate_role(member.role, allow_kind_label=True)  # the name was validated on its own above
        previous: Optional[str] = None
        if member.terminal_id:
            previous = claim_check(self.layout, member.terminal_id, self.name, steal=steal)
            if previous:
                release_claim(self.layout, previous, member.terminal_id, "moved to team {}".format(self.name), socket=socket)

        def mutate(team: Team) -> None:
            if any(m.name == member.name and m.status != "left" for m in team.members):
                raise HerdrTeamError("name_taken", "{!r} is already a member of team {!r}".format(member.name, team.team), EXIT_REFUSED, {"name": member.name, "team": team.team})
            existing = team.find_by_terminal(member.terminal_id, include_left=True) if member.terminal_id else None
            if existing is not None and existing.status == "left":
                team.members.remove(existing)
            if not member.joined_at:
                member.joined_at = now_iso()
            team.members.append(member)

        team = self.update(mutate)
        if member.terminal_id:
            write_pane_record(self.layout.session, member.terminal_id, self.name, member.name, member.generation, member.session)
        return team, previous

    def set_status(self, name: str, status: str, **fields: Any) -> Member:
        if status not in STATUSES:
            raise HerdrTeamError("usage", "unknown member status {!r}".format(status), 2, {"status": status})
        found: List[Member] = []

        def mutate(team: Team) -> None:
            member = team.find(name)
            if member is None:
                raise HerdrTeamError("member_not_found", "{!r} is not in team {!r}".format(name, team.team), EXIT_REFUSED, {"name": name, "team": team.team, "roster": team.names()})
            member.status = status
            for key, value in fields.items():
                if hasattr(member, key):
                    setattr(member, key, value)
            found.append(member)

        self.update(mutate)
        return found[0]

    def remove_member(self, api: Any, name: str, keep_name: bool = False, reason: str = "removed", socket: Optional[str] = None) -> Dict[str, Any]:
        """Clear tokens and label, mark ``left`` (tombstone), post ``member_gone``."""
        team = self.load()
        member = team.find(name)
        if member is None or member.is_human:
            raise HerdrTeamError("member_not_found", "{!r} is not an agent member of team {!r}".format(name, self.name), EXIT_REFUSED, {"name": name, "team": self.name, "roster": team.names()})
        if member.status == "left":
            raise HerdrTeamError("member_not_found", "{!r} already left team {!r}".format(name, self.name), EXIT_REFUSED, {"name": name, "team": self.name, "roster": team.names(), "status": "left"})
        cleared = execute_token_commands(api, token_commands(member, self.name, clear=True))
        tokens_cleared = all(entry["ok"] for entry in cleared) if cleared else False
        if member.pane_id:
            label_pane(api, member.pane_id, None)
        name_cleared = False
        if not keep_name and member.pane_id:
            try:
                rename_agent(api, member.pane_id, None)
                name_cleared = True
            except HerdrTeamError:
                name_cleared = False

        def mutate(doc: Team) -> None:
            target = doc.find(name)
            if target is not None:
                target.status = "left"
                target.last_seen_at = now_iso()

        self.update(mutate)
        remove_pane_record(self.layout.session, member.terminal_id)
        seq = append_system_record(self.paths, "member_gone", "{} left team {} ({})".format(member.name, self.name, reason), to=["all"], socket=socket)
        return {"team": self.name, "removed": member.name, "tokens_cleared": tokens_cleared, "name_cleared": name_cleared, "record_seq": seq}

    def leave(self, api: Any, member_name: str, socket: Optional[str] = None) -> Dict[str, Any]:
        result = self.remove_member(api, member_name, keep_name=False, reason="left", socket=socket)
        return {"team": self.name, "left": result["removed"]}

    def bind(self, api: Any, name: str, target: ResolvedTarget, socket: Optional[str] = None) -> Dict[str, Any]:
        """Re-attach a ``missing``/``unbound``/``kind_changed`` member to a live agent: ``generation + 1``."""
        previous_terminal: List[Optional[str]] = []
        previous_pane: List[Optional[str]] = []

        def mutate(team: Team) -> None:
            member = team.find(name)
            if member is None or member.is_human:
                raise HerdrTeamError("member_not_found", "{!r} is not in team {!r}".format(name, team.team), EXIT_REFUSED, {"name": name, "team": team.team, "roster": team.names()})
            if target.kind and target.kind != member.kind and member.status != "kind_changed":
                raise HerdrTeamError("kind_mismatch", "{} is a {} agent, member {!r} is {}".format(target.pane_id, target.kind, name, member.kind), EXIT_REFUSED, {"name": name, "kind": member.kind, "live_kind": target.kind})
            previous_terminal.append(member.terminal_id)
            previous_pane.append(member.pane_id)
            member.terminal_id = target.terminal_id
            member.pane_id = target.pane_id
            member.workspace_id = target.workspace_id
            member.tab_id = target.tab_id
            member.cwd = target.cwd or member.cwd
            if target.kind:
                member.kind = target.kind
            if target.agent_session is not None:
                member.session = target.agent_session
            member.generation = int(member.generation) + 1
            member.status = "active"
            member.last_seen_at = now_iso()
            member.label = label_for(team.team, member.role)

        owner = claim_owner(self.layout, target.terminal_id, exclude_team=self.name)
        if owner is not None:
            raise HerdrTeamError("member_claimed", "terminal {} is {!r} in team {!r}".format(target.terminal_id, owner[1].name, owner[0]), EXIT_REFUSED, {"owner_team": owner[0], "member": owner[1].name})
        team = self.update(mutate)
        member = team.find(name)
        assert member is not None
        if target.name != member.name:
            rename_agent(api, target.pane_id, member.name)
        label_pane(api, target.pane_id, member.label)
        if previous_pane and previous_pane[0] and previous_pane[0] != target.pane_id:
            clear_stale_label(api, previous_pane[0], member.label)  # RT-02: the old pane is a plain shell after a cold restart
        execute_token_commands(api, token_commands(member, self.name, color_slot=team.color_slot))
        if previous_terminal and previous_terminal[0] and previous_terminal[0] != target.terminal_id:
            remove_pane_record(self.layout.session, previous_terminal[0])
        write_pane_record(self.layout.session, target.terminal_id, self.name, member.name, member.generation, member.session)
        append_system_record(self.paths, "member_restarted", "{} rebound to {} (generation {})".format(member.name, target.pane_id, member.generation), to=["all"], socket=socket)
        return {"team": self.name, "member": member.to_json(), "previous_terminal_id": previous_terminal[0] if previous_terminal else None}

    def dissolve(self, api: Any, timestamp: Optional[str] = None) -> Dict[str, Any]:
        """Clear tokens and labels for every member and move the team dir to ``_archive/<team>-<ts>/``."""
        team = self.load()
        cleared = 0
        for member in team.agents():
            if not member.pane_id:
                continue
            results = execute_token_commands(api, token_commands(member, self.name, clear=True))
            if results and all(r["ok"] for r in results):
                cleared += 1
            label_pane(api, member.pane_id, None)
            remove_pane_record(self.layout.session, member.terminal_id)
        stamp = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = self.layout.session.team_archive(self.name, stamp)
        ensure_dir(self.layout.session.archive_dir)
        with store.team_lock(self.paths):
            os.rename(self.paths.root, destination)
        return {"team": self.name, "archived_to": os.fspath(destination), "members_cleared": cleared}

    def adopt_rename(self, member_name: str, new_name: str, socket: Optional[str] = None) -> Member:
        """The roster follows a rename by hand (or ``herdr-team rename``): history plus a board note."""
        validate_member_name(new_name)
        adopted: List[Member] = []

        def mutate(team: Team) -> None:
            member = team.find(member_name)
            if member is None:
                raise HerdrTeamError("member_not_found", "{!r} is not in team {!r}".format(member_name, team.team), EXIT_REFUSED, {"name": member_name, "team": team.team, "roster": team.names()})
            if any(m is not member and m.name == new_name and m.status != "left" for m in team.members):
                raise HerdrTeamError("name_taken", "{!r} is already a member of team {!r}".format(new_name, team.team), EXIT_REFUSED, {"name": new_name})
            apply_adoption(member, new_name)
            adopted.append(member)

        self.update(mutate)
        member = adopted[0]
        migrate_cursor(self.paths, member_name, member.name)
        if member.terminal_id:
            write_pane_record(self.layout.session, member.terminal_id, self.name, member.name, member.generation, member.session)
        append_system_record(self.paths, "renamed", "{} is now {} (old name resolves for 10 min)".format(member_name, new_name), to=["all"], socket=socket)
        return member


# --------------------------------------------------------------------------
# the join routine


def join(layout: Layout, api: Any, team: Team, target: str, role: str, name: Optional[str], brief: Optional[str], steal: bool = False, env: Optional[Dict[str, str]] = None, requested_by: Optional[Dict[str, Any]] = None) -> Member:
    """The one join routine every selection path ends in.

    Resolve the target, validate role and name, claim check, ``agent.rename``
    unless already named so, ``pane.rename`` with the team label, append to
    the roster, stamp tokens, ensure the daemon, enqueue the briefing job.
    Everything that touches Herdr happens only after every local validation
    passed, so a refusal leaves no half-named member.
    """
    role = validate_role(role, allow_kind_label=(team.naming != "plain"))
    wanted = validate_member_name(name) if name else derive_name(team.team, role, team.naming)
    resolved = resolve_target(api, target)
    if resolved.kind is None:
        raise HerdrTeamError("not_an_agent", "pane {} hosts no detected agent".format(resolved.pane_id), EXIT_REFUSED, {"target": target})
    if brief is not None:
        brief = _sanitize_brief(brief)
    existing = [m.name for m in team.members if m.status != "left"]
    taken_live = [n for n in live_names(api) if n != resolved.name]
    if wanted in existing or wanted in taken_live:
        if name is not None:
            code = "name_taken" if wanted in existing else "agent_name_taken"
            raise HerdrTeamError(code, "name {!r} is already in use".format(wanted), EXIT_REFUSED, {"name": wanted, "roster": existing})
        wanted = unique_name(wanted, set(existing) | set(taken_live))
    # Claim check before anything touches Herdr (plan 5.2 order).
    previous_owner = claim_check(layout, resolved.terminal_id, team.team, steal=steal)

    roster = Roster(layout, team.team)
    label = label_for(team.team, role)
    member = Member(
        name=wanted,
        role=role,
        kind=str(resolved.kind),
        terminal_id=resolved.terminal_id,
        pane_id=resolved.pane_id,
        workspace_id=resolved.workspace_id,
        tab_id=resolved.tab_id,
        label=label,
        cwd=resolved.cwd,
        brief=brief,
        managed=False,
        session=resolved.agent_session,
        status="active",
        generation=1,
        delivery="nudge",
        verified_kind=_kind_verified(layout, str(resolved.kind)),
        joined_at=now_iso(),
        last_seen_at=now_iso(),
    )
    # Herdr first: a failed rename must leave the roster untouched.
    if resolved.name != wanted:
        rename_agent(api, resolved.pane_id, wanted)
    label_pane(api, resolved.pane_id, label)
    saved, _previous = roster.add_member(member, steal=steal or previous_owner is not None, socket=os.fspath(layout.socket))
    team.members = saved.members
    team.revision = saved.revision
    execute_token_commands(api, token_commands(member, team.team, color_slot=team.color_slot))
    _ensure_daemon(layout, dict(env or {}))
    write_briefing_job(roster.paths, member.name, requested_by=requested_by)
    return member


def kind_entry_trusted(entry: Any) -> bool:
    """One ``kinds.json`` entry passes gate 4 (plan 8.2) when the kind is ``verified`` (20 clean
    round trips through the ledger), ``trusted`` (an explicit owner override), or has a passed
    ``probe`` (the one verified send-and-read round trip ``hooks probe <kind>`` records).

    Without the probe clause nothing could ever be delivered to a new kind: the ledger needs
    delivered round trips to verify it, and gate 4 held every delivery until it was verified
    (observed live in M0 on 2026-09-05: a fresh Claude member was ``held: kind_unverified``
    forever, probe or not).
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("verified") or entry.get("trusted"):
        return True
    probe = entry.get("probe")
    return bool(isinstance(probe, dict) and probe.get("ok"))


def kind_trusted(doc: Any, kind: str) -> bool:
    """``kind_entry_trusted`` for ``kinds.json[kind]``; a missing or malformed document trusts nothing."""
    if not isinstance(doc, dict) or not kind:
        return False
    return kind_entry_trusted(doc.get(kind))


def _kind_verified(layout: Layout, kind: str) -> bool:
    return kind_trusted(store.read_json(layout.session.kinds_json, default=None), kind)


def _sanitize_brief(text: str) -> str:
    return sanitize.sanitize_text(text, max_len=MAX_TEXT_FALLBACK).strip()


MAX_TEXT_FALLBACK = 2000
