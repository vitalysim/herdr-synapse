"""Work items: what a team tracks, next to the board it talks on (0.19).

A board post says something; a work item is something that has to end. It has
a title, a brief that says what "done" looks like, an owner, dependencies on
other items, optional reviewers, and attempts. The board stays the only place
agents talk: every change to an item is also an ordinary authored post (a
``request`` to the owner, a ``done`` to the requester, ...) carrying a ``work``
field, so delivery, the idle gates, the Stop hook and the ask popup treat work
exactly like the posts they already know.

Storage is ``<team>/work.jsonl``: an append-only event log written under
``team.lock``, one JSON object per line. The current state is derived by
replaying it, the way cursors are derived from the board. Nothing is ever
rewritten, so the history of an item (who claimed it, what each attempt
settled with, what the reviewer said) is the file itself.

Three rules carry the design:

* **The brief contract.** An item says Target, Deliverable, Constraints,
  Ownership and Acceptance: what counts as done is written before anyone
  starts. Acceptance is domain-neutral evidence ("two dated sources per
  claim", "legal approved", "tests pass").
* **The settlement contract.** An attempt ends once, with an explicit outcome
  (``succeeded``, ``failed`` or ``partial``) and a short summary. Failure is
  never left in prose.
* **Generation fencing.** An attempt records the owner's generation when it
  was claimed. A cleared, restarted or swapped member is a new generation with
  no memory of the claim, so it cannot settle the old attempt: it claims again
  and starts a new one, and the log says so.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from herdr_team import store
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, UsageError
from herdr_team.paths import TeamPaths

WORK_VERSION = 1
WORK_ID_RE = re.compile(r"^W-([1-9][0-9]{0,6})\Z")

STATUS_OPEN = "open"
STATUS_ASSIGNED = "assigned"
STATUS_IN_PROGRESS = "in_progress"
STATUS_BLOCKED = "blocked"
STATUS_IN_REVIEW = "in_review"
STATUS_CHANGES = "changes_requested"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_PARTIAL = "partial"
STATUS_CANCELLED = "cancelled"
STATUSES = (
    STATUS_OPEN, STATUS_ASSIGNED, STATUS_IN_PROGRESS, STATUS_BLOCKED, STATUS_IN_REVIEW,
    STATUS_CHANGES, STATUS_DONE, STATUS_FAILED, STATUS_PARTIAL, STATUS_CANCELLED,
)
#: Nothing more happens to these unless somebody reopens them.
FINAL = (STATUS_DONE, STATUS_CANCELLED)
#: An attempt ended here and a person (requester, manager, operator) decides what next.
SETTLED_NEEDS_DECISION = (STATUS_FAILED, STATUS_PARTIAL)
#: Where an owner is working on it.
ACTIVE = (STATUS_IN_PROGRESS, STATUS_BLOCKED, STATUS_CHANGES)

OUTCOMES = ("succeeded", "failed", "partial")
VERDICTS = ("approved", "changes")
BRIEF_FIELDS = ("target", "deliverable", "constraints", "ownership", "acceptance")
ACCEPTANCE_POLICIES = ("off", "warn", "require")
DEFAULT_ACCEPTANCE_POLICY = "warn"

MAX_TITLE_CHARS = 200
MAX_FIELD_CHARS = 1000
MAX_SUMMARY_CHARS = 800
MAX_LIST_ITEMS = 20
#: An attempt this long without a word from its owner is worth a look.
QUIET_AFTER_S = 2 * 3600

OP_CREATE = "create"
OP_ASSIGN = "assign"
OP_CLAIM = "claim"
OP_BLOCK = "block"
OP_UNBLOCK = "unblock"
OP_SETTLE = "settle"
OP_REVIEW = "review"
OP_REOPEN = "reopen"
OP_CLOSE = "close"
OP_CANCEL = "cancel"
OP_UPDATE = "update"
OPS = (OP_CREATE, OP_ASSIGN, OP_CLAIM, OP_BLOCK, OP_UNBLOCK, OP_SETTLE, OP_REVIEW, OP_REOPEN, OP_CLOSE, OP_CANCEL, OP_UPDATE)


def work_jsonl(team: TeamPaths) -> Any:
    return team.root / "work.jsonl"


def parse_id(value: Any) -> str:
    """``W-4``, ``w-4`` or ``4`` -> ``W-4``; anything else is a usage error."""
    text = str(value or "").strip()
    if text.isdigit():
        text = "W-" + text
    text = text[:1].upper() + text[1:]
    if not WORK_ID_RE.match(text):
        raise UsageError("a work id looks like W-4")
    return text


def id_number(work_id: str) -> int:
    match = WORK_ID_RE.match(work_id)
    return int(match.group(1)) if match else 0


# --------------------------------------------------------------------------
# the derived state


@dataclass
class Attempt:
    n: int
    owner: str
    gen: int
    claimed_at: str
    settled_at: Optional[str] = None
    outcome: Optional[str] = None
    summary: Optional[str] = None
    deliverables: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    submissions: int = 0
    closed: bool = False  # reopened, reassigned or cancelled: nothing more settles here

    def to_json(self) -> Dict[str, Any]:
        return {
            "n": self.n, "owner": self.owner, "gen": self.gen, "claimed_at": self.claimed_at,
            "settled_at": self.settled_at, "outcome": self.outcome, "summary": self.summary,
            "deliverables": list(self.deliverables), "evidence": list(self.evidence),
            "submissions": self.submissions, "closed": self.closed,
        }


@dataclass
class Item:
    id: str
    title: str
    requester: str
    created_at: str
    brief: Dict[str, str] = field(default_factory=dict)
    owner: Optional[str] = None
    deps: List[str] = field(default_factory=list)
    review_by: List[str] = field(default_factory=list)
    status: str = STATUS_OPEN
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None
    block_reason: Optional[str] = None
    blocked_from: Optional[str] = None  # the status a block interrupted; unblock returns to it
    attempts: List[Attempt] = field(default_factory=list)
    reviews: List[Dict[str, Any]] = field(default_factory=list)
    history: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def attempt(self) -> Optional[Attempt]:
        """The live attempt, if one is open."""
        if self.attempts and not self.attempts[-1].closed:
            return self.attempts[-1]
        return None

    @property
    def summary(self) -> Optional[str]:
        """The thread brief: the newest settlement summary."""
        for attempt in reversed(self.attempts):
            if attempt.summary:
                return attempt.summary
        return None

    def to_json(self) -> Dict[str, Any]:
        current = self.attempt
        return {
            "id": self.id, "title": self.title, "status": self.status, "owner": self.owner,
            "requester": self.requester, "deps": list(self.deps), "review_by": list(self.review_by),
            "brief": dict(self.brief), "created_at": self.created_at, "updated_at": self.updated_at,
            "updated_by": self.updated_by, "block_reason": self.block_reason,
            "attempt": current.to_json() if current is not None else None,
            "attempts": [a.to_json() for a in self.attempts], "reviews": list(self.reviews),
            "summary": self.summary,
        }


def _int(value: Any, default: int) -> int:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else default
    except (TypeError, ValueError):
        return default


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _strs(value: Any) -> List[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _apply(items: Dict[str, Item], event: Dict[str, Any]) -> None:
    """Replay one event. Lenient: a log written by an older or newer version never breaks reading."""
    op = event.get("op")
    work_id = event.get("id")
    at = str(event.get("at") or "")
    by = str(event.get("by") or "?")
    if not isinstance(work_id, str):
        return
    if op == OP_CREATE:
        items[work_id] = Item(
            id=work_id, title=str(event.get("title") or ""), requester=by, created_at=at,
            brief={k: str(v) for k, v in _dict(event.get("brief")).items() if k in BRIEF_FIELDS and v},
            owner=event.get("owner") if isinstance(event.get("owner"), str) else None,
            deps=_strs(event.get("deps")),
            review_by=_strs(event.get("review_by")),
            status=STATUS_ASSIGNED if isinstance(event.get("owner"), str) else STATUS_OPEN,
            updated_at=at, updated_by=by,
        )
        items[work_id].history.append({"op": op, "at": at, "by": by})
        return
    item = items.get(work_id)
    if item is None:
        return
    item.updated_at = at
    item.updated_by = by
    entry: Dict[str, Any] = {"op": op, "at": at, "by": by}
    current = item.attempt
    if op == OP_ASSIGN:
        if current is not None:
            current.closed = True
        item.owner = event.get("owner") if isinstance(event.get("owner"), str) else None
        item.status = STATUS_ASSIGNED if item.owner else STATUS_OPEN
        item.block_reason = None
        entry["owner"] = item.owner
    elif op == OP_CLAIM:
        if current is not None:
            current.closed = True
        item.owner = by
        n = len(item.attempts) + 1
        item.attempts.append(Attempt(n=n, owner=by, gen=_int(event.get("gen"), 1), claimed_at=at))
        item.status = STATUS_IN_PROGRESS
        item.block_reason = None
        entry.update({"attempt": n, "gen": event.get("gen")})
    elif op == OP_BLOCK:
        item.blocked_from = item.status if item.status != STATUS_BLOCKED else item.blocked_from
        item.status = STATUS_BLOCKED
        item.block_reason = str(event.get("reason") or "")
        entry["reason"] = item.block_reason
    elif op == OP_UNBLOCK:
        fallback = STATUS_IN_PROGRESS if current is not None else (STATUS_ASSIGNED if item.owner else STATUS_OPEN)
        item.status = item.blocked_from if item.blocked_from in (STATUS_IN_PROGRESS, STATUS_ASSIGNED, STATUS_CHANGES) else fallback
        item.blocked_from = None
        item.block_reason = None
    elif op == OP_SETTLE:
        if current is not None:
            current.settled_at = at
            current.outcome = str(event.get("outcome") or "")
            current.summary = str(event.get("summary") or "")
            current.deliverables = [str(d) for d in event.get("deliverables") or [] if d is not None] if isinstance(event.get("deliverables"), list) else []
            current.evidence = [str(e) for e in event.get("evidence") or [] if e is not None] if isinstance(event.get("evidence"), list) else []
            current.submissions += 1
        outcome = event.get("outcome")
        if outcome == "succeeded":
            item.status = STATUS_IN_REVIEW if item.review_by else STATUS_DONE
        elif outcome == "partial":
            item.status = STATUS_PARTIAL
        else:
            item.status = STATUS_FAILED
        item.block_reason = None
        entry.update({"attempt": current.n if current else None, "outcome": outcome})
    elif op == OP_REVIEW:
        verdict = event.get("verdict")
        item.reviews.append({"at": at, "by": by, "verdict": verdict, "note": event.get("note")})
        item.status = STATUS_DONE if verdict == "approved" else STATUS_CHANGES
        entry.update({"verdict": verdict})
    elif op == OP_REOPEN:
        if current is not None:
            current.closed = True
        item.status = STATUS_ASSIGNED if item.owner else STATUS_OPEN
        item.block_reason = None
        entry["reason"] = event.get("reason")
    elif op == OP_CLOSE:
        if current is not None:
            current.closed = True
        item.status = STATUS_DONE
        entry["note"] = event.get("note")
    elif op == OP_CANCEL:
        if current is not None:
            current.closed = True
        item.status = STATUS_CANCELLED
        entry["reason"] = event.get("reason")
    elif op == OP_UPDATE:
        if isinstance(event.get("title"), str) and event["title"]:
            item.title = event["title"]
        for key, value in _dict(event.get("brief")).items():
            if key in BRIEF_FIELDS:
                if value:
                    item.brief[key] = str(value)
                else:
                    item.brief.pop(key, None)
        if isinstance(event.get("deps"), list):
            item.deps = _strs(event["deps"])
        if isinstance(event.get("review_by"), list):
            item.review_by = _strs(event["review_by"])
        entry["fields"] = sorted(k for k in event if k not in ("v", "op", "id", "at", "by", "by_via", "by_gen"))
    else:
        return
    item.history.append(entry)


def read_events(team: TeamPaths) -> List[Dict[str, Any]]:
    """Every well-formed event line; a torn or foreign line is skipped, never fatal."""
    path = work_jsonl(team)
    raw = store.read_bytes(path)
    if not raw:
        return []
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


def load(team: TeamPaths) -> Dict[str, Item]:
    items: Dict[str, Item] = {}
    for event in read_events(team):
        _apply(items, event)
    return items


def sorted_items(items: Dict[str, Item]) -> List[Item]:
    return sorted(items.values(), key=lambda i: id_number(i.id))


# --------------------------------------------------------------------------
# readiness


def deps_done(item: Item, items: Dict[str, Item]) -> bool:
    return all(items.get(dep) is not None and items[dep].status == STATUS_DONE for dep in item.deps)


def waiting_on(item: Item, items: Dict[str, Item]) -> List[str]:
    return [dep for dep in item.deps if items.get(dep) is None or items[dep].status != STATUS_DONE]


def is_ready(item: Item, items: Dict[str, Item]) -> bool:
    """Nobody is on it yet and nothing it depends on is unfinished."""
    return item.status in (STATUS_OPEN, STATUS_ASSIGNED) and deps_done(item, items)


def newly_ready(before: Dict[str, Item], after: Dict[str, Item]) -> List[Item]:
    """Items that became ready because of the change between ``before`` and ``after``."""
    out = []
    for item in sorted_items(after):
        prior = before.get(item.id)
        if is_ready(item, after) and item.deps and (prior is None or not is_ready(prior, before)):
            out.append(item)
    return out


def check_deps(items: Dict[str, Item], work_id: Optional[str], deps: Sequence[str]) -> List[str]:
    """Validated dependency ids: known, not itself, and no cycle."""
    out: List[str] = []
    for dep in deps:
        dep = parse_id(dep)
        if dep == work_id:
            raise HerdrTeamError("work_dep_invalid", "{} cannot depend on itself".format(dep), EXIT_REFUSED, {"id": work_id})
        if dep not in items:
            raise HerdrTeamError("work_not_found", "no work item {}".format(dep), EXIT_REFUSED, {"id": dep, "known": sorted(items, key=id_number)[-10:]})
        if dep not in out:
            out.append(dep)
    if work_id is not None:
        # a cycle would leave every member of it waiting for ever
        seen = set()
        stack = list(out)
        while stack:
            current = stack.pop()
            if current == work_id:
                raise HerdrTeamError("work_dep_cycle", "that would make {} depend on itself".format(work_id), EXIT_REFUSED, {"id": work_id, "deps": out})
            if current in seen:
                continue
            seen.add(current)
            stack.extend(items[current].deps if current in items else [])
    return out


# --------------------------------------------------------------------------
# transitions: validated under the lock, then appended


def _refuse(code: str, message: str, item: Optional[Item] = None, **details: Any) -> HerdrTeamError:
    if item is not None:
        details.setdefault("id", item.id)
        details.setdefault("status", item.status)
    return HerdrTeamError(code, message, EXIT_REFUSED, details)


def validate(items: Dict[str, Item], event: Dict[str, Any]) -> None:
    """Refuse an event the current state does not allow. Actor authority is the caller's job."""
    op = event["op"]
    work_id = event["id"]
    if isinstance(event.get("deps"), list) and op in (OP_CREATE, OP_UPDATE):
        # Again here, under the lock: two concurrent updates could otherwise close a cycle.
        check_deps(items, work_id if op == OP_UPDATE else None, event["deps"])
    if op == OP_CREATE:
        if work_id in items:
            raise _refuse("work_exists", "{} already exists".format(work_id))
        return
    item = items.get(work_id)
    if item is None:
        raise _refuse("work_not_found", "no work item {}".format(work_id), id=work_id)
    by = event.get("by")
    current = item.attempt
    if op == OP_ASSIGN:
        if item.status in FINAL:
            raise _refuse("work_final", "{} is {}; reopen it first".format(item.id, item.status), item)
    elif op == OP_CLAIM:
        if item.status in FINAL or item.status in SETTLED_NEEDS_DECISION or item.status == STATUS_IN_REVIEW:
            raise _refuse("work_not_claimable", "{} is {}".format(item.id, item.status), item)
        if item.owner and item.owner != by:
            raise _refuse("work_taken", "{} belongs to {}; ask for it to be reassigned".format(item.id, item.owner), item, owner=item.owner)
        if current is not None and current.owner == by and current.gen == _int(event.get("gen"), 1) and item.status != STATUS_CHANGES and item.blocked_from != STATUS_CHANGES:
            raise _refuse("work_already_claimed", "you already hold attempt {} of {}".format(current.n, item.id), item, attempt=current.n)
        if not event.get("force") and not deps_done(item, items):
            raise _refuse("work_waiting", "{} waits on {}".format(item.id, ", ".join(waiting_on(item, items))), item, waiting_on=waiting_on(item, items))
    elif op == OP_BLOCK:
        if item.status not in (STATUS_IN_PROGRESS, STATUS_ASSIGNED, STATUS_CHANGES):
            raise _refuse("work_not_active", "only work in progress can be blocked; {} is {}".format(item.id, item.status), item)
    elif op == OP_UNBLOCK:
        if item.status != STATUS_BLOCKED:
            raise _refuse("work_not_blocked", "{} is not blocked".format(item.id), item)
    elif op == OP_SETTLE:
        if item.status not in ACTIVE or current is None:
            raise _refuse("work_not_active", "{} has no attempt in progress ({})".format(item.id, item.status), item)
        if current.owner != by:
            raise _refuse("work_not_owner", "attempt {} of {} belongs to {}".format(current.n, item.id, current.owner), item, owner=current.owner)
        gen = _int(event.get("gen"), 1)
        if current.gen != gen:
            raise _refuse(
                "attempt_fenced",
                "attempt {} of {} was claimed by generation {} of {}; you are generation {} (restarted, cleared or swapped since). "
                "Claim it again to start a new attempt: herdr-synapse work claim {}".format(current.n, item.id, current.gen, by, gen, item.id),
                item, attempt=current.n, attempt_gen=current.gen, gen=gen,
            )
        resubmitting = item.status == STATUS_CHANGES or (item.status == STATUS_BLOCKED and item.blocked_from == STATUS_CHANGES)
        if current.settled_at and not resubmitting:
            raise _refuse("work_already_settled", "attempt {} of {} already settled".format(current.n, item.id), item, attempt=current.n)
    elif op == OP_REVIEW:
        if item.status != STATUS_IN_REVIEW:
            raise _refuse("work_not_in_review", "{} is {}, not waiting for review".format(item.id, item.status), item)
    elif op == OP_REOPEN:
        if item.status not in FINAL + SETTLED_NEEDS_DECISION + (STATUS_IN_REVIEW,):
            raise _refuse("work_not_settled", "{} is {}; nothing to reopen".format(item.id, item.status), item)
    elif op == OP_CLOSE:
        if item.status not in SETTLED_NEEDS_DECISION + (STATUS_IN_REVIEW,):
            raise _refuse("work_not_settled", "only failed, partial or in-review work is closed by decision; {} is {}".format(item.id, item.status), item)
    elif op == OP_CANCEL:
        if item.status in FINAL:
            raise _refuse("work_final", "{} is already {}".format(item.id, item.status), item)
    elif op == OP_UPDATE:
        if item.status in FINAL:
            raise _refuse("work_final", "{} is {}; reopen it first".format(item.id, item.status), item)


