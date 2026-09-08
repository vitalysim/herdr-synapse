"""Per-member eligibility gate for nudges and briefings (plan 8.2), fail-fast.

Pure decision logic over snapshots the daemon collected; no I/O here so the
gate is unit-testable with fakes. Every hold reason below is a stable
string that lands in the ledger and in ``who`` output.

Three layers, all pure:

* Region extraction mirroring Herdr's detector at tag v0.8.2
  (``src/detect/manifest.rs``): ``after_last_horizontal_rule``,
  ``prompt_box_body`` (Claude), ``after_last_prompt_marker`` (Codex),
  ``bottom_non_empty_lines``. The dialog heuristic (gate 8) and the
  prompt-line check (gate 9) only ever look inside those regions.
* ``evaluate(snapshot, pending, now_ms, global_last_nudge_ms, pair_exchanges)``:
  gates 1 to 11 in order; the first failing gate names the hold. Terminal
  outcomes (nothing to deliver, ever) are ``drop``; everything else is
  ``hold`` and re-evaluated on the next event.
* ``decide(...)``: the daemon-facing wrapper that builds the snapshot from a
  roster member, an ``agent.list`` row, ``agent explain --json``, the
  detection-source text, and the sample history, then calls ``evaluate``.

Post-send classification (``classify_result``) and the retry table
(``next_action``) live here too so the daemon stays a thin loop.

Clocks: every ``*_ms`` value is ``time.monotonic()`` milliseconds. The post
TTL is target-active time (``ActiveClock``), never wall-clock age.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, fields
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

STABLE_MS_SCREEN = 2000
STABLE_MS_HOOK = 750
STABLE_MS_HOOKS_DELIVERY = 15000
DONE_HOLD_MS = 60000
MIN_INTERVAL_MS = 20000
GLOBAL_INTERVAL_MS = 1500
FOCUS_MAX_HOLD_MS = 300000
FOCUS_SNAPSHOT_STABLE_MS = 3000
DIALOG_HOLD_CAP_MS = 600000
PAIR_BUDGET = 10
PAIR_WINDOW_MS = 600000
SAMPLE_GAP_RESET_MS = 10000
#: Plan 12 "same-second bursts -> one nudge per range": a nudge waits this long after its newest seq arrived.
BURST_WINDOW_MS = 1000
POST_TTL_MS = 1800000
#: Accepted ``nudge_focused`` values: gate 10 holds a focused pane only for the exact word ``never``.
NUDGE_FOCUSED_VALUES = ("never", "always")
#: Kinds whose running turn a teammate's ``post --interrupt`` may be typed into. Claude Code queues a
#: line typed mid-turn behind its current step (verified live with the console's ``!!`` on 2026-09-05);
#: other kinds are unverified, so the default is Claude only. ``team.json`` ``config.gate.interrupt_kinds``
#: widens it or, empty, turns interrupts off.
INTERRUPT_KINDS = ("claude",)
#: One interrupt per sender and target inside this window; a second one is an ordinary urgent nudge.
INTERRUPT_COOLDOWN_MS = 600000
HOLD_REEVAL_MS = 5000
TRANSIENT_BACKOFF_MIN_MS = 3000
TRANSIENT_BACKOFF_MAX_MS = 60000
PING_INTERVAL_MS = 5000
PANE_STUCK_MS = 60000
POST_SEND_READ_DELAY_MS = 1500
RENUDGE_SCHEDULE_MS = (120000, 300000, 600000)
MAX_LANDED_ATTEMPTS = 1 + len(RENUDGE_SCHEDULE_MS)
NOT_SUBMITTED_HOLD_MS = 60000

ACTION_SEND = "send"
ACTION_HOLD = "hold"
ACTION_DROP = "drop"

HOLD_READ_BEFORE_NUDGE = "read_before_nudge"
HOLD_NOTHING_PENDING = "nothing_pending"
HOLD_EXPIRED = "expired"
HOLD_SELF = "self"
HOLD_NO_TERMINAL = "no_terminal"
HOLD_BROADCAST = "broadcast"
HOLD_ABSENT = "absent"
HOLD_KIND_MISMATCH = "kind_mismatch"
HOLD_KIND_UNVERIFIED = "kind_unverified"
HOLD_LAUNCH_PENDING = "launch_pending"
HOLD_NOT_IDLE = "not_idle"
HOLD_UNSTABLE = "unstable"
HOLD_WEAK_IDLE = "weak_idle"
HOLD_DONE_HOLD = "done_hold"
HOLD_BLOCKED = "blocked"
HOLD_VISIBLE_BLOCKER = "visible_blocker"
HOLD_SKIP_STATE_UPDATE = "skip_state_update"
HOLD_DIALOG = "dialog"
HOLD_DRAFT_PRESENT = "draft_present"
HOLD_FOCUSED = "focused"
HOLD_IN_FLIGHT = "in_flight"
HOLD_INTERVAL = "interval"
HOLD_GLOBAL_INTERVAL = "global_interval"
HOLD_PAIR_BUDGET = "pair_budget"
HOLD_MUTED = "muted"
HOLD_PANE_STUCK = "pane_stuck"
HOLD_NAME_MISMATCH = "name_mismatch"
#: Daemon-side hold (plan 12, SK-08): a Claude Stop-hook block covering the pending seqs within 10 min.
HOLD_STOP_BLOCKED = "stop_blocked"

#: Reasons that end the pending work for this member instead of pausing it.
DROP_REASONS = frozenset({
    HOLD_READ_BEFORE_NUDGE, HOLD_NOTHING_PENDING, HOLD_EXPIRED, HOLD_SELF,
    HOLD_NO_TERMINAL, HOLD_BROADCAST,
})
#: Gate 11 holds; the task vocabulary calls the family ``rate_limited``.
RATE_LIMIT_HOLDS = frozenset({
    HOLD_IN_FLIGHT, HOLD_INTERVAL, HOLD_GLOBAL_INTERVAL, HOLD_PAIR_BUDGET, HOLD_PANE_STUCK,
})
#: Holds that ask the daemon for one coalesced toast per (member, reason).
TOAST_HOLDS = frozenset({HOLD_KIND_MISMATCH, HOLD_KIND_UNVERIFIED, HOLD_PAIR_BUDGET})

IDLE_STATUSES = ("idle", "done")

DIALOG_MARKERS = (
    "esc to cancel", "enter to confirm", "enter to select", "action required",
    "allow command?", "do you trust the contents", "run a dynamic workflow?",
    "requests your input", "enter to submit", "tab to amend", "select model",
    "showing detailed transcript",
    # Kinds whose manifests carry no ``blocked`` rule rely on this list alone (a human ``say`` types
    # into them without waiting for idle): Claude's folder trust, Codex's continue prompt, Gemini's approvals.
    "do you trust the files in this folder", "press enter to continue", "allow execution",
)
CODEX_VIEWER_MARKERS = ("↑/↓ to scroll", "q to quit")

#: Bottom-of-screen window used for kinds without a Herdr region mapping.
GENERIC_DIALOG_LINES = 6

RESULT_LANDED_WORKING = "landed_working"
RESULT_LANDED_BLOCKED = "landed_blocked"
RESULT_LANDED_IN_TURN = "landed_in_turn"
RESULT_WRONG_OCCUPANT = "wrong_occupant"
RESULT_STALLED = "stalled"
RESULT_HUNG = "hung"
RESULT_NOT_SUBMITTED = "not_submitted"
RESULT_SENT = "sent"
RESULT_REFUSED = "refused"
RESULT_BUSY = "busy"
RESULT_TRANSIENT = "transient"
RESULT_PANE_STUCK = "pane_stuck"
RESULT_RE_RESOLVE = "re_resolve"
RESULT_UNKNOWN_ERROR = "unknown_error"

LANDED_RESULTS = frozenset({RESULT_LANDED_WORKING, RESULT_LANDED_BLOCKED, RESULT_STALLED, RESULT_SENT})

RETRY_RETRY = "retry"
RETRY_WAIT = "wait"
RETRY_RENUDGE = "renudge"
RETRY_RE_RESOLVE = "re_resolve"
RETRY_PING = "ping"
RETRY_HOLD = "hold"
RETRY_ABANDON = "abandon"
RETRY_DONE = "done"

_CLAUDE_PROMPT_RE = re.compile(r"^\s*❯")
_CLAUDE_PROMPT_SPLIT_RE = re.compile(r"^\s*❯[ \t]?")
_GENERIC_PROMPT_MARKERS = ("❯", "›", "> ", "$ ")

#: One CSI sequence (``ESC [ params intermediates final``) or one OSC string, as painted by ``agent.read
#: --source visible --format ansi``. The detection source is always plain text (Herdr 0.8.2), so styling
#: is only available from the visible viewport.
_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
#: SGR parameters that reset the faint attribute (2): a full reset or ``22`` (normal intensity).
_SGR_UNFAINT = frozenset({"", "0", "22"})


def _sgr_codes(params: str) -> List[str]:
    """SGR parameters with the sub-parameters of extended colours (38/48/58 ``;2;r;g;b`` and ``;5;n``) dropped.

    ``\x1b[38;2;255;255;255m`` carries a literal ``2`` that is a colour-space
    selector, not the faint attribute; a naive split would read it as dim.
    """
    parts = params.split(";") if params else [""]
    out: List[str] = []
    index = 0
    while index < len(parts):
        code = parts[index]
        out.append(code)
        if code in ("38", "48", "58") and index + 1 < len(parts):
            selector = parts[index + 1]
            index += 1 + (4 if selector == "2" else 1 if selector == "5" else 0)
        index += 1
    return out


def visible_line_text(line: str, drop_faint: bool = False) -> str:
    """The plain text of one ANSI-styled screen line; with ``drop_faint`` the runs painted with SGR 2 are removed.

    Claude Code 2.1.261 paints its prompt suggestion ("ghost text": a
    proposed next prompt the user accepts with Tab) inside the prompt box as
    ``❯\xa0ESC[2m<suggestion>ESC[0m``. It reads as a draft in the plain
    detection text, so gate 9 held every nudge as ``draft_present`` for as
    long as the suggestion stayed on screen (observed live 2026-09-05, rig
    ND-04-prep). A typed draft is painted without the faint attribute.
    """
    out: List[str] = []
    faint = False
    pos = 0
    for match in _CSI_RE.finditer(line):
        segment = line[pos:match.start()]
        if segment and not (drop_faint and faint):
            out.append(segment)
        pos = match.end()
        seq = match.group()
        if seq.endswith("m"):
            for code in _sgr_codes(seq[2:-1]):
                if code in _SGR_UNFAINT:
                    faint = False
                elif code == "2":
                    faint = True
    segment = line[pos:]
    if segment and not (drop_faint and faint):
        out.append(segment)
    return _OSC_RE.sub("", "".join(out)).rstrip("\r")


def styled_prompt_line_text(visible_ansi: Optional[str], kind: str) -> Optional[str]:
    """Gate 9 on a ``--source visible --format ansi`` read: the *typed* draft, faint ghost text excluded.

    Only Claude paints suggestions today, so other kinds get None (caller
    falls back to the detection text). None also when the viewport shows no
    prompt box (scrolled away, or another screen), so the detection text
    keeps the last word; ``""`` means the box is there and nothing is typed.
    """
    if visible_ansi is None or kind != "claude":
        return None
    plain_lines: List[str] = []
    for raw in visible_ansi.split("\n"):
        plain = visible_line_text(raw)
        if _CLAUDE_PROMPT_RE.match(plain):
            plain = visible_line_text(raw, drop_faint=True)
        plain_lines.append(plain)
    plain_text = "\n".join(plain_lines)
    if prompt_box_body(plain_text) is None:
        return None
    return prompt_line_text(plain_text, kind)


# --------------------------------------------------------------------------
# configuration


@dataclass(frozen=True)
class GateConfig:
    """Every tunable of section 8.2; ``from_mapping`` accepts partial overrides."""

    stable_ms_screen: int = STABLE_MS_SCREEN
    stable_ms_hook: int = STABLE_MS_HOOK
    stable_ms_hooks_delivery: int = STABLE_MS_HOOKS_DELIVERY
    done_hold_ms: int = DONE_HOLD_MS
    min_interval_ms: int = MIN_INTERVAL_MS
    global_interval_ms: int = GLOBAL_INTERVAL_MS
    focus_max_hold_ms: int = FOCUS_MAX_HOLD_MS
    focus_snapshot_stable_ms: int = FOCUS_SNAPSHOT_STABLE_MS
    dialog_hold_cap_ms: int = DIALOG_HOLD_CAP_MS
    pair_budget: int = PAIR_BUDGET
    pair_window_ms: int = PAIR_WINDOW_MS
    sample_gap_reset_ms: int = SAMPLE_GAP_RESET_MS
    post_ttl_ms: int = POST_TTL_MS
    burst_window_ms: int = BURST_WINDOW_MS
    nudge_focused: str = "never"
    #: Kinds an agent's ``post --interrupt`` may be typed into while they work (empty: interrupts off).
    interrupt_kinds: Tuple[str, ...] = INTERRUPT_KINDS
    #: One interrupt per sender and target inside this window.
    interrupt_cooldown_ms: int = INTERRUPT_COOLDOWN_MS

    @classmethod
    def from_mapping(cls, overrides: Optional[Mapping[str, Any]]) -> "GateConfig":
        if overrides is None:
            return cls()
        if isinstance(overrides, cls):
            return overrides
        known = {f.name for f in fields(cls)}
        values = {}
        for key, value in overrides.items():
            if key not in known:
                raise ValueError("unknown gate config key: {}".format(key))
            if key == "interrupt_kinds" and isinstance(value, (list, tuple)):
                value = tuple(str(v) for v in value)
            values[key] = value
        return cls(**values)


DEFAULT_CONFIG = GateConfig()


# --------------------------------------------------------------------------
# snapshots and decisions


@dataclass
class MemberSnapshot:
    """What the daemon knows about one member at decision time."""

    name: str
    kind: str
    terminal_id: Optional[str]
    pane_id: Optional[str]
    agent_kind: Optional[str]  # ``agent`` from agent.list
    agent_status: str  # idle|working|blocked|done|unknown
    state_change_seq: int
    launch_pending: bool
    focused: bool
    screen_detection_skipped: bool
    delivery: str
    verified_kind: bool
    stable_since_ms: Optional[float]  # monotonic ms since state_change_seq last changed
    idle_since_ms: Optional[float]
    explain: Optional[Dict[str, Any]] = None  # agent explain --json
    detection_text: Optional[str] = None  # agent read --source detection
    prompt_line: Optional[str] = None
    muted_until: Optional[float] = None
    in_flight: bool = False
    last_nudge_ms: Optional[float] = None
    pane_stuck_until_ms: Optional[float] = None
    # Additive runtime state (defaults keep the scaffold constructor valid).
    fresh_state_change_seq: Optional[int] = None  # from the confirming agent.get
    last_nudge_cursor_seq: Optional[int] = None  # cursor at the last nudge
    follow_up_used: bool = False  # the one immediate follow-up already spent
    focus_hold_since_ms: Optional[float] = None  # first focused hold for this work
    detection_stable_since_ms: Optional[float] = None  # detection hash unchanged since
    dialog_hold_since_ms: Optional[float] = None  # first consecutive dialog hold
    live_name: Optional[str] = None  # ``name`` from agent.list (gate 3: must match the roster name)


@dataclass
class PendingWork:
    seqs: List[int]
    urgent: bool
    cursor_seq: int
    authors: List[str] = field(default_factory=list)
    broadcast: bool = False  # every pending post is addressed to ``all``
    force: bool = False  # ``nudge --force``: skips done_hold and the interval
    active_ms: Optional[float] = None  # target-active age of the oldest post
    interrupt: bool = False  # a teammate's ``post --interrupt`` is among the seqs
    interrupt_ok: bool = False  # the daemon allows it now: kind in ``interrupt_kinds``, sender not in cooldown
    #: The team manager wrote one of these posts. It lifts gate 2's broadcast
    #: hold and nothing else: a manager's plan for the whole team should not
    #: wait a median 42 minutes for everyone to read the board on their own.
    #: Deliberately not routed through ``urgent``, which would also bypass the
    #: done-hold and the per-member interval.
    from_manager: bool = False


@dataclass
class GateDecision:
    deliver: bool
    hold: Optional[str] = None
    detail: Optional[str] = None
    weak_idle: bool = False
    stable_ms_required: int = STABLE_MS_SCREEN
    matched_dialog_line: Optional[str] = None
    action: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.action:
            if self.deliver:
                self.action = ACTION_SEND
            elif self.hold in DROP_REASONS:
                self.action = ACTION_DROP
            else:
                self.action = ACTION_HOLD

    @property
    def reason(self) -> Optional[str]:
        return None if self.deliver else self.hold

    @property
    def toast(self) -> bool:
        return bool(self.details.get("toast"))

    def to_json(self) -> Dict[str, Any]:
        return {
            "action": self.action, "reason": self.reason, "detail": self.detail,
            "weak_idle": self.weak_idle, "stable_ms_required": self.stable_ms_required,
            "matched_dialog_line": self.matched_dialog_line, "details": dict(self.details),
        }


@dataclass
class Decision:
    """The task-shaped result of ``decide``: action, reason, details, and the gate view."""

    action: str
    reason: Optional[str]
    details: Dict[str, Any] = field(default_factory=dict)
    gate: Optional[GateDecision] = None

    @property
    def deliver(self) -> bool:
        return self.action == ACTION_SEND

    def to_json(self) -> Dict[str, Any]:
        return {"action": self.action, "reason": self.reason, "details": dict(self.details)}


@dataclass
class WindowState:
    """What the sample history says about the stable window."""

    state_change_seq: Optional[int]
    agent_status: Optional[str]
    stable_since_ms: Optional[float]
    idle_since_ms: Optional[float]
    samples: int = 0
    reset: bool = False  # a gap over ``sample_gap_reset_ms`` voided the window


@dataclass
class ActiveClock:
    """Post TTL clock that only runs while the target is present (plan 8.3).

    ``mark(now, active)`` records a presence transition; ``elapsed_ms(now)``
    is the accumulated active time. Missing members pause the clock.
    """

    accumulated_ms: float = 0.0
    active_since_ms: Optional[float] = None

    def mark(self, now_ms: float, active: bool) -> "ActiveClock":
        if active and self.active_since_ms is None:
            self.active_since_ms = now_ms
        elif not active and self.active_since_ms is not None:
            self.accumulated_ms += max(0.0, now_ms - self.active_since_ms)
            self.active_since_ms = None
        return self

    def elapsed_ms(self, now_ms: float) -> float:
        running = 0.0 if self.active_since_ms is None else max(0.0, now_ms - self.active_since_ms)
        return self.accumulated_ms + running

    def expired(self, now_ms: float, ttl_ms: int = POST_TTL_MS) -> bool:
        return self.elapsed_ms(now_ms) >= ttl_ms

    def to_json(self) -> Dict[str, Any]:
        return {"accumulated_ms": self.accumulated_ms, "active_since_ms": self.active_since_ms}

    @classmethod
    def from_json(cls, obj: Optional[Mapping[str, Any]]) -> "ActiveClock":
        if not obj:
            return cls()
        return cls(float(obj.get("accumulated_ms") or 0.0), obj.get("active_since_ms"))


@dataclass
class RetryAction:
    action: str
    not_before_ms: Optional[float] = None
    reason: Optional[str] = None
    attempts: int = 0
    detail: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        return {"action": self.action, "not_before_ms": self.not_before_ms, "reason": self.reason, "attempts": self.attempts, "detail": self.detail}


# --------------------------------------------------------------------------
# region extraction (mirrors src/detect/manifest.rs at v0.8.2)


def split_lines(text: Optional[str]) -> List[str]:
    """Rust ``str::lines`` semantics: split on ``\\n``, drop a trailing ``\\r`` and the final empty piece."""
    if not text:
        return []
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    return [p[:-1] if p.endswith("\r") else p for p in parts]


def is_horizontal_rule(line: str) -> bool:
    trimmed = line.strip()
    if not trimmed:
        return False
    count = 0
    for ch in trimmed:
        if ch == "─":
            count += 1
        else:
            break
    if count == 0:
        return False
    suffix = trimmed[count:].lstrip()
    return suffix == "" or count >= 3


def is_codex_prompt_line(line: str) -> bool:
    return line == "›" or line.startswith("› ")


#: Placeholder text Codex 0.153 paints on an empty prompt line ("› Ask Codex to do anything"); it is
#: not a draft. Observed live 2026-09-05 (Herdr 0.8.2): the gate held a fresh idle Codex member with
#: ``draft_present (Ask Codex to do anything)`` and never nudged it.
CODEX_PROMPT_PLACEHOLDERS = ("Ask Codex to do anything",)


def codex_prompt_draft(line: str) -> str:
    """The draft on a Codex prompt line, ``""`` for the bare marker or a known placeholder."""
    if line == "›":
        return ""
    draft = line[2:].strip()
    return "" if draft in CODEX_PROMPT_PLACEHOLDERS else draft


def after_last_horizontal_rule(text: Optional[str]) -> str:
    lines = split_lines(text)
    last_rule = -1
    for index, line in enumerate(lines):
        if is_horizontal_rule(line):
            last_rule = index
    return "\n".join(lines[last_rule + 1:])


def prompt_box_top_border_index(lines: Sequence[str]) -> Optional[int]:
    seen = 0
    for index in range(len(lines) - 1, -1, -1):
        if is_horizontal_rule(lines[index]):
            seen += 1
            if seen == 2:
                return index
    return None


def prompt_box_body(text: Optional[str]) -> Optional[str]:
    """Lines between the second-from-bottom rule and the next rule below it; None without a box."""
    lines = split_lines(text)
    top = prompt_box_top_border_index(lines)
    if top is None:
        return None
    end = len(lines)
    for offset, line in enumerate(lines[top + 1:]):
        if is_horizontal_rule(line):
            end = top + 1 + offset
            break
    return "\n".join(lines[top + 1:end])


def after_last_prompt_marker(text: Optional[str]) -> str:
    lines = split_lines(text)
    for index in range(len(lines) - 1, -1, -1):
        if is_codex_prompt_line(lines[index]):
            return "\n".join(lines[index + 1:])
    return "\n".join(lines)


def bottom_non_empty_lines(text: Optional[str], count: int) -> str:
    lines = split_lines(text)
    start: Optional[int] = None
    seen = 0
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip():
            seen += 1
            start = index
            if seen >= count:
                break
    if start is None:
        return ""
    return "\n".join(lines[start:])


def dialog_regions(text: Optional[str], kind: str) -> List[Tuple[str, str]]:
    """The (name, text) regions the dialog heuristic may inspect for ``kind``."""
    if text is None:
        return []
    if kind == "claude":
        regions = [("after_last_horizontal_rule", after_last_horizontal_rule(text))]
        body = prompt_box_body(text)
        if body is not None:
            regions.append(("prompt_box_body", body))
        return regions
    if kind == "codex":
        return [("after_last_prompt_marker", after_last_prompt_marker(text))]
    return [("bottom_non_empty_lines({})".format(GENERIC_DIALOG_LINES), bottom_non_empty_lines(text, GENERIC_DIALOG_LINES))]


def dialog_line(detection_text: Optional[str], kind: str) -> Optional[str]:
    """The first detection-region line carrying a strong dialog marker, or None."""
    for _name, region in dialog_regions(detection_text, kind):
        lines = split_lines(region)
        for line in lines:
            lowered = line.lower()
            for marker in DIALOG_MARKERS:
                if marker in lowered:
                    return line.strip()
        if kind == "codex":
            lowered_region = region.lower()
            if all(marker in lowered_region for marker in CODEX_VIEWER_MARKERS):
                for line in lines:
                    if CODEX_VIEWER_MARKERS[1] in line.lower():
                        return line.strip()
    return None


def prompt_line_text(detection_text: Optional[str], kind: str) -> Optional[str]:
    """The draft on the prompt line, ``""`` when empty, None when no prompt line is visible."""
    if detection_text is None:
        return None
    if kind == "claude":
        body = prompt_box_body(detection_text)
        if body is not None:
            lines = split_lines(body)
            for index, line in enumerate(lines):
                if _CLAUDE_PROMPT_RE.match(line):
                    first = _CLAUDE_PROMPT_SPLIT_RE.sub("", line, count=1)
                    rest = [l for l in lines[index + 1:] if l.strip()]
                    return "\n".join([first] + rest).strip()
            return None
        lines = split_lines(detection_text)
        for index in range(len(lines) - 1, -1, -1):
            if _CLAUDE_PROMPT_RE.match(lines[index]):
                return _CLAUDE_PROMPT_SPLIT_RE.sub("", lines[index], count=1).strip()
        return None
    if kind == "codex":
        lines = split_lines(detection_text)
        for index in range(len(lines) - 1, -1, -1):
            if is_codex_prompt_line(lines[index]):
                return codex_prompt_draft(lines[index])
        return None
    for line in reversed(split_lines(detection_text)):
        if not line.strip():
            continue
        stripped = line.lstrip()
        for marker in _GENERIC_PROMPT_MARKERS:
            if stripped.startswith(marker):
                return stripped[len(marker):].strip()
        return None
    return None


def prompt_line_empty(detection_text: Optional[str], kind: str) -> bool:
    """True unless a draft is visible on the prompt line (unknown counts as empty)."""
    draft = prompt_line_text(detection_text, kind)
    return not (draft or "").strip()


def detection_hash(detection_text: Optional[str]) -> str:
    """Stable digest of a detection read, trailing whitespace per line ignored."""
    normalized = "\n".join(line.rstrip() for line in split_lines(detection_text))
    return hashlib.sha1(normalized.encode("utf-8", "replace")).hexdigest()


# --------------------------------------------------------------------------
# explain and stable window


def is_weak_idle(explain: Optional[Dict[str, Any]]) -> bool:
    """``matched_rule`` null or an ``osc_*`` region; only prompt-box rules are strong."""
    if not explain:
        return True
    rule = explain.get("matched_rule")
    if not isinstance(rule, dict):
        return True
    region = str(rule.get("region") or "")
    return region == "osc_title" or region.startswith("osc_")


def stable_ms_for(snapshot: MemberSnapshot, weak_idle: bool, config: Optional[GateConfig] = None) -> int:
    cfg = config or DEFAULT_CONFIG
    if snapshot.delivery == "hooks":
        return cfg.stable_ms_hooks_delivery
    if snapshot.screen_detection_skipped:
        return cfg.stable_ms_hook
    base = cfg.stable_ms_screen
    return base * 2 if weak_idle else base


def window_from_samples(samples: Iterable[Tuple[float, int, str]], now_ms: float, gap_reset_ms: int = SAMPLE_GAP_RESET_MS) -> WindowState:
    """Stable and idle windows from ``(monotonic_ms, state_change_seq, agent_status)`` samples.

    A gap over ``gap_reset_ms`` between consecutive samples, or between the
    newest sample and ``now_ms``, resets every window (plan 8.1).
    """
    ordered = sorted((float(ts), int(seq), str(status)) for ts, seq, status in samples)
    if not ordered:
        return WindowState(None, None, None, None, 0)
    newest_ts, newest_seq, newest_status = ordered[-1]
    if now_ms - newest_ts > gap_reset_ms:
        return WindowState(newest_seq, newest_status, None, None, len(ordered), reset=True)
    stable_since = newest_ts
    idle_since: Optional[float] = newest_ts if newest_status in IDLE_STATUSES else None
    stable_open = True
    idle_open = idle_since is not None
    previous_ts = newest_ts
    for ts, seq, status in reversed(ordered[:-1]):
        if previous_ts - ts > gap_reset_ms:
            break
        if stable_open and seq == newest_seq:
            stable_since = ts
        else:
            stable_open = False
        if idle_open and status in IDLE_STATUSES:
            idle_since = ts
        else:
            idle_open = False
        if not stable_open and not idle_open:
            break
        previous_ts = ts
    return WindowState(newest_seq, newest_status, stable_since, idle_since, len(ordered))


# --------------------------------------------------------------------------
# the gate


def evaluate(snapshot: MemberSnapshot, pending: PendingWork, now_ms: float, global_last_nudge_ms: Optional[float], pair_exchanges: int, config: Optional[GateConfig] = None) -> GateDecision:
    """Gates 1 to 11 in order; the first failing gate names the hold."""
    cfg = config or DEFAULT_CONFIG
    weak = is_weak_idle(snapshot.explain)
    required = stable_ms_for(snapshot, weak, cfg)
    details: Dict[str, Any] = {"gate": None}
    bypass_holds = bool(pending.urgent or pending.force)

    def hold(gate: int, reason: str, detail: Optional[str] = None, line: Optional[str] = None, **extra: Any) -> GateDecision:
        info = dict(details)
        info["gate"] = gate
        info.update(extra)
        if reason in TOAST_HOLDS:
            info["toast"] = True
        return GateDecision(False, reason, detail, weak, required, line, details=info)

    # 1. cursor re-read
    if not pending.seqs:
        return hold(1, HOLD_NOTHING_PENDING)
    top = max(int(s) for s in pending.seqs)
    if pending.cursor_seq >= top:
        return hold(1, HOLD_READ_BEFORE_NUDGE, "cursor {} >= {}".format(pending.cursor_seq, top))
    if pending.active_ms is not None and pending.active_ms >= cfg.post_ttl_ms:
        return hold(1, HOLD_EXPIRED, "active {:.0f} ms >= ttl {} ms".format(pending.active_ms, cfg.post_ttl_ms))

    # 2. not self, not console, not human; needs a terminal; broadcasts only when
    #    urgent or from the team manager
    if pending.authors and all(author == snapshot.name for author in pending.authors):
        return hold(2, HOLD_SELF)
    if snapshot.kind == "human" or not snapshot.terminal_id:
        return hold(2, HOLD_NO_TERMINAL)
    if pending.broadcast and not (pending.urgent or pending.from_manager):
        return hold(2, HOLD_BROADCAST)
    if snapshot.muted_until is not None and now_ms < snapshot.muted_until:
        return hold(2, HOLD_MUTED, "{:.0f} ms left".format(snapshot.muted_until - now_ms))

    # 3. present in agent.list with the roster kind, launch settled
    if snapshot.agent_kind is None:
        return hold(3, HOLD_ABSENT)
    if snapshot.agent_kind != snapshot.kind:
        return hold(3, HOLD_KIND_MISMATCH, "agent {} != roster {}".format(snapshot.agent_kind, snapshot.kind))
    if snapshot.live_name is not None and snapshot.live_name != snapshot.name:
        # A hand rename not yet adopted: the daemon reconciles first, then nudges under the new name.
        return hold(3, HOLD_NAME_MISMATCH, "agent {!r} != roster {!r}".format(snapshot.live_name, snapshot.name))
    if snapshot.launch_pending:
        return hold(3, HOLD_LAUNCH_PENDING)

    # 4. kind verified in kinds.json
    if not snapshot.verified_kind:
        return hold(4, HOLD_KIND_UNVERIFIED)

    # An allowed interrupt (a teammate's ``post --interrupt``, kind in ``interrupt_kinds``, sender out of
    # cooldown) skips the two idle gates while the member is working: the line goes into the running turn.
    # Every later gate still applies: dialog, overlay, draft, focus policy, and the rate limits.
    in_turn = bool(pending.interrupt and pending.interrupt_ok and snapshot.agent_status == "working")
    if in_turn:
        details["interrupt"] = True
    else:
        # 5. idle or done
        if snapshot.agent_status not in IDLE_STATUSES:
            return hold(5, HOLD_NOT_IDLE, snapshot.agent_status)

        # 6. stable window, fresh-get confirmation, done_hold
        if snapshot.stable_since_ms is None:
            return hold(6, HOLD_UNSTABLE, "no stable window")
        elapsed = now_ms - snapshot.stable_since_ms
        if elapsed < required:
            base = stable_ms_for(snapshot, False, cfg)
            reason = HOLD_WEAK_IDLE if (weak and elapsed >= base) else HOLD_UNSTABLE
            return hold(6, reason, "{:.0f}/{} ms".format(elapsed, required), stable_elapsed_ms=elapsed)
        if snapshot.fresh_state_change_seq is not None and snapshot.fresh_state_change_seq != snapshot.state_change_seq:
            return hold(6, HOLD_UNSTABLE, "fresh agent.get seq {} != {}".format(snapshot.fresh_state_change_seq, snapshot.state_change_seq))
        if not bypass_holds and snapshot.idle_since_ms is not None:
            idle_for = now_ms - snapshot.idle_since_ms
            if idle_for < cfg.done_hold_ms:
                return hold(6, HOLD_DONE_HOLD, "{:.0f}/{} ms".format(idle_for, cfg.done_hold_ms))

    # 7. explain
    explain = snapshot.explain or {}
    if explain:
        if explain.get("state") == "blocked":
            return hold(7, HOLD_BLOCKED, _rule_id(explain))
        if explain.get("visible_blocker"):
            return hold(7, HOLD_VISIBLE_BLOCKER, _rule_id(explain))
        skipped_reason = explain.get("skipped_update_reason")
        if explain.get("skip_state_update") or skipped_reason:
            return hold(7, HOLD_SKIP_STATE_UPDATE, str(skipped_reason or _rule_id(explain) or "skip_state_update"))

    # 8. dialog heuristic on the detection regions
    line = dialog_line(snapshot.detection_text, snapshot.kind)
    if line is not None:
        capped = snapshot.dialog_hold_since_ms is not None and now_ms - snapshot.dialog_hold_since_ms >= cfg.dialog_hold_cap_ms
        return hold(8, HOLD_DIALOG, line, line=line, dialog_hold_capped=capped, toast=capped)

    # 9. prompt line empty
    # The daemon sets ``prompt_line`` from the styled visible read when the plain detection text shows a
    # draft (``styled_prompt_line_text``), so it wins when present; the detection text is the fallback.
    draft: Optional[str]
    if snapshot.prompt_line is not None:
        draft = snapshot.prompt_line
    elif snapshot.detection_text is not None:
        draft = prompt_line_text(snapshot.detection_text, snapshot.kind)
    else:
        draft = None
    if draft is not None and draft.strip():
        preview = draft.strip().splitlines()[0][:40]
        return hold(9, HOLD_DRAFT_PRESENT, preview)
    details["prompt_line"] = "empty" if draft is not None else "unknown"

    # 10. focus policy
    if snapshot.focused and cfg.nudge_focused == "never":
        since = snapshot.focus_hold_since_ms
        if since is None or now_ms - since < cfg.focus_max_hold_ms:
            held = 0.0 if since is None else now_ms - since
            return hold(10, HOLD_FOCUSED, "max hold {:.0f}/{} ms".format(held, cfg.focus_max_hold_ms))
        stable = snapshot.detection_stable_since_ms
        if stable is None or now_ms - stable < cfg.focus_snapshot_stable_ms:
            return hold(10, HOLD_FOCUSED, "snapshot changing", focus_max_hold_elapsed=True)
        details["focused_after_max_hold"] = True

    # 11. rate limits
    if snapshot.pane_stuck_until_ms is not None and now_ms < snapshot.pane_stuck_until_ms:
        return hold(11, HOLD_PANE_STUCK, "{:.0f} ms left".format(snapshot.pane_stuck_until_ms - now_ms))
    if snapshot.in_flight:
        return hold(11, HOLD_IN_FLIGHT)
    if snapshot.last_nudge_ms is not None and not bypass_holds:
        since_last = now_ms - snapshot.last_nudge_ms
        if since_last < cfg.min_interval_ms:
            follow_up = (
                snapshot.last_nudge_cursor_seq is not None
                and pending.cursor_seq > snapshot.last_nudge_cursor_seq
                and not snapshot.follow_up_used
            )
            if not follow_up:
                return hold(11, HOLD_INTERVAL, "{:.0f}/{} ms".format(since_last, cfg.min_interval_ms))
            details["follow_up"] = True
    if global_last_nudge_ms is not None and now_ms - global_last_nudge_ms < cfg.global_interval_ms:
        return hold(11, HOLD_GLOBAL_INTERVAL, "{:.0f}/{} ms".format(now_ms - global_last_nudge_ms, cfg.global_interval_ms))
    if pair_exchanges >= cfg.pair_budget:
        return hold(11, HOLD_PAIR_BUDGET, "{} exchanges in {} ms".format(pair_exchanges, cfg.pair_window_ms))

    details["gate"] = 11
    details["bypass"] = bypass_holds
    return GateDecision(True, None, None, weak, required, None, details=details)


def _rule_id(explain: Dict[str, Any]) -> Optional[str]:
    rule = explain.get("matched_rule")
    if isinstance(rule, dict):
        value = rule.get("id")
        return str(value) if value is not None else None
    return None


# --------------------------------------------------------------------------
# daemon-facing wrapper


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def pending_from_posts(posts: Iterable[Mapping[str, Any]], member_name: str, cursor_seq: int, force: bool = False, active_ms: Optional[float] = None) -> PendingWork:
    """Collapse board records addressed to ``member_name`` into ``PendingWork``.

    Only records with ``seq > cursor_seq`` count. ``broadcast`` is True when
    none of them names the member directly (all went to ``all``).
    """
    seqs: List[int] = []
    authors: List[str] = []
    urgent = False
    directed = False
    for post in posts:
        seq = post.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= cursor_seq:
            continue
        seqs.append(seq)
        authors.append(str(post.get("from") or ""))
        urgent = urgent or bool(post.get("urgent"))
        recipients = post.get("to") or []
        if isinstance(recipients, str):
            recipients = [recipients]
        if member_name in recipients:
            directed = True
    seqs.sort()
    return PendingWork(seqs, urgent, cursor_seq, authors, broadcast=bool(seqs) and not directed, force=force, active_ms=active_ms)


def snapshot_from(member: Any, agent_row: Optional[Mapping[str, Any]], explain: Optional[Dict[str, Any]], detection_text: Optional[str], samples: Optional[Iterable[Tuple[float, int, str]]], now_ms: float, config: Optional[GateConfig] = None, **state: Any) -> MemberSnapshot:
    """Build a ``MemberSnapshot`` from roster, ``agent.list`` row, explain, detection text, and samples.

    ``state`` carries the daemon's per-member runtime fields (``in_flight``,
    ``last_nudge_ms``, ``muted_until``, ``pane_stuck_until_ms``,
    ``focus_hold_since_ms``, ``detection_stable_since_ms``,
    ``dialog_hold_since_ms``, ``last_nudge_cursor_seq``, ``follow_up_used``,
    ``fresh_state_change_seq``, ``verified_kind``).
    """
    cfg = config or DEFAULT_CONFIG
    member_terminal = _get(member, "terminal_id")
    window = window_from_samples(samples or [], now_ms, cfg.sample_gap_reset_ms)
    agent_kind: Optional[str] = None
    status = "unknown"
    seq = window.state_change_seq if window.state_change_seq is not None else 0
    launch_pending = False
    focused = False
    skipped = False
    live_name: Optional[str] = None
    if agent_row is not None:
        row_terminal = agent_row.get("terminal_id")
        if member_terminal and row_terminal and row_terminal != member_terminal:
            agent_kind = None
        else:
            agent_kind = agent_row.get("agent")
            status = str(agent_row.get("agent_status") or "unknown")
            seq = int(agent_row.get("state_change_seq") or 0)
            launch_pending = bool(agent_row.get("launch_pending", False))
            focused = bool(agent_row.get("focused", False))
            skipped = bool(agent_row.get("screen_detection_skipped", False))
            live_name = agent_row.get("name") if isinstance(agent_row.get("name"), str) else None
    stable_since = window.stable_since_ms
    if stable_since is not None and window.state_change_seq is not None and agent_row is not None and window.state_change_seq != seq:
        stable_since = None  # the row moved past the sampled seq: no window yet
    verified = state.pop("verified_kind", None)
    if verified is None:
        verified = bool(_get(member, "verified_kind", False))
    snapshot = MemberSnapshot(
        name=str(_get(member, "name")),
        kind=str(_get(member, "kind")),
        terminal_id=member_terminal,
        pane_id=_get(member, "pane_id"),
        agent_kind=agent_kind,
        agent_status=status,
        state_change_seq=seq,
        launch_pending=launch_pending,
        focused=focused,
        screen_detection_skipped=skipped,
        delivery=str(_get(member, "delivery", "nudge") or "nudge"),
        verified_kind=bool(verified),
        stable_since_ms=stable_since,
        idle_since_ms=window.idle_since_ms,
        explain=explain,
        detection_text=detection_text,
        live_name=live_name,
    )
    known = {f.name for f in fields(MemberSnapshot)}
    for key, value in state.items():
        if key not in known:
            raise ValueError("unknown snapshot state key: {}".format(key))
        setattr(snapshot, key, value)
    return snapshot


def decide(member: Any, pending_posts: Iterable[Mapping[str, Any]], cursor_seq: int, agent_row: Optional[Mapping[str, Any]], explain: Optional[Dict[str, Any]], detection_text: Optional[str], samples: Optional[Iterable[Tuple[float, int, str]]], config: Optional[Mapping[str, Any]], now: float, global_last_nudge_ms: Optional[float] = None, pair_exchanges: int = 0, force: bool = False, active_ms: Optional[float] = None, **state: Any) -> Decision:
    """The task-shaped evaluator: roster member + posts + live facts -> ``Decision``."""
    cfg = GateConfig.from_mapping(config)
    member_name = str(_get(member, "name"))
    pending = pending_from_posts(pending_posts, member_name, cursor_seq, force=force, active_ms=active_ms)
    snapshot = snapshot_from(member, agent_row, explain, detection_text, samples, now, cfg, **state)
    gate = evaluate(snapshot, pending, now, global_last_nudge_ms, pair_exchanges, cfg)
    details = dict(gate.details)
    details["seqs"] = list(pending.seqs)
    details["urgent"] = pending.urgent
    details["weak_idle"] = gate.weak_idle
    details["stable_ms_required"] = gate.stable_ms_required
    if gate.detail is not None:
        details["detail"] = gate.detail
    if agent_row is not None and agent_row.get("name") not in (None, member_name):
        details["name_mismatch"] = agent_row.get("name")
    return Decision(gate.action, gate.reason, details, gate)


# --------------------------------------------------------------------------
# post-send classification and the retry table


def classify_result(gate_snapshot: MemberSnapshot, prompt_response: Optional[Mapping[str, Any]], post_send_text: Optional[str] = None, post_send_agent: Optional[Mapping[str, Any]] = None, waited: bool = False) -> str:
    """Name what an ``agent.prompt`` attempt did (plan 8.3).

    ``prompt_response`` is the raw envelope: ``{"result": {"agent": {...}}}``
    or ``{"error": {"code", "message"}}`` (a bare result object or a bare
    ``agent`` object is accepted too); None means the client timed out.
    ``waited`` says the request carried ``wait``, in which case the
    ``agent`` object is the post-wait state; otherwise it is the send-time
    snapshot and ``post_send_agent`` (a later ``agent.get``) settles the
    outcome. ``post_send_text`` is the detection read 1.5 s after a stall.
    """
    if prompt_response is None:
        return RESULT_HUNG
    error = prompt_response.get("error")
    if isinstance(error, Mapping):
        return _classify_error(gate_snapshot, str(error.get("code") or ""), str(error.get("message") or ""), post_send_text, post_send_agent, waited)
    agent = _agent_from_envelope(prompt_response)
    if agent is None:
        return RESULT_UNKNOWN_ERROR
    if not _identity_matches(gate_snapshot, agent):
        return RESULT_WRONG_OCCUPANT
    status = str(agent.get("agent_status") or "unknown")
    seq = int(agent.get("state_change_seq") or 0)
    if waited:
        if status == "working":
            return RESULT_LANDED_WORKING
        if status == "blocked":
            return RESULT_LANDED_BLOCKED
        return RESULT_STALLED
    if status not in IDLE_STATUSES or seq != gate_snapshot.state_change_seq:
        return RESULT_LANDED_IN_TURN
    if post_send_agent is not None:
        if not _identity_matches(gate_snapshot, post_send_agent):
            return RESULT_WRONG_OCCUPANT
        later_status = str(post_send_agent.get("agent_status") or "unknown")
        later_seq = int(post_send_agent.get("state_change_seq") or 0)
        if later_status == "working":
            return RESULT_LANDED_WORKING
        if later_status == "blocked":
            return RESULT_LANDED_BLOCKED
        if later_seq > gate_snapshot.state_change_seq:
            return RESULT_LANDED_WORKING  # a fast turn already completed
        if post_send_text is not None and not prompt_line_empty(post_send_text, gate_snapshot.kind):
            return RESULT_NOT_SUBMITTED
        return RESULT_STALLED
    if post_send_text is not None and not prompt_line_empty(post_send_text, gate_snapshot.kind):
        return RESULT_NOT_SUBMITTED
    return RESULT_SENT


def _classify_error(snapshot: MemberSnapshot, code: str, message: str, post_send_text: Optional[str], post_send_agent: Optional[Mapping[str, Any]], waited: bool) -> str:
    lowered = message.lower()
    if code in ("herdr_timeout", "server_not_running", "herdr_protocol", "herdr_unreachable"):
        return RESULT_HUNG
    if code == "agent_prompt_stalled":
        if post_send_text is not None and not prompt_line_empty(post_send_text, snapshot.kind):
            return RESULT_NOT_SUBMITTED
        if post_send_agent is not None and str(post_send_agent.get("agent_status")) in ("working", "blocked"):
            return RESULT_LANDED_WORKING if post_send_agent.get("agent_status") == "working" else RESULT_LANDED_BLOCKED
        return RESULT_STALLED
    if code == "timeout":
        if post_send_text is not None and not prompt_line_empty(post_send_text, snapshot.kind):
            return RESULT_NOT_SUBMITTED
        # With ``wait`` the effect wait is skipped only when the send-time
        # status was already working, so an until-wait timeout means the
        # text landed inside a running turn.
        return RESULT_LANDED_IN_TURN if waited else RESULT_STALLED
    if code in ("agent_not_found", "agent_not_running", "agent_target_ambiguous"):
        return RESULT_RE_RESOLVE
    if code == "agent_not_ready":
        return RESULT_BUSY if "foreground" in lowered else RESULT_TRANSIENT
    if code == "agent_prompt_failed":
        if "full" in lowered:
            return RESULT_PANE_STUCK
        return RESULT_TRANSIENT
    if code in ("agent_blocked", "empty_agent_prompt"):
        return RESULT_REFUSED
    return RESULT_UNKNOWN_ERROR


def _agent_from_envelope(envelope: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    result = envelope.get("result")
    if isinstance(result, Mapping):
        agent = result.get("agent")
        return agent if isinstance(agent, Mapping) else None
    agent = envelope.get("agent")
    if isinstance(agent, Mapping):
        return agent
    if "terminal_id" in envelope and "agent_status" in envelope:
        return envelope
    return None


def _identity_matches(snapshot: MemberSnapshot, agent: Mapping[str, Any]) -> bool:
    terminal = agent.get("terminal_id")
    if snapshot.terminal_id and terminal and terminal != snapshot.terminal_id:
        return False
    kind = agent.get("agent")
    if kind is not None and kind != snapshot.kind:
        return False
    return True


def _entry(item: Any, key: str, default: Any = None) -> Any:
    return _get(item, key, default)


def next_action(history: Sequence[Any], now_ms: float, turn_completed_since: bool = False, gate_passes: bool = True) -> RetryAction:
    """The retry table of plan 8.3 as a pure function of the attempt history.

    ``history`` entries are mappings (or objects) with ``ts_ms`` and
    ``result`` (a ``RESULT_*`` value) plus optional ``cursor_advanced``.
    ``turn_completed_since`` says the target completed a turn since the last
    landing, a precondition for re-nudging. Hold decisions never enter the
    history; the daemon re-evaluates them on the next event, at most every
    ``HOLD_REEVAL_MS``.
    """
    entries = [h for h in history if _entry(h, "result") is not None]
    if not entries:
        return RetryAction(RETRY_RETRY, None, "first_attempt", 0)
    attempts = len(entries)
    last = entries[-1]
    last_result = str(_entry(last, "result"))
    last_ts = float(_entry(last, "ts_ms", now_ms))
    if any(_entry(h, "cursor_advanced") for h in entries):
        return RetryAction(RETRY_DONE, None, "read", attempts)

    if last_result in LANDED_RESULTS:
        landed = [h for h in entries if str(_entry(h, "result")) in LANDED_RESULTS]
        if len(landed) >= MAX_LANDED_ATTEMPTS:
            return RetryAction(RETRY_ABANDON, None, "unread_after_{}_attempts".format(len(landed)), attempts)
        first_landed_ts = float(_entry(landed[0], "ts_ms", last_ts))
        due = first_landed_ts + RENUDGE_SCHEDULE_MS[len(landed) - 1]
        if now_ms < due:
            return RetryAction(RETRY_WAIT, due, "renudge_schedule", attempts, "{:.0f} ms".format(due - now_ms))
        if not turn_completed_since:
            return RetryAction(RETRY_WAIT, None, "turn_not_completed", attempts)
        if not gate_passes:
            return RetryAction(RETRY_HOLD, now_ms + HOLD_REEVAL_MS, "gate", attempts)
        return RetryAction(RETRY_RENUDGE, None, "unread", attempts)

    if last_result == RESULT_LANDED_IN_TURN:
        return RetryAction(RETRY_RETRY, None, RESULT_LANDED_IN_TURN, attempts, "text lost inside a turn; the stable window paces the retry")
    if last_result in (RESULT_TRANSIENT, RESULT_BUSY):
        streak = 0
        for item in reversed(entries):
            if str(_entry(item, "result")) in (RESULT_TRANSIENT, RESULT_BUSY):
                streak += 1
            else:
                break
        delay = min(TRANSIENT_BACKOFF_MIN_MS * (2 ** (streak - 1)), TRANSIENT_BACKOFF_MAX_MS)
        return RetryAction(RETRY_WAIT, last_ts + delay, last_result, attempts, "backoff {} ms".format(delay))
    if last_result in (RESULT_HUNG, RESULT_PANE_STUCK):
        return RetryAction(RETRY_WAIT, last_ts + PANE_STUCK_MS, RESULT_PANE_STUCK, attempts)
    if last_result in (RESULT_RE_RESOLVE, RESULT_WRONG_OCCUPANT):
        return RetryAction(RETRY_RE_RESOLVE, None, last_result, attempts)
    if last_result == RESULT_NOT_SUBMITTED:
        return RetryAction(RETRY_HOLD, last_ts + NOT_SUBMITTED_HOLD_MS, RESULT_NOT_SUBMITTED, attempts, "kind needs a submit profile; toast the human")
    if last_result == RESULT_REFUSED:
        return RetryAction(RETRY_HOLD, last_ts + HOLD_REEVAL_MS, RESULT_REFUSED, attempts)
    return RetryAction(RETRY_HOLD, last_ts + HOLD_REEVAL_MS, RESULT_UNKNOWN_ERROR, attempts, "held; toast once")
