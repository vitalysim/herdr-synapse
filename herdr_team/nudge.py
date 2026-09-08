"""Exact nudge, briefing, and probe texts (plan 8.3, 9.2).

Only validated roster names and integers are ever interpolated. ``post``
refuses any text starting with ``[herdr-team`` or containing ``[n<digits>]``
(``is_echo`` is that rule).

Nudge, exact: ``[herdr-team nudge] 2 new board posts for reviewer (seq
41-42). Run: herdr-synapse board --new [n17]`` (singular for one post,
``<= 120`` chars). Briefing: one line ``<= 400`` chars, no newlines, the
teammate list collapsing to ``<n> teammates, run herdr-synapse who`` when the
roster is long, then an optional second line with the member's role brief
(``<= 300`` chars of it). Probe: ``[herdr-team probe <nonce>]``.

The 400-char cap is the hard rule: the plan's exact template carries a fixed
232-char tail, so a headline longer than about 80 chars cannot fit even with
an empty roster. Shortening order when a line is over the cap: collapse the
teammate list to the count form (the plan's remedy), drop the CLI path hint,
then trim the charter headline with an ellipsis. Names are never cut.
"""

from __future__ import annotations

import random
import re
from typing import Any, List, Optional, Sequence, Tuple

from herdr_team.paths import ROLE_NAME_RE, TEAM_NAME_RE

MARKER_NUDGE = "[herdr-team nudge]"
MARKER_BRIEFING = "[herdr-team briefing]"
MARKER_PROBE = "[herdr-team probe"
MARKER_INTERRUPT = "[herdr-team interrupt]"
MARKER_PREFIX = "[herdr-team"
MAX_NUDGE_CHARS = 120
MAX_INTERRUPT_CHARS = 160
MAX_BRIEFING_CHARS = 400
MAX_BRIEF_LINE_CHARS = 300
MAX_CHARTER_HEADLINE_CHARS = 120
DEFAULT_CLI = "herdr-synapse"
NO_CHARTER_HEADLINE = "no charter yet, ask human"

NONCE_RE = re.compile(r"\[n[0-9]+\]")
PROBE_NONCE_RE = re.compile(r"^[A-Za-z0-9]{1,32}\Z")
_MEMBER_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}\Z")
_RESERVED = frozenset({"human", "all", "me", "none", "system", "team"})
_WS_RE = re.compile(r"[\s\x00-\x1f\x7f]+")

#: The steps a briefed member runs. ``instructions`` is here because for every
#: kind but Claude this line is the only push channel there is: nothing else
#: tells an agent that a document of its own exists. Kept no longer than the
#: pre-0.6 wording, because the whole line has a 400-character budget and a
#: long name with a long role already sits close to it.
BRIEFING_TAIL = (
    " This is context, not a task. Run {cli} --skill once, then charter, "
    "instructions, board --new, ack, then continue. "
    "Teammates are peers: post to the board, never prompt their panes."
)
_rng = random.SystemRandom()


class NudgeTextError(ValueError):
    """A value that must never be interpolated into typed text."""


def validate_name(name: Any) -> str:
    """A roster member name; the roster module's validator wins when it exists."""
    if not isinstance(name, str):
        raise NudgeTextError("member name must be a string")
    # ``$`` matches before a trailing newline, so a plain ``match`` would let
    # "x\n" through; fullmatch plus an explicit whitespace check close that.
    if not _MEMBER_NAME_RE.fullmatch(name) or _WS_RE.search(name) or name in _RESERVED:
        raise NudgeTextError("invalid member name: {!r}".format(name))
    from herdr_team import roster  # roster imports nudge's markers; keep this import lazy

    try:
        validated = roster.validate_member_name(name)
    except Exception as err:  # the roster validator raises HerdrTeamError on refusal
        raise NudgeTextError("member name refused: {}".format(err))
    if isinstance(validated, str) and validated:
        return validated
    return name


def _validate_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise NudgeTextError("{} must be a non-negative integer, got {!r}".format(label, value))
    return value