def next_id(items: Dict[str, Item]) -> str:
    return "W-{}".format(max([id_number(i) for i in items] + [0]) + 1)


def append(team: TeamPaths, event: Dict[str, Any], lock_timeout: float = store.TEAM_LOCK_TIMEOUT_S) -> Tuple[Dict[str, Item], Dict[str, Item], Dict[str, Any]]:
    """Validate ``event`` against the current state and append it, under ``team.lock``.

    ``event["id"]`` may be ``None`` for a create: the next id is allocated
    inside the lock. Returns ``(before, after, event)``.
    """
    with store.team_lock(team, lock_timeout):
        before = load(team)
        event = dict(event)
        if event.get("op") == OP_CREATE and not event.get("id"):
            event["id"] = next_id(before)
        event.setdefault("v", WORK_VERSION)
        event.setdefault("at", store.now_iso())
        validate(before, event)
        stored = {k: v for k, v in event.items() if k != "force"}
        store.append_line(work_jsonl(team), json.dumps(stored, ensure_ascii=False, sort_keys=False, separators=(",", ":")).encode("utf-8"))
        after = load(team)
    return before, after, stored


# --------------------------------------------------------------------------
# briefs


_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*#*\s*$")
_BRIEF_ALIASES = {
    "target": "target", "scope": "target",
    "deliverable": "deliverable", "deliverables": "deliverable", "change": "deliverable", "output": "deliverable",
    "constraints": "constraints", "constraint": "constraints",
    "ownership": "ownership", "owner": "ownership", "boundaries": "ownership",
    "acceptance": "acceptance", "acceptance evidence": "acceptance", "definition of done": "acceptance", "done when": "acceptance",
}


