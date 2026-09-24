"""Team facts: findings that know when they were true, where they came from, and who disagrees (0.19).

A finding used to be a line appended to ``knowledge.jsonl`` for ever: nothing
ever became "no longer true", so a week-old price or a disproved hypothesis
reached every re-oriented agent as if it were current. A fact is a finding
with two timelines, following graphiti's model of a temporal knowledge graph
without its graph database or its per-post LLM calls:

* when it was true in the world: ``valid_from`` / ``valid_to``;
* when the team learned and unlearned it: ``recorded_at`` / ``retired_at``.

Nothing is deleted. Superseding a fact retires the old one and links the two,
so ``facts --history`` shows how the team's belief moved and ``--as-of`` shows
what it believed on a given day.

Provenance travels with every fact: the member and generation that asserted
it, board posts, dated external sources, files. When another member
independently asserts the same thing it becomes support for the existing fact
rather than a duplicate, and the number of distinct supporters is its
confidence. Facts are peer notes: they never carry the operator's authority,
whoever wrote them; the operator's rules stay in ``knowledge set``.

Contradictions. Two members asserting different values for the same subject
and attribute is a dispute. What happens next is a per-team switch
(``config.contradictions.mode``) and never touches what agents may say to each
other: posting is never refused, delayed or filtered because of a dispute.

* ``off``: nothing; both facts stay current side by side.
* ``observe`` (default): the dispute is recorded and both facts are marked
  disputed where the human looks; no agent is told.
* ``debate``: the two authors are introduced to each other's claim and settle
  it on the board; unresolved after the timeout, it goes to the manager or the
  human.
* ``escalate``: the manager, or the human, decides at once.

A member may retire or refine only its own facts; overriding a peer's needs
the manager or the operator. A resolution is itself recorded, so the
disagreement stays visible in the history.

Storage: ``<team>/facts.jsonl``, an append-only event log under ``team.lock``.
Findings written before 0.19 stay in ``knowledge.jsonl`` and are read in place
as legacy facts ``L-1``, ``L-2``, ... (no migration is written); they can be
retired and superseded like any other.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from herdr_team import store
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, UsageError
from herdr_team.paths import TeamPaths

FACTS_VERSION = 1
FACT_ID_RE = re.compile(r"^(F|L)-([1-9][0-9]{0,6})\Z")
DISPUTE_ID_RE = re.compile(r"^D-([1-9][0-9]{0,6})\Z")

MODE_OFF = "off"
MODE_OBSERVE = "observe"
MODE_DEBATE = "debate"
MODE_ESCALATE = "escalate"
MODES = (MODE_OFF, MODE_OBSERVE, MODE_DEBATE, MODE_ESCALATE)
DEFAULT_MODE = MODE_OBSERVE
DEFAULT_DEBATE_TIMEOUT_MS = 30 * 60 * 1000

MAX_STATEMENT_CHARS = 400
MAX_SUBJECT_CHARS = 120
MAX_SOURCES = 10
#: Near-duplicate threshold on character 3-gram Jaccard. graphiti uses 0.9 for entity
#: names; whole sentences differ by a digit or a word at ~0.87-0.9, and this only warns.
NEAR_DUPLICATE_JACCARD = 0.85

OP_ADD = "add"
OP_SUPPORT = "support"
OP_RETIRE = "retire"
OP_DISPUTE = "dispute"
OP_ESCALATE = "escalate"
OP_RESOLVE = "resolve"
OP_JOIN = "join"  # a further clashing fact joins the open dispute on the same subject and attribute
OPS = (OP_ADD, OP_SUPPORT, OP_RETIRE, OP_DISPUTE, OP_ESCALATE, OP_RESOLVE, OP_JOIN)


def facts_jsonl(team: TeamPaths) -> Any:
    return team.root / "facts.jsonl"


def parse_fact_id(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text.isdigit():
        text = "F-" + text
    if not FACT_ID_RE.match(text):
        raise UsageError("a fact id looks like F-12 (or L-3 for a finding recorded before 0.19)")
    return text


def parse_dispute_id(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text.isdigit():
        text = "D-" + text
    if not DISPUTE_ID_RE.match(text):
        raise UsageError("a dispute id looks like D-2")
    return text


def _num(fact_id: str) -> Tuple[int, int]:
    match = FACT_ID_RE.match(fact_id) or DISPUTE_ID_RE.match(fact_id)
    if not match:
        return (9, 0)
    if match.re is DISPUTE_ID_RE:
        return (2, int(match.group(1)))
    return (0 if match.group(1) == "L" else 1, int(match.group(2)))


# --------------------------------------------------------------------------
# time


def parse_when(value: Any, what: str = "date") -> Optional[str]:
    """``2026-09-20``, ``2026-09-20T14:00``, or a full ISO timestamp -> ISO UTC with ``Z``."""
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M%z"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(parsed.microsecond // 1000)
    raise UsageError("{} must look like 2026-09-20 or 2026-09-20T14:00".format(what))


def epoch(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# normalisation and near-duplicates


def normalize(text: Any) -> str:
    """Lower case, one space, no surrounding punctuation: what "the same words" means here."""
    flat = " ".join(str(text or "").lower().split())
    return flat.strip(" .,;:!?\"'`()[]{}")


def shingles(text: str, n: int = 3) -> Set[str]:
    norm = normalize(text)
    if len(norm) < n:
        return {norm} if norm else set()
    return {norm[i:i + n] for i in range(len(norm) - n + 1)}


def clip(text: Any, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "\u2026"


def jaccard(a: str, b: str) -> float:
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


# --------------------------------------------------------------------------
# the derived state


@dataclass
class Fact:
    id: str
    statement: str
    author: str
    recorded_at: str
    author_kind: Optional[str] = None
    author_gen: Optional[int] = None
    about: Optional[str] = None
    attribute: Optional[str] = None
    type: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    retired_at: Optional[str] = None
    retired_by: Optional[str] = None
    retire_reason: Optional[str] = None
    supersedes: Optional[str] = None
    superseded_by: Optional[str] = None
    sources: List[Dict[str, Any]] = field(default_factory=list)
    supporters: List[Dict[str, Any]] = field(default_factory=list)
    disputes: List[str] = field(default_factory=list)
    legacy: bool = False

    @property
    def current(self) -> bool:
        return self.retired_at is None

    @property
    def members(self) -> List[str]:
        """Distinct members who stand behind it: the author, then each supporter."""
        out = [self.author]
        for s in self.supporters:
            if s.get("by") and s["by"] not in out:
                out.append(str(s["by"]))
        return out

    def status(self, disputes: Dict[str, "Dispute"]) -> str:
        if self.superseded_by:
            return "superseded"
        if self.retired_at:
            return "retired"
        if any(disputes.get(d) is not None and disputes[d].open for d in self.disputes):
            return "disputed"
        return "current"

    def believed_at(self, when: float) -> bool:
        """The team held it then, and it was about something true then."""
        recorded = epoch(self.recorded_at)
        retired = epoch(self.retired_at)
        if recorded is not None and recorded > when:
            return False
        if retired is not None and retired <= when:
            return False
        start = epoch(self.valid_from)
        end = epoch(self.valid_to)
        if start is not None and start > when:
            return False
        if end is not None and end <= when:
            return False
        return True

    def confidence(self) -> str:
        members = len(self.members)
        external = len([s for s in self.sources if s.get("kind") == "url"])
        return "{} member{}, {} source{}".format(members, "" if members == 1 else "s", external, "" if external == 1 else "s")

    def label(self) -> str:
        subject = ""
        if self.about:
            subject = str(self.about) + (" · " + str(self.attribute) if self.attribute else "") + ": "
        return subject + str(self.statement)

    def to_json(self, disputes: Optional[Dict[str, "Dispute"]] = None) -> Dict[str, Any]:
        return {
            "id": self.id, "statement": self.statement, "about": self.about, "attribute": self.attribute, "type": self.type,
            "author": self.author, "author_kind": self.author_kind, "author_gen": self.author_gen,
            "recorded_at": self.recorded_at, "valid_from": self.valid_from, "valid_to": self.valid_to,
            "retired_at": self.retired_at, "retired_by": self.retired_by, "retire_reason": self.retire_reason,
            "supersedes": self.supersedes, "superseded_by": self.superseded_by,
            "sources": list(self.sources), "supporters": list(self.supporters), "members": self.members,
            "confidence": self.confidence(), "disputes": list(self.disputes), "legacy": self.legacy,
            "status": self.status(disputes or {}),
        }


@dataclass
class Dispute:
    id: str
    facts: List[str]
    opened_at: str
    mode: str
    about: Optional[str] = None
    attribute: Optional[str] = None
    escalated_at: Optional[str] = None
    escalated_to: Optional[str] = None
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None
    keep: List[str] = field(default_factory=list)
    reason: Optional[str] = None

    @property
    def open(self) -> bool:
        return self.resolved_at is None

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id, "facts": list(self.facts), "opened_at": self.opened_at, "mode": self.mode,
            "about": self.about, "attribute": self.attribute, "escalated_at": self.escalated_at,
            "escalated_to": self.escalated_to, "resolved_at": self.resolved_at, "resolved_by": self.resolved_by,
            "keep": list(self.keep), "reason": self.reason, "open": self.open,
        }


@dataclass
class State:
    facts: Dict[str, Fact] = field(default_factory=dict)
    disputes: Dict[str, Dispute] = field(default_factory=dict)

    def ordered(self) -> List[Fact]:
        return sorted(self.facts.values(), key=lambda f: _num(f.id))

    def current(self) -> List[Fact]:
        return [f for f in self.ordered() if f.current]

    def open_disputes(self) -> List[Dispute]:
        return [d for d in sorted(self.disputes.values(), key=lambda d: _num(d.id)) if d.open]


def _legacy(team: TeamPaths) -> List[Fact]:
    """``knowledge.jsonl`` findings as read-only facts ``L-n``, in file order."""
    raw = store.read_bytes(team.knowledge_jsonl, b"") or b""
    out: List[Fact] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or not isinstance(record.get("text"), str):
            continue
        out.append(Fact(
            id="L-{}".format(len(out) + 1), statement=record["text"], author=str(record.get("author") or "?"),
            recorded_at=str(record.get("at") or ""), author_kind=record.get("kind"), legacy=True,
        ))
    return out


def read_events(team: TeamPaths) -> List[Dict[str, Any]]:
    raw = store.read_bytes(facts_jsonl(team), b"") or b""
    out: List[Dict[str, Any]] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("op") in OPS:
            out.append(event)
    return out


def _retire(fact: Fact, at: str, by: str, reason: Optional[str], superseded_by: Optional[str] = None, valid_to: Optional[str] = None) -> None:
    if fact.retired_at is not None:
        return
    fact.retired_at = at
    fact.retired_by = by
    fact.retire_reason = reason
    if superseded_by:
        fact.superseded_by = superseded_by
    if valid_to and not fact.valid_to:
        fact.valid_to = valid_to


def _auto_resolve(state: State, at: str, by: Optional[str] = None, conceded: bool = True) -> None:
    """A dispute with at most one of its facts still current is settled.

    By a concession when the fact that went was retired by its own author;
    otherwise by whoever retired it (the manager or the operator overriding),
    so the history never calls an override a concession.
    """
    for dispute in state.disputes.values():
        if not dispute.open:
            continue
        live = [fid for fid in dispute.facts if fid in state.facts and state.facts[fid].current]
        if len(live) <= 1:
            dispute.resolved_at = at
            dispute.resolved_by = "concession" if conceded or not by else by
            dispute.keep = live


def apply(state: State, event: Dict[str, Any]) -> None:
    """Replay one event; lenient, so a log from another version never breaks reading."""
    op = event.get("op")
    at = str(event.get("at") or "")
    by = str(event.get("by") or "?")
    if op == OP_ADD:
        fact_id = event.get("id")
        if not isinstance(fact_id, str):
            return
        def text(key: str) -> Optional[str]:
            value = event.get(key)
            return value if isinstance(value, str) and value else None

        fact = Fact(
            id=fact_id, statement=str(event.get("statement") or ""), author=by, recorded_at=at,
            author_kind=text("by_kind"), author_gen=event.get("by_gen") if isinstance(event.get("by_gen"), int) else None,
            about=text("about"), attribute=text("attribute"), type=text("type"),
            valid_from=text("valid_from"), valid_to=text("valid_to"), supersedes=text("supersedes"),
            sources=[s for s in event.get("sources") or [] if isinstance(s, dict)] if isinstance(event.get("sources"), list) else [],
        )
        state.facts[fact_id] = fact
        old = state.facts.get(fact.supersedes) if fact.supersedes else None
        if old is not None:
            _retire(old, at, by, "superseded by {}".format(fact_id), superseded_by=fact_id, valid_to=fact.valid_from or at)
            _auto_resolve(state, at, by, conceded=old.author == by)
    elif op == OP_SUPPORT:
        fact = state.facts.get(str(event.get("id")))
        if fact is not None:
            fact.supporters.append({"by": by, "at": at, "gen": event.get("by_gen"), "sources": [s for s in event.get("sources") or [] if isinstance(s, dict)]})
            fact.sources.extend(s for s in event.get("sources") or [] if isinstance(s, dict))
    elif op == OP_RETIRE:
        fact = state.facts.get(str(event.get("id")))
        if fact is not None:
            _retire(fact, at, by, event.get("reason"), valid_to=event.get("valid_to"))
            _auto_resolve(state, at, by, conceded=fact.author == by)
    elif op == OP_DISPUTE:
        dispute_id = event.get("id")
        if not isinstance(dispute_id, str):
            return
        dispute = Dispute(id=dispute_id, facts=[f for f in event.get("facts") or [] if isinstance(f, str)], opened_at=at,
                          mode=str(event.get("mode") or DEFAULT_MODE), about=event.get("about"), attribute=event.get("attribute"))
        state.disputes[dispute_id] = dispute
        for fid in dispute.facts:
            if fid in state.facts and dispute_id not in state.facts[fid].disputes:
                state.facts[fid].disputes.append(dispute_id)
    elif op == OP_ESCALATE:
        dispute = state.disputes.get(str(event.get("id")))
        if dispute is not None:
            dispute.escalated_at = at
            dispute.escalated_to = event.get("to")
    elif op == OP_RESOLVE:
        dispute = state.disputes.get(str(event.get("id")))
        if dispute is None:
            return
        keep = [f for f in event.get("keep") or [] if isinstance(f, str)]
        dispute.resolved_at = at
        dispute.resolved_by = by
        dispute.keep = keep
        dispute.reason = event.get("reason")
        for fid in dispute.facts:
            if fid not in keep and fid in state.facts:
                _retire(state.facts[fid], at, by, "resolved {}: {}".format(dispute.id, event.get("reason") or "not kept"))
        _auto_resolve(state, at, by, conceded=False)
    elif op == OP_JOIN:
        dispute = state.disputes.get(str(event.get("id")))
        fact_id = event.get("fact")
        if dispute is not None and isinstance(fact_id, str) and fact_id not in dispute.facts:
            dispute.facts.append(fact_id)
            if fact_id in state.facts and dispute.id not in state.facts[fact_id].disputes:
                state.facts[fact_id].disputes.append(dispute.id)


def load(team: TeamPaths) -> State:
    state = State()
    for fact in _legacy(team):
        state.facts[fact.id] = fact
    for event in read_events(team):
        apply(state, event)
    return state


def next_fact_id(state: State) -> str:
    return "F-{}".format(max([_num(f)[1] for f in state.facts if f.startswith("F-")] + [0]) + 1)


def next_dispute_id(state: State) -> str:
    return "D-{}".format(max([_num(d)[1] for d in state.disputes] + [0]) + 1)


# --------------------------------------------------------------------------
# policy


def contradictions_config(doc: Dict[str, Any]) -> Dict[str, Any]:
    config = doc.get("config") if isinstance(doc.get("config"), dict) else {}
    raw = config.get("contradictions") if isinstance(config.get("contradictions"), dict) else {}
    mode = raw.get("mode") if raw.get("mode") in MODES else DEFAULT_MODE
    try:
        timeout_ms = int(raw.get("debate_timeout_ms") or DEFAULT_DEBATE_TIMEOUT_MS)
    except (TypeError, ValueError):
        timeout_ms = DEFAULT_DEBATE_TIMEOUT_MS
    return {"mode": mode, "debate_timeout_ms": max(60_000, timeout_ms)}


def vocabulary(doc: Dict[str, Any]) -> Dict[str, str]:
    """Entity types a template declared (``config.vocabulary``); empty means any label is fine."""
    config = doc.get("config") if isinstance(doc.get("config"), dict) else {}
    raw = config.get("vocabulary")
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if isinstance(k, str)}
    return {}


# --------------------------------------------------------------------------
# writing


def _append(team: TeamPaths, event: Dict[str, Any]) -> None:
    event.setdefault("v", FACTS_VERSION)
    store.append_line(facts_jsonl(team), json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


@dataclass
class AddResult:
    fact: Optional[Fact]
    supported: Optional[Fact] = None  # the exact statement already existed: recorded as support instead
    superseded: Optional[Fact] = None
    near_duplicates: List[Fact] = field(default_factory=list)
    dispute: Optional[Dispute] = None
    state: Optional[State] = None


def conflicting(state: State, about: Optional[str], attribute: Optional[str], statement: str, author: str, exclude: Iterable[str] = ()) -> List[Fact]:
    """Current facts by other members that give a different statement for the same subject and attribute."""
    if not about or not attribute:
        return []
    skip = set(exclude)
    out = []
    for fact in state.current():
        if fact.id in skip or fact.author == author or fact.legacy:
            continue
        if normalize(fact.about) == normalize(about) and normalize(fact.attribute) == normalize(attribute) and normalize(fact.statement) != normalize(statement):
            out.append(fact)
    return out


def add(team: TeamPaths, fields: Dict[str, Any], mode: str, may_override: bool = False) -> AddResult:
    """Record a fact under the team lock: support, supersede, near-duplicate and dispute handling in one step.

    ``fields`` carries ``statement``, ``by``, ``by_kind``, ``by_gen`` and the
    optional ``about``, ``attribute``, ``type``, ``valid_from``, ``valid_to``,
    ``sources``, ``supersedes``. ``may_override`` is the caller's authority to
    supersede a fact another member wrote (the manager or the operator).
    """
    by = str(fields["by"])
    with store.team_lock(team):
        state = load(team)
        statement = str(fields["statement"])
        supersedes = fields.get("supersedes")
        if supersedes:
            old = state.facts.get(supersedes)
            if old is None:
                raise HerdrTeamError("fact_not_found", "no fact {}".format(supersedes), EXIT_REFUSED, {"id": supersedes})
            if not old.current:
                raise HerdrTeamError("fact_not_current", "{} is already {}".format(old.id, old.status(state.disputes)), EXIT_REFUSED, {"id": old.id})
            if old.author != by and not may_override:
                raise HerdrTeamError("fact_not_yours", "{} was recorded by {}; only it, the manager or the operator can supersede it. Disagree with a fact of your own instead.".format(old.id, old.author),
                                     EXIT_REFUSED, {"id": old.id, "author": old.author})
            twin = next((f for f in state.current() if f.id != old.id and normalize(f.statement) == normalize(statement)
                         and normalize(f.about) == normalize(fields.get("about")) and normalize(f.attribute) == normalize(fields.get("attribute"))), None)
            if twin is not None:
                raise HerdrTeamError("fact_duplicate", "{} already says that; support it (fact support {}) and retire {} instead".format(twin.id, twin.id, old.id),
                                     EXIT_REFUSED, {"id": twin.id, "supersedes": old.id})
        # the same words from another member are support, not a second fact
        if not supersedes:
            for fact in state.current():
                if normalize(fact.statement) == normalize(statement) and normalize(fact.about) == normalize(fields.get("about")) and normalize(fact.attribute) == normalize(fields.get("attribute")):
                    if fact.author == by or any(s.get("by") == by for s in fact.supporters):
                        return AddResult(fact=None, supported=fact, state=state)
                    _append(team, {"op": OP_SUPPORT, "id": fact.id, "at": store.now_iso(), "by": by, "by_gen": fields.get("by_gen"), "sources": fields.get("sources") or []})
                    state = load(team)
                    return AddResult(fact=None, supported=state.facts[fact.id], state=state)
        # the same member refining its own claim about the same subject and attribute supersedes it
        if not supersedes and fields.get("about") and fields.get("attribute"):
            for fact in state.current():
                if fact.author == by and not fact.legacy and normalize(fact.about) == normalize(fields["about"]) and normalize(fact.attribute) == normalize(fields["attribute"]):
                    supersedes = fact.id
                    break
        near = [f for f in state.current() if f.id != supersedes and jaccard(f.statement, statement) >= NEAR_DUPLICATE_JACCARD]
        fact_id = next_fact_id(state)
        event = {"op": OP_ADD, "id": fact_id, "at": store.now_iso(), "by": by, "by_kind": fields.get("by_kind"), "by_gen": fields.get("by_gen"),
                 "statement": statement}
        for key in ("about", "attribute", "type", "valid_from", "valid_to"):
            if fields.get(key):
                event[key] = fields[key]
        if fields.get("sources"):
            event["sources"] = fields["sources"]
        if supersedes:
            event["supersedes"] = supersedes
        _append(team, event)
        state = load(team)
        clashing = conflicting(state, fields.get("about"), fields.get("attribute"), statement, by, exclude=[fact_id])
        dispute = None
        if clashing and mode != MODE_OFF:
            # One open dispute per subject and attribute: a third view joins it rather than
            # opening an overlapping one that a later resolution would leave behind.
            ongoing = next((d for d in state.open_disputes()
                            if normalize(d.about) == normalize(fields.get("about")) and normalize(d.attribute) == normalize(fields.get("attribute"))), None)
            if ongoing is not None:
                for fid in [f.id for f in clashing if f.id not in ongoing.facts] + [fact_id]:
                    _append(team, {"op": OP_JOIN, "id": ongoing.id, "fact": fid, "at": store.now_iso()})
                state = load(team)
                dispute = state.disputes[ongoing.id]
            else:
                dispute_id = next_dispute_id(state)
                _append(team, {"op": OP_DISPUTE, "id": dispute_id, "at": store.now_iso(), "facts": [f.id for f in clashing] + [fact_id],
                               "mode": mode, "about": fields.get("about"), "attribute": fields.get("attribute")})
                state = load(team)
                dispute = state.disputes[dispute_id]
        superseded = state.facts.get(supersedes) if supersedes else None
        return AddResult(fact=state.facts[fact_id], superseded=superseded, near_duplicates=near, dispute=dispute, state=state)


def support(team: TeamPaths, fact_id: str, by: str, by_gen: Optional[int], sources: List[Dict[str, Any]]) -> Fact:
    with store.team_lock(team):
        state = load(team)
        fact = state.facts.get(fact_id)
        if fact is None:
            raise HerdrTeamError("fact_not_found", "no fact {}".format(fact_id), EXIT_REFUSED, {"id": fact_id})
        if not fact.current:
            raise HerdrTeamError("fact_not_current", "{} is {}".format(fact_id, fact.status(state.disputes)), EXIT_REFUSED, {"id": fact_id})
        if (fact.author == by or any(s.get("by") == by for s in fact.supporters)) and not sources:
            return fact
        _append(team, {"op": OP_SUPPORT, "id": fact_id, "at": store.now_iso(), "by": by, "by_gen": by_gen, "sources": sources})
        return load(team).facts[fact_id]


def retire(team: TeamPaths, fact_id: str, by: str, reason: Optional[str], may_override: bool, valid_to: Optional[str] = None) -> Tuple[Fact, List[Dispute]]:
    """Retire a fact; returns it and any dispute this concession settled."""
    with store.team_lock(team):
        state = load(team)
        fact = state.facts.get(fact_id)
        if fact is None:
            raise HerdrTeamError("fact_not_found", "no fact {}".format(fact_id), EXIT_REFUSED, {"id": fact_id})
        if not fact.current:
            raise HerdrTeamError("fact_not_current", "{} is already {}".format(fact_id, fact.status(state.disputes)), EXIT_REFUSED, {"id": fact_id})
        if fact.author != by and not may_override:
            raise HerdrTeamError("fact_not_yours", "{} was recorded by {}; you may retire only your own facts".format(fact_id, fact.author), EXIT_REFUSED, {"id": fact_id, "author": fact.author})
        was_open = {d.id for d in state.open_disputes()}
        event: Dict[str, Any] = {"op": OP_RETIRE, "id": fact_id, "at": store.now_iso(), "by": by}
        if reason:
            event["reason"] = reason
        if valid_to:
            event["valid_to"] = valid_to
        _append(team, event)
        state = load(team)
        settled = [state.disputes[d] for d in was_open if not state.disputes[d].open]
        return state.facts[fact_id], settled


def resolve(team: TeamPaths, dispute_id: str, by: str, keep: List[str], reason: Optional[str]) -> Dispute:
    with store.team_lock(team):
        state = load(team)
        dispute = state.disputes.get(dispute_id)
        if dispute is None:
            raise HerdrTeamError("dispute_not_found", "no dispute {}".format(dispute_id), EXIT_REFUSED, {"id": dispute_id})
        if not dispute.open:
            raise HerdrTeamError("dispute_resolved", "{} was already resolved by {}".format(dispute_id, dispute.resolved_by), EXIT_REFUSED, {"id": dispute_id})
        unknown = [f for f in keep if f not in dispute.facts]
        if unknown:
            raise HerdrTeamError("fact_not_in_dispute", "{} is not part of {} ({})".format(", ".join(unknown), dispute_id, ", ".join(dispute.facts)), EXIT_REFUSED, {"id": dispute_id, "facts": dispute.facts})
        event: Dict[str, Any] = {"op": OP_RESOLVE, "id": dispute_id, "at": store.now_iso(), "by": by, "keep": keep}
        if reason:
            event["reason"] = reason
        _append(team, event)
        return load(team).disputes[dispute_id]


def escalate(team: TeamPaths, dispute_id: str, to: str) -> Optional[Dispute]:
    """Mark a debated dispute escalated; None when it was settled or escalated meanwhile."""
    with store.team_lock(team):
        state = load(team)
        dispute = state.disputes.get(dispute_id)
        if dispute is None or not dispute.open or dispute.escalated_at:
            return None
        _append(team, {"op": OP_ESCALATE, "id": dispute_id, "at": store.now_iso(), "to": to})
        return load(team).disputes[dispute_id]


def due_for_escalation(state: State, timeout_ms: int, now: Optional[float] = None) -> List[Dispute]:
    now = time.time() if now is None else now
    out = []
    for dispute in state.open_disputes():
        if dispute.mode != MODE_DEBATE or dispute.escalated_at:
            continue
        opened = epoch(dispute.opened_at)
        if opened is not None and (now - opened) * 1000.0 >= timeout_ms:
            out.append(dispute)
    return out


# --------------------------------------------------------------------------
# sources


_URL_RE = re.compile(r"^https?://\S+$")


def url_source(raw: str) -> Dict[str, Any]:
    """``URL`` or ``URL@2026-09-20``: an external source with the day it was read."""
    text = str(raw or "").strip()
    retrieved = None
    if "@" in text and not text.rsplit("@", 1)[1].startswith("/"):
        head, tail = text.rsplit("@", 1)
        if re.match(r"^\d{4}-\d{2}-\d{2}", tail):
            text, retrieved = head, tail
    if not _URL_RE.match(text):
        raise UsageError("--source takes an http(s) URL, optionally URL@YYYY-MM-DD")
    return {"kind": "url", "url": text, "retrieved_at": parse_when(retrieved, "--source date") if retrieved else store.now_iso()}


def findings_view(team: TeamPaths, limit: int = 0) -> List[Dict[str, Any]]:
    """Current facts in the old findings shape (``at``, ``author``, ``kind``, ``text``) for existing readers."""
    state = load(team)
    rows = []
    for fact in state.current():
        marker = " [disputed {}]".format(", ".join(d for d in fact.disputes if state.disputes.get(d) and state.disputes[d].open)) if fact.status(state.disputes) == "disputed" else ""
        # ``text`` stays the plain statement every existing reader expects; the id and any open
        # dispute ride alongside for the readers that know about facts.
        rows.append({"at": fact.recorded_at, "author": fact.author, "kind": fact.author_kind, "id": fact.id,
                     "text": fact.label(), "display": "[{}] {}{}".format(fact.id, fact.label(), marker), "status": fact.status(state.disputes)})
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    return rows


def counts(team: TeamPaths) -> Dict[str, int]:
    state = load(team)
    current = state.current()
    return {"current": len(current), "disputed": len([f for f in current if f.status(state.disputes) == "disputed"]), "open_disputes": len(state.open_disputes())}