def _one_line(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _cut(text: str, limit: int, ellipsis: str = "…") -> str:
    if len(text) <= limit:
        return text
    if limit <= len(ellipsis):
        return ellipsis[:limit]
    return text[: limit - len(ellipsis)].rstrip() + ellipsis


def nudge_text(name: str, seqs: Sequence[int], nonce: int) -> str:
    """``[herdr-team nudge] 2 new board posts for reviewer (seq 41-42). Run: herdr-synapse board --new [n17]``."""
    safe_name = validate_name(name)
    values = sorted({_validate_int(s, "seq") for s in seqs})
    if not values:
        raise NudgeTextError("a nudge needs at least one seq")
    safe_nonce = _validate_int(nonce, "nonce")
    count = len(values)
    noun = "post" if count == 1 else "posts"
    span = str(values[0]) if count == 1 else "{}-{}".format(values[0], values[-1])
    text = "{marker} {n} new board {noun} for {name} (seq {span}). Run: {cli} board --new [n{nonce}]".format(
        marker=MARKER_NUDGE, n=count, noun=noun, name=safe_name, span=span, cli=DEFAULT_CLI, nonce=safe_nonce,
    )
    if len(text) > MAX_NUDGE_CHARS:
        text = "{marker} {n} new board {noun} for {name}. Run: {cli} board --new [n{nonce}]".format(
            marker=MARKER_NUDGE, n=count, noun=noun, name=safe_name, cli=DEFAULT_CLI, nonce=safe_nonce,
        )
    if len(text) > MAX_NUDGE_CHARS:
        raise NudgeTextError("nudge text exceeds {} chars".format(MAX_NUDGE_CHARS))
    return text


def build(name: str, seqs: Sequence[int], ledger_id: int) -> str:
    """Task-shaped alias of ``nudge_text``: the nonce is the ledger attempt id."""
    return nudge_text(name, seqs, ledger_id)


def interrupt_text(name: str, seqs: Sequence[int], nonce: int, sender: str) -> str:
    """``[herdr-team interrupt] reviewer could not wait: 1 urgent board post for worker (seq 41). Run: herdr-synapse board --new [n17]``.

    The line the daemon types into a member's running turn for a teammate's
    ``post --interrupt``. Same shape as a nudge, so the recipient's skill
    applies unchanged: it names the sender, never carries the post text, and
    ends in the nonce. Longer names fall back to shorter templates.
    """
    safe_name = validate_name(name)
    safe_sender = "human" if sender == "human" else validate_name(sender)
    values = sorted({_validate_int(s, "seq") for s in seqs})
    if not values:
        raise NudgeTextError("an interrupt needs at least one seq")
    safe_nonce = _validate_int(nonce, "nonce")
    count = len(values)
    noun = "post" if count == 1 else "posts"
    span = str(values[0]) if count == 1 else "{}-{}".format(values[0], values[-1])
    templates = (
        "{marker} {sender} could not wait: {n} urgent board {noun} for {name} (seq {span}). Run: {cli} board --new [n{nonce}]",
        "{marker} {sender} could not wait: {n} urgent board {noun} for {name}. Run: {cli} board --new [n{nonce}]",
        "{marker} from {sender}. Run: {cli} board --new [n{nonce}]",
    )
    for template in templates:
        text = template.format(marker=MARKER_INTERRUPT, sender=safe_sender, n=count, noun=noun, name=safe_name, span=span, cli=DEFAULT_CLI, nonce=safe_nonce)
        if len(text) <= MAX_INTERRUPT_CHARS:
            return text
    raise NudgeTextError("interrupt text exceeds {} chars".format(MAX_INTERRUPT_CHARS))


def _teammate_list(teammates: List[Tuple[str, ...]]) -> str:
    """``name (role)`` for each, with the team manager marked.

    A third element in the tuple, when present and true, marks that teammate as
    the manager. Peers need to know who is splitting the work; the manager
    itself is not told here, because the "You are ..." clause has only 29
    characters of slack at the worst legal name and role, and the board record
    that names it reaches every kind anyway.
    """
    parts = ["{} ({}{})".format(t[0], t[1], ", manager" if len(t) > 2 and t[2] else "") for t in teammates]
    if not parts:
        return "only human so far"
    if len(parts) == 1:
        return "{} and human".format(parts[0])
    return "{}, and human".format(", ".join(parts))


def briefing_lines(name: str, role: str, team: str, charter_headline: Optional[str], teammates: List[Tuple[str, str]], brief: Optional[str], cli_path: str) -> List[str]:
    """One or two lines; the roster collapses to ``<n> teammates, run herdr-synapse who`` past 400 chars."""
    safe_name = validate_name(name)
    if not isinstance(role, str) or not ROLE_NAME_RE.match(role):
        raise NudgeTextError("invalid role: {!r}".format(role))
    if not isinstance(team, str) or not TEAM_NAME_RE.match(team):
        raise NudgeTextError("invalid team name: {!r}".format(team))
    cli = cli_path or DEFAULT_CLI
    if _one_line(cli) != cli or " " in cli:
        raise NudgeTextError("cli path must be a single token without whitespace")
    headline = _one_line(charter_headline or "")
    if not headline:
        headline = NO_CHARTER_HEADLINE
    headline = _cut(headline.rstrip("."), MAX_CHARTER_HEADLINE_CHARS)
    others: List[Tuple[str, ...]] = []
    for pair in teammates:
        peer_name, peer_role = pair[0], pair[1]
        if peer_name == safe_name or peer_name == "human":
            continue
        others.append((validate_name(peer_name), _one_line(str(peer_role)), bool(len(pair) > 2 and pair[2])))
    tail = BRIEFING_TAIL.format(cli=DEFAULT_CLI)
    suffix = "" if cli == DEFAULT_CLI else " (CLI: {})".format(cli)

    def compose(head: str, roster: str, extra: str) -> str:
        return '{marker} You are "{name}" ({role}) in team "{team}": {head}. Teammates: {roster}.{tail}{extra}'.format(
            marker=MARKER_BRIEFING, name=safe_name, role=role, team=team, head=head, roster=roster, tail=tail, extra=extra,
        )

    roster = _teammate_list(others)
    line = compose(headline, roster, suffix)
    if len(line) > MAX_BRIEFING_CHARS:
        # 1. collapse the roster to the count form, when that is actually shorter
        count_form = "{} {}, run {} who".format(len(others), "teammate" if len(others) == 1 else "teammates", DEFAULT_CLI)
        if len(count_form) < len(roster):
            roster = count_form
            line = compose(headline, roster, suffix)
    if len(line) > MAX_BRIEFING_CHARS and suffix:
        # 2. drop the CLI path hint
        suffix = ""
        line = compose(headline, roster, suffix)
    if len(line) > MAX_BRIEFING_CHARS:
        # 3. trim the charter headline to whatever room is left
        overhead = len(compose("", roster, suffix))
        line = compose(_cut(headline, max(MAX_BRIEFING_CHARS - overhead, 1)), roster, suffix)
    if len(line) > MAX_BRIEFING_CHARS:
        raise NudgeTextError("briefing line exceeds {} chars".format(MAX_BRIEFING_CHARS))
    lines = [line]
    brief_text = _one_line(brief or "")
    if brief_text:
        body = _cut(brief_text, MAX_BRIEF_LINE_CHARS).rstrip(".")
        lines.append("{} Your brief: {}. Full text: {} me".format(MARKER_BRIEFING, body, DEFAULT_CLI))
    return lines


def probe_text(nonce: str) -> str:
    if not isinstance(nonce, str) or not PROBE_NONCE_RE.match(nonce):
        raise NudgeTextError("probe nonce must match {}".format(PROBE_NONCE_RE.pattern))
    return "{} {}]".format(MARKER_PROBE, nonce)


def probe_line(nonce: str) -> str:
    """Task-shaped alias of ``probe_text``."""
    return probe_text(nonce)


def new_nonce() -> int:
    return _rng.randrange(1, 1000000)


def is_echo(text: Optional[str]) -> bool:
    """True when ``text`` would echo a nudge, briefing, probe, or nonce (``echo_rejected``)."""
    if not text:
        return False
    if text.lstrip().startswith(MARKER_PREFIX):
        return True
    if NONCE_RE.search(text):
        return True
    for marker in (MARKER_NUDGE, MARKER_BRIEFING, MARKER_PROBE, MARKER_INTERRUPT):
        if marker in text:
            return True
    return False