def parse_brief_markdown(text: str) -> Dict[str, str]:
    """``## Target`` / ``## Deliverable`` / ... sections of a brief file; unknown headings are ignored."""
    out: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            current = _BRIEF_ALIASES.get(match.group(1).strip().rstrip(":").lower())
            continue
        if current is not None:
            out.setdefault(current, []).append(line)
    return {key: "\n".join(lines).strip() for key, lines in out.items() if "\n".join(lines).strip()}


def missing_brief(brief: Dict[str, str]) -> List[str]:
    return [key for key in BRIEF_FIELDS if not brief.get(key)]


# --------------------------------------------------------------------------
# attention: what should happen next, as a command someone can run


def _hint(code: str, who: str, argv: List[str], why: str, name: Optional[str] = None) -> Dict[str, Any]:
    return {"attention": code, "for": who, "name": name, "argv": argv, "why": why}


def hints(item: Item, items: Dict[str, Item], members: Dict[str, Dict[str, Any]], manager: Optional[str], now: Optional[float] = None) -> List[Dict[str, Any]]:
    """The actionable next steps for one item, each a literal ``argv`` and who should run it.

    ``members`` maps a member name to its roster record (status, generation).
    A manager agent can follow these rows mechanically instead of re-reading
    the board to decide what is stuck; the human sees the same rows.
    """
    now = time.time() if now is None else now
    decider = manager or "human"
    decider_role = "manager" if manager else "human"
    cli = "herdr-synapse"
    out: List[Dict[str, Any]] = []
    current = item.attempt
    # A dependency that was cancelled or failed will never be done: somebody has to decide.
    dead = [d for d in item.deps if items.get(d) is not None and items[d].status in (STATUS_CANCELLED, STATUS_FAILED)]
    if dead and item.status in (STATUS_OPEN, STATUS_ASSIGNED):
        out.append(_hint("dependency_dead", decider_role, [cli, "work", "update", item.id, "--deps", ",".join(d for d in item.deps if d not in dead)],
                         "waits on {} which ended {}; drop the dependency or cancel this".format(", ".join(dead), "/".join(sorted({items[d].status for d in dead}))), decider))
        return out
    if item.status == STATUS_OPEN:
        if deps_done(item, items):
            out.append(_hint("unassigned", decider_role, [cli, "work", "assign", item.id, "<member>"], "ready and nobody owns it", decider))
        else:
            out.append(_hint("waiting_on_deps", "nobody", [cli, "work", "show", item.id], "waits on {}".format(", ".join(waiting_on(item, items)))))
    elif item.status == STATUS_ASSIGNED:
        if deps_done(item, items):
            out.append(_hint("ready", "owner", [cli, "work", "claim", item.id], "assigned and ready to start", item.owner))
        else:
            out.append(_hint("waiting_on_deps", "nobody", [cli, "work", "show", item.id], "waits on {}".format(", ".join(waiting_on(item, items)))))
    elif item.status in ACTIVE and current is not None:
        owner = members.get(current.owner) or {}
        try:
            owner_gen = int(owner.get("generation") or 1)
        except (TypeError, ValueError):
            owner_gen = 1
        if owner and owner.get("status") not in ("active", "starting", None):
            out.append(_hint("owner_absent", decider_role, [cli, "resume", current.owner], "{} is {}".format(current.owner, owner.get("status")), decider))
        elif owner and owner_gen != current.gen:
            out.append(_hint("stale_attempt", "owner", [cli, "work", "claim", item.id],
                             "{} restarted since claiming attempt {} (generation {} -> {})".format(current.owner, current.n, current.gen, owner_gen), current.owner))
        if item.status == STATUS_BLOCKED:
            out.append(_hint("blocked", decider_role, [cli, "work", "show", item.id], item.block_reason or "blocked", decider))
        elif item.status == STATUS_CHANGES:
            out.append(_hint("changes_requested", "owner", [cli, "work", "show", item.id], "the reviewer asked for changes", current.owner))
        else:
            claimed = store_parse_iso(current.claimed_at)
            last = store_parse_iso(item.updated_at) or claimed
            if last is not None and now - last > QUIET_AFTER_S:
                out.append(_hint("quiet", decider_role, [cli, "board", "--from", current.owner, "--last", "5"],
                                 "no change for {:.0f} h".format((now - last) / 3600.0), decider))
    elif item.status == STATUS_IN_REVIEW:
        reviewer = item.review_by[0] if item.review_by else decider
        out.append(_hint("awaiting_review", "reviewer", [cli, "work", "review", item.id, "--approve"], "settled; waiting for {}".format(", ".join(item.review_by) or decider), reviewer))
    elif item.status in SETTLED_NEEDS_DECISION:
        out.append(_hint("settled_needs_decision", decider_role, [cli, "work", "reopen", item.id],
                         "ended {}; reopen it, or accept it with: herdr-synapse work close {}".format(item.status, item.id), decider))
    return out


def store_parse_iso(value: Any) -> Optional[float]:
    from herdr_team.roster import parse_iso

    return parse_iso(value) if isinstance(value, str) else None


def member_view(items: Dict[str, Item], name: str) -> Dict[str, List[Dict[str, Any]]]:
    """What ``me``, ``orient`` and ``who`` say about one member's work."""
    owned = [i for i in sorted_items(items) if i.owner == name and i.status not in FINAL]
    reviewing = [i for i in sorted_items(items) if i.status == STATUS_IN_REVIEW and name in i.review_by]
    requested = [i for i in sorted_items(items) if i.requester == name and i.status in SETTLED_NEEDS_DECISION]
    return {
        "owned": [{"id": i.id, "title": i.title, "status": i.status, "ready": is_ready(i, items),
                   "attempt": i.attempt.n if i.attempt else None, "attempt_gen": i.attempt.gen if i.attempt else None} for i in owned],
        "reviewing": [{"id": i.id, "title": i.title} for i in reviewing],
        "decide": [{"id": i.id, "title": i.title, "status": i.status} for i in requested],
    }


def counts(items: Dict[str, Item]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for item in items.values():
        out[item.status] = out.get(item.status, 0) + 1
    return out


def clip(text: Any, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def clean_list(values: Optional[Iterable[Any]], what: str) -> List[str]:
    out = [str(v).strip() for v in (values or []) if str(v).strip()]
    if len(out) > MAX_LIST_ITEMS:
        raise UsageError("at most {} {}".format(MAX_LIST_ITEMS, what))
    return out
